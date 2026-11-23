"""The scipy person graph must reproduce the networkx graph and pagerank it replaced."""

import duckdb
import networkx as nx
import pytest

from wactx import graph_search

PERSONS = [(i, f"p{i}@s.whatsapp.net") for i in range(1, 9)]
MESSAGED = [(1, 2, 4), (2, 1, 1), (2, 3, 2), (3, 3, 1), (4, 1, 3), (1, 2, 1)]  # dup pair, self-loop
CONVERSED = [(1, 5, 2), (5, 6, 1)]
IN_GROUP = [(1, "g1"), (2, "g1"), (5, "g1"), (6, "g2"), (7, "g2"), (2, "g2")]
MENTIONS = [(1, 10), (3, 10), (7, 11), (4, 11)]
# person 8 has no edges at all; person 4 only points outward (dangling inbound only)


def _make_db(include=("messaged", "conversed", "in_group", "mentions")):
    conn = duckdb.connect()
    conn.execute("CREATE TABLE graph_persons (person_id BIGINT, source_id VARCHAR)")
    conn.executemany("INSERT INTO graph_persons VALUES (?, ?)", PERSONS)
    if "messaged" in include:
        conn.execute("CREATE TABLE edge_person_messaged (sender_person_id BIGINT, receiver_person_id BIGINT, message_count INT)")
        conn.executemany("INSERT INTO edge_person_messaged VALUES (?, ?, ?)", MESSAGED)
    if "conversed" in include:
        conn.execute("CREATE TABLE edge_person_conversed (person1_id BIGINT, person2_id BIGINT, exchange_count INT)")
        conn.executemany("INSERT INTO edge_person_conversed VALUES (?, ?, ?)", CONVERSED)
    if "in_group" in include:
        conn.execute("CREATE TABLE edge_person_in_group (person_id BIGINT, group_jid VARCHAR)")
        conn.executemany("INSERT INTO edge_person_in_group VALUES (?, ?)", IN_GROUP)
    if "mentions" in include:
        conn.execute("CREATE TABLE edge_person_mentions_entity (person_id BIGINT, entity_id BIGINT)")
        conn.executemany("INSERT INTO edge_person_mentions_entity VALUES (?, ?)", MENTIONS)
    return conn


def _reference_nx_graph() -> nx.DiGraph:
    """The edge construction of the networkx implementation this module replaced."""
    jid = dict(PERSONS)
    G = nx.DiGraph()

    def add(u, v, w):
        G.add_edge(jid[u], jid[v], weight=G.get_edge_data(jid[u], jid[v], {}).get("weight", 0) + w)

    for s, d, c in MESSAGED:
        add(s, d, c * 3.0)
    for a, b, c in CONVERSED:
        add(a, b, c * 5.0)
        add(b, a, c * 5.0)
    for table, scale in ((IN_GROUP, 1.0), (MENTIONS, 2.0)):
        pairs = {}
        for p1, k1 in table:
            for p2, k2 in table:
                if k1 == k2 and p1 != p2:
                    pairs[(p1, p2)] = pairs.get((p1, p2), 0) + 1
        for (p1, p2), n in pairs.items():
            add(p1, p2, n * scale)
    return G


@pytest.fixture(autouse=True)
def _fresh_cache():
    graph_search.clear_graph_cache()
    yield
    graph_search.clear_graph_cache()


def test_graph_matches_networkx_construction():
    G = graph_search.load_person_graph(_make_db())
    ref = _reference_nx_graph()
    assert set(G.node_ids) == set(ref.nodes)
    assert G.num_edges == ref.number_of_edges()
    for u, v, w in ref.edges(data="weight"):
        assert G.adj[G.index[u], G.index[v]] == pytest.approx(w)


