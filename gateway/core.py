"""The data plane: five explicit, separately timed stages per request.

Span tree per request:
  gateway.request
    gateway.identify | gateway.authorize | gateway.budget | gateway.route | gateway.guard (request)
    gateway.upstream (one per model attempt; not a governance stage)
    gateway.guard (response) | gateway.budget (reconcile)
A refusal ends the pipeline at the stage that refused.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any

import httpx
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

from gateway.circuit import CircuitBreakers
from gateway.config import Settings
from gateway.context import GovRefusal, RequestContext
from gateway.opa import OpaClient
from gateway.snapshot import SnapshotManager
from gateway.stages import guard
from gateway.stages.authorize import authorize
from gateway.stages.budget import BudgetManager, Reservation
from gateway.stages.identify import identify
from gateway.stages.route import Features, check_exception, plan_route, record_versions_attr
from govlib import telemetry as T
from govlib import tokens
from govlib.canonical import tool_definition_hash
from govlib.tokencount import count_messages, count_text
from telemetry.evidence_store.store import AsyncEvidenceWriter

log = logging.getLogger("gateway")


import os

# Native (non-Docker) development only: map container host names to other hosts, e.g.
# GOV_HOST_OVERRIDES="model-server=localhost,mcp-server=localhost". Empty in docker-compose.
_HOST_OVERRIDES = dict(p.split("=", 1) for p in os.environ.get("GOV_HOST_OVERRIDES", "").split(",") if "=" in p)


def resolve(url: str) -> str:
    for host, repl in _HOST_OVERRIDES.items():
        url = url.replace(f"//{host}:", f"//{repl}:")
    return url


def model_cost(model: dict, input_tokens: int, output_tokens: int) -> float:
    return input_tokens / 1000 * model["price_per_1k_input"] + output_tokens / 1000 * model["price_per_1k_output"]


class Gateway:
    def __init__(self, settings: Settings, tracer, evidence: AsyncEvidenceWriter):
        self.s = settings
        self.tracer = tracer
        self.evidence = evidence
        self.opa = OpaClient(settings.opa_url)
        self.snapshots = SnapshotManager(self.opa, self.emit)
        self.breakers = CircuitBreakers(settings.cb_failure_threshold, settings.cb_reset_seconds)
        self.budget = BudgetManager(settings.redis_url, self.opa, settings.redis_timeout_s)
        self.http = httpx.AsyncClient(timeout=settings.upstream_timeout_s,
                                      limits=httpx.Limits(max_keepalive_connections=100, max_connections=200))
        self.quarantine: set[tuple[str, str]] = set()
        self.used_exceptions: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def emit(self, kind: str, record: dict) -> None:
        self.evidence.emit(kind, record)

    @property
    def snap(self) -> dict:
        if self.snapshots.current is None:
            raise GovRefusal(503, "identify", "no_active_config")
        return self.snapshots.current

    @contextmanager
    def stage(self, name: str, **attrs):
        with self.tracer.start_as_current_span(f"gateway.{name}") as span:
            for k, v in attrs.items():
                span.set_attribute(k, v)
            try:
                yield span
            except GovRefusal as r:
                span.set_attribute("gov.decision", r.decision)
                span.set_attribute("gov.decision.reason", r.reason)
                span.set_status(Status(StatusCode.ERROR, r.reason))
                raise

    def _new_ctx(self, operation: str, span) -> RequestContext:
        ctx = RequestContext(operation=operation)
        ctx.trace_id = format(span.get_span_context().trace_id, "032x")
        snap = self.snapshots.current or {}
        ctx.config_version, ctx.policy_version = snap.get("config_version"), snap.get("policy_version")
        return ctx

    def _finish(self, span, ctx: RequestContext, started: float) -> None:
        ctx.attrs.update({"gov.decision": ctx.decision, "gov.config.version": ctx.config_version or -1,
                          "gov.policy.version": ctx.policy_version or ""})
        if ctx.reason:
            ctx.attrs["gov.decision.reason"] = ctx.reason
        if ctx.stage:
            ctx.attrs["gov.decision.stage"] = ctx.stage
        for k, v in ctx.attrs.items():
            if v is not None:
                span.set_attribute(k, v)
        record = {"request_id": ctx.request_id, "trace_id": ctx.trace_id, "operation": ctx.operation,
                  "client_id": ctx.client_id, "root_request_id": ctx.root_request_id,
                  "gateway_ms": round((time.perf_counter() - started) * 1000, 3),
                  **{k: v for k, v in ctx.attrs.items() if k.startswith(("gov.", "gen_ai."))}, **ctx.evidence}
        self.emit("decision", record)

    def _headers(self, ctx: RequestContext) -> dict:
        h = {"x-trace-id": ctx.trace_id, "x-gov-decision": ctx.decision}
        if ctx.root_request_id:
            h["x-gov-root-request-id"] = ctx.root_request_id
        if ctx.root_token_issued:
            h["x-gov-root-token"] = ctx.root_token_issued
        return h

    def _refusal(self, span, ctx: RequestContext, r: GovRefusal, started: float, jsonrpc_id: Any = None):
        ctx.decision, ctx.reason, ctx.stage = r.decision, r.reason, r.stage
        span.set_status(Status(StatusCode.ERROR, r.reason))
        self._finish(span, ctx, started)
        body = {"decision": r.decision, "reason": r.reason, "stage": r.stage, "detail": r.detail,
                "root_request_id": ctx.root_request_id,
                "error": {"type": "governance_refusal", "message": f"{r.stage}: {r.reason}"}}
        if jsonrpc_id is not None or ctx.operation.startswith(("mcp", "a2a")):
            body = {"jsonrpc": "2.0", "id": jsonrpc_id,
                    "error": {"code": -32003, "message": f"{r.stage}: {r.reason}", "data": body}}
        return JSONResponse(body, status_code=r.status, headers=self._headers(ctx))

    # ------------------------------------------------------------------ chat
    async def handle_chat(self, headers, body: dict) -> JSONResponse:
        started = time.perf_counter()
        with self.tracer.start_as_current_span("gateway.request", context=extract(dict(headers)),
                                               kind=SpanKind.SERVER) as span:
            ctx = self._new_ctx("chat", span)
            ctx.attrs[T.GEN_AI_OPERATION_NAME] = "chat"
            reservation: Reservation | None = None
            try:
                snap = self.snap
                with self.stage("identify") as s:
                    identify(ctx, headers, snap, s)
                messages = body.get("messages") or []
                max_tokens = int(body.get("max_tokens") or self.s.default_max_tokens)
                requested = body.get("model")
                if requested:
                    ctx.attrs[T.GEN_AI_REQUEST_MODEL] = requested
                with self.stage("authorize") as s:
                    fields = {"max_tokens": max_tokens}
                    if requested:
                        fields["requested_model"] = requested
                    await authorize(ctx, self.opa, "model.chat", s, **fields)

                input_tokens = count_messages(messages)
                pairing = snap["pairings"].get(ctx.use_case_id, [])
                primary = requested if requested and requested != "auto" else pairing[0]["model"]
                est_cost = max(model_cost(snap["models"][e["model"]], input_tokens, max_tokens) for e in pairing)
                with self.stage("budget", **{"gov.budget.phase": "reserve"}) as s:
                    reservation = await self.budget.reserve(snap, ctx, input_tokens + max_tokens, est_cost, primary, s)

                features = Features(input_tokens, max_tokens, bool(body.get("tools")),
                                    (body.get("response_format") or {}).get("type") in ("json_object", "json_schema"))
                with self.stage("route") as s:
                    plan = await plan_route(snapshot=snap, use_case_id=ctx.use_case_id,
                                            data_classes=ctx.use_case["data_classes"], region=self.s.region,
                                            opa=self.opa, breakers=self.breakers, features=features,
                                            requested_model=requested, span=s)
                with self.stage("guard") as s:
                    red_messages, counts = guard.redact_messages(messages)
                    guard.record(s, ctx, "request", counts)

                upstream_body = {k: v for k, v in body.items() if k not in ("model", "messages")}
                upstream_body.update({"messages": red_messages, "max_tokens": max_tokens})
                served, resp = None, None
                for cand in plan.eligible:
                    status, resp = await self._call_model(snap["models"][cand["model"]], upstream_body)
                    if status == 200:
                        served = cand
                        break
                    plan.skip(cand["model"], "upstream_error" if status is None or status >= 500 else "upstream_rejected")
                    resp = None
                ctx.attrs["gov.record.versions"] = record_versions_attr(plan.record_versions)
                if plan.skipped:
                    ctx.attrs["gov.route.skipped"] = ";".join(f"{m}:{','.join(rs)}" for m, rs in plan.skipped.items())
                ctx.evidence["route_skipped"] = plan.skipped

                if served is None:
                    await self.budget.release(reservation)
                    reservation = None
                    if plan.fallback == "degrade":
                        return self._degrade(span, ctx, plan, started)
                    return await self._hold(span, ctx, plan, upstream_body, started)

                with self.stage("guard") as s:
                    rcounts = guard.Counter()
                    for ch in resp.get("choices", []):
                        msg = ch.get("message") or {}
                        if isinstance(msg.get("content"), str):
                            msg["content"] = guard.redact_text(msg["content"], rcounts)
                    guard.record(s, ctx, "response", rcounts)

                usage = resp.get("usage") or {}
                in_tok = int(usage.get("prompt_tokens", input_tokens))
                out_tok = int(usage.get("completion_tokens", 0))
                model = snap["models"][served["model"]]
                cost = model_cost(model, in_tok, out_tok)
                with self.stage("budget", **{"gov.budget.phase": "reconcile"}):
                    await self.budget.reconcile(reservation, in_tok + out_tok, cost)

                ctx.decision = "allow"
                ctx.reason = plan.route_reason(served["rank"])
                ctx.attrs.update({
                    T.GEN_AI_REQUEST_MODEL: requested or primary,
                    T.GEN_AI_RESPONSE_MODEL: served["model"],
                    T.GEN_AI_PROVIDER_NAME: model["provider"],
                    T.GEN_AI_USAGE_INPUT_TOKENS: in_tok,
                    T.GEN_AI_USAGE_OUTPUT_TOKENS: out_tok,
                    "gov.route.rank": served["rank"],
                    "gov.route.reason": ctx.reason,
                    "gov.cost.amount": round(cost, 8),
                })
                self._finish(span, ctx, started)
                resp["gov"] = {"decision": "allow", "route_rank": served["rank"], "route_reason": ctx.reason,
                               "model": served["model"], "root_request_id": ctx.root_request_id,
                               "config_version": ctx.config_version, "cost_usd": round(cost, 8)}
                return JSONResponse(resp, headers=self._headers(ctx))
            except GovRefusal as r:
                await self.budget.release(reservation)
                return self._refusal(span, ctx, r, started)

    async def _call_model(self, model: dict, body: dict) -> tuple[int | None, dict | None]:
        with self.tracer.start_as_current_span("gateway.upstream", kind=SpanKind.CLIENT) as s:
            s.set_attribute(T.GEN_AI_OPERATION_NAME, "chat")
            s.set_attribute(T.GEN_AI_REQUEST_MODEL, model["id"])
            s.set_attribute(T.GEN_AI_PROVIDER_NAME, model["provider"])
            headers: dict = {}
            inject(headers)
            try:
                r = await self.http.post(resolve(model["endpoint"]), json={**body, "model": model["id"]}, headers=headers)
            except httpx.HTTPError as exc:
                s.set_attribute("gov.upstream.error", type(exc).__name__)
                self.breakers.record_failure(model["id"])
                return None, None
            s.set_attribute("http.response.status_code", r.status_code)
            if r.status_code >= 500:
                self.breakers.record_failure(model["id"])
                s.set_attribute("gov.circuit.state", self.breakers.state(model["id"]))
                return r.status_code, None
            if r.status_code != 200:
                return r.status_code, None
            self.breakers.record_success(model["id"])
            return 200, r.json()

    def _degrade(self, span, ctx: RequestContext, plan, started: float) -> JSONResponse:
        ctx.decision, ctx.reason, ctx.stage = "degrade", "no_eligible_model", "route"
        ctx.attrs["gov.route.reason"] = "no_eligible_model"
        self._finish(span, ctx, started)
        return JSONResponse({
            "degraded": True, "decision": "degrade", "reason": "no_eligible_model", "skipped": plan.skipped,
            "choices": [{"index": 0, "message": {"role": "assistant", "content":
                         "This assistant cannot answer with an approved model right now. Please try again later."},
                         "finish_reason": "stop"}],
            "gov": {"decision": "degrade", "root_request_id": ctx.root_request_id},
        }, headers=self._headers(ctx))

    async def _hold(self, span, ctx: RequestContext, plan, upstream_body: dict, started: float) -> JSONResponse:
        carrier: dict = {}
        inject(carrier)
        hold_req = {"use_case_id": ctx.use_case_id, "client_id": ctx.client_id, "end_user": ctx.end_user,
                    "root_request_id": ctx.root_request_id, "data_classes": ctx.use_case["data_classes"],
                    "interaction": ctx.use_case["interaction"], "request": upstream_body,
                    "reasons": plan.skipped, "config_version": ctx.config_version, "trace_context": carrier,
                    "trace_id": ctx.trace_id}
        r = await self.http.post(f"{self.s.approval_url}/holds", json=hold_req,
                                 headers={"x-internal-token": INTERNAL_TOKEN()})
        if r.status_code != 201:
            raise GovRefusal(503, "route", "approval_service_unavailable")
        hold_id = r.json()["hold_id"]
        ctx.decision, ctx.reason, ctx.stage = "hold", "no_eligible_model", "route"
        ctx.attrs.update({"gov.route.reason": "no_eligible_model", "gov.hold.id": hold_id})
        self._finish(span, ctx, started)
        return JSONResponse({"decision": "hold", "hold_id": hold_id, "reason": "no_eligible_model",
                             "skipped": plan.skipped, "status_url": f"/v1/holds/{hold_id}",
                             "root_request_id": ctx.root_request_id},
                            status_code=202, headers=self._headers(ctx))

    # ------------------------------------------------------------------ resume after approval
    async def resume_hold(self, hold_id: str) -> JSONResponse:
        started = time.perf_counter()
        r = await self.http.get(f"{self.s.approval_url}/holds/{hold_id}", headers={"x-internal-token": INTERNAL_TOKEN()})
        if r.status_code != 200:
            return JSONResponse({"error": "unknown hold"}, 404)
        hold = r.json()
        links = []
        parent = extract(hold.get("trace_context") or {})
        pctx = trace.get_current_span(parent).get_span_context()
        if pctx.is_valid:
            links.append(Link(pctx, {"gov.link": "hold"}))
        with self.tracer.start_as_current_span("gateway.request", links=links, kind=SpanKind.SERVER,
                                               context=None) as span:
            ctx = self._new_ctx("chat.resume", span)
            ctx.attrs[T.GEN_AI_OPERATION_NAME] = "chat"
            ctx.attrs["gov.hold.id"] = hold_id
            ctx.attrs["gov.hold.trace_id"] = hold.get("trace_id", "")
            reservation = None
            try:
                snap = self.snap
                with self.stage("identify") as s:
                    if hold["status"] != "approved" or not hold.get("exception_id"):
                        raise GovRefusal(409, "identify", "hold_not_approved")
                    use_case_id = snap["workloads"].get(hold["client_id"])
                    if use_case_id != hold["use_case_id"]:
                        raise GovRefusal(403, "identify", "identity_not_onboarded")
                    uc = snap["use_cases"][use_case_id]
                    ctx.client_id, ctx.use_case_id, ctx.use_case = hold["client_id"], use_case_id, uc
                    ctx.end_user, ctx.root_request_id = hold.get("end_user", "anonymous"), hold["root_request_id"]
                    ctx.attrs.update({"gov.use_case.id": use_case_id, "gov.business_unit": uc["business_unit"],
                                      "gov.team": uc["team"], "gov.root_request.id": ctx.root_request_id,
                                      "gov.exception.id": hold["exception_id"], "gov.approver": hold["approver"]})
                    s.set_attribute("gov.client.id", ctx.client_id)
                body = hold["request"]
                exception_model = hold["exception_model"]
                max_tokens = int(body.get("max_tokens") or self.s.default_max_tokens)
                with self.stage("authorize") as s:
                    await authorize(ctx, self.opa, "model.chat", s, max_tokens=max_tokens)
                    if hold["exception_id"] in self.used_exceptions:
                        raise GovRefusal(409, "authorize", "exception_already_used")
                input_tokens = count_messages(body["messages"])
                model = snap["models"].get(exception_model)
                features = Features(input_tokens, max_tokens, bool(body.get("tools")), False)
                with self.stage("route") as s:
                    ok, reasons = await check_exception(
                        snapshot=snap, use_case_id=use_case_id, data_classes=uc["data_classes"], region=self.s.region,
                        exception_model=exception_model, opa=self.opa, breakers=self.breakers, features=features)
                    s.set_attribute("gov.exception.model", exception_model)
                    if not ok:
                        ctx.evidence["exception_rejected"] = reasons
                        raise GovRefusal(403, "route", reasons[0] if reasons else "exception_not_eligible",
                                         detail={"reasons": reasons})
                with self.stage("budget", **{"gov.budget.phase": "reserve"}) as s:
                    reservation = await self.budget.reserve(snap, ctx, input_tokens + max_tokens,
                                                            model_cost(model, input_tokens, max_tokens),
                                                            exception_model, s)
                with self.stage("guard") as s:
                    msgs, counts = guard.redact_messages(body["messages"])
                    guard.record(s, ctx, "request", counts)
                self.used_exceptions.add(hold["exception_id"])  # one-time
                status, resp = await self._call_model(model, {**body, "messages": msgs})
                if status != 200:
                    raise GovRefusal(502, "route", "exception_model_failed")
                with self.stage("guard") as s:
                    rc = guard.Counter()
                    for ch in resp.get("choices", []):
                        m = ch.get("message") or {}
                        if isinstance(m.get("content"), str):
                            m["content"] = guard.redact_text(m["content"], rc)
                    guard.record(s, ctx, "response", rc)
                usage = resp.get("usage") or {}
                in_tok, out_tok = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
                cost = model_cost(model, in_tok, out_tok)
                with self.stage("budget", **{"gov.budget.phase": "reconcile"}):
                    await self.budget.reconcile(reservation, in_tok + out_tok, cost)
                ctx.decision, ctx.reason = "allow", "exception"
                ctx.attrs.update({T.GEN_AI_REQUEST_MODEL: exception_model, T.GEN_AI_RESPONSE_MODEL: exception_model,
                                  T.GEN_AI_PROVIDER_NAME: model["provider"],
                                  T.GEN_AI_USAGE_INPUT_TOKENS: in_tok, T.GEN_AI_USAGE_OUTPUT_TOKENS: out_tok,
                                  "gov.route.rank": 0, "gov.route.reason": "exception",
                                  "gov.record.versions": f"{exception_model}={model['governance_record']['version']}",
                                  "gov.cost.amount": round(cost, 8)})
                self._finish(span, ctx, started)
                resp["gov"] = {"decision": "allow", "route_reason": "exception", "model": exception_model,
                               "exception_id": hold["exception_id"], "approver": hold["approver"],
                               "root_request_id": ctx.root_request_id, "cost_usd": round(cost, 8)}
                return JSONResponse(resp, headers=self._headers(ctx))
            except GovRefusal as r:
                await self.budget.release(reservation)
                return self._refusal(span, ctx, r, started)

    # ------------------------------------------------------------------ MCP
    async def _mcp_tools(self, url: str, token: str) -> list:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        headers = {"authorization": f"Bearer {token}"}
        inject(headers)
        async with httpx2.AsyncClient(headers=headers, timeout=30) as hc:
            async with Client(streamable_http_client(resolve(url), http_client=hc), cache=None) as c:
                return (await c.list_tools()).tools

    async def _mcp_call(self, url: str, token: str, name: str, arguments: dict) -> dict:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        headers = {"authorization": f"Bearer {token}"}
        inject(headers)
        async with httpx2.AsyncClient(headers=headers, timeout=30) as hc:
            async with Client(streamable_http_client(resolve(url), http_client=hc), cache=None) as c:
                res = await c.call_tool(name, arguments)
        return res.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def handle_mcp(self, headers, server_id: str, body: dict) -> JSONResponse:
        started = time.perf_counter()
        method = body.get("method")
        rpc_id = body.get("id")
        op = {"tools/list": "mcp.tools_list", "tools/call": "mcp.tools_call"}.get(method, "mcp.other")
        with self.tracer.start_as_current_span("gateway.request", context=extract(dict(headers)),
                                               kind=SpanKind.SERVER) as span:
            ctx = self._new_ctx(op, span)
            ctx.attrs.update({T.GEN_AI_OPERATION_NAME: "execute_tool" if op == "mcp.tools_call" else "list_tools",
                              "gov.mcp.server": server_id})
            try:
                snap = self.snap
                with self.stage("identify") as s:
                    identify(ctx, headers, snap, s)
                    server = snap["tools"].get(server_id)
                    downstream = tokens.issue_downstream_token(
                        audience=f"mcp:{server_id}", use_case_id=ctx.use_case_id, root_request_id=ctx.root_request_id,
                        root_budget_tokens=ctx.root_budget_tokens, hop=ctx.hop, scope=method or "", actor=ctx.client_id)
                    s.set_attribute("gov.token_exchange.audience", f"mcp:{server_id}")
                with self.stage("authorize") as s:
                    if server is None:
                        raise GovRefusal(404, "authorize", "server_not_registered")
                    live = await self._mcp_tools(server["url"], downstream)
                    current = {t.name: tool_definition_hash(t.name, t.description, t.input_schema) for t in live}
                    params = body.get("params") or {}
                    tool = params.get("name")
                    if tool:
                        ctx.attrs["gen_ai.tool.name"] = tool
                    # quarantine any registered tool whose current definition differs from its registered hash
                    changed = [t for t, h in current.items()
                               if t in server["tools"] and h != server["tools"][t]["sha256"]]
                    for t in changed:
                        if (server_id, t) not in self.quarantine:
                            self.quarantine.add((server_id, t))
                            self.emit("tool_quarantined", {"server": server_id, "tool": t,
                                                           "reason": "tool_definition_changed",
                                                           "trace_id": ctx.trace_id,
                                                           "config_version": ctx.config_version})
                    if changed:
                        s.set_attribute("gov.tool.quarantined", ",".join(sorted(changed)))
                    quarantined = sorted(t for (srv, t) in self.quarantine if srv == server_id)
                    fields = {"server": server_id, "current_hashes": current, "quarantined": quarantined}
                    if tool:
                        fields["tool"] = tool
                    decision = await authorize(ctx, self.opa, op, s, **fields)
                with self.stage("budget", **{"gov.budget.applicable": False}):
                    pass
                with self.stage("route") as s:
                    s.set_attribute("gov.route.target", server["url"])
                with self.stage("guard") as s:
                    counts = guard.Counter()
                    args = guard.redact_json(params.get("arguments") or {}, counts)
                    guard.record(s, ctx, "request", counts)

                if op == "mcp.tools_list":
                    visible = set(decision["visible_tools"])
                    result = {"tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                                        for t in live if t.name in visible]}
                elif op == "mcp.tools_call":
                    with self.tracer.start_as_current_span("gateway.upstream", kind=SpanKind.CLIENT) as us:
                        us.set_attribute("gen_ai.tool.name", tool)
                        result = await self._mcp_call(server["url"], downstream, tool, args)
                else:
                    raise GovRefusal(400, "authorize", "method_not_supported")
                with self.stage("guard") as s:
                    rc = guard.Counter()
                    result = guard.redact_json(result, rc)
                    guard.record(s, ctx, "response", rc)
                ctx.decision, ctx.reason = "allow", "allowed"
                self._finish(span, ctx, started)
                return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result}, headers=self._headers(ctx))
            except GovRefusal as r:
                return self._refusal(span, ctx, r, started, rpc_id)

    # ------------------------------------------------------------------ A2A
    async def handle_a2a(self, headers, agent_id: str, body: dict) -> JSONResponse:
        started = time.perf_counter()
        rpc_id = body.get("id")
        with self.tracer.start_as_current_span("gateway.request", context=extract(dict(headers)),
                                               kind=SpanKind.SERVER) as span:
            ctx = self._new_ctx("a2a.delegate", span)
            ctx.attrs.update({T.GEN_AI_OPERATION_NAME: "invoke_agent", "gen_ai.agent.name": agent_id})
            try:
                snap = self.snap
                with self.stage("identify") as s:
                    identify(ctx, headers, snap, s)
                next_hop = ctx.hop + 1
                ctx.attrs["gov.a2a.next_hop"] = next_hop
                with self.stage("authorize") as s:
                    if body.get("method") != "SendMessage":
                        raise GovRefusal(400, "authorize", "method_not_supported")
                    await authorize(ctx, self.opa, "a2a.delegate", s, agent=agent_id, hop=next_hop)
                with self.stage("budget", **{"gov.budget.applicable": False}):
                    pass  # delegated model calls are charged to the root budget when they happen
                with self.stage("route") as s:
                    url = snap["agents"][agent_id]["jsonrpc_url"]
                    s.set_attribute("gov.route.target", url)
                with self.stage("guard") as s:
                    counts = guard.Counter()
                    fwd = guard.redact_json(body, counts)
                    guard.record(s, ctx, "request", counts)
                    downstream = tokens.issue_downstream_token(
                        audience=f"a2a:{agent_id}", use_case_id=ctx.use_case_id, root_request_id=ctx.root_request_id,
                        root_budget_tokens=ctx.root_budget_tokens, hop=next_hop, scope="a2a:SendMessage",
                        actor=ctx.client_id)
                with self.tracer.start_as_current_span("gateway.upstream", kind=SpanKind.CLIENT) as us:
                    us.set_attribute("gen_ai.agent.name", agent_id)
                    h = {"authorization": f"Bearer {downstream}"}
                    inject(h)
                    r = await self.http.post(resolve(url), json=fwd, headers=h)
                    us.set_attribute("http.response.status_code", r.status_code)
                with self.stage("guard") as s:
                    rc = guard.Counter()
                    out = guard.redact_json(r.json(), rc)
                    guard.record(s, ctx, "response", rc)
                ctx.decision, ctx.reason = "allow", "allowed"
                self._finish(span, ctx, started)
                return JSONResponse(out, status_code=r.status_code, headers=self._headers(ctx))
            except GovRefusal as r:
                return self._refusal(span, ctx, r, started, rpc_id)


def INTERNAL_TOKEN() -> str:  # noqa: N802
    from govlib import keys
    return keys.read_secret("internal_token")


__all__ = ["Gateway", "model_cost", "uuid", "count_text"]
