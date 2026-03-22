from __future__ import annotations

import importlib
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    import networkx as nx

log = logging.getLogger("wactx.graph_search")

_NX_GRAPH_CACHE: dict[str, "nx.DiGraph"] = {}


def _build_nx_graph(conn: duckdb.DuckDBPyConnection) -> "nx.DiGraph":
    import networkx as nx

    cache_key = "person_graph"
    cached = _NX_GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    G = nx.DiGraph()

    try:
        rows = conn.execute(
            """
            SELECT gp1.source_id AS src, gp2.source_id AS dst, epm.message_count * 3.0 AS weight
            FROM edge_person_messaged epm
            JOIN graph_persons gp1 ON epm.sender_person_id = gp1.person_id
            JOIN graph_persons gp2 ON epm.receiver_person_id = gp2.person_id
        """
        ).fetchall()
        for src, dst, w in rows:
            G.add_edge(
                src,
                dst,
                weight=G.get_edge_data(src, dst, {}).get("weight", 0) + float(w),
            )
    except Exception:
        pass

    try:
        rows = conn.execute(
            """
            SELECT gp1.source_id AS src, gp2.source_id AS dst, epc.exchange_count * 5.0 AS weight
            FROM edge_person_conversed epc
            JOIN graph_persons gp1 ON epc.person1_id = gp1.person_id
            JOIN graph_persons gp2 ON epc.person2_id = gp2.person_id
        """
        ).fetchall()
        for src, dst, w in rows:
            G.add_edge(
                src,
                dst,
                weight=G.get_edge_data(src, dst, {}).get("weight", 0) + float(w),
            )
            G.add_edge(
                dst,
                src,
                weight=G.get_edge_data(dst, src, {}).get("weight", 0) + float(w),
            )
    except Exception:
        pass

    try:
        rows = conn.execute(
            """
            SELECT gp1.source_id AS src, gp2.source_id AS dst, COUNT(*) * 1.0 AS weight
            FROM edge_person_in_group e1
            JOIN edge_person_in_group e2 ON e1.group_jid = e2.group_jid AND e1.person_id != e2.person_id
            JOIN graph_persons gp1 ON e1.person_id = gp1.person_id
            JOIN graph_persons gp2 ON e2.person_id = gp2.person_id
            GROUP BY gp1.source_id, gp2.source_id
        """
        ).fetchall()
        for src, dst, w in rows:
            G.add_edge(
                src,
                dst,
                weight=G.get_edge_data(src, dst, {}).get("weight", 0) + float(w),
            )
    except Exception:
        pass

    try:
        rows = conn.execute(
            """
            SELECT gp1.source_id AS src, gp2.source_id AS dst, COUNT(*) * 2.0 AS weight
            FROM edge_person_mentions_entity e1
            JOIN edge_person_mentions_entity e2 ON e1.entity_id = e2.entity_id AND e1.person_id != e2.person_id
            JOIN graph_persons gp1 ON e1.person_id = gp1.person_id
            JOIN graph_persons gp2 ON e2.person_id = gp2.person_id
            GROUP BY gp1.source_id, gp2.source_id
        """
        ).fetchall()
        for src, dst, w in rows:
            G.add_edge(
                src,
                dst,
                weight=G.get_edge_data(src, dst, {}).get("weight", 0) + float(w),
            )
    except Exception:
        pass

    log.info(
        "Built NetworkX graph: %d nodes, %d edges",
        G.number_of_nodes(),
        G.number_of_edges(),
    )
    _NX_GRAPH_CACHE[cache_key] = G
    return G


def clear_graph_cache() -> None:
    _NX_GRAPH_CACHE.clear()


def ppr_score(
    conn: duckdb.DuckDBPyConnection,
    seed_jids: list[str],
    seed_scores: dict[str, float] | None = None,
    alpha: float = 0.85,
    max_iter: int = 100,
    top_k: int = 50,
) -> list[tuple[str, float]]:
    import networkx as nx

    G = _build_nx_graph(conn)
    if G.number_of_nodes() == 0:
        return []

    if seed_scores is None:
        seed_scores = {jid: 1.0 for jid in seed_jids}

    personalization = {}
    total = sum(seed_scores.get(jid, 1.0) for jid in seed_jids if jid in G)
    if total == 0:
        return []
    for jid in seed_jids:
        if jid in G:
            personalization[jid] = seed_scores.get(jid, 1.0) / total

    if not personalization:
        return []

    t0 = time.time()
    try:
        scores = nx.pagerank(
            G,
            alpha=alpha,
            personalization=personalization,
            weight="weight",
            max_iter=max_iter,
            tol=1e-8,
        )
    except nx.PowerIterationFailedConvergence:
        scores = nx.pagerank(
            G,
            alpha=alpha,
            personalization=personalization,
            weight="weight",
            max_iter=300,
            tol=1e-6,
        )

    elapsed = time.time() - t0
    log.info("PPR completed in %.2fs (%d nodes scored)", elapsed, len(scores))

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [(jid, score) for jid, score in ranked[:top_k] if score > 0]


