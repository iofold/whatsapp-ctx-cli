from pathlib import Path
from unittest.mock import patch, MagicMock

from wactx.config import Config, SyncConfig
from wactx.sync import find_binary, _platform_binary_name, _PACKAGE_BIN


def test_platform_binary_name():
    name = _platform_binary_name()
    assert name.startswith("whatsapp-sync-")
    assert "linux" in name or "darwin" in name or "windows" in name


def test_find_binary_from_config(tmp_path):
    binary = tmp_path / "whatsapp-sync"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    cfg = Config()
    cfg.sync = SyncConfig(binary_path=str(binary))
    assert find_binary(cfg) == binary


def test_find_binary_falls_through_bad_config():
    cfg = Config()
    cfg.sync = SyncConfig(binary_path="/nonexistent/path/whatsapp-sync")
    found = find_binary(cfg)
    assert found is None or found != Path("/nonexistent/path/whatsapp-sync")


def test_find_binary_package_dir(tmp_path):
    binary = _PACKAGE_BIN / "whatsapp-sync"
    if binary.exists():
        cfg = Config()
        cfg.sync = SyncConfig(binary_path="")
        found = find_binary(cfg)
        assert found is not None
