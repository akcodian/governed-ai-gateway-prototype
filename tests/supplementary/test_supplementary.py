"""Supplementary checks: quarantine, hop limit, tampering, fail-open/closed, evidence chain, observation mode."""
from __future__ import annotations

import copy
import json
import shutil
import time

import pytest

from cp_compile.compile import write_snapshot
from telemetry.evidence_store.store import verify_chain
from tests.helpers import EVIDENCE_DIR, ROOT, evidence_records, redis_start, redis_stop, wait_evidence

BUILD = ROOT / "control-plane" / "build"


def _current_snapshot() -> dict:
    return json.loads((BUILD / "snapshot.json").read_text())


def test_tool_definition_change_quarantines(clean, spans, supplementary):
    stack = clean
    with supplementary("tool_quarantine", "changed tool definition -> deny tool_definition_changed; tool quarantined") as r:
        stack.http.post("http://localhost:8200/admin/mutate").raise_for_status()
        call = stack.mcp("uc30-orchestrator", "tools/call", {"name": "search_docs", "arguments": {"query": "x"}})
        listed = stack.mcp("uc30-orchestrator", "tools/list")
        stack.http.post("http://localhost:8200/admin/reset").raise_for_status()   # definition restored...
        again = stack.mcp("uc30-orchestrator", "tools/call", {"name": "search_docs", "arguments": {"query": "x"}})
        a = spans.request_span(stack, call.headers["x-trace-id"])["attrs"]
        stack.flush()
        q = evidence_records(kind="tool_quarantined")
        r.check("denied_403", call.status_code == 403)
        r.check("reason_tool_definition_changed", a.get("gov.decision.reason") == "tool_definition_changed")
        r.check("hidden_from_tools_list", listed.json()["result"]["tools"] == [])
        r.check("stays_quarantined_until_released", again.status_code == 403)
        r.check("quarantine_evidence", any(e["record"]["tool"] == "search_docs" for e in q))
        r.observed(f"call -> {call.status_code} {a.get('gov.decision.reason')}; tools/list -> "
                   f"{listed.json()['result']['tools']}; after restore still {again.status_code}")


def test_a2a_hop_limit(clean, spans, supplementary):
    stack = clean
    with supplementary("hop_limit", "delegation beyond hop limit 3 denied (hop_limit_exceeded)") as r:
        task = {"prompt": "short", "max_tokens": 8, "delegate_chain": ["agent-c", "agent-b", "agent-c"]}
        resp = stack.a2a("uc30-orchestrator", "agent-b", task)
        root_id = resp.headers["x-gov-root-request-id"]
        reqs = []
        for _ in range(40):
            stack.flush()
            reqs = spans.requests_for_root(root_id)
            if any(s["attrs"].get("gov.decision.reason") == "hop_limit_exceeded" for s in reqs):
                break
            time.sleep(0.25)
        denied = [s for s in reqs if s["attrs"].get("gov.decision.reason") == "hop_limit_exceeded"]
        hops = sorted(s["attrs"].get("gov.a2a.next_hop") for s in reqs if s["attrs"].get("gov.a2a.next_hop"))
        r.check("hop_4_denied", len(denied) == 1 and denied[0]["attrs"].get("gov.a2a.next_hop") == 4)
        r.check("hops_1_to_3_allowed", all(s["attrs"].get("gov.decision") == "allow" for s in reqs
                                           if (s["attrs"].get("gov.a2a.next_hop") or 0) in (1, 2, 3)))
        r.observed(f"delegation hops seen {hops}; denied at hop "
                   f"{denied[0]['attrs'].get('gov.a2a.next_hop') if denied else None}")


def test_tampered_snapshot_rejected(clean, supplementary):
    stack = clean
    with supplementary("snapshot_tamper", "bad signature rejected; gateway keeps serving last known good") as r:
        v0 = stack.health()["config_version"]
        tdir = BUILD / "test_tamper"
        tdir.mkdir(exist_ok=True)
        snap = _current_snapshot()
        snap["config_version"] = v0 + 10
        snap["use_cases"]["UC-17"]["state"] = "suspended"       # edit without re-signing
        (tdir / "snapshot.json").write_text(json.dumps(snap))
        shutil.copy(BUILD / "snapshot.sig", tdir / "snapshot.sig")
        resp = stack.reload("test_tamper")
        still = stack.chat("uc17-batch-runner")
        r.check("rejected_409_bad_signature", resp.status_code == 409 and resp.json()["reason"] == "bad_signature")
        r.check("version_unchanged", stack.health()["config_version"] == v0)
        r.check("still_serving", still.status_code == 200)
        # stale version: correctly signed but lower config_version
        old = _current_snapshot()
        old["config_version"] = v0 - 1
        write_snapshot(old, BUILD / "test_stale")
        stale = stack.reload("test_stale")
        r.check("stale_version_rejected", stale.status_code == 409 and stale.json()["reason"] == "stale_config_version")
        stack.flush()
        rejected = [e for e in evidence_records(kind="config_rejected")]
        r.check("rejection_evidence", {"bad_signature", "stale_config_version"} <= {e["record"]["reason"] for e in rejected})
        r.observed(f"tampered -> {resp.status_code} {resp.json().get('reason')}; stale -> {stale.status_code} "
                   f"{stale.json().get('reason')}; serving v{stack.health()['config_version']}; UC-17 -> {still.status_code}")


