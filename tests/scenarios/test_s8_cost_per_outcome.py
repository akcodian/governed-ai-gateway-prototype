"""S8: KPI events joined to spend -> cost per outcome."""
import csv
import datetime as dt
import time

from mocks.kpi_emitter.emit import kpi_event, post_events
from telemetry.otlp import iter_logs, iter_spans
from telemetry.warehouse.build import build
from tests.helpers import LOGS_FILE, RESULTS, ROOT, SPANS_FILE


def _independent(period: str) -> tuple[float, int]:
    spend = sum(s["attrs"].get("gov.cost.amount", 0.0) for s in iter_spans(SPANS_FILE)
                if s["name"] == "gateway.request" and s["attrs"].get("gov.use_case.id") == "UC-17"
                and s["attrs"].get("gov.cost.amount") is not None
                and dt.datetime.fromtimestamp(s["start_ns"] / 1e9, dt.timezone.utc).strftime("%Y-%m") == period)
    outcomes = sum(1 for lg in iter_logs(LOGS_FILE) if lg["attrs"].get("event.name") == "gov.kpi"
                   and lg["attrs"].get("gov.use_case.id") == "UC-17"
                   and dt.datetime.fromtimestamp(lg["time_ns"] / 1e9, dt.timezone.utc).strftime("%Y-%m") == period)
    return spend, outcomes


def test_s8_cost_per_outcome(clean, spans, scenario):
    stack = clean
    with scenario("S8", "cost_per_outcome.csv row for UC-17 = total gov.cost.amount / count of outcomes "
                        "(computed independently)") as r:
        traffic = [stack.chat("uc17-batch-runner", prompt=f"triage synthetic claim {i}", max_tokens=40) for i in range(10)]
        post_events([kpi_event("UC-17", "claims_triaged") for _ in range(4)])
        # wait until spans and KPI logs are on disk and stable
        last_ids = [t.headers["x-trace-id"] for t in traffic]
        spans.wait_trace(stack, last_ids[-1])
        period = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        prev = None
        for _ in range(40):
            cur = _independent(period)
            if cur == prev and cur[1] >= 4:
                break
            prev = cur
            time.sleep(0.5)
        spend, outcomes = _independent(period)
        csv_path = RESULTS / "cost_per_outcome.csv"
        build(ROOT / "telemetry" / "out", csv_path)
        with csv_path.open() as f:
            rows = [x for x in csv.DictReader(f) if x["level"] == "use_case" and x["key"] == "UC-17"
                    and x["period"] == period]
        row = rows[0] if rows else {}
        expected = spend / outcomes if outcomes else None
        got = float(row["cost_per_outcome_usd"]) if row.get("cost_per_outcome_usd") else None
        r.check("all_traffic_allowed", all(t.status_code == 200 for t in traffic))
        r.check("row_present", bool(row))
        r.check("cost_per_outcome_matches", expected is not None and got is not None and abs(got - expected) < 1e-9)
        r.observed(f"UC-17 {period}: spend {spend:.6f} USD / {outcomes} outcomes = {expected if expected else 'n/a'}; "
                   f"warehouse row = {got}")
        r.evidence(["use_case_id", "business_unit", "team", "period", "spend_usd", "outcomes", "cost_per_outcome_usd"],
                   {"use_case_id": row.get("key"), **{k: row.get(k) for k in
                                                      ("business_unit", "team", "period", "spend_usd", "outcomes",
                                                       "cost_per_outcome_usd")}})
