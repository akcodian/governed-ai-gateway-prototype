"""Load collector output and KPI events into DuckDB and compute cost per outcome (R7).

Usage: python -m telemetry.warehouse.build [--out-dir telemetry/out] [--csv results/cost_per_outcome.csv]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import duckdb

from telemetry.otlp import iter_logs, iter_spans

ROOT = Path(__file__).resolve().parents[2]
SQL = Path(__file__).with_name("cost_per_outcome.sql")


def build(out_dir: Path, csv_path: Path, db_path: Path | None = None) -> list[dict]:
    con = duckdb.connect(str(db_path) if db_path else ":memory:")
    con.execute("CREATE TABLE spans_flat (trace_id VARCHAR, name VARCHAR, ts TIMESTAMP, use_case_id VARCHAR, "
                "business_unit VARCHAR, team VARCHAR, cost DOUBLE, decision VARCHAR, attrs JSON)")
    rows = []
    for s in iter_spans(out_dir / "spans.jsonl"):
        if s["name"] != "gateway.request":
            continue
        a = s["attrs"]
        rows.append((s["trace_id"], s["name"], dt.datetime.fromtimestamp(s["start_ns"] / 1e9, dt.timezone.utc).replace(tzinfo=None),
                     a.get("gov.use_case.id"), a.get("gov.business_unit"), a.get("gov.team"),
                     a.get("gov.cost.amount"), a.get("gov.decision"), json.dumps(a)))
    if rows:
        con.executemany("INSERT INTO spans_flat VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    con.execute("CREATE TABLE kpi_events (use_case_id VARCHAR, kpi_name VARCHAR, kpi_value DOUBLE, kpi_unit VARCHAR, "
                "ts TIMESTAMP)")
    krows = []
    for lg in iter_logs(out_dir / "logs.jsonl"):
        a = lg["attrs"]
        if a.get("event.name") != "gov.kpi" and lg.get("event_name") != "gov.kpi":
            continue
        krows.append((a.get("gov.use_case.id"), a.get("gov.kpi.name"), float(a.get("gov.kpi.value", 1.0)),
                      a.get("gov.kpi.unit"),
                      dt.datetime.fromtimestamp(lg["time_ns"] / 1e9, dt.timezone.utc).replace(tzinfo=None)))
    if krows:
        con.executemany("INSERT INTO kpi_events VALUES (?, ?, ?, ?, ?)", krows)

    import yaml
    ucs = yaml.safe_load((ROOT / "control-plane" / "use_cases.yaml").read_text())["use_cases"]
    con.execute("CREATE TABLE use_cases (use_case_id VARCHAR, business_unit VARCHAR, team VARCHAR)")
    con.executemany("INSERT INTO use_cases VALUES (?, ?, ?)", [(u["id"], u["business_unit"], u["team"]) for u in ucs])

    con.execute(SQL.read_text())
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY cost_per_outcome TO '{csv_path.as_posix()}' (HEADER, DELIMITER ',')")
    cols = [d[0] for d in con.execute("SELECT * FROM cost_per_outcome").description]
    result = [dict(zip(cols, r)) for r in con.execute("SELECT * FROM cost_per_outcome").fetchall()]
    con.close()
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default=str(ROOT / "telemetry" / "out"))
    p.add_argument("--csv", default=str(ROOT / "results" / "cost_per_outcome.csv"))
    a = p.parse_args()
    rows = build(Path(a.out_dir), Path(a.csv))
    print(f"wrote {len(rows)} rows -> {a.csv}")


if __name__ == "__main__":
    main()
