"""S4a/S4b: no approved fallback left (A's circuit open; request too large for B's context)."""
import csv

from approval_service.cli import SimulatedApprover
from tests.helpers import RESULTS, GATEWAY, auth, evidence_records, wait_evidence, words

BIG_PROMPT = words(2100)  # > model-b context_length (2048)

REQUIRED_HOLD = ["gov.use_case.id", "gov.root_request.id", "gov.decision", "gov.decision.reason", "gov.hold.id",
                 "gov.record.versions", "gov.config.version", "gov.policy.version", "route_skipped"]
REQUIRED_RESUME = ["gov.use_case.id", "gov.root_request.id", "gov.decision", "gov.route.reason", "gov.route.rank",
                   "gov.exception.id", "gov.approver", "gov.hold.id", "gov.record.versions", "gov.cost.amount",
                   "gen_ai.response.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
                   "gov.config.version", "gov.policy.version"]
REQUIRED_DEGRADE = ["gov.use_case.id", "gov.root_request.id", "gov.decision", "gov.decision.reason",
                    "gov.record.versions", "gov.config.version", "gov.policy.version", "route_skipped"]


def _open_circuit(stack, client_id):
    stack.set_model("model-a", fail=True)
    for _ in range(3):
        stack.chat(client_id)


def test_s4a_hold_then_exception(clean, spans, scenario):
    stack = clean
    with scenario("S4a", "HTTP 202 with hold_id; approver grants exception to C; completes on C; spans show hold "
                         "then allow with gov.route.reason=exception, gov.exception.id, gov.approver; approval wait "
                         "recorded separately") as r:
        _open_circuit(stack, "uc17-batch-runner")
        calls0 = stack.model_calls()
        held = stack.chat("uc17-batch-runner", prompt=BIG_PROMPT, max_tokens=64)
        calls1 = stack.model_calls()
        body = held.json()
        hold_id = body.get("hold_id")
        hold_attrs = spans.request_span(stack, held.headers["x-trace-id"])["attrs"]
        hold_ev = wait_evidence(stack, held.headers["x-trace-id"])

        result = SimulatedApprover("sim-approver-01", think_time_s=0.5).approve(hold_id, "model-c", "no approved fallback")
        calls2 = stack.model_calls()
        status = stack.http.get(f"{GATEWAY}/v1/holds/{hold_id}", headers=auth("uc17-batch-runner")).json()
        resume_trace = result["result"]["trace_id"]
        res_attrs = spans.request_span(stack, resume_trace)["attrs"]
        res_ev = wait_evidence(stack, resume_trace)
        approval_ev = [e for e in evidence_records(file="approval.jsonl") if e["record"].get("hold_id") == hold_id]

        r.check("http_202_with_hold_id", held.status_code == 202 and bool(hold_id))
        r.check("hold_span_decision_hold", hold_attrs.get("gov.decision") == "hold")
        r.check("no_model_called_while_holding", calls1 == calls0)
        r.check("completed_on_C", status["status"] == "completed"
                and result["result"]["body"]["gov"]["model"] == "model-c")
        r.check("resume_span_allow", res_attrs.get("gov.decision") == "allow")
        r.check("route_reason_exception", res_attrs.get("gov.route.reason") == "exception")
        r.check("exception_id_and_approver", res_attrs.get("gov.exception.id") == status["exception_id"]
                and res_attrs.get("gov.approver") == "sim-approver-01")
        r.check("same_root_request", res_attrs.get("gov.root_request.id") == hold_attrs.get("gov.root_request.id"))
        r.check("model_c_called_once", calls2.get("model-c", 0) - calls1.get("model-c", 0) == 1)
        r.check("approval_wait_recorded", (status.get("wait_ms") or 0) > 0
                and any(e["kind"] == "exception_granted" for e in approval_ev))
        r.observed(f"HTTP {held.status_code} hold_id={hold_id} -> approved (wait {status.get('wait_ms', 0):.0f} ms) -> "
                   f"{res_attrs.get('gov.decision')} on {res_attrs.get('gen_ai.response.model')} "
                   f"reason={res_attrs.get('gov.route.reason')} exception={res_attrs.get('gov.exception.id')} "
                   f"approver={res_attrs.get('gov.approver')}")
        hold_req = [k for k in REQUIRED_HOLD]
        res_req = [f"resume:{k}" for k in REQUIRED_RESUME]
        r.evidence(hold_req + res_req, hold_attrs, hold_ev,
                   {f"resume:{k}": v for k, v in {**res_attrs, **res_ev}.items()})

        RESULTS.mkdir(exist_ok=True)
        with (RESULTS / "approval_wait.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["scenario", "hold_id", "approver", "approval_wait_ms", "note"])
            w.writerow(["S4a", hold_id, "sim-approver-01", f"{status.get('wait_ms', 0):.1f}",
                        "simulated approver with 0.5 s think time; excluded from gateway latency"])


def test_s4b_interactive_degrades(clean, spans, scenario):
    stack = clean
    with scenario("S4b", "HTTP 200 with degraded=true; gov.decision=degrade; no unapproved model called") as r:
        _open_circuit(stack, "uc22-desk-app")
        calls0 = stack.model_calls()
        resp = stack.chat("uc22-desk-app", prompt=BIG_PROMPT, max_tokens=64)
        calls1 = stack.model_calls()
        a = spans.request_span(stack, resp.headers["x-trace-id"])["attrs"]
        ev = wait_evidence(stack, resp.headers["x-trace-id"])
        r.check("http_200", resp.status_code == 200)
        r.check("degraded_true", resp.json().get("degraded") is True)
        r.check("decision_degrade", a.get("gov.decision") == "degrade")
        r.check("no_model_called", calls1 == calls0)
        r.observed(f"HTTP {resp.status_code} degraded={resp.json().get('degraded')} decision={a.get('gov.decision')} "
                   f"reason={a.get('gov.decision.reason')}; models called during request: "
                   f"{ {k: calls1.get(k, 0) - calls0.get(k, 0) for k in calls1 if calls1.get(k, 0) != calls0.get(k, 0)} }")
        r.evidence(REQUIRED_DEGRADE, a, ev)
