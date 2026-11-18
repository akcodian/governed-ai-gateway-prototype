"""Minimal A2A agent (protocol 1.0, JSON-RPC binding) used as `agent-b` and `agent-c`.

The A2A Python SDK 1.x is protobuf/gRPC-first; per the build spec we use a minimal JSON-RPC
server that follows the specification:
  * Agent Card at  GET /.well-known/agent-card.json   (a2a.utils.constants.AGENT_CARD_WELL_KNOWN_PATH)
  * JSON-RPC 2.0 at POST /a2a with method "SendMessage" (proto-JSON field names, camelCase)
The card is validated against the SDK's protobuf AgentCard type by cp_compile/register.py.

When delegated a task the agent
  1. verifies the gateway-minted downstream token (audience `a2a:<agent id>`),
  2. calls the model *through the gateway* (its spend counts against the root budget), and
  3. optionally delegates further through the gateway (for the hop-limit test).
Task text (first text part) is JSON: {"prompt": str, "max_tokens": int, "delegate_chain": [agent ids]}.
"""
from __future__ import annotations

import json
import os
import uuid

import httpx
import jwt
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from govlib import tokens

AGENT_ID = os.environ.get("AGENT_ID", "agent-b")
PORT = int(os.environ.get("PORT", "8301"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://{AGENT_ID}:{PORT}")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8080")
PROTOCOL_VERSION = "1.0"

app = FastAPI(title=f"a2a-{AGENT_ID}")
_client: httpx.AsyncClient | None = None


def agent_card() -> dict:
    return {
        "name": AGENT_ID,
        "description": f"Synthetic research sub-agent {AGENT_ID}: summarises synthetic case material.",
        "supportedInterfaces": [
            {"url": f"{PUBLIC_URL}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}
        ],
        "provider": {"organization": "synthetic-org", "url": "https://example.invalid"},
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": {
            "gatewayToken": {"httpAuthSecurityScheme": {"scheme": "Bearer", "bearerFormat": "JWT",
                                                        "description": "Downstream token minted by the gateway"}}
        },
        "securityRequirements": [{"schemes": {"gatewayToken": {"list": []}}}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "summarise", "name": "Summarise case material", "description": "Summarise synthetic text",
                    "tags": ["summarise"]}],
    }


@app.get("/.well-known/agent-card.json")
async def card():
    return agent_card()


@app.get("/health")
async def health():
    return {"status": "ok", "agent": AGENT_ID}


def _rpc_error(rpc_id, code: int, message: str, status: int = 200):
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}, status)


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


@app.post("/a2a")
async def a2a(request: Request):
    body = await request.json()
    rpc_id = body.get("id")
    if body.get("method") != "SendMessage":
        return _rpc_error(rpc_id, -32601, "Method not found")
    auth = request.headers.get("authorization", "")
    delegation = auth.removeprefix("Bearer ").strip()
    try:
        claims = tokens.verify_downstream_token(delegation, audience=f"a2a:{AGENT_ID}")
    except jwt.PyJWTError as exc:
        return _rpc_error(rpc_id, -32001, f"unauthenticated: {exc}", 401)

    message = (body.get("params") or {}).get("message") or {}
    text = next((p.get("text") for p in message.get("parts", []) if "text" in p), "{}")
    try:
        task = json.loads(text)
    except json.JSONDecodeError:
        task = {"prompt": text}

    fwd = {"x-gov-delegation": delegation,
           "authorization": f"Bearer {tokens.issue_workload_token(AGENT_ID)}"}
    if tp := request.headers.get("traceparent"):
        fwd["traceparent"] = tp
    client = _client_get()

    result: dict = {"agent": AGENT_ID, "hop": claims.get("hop"), "root_request_id": claims.get("rrid")}
    r = await client.post(f"{GATEWAY_URL}/v1/chat/completions", headers=fwd, json={
        "messages": [{"role": "user", "content": task.get("prompt", "summarise the synthetic case")}],
        "max_tokens": int(task.get("max_tokens", 64)),
    })
    rj = r.json()
    result["model_call"] = {"status": r.status_code, "decision": rj.get("decision"), "reason": rj.get("reason"),
                            "usage": rj.get("usage"), "model": rj.get("model")}

    chain = list(task.get("delegate_chain") or [])
    if chain:
        nxt = chain.pop(0)
        sub = {"prompt": task.get("prompt", ""), "max_tokens": int(task.get("max_tokens", 64)), "delegate_chain": chain}
        d = await client.post(f"{GATEWAY_URL}/a2a/{nxt}", headers=fwd, json={
            "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "SendMessage",
            "params": {"message": {"messageId": uuid.uuid4().hex, "role": "ROLE_USER",
                                   "parts": [{"text": json.dumps(sub)}]}},
        })
        result["delegation"] = {"to": nxt, "status": d.status_code, "body": d.json()}

    ok = r.status_code == 200 and all(
        x.get("status", 200) == 200 for x in [result.get("delegation") or {}])
    state = "TASK_STATE_COMPLETED" if ok else "TASK_STATE_FAILED"
    task_id = uuid.uuid4().hex
    return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": {
        "id": task_id,
        "contextId": message.get("contextId") or uuid.uuid4().hex,
        "status": {"state": state, "message": {"messageId": uuid.uuid4().hex, "role": "ROLE_AGENT",
                                               "parts": [{"text": f"{AGENT_ID} finished with state {state}"}]}},
        "artifacts": [{"artifactId": uuid.uuid4().hex, "name": "result", "parts": [{"text": json.dumps(result)}]}],
    }}}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
