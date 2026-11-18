"""Clear generated run state (telemetry output, evidence chains, results). Folders are kept and
emptied (deleting synced folders such as OneDrive ones can be refused by the OS)."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

for d in ("telemetry/out", "evidence", "results"):
    p = ROOT / d
    p.mkdir(parents=True, exist_ok=True)
    for child in p.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
print("cleared telemetry/out, evidence/ and results/")
