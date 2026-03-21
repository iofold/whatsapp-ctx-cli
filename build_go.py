"""Build the whatsapp-sync Go binary for the current platform or cross-compile for distribution.

Cross-compilation note: go-duckdb requires CGO. Local --all only succeeds for
targets where a C cross-compiler is available. The CI workflow builds natively
on each platform (ubuntu/macos/windows runners) to avoid this limitation.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GO_SRC = ROOT / "whatsapp-sync"
BIN_DIR = ROOT / "wactx" / "bin"

TARGETS = [
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("darwin", "amd64"),
    ("darwin", "arm64"),
    ("windows", "amd64"),
]

_CROSS_CC: dict[tuple[str, str], str] = {
    ("linux", "arm64"): "aarch64-linux-gnu-gcc",
}


def _binary_name(goos: str, goarch: str) -> str:
    name = f"whatsapp-sync-{goos}-{goarch}"
    if goos == "windows":
        name += ".exe"
    return name


def _host_target() -> tuple[str, str]:
    goos = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(
        platform.system(), "linux"
    )
    goarch = {
        "x86_64": "amd64",
        "AMD64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine(), "amd64")
    return goos, goarch


def _current_platform_binary() -> str:
    return _binary_name(*_host_target())


def _is_native(goos: str, goarch: str) -> bool:
    return (goos, goarch) == _host_target()


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

    built: list[Path] = []
    skipped: list[str] = []

    for goos, goarch in TARGETS:
        name = _binary_name(goos, goarch)
        output = BIN_DIR / name
        native = _is_native(goos, goarch)

        env = {**os.environ, "GOOS": goos, "GOARCH": goarch}

        if native:
            env["CGO_ENABLED"] = "1"
            label = "native"
        elif (goos, goarch) in _CROSS_CC:
            cc = _CROSS_CC[(goos, goarch)]
            if not shutil.which(cc):
                skipped.append(
                    f"  SKIP: {goos}/{goarch} — cross-compiler '{cc}' not found "
                    f"(install with: sudo apt-get install gcc-aarch64-linux-gnu)"
                )
                continue
            env["CGO_ENABLED"] = "1"
            env["CC"] = cc
            label = f"cross (CC={cc})"
        else:
            skipped.append(
                f"  SKIP: {goos}/{goarch} — no C cross-compiler available "
                f"(go-duckdb requires CGO; use CI for this target)"
            )
            continue

        print(f"Building for {goos}/{goarch} [{label}]...")
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

    if skipped:
        print(
            f"\n{len(skipped)} target(s) skipped (CGO cross-compilation not available):"
        )
        for msg in skipped:
            print(msg, file=sys.stderr)
        print(
            "\nNote: The CI workflow builds all platforms natively. "
            "Push a v* tag to trigger a full build.",
            file=sys.stderr,
        )

    return built


if __name__ == "__main__":
    if "--all" in sys.argv:
        build_all()
    else:
        build_current()
