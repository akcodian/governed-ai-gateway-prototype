"""Unit tests that need no running stack (run in CI)."""
from __future__ import annotations

import copy
import json
import re

import pytest
import yaml

from cp_compile import compile as cpc
from gateway.circuit import CircuitBreakers
from gateway.stages import guard
from gateway.stages.route import Features, RoutePlan, capability_ok
from govlib import keys, tokens
from govlib.canonical import pairing_signing_payload, tool_definition_hash
from govlib.tokencount import count_messages
from telemetry.evidence_store.store import EvidenceStore, verify_chain


def test_token_count_rule():
    assert count_messages([{"role": "user", "content": "a b  c"}, {"role": "system", "content": "d"}]) == 4


def test_guard_redacts_and_counts_without_values():
    msgs, counts = guard.redact_messages([{"role": "user", "content": "pay ACCT-1234-5678-9012 and NID-123-45-6789"}])
    assert "ACCT-" not in msgs[0]["content"] and "NID-" not in msgs[0]["content"]
    assert counts == {"account_number": 1, "national_id": 1}


def test_evidence_chain_detects_edit_and_truncation(tmp_path):
    store = EvidenceStore(tmp_path / "e.jsonl")
    for i in range(5):
        store.append("decision", {"i": i})
    assert verify_chain(tmp_path / "e.jsonl")[0]
    lines = (tmp_path / "e.jsonl").read_text().splitlines()
    (tmp_path / "e.jsonl").write_text("\n".join(lines[:2] + [lines[2].replace('"i":2', '"i":9')] + lines[3:]) + "\n")
    assert not verify_chain(tmp_path / "e.jsonl")[0]
    (tmp_path / "e.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    assert not verify_chain(tmp_path / "e.jsonl")[0]


def test_circuit_breaker_opens_and_half_opens():
    cb = CircuitBreakers(threshold=3, reset_seconds=0.0)
    for _ in range(3):
        cb.record_failure("m")
    assert cb.state("m") == "half_open"  # reset 0 s -> immediately half-open
    cb2 = CircuitBreakers(threshold=3, reset_seconds=60)
    for _ in range(3):
        cb2.record_failure("m")
    assert not cb2.available("m")
    cb2.record_success("m")
    assert cb2.available("m")


def test_capability_check():
    model = {"capabilities": {"context_length": 2048, "tool_calling": False, "structured_output": True}}
    assert capability_ok(model, Features(1000, 64, False, False))
    assert not capability_ok(model, Features(2100, 64, False, False))
    assert not capability_ok(model, Features(10, 10, True, False))


def test_route_reason_mapping():
    p = RoutePlan(primary_model="model-a")
    p.skip("model-a", "model_unhealthy")
    assert p.route_reason(2) == "primary_unhealthy"
    q = RoutePlan(primary_model="model-d")
    q.skip("model-d", "governance_record_expired")
    assert q.route_reason(2) == "governance_record_expired"
    assert q.route_reason(1) == "primary"


def test_pairing_signature_roundtrip():
    keys.generate_all()
    entry = {"model": "model-a", "rank": 1, "data_classes": ["internal"], "regions": ["eu-west"],
             "evaluation_ref": "E", "expires_at": "2030-01-01T00:00:00Z", "approval": {"approved_by": "x"}}
    sig = keys.sign_b64("approving_function", pairing_signing_payload("UC-1", entry))
    pub = keys.load_public("approving_function")
    assert keys.verify_b64(pub, pairing_signing_payload("UC-1", entry), sig)
    tampered = {**entry, "rank": 2}
    assert not keys.verify_b64(pub, pairing_signing_payload("UC-1", tampered), sig)


def test_tokens_roundtrip_and_audience():
    keys.generate_all()
    t = tokens.issue_downstream_token(audience="a2a:agent-b", use_case_id="UC-30", root_request_id="r", root_budget_tokens=5000,
                                      hop=1, scope="s", actor="x")
    assert tokens.verify_downstream_token(t, "a2a:agent-b")["hop"] == 1
    with pytest.raises(Exception):
        tokens.verify_downstream_token(t, "a2a:agent-c")


def test_tool_hash_changes_with_description():
    assert tool_definition_hash("t", "a", {}) != tool_definition_hash("t", "b", {})


def _docs():
    return cpc.load_sources()


def test_control_plane_sources_valid():
    d = _docs()
    cpc.check_references(d)
    cpc.check_promotion(d)
    b = cpc.check_budgets(d)
    assert b["paths"]["UC-17"] == ["UC-17", "team-claims", "bu-operations", "org"]


def test_promotion_rule_rejects_missing_evaluation():
    d = copy.deepcopy(_docs())
    for m in d["models.yaml"]["models"]:
        if m["id"] == "model-b":
            m["governance_record"]["evaluations"] = [e for e in m["governance_record"]["evaluations"]
                                                     if e["use_case"] != "UC-17"]
    with pytest.raises(cpc.CompileError, match="no evaluation evidence"):
        cpc.check_promotion(d)


def test_promotion_rule_rejects_missing_owner_and_review():
    d = copy.deepcopy(_docs())
    uc = next(u for u in d["use_cases.yaml"]["use_cases"] if u["id"] == "UC-22")
    uc["owner"], uc["risk_review"] = None, None
    with pytest.raises(cpc.CompileError) as e:
        cpc.check_promotion(d)
    assert "owner" in str(e.value) and "risk review" in str(e.value)


def test_expired_agreement_warning():
    import datetime as dt
    warns = cpc.governance_warnings(_docs(), dt.datetime(2026, 11, 18, tzinfo=dt.timezone.utc))
    assert any(w["subject"] == "model-d" and w["code"] == "governance_record_expired" for w in warns)


def test_snapshot_signature_detects_tamper(tmp_path):
    keys.generate_all()
    snap = {"config_version": 1, "x": [1, 2]}
    cpc.write_snapshot(snap, tmp_path)
    from govlib.canonical import canonical_json
    pub = keys.load_public("control_plane")
    loaded = json.loads((tmp_path / "snapshot.json").read_text())
    sig = (tmp_path / "snapshot.sig").read_text()
    assert keys.verify_b64(pub, canonical_json(loaded), sig)
    loaded["x"].append(3)
    assert not keys.verify_b64(pub, canonical_json(loaded), sig)


def test_yaml_has_no_real_secrets():
    for f in ("models.yaml", "use_cases.yaml", "pairings.yaml"):
        text = (cpc.CP / f).read_text()
        assert not re.search(r"\bsk-[A-Za-z0-9]{20,}", text) and "BEGIN PRIVATE KEY" not in text
        yaml.safe_load(text)
