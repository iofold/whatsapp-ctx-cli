import duckdb

from wactx.config import Config
from wactx.db import get_connection, ensure_schema, get_table_counts, table_exists


def test_ensure_schema(tmp_path):
    cfg = Config(db_path=str(tmp_path / "test.duckdb"))
    conn = get_connection(cfg)
    ensure_schema(conn)

    tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
    assert "messages" in tables
    assert "contacts" in tables
    assert "extracted_entities" in tables


def test_get_table_counts(tmp_path):
    cfg = Config(db_path=str(tmp_path / "test.duckdb"))
    conn = get_connection(cfg)
    ensure_schema(conn)

    counts = get_table_counts(conn)
    assert counts["messages"] == 0
    assert counts["contacts"] == 0


def test_table_exists(tmp_path):
    cfg = Config(db_path=str(tmp_path / "test.duckdb"))
    conn = get_connection(cfg)
    ensure_schema(conn)

    assert table_exists(conn, "messages")
    assert not table_exists(conn, "nonexistent_table")


def test_insert_and_count(tmp_path):
    cfg = Config(db_path=str(tmp_path / "test.duckdb"))
    conn = get_connection(cfg)
    ensure_schema(conn)

    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender_jid, text_content) VALUES (?, ?, ?, ?)",
        ["msg1", "chat1", "sender1", "hello world"],
    )
    counts = get_table_counts(conn)
    assert counts["messages"] == 1
