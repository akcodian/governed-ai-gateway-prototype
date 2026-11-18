package gov.authz_test

import rego.v1

import data.gov.authz

snap := {
	"use_cases": {
		"UC-17": {"state": "production", "expires_at": "2030-12-31T23:59:59Z", "interaction": "batch"},
		"UC-30": {"state": "production", "expires_at": "2030-12-31T23:59:59Z", "interaction": "interactive"},
		"UC-99": {"state": "suspended", "expires_at": "2030-12-31T23:59:59Z", "interaction": "batch"},
	},
	"pairings": {
		"UC-17": [{"model": "model-a", "rank": 1}, {"model": "model-b", "rank": 2}],
		"UC-30": [{"model": "model-a", "rank": 1}],
	},
	"tools": {"mcp-docs": {"tools": {
		"search_docs": {"sha256": "h1", "class": "read"},
		"update_case": {"sha256": "h3", "class": "write"},
	}}},
	"agents": {"agent-b": {}, "agent-c": {}},
	"allowlists": {"UC-30": {"tools": {"mcp-docs": ["search_docs"]}, "agents": ["agent-b", "agent-c"]}},
	"settings": {"a2a_hop_limit": 3},
	"policy_modes": {"max_tokens_cap": "observation"},
}

now := "2026-11-18T00:00:00Z"

test_default_deny_unknown_action if {
	d := authz.decision with input as {"action": "other", "use_case_id": "UC-17", "now": now}
		with data.snapshot as snap
	not d.allow
	"action_not_permitted" in d.reasons
}

test_model_in_pairing_allowed if {
	d := authz.decision with input as {"action": "model.chat", "use_case_id": "UC-17", "now": now, "requested_model": "model-a"}
		with data.snapshot as snap
	d.allow
}

test_model_not_in_pairing_denied if {
	d := authz.decision with input as {"action": "model.chat", "use_case_id": "UC-17", "now": now, "requested_model": "model-c"}
		with data.snapshot as snap
	not d.allow
	d.reasons == ["model_not_in_pairing"]
}

test_suspended_use_case_denied if {
	d := authz.decision with input as {"action": "model.chat", "use_case_id": "UC-99", "now": now}
		with data.snapshot as snap
	"use_case_not_active" in d.reasons
}

test_unknown_use_case_denied if {
	d := authz.decision with input as {"action": "model.chat", "use_case_id": "UC-00", "now": now}
		with data.snapshot as snap
	not d.allow
	"use_case_unknown" in d.reasons
}

test_write_tool_not_allowed if {
	d := authz.decision with input as {"action": "mcp.tools_call", "use_case_id": "UC-30", "now": now, "server": "mcp-docs",
		"tool": "update_case", "current_hashes": {"search_docs": "h1", "update_case": "h3"}}
		with data.snapshot as snap
	d.reasons == ["tool_not_allowed"]
}

test_tools_list_filtered if {
	d := authz.decision with input as {"action": "mcp.tools_list", "use_case_id": "UC-30", "now": now, "server": "mcp-docs",
		"current_hashes": {"search_docs": "h1", "update_case": "h3"}}
		with data.snapshot as snap
	d.allow
	d.visible_tools == ["search_docs"]
}

test_tool_definition_changed if {
	d := authz.decision with input as {"action": "mcp.tools_call", "use_case_id": "UC-30", "now": now, "server": "mcp-docs",
		"tool": "search_docs", "current_hashes": {"search_docs": "CHANGED"}}
		with data.snapshot as snap
	d.reasons == ["tool_definition_changed"]
	d.changed_tools == ["search_docs"]
}

test_hop_limit if {
	ok := authz.decision with input as {"action": "a2a.delegate", "use_case_id": "UC-30", "now": now, "agent": "agent-b", "hop": 3}
		with data.snapshot as snap
	ok.allow
	bad := authz.decision with input as {"action": "a2a.delegate", "use_case_id": "UC-30", "now": now, "agent": "agent-b", "hop": 4}
		with data.snapshot as snap
	bad.reasons == ["hop_limit_exceeded"]
}

test_observation_mode_records_would_deny if {
	d := authz.decision with input as {"action": "model.chat", "use_case_id": "UC-17", "now": now, "max_tokens": 5000}
		with data.snapshot as snap
	d.allow
	d.would_deny == ["max_tokens_cap"]
}

test_enforce_mode_blocks if {
	s := object.union(snap, {"policy_modes": {"max_tokens_cap": "enforce"}})
	d := authz.decision with input as {"action": "model.chat", "use_case_id": "UC-17", "now": now, "max_tokens": 5000}
		with data.snapshot as s
	not d.allow
	d.reasons == ["max_tokens_cap"]
}