def test_tampered_pairing_signature(clean, spans, supplementary):
    stack = clean
    with supplementary("pairing_signature_tamper", "entry with bad pairing signature rejected at route time") as r:
        good = _current_snapshot()
        v0 = good["config_version"]
        bad = copy.deepcopy(good)
        bad["config_version"] = v0 + 1
        sig = bad["pairings"]["UC-17"][0]["approval"]["signature"]
        bad["pairings"]["UC-17"][0]["approval"]["signature"] = ("A" if sig[0] != "A" else "B") + sig[1:]
        write_snapshot(bad, BUILD / "test_pairing")
        loaded = stack.reload("test_pairing")
        resp = stack.chat("uc17-batch-runner")
        a = spans.request_span(stack, resp.headers["x-trace-id"])["attrs"]
        # restore: the good content under a higher version becomes the active build output
        good["config_version"] = v0 + 2
        write_snapshot(good, BUILD)
        restored = stack.reload()
        r.check("tampered_snapshot_loaded", loaded.status_code == 200)
        r.check("rank1_rejected_served_by_B", a.get("gen_ai.response.model") == "model-b" and a.get("gov.route.rank") == 2)
        r.check("reason_pairing_signature_invalid", "model-a:pairing_signature_invalid" in str(a.get("gov.route.skipped"))
                and a.get("gov.route.reason") == "pairing_signature_invalid")
        r.check("restored", restored.status_code == 200)
        r.observed(f"served by {a.get('gen_ai.response.model')} rank {a.get('gov.route.rank')} reason "
                   f"{a.get('gov.route.reason')}; restored v{restored.json().get('config_version')}")


def test_redis_down_fail_modes(clean, spans, supplementary):
    stack = clean
    with supplementary("redis_fail_modes", "Redis down: fail_mode closed refuses (UC-17), fail_mode open allows and logs (UC-01)") as r:
        redis_stop()
        try:
            open_resp = stack.chat("uc01-notebook")
            closed_resp = stack.chat("uc17-batch-runner")
        finally:
            redis_start()
            for _ in range(60):
                try:
                    if stack.redis.ping():
                        break
                except Exception:  # noqa: BLE001
                    time.sleep(0.5)
        oa = spans.request_span(stack, open_resp.headers["x-trace-id"])["attrs"]
        ca = spans.request_span(stack, closed_resp.headers["x-trace-id"])["attrs"]
        ev = wait_evidence(stack, open_resp.headers["x-trace-id"])
        r.check("open_allows", open_resp.status_code == 200 and oa.get("gov.budget.fail_mode") == "fail_open")
        r.check("open_logged", ev.get("budget_fail_mode", {}).get("mode") == "fail_open")
        r.check("closed_refuses_429", closed_resp.status_code == 429
                and ca.get("gov.decision.reason") == "budget_store_unavailable")
        r.observed(f"UC-01 (open) -> {open_resp.status_code} {oa.get('gov.budget.fail_mode')}; UC-17 (closed) -> "
                   f"{closed_resp.status_code} {ca.get('gov.decision.reason')}")


def test_evidence_chain_tamper(clean, tmp_path, supplementary):
    stack = clean
    with supplementary("evidence_tamper", "edit, deletion, or truncation of the evidence chain detected") as r:
        stack.chat("uc01-notebook")
        stack.flush()
        src = EVIDENCE_DIR / "gateway.jsonl"
        lines = src.read_bytes().splitlines(keepends=True)
        head = json.dumps({"count": len(lines), "last_hash": __import__("hashlib").sha256(lines[-1].rstrip(b"\n")).hexdigest()})

        def make(name, content: list[bytes]):
            p = tmp_path / f"{name}.jsonl"
            p.write_bytes(b"".join(content))
            (tmp_path / f"{name}.jsonl.head").write_text(head)
            return p

        ok, _ = verify_chain(make("intact", lines))
        edited = list(lines)
        rec = json.loads(edited[1])
        rec["record"]["gov.decision"] = "allow-edited"
        edited[1] = (json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode()
        e_ok, e_msg = verify_chain(make("edited", edited))
        d_ok, d_msg = verify_chain(make("deleted", lines[:1] + lines[2:]))
        t_ok, t_msg = verify_chain(make("truncated", lines[:-1]))
        r.check("intact_verifies", ok)
        r.check("edit_detected", not e_ok)
        r.check("deletion_detected", not d_ok)
        r.check("truncation_detected", not t_ok)
        r.observed(f"intact={ok}; edit -> {e_msg}; delete -> {d_msg}; truncate -> {t_msg}")


def test_observation_mode(clean, spans, supplementary):
    stack = clean
    with supplementary("observation_mode", "new deny rule in observation mode records would_deny and does not block") as r:
        resp = stack.chat("uc17-batch-runner", max_tokens=5000)
        a = spans.request_span(stack, resp.headers["x-trace-id"])["attrs"]
        ev = wait_evidence(stack, resp.headers["x-trace-id"])
        r.check("not_blocked", resp.status_code == 200 and a.get("gov.decision") == "allow")
        r.check("would_deny_recorded", a.get("gov.policy.would_deny") == "max_tokens_cap"
                and ev.get("would_deny") == ["max_tokens_cap"])
        r.observed(f"HTTP {resp.status_code} decision={a.get('gov.decision')} would_deny={a.get('gov.policy.would_deny')}")


@pytest.fixture(autouse=True, scope="module")
def _cleanup_build_dirs():
    yield
    for d in ("test_tamper", "test_stale", "test_pairing"):
        shutil.rmtree(BUILD / d, ignore_errors=True)
