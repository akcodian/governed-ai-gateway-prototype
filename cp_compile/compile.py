"""Control-plane compiler (R6, R8): `make compile`.

1. Validate every YAML file against its JSON Schema and cross-references.
2. Run `opa test` on the Rego policies.
3. Enforce the promotion rule for production use cases.
4. Validate the budget tree.
5. Warn about governance records expired or expiring within 30 days.
6. Resolve everything into one snapshot.json with a monotonically increasing config_version.
7. Sign it (Ed25519, control-plane key) -> snapshot.sig.
8. Write decision records for use-case state changes and the compile itself to the evidence chain.

Exit code 0 on success, 1 on any error. Warnings do not fail the build.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import yaml

from cp_compile.approve_pairings import OUT as APPROVALS_PATH
from cp_compile.approve_pairings import entry_key
from cp_compile.schemas import SCHEMAS
from govlib import keys
from govlib.canonical import canonical_json, pairing_signing_payload, sha256_hex
from telemetry.evidence_store.store import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "control-plane"
BUILD = CP / "build"
POLICIES = CP / "policies"
WARN_DAYS = 30


class CompileError(Exception):
    pass


def opa_bin() -> str:
    if os.environ.get("OPA_BIN"):
        return os.environ["OPA_BIN"]
    local = ROOT / ".tools" / ("opa.exe" if platform.system() == "Windows" else "opa")
    if local.exists():
        return str(local)
    found = shutil.which("opa")
    if not found:
        raise CompileError("OPA CLI not found: run `make setup` (downloads the pinned OPA into .tools/)")
    return found


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_sources() -> dict[str, dict]:
    docs = {}
    errors = []
    for fname, schema in SCHEMAS.items():
        path = CP / fname
        doc = yaml.safe_load(path.read_text())
        v = jsonschema.Draft202012Validator(schema)
        for err in sorted(v.iter_errors(doc), key=lambda e: list(e.path)):
            errors.append(f"{fname}: {'/'.join(map(str, err.path)) or '<root>'}: {err.message}")
        docs[fname] = doc
    if errors:
        raise CompileError("schema validation failed:\n  " + "\n  ".join(errors))
    return docs


def check_references(d: dict) -> None:
    errors = []
    ucs = {u["id"]: u for u in d["use_cases.yaml"]["use_cases"]}
    models = {m["id"] for m in d["models.yaml"]["models"]}
    tools = {s["id"]: {t["name"] for t in s["tools"]} for s in d["tools.yaml"]["mcp_servers"]}
    agents = {a["id"] for a in d["agents.yaml"]["agents"]}
    seen_workloads: dict[str, str] = {}
    for u in ucs.values():
        for w in u["workloads"]:
            if w in seen_workloads:
                errors.append(f"workload {w} is assigned to both {seen_workloads[w]} and {u['id']}")
            seen_workloads[w] = u["id"]
    for uc, entries in d["pairings.yaml"]["pairings"].items():
        if uc not in ucs:
            errors.append(f"pairings: unknown use case {uc}")
        ranks = [e["rank"] for e in entries]
        if len(set(ranks)) != len(ranks):
            errors.append(f"pairings: duplicate rank for {uc}")
        for e in entries:
            if e["model"] not in models:
                errors.append(f"pairings: {uc} references unknown model {e['model']}")
    for uc, al in d["allowlists.yaml"]["allowlists"].items():
        if uc not in ucs:
            errors.append(f"allowlists: unknown use case {uc}")
        for server, names in (al.get("tools") or {}).items():
            if server not in tools:
                errors.append(f"allowlists: {uc} references unknown MCP server {server}")
            for n in names:
                if n not in tools.get(server, set()):
                    errors.append(f"allowlists: {uc} references unknown tool {server}/{n}")
        for a in al.get("agents") or []:
            if a not in agents:
                errors.append(f"allowlists: {uc} references unknown agent {a}")
    nodes = {n["id"] for n in d["budgets.yaml"]["nodes"]}
    for u in ucs.values():
        for nid in (u["id"], u["team"], u["business_unit"]):
            if nid not in nodes:
                errors.append(f"budgets: no budget node {nid} for {u['id']}")
    if errors:
        raise CompileError("reference check failed:\n  " + "\n  ".join(errors))


def check_promotion(d: dict) -> None:
    """A use case cannot be production unless every paired model has evaluation evidence for it,
    the risk review for its tier is present, and an owner is set."""
    errors = []
    models = {m["id"]: m for m in d["models.yaml"]["models"]}
    pairings = d["pairings.yaml"]["pairings"]
    for u in d["use_cases.yaml"]["use_cases"]:
        if u["state"] != "production":
            continue
        if not u.get("owner"):
            errors.append(f"{u['id']}: production requires an owner")
        rr = u.get("risk_review")
        if not rr or rr.get("tier") != u["risk_tier"]:
            errors.append(f"{u['id']}: production requires a risk review for tier {u['risk_tier']}")
        if not u.get("approved_by"):
            errors.append(f"{u['id']}: production requires approved_by")
        entries = pairings.get(u["id"], [])
        if not entries:
            errors.append(f"{u['id']}: production requires at least one paired model")
        for e in entries:
            evals = models[e["model"]]["governance_record"]["evaluations"]
            if not any(ev["use_case"] == u["id"] for ev in evals):
                errors.append(f"{u['id']}: paired model {e['model']} has no evaluation evidence for this use case")
    if errors:
        raise CompileError("promotion rule violated:\n  " + "\n  ".join(errors))


def check_budgets(d: dict) -> dict:
    nodes = {n["id"]: n for n in d["budgets.yaml"]["nodes"]}
    errors = []
    roots = [n for n in nodes.values() if n["parent"] is None]
    if len(roots) != 1 or roots[0]["level"] != "org":
        errors.append("budget tree must have exactly one org root")
    order = {"org": 0, "business_unit": 1, "team": 2, "use_case": 3}
    for n in nodes.values():
        if not (n["floor_usd"] <= n["monthly_usd"] <= n["ceiling_usd"]):
            errors.append(f"budget {n['id']}: need floor <= monthly <= ceiling")
        if n["parent"]:
            p = nodes.get(n["parent"])
            if not p:
                errors.append(f"budget {n['id']}: unknown parent {n['parent']}")
            elif order[p["level"]] != order[n["level"]] - 1:
                errors.append(f"budget {n['id']}: level {n['level']} cannot sit under {p['level']}")
    for pid, p in nodes.items():
        floors = sum(c["floor_usd"] for c in nodes.values() if c["parent"] == pid)
        if floors > p["monthly_usd"]:
            errors.append(f"budget {pid}: children's guaranteed floors ({floors}) exceed its monthly budget")
    if errors:
        raise CompileError("budget tree invalid:\n  " + "\n  ".join(errors))
    paths = {}
    for u in d["use_cases.yaml"]["use_cases"]:
        path, cur = [], u["id"]
        while cur:
            path.append(cur)
            cur = nodes[cur]["parent"]
        if path[1:3] != [u["team"], u["business_unit"]]:
            raise CompileError(f"budget path for {u['id']} does not match its team/business unit: {path}")
        paths[u["id"]] = path  # leaf (use case) -> root (org); user node is added at request time
    return {"nodes": nodes, "paths": paths}


def governance_warnings(d: dict, now: dt.datetime) -> list[dict]:
    warns = []
    horizon = now + dt.timedelta(days=WARN_DAYS)

    def check(subject: str, what: str, ts: str):
        t = parse_ts(ts)
        if t < now:
            warns.append({"level": "warning", "code": "governance_record_expired", "subject": subject,
                          "detail": f"{what} expired at {ts}"})
        elif t < horizon:
            warns.append({"level": "warning", "code": "governance_record_expiring", "subject": subject,
                          "detail": f"{what} expires at {ts} (within {WARN_DAYS} days)"})

    for m in d["models.yaml"]["models"]:
        g = m["governance_record"]
        check(m["id"], f"agreement {g['agreement']['ref']}", g["agreement"]["expires_at"])
        check(m["id"], f"data processing terms {g['data_processing_terms']['ref']}",
              g["data_processing_terms"]["expires_at"])
        check(m["id"], f"third-party risk assessment {g['third_party_risk_assessment']['ref']}",
              g["third_party_risk_assessment"]["valid_until"])
    for uc, entries in d["pairings.yaml"]["pairings"].items():
        for e in entries:
            check(f"{uc}/{e['model']}", "pairing", e["expires_at"])
    for u in d["use_cases.yaml"]["use_cases"]:
        check(u["id"], "use case approval", u["expires_at"])
    return warns


def run_opa_test() -> str:
    proc = subprocess.run([opa_bin(), "test", str(POLICIES), "--format", "pretty"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise CompileError(f"opa test failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "ok"


def next_version() -> int:
    prev = 0
    snap = BUILD / "snapshot.json"
    if snap.exists():
        prev = int(json.loads(snap.read_text()).get("config_version", 0))
    counter = BUILD / "version_counter"
    if counter.exists():
        prev = max(prev, int(counter.read_text().strip() or 0))
    return prev + 1


def build_snapshot(d: dict, budgets: dict, version: int, now: dt.datetime, warnings: list[dict]) -> dict:
    approvals = json.loads(APPROVALS_PATH.read_text()) if APPROVALS_PATH.exists() else {}
    pub = keys.load_public("approving_function")
    pairings = {}
    for uc, entries in d["pairings.yaml"]["pairings"].items():
        out = []
        for e in sorted(entries, key=lambda x: x["rank"]):
            e = json.loads(json.dumps(e))
            sig = approvals.get(entry_key(uc, e))
            e["approval"]["signature"] = sig
            if not keys.verify_b64(pub, pairing_signing_payload(uc, e), sig):
                warnings.append({"level": "warning", "code": "pairing_signature_invalid", "subject": f"{uc}/{e['model']}",
                                 "detail": "no valid approving-function signature; the router will reject this entry "
                                           "(run `make approve-pairings`)"})
            out.append(e)
        pairings[uc] = out

    policies = {p.name: p.read_text() for p in sorted(POLICIES.glob("*.rego")) if not p.name.endswith("_test.rego")}
    use_cases = {u["id"]: u for u in d["use_cases.yaml"]["use_cases"]}
    budget_doc = d["budgets.yaml"]
    root_budgets = {}
    for u in use_cases.values():
        if u.get("agentic"):
            node = budgets["nodes"][u["id"]]
            root_budgets[u["id"]] = node.get("root_request_budget_tokens",
                                             budget_doc["defaults"]["root_request_budget_tokens"])
    source_hash = sha256_hex(b"".join(canonical_json(d[k]) for k in sorted(d)))
    return {
        "config_version": version,
        "compiled_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_hash": source_hash,
        "policy_version": sha256_hex(canonical_json(policies))[:16],
        "use_cases": use_cases,
        "workloads": {w: u["id"] for u in use_cases.values() for w in u["workloads"]},
        "models": {m["id"]: m for m in d["models.yaml"]["models"]},
        "pairings": pairings,
        "tools": {s["id"]: {"url": s["url"], "tools": {t["name"]: {"sha256": t["sha256"], "class": t["class"]}
                                                     for t in s["tools"]}}
                  for s in d["tools.yaml"]["mcp_servers"]},
        "agents": {a["id"]: a for a in d["agents.yaml"]["agents"]},
        "allowlists": d["allowlists.yaml"]["allowlists"],
        "settings": {"a2a_hop_limit": d["allowlists.yaml"]["settings"]["a2a_hop_limit"],
                     "root_request_budget_tokens": budget_doc["defaults"]["root_request_budget_tokens"]},
        "budgets": {"nodes": budgets["nodes"], "paths": budgets["paths"], "user_defaults": budget_doc["defaults"]["user"],
                    "root_budget_tokens": root_budgets},
        "policy_modes": d["policy_modes.yaml"]["policy_modes"],
        "policies": policies,
        "compile_warnings": warnings,
    }


def sign_snapshot(snapshot: dict) -> str:
    return keys.sign_b64("control_plane", canonical_json(snapshot))


def write_snapshot(snapshot: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "snapshot.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    (directory / "snapshot.sig").write_text(sign_snapshot(snapshot))


def decision_records(prev: dict | None, snap: dict) -> list[dict]:
    """One record per use-case state change (approver, evidence references, config version)."""
    recs = []
    prev_ucs = (prev or {}).get("use_cases", {})
    for uid, u in snap["use_cases"].items():
        before = prev_ucs.get(uid, {}).get("state")
        if before == u["state"]:
            continue
        evidence = []
        if u.get("risk_review"):
            evidence.append(u["risk_review"]["ref"])
        for e in snap["pairings"].get(uid, []):
            evidence.append(e["evaluation_ref"])
        recs.append({"use_case_id": uid, "from_state": before, "to_state": u["state"],
                     "approver": u.get("approved_by"), "approved_at": u.get("approved_at"),
                     "evidence_refs": evidence, "config_version": snap["config_version"]})
    return recs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="override current time (ISO 8601) for reproducible checks")
    ap.add_argument("--skip-opa-test", action="store_true")
    a = ap.parse_args(argv)
    now = parse_ts(a.now) if a.now else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    try:
        d = load_sources()
        check_references(d)
        check_promotion(d)
        budgets = check_budgets(d)
        opa_result = "skipped" if a.skip_opa_test else run_opa_test()
        warnings = governance_warnings(d, now)
        version = next_version()
        snap = build_snapshot(d, budgets, version, now, warnings)
    except CompileError as exc:
        print(f"COMPILE FAILED: {exc}", file=sys.stderr)
        return 1

    prev_path = BUILD / "snapshot.json"
    ev_path = ROOT / "evidence" / "control_plane.jsonl"
    # A fresh evidence chain starts with a full set of decision records.
    prev = json.loads(prev_path.read_text()) if prev_path.exists() and ev_path.exists() else None
    write_snapshot(snap, BUILD)
    (BUILD / "version_counter").write_text(str(version))
    (BUILD / "compile_warnings.json").write_text(json.dumps(warnings, indent=2))

    ev = EvidenceStore(ev_path)
    for rec in decision_records(prev, snap):
        ev.append("use_case_state_change", rec)
    for w in warnings:
        ev.append("compile_warning", {**w, "config_version": version})
    ev.append("snapshot_compiled", {"config_version": version, "policy_version": snap["policy_version"],
                                    "source_hash": snap["source_hash"],
                                    "snapshot_sha256": sha256_hex(canonical_json(snap))})

    for w in warnings:
        print(f"WARNING [{w['code']}] {w['subject']}: {w['detail']}")
    print(f"opa test: {opa_result}")
    print(f"compiled snapshot config_version={version} policy_version={snap['policy_version']} -> {BUILD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
