from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any, cast
import zipfile

import duckdb

from wactx.config import Config
from wactx.db import get_connection

log = logging.getLogger("wactx.sync")

LINE_PATTERN = re.compile(
    r"^\[?(\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM|am|pm))?)\]?"
    r"(?:\s*[-–]\s*|\s+)"
    r"(.*?):\s*(.*)$"
)

_TS_FORMATS = (
    "%m/%d/%y, %I:%M %p",
    "%m/%d/%Y, %I:%M %p",
    "%m/%d/%y, %I:%M:%S %p",
    "%m/%d/%Y, %I:%M:%S %p",
    "%d/%m/%y, %H:%M",
    "%d/%m/%Y, %H:%M",
    "%d/%m/%y, %H:%M:%S",
    "%d/%m/%Y, %H:%M:%S",
    "%m/%d/%y, %H:%M",
    "%m/%d/%Y, %H:%M",
    "%m/%d/%y, %H:%M:%S",
    "%m/%d/%Y, %H:%M:%S",
)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "unknown"


def _parse_timestamp(value: str) -> datetime:
    normalized = re.sub(r"\s+", " ", value.replace("[", "").replace("]", "")).strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unsupported WhatsApp timestamp: {value}")


def _make_message_id(timestamp: datetime, sender: str, text: str, sequence: int) -> str:
    raw = f"{timestamp.isoformat()}|{sender}|{text}|{sequence}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return (
        f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


def _parse_lines(lines: list[str]) -> list[dict]:
    messages: list[dict] = []
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if not current:
            return
        seq = len(messages)
        current["id"] = _make_message_id(
            current["timestamp"], current["sender"], current["text"], seq
        )
        messages.append(current)
        current = None

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        match = LINE_PATTERN.match(line)
        if match:
            flush()
            ts_str, sender, text = match.groups()
            current = {
                "timestamp": _parse_timestamp(ts_str),
                "sender": sender.strip(),
                "text": text,
            }
            continue

        if current is not None:
            current["text"] = f"{current['text']}\n{line}" if line else current["text"]

    flush()
    return messages


def _scalar_count(conn: duckdb.DuckDBPyConnection, sql: str, params: list[str]) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    row_tuple = cast(tuple[Any, ...], row)
    return int(row_tuple[0])


def parse_export(path: Path) -> tuple[str, list[dict]]:
    chat_name = path.stem
    messages = _parse_lines(
        path.read_text(encoding="utf-8", errors="replace").splitlines()
    )
    return chat_name, messages


