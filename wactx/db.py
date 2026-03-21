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
            embedding FLOAT[768],
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


def ensure_fts_index(conn: duckdb.DuckDBPyConnection) -> None:
    try:
        conn.execute("INSTALL fts")
        conn.execute("LOAD fts")
    except Exception:
        pass
    try:
        conn.execute(
            "PRAGMA create_fts_index('messages', 'id', 'text_content', "
            "stemmer='english', stopwords='english', overwrite=1)"
        )
    except Exception as e:
        logging.getLogger("wactx.db").warning("FTS index creation failed: %s", e)


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
