from __future__ import annotations

from collections import defaultdict
import click
import json
import logging
import time

import duckdb
from openai import OpenAI

from wactx.config import Config

log = logging.getLogger("wactx.search")

DEPTH_PRESETS = {
    "fast": {"variants": 1, "top": 10, "graph": False, "iterations": 1},
    "balanced": {"variants": 5, "top": 15, "graph": True, "iterations": 3},
    "deep": {"variants": 8, "top": 30, "graph": True, "iterations": 3},
}

QUERY_EXPANSION_PROMPT = """\
You are helping search a personal WhatsApp message database of tech founders, \
startup communities, and professional networks.

Given the user's search query, generate {n} alternative search queries that \
capture different semantic facets. Think about different phrasings, related roles, \
specific activities, and adjacent topics.

User query: "{query}"

Return ONLY a JSON array of {n} strings. No explanation, no markdown fences.
"""


def _phone_from_jid(jid: str) -> str:
    if not jid or "@" not in jid:
        return ""
    num = jid.split("@")[0]
    return f"+{num}" if num.isdigit() else ""


def expand_query(client: OpenAI, config: Config, query: str, n: int) -> list[str]:
    if n <= 1:
        return [query]
    try:
        resp = client.chat.completions.create(
            model=config.api.chat_model,
            messages=[
                {
                    "role": "user",
                    "content": QUERY_EXPANSION_PROMPT.format(query=query, n=n - 1),
                }
            ],
            max_completion_tokens=1024,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return [query] + json.loads(text)[: n - 1]
    except Exception:
        return [query]


def embed_queries(
    client: OpenAI, config: Config, queries: list[str]
) -> list[list[float]]:
    resp = client.embeddings.create(
        model=config.api.embedding_model,
        input=queries,
        dimensions=config.api.embedding_dims,
    )
    return [e.embedding for e in resp.data]


def _build_filter_clauses(
    keywords: list[str] | None,
    chat_jids: list[str] | None,
    after: str | None,
    before: str | None,
    *,
    col_prefix: str = "",
) -> tuple[list[str], list]:
    """Build parameterized SQL WHERE fragments for search filters.

    col_prefix is a column qualifier like "m." (or empty for unqualified).
    Returns (clauses, params).
    """
    clauses: list[str] = []
    params: list = []
    p = col_prefix
    for kw in keywords or []:
        clauses.append(f"{p}text_content ILIKE ?")
        params.append(f"%{kw}%")
    if chat_jids:
        placeholders = ", ".join(["?"] * len(chat_jids))
        clauses.append(f"{p}chat_jid IN ({placeholders})")
        params.extend(chat_jids)
    if after:
        clauses.append(f"{p}timestamp >= ?::TIMESTAMPTZ")
        params.append(after)
    if before:
        clauses.append(f"{p}timestamp < (?::TIMESTAMPTZ + INTERVAL '1 day')")
        params.append(before)
    return clauses, params


def resolve_chat_filter(
    conn: duckdb.DuckDBPyConnection, chat_arg: str | None
) -> list[str]:
    """Resolve a user-supplied chat argument to a list of JIDs.

    Accepts a literal JID (contains '@') or a case-insensitive substring
    match against group names / push names / full names in the contacts table.
    Returns [] when chat_arg is falsy. Raises ValueError if no match found.
    """
    if not chat_arg:
        return []
    if "@" in chat_arg:
        return [chat_arg]
    like = f"%{chat_arg}%"
    try:
        rows = conn.execute(
            """SELECT jid FROM contacts
               WHERE (is_group = true  AND group_name ILIKE ?)
                  OR (is_group = false AND (push_name ILIKE ? OR full_name ILIKE ?))""",
            [like, like, like],
        ).fetchall()
    except Exception:
        rows = []
    jids = [r[0] for r in rows]
    if not jids:
        raise ValueError(f"No chats match: {chat_arg!r}")
    return jids


def bm25_search(
    conn: duckdb.DuckDBPyConnection,
    query: str,
    top_k: int,
    *,
    keywords: list[str] | None = None,
    chat_jids: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    filter_clauses, filter_params = _build_filter_clauses(
        keywords, chat_jids, after, before, col_prefix="m."
    )
    where = " AND ".join(["score IS NOT NULL", *filter_clauses])
    sql = f"""SELECT m.id, m.text_content, m.push_name, m.sender_jid, m.chat_jid,
                     m.timestamp, m.media_type, m.media_path,
                     fts_main_messages.match_bm25(m.id, ?, fields := 'text_content') AS score
              FROM messages m
              WHERE {where}
              ORDER BY score DESC
              LIMIT ?"""
    try:
        rows = conn.execute(sql, [query, *filter_params, top_k]).fetchall()
    except Exception:
        return []

    return [
        {
            "id": r[0],
            "text": r[1],
            "sender": r[2],
            "sender_jid": r[3],
            "chat_jid": r[4],
            "time": r[5],
            "media_type": r[6],
            "media_path": r[7],
            "bm25_score": float(r[8]) if r[8] else 0.0,
        }
        for r in rows
    ]


def semantic_search(
    conn: duckdb.DuckDBPyConnection,
    vectors: list[list[float]],
    dims: int,
    top_k: int,
    *,
    keywords: list[str] | None = None,
    chat_jids: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    filter_clauses, filter_params = _build_filter_clauses(
        keywords, chat_jids, after, before
    )
    where = " AND ".join(["embedding IS NOT NULL", *filter_clauses])
    sql = f"""SELECT id, text_content, push_name, sender_jid, chat_jid, timestamp,
                    media_type, media_path,
                    array_cosine_similarity(embedding, ?::FLOAT[{dims}]) AS similarity
             FROM messages WHERE {where}
             ORDER BY similarity DESC LIMIT ?"""
    all_results: dict[str, dict] = {}
    for qvec in vectors:
        rows = conn.execute(sql, [qvec, *filter_params, top_k]).fetchall()
        for r in rows:
            mid, sim = r[0], float(r[8])
            if mid not in all_results or sim > all_results[mid]["similarity"]:
                all_results[mid] = {
                    "id": mid,
                    "text": r[1],
                    "sender": r[2],
                    "sender_jid": r[3],
                    "chat_jid": r[4],
                    "time": r[5],
                    "media_type": r[6],
                    "media_path": r[7],
                    "similarity": sim,
                }
    return sorted(all_results.values(), key=lambda x: x["similarity"], reverse=True)[
        :top_k
    ]


def rrf_fuse(rankings: list[tuple[str, list[dict]]], k: int = 60) -> list[dict]:
    scores: dict[str, float] = defaultdict(float)
    docs: dict[str, dict] = {}
    for _name, results in rankings:
        for rank, doc in enumerate(results):
            doc_id = doc["id"]
            scores[doc_id] += 1.0 / (k + rank + 1)
            if doc_id not in docs:
                docs[doc_id] = doc

    fused = []
    for doc_id, score in sorted(scores.items(), key=lambda x: -x[1]):
        doc = docs[doc_id]
        doc["rrf_score"] = score
        doc.setdefault("similarity", 0.0)
        fused.append(doc)
    return fused


def enrich_results(
    conn: duckdb.DuckDBPyConnection,
    results: list[dict],
    owner_name: str,
    use_graph: bool,
) -> list[dict]:
    from wactx.db import table_exists

    for r in results:
        row = conn.execute(
            "SELECT COALESCE(group_name, push_name, jid), is_group FROM contacts WHERE jid = ?",
            [r["chat_jid"]],
        ).fetchone()
        r["group_name"] = (row[0] if row[1] else "DM") if row else r["chat_jid"]

        row = conn.execute(
            "SELECT push_name, full_name, jid FROM contacts WHERE jid = ?",
            [r["sender_jid"]],
        ).fetchone()
        if row:
            r["display_name"] = row[1] or row[0] or row[2]
            r["phone"] = _phone_from_jid(row[2])
        else:
            r["display_name"] = r["sender"] or "?"
            r["phone"] = _phone_from_jid(r["sender_jid"])

        r.setdefault("dm_volume", 0)
        r.setdefault("shared_groups", [])
        r.setdefault("entities", [])

        if not use_graph:
            continue

        try:
            dm = conn.execute(
                """SELECT COALESCE(SUM(message_count), 0)
                   FROM edge_person_messaged epm
                   JOIN graph_persons gp ON (epm.sender_person_id = gp.person_id OR epm.receiver_person_id = gp.person_id)
                   WHERE gp.source_id = ?""",
                [r["sender_jid"]],
            ).fetchone()
            r["dm_volume"] = dm[0] if dm else 0
        except Exception:
            pass

        try:
            if owner_name:
                shared = conn.execute(
                    """SELECT LIST(DISTINCT gg.group_name)
                       FROM edge_person_in_group e1
                       JOIN edge_person_in_group e2 ON e1.group_jid = e2.group_jid
                       JOIN graph_groups gg ON e1.group_jid = gg.group_jid
                       JOIN graph_persons gp1 ON e1.person_id = gp1.person_id
                       JOIN graph_persons gp2 ON e2.person_id = gp2.person_id
                       WHERE gp1.source_id = ? AND gp2.display_name = ?""",
                    [r["sender_jid"], owner_name],
                ).fetchone()
                r["shared_groups"] = shared[0] if shared and shared[0] else []
        except Exception:
            pass

        try:
            if table_exists(conn, "edge_person_mentions_entity"):
                entities = conn.execute(
                    """SELECT ge.entity_type, ge.entity_value, epm.mention_count
                       FROM edge_person_mentions_entity epm
                       JOIN graph_entities ge ON epm.entity_id = ge.entity_id
                       JOIN graph_persons gp ON epm.person_id = gp.person_id
                       WHERE gp.source_id = ?
                       ORDER BY epm.mention_count DESC LIMIT 5""",
                    [r["sender_jid"]],
                ).fetchall()
                r["entities"] = [(t, v, c) for t, v, c in entities]
        except Exception:
            pass

    return results


def fetch_conversation_context(
    conn: duckdb.DuckDBPyConnection, results: list[dict], limit: int = 10
) -> list[dict]:
    for r in results[:limit]:
        try:
            thread = conn.execute(
                """SELECT push_name, text_content, timestamp
                   FROM messages
                   WHERE chat_jid = ?
                     AND timestamp BETWEEN ?::TIMESTAMPTZ - INTERVAL '1 hour'
                                       AND ?::TIMESTAMPTZ + INTERVAL '1 hour'
                     AND text_content IS NOT NULL
                   ORDER BY timestamp
                   LIMIT 10""",
                [r["chat_jid"], r["time"], r["time"]],
            ).fetchall()
            r["conversation_thread"] = (
                [{"sender": t[0] or "?", "text": t[1], "time": t[2]} for t in thread]
                if thread
                else []
            )
        except Exception:
            r["conversation_thread"] = []

        r["conversation_boost"] = min(1.0, len(r.get("conversation_thread", [])) / 8.0)
    return results


def find_related_people(results: list[dict]) -> list[dict]:
    by_person: dict[str, dict] = {}
    for r in results:
        jid = r["sender_jid"]
        if jid not in by_person:
            by_person[jid] = {
                "display_name": r.get("display_name", r.get("sender", "?")),
                "phone": r.get("phone", ""),
                "sender_jid": jid,
                "max_similarity": r.get("similarity", 0.0),
                "max_rrf": r.get("rrf_score", 0.0),
                "max_ppr": r.get("ppr_score", 0.0),
                "message_count": 0,
                "dm_volume": r.get("dm_volume", 0),
                "shared_groups": r.get("shared_groups", []),
                "entities": r.get("entities", []),
                "conversation_boost": r.get("conversation_boost", 0.0),
                "messages": [],
            }
        p = by_person[jid]
        p["message_count"] += 1
        p["max_similarity"] = max(p["max_similarity"], r.get("similarity", 0.0))
        p["max_rrf"] = max(p["max_rrf"], r.get("rrf_score", 0.0))
        p["max_ppr"] = max(p["max_ppr"], r.get("ppr_score", 0.0))
        p["conversation_boost"] = max(
            p["conversation_boost"], r.get("conversation_boost", 0.0)
        )
        if r.get("dm_volume", 0) > p["dm_volume"]:
            p["dm_volume"] = r["dm_volume"]
        if len(r.get("shared_groups", [])) > len(p["shared_groups"]):
            p["shared_groups"] = r["shared_groups"]
        if len(r.get("entities", [])) > len(p["entities"]):
            p["entities"] = r["entities"]
        p["messages"].append(r)

    people_list = list(by_person.values())
    if not people_list:
        return []

    max_rrf = max((p["max_rrf"] for p in people_list), default=1) or 1
    max_sim = max((p["max_similarity"] for p in people_list), default=1) or 1
    max_ppr = max((p["max_ppr"] for p in people_list), default=1) or 1e-10

    for p in people_list:
        retrieval = max(p["max_rrf"] / max_rrf, p["max_similarity"] / max_sim)

        ppr = p["max_ppr"] / max_ppr

        graph = min(
            1.0,
            (0.3 if p.get("dm_volume", 0) > 0 else 0)
            + 0.1 * min(3, len(p["shared_groups"]))
            + 0.1 * min(3, p["message_count"])
            + 0.05 * min(3, len(p["entities"])),
        )

        conv = p["conversation_boost"]

        p["score"] = 0.35 * retrieval + 0.35 * ppr + 0.15 * graph + 0.15 * conv

    return sorted(people_list, key=lambda x: x["score"], reverse=True)


def compute_graph_insights(
    conn: duckdb.DuckDBPyConnection, people: list[dict], owner_name: str
) -> dict:
    insights: dict = {
        "relationships": [],
        "connections": [],
        "relevant_topics": [],
    }
    if not people:
        return insights

    names = {p["sender_jid"]: p["display_name"][:25] for p in people}
    jids = [p["sender_jid"] for p in people[:15]]

    if not jids:
        return insights

    placeholders = ", ".join(["?"] * len(jids))

    try:
        rels = conn.execute(
            f"""
            SELECT gp1.display_name, gp2.display_name,
                   SUM(epc.exchange_count) AS exchanges,
                   LIST(DISTINCT gg.group_name) AS groups
            FROM edge_person_conversed epc
            JOIN graph_persons gp1 ON epc.person1_id = gp1.person_id
            JOIN graph_persons gp2 ON epc.person2_id = gp2.person_id
            LEFT JOIN graph_groups gg ON epc.group_jid = gg.group_jid
            WHERE gp1.source_id IN ({placeholders})
              AND gp2.source_id IN ({placeholders})
            GROUP BY gp1.display_name, gp2.display_name
            ORDER BY exchanges DESC
            LIMIT 6
        """,
            jids + jids,
        ).fetchall()

        for r in rels:
            groups = [g for g in (r[3] or []) if g][:2]
            insights["relationships"].append(
                {
                    "person1": r[0][:25],
                    "person2": r[1][:25],
                    "exchanges": r[2],
                    "groups": groups,
                }
            )
    except Exception:
        pass

    for p in people[:10]:
        if p["display_name"] in ("Self", "?"):
            continue
        parts = []
        dm_vol = p.get("dm_volume", 0)
        if dm_vol:
            parts.append(f"{dm_vol} DMs")
        shared = p.get("shared_groups", [])
        if shared:
            top = ", ".join(g[:25] for g in shared[:2])
            more = f" +{len(shared) - 2}" if len(shared) > 2 else ""
            parts.append(f"{len(shared)} groups ({top}{more})")

        try:
            conv = conn.execute(
                """
                SELECT SUM(exchange_count)
                FROM edge_person_conversed epc
                JOIN graph_persons gp1 ON epc.person1_id = gp1.person_id
                JOIN graph_persons gp2 ON epc.person2_id = gp2.person_id
                WHERE (gp1.source_id = ? AND gp2.display_name = ?)
                   OR (gp2.source_id = ? AND gp1.display_name = ?)
            """,
                [p["sender_jid"], owner_name, p["sender_jid"], owner_name],
            ).fetchone()
            if conv and conv[0]:
                parts.append(f"{conv[0]} group exchanges")
        except Exception:
            pass

        if parts:
            strength = "strong" if dm_vol > 10 else "weak" if dm_vol > 0 else "indirect"
            insights["connections"].append(
                {
                    "name": names.get(p["sender_jid"], "?"),
                    "strength": strength,
                    "details": " · ".join(parts),
                }
            )

    GENERIC_ENTITIES = {
        "AI",
        "India",
        "US",
        "USA",
        "UK",
        "Google",
        "the",
        "The",
        "a",
        "an",
        "San Francisco",
        "New York",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        "today",
        "tomorrow",
        "yesterday",
    }

    entity_people: dict[str, list[str]] = {}
    entity_counts: dict[str, int] = {}
    for p in people:
        for _, val, count in p.get("entities", []):
            if val in GENERIC_ENTITIES or len(val) < 3:
                continue
            entity_people.setdefault(val, []).append(names.get(p["sender_jid"], "?"))
            entity_counts[val] = entity_counts.get(val, 0) + (count or 1)

    insights["relevant_topics"] = [
        {"entity": e, "people": ppl, "total_mentions": entity_counts.get(e, 0)}
        for e, ppl in sorted(
            entity_people.items(), key=lambda x: -entity_counts.get(x[0], 0)
        )
        if len(ppl) >= 2
    ][:8]

    return insights


def run_search(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    query: str,
    depth: str = "balanced",
    variants: int | None = None,
    top: int | None = None,
    no_graph: bool = False,
    iterations: int | None = None,
    output_json: bool = False,
    keywords: list[str] | None = None,
    chat: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> dict:
    import click

    preset = DEPTH_PRESETS.get(depth, DEPTH_PRESETS["balanced"])
    n_variants = variants if variants is not None else preset["variants"]
    top_k = top if top is not None else preset["top"]
    use_graph = preset["graph"] and not no_graph
    n_iterations = iterations if iterations is not None else preset.get("iterations", 1)

    keywords = list(keywords) if keywords else []
    chat_jids = resolve_chat_filter(conn, chat) if chat else None
    filter_kwargs = dict(
        keywords=keywords, chat_jids=chat_jids, after=after, before=before
    )

    ctx = click.get_current_context(silent=True)
    if ctx is not None:
        output_json = output_json or bool(ctx.params.get("output_json"))

    from rich.console import Console

    console = Console()
    client = OpenAI(base_url=config.api.base_url, api_key=config.api.key)
    t0 = time.time()
    progress: list[dict] = []
    show = not output_json

    if show:
        console.print("[dim]  Expanding query...[/dim]", end="\r")
    queries = expand_query(client, config, query, n_variants)
    if show:
        console.print(
            f"[dim]  \u2713 Query → {len(queries)} variants. Searching...[/dim]",
            end="\r",
        )
    vectors = embed_queries(client, config, queries)
    dims = config.api.embedding_dims
    progress.append({"label": f"Query → {len(queries)} variants"})

    bm25_results = bm25_search(conn, query, top_k=top_k * 3, **filter_kwargs)
    vector_results = semantic_search(conn, vectors, dims, top_k * 3, **filter_kwargs)

    if bm25_results:
        candidates = rrf_fuse([("bm25", bm25_results), ("vector", vector_results)])
    else:
        candidates = vector_results
        for doc in candidates:
            doc["rrf_score"] = doc.get("similarity", 0.0)

    candidates = candidates[: top_k * 3]
    progress.append({"label": f"BM25 + vector → {len(candidates)} candidates"})
    if show:
        console.print(
            f"[dim]  \u2713 {len(candidates)} candidates. Running PPR...[/dim]",
            end="\r",
        )

    ppr_ranked = []
    paths = []
    if use_graph and n_iterations >= 2 and candidates:
        from wactx.graph_search import ppr_score, pathrag_flow

        seed_jids = list({c["sender_jid"] for c in candidates[:20]})
        seed_scores = {}
        for c in candidates[:20]:
            jid = c["sender_jid"]
            seed_scores[jid] = max(
                seed_scores.get(jid, 0),
                c.get("rrf_score", c.get("similarity", 0)),
            )

        ppr_ranked = ppr_score(
            conn,
            seed_jids,
            seed_scores,
            alpha=0.85,
            top_k=top_k * 3,
        )
        progress.append({"label": f"PPR → {len(ppr_ranked)} people scored"})
        if show:
            console.print(
                f"[dim]  \u2713 PPR scored {len(ppr_ranked)} people. Expanding...[/dim]",
                end="\r",
            )

        if ppr_ranked:
            ppr_jids = [jid for jid, _ in ppr_ranked[:30]]
            ppr_lookup = {jid: score for jid, score in ppr_ranked}

            # Fetch messages from PPR-ranked people via vector search
            expanded = []
            ppr_placeholders = ", ".join(["?"] * len(ppr_jids))
            expand_clauses, expand_params = _build_filter_clauses(
                keywords, chat_jids, after, before
            )
            extra_where = (" AND " + " AND ".join(expand_clauses)) if expand_clauses else ""
            for qvec in vectors[:2]:
                try:
                    rows = conn.execute(
                        f"""SELECT id, text_content, push_name, sender_jid, chat_jid, timestamp,
                                   media_type, media_path,
                                   array_cosine_similarity(embedding, ?::FLOAT[{dims}]) AS similarity
                            FROM messages
                            WHERE embedding IS NOT NULL
                              AND sender_jid IN ({ppr_placeholders}){extra_where}
                            ORDER BY similarity DESC LIMIT ?""",
                        [qvec, *ppr_jids, *expand_params, top_k * 2],
                    ).fetchall()
                except Exception:
                    continue

                for r in rows:
                    ppr_s = ppr_lookup.get(r[3], 0)
                    expanded.append(
                        {
                            "id": r[0],
                            "text": r[1],
                            "sender": r[2],
                            "sender_jid": r[3],
                            "chat_jid": r[4],
                            "time": r[5],
                            "media_type": r[6],
                            "media_path": r[7],
                            "similarity": float(r[8]),
                            "ppr_score": ppr_s,
                        }
                    )

            if expanded:
                seen = set()
                unique_expanded = []
                for doc in sorted(expanded, key=lambda x: -x["similarity"]):
                    if doc["id"] not in seen:
                        seen.add(doc["id"])
                        unique_expanded.append(doc)
                candidates = rrf_fuse(
                    [
                        ("retrieval", candidates),
                        ("ppr_expanded", unique_expanded),
                    ]
                )
                progress.append({"label": f"PPR expansion → {len(candidates)} merged"})

        # Phase 4: PathRAG flow for path insights
        if n_iterations >= 3 and seed_jids:
            paths = pathrag_flow(
                conn, seed_jids[:10], alpha=0.7, theta=0.01, max_hops=3
            )
            if paths:
                progress.append({"label": f"PathRAG → {len(paths)} paths"})

    candidates = candidates[: top_k * 4]

    # Phase 5: Enrich + conversation context
    candidates = enrich_results(conn, candidates, config.search.owner_name, use_graph)

    # Add PPR scores to candidates
    if ppr_ranked:
        ppr_lookup = {jid: score for jid, score in ppr_ranked}
        for c in candidates:
            c["ppr_score"] = ppr_lookup.get(c["sender_jid"], 0)

    # Add community labels
    if use_graph:
        try:
            for c in candidates:
                row = conn.execute(
                    "SELECT community_id FROM graph_persons WHERE source_id = ?",
                    [c["sender_jid"]],
                ).fetchone()
                c["community_id"] = row[0] if row and row[0] >= 0 else -1
        except Exception:
            pass

    candidates = fetch_conversation_context(conn, candidates, limit=top_k)
    progress.append({"label": f"Enriched {len(candidates)} results"})

    people = find_related_people(candidates)
    insights = (
        compute_graph_insights(conn, people[:15], config.search.owner_name)
        if use_graph
        else {}
    )

    # Add paths to insights
    if paths:
        insights["paths"] = paths[:5]

    return {
        "query": query,
        "queries_used": queries,
        "progress": progress,
        "depth": depth,
        "use_graph": use_graph,
        "elapsed": time.time() - t0,
        "people": people,
        "messages": candidates[:top_k],
        "insights": insights,
        "filters": {
            "keywords": keywords,
            "chat": chat,
            "chat_jids": chat_jids,
            "after": after,
            "before": before,
        },
    }
