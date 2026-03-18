from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence

import duckdb
from openai import AsyncOpenAI

from wactx.config import Config
from wactx.db import get_connection

log = logging.getLogger("wactx.embed")

BATCH_SIZE = 100
MAX_TEXT_CHARS = 8000


def ensure_embedding_column(conn: duckdb.DuckDBPyConnection, dims: int) -> None:
    cols = {c[0]: c[1] for c in conn.execute("DESCRIBE messages").fetchall()}
    expected = f"FLOAT[{dims}]"
    if "embedding" in cols:
        if cols["embedding"] != expected:
            log.info(
                "Migrating embedding column: %s -> %s", cols["embedding"], expected
            )
            try:
                conn.execute("DROP INDEX IF EXISTS idx_msg_embedding")
            except Exception:
                pass
            conn.execute("ALTER TABLE messages DROP COLUMN embedding")
            conn.execute(f"ALTER TABLE messages ADD COLUMN embedding FLOAT[{dims}]")
    else:
        conn.execute(f"ALTER TABLE messages ADD COLUMN embedding FLOAT[{dims}]")


def create_hnsw_index(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("INSTALL vss; LOAD vss;")
    conn.execute("SET hnsw_enable_experimental_persistence = true")
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_embedding "
            "ON messages USING HNSW (embedding) WITH (metric = 'cosine')"
        )
        log.info("HNSW index created")
    except Exception as e:
        if "already exists" in str(e).lower():
            log.info("HNSW index already exists")
        else:
            log.warning("Index creation failed: %s", e)


async def _embed_batch(
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
    model: str,
    dims: int,
    ids: Sequence[str],
    texts: Sequence[str],
    batch_idx: int,
) -> tuple[int, list[tuple[list[float], str]]]:
    async with sem:
        try:
            resp = await client.embeddings.create(
                model=model,
                input=list(texts),
                dimensions=dims,
            )
        except Exception as e:
            log.error("Embedding batch %d failed: %s", batch_idx + 1, e)
            return batch_idx, []

    payload = [
        (row.embedding, msg_id) for row, msg_id in zip(resp.data, ids, strict=False)
    ]
    return batch_idx, payload


async def embed_texts(
    conn: duckdb.DuckDBPyConnection, config: Config, reset: bool = False
) -> int:
    dims = config.api.embedding_dims
    conn.execute("INSTALL vss; LOAD vss;")
    ensure_embedding_column(conn, dims)

    if reset:
        log.info("Resetting all embeddings to NULL")
        try:
            conn.execute("DROP INDEX IF EXISTS idx_msg_embedding")
        except Exception:
            pass
        conn.execute("UPDATE messages SET embedding = NULL")

    remaining_row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE embedding IS NULL AND text_content IS NOT NULL AND TRIM(text_content) != ''"
    ).fetchone()
    remaining = int(remaining_row[0]) if remaining_row else 0

    if remaining == 0:
        log.info("All messages already embedded")
        return 0

    log.info("Embedding %d messages...", remaining)
    rows = conn.execute(
        "SELECT id, text_content FROM messages "
        "WHERE embedding IS NULL AND text_content IS NOT NULL AND TRIM(text_content) != '' "
        "ORDER BY id"
    ).fetchall()

    clean_rows: list[tuple[str, str]] = []
    for msg_id, text_content in rows:
        cleaned = (text_content or "").strip()
        if not cleaned:
            continue
        clean_rows.append((msg_id, cleaned[:MAX_TEXT_CHARS]))

    batches: list[tuple[list[str], list[str]]] = []
    for i in range(0, len(clean_rows), BATCH_SIZE):
        chunk = clean_rows[i : i + BATCH_SIZE]
        ids = [row[0] for row in chunk]
        texts = [row[1] for row in chunk]
        batches.append((ids, texts))

    client = AsyncOpenAI(base_url=config.api.base_url, api_key=config.api.key)
    sem = asyncio.Semaphore(max(1, int(config.api.max_concurrent)))

    embedded = 0
    failed_batches = 0
    t0 = time.time()

    tasks = [
        asyncio.create_task(
            _embed_batch(client, sem, config.api.embedding_model, dims, ids, texts, idx)
        )
        for idx, (ids, texts) in enumerate(batches)
    ]

    completed = 0
    for task in asyncio.as_completed(tasks):
        _, payload = await task
        completed += 1

        if payload:
            conn.executemany("UPDATE messages SET embedding = ? WHERE id = ?", payload)
            embedded += len(payload)
        else:
            failed_batches += 1

        if completed % 20 == 0 or completed == len(batches):
            elapsed = time.time() - t0
            rate = embedded / elapsed if elapsed > 0 else 0
            log.info(
                "Embedded batches %d/%d (%d/%d messages, %.0f msg/s, %d batch errors)",
                completed,
                len(batches),
                embedded,
                len(clean_rows),
                rate,
                failed_batches,
            )

    elapsed = time.time() - t0
    log.info(
        "Text embedding complete: %d messages in %.1fs (%.0f msg/s, %d batch errors)",
        embedded,
        elapsed,
        embedded / elapsed if elapsed > 0 else 0,
        failed_batches,
    )
    return embedded


async def run_pipeline(config: Config, reset: bool = False) -> None:
    conn = get_connection(config)
    try:
        ensure_embedding_column(conn, config.api.embedding_dims)
        await embed_texts(conn, config, reset)
        create_hnsw_index(conn)
    finally:
        conn.close()
