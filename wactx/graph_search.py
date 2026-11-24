from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import duckdb

from wactx.db import bulk_rows, table_exists

if TYPE_CHECKING:
    import numpy as np
    import scipy.sparse as sp

log = logging.getLogger("wactx.graph_search")


# Person graph used by PPR and PathRAG.
#
# This used to be a networkx DiGraph built edge-by-edge in Python on every search (one
# process per CLI call, so the cache never helped): ~5M edges took ~28 s to build and
# ~13 s more to convert for one pagerank call. The same weighted edges are now
# aggregated in DuckDB and loaded straight into a scipy CSR matrix (~2-3 s), and PPR is
# the same power iteration networkx runs (`_pagerank_scipy`), so scores match it.
#
# Edge weights (summed per directed pair, unchanged from the networkx version):
#   messaged        src -> dst        message_count * 3
#   conversed       both directions   exchange_count * 5
#   co-member       both directions   shared groups * 1
#   co-mention      both directions   shared entities * 2
_EDGE_PARTS = {
    "edge_person_messaged": (
        "SELECT sender_person_id AS s, receiver_person_id AS d, message_count * 3.0 AS w "
        "FROM edge_person_messaged",
    ),
    "edge_person_conversed": (
        "SELECT person1_id AS s, person2_id AS d, exchange_count * 5.0 AS w FROM edge_person_conversed",
        "SELECT person2_id AS s, person1_id AS d, exchange_count * 5.0 AS w FROM edge_person_conversed",
    ),
    "edge_person_in_group": (
        "SELECT e1.person_id AS s, e2.person_id AS d, COUNT(*) * 1.0 AS w "
        "FROM edge_person_in_group e1 JOIN edge_person_in_group e2 "
        "ON e1.group_jid = e2.group_jid AND e1.person_id != e2.person_id GROUP BY 1, 2",
    ),
    "edge_person_mentions_entity": (
        "SELECT e1.person_id AS s, e2.person_id AS d, COUNT(*) * 2.0 AS w "
        "FROM edge_person_mentions_entity e1 JOIN edge_person_mentions_entity e2 "
        "ON e1.entity_id = e2.entity_id AND e1.person_id != e2.person_id GROUP BY 1, 2",
    ),
}


@dataclass
class PersonGraph:
    """Directed weighted person graph; node i is the person whose JID is node_ids[i]."""

    node_ids: list[str]
    index: dict[str, int]
    adj: "sp.csr_matrix"
    _transition_t: "sp.csr_matrix | None" = field(default=None, repr=False)
    _dangling: "np.ndarray | None" = field(default=None, repr=False)

    def __len__(self) -> int:
        return len(self.node_ids)

    def __contains__(self, jid: object) -> bool:
        return jid in self.index

    @property
    def num_edges(self) -> int:
        return int(self.adj.nnz)

    def out_degree(self, i: int) -> int:
        return int(self.adj.indptr[i + 1] - self.adj.indptr[i])

    def successors(self, i: int) -> "np.ndarray":
        return self.adj.indices[self.adj.indptr[i] : self.adj.indptr[i + 1]]

    def transition_t(self) -> tuple["sp.csr_matrix", "np.ndarray"]:
        """Transpose of the row-normalised adjacency, and the dangling-node mask."""
        if self._transition_t is None:
            import numpy as np
            import scipy.sparse as sp

            out_strength = np.asarray(self.adj.sum(axis=1)).ravel()
            inv = np.zeros_like(out_strength)
            nonzero = out_strength != 0
            inv[nonzero] = 1.0 / out_strength[nonzero]
            self._transition_t = (sp.diags(inv) @ self.adj).T.tocsr()
            self._dangling = ~nonzero
        return self._transition_t, self._dangling


_GRAPH_CACHE: dict[str, PersonGraph] = {}


def _empty_graph() -> PersonGraph:
    import scipy.sparse as sp

    return PersonGraph(node_ids=[], index={}, adj=sp.csr_matrix((0, 0)))


