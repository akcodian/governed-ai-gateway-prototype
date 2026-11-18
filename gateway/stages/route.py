"""Stage 4 - Route (R3, R6).

Walk the pairing in rank order and accept an entry only if
  * the pairing is current and its approving-function signature is valid,
  * the model's governance record is current and allows the request's region and data classes
    (evaluated by OPA policy gov.route),
  * the model is healthy (circuit breaker), and
  * its capabilities suffice (context length, tool calling, structured output).
If no entry is left the policy's fallback applies: interactive -> degrade, batch -> hold.
The router never calls a model outside the pairing unless a recorded one-time exception names it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from gateway.circuit import CircuitBreakers
from gateway.opa import OpaClient
from gateway.stages.authorize import now_rfc3339
from govlib import keys
from govlib.canonical import canonical_json, pairing_signing_payload

PRIMARY_UNHEALTHY = {"model_unhealthy", "upstream_error", "upstream_rejected"}


@lru_cache(maxsize=1)
def _approver_key():
    return keys.load_public("approving_function")


@lru_cache(maxsize=4096)
def _sig_ok(payload: bytes, sig: str | None) -> bool:
    return keys.verify_b64(_approver_key(), payload, sig)


def signature_valid(use_case_id: str, entry: dict) -> bool:
    return _sig_ok(pairing_signing_payload(use_case_id, entry), (entry.get("approval") or {}).get("signature"))


@dataclass
class Features:
    input_tokens: int
    max_tokens: int
    needs_tools: bool
    needs_structured: bool


def capability_ok(model: dict, f: Features) -> bool:
    c = model["capabilities"]
    if f.input_tokens + f.max_tokens > c["context_length"]:
        return False
    if f.needs_tools and not c["tool_calling"]:
        return False
    if f.needs_structured and not c["structured_output"]:
        return False
    return True


@dataclass
class RoutePlan:
    eligible: list[dict] = field(default_factory=list)        # [{"model", "rank", "record_version"}]
    skipped: dict[str, list[str]] = field(default_factory=dict)  # model -> reasons
    primary_model: str | None = None
    fallback: str = "hold"
    record_versions: dict[str, str] = field(default_factory=dict)

    def skip(self, model: str, reason: str) -> None:
        self.skipped.setdefault(model, [])
        if reason not in self.skipped[model]:
            self.skipped[model].append(reason)

    def route_reason(self, served_rank: int) -> str:
        if served_rank == 1:
            return "primary"
        reasons = self.skipped.get(self.primary_model or "", [])
        if not reasons:
            return "requested_model"
        if any(r in PRIMARY_UNHEALTHY for r in reasons):
            return "primary_unhealthy"
        return reasons[0]


async def plan_route(*, snapshot: dict, use_case_id: str, data_classes: list[str], region: str, opa: OpaClient,
                     breakers: CircuitBreakers, features: Features, requested_model: str | None, span) -> RoutePlan:
    decision = await opa.decide("gov/route/decision", {
        "use_case_id": use_case_id, "data_classes": data_classes, "region": region, "now": now_rfc3339()})
    plan = RoutePlan(fallback=decision["fallback"])
    entries = {e["model"]: e for e in snapshot["pairings"].get(use_case_id, [])}
    candidates = sorted(decision["candidates"], key=lambda c: c["rank"])
    if requested_model and requested_model != "auto":
        candidates.sort(key=lambda c: (c["model"] != requested_model, c["rank"]))
    if candidates:
        plan.primary_model = min(candidates, key=lambda c: c["rank"])["model"]
    for c in candidates:
        model_id = c["model"]
        model = snapshot["models"][model_id]
        plan.record_versions[model_id] = c["record_version"]
        if not signature_valid(use_case_id, entries[model_id]):
            plan.skip(model_id, "pairing_signature_invalid")
        for r in c["reasons"]:
            plan.skip(model_id, r)
        if not breakers.available(model_id):
            plan.skip(model_id, "model_unhealthy")
        if not capability_ok(model, features):
            plan.skip(model_id, "insufficient_capability")
        if model_id not in plan.skipped:
            plan.eligible.append({"model": model_id, "rank": c["rank"], "record_version": c["record_version"]})
    span.set_attribute("gov.route.candidates", ",".join(f"{c['rank']}:{c['model']}" for c in candidates))
    span.set_attribute("gov.route.eligible", ",".join(e["model"] for e in plan.eligible))
    for m, rs in plan.skipped.items():
        span.set_attribute(f"gov.route.skipped.{m}", ",".join(rs))
    return plan


async def check_exception(*, snapshot: dict, use_case_id: str, data_classes: list[str], region: str,
                          exception_model: str, opa: OpaClient, breakers: CircuitBreakers,
                          features: Features) -> tuple[bool, list[str]]:
    decision = await opa.decide("gov/route/decision", {
        "use_case_id": use_case_id, "data_classes": data_classes, "region": region, "now": now_rfc3339(),
        "exception_model": exception_model})
    exc = decision.get("exception") or {"eligible": False, "reasons": ["model_not_in_catalog"]}
    reasons = list(exc["reasons"])
    model = snapshot["models"].get(exception_model)
    if model is not None:
        if not breakers.available(exception_model):
            reasons.append("model_unhealthy")
        if not capability_ok(model, features):
            reasons.append("insufficient_capability")
    return (exc["eligible"] and not reasons), reasons


def record_versions_attr(versions: dict[str, str]) -> str:
    return ",".join(f"{m}={v}" for m, v in versions.items())


__all__ = ["Features", "RoutePlan", "plan_route", "check_exception", "record_versions_attr", "canonical_json"]
