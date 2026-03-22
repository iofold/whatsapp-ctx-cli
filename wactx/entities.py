from __future__ import annotations

import logging
import importlib
import re
import time

import duckdb

from wactx.config import Config
from wactx.db import table_exists

log = logging.getLogger("wactx.entities")

ENTITY_TYPES = ["person", "org", "tech", "url", "event"]

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
_MENTION_RE = re.compile(r"@(\+?\d[\d\s\-]{5,}\d|[\w.]+)", re.UNICODE)

_SPACY_TO_ENTITY_TYPE: dict[str, str] = {
    "PERSON": "person",
    "ORG": "org",
    "GPE": "org",
    "EVENT": "event",
    "PRODUCT": "tech",
    "WORK_OF_ART": "tech",
    "FAC": "org",
}

_STOP_ENTITIES: set[str] = {
    "ai",
    "dm",
    "us",
    "usa",
    "uk",
    "api",
    "llm",
    "ui",
    "ux",
    "sf",
    "ip",
    "gtm",
    "cc",
    "cli",
    "sdk",
    "vc",
    "pm",
    "cto",
    "ceo",
    "hr",
    "pr",
    "qa",
    "ot",
    "it",
    "ml",
    "dl",
    "nlp",
    "cv",
    "rl",
    "ops",
    "swe",
    "oss",
    "india",
    "bangalore",
    "mumbai",
    "delhi",
    "chennai",
    "hyderabad",
    "pune",
    "san francisco",
    "new york",
    "london",
    "singapore",
    "dubai",
    "austin",
    "today",
    "tomorrow",
    "yesterday",
    "daily",
    "weekly",
    "next week",
    "last week",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "2024",
    "2025",
    "2026",
    "2027",
    "the",
    "a",
    "an",
    "this",
    "that",
    "one",
    "two",
    "three",
    "max",
    "hey",
    "hi",
    "hello",
    "thanks",
    "thank",
    "yes",
    "no",
    "ok",
    "lol",
    "haha",
    "google",
    "apple",
    "meta",
    "amazon",
    "microsoft",
    "holi",
    "diwali",
    "christmas",
    "hai",
    "bhai",
    "ji",
    "sir",
    "spc",
    "intro",
    "time",
    "ca",
    "dming",
    "saas",
    "b2b",
    "b2c",
    "bengaluru",
    "gurgaon",
    "noida",
    "chennai",
    "kolkata",
    "jaipur",
    "chandigarh",
    "china",
    "japan",
    "canada",
    "europe",
    "africa",
}

_TYPE_OVERRIDES: dict[str, str] = {
    "claude": "tech",
    "claude code": "tech",
    "chatgpt": "tech",
    "gpt": "tech",
    "gemini": "tech",
    "copilot": "tech",
    "cursor": "tech",
    "openai": "org",
    "anthropic": "org",
    "whatsapp": "tech",
    "linkedin": "tech",
    "slack": "tech",
    "kubernetes": "tech",
    "docker": "tech",
    "react": "tech",
    "python": "tech",
    "langchain": "tech",
    "langgraph": "tech",
    "autogpt": "tech",
    "gpu": "tech",
    "mcp": "tech",
    "aws": "tech",
    "github": "tech",
    "linkedin": "tech",
    "phd": "org",
}

_ALIAS_MAP: dict[str, str] = {
    "claude code": "Claude Code",
    "cc": "Claude Code",
    "claude": "Claude",
    "chatgpt": "ChatGPT",
    "gpt": "GPT",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Gemini",
    "copilot": "Copilot",
    "cursor": "Cursor",
    "langchain": "LangChain",
    "langgraph": "LangGraph",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "docker": "Docker",
    "react": "React",
}


def _normalize_entity(etype: str, value: str) -> tuple[str, str] | None:
    v_lower = value.lower().strip()
    if v_lower in _STOP_ENTITIES or len(v_lower) < 2:
        return None
    if v_lower.startswith("@") or v_lower.startswith("http") or "@" in v_lower:
        return None
    if v_lower.isdigit():
        return None
    etype = _TYPE_OVERRIDES.get(v_lower, etype)
    canonical = _ALIAS_MAP.get(v_lower, value.strip())
    return etype, canonical


