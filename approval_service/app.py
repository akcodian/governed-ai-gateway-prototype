"""Human-in-the-loop approval service (hold, approve, reject, timeout). FastAPI + SQLite.

Gateway-facing (x-internal-token):
  POST /holds                      create a hold (stored request is already guard-redacted)
  GET  /holds/{id}                 read a hold
Approver-facing (x-approver-token):
  GET  /holds?status=pending       list holds
  POST /holds/{id}/approve         {exception_model, approver, reason} -> one-time exception; gateway resumes
  POST /holds/{id}/reject          {approver, reason}
Holds still pending after HOLD_TIMEOUT_SECONDS (default 600) become `timed_out`.
Approval wait time (created -> decided) is recorded separately from gateway latency.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from govlib import keys
from telemetry.evidence_store.store import EvidenceStore

DB_PATH = Path(os.environ.get("APPROVAL_DB", "approvals.db"))
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
TIMEOUT_S = float(os.environ.get("HOLD_TIMEOUT_SECONDS", "600"))
EVIDENCE = EvidenceStore(Path(os.environ.get("GOV_EVIDENCE_DIR", "evidence")) / "approval.jsonl")

COLUMNS = ("hold_id", "status", "use_case_id", "client_id", "end_user", "root_request_id", "data_classes",
           "interaction", "request", "reasons", "config_version", "trace_context", "trace_id", "created_at",
           "decided_at", "wait_ms", "approver", "decision_reason", "exception_id", "exception_model", "result",
           "completed_at")
JSON_COLS = {"data_classes", "request", "reasons", "trace_context", "result"}


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute(f"CREATE TABLE IF NOT EXISTS holds ({', '.join(c + ' TEXT' for c in COLUMNS)}, PRIMARY KEY(hold_id))")


def row_to_hold(row: sqlite3.Row) -> dict:
    h = dict(row)
    for c in JSON_COLS:
        if h.get(c):
            h[c] = json.loads(h[c])
    if h.get("wait_ms") is not None:
        h["wait_ms"] = float(h["wait_ms"])
    return h


def get(hold_id: str) -> dict:
    with db() as con:
        row = con.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown hold")
    return row_to_hold(row)


def update(hold_id: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = [json.dumps(v) if k in JSON_COLS and v is not None else v for k, v in fields.items()]
    with db() as con:
        con.execute(f"UPDATE holds SET {sets} WHERE hold_id=?", (*vals, hold_id))


async def timeout_loop() -> None:
    while True:
        await asyncio.sleep(2)
        cutoff = (now() - dt.timedelta(seconds=TIMEOUT_S)).isoformat()
        with db() as con:
            rows = con.execute("SELECT hold_id, created_at FROM holds WHERE status='pending' AND created_at < ?",
                               (cutoff,)).fetchall()
        for r in rows:
            wait = (now() - dt.datetime.fromisoformat(r["created_at"])).total_seconds() * 1000
            update(r["hold_id"], status="timed_out", decided_at=now().isoformat(), wait_ms=wait)
            EVIDENCE.append("hold_timed_out", {"hold_id": r["hold_id"], "wait_ms": round(wait, 1)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(timeout_loop())
    yield
    task.cancel()


app = FastAPI(title="approval-service", lifespan=lifespan)


def _check(token: str | None, name: str) -> None:
    if token != keys.read_secret(name):
        raise HTTPException(403, f"{name} required")


class HoldIn(BaseModel):
    use_case_id: str
    client_id: str
    end_user: str = "anonymous"
    root_request_id: str
    data_classes: list[str]
    interaction: str
    request: dict
    reasons: dict
    config_version: int | None = None
    trace_context: dict = {}
    trace_id: str = ""


class Approve(BaseModel):
    exception_model: str
    approver: str
    reason: str = ""


class Reject(BaseModel):
    approver: str
    reason: str = ""


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/holds", status_code=201)
async def create_hold(h: HoldIn, x_internal_token: str | None = Header(default=None)):
    _check(x_internal_token, "internal_token")
    hold_id = "hold-" + uuid.uuid4().hex[:16]
    rec = {**h.model_dump(), "hold_id": hold_id, "status": "pending", "created_at": now().isoformat()}
    cols = [c for c in COLUMNS if c in rec]
    vals = [json.dumps(rec[c]) if c in JSON_COLS else rec[c] for c in cols]
    with db() as con:
        con.execute(f"INSERT INTO holds ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    EVIDENCE.append("hold_created", {"hold_id": hold_id, "use_case_id": h.use_case_id,
                                     "root_request_id": h.root_request_id, "reasons": h.reasons,
                                     "config_version": h.config_version, "trace_id": h.trace_id})
    return {"hold_id": hold_id, "status": "pending"}


@app.get("/holds")
async def list_holds(status: str | None = None, x_approver_token: str | None = Header(default=None)):
    _check(x_approver_token, "admin_token")
    q, args = "SELECT * FROM holds", ()
    if status:
        q, args = q + " WHERE status=?", (status,)
    with db() as con:
        rows = con.execute(q + " ORDER BY created_at", args).fetchall()
    return [{k: v for k, v in row_to_hold(r).items() if k not in ("request", "trace_context")} for r in rows]


@app.get("/holds/{hold_id}")
async def read_hold(hold_id: str, x_internal_token: str | None = Header(default=None)):
    _check(x_internal_token, "internal_token")
    return get(hold_id)


@app.post("/holds/{hold_id}/approve")
async def approve(hold_id: str, a: Approve, x_approver_token: str | None = Header(default=None)):
    _check(x_approver_token, "admin_token")
    h = get(hold_id)
    if h["status"] != "pending":
        raise HTTPException(409, f"hold is {h['status']}")
    decided = now()
    wait_ms = (decided - dt.datetime.fromisoformat(h["created_at"])).total_seconds() * 1000
    exception_id = "exc-" + uuid.uuid4().hex[:16]
    update(hold_id, status="approved", decided_at=decided.isoformat(), wait_ms=wait_ms, approver=a.approver,
           decision_reason=a.reason, exception_id=exception_id, exception_model=a.exception_model)
    EVIDENCE.append("exception_granted", {"hold_id": hold_id, "exception_id": exception_id,
                                          "exception_model": a.exception_model, "approver": a.approver,
                                          "reason": a.reason, "approval_wait_ms": round(wait_ms, 1),
                                          "use_case_id": h["use_case_id"], "root_request_id": h["root_request_id"]})
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{GATEWAY_URL}/internal/holds/{hold_id}/resume",
                         headers={"x-internal-token": keys.read_secret("internal_token")})
    result = {"status_code": r.status_code, "body": r.json(), "trace_id": r.headers.get("x-trace-id")}
    status = "completed" if r.status_code == 200 else "failed"
    update(hold_id, status=status, result=result, completed_at=now().isoformat())
    EVIDENCE.append("hold_resumed", {"hold_id": hold_id, "exception_id": exception_id, "status": status,
                                     "gateway_status": r.status_code, "trace_id": result["trace_id"]})
    return get(hold_id)


@app.post("/holds/{hold_id}/reject")
async def reject(hold_id: str, rj: Reject, x_approver_token: str | None = Header(default=None)):
    _check(x_approver_token, "admin_token")
    h = get(hold_id)
    if h["status"] != "pending":
        raise HTTPException(409, f"hold is {h['status']}")
    decided = now()
    wait_ms = (decided - dt.datetime.fromisoformat(h["created_at"])).total_seconds() * 1000
    update(hold_id, status="rejected", decided_at=decided.isoformat(), wait_ms=wait_ms, approver=rj.approver,
           decision_reason=rj.reason)
    EVIDENCE.append("hold_rejected", {"hold_id": hold_id, "approver": rj.approver, "reason": rj.reason,
                                      "approval_wait_ms": round(wait_ms, 1)})
    return get(hold_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")), log_level="warning")
