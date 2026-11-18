"""`make verify-evidence`: check every hash chain in the evidence directory."""
from __future__ import annotations

import sys
from pathlib import Path

from telemetry.evidence_store.store import verify_chain


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path("evidence")
    files = [target] if target.is_file() else sorted(target.glob("*.jsonl"))
    if not files:
        print(f"no evidence files in {target}")
        return 1
    bad = 0
    for f in files:
        ok, msg = verify_chain(f)
        print(f"{'OK  ' if ok else 'FAIL'} {f}: {msg}")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
