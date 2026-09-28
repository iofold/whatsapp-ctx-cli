from wactx import search
from wactx.config import Config
from wactx.db import FTS_SCHEMA, ensure_fts_index, get_connection
from wactx.embed import create_hnsw_index, ensure_embedding_column

DIMS = 4


class RecordingConn:
    """Pass-through connection that records the SQL a search function runs."""

    def __init__(self, conn):
        self.conn = conn
        self.sql: list[tuple[str, list | None]] = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return self.conn.execute(sql, params) if params is not None else self.conn.execute(sql)


def _db(tmp_path, rows):
    """rows: (id, chat_jid, text, embedding)"""
    conn = get_connection(Config(db_path=tmp_path / "test.duckdb"))
    ensure_embedding_column(conn, DIMS)
    for mid, chat, text, emb in rows:
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender_jid, text_content, embedding) VALUES (?, ?, 's', ?, ?)",
            [mid, chat, text, emb],
        )
    ensure_fts_index(conn)
    return conn


ROWS = [
    ("dup", "chat-a@g.us", "quarterly invoice reminder", [1.0, 0.0, 0.0, 0.0]),
    ("dup", "chat-b@g.us", "quarterly invoice reminder", [0.9, 0.1, 0.0, 0.0]),
    ("m1", "chat-a@g.us", "lunch plans for friday", [0.0, 1.0, 0.0, 0.0]),
    ("m2", "chat-b@g.us", "flight lands at noon", [0.0, 0.0, 1.0, 0.0]),
    ("m3", "chat-b@g.us", "invoice paid, thanks", [0.7, 0.0, 0.0, 0.3]),
]


def test_bm25_finds_messages_that_share_an_id_across_chats(tmp_path):
    conn = _db(tmp_path, ROWS)
    results = search.bm25_search(conn, "invoice", top_k=10)
    keys = {(r["id"], r["chat_jid"]) for r in results}
    assert keys == {("dup", "chat-a@g.us"), ("dup", "chat-b@g.us"), ("m3", "chat-b@g.us")}


def test_bm25_respects_chat_filter(tmp_path):
    conn = _db(tmp_path, ROWS)
    results = search.bm25_search(conn, "invoice", top_k=10, chat_jids=["chat-a@g.us"])
    assert [(r["id"], r["chat_jid"]) for r in results] == [("dup", "chat-a@g.us")]


def test_fts_index_rebuilds_only_when_messages_change(tmp_path):
    conn = _db(tmp_path, ROWS)
    rec = RecordingConn(conn)
    ensure_fts_index(rec)
    assert not any("create_fts_index" in sql for sql, _ in rec.sql)

    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender_jid, text_content) VALUES ('m4', 'chat-a@g.us', 's', 'new invoice attached')"
    )
    ensure_fts_index(conn)
    assert ("m4", "chat-a@g.us") in {(r["id"], r["chat_jid"]) for r in search.bm25_search(conn, "invoice", top_k=10)}


def test_fts_index_replaces_legacy_index_keyed_on_message_id(tmp_path):
    conn = _db(tmp_path, ROWS)
    conn.execute(
        "PRAGMA create_fts_index('messages', 'id', 'text_content', stemmer='english', stopwords='english', overwrite=1)"
    )
    conn.execute("DROP TABLE messages_fts_doc")
    ensure_fts_index(conn)
    schemas = {r[0] for r in conn.execute("SELECT DISTINCT schema_name FROM duckdb_tables()").fetchall()}
    assert FTS_SCHEMA in schemas
    assert "fts_main_messages" not in schemas


def test_unfiltered_vector_search_uses_hnsw_index(tmp_path):
    conn = _db(tmp_path, ROWS)
    create_hnsw_index(conn)
    rec = RecordingConn(conn)
    results = search.semantic_search(rec, [[1.0, 0.0, 0.0, 0.0]], DIMS, top_k=3)

    sql, params = next((s, p) for s, p in rec.sql if "array_cosine_distance" in s)
    plan = conn.execute("EXPLAIN " + sql, params).fetchall()[0][1]
    assert "HNSW_INDEX_SCAN" in plan
    assert [(r["id"], r["chat_jid"]) for r in results] == [
        ("dup", "chat-a@g.us"),
        ("dup", "chat-b@g.us"),
        ("m3", "chat-b@g.us"),
    ]
    assert results[0]["similarity"] == 1.0


def test_filtered_vector_search_is_exact_despite_index(tmp_path):
    conn = _db(tmp_path, ROWS)
    create_hnsw_index(conn)
    results = search.semantic_search(conn, [[1.0, 0.0, 0.0, 0.0]], DIMS, top_k=5, chat_jids=["chat-b@g.us"])
    assert [(r["id"], r["chat_jid"]) for r in results] == [
        ("dup", "chat-b@g.us"),
        ("m3", "chat-b@g.us"),
        ("m2", "chat-b@g.us"),
    ]


def test_rrf_fuse_keeps_messages_that_share_an_id():
    a = {"id": "dup", "chat_jid": "chat-a@g.us"}
    b = {"id": "dup", "chat_jid": "chat-b@g.us"}
    fused = search.rrf_fuse([("bm25", [a, b]), ("vector", [b])])
    assert [(d["id"], d["chat_jid"]) for d in fused] == [("dup", "chat-b@g.us"), ("dup", "chat-a@g.us")]
