"""Shared helpers for integration tests and the benchmark (talk to the running stack on localhost)."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import redis

from govlib import keys, tokens

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
# Defaults reach the stack through published ports; the in-network benchmark overrides them.
GATEWAY = os.environ.get("BENCH_GATEWAY_URL", "http://localhost:8080")
APPROVAL = os.environ.get("BENCH_APPROVAL_URL", "http://localhost:8090")
MODEL_SERVER = os.environ.get("BENCH_MODEL_URL", "http://localhost:8100")
MCP_SERVER = os.environ.get("BENCH_MCP_URL", "http://localhost:8200")
REDIS_HOST = os.environ.get("BENCH_REDIS_HOST", "localhost")
SPANS_FILE = ROOT / "telemetry" / "out" / "spans.jsonl"
LOGS_FILE = ROOT / "telemetry" / "out" / "logs.jsonl"
EVIDENCE_DIR = ROOT / "evidence"


def admin_headers() -> dict:
    return {"x-admin-token": keys.read_secret("admin_token")}


def auth(client_id: str, **extra) -> dict:
    h = {"authorization": f"Bearer {tokens.issue_workload_token(client_id)}"}
    h.update(extra)
    return h


def words(n: int, word: str = "claim") -> str:
    return " ".join([word] * n)


class Stack:
    def __init__(self):
        self.http = httpx.Client(timeout=60)
        self.redis = redis.Redis(host=REDIS_HOST, port=6379, socket_timeout=2)

    # --- gateway calls
    def chat(self, client_id: str, prompt: str = "summarise the synthetic claim", max_tokens: int = 16,
             model: str | None = None, headers: dict | None = None, **body) -> httpx.Response:
        payload = {"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, **body}
        if model:
            payload["model"] = model
        return self.http.post(f"{GATEWAY}/v1/chat/completions", json=payload, headers={**auth(client_id), **(headers or {})})

    def mcp(self, client_id: str, method: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        return self.http.post(f"{GATEWAY}/mcp/mcp-docs", headers={**auth(client_id), **(headers or {})},
                              json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def a2a(self, client_id: str, agent: str, task: dict, headers: dict | None = None) -> httpx.Response:
        return self.http.post(f"{GATEWAY}/a2a/{agent}", headers={**auth(client_id), **(headers or {})}, json={
            "jsonrpc": "2.0", "id": "t1", "method": "SendMessage",
            "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": json.dumps(task)}]}}})

    # --- mocks and admin
    def set_model(self, model: str, **cfg) -> None:
        self.http.post(f"{MODEL_SERVER}/admin/models/{model}", json=cfg).raise_for_status()

    def model_calls(self) -> dict:
        return self.http.get(f"{MODEL_SERVER}/admin/calls").json()

    def tool_calls(self) -> dict:
        return self.http.get(f"{MCP_SERVER}/admin/calls").json()

    def reset(self) -> None:
        self.http.post(f"{MODEL_SERVER}/admin/reset").raise_for_status()
        self.http.post(f"{MCP_SERVER}/admin/reset").raise_for_status()
        self.http.post(f"{GATEWAY}/admin/reset", headers=admin_headers()).raise_for_status()

    def flush(self) -> None:
        self.http.post(f"{GATEWAY}/admin/flush", headers=admin_headers())

    def reload(self, sub: str | None = None) -> httpx.Response:
        return self.http.post(f"{GATEWAY}/admin/reload", headers=admin_headers(), json={"dir": sub} if sub else {})

    def health(self) -> dict:
        return self.http.get(f"{GATEWAY}/health").json()

    def spend(self, node: str) -> int:
        period = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        return int(self.redis.get(f"spend:{period}:{node}") or 0)


class SpanIndex:
    """Incrementally reads the collector's span file and indexes spans by trace ID."""

    def __init__(self, path: Path = SPANS_FILE):
        self.path = path
        self.offset = 0
        self.by_trace: dict[str, list[dict]] = {}
        self.all: list[dict] = []

    def refresh(self) -> None:
        from telemetry.otlp import attrs
        if not self.path.exists():
            return
        with self.path.open("rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for line in data[: end + 1].splitlines():
            if not line.strip():
                continue
            batch = json.loads(line)
            for rs in batch.get("resourceSpans", []):
                svc = attrs(rs.get("resource", {}).get("attributes")).get("service.name")
                for ss in rs.get("scopeSpans", []):
                    for sp in ss.get("spans", []):
                        s = {"trace_id": sp["traceId"].lower(), "span_id": sp["spanId"].lower(), "name": sp["name"],
                             "start_ns": int(sp["startTimeUnixNano"]), "end_ns": int(sp["endTimeUnixNano"]),
                             "service": svc, "attrs": attrs(sp.get("attributes"))}
                        self.by_trace.setdefault(s["trace_id"], []).append(s)
                        self.all.append(s)

    def wait_trace(self, stack: Stack, trace_id: str, timeout: float = 20.0, need: str = "gateway.request") -> list[dict]:
        deadline = time.time() + timeout
        stack.flush()
        while time.time() < deadline:
            self.refresh()
            spans = self.by_trace.get(trace_id, [])
            if any(s["name"] == need for s in spans):
                time.sleep(0.3)
                self.refresh()
                return self.by_trace.get(trace_id, [])
            time.sleep(0.25)
        raise AssertionError(f"no {need} span for trace {trace_id} within {timeout}s")

    def request_span(self, stack: Stack, trace_id: str) -> dict:
        return next(s for s in self.wait_trace(stack, trace_id) if s["name"] == "gateway.request")

    def requests_for_root(self, root_id: str) -> list[dict]:
        self.refresh()
        return [s for s in self.all if s["name"] == "gateway.request" and s["attrs"].get("gov.root_request.id") == root_id]


def evidence_records(trace_id: str | None = None, kind: str | None = None, file: str = "gateway.jsonl") -> list[dict]:
    path = EVIDENCE_DIR / file
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if kind and e["kind"] != kind:
            continue
        if trace_id and e["record"].get("trace_id") != trace_id:
            continue
        out.append(e)
    return out


def wait_evidence(stack: Stack, trace_id: str, timeout: float = 10.0, where=None) -> dict:
    """Decision record for a trace. A trace can hold several gateway decisions (e.g. an A2A delegation
    and the delegated agent's model call share trace context); `where` selects one."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        stack.flush()
        recs = [e for e in evidence_records(trace_id, "decision") if where is None or where(e["record"])]
        if recs:
            return recs[-1]["record"]
        time.sleep(0.25)
    raise AssertionError(f"no evidence record for trace {trace_id}")


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True)


# STACK_MODE=compose (default): services run under docker compose.
# STACK_MODE=native: services run as local processes (scripts/native_stack.py, e.g. on Code Ocean).
STACK_MODE = os.environ.get("STACK_MODE", "compose")


def redis_stop() -> None:
    if STACK_MODE == "native":
        from scripts.native_stack import stop_redis
        stop_redis()
    else:
        compose("stop", "redis")


def redis_start() -> None:
    if STACK_MODE == "native":
        from scripts.native_stack import start_redis
        start_redis()
    else:
        compose("start", "redis")


# ------------------------------------------------------------------ result recording
@dataclass
class Row:
    scenario: str
    expected: str
    observed: str = ""
    checks: dict = field(default_factory=dict)
    required: list = field(default_factory=list)
    present: list = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and bool(self.checks) and all(self.checks.values())

    @property
    def completeness(self) -> float | None:
        if not self.required:
            return None
        return 100.0 * len(self.present) / len(self.required)


class Recorder:
    def __init__(self):
        self.rows: dict[str, list[Row]] = {"scenarios": [], "supplementary": []}

    def open(self, kind: str, scenario: str, expected: str) -> "RowCtx":
        row = Row(scenario, expected)
        self.rows[kind].append(row)
        return RowCtx(row)

    def write(self) -> None:
        RESULTS.mkdir(exist_ok=True)
        for kind, rows in self.rows.items():
            if not rows:
                continue
            with (RESULTS / f"{kind}.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["scenario", "expected", "observed", "pass", "evidence_completeness_pct",
                            "failed_checks", "missing_attributes"])
                for r in rows:
                    failed = [k for k, v in r.checks.items() if not v]
                    if r.error:
                        failed.append(f"error: {r.error}")
                    w.writerow([r.scenario, r.expected, r.observed, "pass" if r.passed else "fail",
                                "" if r.completeness is None else f"{r.completeness:.1f}", "; ".join(failed),
                                "; ".join(sorted(set(r.required) - set(r.present)))])


class RowCtx:
    def __init__(self, row: Row):
        self.row = row

    def __enter__(self):
        return self

    def check(self, name: str, ok: bool) -> bool:
        self.row.checks[name] = bool(ok)
        return bool(ok)

    def observed(self, text: str) -> None:
        self.row.observed = text

    def evidence(self, required: list[str], *sources: dict) -> None:
        """Share of required attributes present across spans and evidence records."""
        merged: dict = {}
        for s in sources:
            merged.update({k: v for k, v in s.items() if v not in (None, "")})
        self.row.required = list(required)
        self.row.present = [k for k in required if k in merged]

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and not isinstance(exc, AssertionError):
            self.row.error = f"{exc_type.__name__}: {exc}"[:300]
        failed = [k for k, v in self.row.checks.items() if not v]
        if exc is None and failed:
            raise AssertionError(f"{self.row.scenario} failed checks: {failed}; observed: {self.row.observed}")
        if isinstance(exc, AssertionError):
            self.row.error = str(exc)[:300]
        return False