# Built by `wactx graph` (materialize_person_graph) so searches do not re-aggregate ~5M
# edges each time: the finished CSR arrays are stored as BLOBs in one row (~60 MB), which
# reads back in ~0.1-0.2 s; a row-per-edge table took ~1.2 s and re-aggregating ~3.8 s.
# Guarded by a fingerprint of the source tables: if the graph was rebuilt by something
# that did not refresh the stored copy, it is ignored and edges are aggregated instead.
PERSON_CSR_TABLE = "graph_person_csr"


def _edge_union(conn: duckdb.DuckDBPyConnection, parts_by_table: dict[str, tuple[str, ...]]) -> str | None:
    parts = [q for table, qs in parts_by_table.items() if table_exists(conn, table) for q in qs]
    if not parts or not table_exists(conn, "graph_persons"):
        return None
    return "\n  UNION ALL ".join(parts)


def _graph_fingerprint(conn: duckdb.DuckDBPyConnection) -> str:
    """Order-insensitive row hash of the graph's source tables (~20 ms on the live DB)."""
    parts = []
    for table in ("graph_persons", *_EDGE_PARTS):
        if table_exists(conn, table):
            # graph_persons.community_id is rewritten after materializing, so leave it out.
            row_hash = "hash(x.person_id, x.source_id)" if table == "graph_persons" else "hash(x)"
            row = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM({row_hash}), 0) FROM {table} AS x"
            ).fetchone()
            parts.append(f"{table}:{row[0]}:{row[1]}")
    return ";".join(parts)


def materialize_person_graph(conn: duckdb.DuckDBPyConnection) -> int:
    """Store the aggregated PPR/PathRAG graph as CSR arrays for fast loading."""
    conn.execute(f"DROP TABLE IF EXISTS {PERSON_CSR_TABLE}")
    t0 = time.time()
    graph = _load_aggregated(conn)
    if len(graph) == 0:
        return 0
    adj = graph.adj
    conn.execute(
        f"""CREATE TABLE {PERSON_CSR_TABLE} (
            fingerprint VARCHAR, source_ids VARCHAR[], indptr BLOB, indices BLOB, data BLOB)"""
    )
    conn.execute(
        f"INSERT INTO {PERSON_CSR_TABLE} VALUES (?, ?, ?, ?, ?)",
        [
            _graph_fingerprint(conn),
            graph.node_ids,
            adj.indptr.astype("<i8").tobytes(),
            adj.indices.astype("<i4").tobytes(),
            adj.data.astype("<f8").tobytes(),
        ],
    )
    log.info("Materialized person graph: %d edges in %.2fs", adj.nnz, time.time() - t0)
    return int(adj.nnz)


def _load_materialized(conn: duckdb.DuckDBPyConnection) -> PersonGraph | None:
    import numpy as np
    import scipy.sparse as sp

    if not table_exists(conn, PERSON_CSR_TABLE):
        return None
    row = conn.execute(f"SELECT fingerprint FROM {PERSON_CSR_TABLE}").fetchone()
    if row is None or row[0] != _graph_fingerprint(conn):
        log.info("Stored person graph is stale; aggregating edges instead (run `wactx graph`)")
        return None
    source_ids, indptr, indices, data = conn.execute(
        f"SELECT source_ids, indptr, indices, data FROM {PERSON_CSR_TABLE}"
    ).fetchone()
    n = len(source_ids)
    adj = sp.csr_matrix(
        (
            np.frombuffer(data, dtype="<f8"),
            np.frombuffer(indices, dtype="<i4"),
            np.frombuffer(indptr, dtype="<i8"),
        ),
        shape=(n, n),
    )
    node_ids = list(source_ids)
    return PersonGraph(node_ids=node_ids, index={j: i for i, j in enumerate(node_ids)}, adj=adj)


