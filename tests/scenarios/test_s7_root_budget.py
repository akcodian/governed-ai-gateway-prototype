"""S7: agent fan-out exceeds the root-request budget (5,000 tokens)."""
import json
import time

from tests.helpers import wait_evidence

REQUIRED = ["gov.use_case.id", "gov.root_request.id", "gov.decision", "gov.decision.reason", "gov.decision.stage",
            "gov.config.version", "gov.policy.version", "gov.root_request.budget_tokens", "gov.delegation.actor",
            "client_id", "budget"]


def test_s7_root_budget(clean, spans, scenario):
    stack = clean
    with scenario("S7", "call that would exceed the root budget refused with 429 root_budget_exceeded; all calls share "
                        "one gov.root_request.id; total charged <= root budget") as r:
        first = stack.chat("uc30-orchestrator", prompt="plan the synthetic case research", max_tokens=1000)
        root_id = first.headers["x-gov-root-request-id"]
        root_tok = first.headers["x-gov-root-token"]
        hdr = {"x-gov-root-token": root_tok}
        task = {"prompt": "summarise the synthetic case file", "max_tokens": 2000, "delegate_chain": []}
        b = stack.a2a("uc30-orchestrator", "agent-b", task, headers=hdr)
        c = stack.a2a("uc30-orchestrator", "agent-c", task, headers=hdr)

        def agent_result(resp):
            art = resp.json()["result"]["task"]["artifacts"][0]["parts"][0]["text"]
            return json.loads(art)

        rb, rc = agent_result(b), agent_result(c)
        stack.flush()
        refused_trace = None
        reqs = []
        for _ in range(40):
            reqs = spans.requests_for_root(root_id)
            refused = [s for s in reqs if s["attrs"].get("gov.decision.reason") == "root_budget_exceeded"]
            if len(reqs) >= 5 and refused:
                refused_trace = refused[0]["trace_id"]
                break
            stack.flush()
            time.sleep(0.25)
        refused_attrs = next(s["attrs"] for s in reqs if s["trace_id"] == refused_trace) if refused_trace else {}
        ev = wait_evidence(stack, refused_trace, where=lambda e: e.get("gov.decision.reason") == "root_budget_exceeded") \
            if refused_trace else {}
        charged = int(stack.redis.get(f"root:{root_id}") or 0)
        allowed_tokens = sum(s["attrs"].get("gen_ai.usage.input_tokens", 0) + s["attrs"].get("gen_ai.usage.output_tokens", 0)
                             for s in reqs if s["attrs"].get("gov.decision") == "allow"
                             and s["attrs"].get("gen_ai.operation.name") == "chat")
        root_ids = {s["attrs"].get("gov.root_request.id") for s in reqs}

        r.check("orchestrator_call_allowed", first.status_code == 200)
        r.check("agent_b_model_call_allowed", rb["model_call"]["status"] == 200)
        r.check("agent_c_model_call_429", rc["model_call"]["status"] == 429
                and rc["model_call"]["reason"] == "root_budget_exceeded")
        r.check("refusal_span_present", refused_attrs.get("gov.decision") == "deny"
                and refused_attrs.get("gov.decision.stage") == "budget")
        r.check("single_root_request_id", root_ids == {root_id} and len(reqs) >= 5)
        r.check("total_charged_within_budget", charged <= 5000 and allowed_tokens <= 5000)
        r.observed(f"orchestrator {first.status_code}; agent-b model call {rb['model_call']['status']}; agent-c model "
                   f"call {rc['model_call']['status']} {rc['model_call']['reason']}; {len(reqs)} gateway requests share "
                   f"root {root_id[:8]}...; charged {charged} tokens (allowed usage {allowed_tokens}) of 5000")
        r.evidence(REQUIRED, refused_attrs, ev)
