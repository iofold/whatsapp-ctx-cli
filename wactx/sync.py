from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from wactx.config import Config

log = logging.getLogger("wactx.sync")

_PACKAGE_BIN = Path(__file__).resolve().parent / "bin"


def _platform_binary_name() -> str:
    goos = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(
        platform.system(), "linux"
    )
    goarch = {
        "x86_64": "amd64",
        "AMD64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine(), "amd64")
    name = f"whatsapp-sync-{goos}-{goarch}"
    if goos == "windows":
        name += ".exe"
    return name


def find_binary(config: Config) -> Path | None:
    if config.sync.binary_path:
        p = Path(config.sync.binary_path).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p

    candidates = [
        _PACKAGE_BIN / "whatsapp-sync",
        _PACKAGE_BIN / _platform_binary_name(),
    ]

    found = shutil.which("whatsapp-sync")
    if found:
        candidates.insert(0, Path(found))

    candidates.extend(
        [
            Path.cwd() / "whatsapp-sync",
            Path.home() / ".local" / "bin" / "whatsapp-sync",
        ]
    )

    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return None


def _require_binary(config: Config) -> Path:
    binary = find_binary(config)
    if binary:
        return binary

    log.error(
        "whatsapp-sync binary not found.\n\n"
        "Build it:   python build_go.py\n"
        "Or set:     wactx config sync.binary_path /path/to/whatsapp-sync\n"
    )
    sys.exit(1)


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


def _drop_hnsw_index(config: Config) -> bool:
    import duckdb

    db_path = config.db_path
    if not db_path.exists():
        return False
    try:
        conn = duckdb.connect(str(db_path))
        conn.execute("INSTALL vss; LOAD vss")
        conn.execute("DROP INDEX IF EXISTS idx_msg_embedding")
        conn.close()
        log.debug("Dropped HNSW index before sync")
        return True
    except Exception:
        return False


def _recreate_hnsw_index(config: Config) -> None:
    import duckdb

    try:
        conn = duckdb.connect(str(config.db_path))
        conn.execute("INSTALL vss; LOAD vss")
        has_embeddings = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        if has_embeddings > 0:
            conn.execute("SET hnsw_enable_experimental_persistence = true")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_embedding "
                "ON messages USING HNSW (embedding) WITH (metric = 'cosine')"
            )
            log.debug("Recreated HNSW index after sync")
        conn.close()
    except Exception as e:
        log.debug(
            "Could not recreate HNSW index (will be created on next 'wactx index'): %s",
            e,
        )


def sync_whatsapp(config: Config, incremental: bool = True, live: bool = False) -> None:
    binary = _require_binary(config)

    had_index = _drop_hnsw_index(config)

    cmd = [
        str(binary),
        "sync",
        "-db",
        str(config.db_path),
        "-wa-db",
        _resolve_wa_db(config),
        "-timeout",
        config.sync.timeout,
    ]
    if incremental:
        cmd.append("-incremental")
    if live:
        cmd.append("-live")

    log.info("Running: %s", " ".join(cmd))
    if not Path(_resolve_wa_db(config)).exists():
        log.info("First run — scan the QR code with WhatsApp on your phone.")

    try:
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            log.error("Sync exited with code %d", proc.returncode)
    except KeyboardInterrupt:
        log.info("Sync interrupted")
    finally:
        if had_index:
            _recreate_hnsw_index(config)


def download_media(
    config: Config,
    chat: str | None = None,
    types: str = "image,video,audio,document",
    after: str | None = None,
    before: str | None = None,
) -> None:
    binary = _require_binary(config)
    media_dir = _resolve_media_dir(config)

    cmd = [
        str(binary),
        "download",
        "-db",
        str(config.db_path),
        "-wa-db",
        _resolve_wa_db(config),
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
        log.info("Download interrupted")
