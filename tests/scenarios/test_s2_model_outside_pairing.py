"""S2: a request names a model outside the approved pairing."""
from tests.helpers import wait_evidence

REQUIRED = ["gov.use_case.id", "gov.business_unit", "gov.team", "gov.root_request.id", "gov.decision",
            "gov.decision.reason", "gov.decision.stage", "gov.config.version", "gov.policy.version",
            "gen_ai.request.model", "gen_ai.operation.name"]


def test_s2_model_outside_pairing(clean, spans, scenario):
    stack = clean
    with scenario("S2", "HTTP 403; gov.decision=deny; reason model_not_in_pairing; model C receives no call") as r:
        calls_before = stack.model_calls().get("model-c", 0)
        resp = stack.chat("uc17-batch-runner", model="model-c")
        calls_after = stack.model_calls().get("model-c", 0)
        a = spans.request_span(stack, resp.headers["x-trace-id"])["attrs"]
        ev = wait_evidence(stack, resp.headers["x-trace-id"])
        r.check("http_403", resp.status_code == 403)
        r.check("decision_deny", a.get("gov.decision") == "deny")
        r.check("reason_model_not_in_pairing", a.get("gov.decision.reason") == "model_not_in_pairing")
        r.check("model_c_not_called", calls_after == calls_before)
        r.observed(f"HTTP {resp.status_code} decision={a.get('gov.decision')} reason={a.get('gov.decision.reason')} "
                   f"stage={a.get('gov.decision.stage')}; model-c calls {calls_before}->{calls_after}")
        r.evidence(REQUIRED, a, ev)
