"""S3: the primary model is unavailable."""
from tests.helpers import wait_evidence

REQUIRED = ["gov.use_case.id", "gov.root_request.id", "gov.decision", "gov.decision.reason", "gov.route.rank",
            "gov.route.reason", "gov.record.versions", "gov.config.version", "gov.policy.version", "gov.cost.amount",
            "gen_ai.request.model", "gen_ai.response.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
            "route_skipped"]


def test_s3_primary_unavailable(clean, spans, scenario):
    stack = clean
    with scenario("S3", "served by model B; gov.route.rank=2; gov.route.reason=primary_unhealthy") as r:
        stack.set_model("model-a", fail=True)
        warm = [stack.chat("uc17-batch-runner") for _ in range(3)]       # A fails 3x -> circuit opens
        a_calls_before = stack.model_calls().get("model-a", 0)
        resp = stack.chat("uc17-batch-runner")
        a_calls_after = stack.model_calls().get("model-a", 0)
        a = spans.request_span(stack, resp.headers["x-trace-id"])["attrs"]
        ev = wait_evidence(stack, resp.headers["x-trace-id"])
        r.check("opening_requests_served", all(w.status_code == 200 for w in warm))
        r.check("http_200", resp.status_code == 200)
        r.check("served_by_B", a.get("gen_ai.response.model") == "model-b")
        r.check("rank_2", a.get("gov.route.rank") == 2)
        r.check("reason_primary_unhealthy", a.get("gov.route.reason") == "primary_unhealthy")
        r.check("circuit_open_A_not_called", a_calls_after == a_calls_before)
        r.observed(f"HTTP {resp.status_code} model={a.get('gen_ai.response.model')} rank={a.get('gov.route.rank')} "
                   f"reason={a.get('gov.route.reason')}; model-a called {a_calls_after - a_calls_before}x after circuit opened")
        r.evidence(REQUIRED, a, ev)