def pathrag_flow(
    conn: duckdb.DuckDBPyConnection,
    seed_jids: list[str],
    alpha: float = 0.7,
    theta: float = 0.01,
    max_hops: int = 3,
    top_k: int = 20,
) -> list[dict]:
    """PathRAG flow: propagate resources from each seed, then find shortest paths
    between seed pairs through high-resource intermediaries.

    S(v_i) = Σ [α · S(v_j) / degree(v_j)]
    Path score = avg(S(v_i)) across path nodes.
    """
    import networkx as nx

    G = _build_nx_graph(conn)
    if G.number_of_nodes() == 0:
        return []

    seed_set = [j for j in seed_jids if j in G]
    if len(seed_set) < 2:
        return []

    t0 = time.time()

    seed_resources: dict[str, dict[str, float]] = {}
    for source in seed_set[:8]:
        resource: dict[str, float] = defaultdict(float)
        resource[source] = 1.0

        frontier = {source}
        for _hop in range(max_hops):
            next_frontier: set[str] = set()
            for node in list(frontier)[:500]:
                out_deg = G.out_degree(node)
                if out_deg == 0:
                    continue
                share = alpha * resource[node] / out_deg
                if share < theta * 0.001:
                    continue
                for neighbor in G.successors(node):
                    resource[neighbor] += share
                    if resource[neighbor] > theta * 0.001:
                        next_frontier.add(neighbor)
            frontier = next_frontier
            if not frontier:
                break
        seed_resources[source] = resource

    all_paths: list[dict] = []
    for i, source in enumerate(seed_set[:8]):
        for target in seed_set[i + 1 :]:
            src_res = seed_resources.get(source, {})
            tgt_res = seed_resources.get(target, {})

            if (
                src_res.get(target, 0) < theta * 0.0001
                and tgt_res.get(source, 0) < theta * 0.0001
            ):
                continue

            try:
                path = nx.shortest_path(G, source, target, weight=None)
                if len(path) > max_hops + 1:
                    continue
                combined_resource = {
                    n: max(src_res.get(n, 0), tgt_res.get(n, 0)) for n in path
                }
                path_score = sum(combined_resource.values()) / len(path)
                all_paths.append(
                    {
                        "source": source,
                        "target": target,
                        "path": path,
                        "score": path_score,
                        "hops": len(path) - 1,
                    }
                )
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                pass

    all_paths.sort(key=lambda x: -x["score"])
    elapsed = time.time() - t0
    log.info("PathRAG flow: %d paths scored in %.2fs", len(all_paths), elapsed)
    return all_paths[:top_k]


