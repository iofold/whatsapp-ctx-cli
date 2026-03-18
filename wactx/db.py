from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from wactx.config import Config

log = logging.getLogger("wactx.db")

SCHEMA_VERSION = 1

CORE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id VARCHAR PRIMARY KEY,
    chat_jid VARCHAR,
    sender_jid VARCHAR,
    is_from_me BOOLEAN DEFAULT false,
    is_group BOOLEAN DEFAULT false,
    timestamp TIMESTAMP,
    msg_type VARCHAR DEFAULT 'text',
    text_content VARCHAR,
    media_type VARCHAR,
    media_path VARCHAR,
    media_downloaded BOOLEAN DEFAULT false,
    push_name VARCHAR,
    sent_date DATE,
    sent_hour INTEGER,
    sent_dow INTEGER
);

CREATE TABLE IF NOT EXISTS contacts (
    jid VARCHAR PRIMARY KEY,
    push_name VARCHAR,
    full_name VARCHAR,
    business_name VARCHAR,
    is_group BOOLEAN DEFAULT false,
    group_name VARCHAR,
    phone VARCHAR
);

CREATE TABLE IF NOT EXISTS classifications (
    message_id VARCHAR,
    chat_jid VARCHAR,
    category VARCHAR,
    confidence DOUBLE,
    summary VARCHAR,
    PRIMARY KEY (message_id, chat_jid)
);

CREATE TABLE IF NOT EXISTS extracted_entities (
    message_id VARCHAR NOT NULL,
    chat_jid VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_value VARCHAR NOT NULL,
    PRIMARY KEY (message_id, entity_type, entity_value)
);

CREATE TABLE IF NOT EXISTS _meta (
    key VARCHAR PRIMARY KEY,
    value VARCHAR
);
"""


def get_connection(
    config: Config, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    db_path = config.db_path_resolved
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path), read_only=read_only)
    return conn


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(CORE_TABLES_SQL)
    existing = conn.execute(
        "SELECT value FROM _meta WHERE key = 'schema_version'"
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('schema_version', ?)",
            [str(SCHEMA_VERSION)],
        )
    log.debug("Schema ensured (v%d)", SCHEMA_VERSION)


def get_table_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
    counts = {}
    for t in tables:
        if t.startswith("_"):
            continue
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return counts


def table_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()
    return row is not None and row[0] > 0
