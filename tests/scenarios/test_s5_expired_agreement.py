"""S5: the provider agreement for the primary model has expired."""
import json

from tests.helpers import ROOT, evidence_records, wait_evidence

REQUIRED = ["gov.use_case.id", "gov.root_request.id", "gov.decision", "gov.route.rank", "gov.route.reason",
            "gov.record.versions", "gov.config.version", "gov.policy.version", "gov.cost.amount",
            "gen_ai.response.model", "route_skipped"]


def test_s5_expired_agreement(clean, spans, scenario):
    stack = clean
    with scenario("S5", "served by model A; reason governance_record_expired; compiler warning logged") as r:
        d_before = stack.model_calls().get("model-d", 0)
        resp = stack.chat("uc40-report-batch")
        d_after = stack.model_calls().get("model-d", 0)
        a = spans.request_span(stack, resp.headers["x-trace-id"])["attrs"]
        ev = wait_evidence(stack, resp.headers["x-trace-id"])
        warnings = json.loads((ROOT / "control-plane" / "build" / "compile_warnings.json").read_text())
        cp_ev = evidence_records(kind="compile_warning", file="control_plane.jsonl")
        r.check("http_200", resp.status_code == 200)
        r.check("served_by_A", a.get("gen_ai.response.model") == "model-a")
        r.check("rank_2", a.get("gov.route.rank") == 2)
        r.check("reason_governance_record_expired", a.get("gov.route.reason") == "governance_record_expired")
        r.check("model_d_not_called", d_after == d_before)
        r.check("compiler_warning_logged",
                any(w["subject"] == "model-d" and w["code"] == "governance_record_expired" for w in warnings)
                and any(e["record"]["subject"] == "model-d" for e in cp_ev))
        r.observed(f"HTTP {resp.status_code} model={a.get('gen_ai.response.model')} rank={a.get('gov.route.rank')} "
                   f"reason={a.get('gov.route.reason')} versions={a.get('gov.record.versions')}; "
                   f"compile warning for model-d present={r.row.checks['compiler_warning_logged']}")
        r.evidence(REQUIRED, a, ev)
