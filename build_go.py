"""Build the whatsapp-sync Go binary for the current platform or cross-compile for distribution."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GO_SRC = ROOT / "go"
BIN_DIR = ROOT / "wactx" / "bin"

TARGETS = [
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("darwin", "amd64"),
    ("darwin", "arm64"),
    ("windows", "amd64"),
]


def _binary_name(goos: str, goarch: str) -> str:
    name = f"whatsapp-sync-{goos}-{goarch}"
    if goos == "windows":
        name += ".exe"
    return name


def _current_platform_binary() -> str:
    goos = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(
        platform.system(), "linux"
    )
    goarch = {
        "x86_64": "amd64",
        "AMD64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine(), "amd64")
    return _binary_name(goos, goarch)


def build_current() -> Path:
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    go_bin = shutil.which("go")
    if not go_bin:
        print(
            "ERROR: Go compiler not found. Install Go from https://go.dev/dl/",
            file=sys.stderr,
        )
        sys.exit(1)

    output = BIN_DIR / "whatsapp-sync"
    if platform.system() == "Windows":
        output = output.with_suffix(".exe")

    env = {**os.environ, "CGO_ENABLED": "1"}

    print(f"Building whatsapp-sync for {platform.system()}/{platform.machine()}...")
    subprocess.run(
        [go_bin, "build", "-o", str(output), "."],
        cwd=str(GO_SRC),
        env=env,
        check=True,
    )
    output.chmod(0o755)
    print(f"Built: {output} ({output.stat().st_size / 1024 / 1024:.1f} MB)")

    platform_name = _current_platform_binary()
    platform_copy = BIN_DIR / platform_name
    if platform_copy != output:
        shutil.copy2(output, platform_copy)

    return output


def build_all() -> list[Path]:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    go_bin = shutil.which("go")
    if not go_bin:
        print("ERROR: Go compiler not found.", file=sys.stderr)
        sys.exit(1)

    built = []
    for goos, goarch in TARGETS:
        name = _binary_name(goos, goarch)
        output = BIN_DIR / name
        print(f"Cross-compiling for {goos}/{goarch}...")

        cgo = "0"
        env = {**os.environ, "GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": cgo}

        try:
            subprocess.run(
                [go_bin, "build", "-o", str(output), "."],
                cwd=str(GO_SRC),
                env=env,
                check=True,
            )
            output.chmod(0o755)
            built.append(output)
            print(f"  Built: {name} ({output.stat().st_size / 1024 / 1024:.1f} MB)")
        except subprocess.CalledProcessError as e:
            print(f"  FAILED: {goos}/{goarch}: {e}", file=sys.stderr)

    return built


if __name__ == "__main__":
    if "--all" in sys.argv:
        build_all()
    else:
        build_current()
