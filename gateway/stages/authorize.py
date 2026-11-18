"""Stage 2 - Authorize (R4). Asks OPA (gov.authz); default deny; observation-mode rules recorded."""
from __future__ import annotations

import datetime as dt

from gateway.context import GovRefusal, RequestContext
from gateway.opa import OpaClient


def now_rfc3339() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def authorize(ctx: RequestContext, opa: OpaClient, action: str, span, **fields) -> dict:
    input_ = {"action": action, "use_case_id": ctx.use_case_id, "now": now_rfc3339(), **fields}
    decision = await opa.decide("gov/authz/decision", input_)
    if decision.get("would_deny"):
        wd = ",".join(decision["would_deny"])
        ctx.attrs["gov.policy.would_deny"] = wd
        ctx.evidence["would_deny"] = decision["would_deny"]
        span.set_attribute("gov.policy.would_deny", wd)
    if not decision["allow"]:
        reasons = decision.get("reasons") or ["default_deny"]
        ctx.evidence["deny_reasons"] = reasons
        raise GovRefusal(403, "authorize", reasons[0], detail={"reasons": reasons})
    return decision