def import_to_db(
    conn: duckdb.DuckDBPyConnection, messages: list[dict], chat_name: str
) -> int:
    chat_jid = f"{_slug(chat_name)}@g.us"
    unique_senders = {m["sender"] for m in messages}
    is_group = len(unique_senders) > 2
    before_count = _scalar_count(
        conn, "SELECT COUNT(*) FROM messages WHERE chat_jid = ?", [chat_jid]
    )

    rows: list[tuple] = []
    for msg in messages:
        ts: datetime = msg["timestamp"].astimezone(timezone.utc)
        sender = str(msg["sender"]).strip() or "unknown"
        sender_jid = f"{_slug(sender)}@s.whatsapp.net"
        is_from_me = sender.lower() in {"you", "me"}
        sent_dow = (ts.weekday() + 1) % 7
        rows.append(
            (
                msg["id"],
                chat_jid,
                sender_jid,
                is_from_me,
                is_group,
                ts,
                "text",
                msg.get("text") or "",
                None,
                sender,
                ts.date(),
                ts.hour,
                sent_dow,
            )
        )

    conn.executemany(
        """
        INSERT OR IGNORE INTO messages (
            id,
            chat_jid,
            sender_jid,
            is_from_me,
            is_group,
            timestamp,
            msg_type,
            text_content,
            media_type,
            push_name,
            sent_date,
            sent_hour,
            sent_dow
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    after_count = _scalar_count(
        conn, "SELECT COUNT(*) FROM messages WHERE chat_jid = ?", [chat_jid]
    )
    return after_count - before_count


def import_file(config: Config, path: Path) -> tuple[str, int]:
    path = path.expanduser().resolve()
    suffix = path.suffix.lower()
    if suffix == ".txt":
        chat_name, messages = parse_export(path)
    elif suffix == ".zip":
        with zipfile.ZipFile(path, "r") as zf:
            txt_names = [
                n
                for n in zf.namelist()
                if n.lower().endswith(".txt") and not n.endswith("/")
            ]
            if not txt_names:
                raise ValueError(f"No .txt export found inside zip: {path}")
            txt_name = txt_names[0]
            with zf.open(txt_name) as handle:
                content = handle.read().decode("utf-8", errors="replace")
        chat_name = Path(txt_name).stem
        messages = _parse_lines(content.splitlines())
    else:
        raise ValueError(f"Unsupported import file type: {path}")

    if not messages:
        return chat_name, 0

    conn = get_connection(config)
    try:
        count = import_to_db(conn, messages, chat_name)
        return chat_name, count
    finally:
        conn.close()


def _find_binary(config: Config) -> Path | None:
    if config.sync.binary_path:
        p = Path(config.sync.binary_path).expanduser()
        if p.is_file():
            return p
        return None

    candidates = [
        Path.cwd() / "whatsapp-sync",
        Path.home() / ".local" / "bin" / "whatsapp-sync",
        Path(__file__).resolve().parent.parent / "bin" / "whatsapp-sync",
    ]
    found = shutil.which("whatsapp-sync")
    if found:
        candidates.insert(0, Path(found))

    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return None


def _resolve_wa_db(config: Config) -> str:
    p = Path(config.sync.wa_db_path).expanduser()
    if p.is_absolute():
        return str(p)
    return str(config.db_path.parent / p)


def _resolve_media_dir(config: Config) -> str:
    p = Path(config.sync.media_dir).expanduser()
    if p.is_absolute():
        return str(p)
    return str(config.db_path.parent / p)


def sync_whatsapp(config: Config, incremental: bool = True, live: bool = False) -> None:
    binary = _find_binary(config)
    if not binary:
        log.error(
            "whatsapp-sync binary not found. Either:\n"
            "  1. Place it in your PATH or current directory\n"
            "  2. Set the path: wactx config sync.binary_path /path/to/whatsapp-sync\n"
            "  3. Use 'wactx import' to import a .txt export instead"
        )
        sys.exit(1)

    db_path = str(config.db_path)
    wa_db = _resolve_wa_db(config)

    cmd = [
        str(binary),
        "sync",
        "-db",
        db_path,
        "-wa-db",
        wa_db,
        "-timeout",
        config.sync.timeout,
    ]
    if incremental:
        cmd.append("-incremental")
    if live:
        cmd.append("-live")

    log.info("Running: %s", " ".join(cmd))
    log.info(
        "If this is your first sync, scan the QR code with WhatsApp on your phone."
    )

    try:
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            log.error("Sync exited with code %d", proc.returncode)
            sys.exit(proc.returncode)
    except KeyboardInterrupt:
        log.info("Sync interrupted by user")


def download_media(
    config: Config,
    chat: str | None = None,
    types: str = "image,video,audio,document",
    after: str | None = None,
    before: str | None = None,
) -> None:
    binary = _find_binary(config)
    if not binary:
        log.error(
            "whatsapp-sync binary not found. Set: wactx config sync.binary_path /path/to/whatsapp-sync"
        )
        sys.exit(1)

    db_path = str(config.db_path)
    wa_db = _resolve_wa_db(config)
    media_dir = _resolve_media_dir(config)

    cmd = [
        str(binary),
        "download",
        "-db",
        db_path,
        "-wa-db",
        wa_db,
        "-output",
        media_dir,
        "-types",
        types,
    ]
    if chat:
        cmd.extend(["-chat", chat])
    if after:
        cmd.extend(["-after", after])
    if before:
        cmd.extend(["-before", before])

    log.info("Running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            log.error("Download exited with code %d", proc.returncode)
            sys.exit(proc.returncode)
    except KeyboardInterrupt:
        log.info("Download interrupted by user")
