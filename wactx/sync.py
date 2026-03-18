from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from datetime import datetime
from pathlib import Path

import duckdb

from wactx.config import Config
from wactx.db import get_connection, ensure_schema

log = logging.getLogger("wactx.sync")

LINE_RE = re.compile(
    r"\[?(\d{1,2}/\d{1,2}/\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*(AM|PM|am|pm)?\]?"
    r"\s*[-–]\s*"
    r"(.*?):\s(.*)",
    re.DOTALL,
)

DATE_FORMATS = ["%m/%d/%y", "%m/%d/%Y", "%d/%m/%y", "%d/%m/%Y"]


def _parse_timestamp(date_str: str, time_str: str, ampm: str | None) -> datetime | None:
    time_str = time_str.strip()
    if ampm:
        time_str += f" {ampm.upper()}"
        time_fmts = ["%I:%M %p", "%I:%M:%S %p"]
    else:
        time_fmts = ["%H:%M", "%H:%M:%S"]

    for dfmt in DATE_FORMATS:
        for tfmt in time_fmts:
            try:
                return datetime.strptime(
                    f"{date_str.strip()} {time_str}", f"{dfmt} {tfmt}"
                )
            except ValueError:
                continue
    return None


def _make_id(chat_name: str, sender: str, ts: datetime, text: str) -> str:
    raw = f"{chat_name}|{sender}|{ts.isoformat()}|{text[:100]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def parse_export(path: Path) -> tuple[str, list[dict]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    chat_name = path.stem
    messages: list[dict] = []
    current: dict | None = None

    for line in lines:
        m = LINE_RE.match(line)
        if m:
            if current:
                messages.append(current)
            date_str, time_str, ampm, sender, body = m.groups()
            ts = _parse_timestamp(date_str, time_str, ampm)
            if not ts:
                continue
            current = {
                "sender": sender.strip(),
                "timestamp": ts,
                "text": body.strip(),
            }
        elif current:
            current["text"] += "\n" + line

    if current:
        messages.append(current)

    return chat_name, messages


def import_to_db(
    conn: duckdb.DuckDBPyConnection, messages: list[dict], chat_name: str
) -> int:
    chat_jid = _slugify(chat_name) + "@import"
    senders = {m["sender"] for m in messages}
    is_group = len(senders) > 2

    inserted = 0
    for m in messages:
        sender_jid = _slugify(m["sender"]) + "@import"
        msg_id = _make_id(chat_name, m["sender"], m["timestamp"], m["text"])

        exists = conn.execute(
            "SELECT 1 FROM messages WHERE id = ?", [msg_id]
        ).fetchone()
        if exists:
            continue

        conn.execute(
            """INSERT INTO messages (id, chat_jid, sender_jid, is_from_me, is_group,
               timestamp, msg_type, text_content, push_name, sent_date, sent_hour, sent_dow)
               VALUES (?, ?, ?, false, ?, ?, 'text', ?, ?, ?, ?, ?)""",
            [
                msg_id,
                chat_jid,
                sender_jid,
                is_group,
                m["timestamp"],
                m["text"],
                m["sender"],
                m["timestamp"].date(),
                m["timestamp"].hour,
                m["timestamp"].weekday(),
            ],
        )
        inserted += 1

    for sender in senders:
        sender_jid = _slugify(sender) + "@import"
        exists = conn.execute(
            "SELECT 1 FROM contacts WHERE jid = ?", [sender_jid]
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO contacts (jid, push_name, is_group) VALUES (?, ?, false)",
                [sender_jid, sender],
            )

    chat_exists = conn.execute(
        "SELECT 1 FROM contacts WHERE jid = ?", [chat_jid]
    ).fetchone()
    if not chat_exists:
        conn.execute(
            "INSERT INTO contacts (jid, push_name, is_group, group_name) VALUES (?, ?, ?, ?)",
            [chat_jid, chat_name, is_group, chat_name if is_group else None],
        )

    return inserted


def import_file(config: Config, path: Path) -> tuple[str, int]:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            txt_files = [n for n in zf.namelist() if n.endswith(".txt")]
            if not txt_files:
                raise ValueError(f"No .txt file found in {path}")
            extracted = Path(zf.extract(txt_files[0], path.parent))
            chat_name, messages = parse_export(extracted)
            extracted.unlink()
    elif path.suffix == ".txt":
        chat_name, messages = parse_export(path)
    else:
        raise ValueError(
            f"Unsupported file type: {path.suffix} (expected .txt or .zip)"
        )

    if not messages:
        raise ValueError(f"No messages parsed from {path}")

    conn = get_connection(config)
    ensure_schema(conn)
    count = import_to_db(conn, messages, chat_name)
    conn.close()

    log.info(
        "Imported %d messages from '%s' (%d total parsed)",
        count,
        chat_name,
        len(messages),
    )
    return chat_name, count
