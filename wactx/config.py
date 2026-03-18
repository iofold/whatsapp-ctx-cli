from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "wactx"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "wactx" / "messages.duckdb"


@dataclass
class ApiConfig:
    base_url: str = "https://api.openai.com/v1"
    key: str = ""
    embedding_model: str = "text-embedding-3-large"
    embedding_dims: int = 384
    chat_model: str = "gpt-4.1-mini"
    max_concurrent: int = 5
    batch_size: int = 100


@dataclass
class SearchConfig:
    default_depth: str = "balanced"
    owner_name: str = ""


@dataclass
class Config:
    db_path: str = str(DEFAULT_DB_PATH)
    api: ApiConfig = field(default_factory=ApiConfig)
    search: SearchConfig = field(default_factory=SearchConfig)

    @property
    def db_path_resolved(self) -> Path:
        return Path(self.db_path).expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        return Config()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    cfg = Config()
    if "database" in data:
        cfg.db_path = data["database"].get("path", cfg.db_path)
    if "api" in data:
        for k, v in data["api"].items():
            if hasattr(cfg.api, k):
                setattr(cfg.api, k, type(getattr(cfg.api, k))(v))
    if "search" in data:
        for k, v in data["search"].items():
            if hasattr(cfg.search, k):
                setattr(cfg.search, k, v)
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[database]",
        f'path = "{cfg.db_path}"',
        "",
        "[api]",
        f'base_url = "{cfg.api.base_url}"',
        f'key = "{cfg.api.key}"',
        f'embedding_model = "{cfg.api.embedding_model}"',
        f"embedding_dims = {cfg.api.embedding_dims}",
        f'chat_model = "{cfg.api.chat_model}"',
        f"max_concurrent = {cfg.api.max_concurrent}",
        f"batch_size = {cfg.api.batch_size}",
        "",
        "[search]",
        f'default_depth = "{cfg.search.default_depth}"',
        f'owner_name = "{cfg.search.owner_name}"',
    ]
    path.write_text("\n".join(lines) + "\n")


def set_config_value(key: str, value: str, path: Path | None = None) -> None:
    cfg = load_config(path)
    parts = key.split(".")
    if len(parts) == 2:
        section, attr = parts
        obj = {"api": cfg.api, "search": cfg.search, "database": None}.get(section)
        if section == "database" and attr == "path":
            cfg.db_path = value
        elif obj and hasattr(obj, attr):
            target_type = type(getattr(obj, attr))
            setattr(obj, attr, target_type(value))
        else:
            raise ValueError(f"Unknown config key: {key}")
    elif len(parts) == 1 and parts[0] == "db_path":
        cfg.db_path = value
    else:
        raise ValueError(f"Unknown config key: {key}")
    save_config(cfg, path)


def ensure_dirs(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg.db_path_resolved.parent.mkdir(parents=True, exist_ok=True)