@pytest.mark.parametrize("seeds", [["p1@s.whatsapp.net"], ["p3@s.whatsapp.net", "p6@s.whatsapp.net", "p7@s.whatsapp.net"]])
def test_ppr_matches_networkx_pagerank(seeds):
    conn = _make_db()
    seed_scores = {j: 1.0 + i for i, j in enumerate(seeds)}
    got = dict(graph_search.ppr_score(conn, seeds, seed_scores, top_k=100))
    total = sum(seed_scores.values())
    expected = nx.pagerank(
        _reference_nx_graph(),
        alpha=0.85,
        personalization={j: seed_scores[j] / total for j in seeds},
        weight="weight",
        max_iter=100,
        tol=1e-8,
    )
    assert set(got) == {k for k, v in expected.items() if v > 0}
    for k, v in got.items():
        assert v == pytest.approx(expected[k], abs=1e-12)


def test_pathrag_paths_are_shortest_and_deterministic():
    conn = _make_db()
    ref = _reference_nx_graph()
    seeds = [j for _, j in PERSONS[:7]]
    first = graph_search.pathrag_flow(conn, seeds, theta=0.0)
    assert first, "expected at least one path on a connected toy graph"
    for p in first:
        path = p["path"]
        assert path[0] == p["source"] and path[-1] == p["target"]
        assert all(ref.has_edge(a, b) for a, b in zip(path, path[1:]))
        assert len(path) - 1 == nx.shortest_path_length(ref, p["source"], p["target"])
    graph_search.clear_graph_cache()
    assert graph_search.pathrag_flow(conn, seeds, theta=0.0) == first


def test_missing_edge_tables_are_skipped():
    G = graph_search.load_person_graph(_make_db(include=("messaged",)))
    assert G.num_edges == 5  # (1,2) duplicates merge; self-loop kept
    graph_search.clear_graph_cache()
    empty = graph_search.load_person_graph(_make_db(include=()))
    assert len(empty) == 0
    assert graph_search.ppr_score(_make_db(include=()), ["p1@s.whatsapp.net"]) == []


def test_materialized_graph_matches_aggregated_and_detects_staleness():
    conn = _make_db()
    aggregated = graph_search._load_aggregated(conn)
    assert graph_search.materialize_person_graph(conn) == aggregated.num_edges
    materialized = graph_search._load_materialized(conn)
    assert materialized is not None
    assert materialized.node_ids == aggregated.node_ids
    assert (materialized.adj != aggregated.adj).nnz == 0
    assert graph_search.load_person_graph(conn).num_edges == aggregated.num_edges

    # Any change to a source table makes the stored copy stale; loads fall back.
    conn.execute("INSERT INTO edge_person_messaged VALUES (6, 7, 9)")
    assert graph_search._load_materialized(conn) is None
    graph_search.clear_graph_cache()
    G = graph_search.load_person_graph(conn)
    assert G.adj[G.index["p6@s.whatsapp.net"], G.index["p7@s.whatsapp.net"]] > 0


def test_store_communities_bulk_update():
    conn = _make_db()
    communities = {"p1@s.whatsapp.net": 0, "p2@s.whatsapp.net": 0, "p3@s.whatsapp.net": 1}
    graph_search.store_communities(conn, communities)
    rows = dict(conn.execute("SELECT source_id, community_id FROM graph_persons").fetchall())
    assert {j: rows[j] for j in communities} == communities
    assert rows["p8@s.whatsapp.net"] == -1
    # Re-running resets people who left a community.
    graph_search.store_communities(conn, {"p1@s.whatsapp.net": 2})
    rows = dict(conn.execute("SELECT source_id, community_id FROM graph_persons").fetchall())
    assert rows["p1@s.whatsapp.net"] == 2 and rows["p2@s.whatsapp.net"] == -1


def test_leiden_assigns_every_connected_person():
    pytest.importorskip("igraph")
    communities = graph_search.leiden_communities(_make_db())
    connected = {f"p{i}@s.whatsapp.net" for i in range(1, 8)}
    assert set(communities) == connected
