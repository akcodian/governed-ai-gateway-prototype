"""Readers for the OpenTelemetry Collector file exporter output (OTLP JSON, one batch per line)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def _value(v: dict):
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def attrs(lst: list | None) -> dict:
    return {a["key"]: _value(a.get("value", {})) for a in lst or []}


def _lines(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # partially written last line


def iter_spans(path: str | Path) -> Iterator[dict]:
    for batch in _lines(Path(path)):
        for rs in batch.get("resourceSpans", []):
            res = attrs(rs.get("resource", {}).get("attributes"))
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    yield {"trace_id": sp.get("traceId", "").lower(), "span_id": sp.get("spanId", "").lower(),
                           "parent_id": (sp.get("parentSpanId") or "").lower(), "name": sp.get("name"),
                           "start_ns": int(sp.get("startTimeUnixNano", 0)), "end_ns": int(sp.get("endTimeUnixNano", 0)),
                           "service": res.get("service.name"), "attrs": attrs(sp.get("attributes")),
                           "links": [{"trace_id": ln.get("traceId", "").lower(), "attrs": attrs(ln.get("attributes"))}
                                     for ln in sp.get("links", [])]}


def iter_logs(path: str | Path) -> Iterator[dict]:
    for batch in _lines(Path(path)):
        for rl in batch.get("resourceLogs", []):
            res = attrs(rl.get("resource", {}).get("attributes"))
            for sl in rl.get("scopeLogs", []):
                for lr in sl.get("logRecords", []):
                    yield {"time_ns": int(lr.get("timeUnixNano", 0)), "service": res.get("service.name"),
                           "event_name": lr.get("eventName"), "attrs": attrs(lr.get("attributes"))}
