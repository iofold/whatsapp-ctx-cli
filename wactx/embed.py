from __future__ import annotations

import asyncio
import logging
import time

import duckdb
from openai import AsyncOpenAI

from wactx.config import Config

log = logging.getLogger("wactx.embed")


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
    conn.execute("INSTALL vss; LOAD vss")
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
    texts: list[str],
    ids: list[str],
    conn: duckdb.DuckDBPyConnection,
) -> int:
    async with sem:
        try:
            resp = await client.embeddings.create(
                model=model, input=texts, dimensions=dims
            )
        except Exception as e:
            log.error("Batch failed: %s", e)
            return 0
        data = [(emb.embedding, mid) for emb, mid in zip(resp.data, ids)]
        conn.executemany("UPDATE messages SET embedding = ? WHERE id = ?", data)
        return len(data)


async def embed_texts(
    conn: duckdb.DuckDBPyConnection, config: Config, reset: bool = False
) -> int:
    dims = config.api.embedding_dims
    conn.execute("INSTALL vss; LOAD vss")
    ensure_embedding_column(conn, dims)

    if reset:
        log.info("Resetting all embeddings to NULL")
        try:
            conn.execute("DROP INDEX IF EXISTS idx_msg_embedding")
        except Exception:
            pass
        conn.execute("UPDATE messages SET embedding = NULL")

    remaining = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE embedding IS NULL AND text_content IS NOT NULL AND TRIM(text_content) != ''"
    ).fetchone()[0]

    if remaining == 0:
        log.info("All messages already embedded")
        return 0

    log.info("Embedding %d messages...", remaining)
    rows = conn.execute(
        "SELECT id, text_content FROM messages "
        "WHERE embedding IS NULL AND text_content IS NOT NULL AND TRIM(text_content) != '' "
        "ORDER BY id"
    ).fetchall()

    batch_size = config.api.batch_size
    batches = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        ids = [r[0] for r in chunk]
        texts = [r[1].strip()[:8000] or "." for r in chunk]
        batches.append((ids, texts))

    client = AsyncOpenAI(base_url=config.api.base_url, api_key=config.api.key)
    sem = asyncio.Semaphore(config.api.max_concurrent)

    embedded = 0
    t0 = time.time()
    wave_size = config.api.max_concurrent * 5

    for wave_start in range(0, len(batches), wave_size):
        wave_end = min(wave_start + wave_size, len(batches))
        tasks = [
            _embed_batch(
                client, sem, config.api.embedding_model, dims, texts, ids, conn
            )
            for ids, texts in batches[wave_start:wave_end]
        ]
        results = await asyncio.gather(*tasks)
        embedded += sum(results)
        elapsed = time.time() - t0
        rate = embedded / elapsed if elapsed > 0 else 0
        log.info(
            "Progress: %d/%d (%.0f msg/s, %.0fs)", embedded, remaining, rate, elapsed
        )

    elapsed = time.time() - t0
    log.info(
        "Embedded %d messages in %.1fs (%.0f msg/s)",
        embedded,
        elapsed,
        embedded / elapsed if elapsed > 0 else 0,
    )
    return embedded


async def run_pipeline(config: Config, reset: bool = False) -> None:
    from wactx.db import get_connection

    conn = get_connection(config)
    try:
        count = await embed_texts(conn, config, reset)
        if count > 0:
            create_hnsw_index(conn)
    finally:
        conn.close()
