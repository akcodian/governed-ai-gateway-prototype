"""Approver CLI and a programmatic simulated approver.

  python -m approval_service.cli list [--status pending]
  python -m approval_service.cli approve HOLD_ID --model model-c --approver alice --reason "..."
  python -m approval_service.cli reject HOLD_ID --approver alice --reason "..."
"""
from __future__ import annotations

import argparse
import json
import os
import time

import httpx

from govlib import keys

APPROVAL_URL = os.environ.get("APPROVAL_URL", "http://localhost:8090")


def _h() -> dict:
    return {"x-approver-token": keys.read_secret("admin_token")}


def list_holds(status: str | None = None) -> list[dict]:
    return httpx.get(f"{APPROVAL_URL}/holds", params={"status": status} if status else None, headers=_h()).json()


def approve(hold_id: str, model: str, approver: str, reason: str = "") -> dict:
    r = httpx.post(f"{APPROVAL_URL}/holds/{hold_id}/approve", headers=_h(), timeout=60,
                   json={"exception_model": model, "approver": approver, "reason": reason})
    r.raise_for_status()
    return r.json()


def reject(hold_id: str, approver: str, reason: str = "") -> dict:
    r = httpx.post(f"{APPROVAL_URL}/holds/{hold_id}/reject", headers=_h(), json={"approver": approver, "reason": reason})
    r.raise_for_status()
    return r.json()


class SimulatedApprover:
    """Approves a specific hold after a fixed think time (used by tests)."""

    def __init__(self, approver: str = "sim-approver-01", think_time_s: float = 0.5):
        self.approver = approver
        self.think_time_s = think_time_s

    def approve(self, hold_id: str, model: str, reason: str = "simulated approval") -> dict:
        time.sleep(self.think_time_s)
        return approve(hold_id, model, self.approver, reason)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    ap = sub.add_parser("approve")
    ap.add_argument("hold_id")
    ap.add_argument("--model", required=True)
    ap.add_argument("--approver", required=True)
    ap.add_argument("--reason", default="")
    rj = sub.add_parser("reject")
    rj.add_argument("hold_id")
    rj.add_argument("--approver", required=True)
    rj.add_argument("--reason", default="")
    a = p.parse_args()
    if a.cmd == "list":
        out = list_holds(a.status)
    elif a.cmd == "approve":
        out = approve(a.hold_id, a.model, a.approver, a.reason)
    else:
        out = reject(a.hold_id, a.approver, a.reason)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
