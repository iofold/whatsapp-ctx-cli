from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib

CONFIG_DIR = Path.home() / ".config" / "wactx"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "wactx" / "messages.duckdb"


@dataclass
class ApiConfig:
    base_url: str = "https://api.openai.com/v1"
    key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dims: int = 384
    chat_model: str = "gpt-5-mini"
    max_concurrent: int = 10


HISTORY_DAYS_MAP: dict[str, int] = {
    "1_month": 30,
    "3_months": 90,
    "1_year": 365,
    "3_years": 1095,
    "all": 3650,
}


@dataclass
class SyncConfig:
    binary_path: str = ""
    wa_db_path: str = "whatsmeow.db"
    media_dir: str = "media"
    timeout: str = "5m"
    history_sync: str = "3_years"


def history_sync_days(sync_cfg: SyncConfig) -> int:
    return HISTORY_DAYS_MAP.get(sync_cfg.history_sync, 1095)


@dataclass
class SearchConfig:
    default_depth: str = "balanced"
    owner_name: str = ""


@dataclass
class Config:
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    api: ApiConfig = field(default_factory=ApiConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    search: SearchConfig = field(default_factory=SearchConfig)


def _as_str(value: Any, *, field_name: str) -> str:
    if isinstance(value, str):
        return value
    raise ValueError(f"Expected string for '{field_name}', got {type(value).__name__}")


def _as_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(
                f"Expected integer for '{field_name}', got '{value}'"
            ) from exc
    raise ValueError(f"Expected integer for '{field_name}', got {type(value).__name__}")


def load_config(path: Path | str | None = None) -> Config:
    cfg = Config()
    config_path = Path(path).expanduser() if path is not None else CONFIG_PATH
    if not config_path.exists():
        return cfg

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ValueError(f"Failed to read config at {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Invalid TOML root in {config_path}: expected table")

    db_val = data.get("db_path")
    if db_val is None and isinstance(data.get("database"), dict):
        db_val = data["database"].get("path")
    if db_val is not None:
        cfg.db_path = Path(_as_str(db_val, field_name="db_path")).expanduser()

    api_data = data.get("api", {})
    if api_data:
        if not isinstance(api_data, dict):
            raise ValueError("Invalid [api] table in config")
        cfg.api = ApiConfig(
            base_url=_as_str(
                api_data.get("base_url", cfg.api.base_url), field_name="api.base_url"
            ),
            key=_as_str(api_data.get("key", cfg.api.key), field_name="api.key"),
            embedding_model=_as_str(
                api_data.get("embedding_model", cfg.api.embedding_model),
                field_name="api.embedding_model",
            ),
            embedding_dims=_as_int(
                api_data.get("embedding_dims", cfg.api.embedding_dims),
                field_name="api.embedding_dims",
            ),
            chat_model=_as_str(
                api_data.get("chat_model", cfg.api.chat_model),
                field_name="api.chat_model",
            ),
            max_concurrent=_as_int(
                api_data.get("max_concurrent", cfg.api.max_concurrent),
                field_name="api.max_concurrent",
            ),
        )

    sync_data = data.get("sync", {})
    if sync_data:
        if not isinstance(sync_data, dict):
            raise ValueError("Invalid [sync] table in config")
        cfg.sync = SyncConfig(
            binary_path=_as_str(
                sync_data.get("binary_path", cfg.sync.binary_path),
                field_name="sync.binary_path",
            ),
            wa_db_path=_as_str(
                sync_data.get("wa_db_path", cfg.sync.wa_db_path),
                field_name="sync.wa_db_path",
            ),
            media_dir=_as_str(
                sync_data.get("media_dir", cfg.sync.media_dir),
                field_name="sync.media_dir",
            ),
            timeout=_as_str(
                sync_data.get("timeout", cfg.sync.timeout), field_name="sync.timeout"
            ),
            history_sync=_as_str(
                sync_data.get("history_sync", cfg.sync.history_sync),
                field_name="sync.history_sync",
            ),
        )

    search_data = data.get("search", {})
    if search_data:
        if not isinstance(search_data, dict):
            raise ValueError("Invalid [search] table in config")
        cfg.search = SearchConfig(
            default_depth=_as_str(
                search_data.get("default_depth", cfg.search.default_depth),
                field_name="search.default_depth",
            ),
            owner_name=_as_str(
                search_data.get("owner_name", cfg.search.owner_name),
                field_name="search.owner_name",
            ),
        )

    return cfg


def _dump_toml_manual(config: Config) -> str:
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    return "\n".join(
        [
            f'db_path = "{esc(str(config.db_path))}"',
            "",
            "[api]",
            f'base_url = "{esc(config.api.base_url)}"',
            f'key = "{esc(config.api.key)}"',
            f'embedding_model = "{esc(config.api.embedding_model)}"',
            f"embedding_dims = {config.api.embedding_dims}",
            f'chat_model = "{esc(config.api.chat_model)}"',
            f"max_concurrent = {config.api.max_concurrent}",
            "",
            "[sync]",
            f'binary_path = "{esc(config.sync.binary_path)}"',
            f'wa_db_path = "{esc(config.sync.wa_db_path)}"',
            f'media_dir = "{esc(config.sync.media_dir)}"',
            f'timeout = "{esc(config.sync.timeout)}"',
            f'history_sync = "{esc(config.sync.history_sync)}"',
            "",
            "[search]",
            f'default_depth = "{esc(config.search.default_depth)}"',
            f'owner_name = "{esc(config.search.owner_name)}"',
            "",
        ]
    )


def save_config(config: Config, path: Path | str | None = None) -> None:
    config_path = Path(path).expanduser() if path is not None else CONFIG_PATH
    if path is None:
        ensure_dirs(config)
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "db_path": str(config.db_path),
        "api": {
            "base_url": config.api.base_url,
            "key": config.api.key,
            "embedding_model": config.api.embedding_model,
            "embedding_dims": config.api.embedding_dims,
            "chat_model": config.api.chat_model,
            "max_concurrent": config.api.max_concurrent,
        },
        "sync": {
            "binary_path": config.sync.binary_path,
            "wa_db_path": config.sync.wa_db_path,
            "media_dir": config.sync.media_dir,
            "timeout": config.sync.timeout,
            "history_sync": config.sync.history_sync,
        },
        "search": {
            "default_depth": config.search.default_depth,
            "owner_name": config.search.owner_name,
        },
    }

    try:
        import tomli_w  # type: ignore

        text = tomli_w.dumps(data)
    except ImportError:
        text = _dump_toml_manual(config)

    config_path.write_text(text, encoding="utf-8")


def set_config_value(key: str, value: str, path: Path | str | None = None) -> None:
    config = load_config(path)

    if key == "db_path":
        config.db_path = Path(value).expanduser()
    elif "." in key:
        section, field_name = key.split(".", 1)
        if section == "api":
            if not hasattr(config.api, field_name):
                raise ValueError(f"Unknown config key: {key}")
            cast_value: Any = value
            if field_name in {"embedding_dims", "max_concurrent"}:
                cast_value = _as_int(value, field_name=key)
            setattr(config.api, field_name, cast_value)
        elif section == "sync":
            if not hasattr(config.sync, field_name):
                raise ValueError(f"Unknown config key: {key}")
            setattr(config.sync, field_name, value)
        elif section == "search":
            if not hasattr(config.search, field_name):
                raise ValueError(f"Unknown config key: {key}")
            setattr(config.search, field_name, value)
        else:
            raise ValueError(f"Unknown config key: {key}")
    else:
        raise ValueError(f"Unknown config key: {key}")

    save_config(config, path)


def ensure_dirs(config: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
