"""S1: a new use case in sandbox makes its first call."""
from tests.helpers import wait_evidence

REQUIRED = ["gov.use_case.id", "gov.business_unit", "gov.team", "gov.root_request.id", "gov.decision",
            "gov.config.version", "gov.policy.version", "gov.route.rank", "gov.route.reason", "gov.record.versions",
            "gov.cost.amount", "gen_ai.request.model", "gen_ai.response.model", "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens", "gen_ai.operation.name", "gov.identity.header_mismatch"]


def test_s1_sandbox_first_call(clean, spans, scenario):
    stack = clean
    with scenario("S1", "allow; charged to sandbox node; span gov.use_case.id=UC-01; non-onboarded client refused "
                        "at Identify; header claiming another use case ignored") as r:
        before = {n: stack.spend(n) for n in ("UC-01", "team-sandbox-lab", "bu-sandbox", "UC-17")}
        resp = stack.chat("uc01-notebook", headers={"x-gov-use-case": "UC-17"})
        after = {n: stack.spend(n) for n in before}
        span = spans.request_span(stack, resp.headers["x-trace-id"])
        a = span["attrs"]
        ev = wait_evidence(stack, resp.headers["x-trace-id"])

        bad = stack.chat("uc99-not-onboarded")
        bad_span = spans.request_span(stack, bad.headers["x-trace-id"])
        stage_names = {s["name"] for s in spans.wait_trace(stack, bad.headers["x-trace-id"])}

        r.check("allowed_200", resp.status_code == 200)
        r.check("span_use_case_UC-01", a.get("gov.use_case.id") == "UC-01")
        r.check("decision_allow", a.get("gov.decision") == "allow")
        r.check("sandbox_nodes_charged", all(after[n] > before[n] for n in ("UC-01", "team-sandbox-lab", "bu-sandbox")))
        r.check("claimed_use_case_not_charged", after["UC-17"] == before["UC-17"])
        r.check("header_ignored_and_logged", a.get("gov.identity.header_mismatch") is True
                and a.get("gov.identity.header_use_case") == "UC-17")
        r.check("non_onboarded_401", bad.status_code == 401)
        r.check("refused_at_identify", bad_span["attrs"].get("gov.decision.stage") == "identify"
                and bad_span["attrs"].get("gov.decision.reason") == "identity_not_onboarded")
        r.check("pipeline_stopped_at_identify", "gateway.authorize" not in stage_names)
        r.observed(f"HTTP {resp.status_code} decision={a.get('gov.decision')} uc={a.get('gov.use_case.id')} "
                   f"model={a.get('gen_ai.response.model')} sandbox spend +{after['UC-01'] - before['UC-01']} uUSD; "
                   f"header UC-17 ignored={a.get('gov.identity.header_mismatch')}; non-onboarded -> HTTP {bad.status_code} "
                   f"{bad_span['attrs'].get('gov.decision.reason')} at {bad_span['attrs'].get('gov.decision.stage')}")
        r.evidence(REQUIRED, a, ev)
