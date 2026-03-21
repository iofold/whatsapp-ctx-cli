from __future__ import annotations

from collections import defaultdict
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


def bm25_search(conn: duckdb.DuckDBPyConnection, query: str, top_k: int) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT m.id, m.text_content, m.push_name, m.sender_jid, m.chat_jid,
                      m.timestamp, m.media_type, m.media_path,
                      fts_main_messages.match_bm25(m.id, ?, fields := 'text_content') AS score
               FROM messages m
               WHERE score IS NOT NULL
               ORDER BY score
               LIMIT ?""",
            [query, top_k],
        ).fetchall()
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
    conn: duckdb.DuckDBPyConnection, vectors: list[list[float]], dims: int, top_k: int
) -> list[dict]:
    all_results: dict[str, dict] = {}
    for qvec in vectors:
        rows = conn.execute(
            f"""SELECT id, text_content, push_name, sender_jid, chat_jid, timestamp,
                       media_type, media_path,
                       array_cosine_similarity(embedding, ?::FLOAT[{dims}]) AS similarity
                FROM messages WHERE embedding IS NOT NULL
                ORDER BY similarity DESC LIMIT ?""",
            [qvec, top_k],
        ).fetchall()
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


def graph_expand_candidates(
    conn: duckdb.DuckDBPyConnection,
    seed_jids: list[str],
    vectors: list[list[float]],
    dims: int,
    top_k: int = 20,
) -> list[dict]:
    from wactx.db import table_exists

    if not table_exists(conn, "graph_persons") or not seed_jids:
        return []

    placeholders = ", ".join(["?"] * len(seed_jids))

    try:
        neighbour_rows = conn.execute(
            f"""
            WITH seed AS (
                SELECT person_id FROM graph_persons WHERE source_id IN ({placeholders})
            ),
            via_dm AS (
                SELECT CASE
                    WHEN epm.sender_person_id IN (SELECT person_id FROM seed)
                    THEN epm.receiver_person_id ELSE epm.sender_person_id
                END AS person_id, epm.message_count * 3.0 AS weight
                FROM edge_person_messaged epm
                WHERE epm.sender_person_id IN (SELECT person_id FROM seed)
                   OR epm.receiver_person_id IN (SELECT person_id FROM seed)
            ),
            via_group AS (
                SELECT e2.person_id, e2.message_count * 1.0 AS weight
                FROM edge_person_in_group e1
                JOIN edge_person_in_group e2 ON e1.group_jid = e2.group_jid
                WHERE e1.person_id IN (SELECT person_id FROM seed)
                  AND e2.person_id NOT IN (SELECT person_id FROM seed)
            ),
            via_conversation AS (
                SELECT CASE
                    WHEN epc.person1_id IN (SELECT person_id FROM seed)
                    THEN epc.person2_id ELSE epc.person1_id
                END AS person_id, epc.exchange_count * 5.0 AS weight
                FROM edge_person_conversed epc
                WHERE epc.person1_id IN (SELECT person_id FROM seed)
                   OR epc.person2_id IN (SELECT person_id FROM seed)
            ),
            via_entity AS (
                SELECT e2.person_id, e2.mention_count * 2.0 AS weight
                FROM edge_person_mentions_entity e1
                JOIN edge_person_mentions_entity e2 ON e1.entity_id = e2.entity_id
                WHERE e1.person_id IN (SELECT person_id FROM seed)
                  AND e2.person_id NOT IN (SELECT person_id FROM seed)
            ),
            all_neighbours AS (
                SELECT person_id, SUM(weight) AS total_weight
                FROM (
                    SELECT * FROM via_dm
                    UNION ALL SELECT * FROM via_group
                    UNION ALL SELECT * FROM via_conversation
                    UNION ALL SELECT * FROM via_entity
                ) combined
                WHERE person_id NOT IN (SELECT person_id FROM seed)
                GROUP BY person_id
            )
            SELECT gp.source_id, an.total_weight
            FROM all_neighbours an
            JOIN graph_persons gp ON an.person_id = gp.person_id
            ORDER BY an.total_weight DESC
            LIMIT 30
            """,
            seed_jids,
        ).fetchall()
    except Exception:
        return []

    if not neighbour_rows:
        return []

    neighbour_jids = [r[0] for r in neighbour_rows]
    neighbour_weights = {r[0]: r[1] for r in neighbour_rows}
    n_placeholders = ", ".join(["?"] * len(neighbour_jids))

    expanded = []
    for qvec in vectors[:2]:
        try:
            rows = conn.execute(
                f"""SELECT id, text_content, push_name, sender_jid, chat_jid, timestamp,
                           media_type, media_path,
                           array_cosine_similarity(embedding, ?::FLOAT[{dims}]) AS similarity
                    FROM messages
                    WHERE embedding IS NOT NULL
                      AND sender_jid IN ({n_placeholders})
                    ORDER BY similarity DESC LIMIT ?""",
                [qvec] + neighbour_jids + [top_k],
            ).fetchall()
        except Exception:
            continue

        for r in rows:
            graph_weight = float(neighbour_weights.get(r[3], 1.0))
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
                    "similarity": float(r[8]) * min(2.0, 1.0 + graph_weight / 100.0),
                }
            )

    seen = set()
    unique = []
    for doc in sorted(expanded, key=lambda x: x["similarity"], reverse=True):
        if doc["id"] not in seen:
            seen.add(doc["id"])
            unique.append(doc)
    return unique


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

    for p in by_person.values():
        retrieval = max(p["max_rrf"] * 100, p["max_similarity"])
        graph = min(
            1.0,
            (0.3 if p["dm_volume"] > 0 else 0)
            + 0.1 * min(3, len(p["shared_groups"]))
            + 0.1 * min(3, p["message_count"])
            + 0.05 * min(3, len(p["entities"])),
        )
        conv = p["conversation_boost"]
        p["score"] = 0.50 * retrieval + 0.30 * graph + 0.20 * conv

    return sorted(by_person.values(), key=lambda x: x["score"], reverse=True)


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
) -> dict:
    preset = DEPTH_PRESETS.get(depth, DEPTH_PRESETS["balanced"])
    n_variants = variants if variants is not None else preset["variants"]
    top_k = top if top is not None else preset["top"]
    use_graph = preset["graph"] and not no_graph
    n_iterations = iterations if iterations is not None else preset.get("iterations", 1)

    client = OpenAI(base_url=config.api.base_url, api_key=config.api.key)
    t0 = time.time()

    queries = expand_query(client, config, query, n_variants)
    vectors = embed_queries(client, config, queries)
    dims = config.api.embedding_dims

    # === PASS 1: BM25 + vector candidate generation ===
    bm25_results = bm25_search(conn, query, top_k=top_k * 3)
    vector_results = semantic_search(conn, vectors, dims, top_k * 3)

    if bm25_results:
        candidates = rrf_fuse([("bm25", bm25_results), ("vector", vector_results)])
    else:
        candidates = vector_results
        for doc in candidates:
            doc["rrf_score"] = doc.get("similarity", 0.0)

    candidates = candidates[: top_k * 3]

    # === PASS 2..N: Iterative graph expansion with pruning ===
    if use_graph and n_iterations >= 2 and candidates:
        seen_seeds: set[str] = set()
        graph_passes = n_iterations - 1

        for i in range(graph_passes):
            top_n = max(5, 20 - i * 3)
            seed_jids = list({c["sender_jid"] for c in candidates[:top_n]})
            new_seeds = [j for j in seed_jids if j not in seen_seeds]
            seen_seeds.update(seed_jids)

            if not new_seeds:
                break

            expanded = graph_expand_candidates(
                conn, new_seeds, vectors, dims, top_k=max(5, top_k * 2 - i * 5)
            )
            if not expanded:
                break

            candidates = rrf_fuse(
                [
                    (f"pass{i + 1}", candidates),
                    (f"graph_{i + 1}", expanded),
                ]
            )

            candidates = candidates[: max(top_k * 2, int(top_k * 3 * (0.85**i)))]

    candidates = candidates[: top_k * 4]

    candidates = enrich_results(conn, candidates, config.search.owner_name, use_graph)

    if n_iterations >= 3:
        candidates = fetch_conversation_context(conn, candidates, limit=top_k)

    people = find_related_people(candidates)
    insights = (
        compute_graph_insights(conn, people[:15], config.search.owner_name)
        if use_graph
        else {}
    )

    return {
        "query": query,
        "queries_used": queries,
        "depth": depth,
        "use_graph": use_graph,
        "elapsed": time.time() - t0,
        "people": people,
        "messages": candidates[:top_k],
        "insights": insights,
    }
