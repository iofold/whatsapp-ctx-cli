from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import duckdb

from wactx.config import Config

SCHEMA_VERSION = 1


EXTENSIONS = ["vss", "duckpgq"]


def get_connection(
    config: Config, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    db_path = Path(config.db_path)
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path), read_only=read_only)
    _load_extensions(conn)
    if not read_only:
        ensure_schema(conn)
    return conn


EXTENSION_INSTALL = {
    "vss": "INSTALL vss",
    "duckpgq": "INSTALL duckpgq FROM community",
}


def _load_extensions(conn: duckdb.DuckDBPyConnection) -> None:
    for ext in EXTENSIONS:
        try:
            conn.execute(EXTENSION_INSTALL.get(ext, f"INSTALL {ext}"))
            conn.execute(f"LOAD {ext}")
        except Exception:
            pass


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id VARCHAR NOT NULL,
            chat_jid VARCHAR NOT NULL,
            sender_jid VARCHAR NOT NULL,
            is_from_me BOOLEAN NOT NULL DEFAULT FALSE,
            is_group BOOLEAN NOT NULL DEFAULT FALSE,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            msg_type VARCHAR NOT NULL DEFAULT 'text',
            text_content VARCHAR,
            media_type VARCHAR,
            push_name VARCHAR,
            sent_date DATE NOT NULL DEFAULT CURRENT_DATE,
            sent_hour UTINYINT NOT NULL DEFAULT 0,
            sent_dow UTINYINT NOT NULL DEFAULT 0,
            raw_proto BLOB,
            media_downloaded BOOLEAN DEFAULT FALSE,
            media_path VARCHAR,
            PRIMARY KEY (id, chat_jid)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contacts (
            jid VARCHAR PRIMARY KEY,
            push_name VARCHAR,
            full_name VARCHAR,
            business_name VARCHAR,
            is_group BOOLEAN DEFAULT FALSE,
            group_name VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS classifications (
            message_id VARCHAR NOT NULL,
            chat_jid VARCHAR NOT NULL,
            category VARCHAR NOT NULL,
            confidence VARCHAR,
            summary VARCHAR,
            PRIMARY KEY (message_id, chat_jid)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS extracted_entities (
            id BIGINT,
            message_id VARCHAR,
            chat_jid VARCHAR,
            entity VARCHAR,
            entity_type VARCHAR,
            confidence DOUBLE,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key VARCHAR PRIMARY KEY,
            value VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO schema_meta (key, value)
        SELECT 'schema_version', ?
        WHERE NOT EXISTS (
            SELECT 1 FROM schema_meta WHERE key = 'schema_version'
        )
        """,
        [str(SCHEMA_VERSION)],
    )
    ensure_fts_index(conn)


FTS_DOC_TABLE = "messages_fts_doc"
FTS_SCHEMA = f"fts_main_{FTS_DOC_TABLE}"


def fts_doc_key(prefix: str = "") -> str:
    """SQL for a message's full-text document key; `prefix` qualifies columns, e.g. "m."."""
    return f"{prefix}id || '|' || {prefix}chat_jid"


def ensure_fts_index(conn: duckdb.DuckDBPyConnection) -> None:
    """Build the BM25 index over `messages` when it is missing or behind.

    DuckDB FTS needs a unique document id, and message ids repeat across chats (7,989 on
    2026-09-28), so an index keyed on `messages.id` made every match_bm25 call fail. The
    index is built over a side table keyed by id|chat_jid instead.
    """
    try:
        conn.execute("INSTALL fts")
        conn.execute("LOAD fts")
    except Exception:
        pass
    log = logging.getLogger("wactx.db")
    try:
        eligible = conn.execute(
            "SELECT count(*) FROM messages WHERE text_content IS NOT NULL AND trim(text_content) <> ''"
        ).fetchone()
        indexed = None
        if _fts_index_exists(conn):
            indexed = conn.execute(f"SELECT count(*) FROM {FTS_DOC_TABLE}").fetchone()
        if eligible and indexed and eligible[0] == indexed[0]:
            return
        conn.execute(
            f"CREATE OR REPLACE TABLE {FTS_DOC_TABLE} AS "
            f"SELECT {fts_doc_key()} AS doc_key, text_content FROM messages "
            "WHERE text_content IS NOT NULL AND trim(text_content) <> ''"
        )
        conn.execute(
            f"PRAGMA create_fts_index('{FTS_DOC_TABLE}', 'doc_key', 'text_content', "
            "stemmer='english', stopwords='english', overwrite=1)"
        )
        if _schema_has_table(conn, "fts_main_messages", "docs"):
            conn.execute("PRAGMA drop_fts_index('messages')")
    except Exception as e:
        log.warning("FTS index creation failed: %s", e)


def _fts_index_exists(conn: duckdb.DuckDBPyConnection) -> bool:
    return table_exists(conn, FTS_DOC_TABLE) and _schema_has_table(conn, FTS_SCHEMA, "docs")


def _schema_has_table(conn: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE schema_name = ? AND table_name = ?",
        [schema, table],
    ).fetchone()
    return bool(row and row[0])


def table_exists(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?",
        [table_name],
    ).fetchone()
    return row is not None


def get_table_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("messages", "contacts", "classifications", "extracted_entities"):
        if not table_exists(conn, table):
            continue
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        if row is None:
            counts[table] = 0
            continue
        row_tuple = cast(tuple[Any, ...], row)
        counts[table] = int(row_tuple[0])
    return counts
