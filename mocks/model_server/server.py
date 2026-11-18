"""Mock OpenAI-compatible model server serving synthetic models model-a .. model-d.

POST /v1/chat/completions   OpenAI-style chat completion (model chosen by the `model` field)
POST /admin/models/{id}     {"fail": bool, "delay_ms": int, "context_length": int}
POST /admin/reset           reset failure flags, delays and call counters
GET  /admin/calls           per-model call counters (including refused calls)

Token rule (govlib.tokencount): input tokens = whitespace-separated words across messages;
the reply contains exactly `max_tokens` whitespace-separated words, so output tokens = max_tokens.
If a user message contains the marker "[[leak]]", the reply includes a synthetic account number
(used to test response-side redaction).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import Counter

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from govlib.tokencount import count_messages, message_text

DEFAULT_CONFIG = {
    "model-a": {"context_length": 128000, "fail": False, "delay_ms": 0},
    "model-b": {"context_length": 2048, "fail": False, "delay_ms": 0},
    "model-c": {"context_length": 128000, "fail": False, "delay_ms": 0},
    "model-d": {"context_length": 32000, "fail": False, "delay_ms": 0},
}

app = FastAPI(title="mock-model-server")
CONFIG = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
CALLS: Counter = Counter()
DEFAULT_DELAY_MS = int(os.environ.get("MODEL_DELAY_MS", "0"))


class ModelAdmin(BaseModel):
    fail: bool | None = None
    delay_ms: int | None = None
    context_length: int | None = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/admin/models/{model_id}")
async def admin_model(model_id: str, body: ModelAdmin):
    if model_id not in CONFIG:
        raise HTTPException(404, "unknown model")
    for k, v in body.model_dump(exclude_none=True).items():
        CONFIG[model_id][k] = v
    return CONFIG[model_id]


@app.post("/admin/reset")
async def admin_reset():
    CONFIG.clear()
    CONFIG.update({k: dict(v) for k, v in DEFAULT_CONFIG.items()})
    CALLS.clear()
    return {"reset": True}


@app.get("/admin/calls")
async def admin_calls():
    return dict(CALLS)


@app.post("/v1/chat/completions")
async def chat(body: dict):
    model = body.get("model")
    if model not in CONFIG:
        return JSONResponse({"error": {"message": f"unknown model {model}", "type": "invalid_request_error"}}, 404)
    CALLS[model] += 1
    cfg = CONFIG[model]
    delay = cfg.get("delay_ms") or DEFAULT_DELAY_MS
    if delay:
        await asyncio.sleep(delay / 1000)
    if cfg["fail"]:
        return JSONResponse({"error": {"message": "model unavailable (mock failure)", "type": "server_error"}}, 503)
    messages = body.get("messages") or []
    max_tokens = int(body.get("max_tokens") or 16)
    input_tokens = count_messages(messages)
    if input_tokens + max_tokens > cfg["context_length"]:
        return JSONResponse({"error": {"message": "context length exceeded", "type": "invalid_request_error",
                                       "code": "context_length_exceeded"}}, 400)
    words = [f"Mock-answer-from-{model}:"]
    if any("[[leak]]" in message_text(m) for m in messages):
        words.append("ACCT-1234-5678-9012")
    while len(words) < max_tokens:
        words.append("lorem")
    content = " ".join(words[:max_tokens])
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": input_tokens, "completion_tokens": max_tokens,
                  "total_tokens": input_tokens + max_tokens},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8100")), log_level="warning")
