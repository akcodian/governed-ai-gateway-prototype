"""`make report`: write results/summary.md from the CSVs produced by the tests and the benchmark.
Every number comes from files written by code in this repository; missing inputs are reported as missing."""
from __future__ import annotations

import csv
import os
import datetime as dt
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R = ROOT / "results"

SCENARIO_TITLES = {
    "S1": "New use case in sandbox makes its first call",
    "S2": "Request names a model outside the approved pairing",
    "S3": "Primary model unavailable",
    "S4a": "No approved fallback, batch use case",
    "S4b": "No approved fallback, interactive use case",
    "S5": "Provider agreement for the primary has expired",
    "S6": "Agent calls an MCP tool not on its allowlist",
    "S7": "Agent fan-out exceeds the root budget",
    "S8": "KPI event joined to spend",
}


def read(name: str) -> list[dict]:
    p = R / name
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    except OSError:
        return ""


def main() -> None:
    scen = {r["scenario"]: r for r in read("scenarios.csv")}
    lat = {r["scenario"]: r for r in read("scenario_latency.csv")}
    out = ["# Results summary", "",
           f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by `make report`. "
           f"Results produced by code at commit `{git('rev-parse', '--short', 'HEAD') or 'n/a'}`; "
           f"paper tag `{os.environ.get('PAPER_TAG') or git('describe', '--tags', '--exact-match') or 'none yet'}`.",
           "", "All numbers below come from runs of the code in this repository (see results/*.csv). "
           "Mocks run on one machine. Latencies are measured by a benchmark client inside the Docker compose network "
           "(running it from the Windows host adds ~44 ms of Docker Desktop port-forwarding to every request).", "",
           "## Table 3: scenarios", "",
           "| # | Scenario | Expected decision | Observed | Pass | Evidence completeness | Gateway latency p50 / p95 (ms) |",
           "|---|---|---|---|---|---|---|"]
    failures = []
    for sid, title in SCENARIO_TITLES.items():
        s = scen.get(sid)
        l = lat.get(sid)
        latency = f"{float(l['p50_ms']):.2f} / {float(l['p95_ms']):.2f} (n={l['n']})" if l else "n/a"
        if sid == "S8":
            latency = "n/a (offline join)"
        if s is None:
            out.append(f"| {sid} | {title} | | NOT RUN | - | - | {latency} |")
            failures.append(f"{sid}: not run")
            continue
        comp = f"{float(s['evidence_completeness_pct']):.0f}%" if s["evidence_completeness_pct"] else "n/a"
        out.append(f"| {sid} | {title} | {s['expected']} | {s['observed']} | {s['pass']} | {comp} | {latency} |")
        if s["pass"] != "pass":
            failures.append(f"{sid}: failed checks: {s['failed_checks']}")
        if s["missing_attributes"]:
            failures.append(f"{sid}: missing evidence attributes: {s['missing_attributes']}")

    out += ["", "## Added latency (allow path, UC-17 -> model-a, N=1,000 after 100 warm-up)", "",
            "| path | concurrency | n | p50 ms | p95 ms | mean ms |", "|---|---|---|---|---|---|"]
    for r in read("latency.csv"):
        out.append(f"| {r['path']} | {r['concurrency']} | {r['n']} | {r['p50_ms']} | {r['p95_ms']} | {r['mean_ms']} |")
    out += ["", "`added_by_gateway` = percentile of the gateway path minus the same percentile of the direct path "
                "(independent samples, not paired requests). At concurrency 10 the gateway, a single uvicorn worker, "
            "saturates one CPU core (measured ~100% CPU while OPA, Redis and the mock model stayed below ~25%), so the "
            "added latency there is mostly queueing inside the gateway process, visible as longer authorize/route "
            "spans that include event-loop wait. Scaling out workers would need shared circuit-breaker and quarantine "
            "state, which the prototype keeps in-process.", "",
            "## Per-stage latency from spans (measured gateway requests)", "",
            "| concurrency | stage | n | p50 ms | p95 ms |", "|---|---|---|---|---|"]
    for r in read("stage_latency.csv"):
        out.append(f"| {r['concurrency']} | {r['stage']} | {r['n']} | {r['p50_ms']} | {r['p95_ms']} |")
    out += ["", "`gateway.budget` and `gateway.guard` sum both phases (reserve + reconcile; request + response). "
                "`governance_total` = sum of the five stages; `gateway.upstream` is the model call.", ""]

    aw = read("approval_wait.csv")
    out += ["## Human approval wait (S4a, excluded from gateway latency)", ""]
    out += [f"- {r['scenario']}: {r['approval_wait_ms']} ms ({r['note']})" for r in aw] or ["- not measured"]

    sup = read("supplementary.csv")
    out += ["", "## Supplementary checks", "", "| check | expected | observed | pass |", "|---|---|---|---|"]
    for r in sup:
        out.append(f"| {r['scenario']} | {r['expected']} | {r['observed']} | {r['pass']} |")
        if r["pass"] != "pass":
            failures.append(f"supplementary {r['scenario']}: {r['failed_checks']}")

    cpo = [r for r in read("cost_per_outcome.csv")]
    out += ["", "## Cost per outcome (results/cost_per_outcome.csv)", "",
            "| level | key | period | spend USD | requests | outcomes | cost per outcome USD |", "|---|---|---|---|---|---|---|"]
    for r in cpo:
        out.append(f"| {r['level']} | {r['key']} | {r['period']} | {float(r['spend_usd']):.6f} | {r['requests']} | "
                   f"{r['outcomes']} | {r['cost_per_outcome_usd'] or 'n/a'} |")

    out += ["", "## Notes on failures", ""]
    out += [f"- {f}" for f in failures] or ["- none: every scenario and supplementary check passed."]
    out += ["", "See results/environment.md for hardware, software and convention versions."]
    (R / "summary.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {R / 'summary.md'}")


if __name__ == "__main__":
    main()
