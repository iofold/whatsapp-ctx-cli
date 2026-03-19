from __future__ import annotations

import json
import logging
import time

import duckdb
from openai import OpenAI

from wactx.config import Config

log = logging.getLogger("wactx.search")

DEPTH_PRESETS = {
    "fast": {"variants": 1, "top": 10, "graph": False},
    "balanced": {"variants": 5, "top": 15, "graph": True},
    "deep": {"variants": 8, "top": 30, "graph": True},
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


def enrich_basic(conn: duckdb.DuckDBPyConnection, results: list[dict]) -> list[dict]:
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

        r["dm_volume"] = 0
        r["shared_groups"] = []
        r["entities"] = []
    return results


def enrich_with_graph(
    conn: duckdb.DuckDBPyConnection, results: list[dict], owner_name: str
) -> list[dict]:
    results = enrich_basic(conn, results)
    from wactx.db import table_exists

    for r in results:
        dm = conn.execute(
            """SELECT COALESCE(SUM(message_count), 0)
               FROM edge_person_messaged epm
               JOIN graph_persons gp ON (epm.sender_person_id = gp.person_id OR epm.receiver_person_id = gp.person_id)
               WHERE gp.source_id = ?""",
            [r["sender_jid"]],
        ).fetchone()
        r["dm_volume"] = dm[0] if dm else 0

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
    return results


def find_related_people(results: list[dict]) -> list[dict]:
    by_person: dict[str, dict] = {}
    for r in results:
        jid = r["sender_jid"]
        if jid not in by_person:
            by_person[jid] = {
                "display_name": r["display_name"],
                "phone": r.get("phone", ""),
                "sender_jid": jid,
                "max_similarity": r["similarity"],
                "message_count": 0,
                "dm_volume": r.get("dm_volume", 0),
                "shared_groups": r.get("shared_groups", []),
                "entities": r.get("entities", []),
                "messages": [],
            }
        p = by_person[jid]
        p["message_count"] += 1
        p["max_similarity"] = max(p["max_similarity"], r["similarity"])
        p["messages"].append(r)

    # score = 0.6 × semantic + 0.4 × graph_proximity
    for p in by_person.values():
        graph = min(
            1.0,
            (
                (0.3 if p["dm_volume"] > 0 else 0)
                + (0.1 * min(3, len(p["shared_groups"])))
                + (0.1 * min(3, p["message_count"]))
            ),
        )
        p["score"] = 0.6 * p["max_similarity"] + 0.4 * graph

    return sorted(by_person.values(), key=lambda x: x["score"], reverse=True)


def compute_graph_insights(
    conn: duckdb.DuckDBPyConnection, people: list[dict], owner_name: str
) -> dict:
    insights: dict = {"shared_groups": [], "common_entities": [], "connections": []}
    names = {p["sender_jid"]: p["display_name"][:20] for p in people}

    for i, p1 in enumerate(people):
        for p2 in people[i + 1 :]:
            shared = set(p1.get("shared_groups", [])) & set(p2.get("shared_groups", []))
            if shared:
                insights["shared_groups"].append(
                    {
                        "person1": names.get(p1["sender_jid"], "?"),
                        "person2": names.get(p2["sender_jid"], "?"),
                        "groups": sorted(shared),
                    }
                )

    entity_people: dict[str, list[str]] = {}
    for p in people:
        for _, val, _ in p.get("entities", []):
            entity_people.setdefault(val, []).append(names.get(p["sender_jid"], "?"))
    insights["common_entities"] = [
        {"entity": e, "people": ppl}
        for e, ppl in sorted(entity_people.items(), key=lambda x: -len(x[1]))
        if len(ppl) >= 2
    ][:6]

    for p in people[:10]:
        if p["display_name"] in ("Self", "?"):
            continue
        parts = []
        if p.get("dm_volume"):
            parts.append(f"{p['dm_volume']} DMs")
        n_grp = len(p.get("shared_groups", []))
        if n_grp:
            top = ", ".join(g[:20] for g in p["shared_groups"][:2])
            parts.append(f"{n_grp} groups ({top})")
        if parts:
            strength = (
                "strong"
                if p.get("dm_volume", 0) > 10
                else "weak"
                if p.get("dm_volume", 0) > 0
                else "indirect"
            )
            insights["connections"].append(
                {
                    "name": names.get(p["sender_jid"], "?"),
                    "strength": strength,
                    "details": " · ".join(parts),
                }
            )

    return insights


def run_search(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    query: str,
    depth: str = "balanced",
    variants: int | None = None,
    top: int | None = None,
    no_graph: bool = False,
) -> dict:
    preset = DEPTH_PRESETS.get(depth, DEPTH_PRESETS["balanced"])
    n_variants = variants if variants is not None else preset["variants"]
    top_k = top if top is not None else preset["top"]
    use_graph = preset["graph"] and not no_graph

    client = OpenAI(base_url=config.api.base_url, api_key=config.api.key)
    t0 = time.time()

    queries = expand_query(client, config, query, n_variants)
    vectors = embed_queries(client, config, queries)
    results = semantic_search(conn, vectors, config.api.embedding_dims, top_k)

    if use_graph:
        try:
            results = enrich_with_graph(conn, results, config.search.owner_name)
        except Exception:
            results = enrich_basic(conn, results)
            use_graph = False
    else:
        results = enrich_basic(conn, results)

    people = find_related_people(results)
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
        "messages": results,
        "insights": insights,
    }
