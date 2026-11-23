"""Write codeocean/pip-packages.txt from requirements.lock: every exact pin, without Windows-only
packages, ready to paste into Code Ocean's pip package list (one `name==version` per line)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
out = []
for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    pin, _, marker = line.partition(";")
    if 'sys_platform == "win32"' in marker:
        continue
    out.append(pin.strip())
(ROOT / "codeocean" / "pip-packages.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"wrote {len(out)} pins to codeocean/pip-packages.txt")