def _load_aggregated(conn: duckdb.DuckDBPyConnection) -> PersonGraph:
    import numpy as np
    import scipy.sparse as sp

    union = _edge_union(conn, _EDGE_PARTS)
    if union is None:
        return _empty_graph()
    sql = f"""
        WITH e AS (
          {union}
        ), agg AS (
          SELECT s, d, SUM(w) AS w FROM e GROUP BY s, d
        )
        SELECT agg.s, agg.d, agg.w
        FROM agg
        JOIN graph_persons ps ON ps.person_id = agg.s
        JOIN graph_persons pd ON pd.person_id = agg.d
    """
    edges = conn.execute(sql).fetchnumpy()
    persons = conn.execute("SELECT person_id, source_id FROM graph_persons ORDER BY person_id").fetchnumpy()

    src = np.asarray(edges["s"], dtype=np.int64)
    dst = np.asarray(edges["d"], dtype=np.int64)
    weight = np.asarray(edges["w"], dtype=np.float64)
    # Nodes are the people with at least one edge (as in the networkx graph).
    nodes = np.unique(np.concatenate([src, dst]))
    pid_sorted = np.asarray(persons["person_id"], dtype=np.int64)
    jid_sorted = np.asarray(persons["source_id"], dtype=object)
    node_ids = [str(j) for j in jid_sorted[np.searchsorted(pid_sorted, nodes)]]
    n = len(nodes)
    adj = sp.csr_matrix((weight, (np.searchsorted(nodes, src), np.searchsorted(nodes, dst))), shape=(n, n))
    adj.sum_duplicates()
    return PersonGraph(node_ids=node_ids, index={j: i for i, j in enumerate(node_ids)}, adj=adj)


def load_person_graph(conn: duckdb.DuckDBPyConnection) -> PersonGraph:
    cached = _GRAPH_CACHE.get("person_graph")
    if cached is not None:
        return cached
    t0 = time.time()
    source = "materialized"
    try:
        graph = _load_materialized(conn)
        if graph is None:
            source = "aggregated"
            graph = _load_aggregated(conn)
    except duckdb.Error as exc:
        log.warning("Could not load the person graph; graph ranking disabled: %s", exc)
        graph = _empty_graph()
    log.info(
        "Loaded person graph (%s): %d nodes, %d edges in %.2fs",
        source, len(graph), graph.num_edges, time.time() - t0,
    )
    _GRAPH_CACHE["person_graph"] = graph
    return graph


def clear_graph_cache() -> None:
    _GRAPH_CACHE.clear()


class PowerIterationFailedConvergence(RuntimeError):
    pass


def _personalized_pagerank(
    graph: PersonGraph,
    personalization: dict[int, float],
    alpha: float,
    max_iter: int,
    tol: float,
) -> "np.ndarray":
    """networkx `_pagerank_scipy`: dangling mass and teleport both go to the personalization."""
    import numpy as np

    n = len(graph)
    transition_t, dangling = graph.transition_t()
    p = np.zeros(n)
    for i, v in personalization.items():
        p[i] = v
    p /= p.sum()
    x = np.full(n, 1.0 / n)
    for _ in range(max_iter):
        x_last = x
        x = alpha * (transition_t @ x + x[dangling].sum() * p) + (1 - alpha) * p
        if np.abs(x - x_last).sum() < n * tol:
            return x
    raise PowerIterationFailedConvergence(max_iter)


