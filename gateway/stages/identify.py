"""Stage 1 - Identify (R2).

* Validate the workload JWT (simulated IdP) and resolve the use case from the credential via the
  snapshot's workload registry - never from a request header. A disagreeing header is ignored
  and logged.
* Reject identities that are not onboarded.
* Join an existing root request (gateway-signed root token or delegation token) or create one.
"""
from __future__ import annotations

import re
import uuid

import jwt

from gateway.context import GovRefusal, RequestContext
from govlib import tokens

USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def identify(ctx: RequestContext, headers, snapshot: dict, span) -> None:
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise GovRefusal(401, "identify", "missing_credential")
    try:
        claims = tokens.verify_workload_token(auth[7:].strip())
    except jwt.PyJWTError:
        raise GovRefusal(401, "identify", "invalid_credential")
    client_id = claims["sub"]
    ctx.client_id = client_id
    span.set_attribute("gov.client.id", client_id)

    use_case_id = snapshot["workloads"].get(client_id)
    if use_case_id is None:
        raise GovRefusal(401, "identify", "identity_not_onboarded")
    uc = snapshot["use_cases"][use_case_id]
    ctx.use_case_id, ctx.use_case = use_case_id, uc
    ctx.attrs.update({"gov.use_case.id": use_case_id, "gov.business_unit": uc["business_unit"],
                      "gov.team": uc["team"]})

    claimed = headers.get("x-gov-use-case")
    if claimed is not None and claimed != use_case_id:
        # The header is ignored; the credential decides. Record the disagreement.
        ctx.attrs["gov.identity.header_mismatch"] = True
        ctx.attrs["gov.identity.header_use_case"] = claimed[:32]
        ctx.evidence["header_use_case_ignored"] = claimed[:32]

    user = headers.get("x-gov-end-user", "anonymous")
    ctx.end_user = user if USER_RE.match(user) else "invalid"

    delegation = headers.get("x-gov-delegation")
    root_token = headers.get("x-gov-root-token")
    if delegation:
        try:
            d = tokens.verify_downstream_token(delegation, audience=f"a2a:{client_id}")
        except jwt.PyJWTError:
            raise GovRefusal(401, "identify", "invalid_delegation_token")
        if d["uc"] != use_case_id:
            raise GovRefusal(403, "identify", "delegation_use_case_mismatch")
        ctx.root_request_id, ctx.root_budget_tokens, ctx.hop = d["rrid"], d.get("budget"), int(d.get("hop", 0))
        ctx.attrs["gov.delegation.actor"] = d.get("act", {}).get("sub", "")
    elif root_token:
        try:
            r = tokens.verify_root_token(root_token)
        except jwt.PyJWTError:
            raise GovRefusal(401, "identify", "invalid_root_token")
        if r["uc"] != use_case_id:
            raise GovRefusal(403, "identify", "root_token_use_case_mismatch")
        ctx.root_request_id, ctx.root_budget_tokens = r["rrid"], r.get("budget")
    else:
        ctx.root_request_id = uuid.uuid4().hex
        ctx.root_budget_tokens = snapshot["budgets"]["root_budget_tokens"].get(use_case_id)
        ctx.root_token_issued = tokens.issue_root_token(ctx.root_request_id, use_case_id, ctx.root_budget_tokens)
        ctx.attrs["gov.root_request.created"] = True
    ctx.attrs["gov.root_request.id"] = ctx.root_request_id
    ctx.attrs["gov.a2a.hop"] = ctx.hop
    if ctx.root_budget_tokens is not None:
        ctx.attrs["gov.root_request.budget_tokens"] = ctx.root_budget_tokens
