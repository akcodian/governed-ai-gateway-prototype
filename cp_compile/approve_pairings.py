"""Approving function: sign every pairing entry (Ed25519 over canonical JSON).

Run after reviewing pairings.yaml (`make approve-pairings`). Signatures go to the git-ignored
control-plane/build/pairing_approvals.json; the compiler attaches them to the snapshot and the
gateway verifies them at route time with the approving function's public key.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from govlib import keys
from govlib.canonical import pairing_signing_payload

CP = Path(__file__).resolve().parents[1] / "control-plane"
OUT = CP / "build" / "pairing_approvals.json"


def entry_key(use_case_id: str, entry: dict) -> str:
    return f"{use_case_id}/{entry['model']}/{entry['rank']}"


def main() -> None:
    doc = yaml.safe_load((CP / "pairings.yaml").read_text())
    approvals = {}
    for uc, entries in doc["pairings"].items():
        for e in entries:
            approvals[entry_key(uc, e)] = keys.sign_b64("approving_function", pairing_signing_payload(uc, e))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(approvals, indent=2, sort_keys=True))
    print(f"signed {len(approvals)} pairing entries -> {OUT}")


if __name__ == "__main__":
    main()
