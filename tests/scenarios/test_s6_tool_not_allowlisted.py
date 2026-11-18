"""S6: an agent calls an MCP tool that is not on its allowlist."""
from tests.helpers import wait_evidence

REQUIRED = ["gov.use_case.id", "gov.root_request.id", "gov.decision", "gov.decision.reason", "gov.decision.stage",
            "gov.config.version", "gov.policy.version", "gen_ai.tool.name", "gen_ai.operation.name", "gov.mcp.server"]


def test_s6_tool_not_allowlisted(clean, spans, scenario):
    stack = clean
    with scenario("S6", "update_case denied; tools/list for UC-30 returns only search_docs") as r:
        listed = stack.mcp("uc30-orchestrator", "tools/list")
        names = [t["name"] for t in listed.json()["result"]["tools"]]
        before = stack.tool_calls().get("update_case", 0)
        call = stack.mcp("uc30-orchestrator", "tools/call", {"name": "update_case",
                                                             "arguments": {"case_id": "CASE-0001", "status": "closed"}})
        after = stack.tool_calls().get("update_case", 0)
        ok = stack.mcp("uc30-orchestrator", "tools/call", {"name": "search_docs", "arguments": {"query": "refund"}})
        a = spans.request_span(stack, call.headers["x-trace-id"])["attrs"]
        ev = wait_evidence(stack, call.headers["x-trace-id"])
        r.check("tools_list_only_search_docs", names == ["search_docs"])
        r.check("update_case_denied_403", call.status_code == 403)
        r.check("reason_tool_not_allowed", a.get("gov.decision.reason") == "tool_not_allowed")
        r.check("decision_deny", a.get("gov.decision") == "deny")
        r.check("update_case_never_reached_server", after == before)
        r.check("allowed_tool_still_works", ok.status_code == 200)
        r.observed(f"tools/list={names}; tools/call update_case -> HTTP {call.status_code} "
                   f"{a.get('gov.decision')}/{a.get('gov.decision.reason')}; server update_case calls {before}->{after}; "
                   f"search_docs -> HTTP {ok.status_code}")
        r.evidence(REQUIRED, a, ev)
