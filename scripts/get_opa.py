"""Download the pinned OPA CLI into .tools/ (used by `make compile` for `opa test`).

The binary comes from the official open-policy-agent/opa GitHub release and is checked
against the release's published SHA-256 file.
"""
from __future__ import annotations

import hashlib
import platform
import stat
import sys
from pathlib import Path

import httpx

OPA_VERSION = "v1.22.4"
ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tools"


def asset_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if system == "windows":
        return f"opa_windows_{arch}.exe"
    if system == "darwin":
        return f"opa_darwin_{arch}"
    return f"opa_linux_{arch}_static"


def opa_path() -> Path:
    return TOOLS / ("opa.exe" if platform.system() == "Windows" else "opa")


def main() -> int:
    target = opa_path()
    if target.exists():
        print(f"OPA already present: {target}")
        return 0
    TOOLS.mkdir(exist_ok=True)
    base = f"https://github.com/open-policy-agent/opa/releases/download/{OPA_VERSION}/{asset_name()}"
    with httpx.Client(follow_redirects=True, timeout=120) as c:
        data = c.get(base).raise_for_status().content
        expected = c.get(base + ".sha256").raise_for_status().text.split()[0]
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        print(f"SHA-256 mismatch for {base}: {actual} != {expected}")
        return 1
    target.write_bytes(data)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    print(f"OPA {OPA_VERSION} -> {target} (sha256 {actual})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