_NLP = None


def _get_nlp():
    global _NLP
    if _NLP is not None:
        return _NLP
    try:
        spacy = importlib.import_module("spacy")
        _NLP = spacy.load("en_core_web_sm", enable=["ner"])
    except OSError:
        log.warning(
            "SpaCy model not found. Install with: uv pip install en-core-web-sm@https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
        )
        spacy = importlib.import_module("spacy")
        _NLP = spacy.blank("en")
    return _NLP


def ensure_entity_table(conn: duckdb.DuckDBPyConnection) -> None:
    if table_exists(conn, "extracted_entities"):
        cols = {
            row[0] for row in conn.execute("DESCRIBE extracted_entities").fetchall()
        }
        required = {"message_id", "chat_jid", "entity_type", "entity_value"}
        if required.issubset(cols):
            return
        log.warning(
            "Existing extracted_entities schema is incompatible; recreating table with expected columns"
        )
        conn.execute("DROP TABLE extracted_entities")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS extracted_entities (
            message_id VARCHAR NOT NULL,
            chat_jid VARCHAR NOT NULL,
            entity_type VARCHAR NOT NULL,
            entity_value VARCHAR NOT NULL,
            PRIMARY KEY (message_id, entity_type, entity_value)
        )
    """)


def _extract_regex(text: str) -> list[tuple[str, str]]:
    entities: list[tuple[str, str]] = []
    for url in _URL_RE.findall(text):
        entities.append(("url", url.rstrip(".,;:!?)'\"")))
    for email in _EMAIL_RE.findall(text):
        entities.append(("url", email))
    for mention in _MENTION_RE.findall(text):
        mention = mention.strip()
        if len(mention) > 2:
            entities.append(("person", "@" + mention))
    return entities


def _extract_spacy(
    texts: list[str], sender_names: list[str]
) -> list[list[tuple[str, str]]]:
    nlp = _get_nlp()
    if not nlp.has_pipe("ner"):
        return [[] for _ in texts]

    results: list[list[tuple[str, str]]] = []
    sender_set = {n.lower().strip() for n in sender_names if n}

    for doc in nlp.pipe(texts, batch_size=256):
        entities: list[tuple[str, str]] = []
        seen: set[str] = set()
        for ent in doc.ents:
            etype = _SPACY_TO_ENTITY_TYPE.get(ent.label_)
            if not etype:
                continue
            value = ent.text.strip()
            if value.lower() in sender_set:
                continue
            normalized = _normalize_entity(etype, value)
            if not normalized:
                continue
            etype, value = normalized
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            entities.append((etype, value[:200]))
        results.append(entities)
    return results


async def extract_entities(
    conn: duckdb.DuckDBPyConnection, config: Config, process_all: bool = False
) -> int:
    ensure_entity_table(conn)

    where_skip = ""
    if not process_all:
        where_skip = (
            " AND m.id NOT IN (SELECT DISTINCT message_id FROM extracted_entities)"
        )

    rows = conn.execute(f"""
        SELECT m.id, m.chat_jid, m.push_name, m.text_content
        FROM messages m
        WHERE m.is_group = true
          AND m.text_content IS NOT NULL
          AND TRIM(m.text_content) != ''
          {where_skip}
        ORDER BY m.chat_jid, m.timestamp ASC
    """).fetchall()

    if not rows:
        log.info("No messages to extract entities from")
        return 0

    log.info("Extracting entities from %d messages (local NER)...", len(rows))
    t0 = time.time()

    msg_ids = [r[0] for r in rows]
    chat_jids = [r[1] for r in rows]
    senders = [r[2] or "" for r in rows]
    texts = [r[3] for r in rows]

    spacy_results = _extract_spacy(texts, senders)

    flat: list[tuple[str, str, str, str]] = []
    for i, text in enumerate(texts):
        msg_id = msg_ids[i]
        chat_jid = chat_jids[i]

        for etype, evalue in _extract_regex(text):
            if etype == "url":
                flat.append((msg_id, chat_jid, etype, evalue))
            else:
                normalized = _normalize_entity(etype, evalue)
                if normalized:
                    flat.append((msg_id, chat_jid, normalized[0], normalized[1]))

        for etype, evalue in spacy_results[i]:
            flat.append((msg_id, chat_jid, etype, evalue))

    elapsed = time.time() - t0
    log.info("Extracted %d entity mentions in %.1fs", len(flat), elapsed)

    for i in range(0, len(flat), 1000):
        conn.executemany(
            "INSERT OR IGNORE INTO extracted_entities (message_id, chat_jid, entity_type, entity_value) VALUES (?, ?, ?, ?)",
            flat[i : i + 1000],
        )

    deduped = dedup_entities(conn)
    log.info("Dedup pass merged %d entity values", deduped)
    return len(flat)


def dedup_entities(conn: duckdb.DuckDBPyConnection) -> int:
    from difflib import SequenceMatcher

    if not table_exists(conn, "extracted_entities"):
        return 0

    rows = conn.execute("""
        SELECT entity_type, entity_value, COUNT(*) as c
        FROM extracted_entities
        WHERE entity_type != 'url'
        GROUP BY entity_type, entity_value
        HAVING COUNT(*) >= 2
        ORDER BY c DESC
    """).fetchall()

    by_type: dict[str, list[tuple[str, int]]] = {}
    for etype, val, count in rows:
        by_type.setdefault(etype, []).append((val, count))

    merges: dict[str, str] = {}

    for etype, entities in by_type.items():
        entities.sort(key=lambda x: -x[1])
        canonicals: list[tuple[str, int]] = []

        for val, count in entities:
            v_lower = val.lower()
            merged = False

            for canon, canon_count in canonicals:
                c_lower = canon.lower()

                if v_lower == c_lower:
                    merges[val] = canon
                    merged = True
                    break

                if len(v_lower) > 3 and len(c_lower) > 3:
                    if v_lower in c_lower or c_lower in v_lower:
                        shorter = val if len(val) < len(canon) else canon
                        longer = canon if shorter == val else val
                        target = longer if count < canon_count else shorter
                        if target != val:
                            merges[val] = target
                        merged = True
                        break

                if len(v_lower) > 5 and len(c_lower) > 5:
                    ratio = SequenceMatcher(None, v_lower, c_lower).ratio()
                    if ratio > 0.85:
                        merges[val] = canon
                        merged = True
                        break

            if not merged:
                canonicals.append((val, count))

    if not merges:
        return 0

    updated = 0
    for old_val, new_val in merges.items():
        try:
            conn.execute(
                "UPDATE extracted_entities SET entity_value = ? WHERE entity_value = ?",
                [new_val, old_val],
            )
            updated += 1
        except Exception:
            pass

    try:
        conn.execute("""
            DELETE FROM extracted_entities
            WHERE rowid NOT IN (
                SELECT MIN(rowid)
                FROM extracted_entities
                GROUP BY message_id, entity_type, entity_value
            )
        """)
    except Exception:
        pass

    return updated


def get_entity_stats(conn: duckdb.DuckDBPyConnection) -> dict:
    if not table_exists(conn, "extracted_entities"):
        return {}
    stats = {}
    for etype in ENTITY_TYPES:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT entity_value) FROM extracted_entities WHERE entity_type = ?",
            [etype],
        ).fetchone()
        if row is None:
            mentions = 0
            unique = 0
        else:
            mentions = int(row[0])
            unique = int(row[1])
        stats[etype] = {"mentions": mentions, "unique": unique}

    total_row = conn.execute("SELECT COUNT(*) FROM extracted_entities").fetchone()
    messages_row = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM extracted_entities"
    ).fetchone()
    stats["total"] = int(total_row[0]) if total_row else 0
    stats["messages"] = int(messages_row[0]) if messages_row else 0
    return stats
