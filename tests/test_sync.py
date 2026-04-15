from pathlib import Path

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


def test_find_binary_prefers_bundled_binary_over_cwd(tmp_path, monkeypatch):
    bundled = _PACKAGE_BIN / "whatsapp-sync"
    bundled.parent.mkdir(parents=True, exist_ok=True)
    bundled_exists = bundled.exists()
    bundled_original = bundled.read_bytes() if bundled_exists else None

    cwd_binary = tmp_path / "whatsapp-sync"
    cwd_binary.write_text("#!/bin/sh\n")
    cwd_binary.chmod(0o755)

    try:
        bundled.write_text("#!/bin/sh\n")
        bundled.chmod(0o755)
        monkeypatch.chdir(tmp_path)

        cfg = Config()
        cfg.sync = SyncConfig(binary_path="")
        assert find_binary(cfg) == bundled
    finally:
        if bundled_exists and bundled_original is not None:
            bundled.write_bytes(bundled_original)
            bundled.chmod(0o755)
        elif bundled.exists():
            bundled.unlink()