def ppr_score(
    conn: duckdb.DuckDBPyConnection,
    seed_jids: list[str],
    seed_scores: dict[str, float] | None = None,
    alpha: float = 0.85,
    max_iter: int = 100,
    top_k: int = 50,
) -> list[tuple[str, float]]:
    import numpy as np

    G = load_person_graph(conn)
    if len(G) == 0:
        return []

    if seed_scores is None:
        seed_scores = {jid: 1.0 for jid in seed_jids}

    total = sum(seed_scores.get(jid, 1.0) for jid in seed_jids if jid in G)
    if total == 0:
        return []
    personalization = {
        G.index[jid]: seed_scores.get(jid, 1.0) / total for jid in seed_jids if jid in G
    }
    if not personalization:
        return []

    t0 = time.time()
    try:
        scores = _personalized_pagerank(G, personalization, alpha, max_iter, tol=1e-8)
    except PowerIterationFailedConvergence:
        scores = _personalized_pagerank(G, personalization, alpha, 300, tol=1e-6)
    log.info("PPR completed in %.2fs (%d nodes scored)", time.time() - t0, len(scores))

    order = np.argsort(-scores, kind="stable")[:top_k]
    return [(G.node_ids[i], float(scores[i])) for i in order if scores[i] > 0]


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
    import numpy as np
    from scipy.sparse.csgraph import breadth_first_order

    G = load_person_graph(conn)
    if len(G) == 0:
        return []

    seed_set = [j for j in seed_jids if j in G]
    if len(seed_set) < 2:
        return []

    t0 = time.time()
    n = len(G)
    floor = theta * 0.001

    seed_resources: dict[str, np.ndarray] = {}
    for source in seed_set[:8]:
        resource = np.zeros(n)
        start = G.index[source]
        resource[start] = 1.0

        frontier = {start}
        for _hop in range(max_hops):
            next_frontier: set[int] = set()
            # Cap each hop at the 500 highest-resource nodes (ties by index). The networkx
            # version took the first 500 of a set of JIDs, whose order changes per process
            # with string hashing, so its paths varied run to run for the same query.
            ranked = sorted(frontier, key=lambda k: (-resource[k], k))
            for node in ranked[:500]:
                out_deg = G.out_degree(node)
                if out_deg == 0:
                    continue
                share = alpha * resource[node] / out_deg
                if share < floor:
                    continue
                neighbors = G.successors(node)
                resource[neighbors] += share
                next_frontier.update(neighbors[resource[neighbors] > floor].tolist())
            frontier = next_frontier
            if not frontier:
                break
        seed_resources[source] = resource

    zeros = np.zeros(n)
    predecessors: dict[int, np.ndarray] = {}
    all_paths: list[dict] = []
    for i, source in enumerate(seed_set[:8]):
        s_idx = G.index[source]
        for target in seed_set[i + 1 :]:
            t_idx = G.index[target]
            src_res = seed_resources.get(source, zeros)
            tgt_res = seed_resources.get(target, zeros)

            if src_res[t_idx] < theta * 0.0001 and tgt_res[s_idx] < theta * 0.0001:
                continue

            # Unweighted directed shortest path (networkx shortest_path(weight=None)).
            if s_idx not in predecessors:
                _, predecessors[s_idx] = breadth_first_order(
                    G.adj, s_idx, directed=True, return_predecessors=True
                )
            pred = predecessors[s_idx]
            path_idx = [t_idx]
            while path_idx[-1] != s_idx and len(path_idx) <= max_hops + 1:
                prev = pred[path_idx[-1]]
                if prev < 0:
                    break
                path_idx.append(int(prev))
            if path_idx[-1] != s_idx or len(path_idx) > max_hops + 1:
                continue
            path_idx.reverse()

            path_score = sum(max(src_res[k], tgt_res[k]) for k in path_idx) / len(path_idx)
            all_paths.append(
                {
                    "source": source,
                    "target": target,
                    "path": [G.node_ids[k] for k in path_idx],
                    "score": float(path_score),
                    "hops": len(path_idx) - 1,
                }
            )

    all_paths.sort(key=lambda x: -x["score"])
    log.info("PathRAG flow: %d paths scored in %.2fs", len(all_paths), time.time() - t0)
    return all_paths[:top_k]