def leiden_communities(
    conn: duckdb.DuckDBPyConnection,
    resolution: float = 1.0,
) -> dict[str, int]:
    try:
        ig = importlib.import_module("igraph")
    except ImportError:
        log.warning(
            "igraph not installed — skipping Leiden. Install with: pip install igraph"
        )
        return {}

    t0 = time.time()

    edges = []
    try:
        rows = conn.execute(
            """
            SELECT gp1.source_id, gp2.source_id, epm.message_count
            FROM edge_person_messaged epm
            JOIN graph_persons gp1 ON epm.sender_person_id = gp1.person_id
            JOIN graph_persons gp2 ON epm.receiver_person_id = gp2.person_id
        """
        ).fetchall()
        edges.extend((r[0], r[1], float(r[2]) * 3.0) for r in rows)
    except Exception:
        pass

    try:
        rows = conn.execute(
            """
            SELECT gp1.source_id, gp2.source_id, epc.exchange_count
            FROM edge_person_conversed epc
            JOIN graph_persons gp1 ON epc.person1_id = gp1.person_id
            JOIN graph_persons gp2 ON epc.person2_id = gp2.person_id
        """
        ).fetchall()
        edges.extend((r[0], r[1], float(r[2]) * 5.0) for r in rows)
    except Exception:
        pass

    try:
        rows = conn.execute(
            """
            SELECT gp1.source_id, gp2.source_id, COUNT(*)
            FROM edge_person_in_group e1
            JOIN edge_person_in_group e2 ON e1.group_jid = e2.group_jid AND e1.person_id < e2.person_id
            JOIN graph_persons gp1 ON e1.person_id = gp1.person_id
            JOIN graph_persons gp2 ON e2.person_id = gp2.person_id
            GROUP BY gp1.source_id, gp2.source_id
        """
        ).fetchall()
        edges.extend((r[0], r[1], float(r[2])) for r in rows)
    except Exception:
        pass

    if not edges:
        return {}

    node_set: set[str] = set()
    for s, t, _ in edges:
        node_set.add(s)
        node_set.add(t)
    node_list = sorted(node_set)
    node_idx = {n: i for i, n in enumerate(node_list)}

    G = ig.Graph(n=len(node_list), directed=False)
    G.vs["name"] = node_list

    edge_tuples = []
    edge_weights = []
    seen_edges: set[tuple[int, int]] = set()
    for s, t, w in edges:
        si, ti = node_idx[s], node_idx[t]
        key = (min(si, ti), max(si, ti))
        if key in seen_edges:
            idx = edge_tuples.index(key)
            edge_weights[idx] += w
            continue
        seen_edges.add(key)
        edge_tuples.append(key)
        edge_weights.append(w)

    G.add_edges(edge_tuples)
    G.es["weight"] = edge_weights

    partition = G.community_leiden(
        objective_function="modularity",
        weights="weight",
        resolution=resolution,
        n_iterations=3,
    )

    communities = {}
    for i, community_id in enumerate(partition.membership):
        communities[node_list[i]] = community_id

    n_communities = len(set(communities.values()))
    elapsed = time.time() - t0
    log.info(
        "Leiden: %d communities from %d people in %.2fs",
        n_communities,
        len(communities),
        elapsed,
    )
    return communities


def store_communities(
    conn: duckdb.DuckDBPyConnection,
    communities: dict[str, int],
) -> dict[int, dict]:
    if not communities:
        return {}

    try:
        conn.execute(
            "ALTER TABLE graph_persons ADD COLUMN community_id INTEGER DEFAULT -1"
        )
    except Exception:
        conn.execute("UPDATE graph_persons SET community_id = -1")

    for jid, cid in communities.items():
        conn.execute(
            "UPDATE graph_persons SET community_id = ? WHERE source_id = ?",
            [cid, jid],
        )

    summaries: dict[int, dict] = {}
    try:
        rows = conn.execute(
            """
            SELECT gp.community_id,
                   COUNT(DISTINCT gp.person_id) AS member_count,
                   LIST(DISTINCT gp.display_name ORDER BY gp.display_name) AS members
            FROM graph_persons gp
            WHERE gp.community_id >= 0
            GROUP BY gp.community_id
            HAVING COUNT(DISTINCT gp.person_id) >= 2
            ORDER BY member_count DESC
        """
        ).fetchall()

        for cid, count, members in rows:
            top_groups = []
            try:
                grp_rows = conn.execute(
                    """
                    SELECT gg.group_name, SUM(epg.message_count) AS total
                    FROM edge_person_in_group epg
                    JOIN graph_persons gp ON epg.person_id = gp.person_id
                    JOIN graph_groups gg ON epg.group_jid = gg.group_jid
                    WHERE gp.community_id = ?
                    GROUP BY gg.group_name
                    ORDER BY total DESC LIMIT 3
                """,
                    [cid],
                ).fetchall()
                top_groups = [r[0] for r in grp_rows]
            except Exception:
                pass

            top_entities = []
            try:
                ent_rows = conn.execute(
                    """
                    SELECT ge.entity_value, SUM(epm.mention_count) AS total
                    FROM edge_person_mentions_entity epm
                    JOIN graph_persons gp ON epm.person_id = gp.person_id
                    JOIN graph_entities ge ON epm.entity_id = ge.entity_id
                    WHERE gp.community_id = ?
                    GROUP BY ge.entity_value
                    ORDER BY total DESC LIMIT 5
                """,
                    [cid],
                ).fetchall()
                top_entities = [r[0] for r in ent_rows]
            except Exception:
                pass

            summaries[cid] = {
                "member_count": count,
                "members": members[:10] if members else [],
                "top_groups": top_groups,
                "top_entities": top_entities,
            }
    except Exception as e:
        log.warning("Community summary failed: %s", e)

    log.info("Stored %d community summaries", len(summaries))
    return summaries
