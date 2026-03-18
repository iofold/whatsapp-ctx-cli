from __future__ import annotations

import asyncio
import json
import logging
import time

import duckdb
from openai import AsyncOpenAI

from wactx.config import Config
from wactx.db import table_exists

log = logging.getLogger("wactx.entities")

ENTITY_TYPES = ["person", "org", "tech", "url", "event"]

SYSTEM_PROMPT = """\
You are an entity extractor for WhatsApp group messages in tech/founder/AI communities.

Extract named entities from each message. Return a JSON array with one object per message.

Entity types:
- persons: Names of people mentioned (not the sender themselves)
- orgs: Companies, organizations, funds, accelerators, universities
- techs: Technologies, tools, frameworks, programming languages, AI models
- urls: URLs or domains mentioned
- events: Named events, conferences, meetups, demo days

Rules:
- Return JSON array ONLY — no markdown, no code fences, no explanation
- Each element: {"id": "<message_id>", "persons": [...], "orgs": [...], "techs": [...], "urls": [...], "events": [...]}
- Empty arrays for categories with no matches
- Normalize names: "GCP" -> "Google Cloud", "k8s" -> "Kubernetes"
- Skip generic terms: "the app", "the company", "a startup"
- Skip the sender's own name
- Be conservative — only extract clearly named entities
"""


def ensure_entity_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extracted_entities (
            message_id VARCHAR NOT NULL,
            chat_jid VARCHAR NOT NULL,
            entity_type VARCHAR NOT NULL,
            entity_value VARCHAR NOT NULL,
            PRIMARY KEY (message_id, entity_type, entity_value)
        )
    """)


async def _extract_batch(
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
    model: str,
    batch: list[dict],
    batch_idx: int,
    total: int,
) -> list[tuple[str, str, str, str]]:
    async with sem:
        prompt_lines = []
        id_to_chat = {}
        for m in batch:
            text = m["text"][:500]
            prompt_lines.append(f"[{m['id']}] ({m['group']}) {m['sender']}: {text}")
            id_to_chat[m["id"]] = m["chat_jid"]

        for attempt in range(3):
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": "\n".join(prompt_lines)},
                    ],
                    max_tokens=4096,
                )
                raw = (resp.choices[0].message.content or "").strip()
                if not raw:
                    return []
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()

                parsed = json.loads(raw)
                results = []
                for item in parsed:
                    mid = item.get("id", "")
                    chat_jid = id_to_chat.get(mid, "")
                    if not chat_jid:
                        continue
                    for plural, singular in [
                        ("persons", "person"),
                        ("orgs", "org"),
                        ("techs", "tech"),
                        ("urls", "url"),
                        ("events", "event"),
                    ]:
                        for val in item.get(plural, []):
                            if val and isinstance(val, str) and len(val.strip()) > 1:
                                results.append(
                                    (mid, chat_jid, singular, val.strip()[:200])
                                )

                if (batch_idx + 1) % 10 == 0 or batch_idx + 1 == total:
                    log.info(
                        "  Batch %d/%d: %d entities", batch_idx + 1, total, len(results)
                    )
                return results

            except json.JSONDecodeError:
                if attempt == 2:
                    return []
            except Exception as e:
                log.warning("Batch %d attempt %d: %s", batch_idx + 1, attempt + 1, e)
                await asyncio.sleep(2 ** (attempt + 1))
                if attempt == 2:
                    return []
    return []


async def extract_entities(
    conn: duckdb.DuckDBPyConnection, config: Config, process_all: bool = False
) -> int:
    ensure_entity_table(conn)

    where_extra = ""
    if not process_all:
        where_extra = (
            " AND m.id NOT IN (SELECT DISTINCT message_id FROM extracted_entities)"
            " AND cl.category NOT IN ('banter', 'other')"
        )

    has_cl = table_exists(conn, "classifications")
    if not has_cl and not process_all:
        log.info("No classifications table — extracting from all group messages")
        where_extra = (
            " AND m.id NOT IN (SELECT DISTINCT message_id FROM extracted_entities)"
        )

    join_cl = (
        "LEFT JOIN classifications cl ON m.id = cl.message_id AND m.chat_jid = cl.chat_jid"
        if has_cl
        else ""
    )
    where_cl = "AND cl.message_id IS NOT NULL" if has_cl and not process_all else ""

    rows = conn.execute(f"""
        SELECT m.id, m.chat_jid, m.push_name, m.text_content,
               COALESCE(c.group_name, m.chat_jid) AS group_name
        FROM messages m
        LEFT JOIN contacts c ON m.chat_jid = c.jid
        {join_cl}
        WHERE m.is_group = true
          AND m.text_content IS NOT NULL
          AND TRIM(m.text_content) != ''
          {where_cl}
          {where_extra}
        ORDER BY m.chat_jid, m.timestamp ASC
    """).fetchall()

    messages = [
        {
            "id": r[0],
            "chat_jid": r[1],
            "sender": r[2] or "Unknown",
            "text": r[3],
            "group": r[4],
        }
        for r in rows
    ]

    if not messages:
        log.info("No messages to extract entities from")
        return 0

    log.info("Extracting entities from %d messages...", len(messages))

    batch_size = 40
    batches = [
        messages[i : i + batch_size] for i in range(0, len(messages), batch_size)
    ]

    client = AsyncOpenAI(base_url=config.api.base_url, api_key=config.api.key)
    sem = asyncio.Semaphore(config.api.max_concurrent)

    t0 = time.time()
    tasks = [
        _extract_batch(client, sem, config.api.chat_model, b, i, len(batches))
        for i, b in enumerate(batches)
    ]
    all_results = await asyncio.gather(*tasks)

    flat = [r for batch_results in all_results for r in batch_results]
    elapsed = time.time() - t0
    log.info("Extracted %d entity mentions in %.1fs", len(flat), elapsed)

    for i in range(0, len(flat), 1000):
        conn.executemany(
            "INSERT OR IGNORE INTO extracted_entities (message_id, chat_jid, entity_type, entity_value) VALUES (?, ?, ?, ?)",
            flat[i : i + 1000],
        )

    return len(flat)


def get_entity_stats(conn: duckdb.DuckDBPyConnection) -> dict:
    if not table_exists(conn, "extracted_entities"):
        return {}
    stats = {}
    for etype in ENTITY_TYPES:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT entity_value) FROM extracted_entities WHERE entity_type = ?",
            [etype],
        ).fetchone()
        stats[etype] = {"mentions": row[0], "unique": row[1]}
    stats["total"] = conn.execute("SELECT COUNT(*) FROM extracted_entities").fetchone()[
        0
    ]
    stats["messages"] = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM extracted_entities"
    ).fetchone()[0]
    return stats
