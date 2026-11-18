"""Business KPI emitter (R7).

Posts KPI events {use_case_id, kpi_name, kpi_value, kpi_unit, timestamp} to the OpenTelemetry
Collector as OTLP/HTTP JSON log records (event name `gov.kpi`). The collector's file exporter
writes them next to the spans, and the DuckDB warehouse joins the two.

Usage: python -m mocks.kpi_emitter.emit --use-case UC-17 --kpi claims_triaged --count 5
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import time

import httpx

COLLECTOR = os.environ.get("OTEL_COLLECTOR_HTTP", "http://localhost:4318")


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def kpi_event(use_case_id: str, kpi_name: str, kpi_value: float = 1.0, kpi_unit: str = "outcome",
              timestamp: dt.datetime | None = None) -> dict:
    ts = timestamp or dt.datetime.now(dt.timezone.utc)
    return {"use_case_id": use_case_id, "kpi_name": kpi_name, "kpi_value": float(kpi_value), "kpi_unit": kpi_unit,
            "timestamp": ts.isoformat()}


def post_events(events: list[dict], collector: str = COLLECTOR) -> None:
    records = []
    for e in events:
        ts_ns = int(dt.datetime.fromisoformat(e["timestamp"]).timestamp() * 1e9)
        records.append({
            "timeUnixNano": str(ts_ns),
            "observedTimeUnixNano": str(time.time_ns()),
            "severityText": "INFO",
            "eventName": "gov.kpi",
            "body": {"stringValue": "kpi"},
            "attributes": [_attr("event.name", "gov.kpi"), _attr("gov.use_case.id", e["use_case_id"]),
                           _attr("gov.kpi.name", e["kpi_name"]), _attr("gov.kpi.value", float(e["kpi_value"])),
                           _attr("gov.kpi.unit", e["kpi_unit"]), _attr("gov.kpi.timestamp", e["timestamp"])],
        })
    payload = {"resourceLogs": [{
        "resource": {"attributes": [_attr("service.name", "kpi-emitter")]},
        "scopeLogs": [{"scope": {"name": "kpi-emitter"}, "logRecords": records}],
    }]}
    r = httpx.post(f"{collector}/v1/logs", json=payload, timeout=10)
    r.raise_for_status()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--use-case", required=True)
    p.add_argument("--kpi", default="outcome")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--value", type=float, default=1.0)
    p.add_argument("--unit", default="outcome")
    a = p.parse_args()
    post_events([kpi_event(a.use_case, a.kpi, a.value, a.unit) for _ in range(a.count)])
    print(f"posted {a.count} KPI events for {a.use_case}")


if __name__ == "__main__":
    main()
