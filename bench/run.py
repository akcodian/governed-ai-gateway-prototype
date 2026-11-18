"""Latency benchmark (spec section 8.3, measure 3).

* Baseline: request sent directly to the mock model server (model-a, 0 ms added delay).
* Gateway path: the identical request through the gateway on the allow path (UC-17 -> model-a, rank 1).
* 100 warm-up requests, then N = 1,000 measured requests, at concurrency 1 and 10.
* Per-stage p50/p95 from the gateway's spans for the measured requests.
* Decision latency for S3 (fallback) and S5 (skip expired), and for every scenario's decision type
  (results/scenario_latency.csv). Human approval wait is excluded (reported separately for S4a).
Outputs: results/latency.csv, results/stage_latency.csv, results/scenario_latency.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import statistics
import time

import httpx

from govlib import tokens
from tests.helpers import GATEWAY, MODEL_SERVER, RESULTS, SpanIndex, Stack, auth, words

BODY = {"messages": [{"role": "user", "content": "summarise the synthetic claim number 42 in one line"}], "max_tokens": 16}
STAGES = ["gateway.identify", "gateway.authorize", "gateway.budget", "gateway.route", "gateway.guard"]


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


async def run_load(make_request, n: int, concurrency: int) -> tuple[list[float], list[str], list[int]]:
    latencies: list[float] = []
    trace_ids: list[str] = []
    statuses: list[int] = []
    counter = iter(range(n))

    async def worker(client: httpx.AsyncClient):
        for _ in counter:
            t0 = time.perf_counter()
            r = await make_request(client)
            latencies.append((time.perf_counter() - t0) * 1000)
            statuses.append(r.status_code)
            if "x-trace-id" in r.headers:
                trace_ids.append(r.headers["x-trace-id"])

    limits = httpx.Limits(max_keepalive_connections=concurrency, max_connections=concurrency)
    async with httpx.AsyncClient(timeout=60, limits=limits) as client:
        await asyncio.gather(*(worker(client) for _ in range(concurrency)))
    return latencies, trace_ids, statuses


def direct_request():
    body = {**BODY, "model": "model-a"}

    async def f(client):
        return await client.post(f"{MODEL_SERVER}/v1/chat/completions", json=body)
    return f


def gateway_request(client_id: str, body: dict | None = None, path: str = "/v1/chat/completions"):
    token = tokens.issue_workload_token(client_id, ttl_s=7200)
    headers = {"authorization": f"Bearer {token}"}
    payload = body or BODY

    async def f(client):
        return await client.post(f"{GATEWAY}{path}", json=payload, headers=headers)
    return f


def stage_stats(index: SpanIndex, stack: Stack, trace_ids: list[str]) -> dict[str, list[float]]:
    stack.flush()
    wanted = set(trace_ids)
    deadline = time.time() + 60
    while time.time() < deadline:
        index.refresh()
        have = sum(1 for t in wanted if any(s["name"] == "gateway.request" for s in index.by_trace.get(t, [])))
        if have >= len(wanted):
            break
        time.sleep(0.5)
    per_stage: dict[str, list[float]] = {s: [] for s in STAGES + ["gateway.request", "gateway.upstream", "governance_total"]}
    for t in wanted:
        spans = index.by_trace.get(t, [])
        if not spans:
            continue
        sums: dict[str, float] = {}
        for s in spans:
            sums[s["name"]] = sums.get(s["name"], 0.0) + (s["end_ns"] - s["start_ns"]) / 1e6
        for name in per_stage:
            if name in sums:
                per_stage[name].append(sums[name])
        per_stage["governance_total"].append(sum(sums.get(s, 0.0) for s in STAGES))
    return per_stage


async def main_async(n: int, warmup: int, scen_n: int) -> None:
    RESULTS.mkdir(exist_ok=True)
    stack = Stack()
    stack.reset()
    index = SpanIndex()
    index.refresh()  # skip spans already on disk

    latency_rows, stage_rows = [], []
    summary: dict = {}
    for conc in (1, 10):
        await run_load(direct_request(), warmup, conc)
        await run_load(gateway_request("uc17-batch-runner"), warmup, conc)
        d_lat, _, d_st = await run_load(direct_request(), n, conc)
        g_lat, g_traces, g_st = await run_load(gateway_request("uc17-batch-runner"), n, conc)
        assert all(s == 200 for s in d_st + g_st), "non-200 responses during the benchmark"
        for path, lat in (("direct", d_lat), ("gateway", g_lat)):
            latency_rows.append({"path": path, "concurrency": conc, "n": len(lat), "p50_ms": pct(lat, 50),
                                 "p95_ms": pct(lat, 95), "mean_ms": statistics.mean(lat)})
        latency_rows.append({"path": "added_by_gateway (gateway - direct)", "concurrency": conc, "n": n,
                             "p50_ms": pct(g_lat, 50) - pct(d_lat, 50), "p95_ms": pct(g_lat, 95) - pct(d_lat, 95),
                             "mean_ms": statistics.mean(g_lat) - statistics.mean(d_lat)})
        stats = stage_stats(index, stack, g_traces)
        for stage, xs in stats.items():
            stage_rows.append({"concurrency": conc, "stage": stage, "n": len(xs), "p50_ms": pct(xs, 50),
                               "p95_ms": pct(xs, 95)})
        summary[conc] = {"direct_p50": pct(d_lat, 50), "gateway_p50": pct(g_lat, 50)}
        print(f"concurrency {conc}: direct p50 {pct(d_lat, 50):.2f} ms, gateway p50 {pct(g_lat, 50):.2f} ms")

    # S3 and S5 decision latency (concurrency 1)
    stack.reset()
    stack.set_model("model-a", fail=True)
    for _ in range(3):
        stack.chat("uc17-batch-runner")
    await run_load(gateway_request("uc17-batch-runner"), 20, 1)
    s3_lat, _, s3_st = await run_load(gateway_request("uc17-batch-runner"), scen_n, 1)
    stack.reset()
    await run_load(gateway_request("uc40-report-batch"), 20, 1)
    s5_lat, _, s5_st = await run_load(gateway_request("uc40-report-batch"), scen_n, 1)
    for path, lat in (("gateway_s3_fallback", s3_lat), ("gateway_s5_skip_expired", s5_lat)):
        latency_rows.append({"path": path, "concurrency": 1, "n": len(lat), "p50_ms": pct(lat, 50),
                             "p95_ms": pct(lat, 95), "mean_ms": statistics.mean(lat)})

    with (RESULTS / "latency.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["path", "concurrency", "n", "p50_ms", "p95_ms", "mean_ms"])
        w.writeheader()
        for row in latency_rows:
            w.writerow({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in row.items()})
    with (RESULTS / "stage_latency.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["concurrency", "stage", "n", "p50_ms", "p95_ms"])
        w.writeheader()
        for row in stage_rows:
            w.writerow({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in row.items()})

    await scenario_latency(stack, scen_n)
    print("wrote results/latency.csv, results/stage_latency.csv, results/scenario_latency.csv")


async def scenario_latency(stack: Stack, n: int) -> None:
    """Client-observed gateway latency of each scenario's decision (concurrency 1, n requests each)."""
    rows = []
    big = {"messages": [{"role": "user", "content": words(2100)}], "max_tokens": 64}
    over_root = {"messages": [{"role": "user", "content": "plan"}], "max_tokens": 6000}

    def mcp_call(tool):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {"case_id": "C1", "status": "x"}}}

    async def measure(name, decision, make, setup=None, expect=None):
        stack.reset()
        if setup:
            setup()
        await run_load(make, 10, 1)
        lat, _, st = await run_load(make, n, 1)
        ok = expect is None or all(s == expect for s in st)
        rows.append({"scenario": name, "decision": decision, "n": len(lat), "p50_ms": pct(lat, 50),
                     "p95_ms": pct(lat, 95), "status_ok": ok})

    def open_a(client):
        def _f():
            stack.set_model("model-a", fail=True)
            for _ in range(3):
                stack.chat(client)
        return _f

    await measure("S1", "allow", gateway_request("uc01-notebook"), expect=200)
    await measure("S2", "deny", gateway_request("uc17-batch-runner", {**BODY, "model": "model-c"}), expect=403)
    await measure("S3", "allow (fallback rank 2)", gateway_request("uc17-batch-runner"), open_a("uc17-batch-runner"), 200)
    await measure("S4a", "hold (time to HTTP 202)", gateway_request("uc17-batch-runner", big), open_a("uc17-batch-runner"), 202)
    await measure("S4b", "degrade", gateway_request("uc22-desk-app", big), open_a("uc22-desk-app"), 200)
    await measure("S5", "allow (skip expired rank 1)", gateway_request("uc40-report-batch"), expect=200)
    await measure("S6", "deny (tools/call update_case)", gateway_request("uc30-orchestrator", mcp_call("update_case"),
                                                                         "/mcp/mcp-docs"), expect=403)
    await measure("S7", "deny (root budget, 429)", gateway_request("uc30-orchestrator", over_root), expect=429)
    stack.reset()
    with (RESULTS / "scenario_latency.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["scenario", "decision", "n", "p50_ms", "p95_ms", "status_ok"])
        w.writeheader()
        for row in rows:
            w.writerow({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in row.items()})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=1000)
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--scenario-n", type=int, default=200)
    a = p.parse_args()
    asyncio.run(main_async(a.n, a.warmup, a.scenario_n))


if __name__ == "__main__":
    main()