# Leiden's edge set (unchanged): messaged x3, conversed x5 (one row per pair), shared
# groups x1, summed per unordered pair; nodes sorted by JID.
_LEIDEN_PARTS = {
    "edge_person_messaged": (
        "SELECT sender_person_id AS s, receiver_person_id AS d, message_count * 3.0 AS w "
        "FROM edge_person_messaged",
    ),
    "edge_person_conversed": (
        "SELECT person1_id AS s, person2_id AS d, exchange_count * 5.0 AS w FROM edge_person_conversed",
    ),
    "edge_person_in_group": (
        "SELECT e1.person_id AS s, e2.person_id AS d, COUNT(*) * 1.0 AS w "
        "FROM edge_person_in_group e1 JOIN edge_person_in_group e2 "
        "ON e1.group_jid = e2.group_jid AND e1.person_id < e2.person_id GROUP BY 1, 2",
    ),
}


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

    import numpy as np

    t0 = time.time()
    union = _edge_union(conn, _LEIDEN_PARTS)
    if union is None:
        return {}

    # Was: ~2.5M rows fetched as Python tuples and de-duplicated with list.index() (11.6 s).
    conn.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _leiden_edges AS
        WITH e AS (
          {union}
        ), u AS (
          SELECT LEAST(s, d) AS a, GREATEST(s, d) AS b, SUM(w) AS w FROM e GROUP BY 1, 2
        )
        SELECT u.a, u.b, u.w
        FROM u
        JOIN graph_persons pa ON pa.person_id = u.a
        JOIN graph_persons pb ON pb.person_id = u.b
        """
    )
    try:
        nodes = conn.execute(
            """
            SELECT person_id, source_id FROM graph_persons
            WHERE person_id IN (SELECT a FROM _leiden_edges UNION SELECT b FROM _leiden_edges)
            ORDER BY source_id
            """
        ).fetchnumpy()
        edges = conn.execute("SELECT a, b, w FROM _leiden_edges").fetchnumpy()
    finally:
        conn.execute("DROP TABLE IF EXISTS _leiden_edges")

    node_list = [str(j) for j in nodes["source_id"]]
    if not node_list:
        return {}
    pids = np.asarray(nodes["person_id"], dtype=np.int64)
    order = np.argsort(pids)
    pos = order[np.searchsorted(pids[order], np.asarray(edges["a"], dtype=np.int64))]
    pos_b = order[np.searchsorted(pids[order], np.asarray(edges["b"], dtype=np.int64))]

    G = ig.Graph(n=len(node_list), edges=np.column_stack([pos, pos_b]).tolist(), directed=False)
    G.es["weight"] = np.asarray(edges["w"], dtype=np.float64).tolist()

    partition = G.community_leiden(
        objective_function="modularity",
        weights="weight",
        resolution=resolution,
        n_iterations=3,
    )

    communities = {node_list[i]: cid for i, cid in enumerate(partition.membership)}
    log.info(
        "Leiden: %d communities from %d people in %.2fs",
        len(set(communities.values())),
        len(communities),
        time.time() - t0,
    )
    return communities


def store_communities(
    conn: duckdb.DuckDBPyConnection,
    communities: dict[str, int],
) -> dict[int, dict]:
    if not communities:
        return {}

    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'graph_persons'"
    ).fetchall()}
    if "community_id" not in cols:
        conn.execute("ALTER TABLE graph_persons ADD COLUMN community_id INTEGER DEFAULT -1")
    else:
        conn.execute("UPDATE graph_persons SET community_id = -1")

    # One statement instead of one UPDATE per person (~9.7k statements, 25 s).
    import numpy as np

    jids = list(communities)
    ids = np.fromiter((communities[j] for j in jids), dtype=np.int32, count=len(jids))
    with bulk_rows(conn, "_communities", source_id=jids, community_id=ids):
        conn.execute(
            """
            UPDATE graph_persons SET community_id = c.community_id
            FROM _communities c
            WHERE graph_persons.source_id = c.source_id
            """
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
                    ORDER BY total DESC, gg.group_name LIMIT 3
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
                    ORDER BY total DESC, ge.entity_value LIMIT 5
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
