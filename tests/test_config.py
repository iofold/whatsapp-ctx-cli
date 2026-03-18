import tempfile
from pathlib import Path

from wactx.config import Config, load_config, save_config, set_config_value


def test_default_config():
    cfg = Config()
    assert cfg.api.base_url == "https://api.openai.com/v1"
    assert cfg.api.embedding_dims == 384
    assert cfg.search.default_depth == "balanced"


def test_save_and_load(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config()
    cfg.api.key = "test-key-123"
    cfg.api.embedding_dims = 768
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.api.key == "test-key-123"
    assert loaded.api.embedding_dims == 768


def test_load_missing_file(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.toml")
    assert cfg.api.key == ""


def test_set_config_value(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config()
    save_config(cfg, path)

    set_config_value("api.key", "new-key", path)
    loaded = load_config(path)
    assert loaded.api.key == "new-key"

    set_config_value("api.embedding_dims", "768", path)
    loaded = load_config(path)
    assert loaded.api.embedding_dims == 768
