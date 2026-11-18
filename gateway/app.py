"""Gateway HTTP surface.

Public:
  POST /v1/chat/completions        OpenAI-compatible chat through the five stages
  POST /mcp/{server_id}            MCP facade: JSON-RPC tools/list and tools/call
  POST /a2a/{agent_id}             A2A facade: JSON-RPC SendMessage (delegation)
  GET  /v1/holds/{hold_id}         status/result of a held request (same use case only)
Internal (x-internal-token): POST /internal/holds/{hold_id}/resume
Admin (x-admin-token): POST /admin/reload, POST /admin/reset, GET /admin/state, POST /admin/flush
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from gateway.config import Settings
from gateway.core import Gateway
from gateway.snapshot import SnapshotRejected
from govlib import keys, tokens
from govlib.telemetry import setup_tracing
from telemetry.evidence_store.store import AsyncEvidenceWriter, EvidenceStore

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

settings = Settings()
gw: Gateway | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gw
    tracer = setup_tracing("governed-gateway")
    writer = AsyncEvidenceWriter(EvidenceStore(settings.evidence_dir / "gateway.jsonl"))
    writer.start()
    gw = Gateway(settings, tracer, writer)
    for attempt in range(60):
        if await gw.opa.health():
            break
        await asyncio.sleep(1)
    await gw.budget.preload()
    try:
        await gw.snapshots.load(settings.snapshot_dir)
    except SnapshotRejected as exc:
        log.error("no valid snapshot at startup: %s", exc)
    yield
    await writer.stop()
    trace.get_tracer_provider().force_flush()


app = FastAPI(title="governed-ai-gateway", lifespan=lifespan)


def _admin(token: str | None) -> None:
    if token != keys.read_secret("admin_token"):
        raise HTTPException(403, "admin token required")


def _internal(token: str | None) -> None:
    if token != keys.read_secret("internal_token"):
        raise HTTPException(403, "internal token required")


@app.get("/health")
async def health():
    return {"status": "ok" if gw and gw.snapshots.current else "degraded",
            "config_version": gw.snapshots.version if gw else None,
            "redis": await gw.budget.ping() if gw else False,
            "opa": await gw.opa.health() if gw else False}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    return await gw.handle_chat(request.headers, body)


@app.post("/mcp/{server_id}")
async def mcp(server_id: str, request: Request):
    return await gw.handle_mcp(request.headers, server_id, await request.json())


@app.post("/a2a/{agent_id}")
async def a2a(agent_id: str, request: Request):
    return await gw.handle_a2a(request.headers, agent_id, await request.json())


@app.get("/v1/holds/{hold_id}")
async def get_hold(hold_id: str, request: Request):
    auth = request.headers.get("authorization", "")
    try:
        claims = tokens.verify_workload_token(auth.removeprefix("Bearer ").strip())
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid_credential")
    uc = gw.snap["workloads"].get(claims["sub"])
    r = await gw.http.get(f"{settings.approval_url}/holds/{hold_id}",
                          headers={"x-internal-token": keys.read_secret("internal_token")})
    if r.status_code != 200:
        raise HTTPException(r.status_code, "unknown hold")
    hold = r.json()
    if hold["use_case_id"] != uc:
        raise HTTPException(403, "hold belongs to another use case")
    return {k: hold.get(k) for k in ("hold_id", "status", "exception_id", "exception_model", "approver",
                                     "created_at", "decided_at", "wait_ms", "result")}


@app.post("/internal/holds/{hold_id}/resume")
async def resume(hold_id: str, x_internal_token: str | None = Header(default=None)):
    _internal(x_internal_token)
    return await gw.resume_hold(hold_id)


@app.post("/admin/reload")
async def reload(request: Request, x_admin_token: str | None = Header(default=None)):
    _admin(x_admin_token)
    body = await request.json() if await request.body() else {}
    sub = body.get("dir")
    directory = settings.snapshot_dir
    if sub:
        candidate = (settings.snapshot_dir / sub).resolve()
        if settings.snapshot_dir.resolve() not in candidate.parents and candidate != settings.snapshot_dir.resolve():
            raise HTTPException(400, "dir must be inside the snapshot directory")
        directory = candidate
    try:
        return await gw.snapshots.load(Path(directory))
    except SnapshotRejected as exc:
        return JSONResponse({"loaded": False, "reason": str(exc), "serving_config_version": gw.snapshots.version}, 409)


@app.post("/admin/reset")
async def reset(x_admin_token: str | None = Header(default=None)):
    _admin(x_admin_token)
    gw.breakers.reset()
    gw.quarantine.clear()
    return {"reset": True}


@app.get("/admin/state")
async def state(x_admin_token: str | None = Header(default=None)):
    _admin(x_admin_token)
    return {"config_version": gw.snapshots.version, "breakers": gw.breakers.snapshot(),
            "quarantine": sorted(f"{s}/{t}" for s, t in gw.quarantine)}


@app.post("/admin/flush")
async def flush(x_admin_token: str | None = Header(default=None)):
    _admin(x_admin_token)
    await gw.evidence.flush()
    trace.get_tracer_provider().force_flush()
    return {"flushed": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")
