# R6/R4: authorization. Default deny.
# Data: data.snapshot (pushed by the gateway from the verified, signed snapshot).
# Input: {action, use_case_id, now, requested_model?, max_tokens?, server?, tool?, current_hashes?,
#         quarantined?, agent?, hop?}
# Any deny reason listed in data.snapshot.policy_modes with mode "observation" is recorded in
# `would_deny` instead of blocking.
package gov.authz

import rego.v1

snapshot := data.snapshot

active_states := {"sandbox", "production"}

known_actions := {"model.chat", "mcp.tools_list", "mcp.tools_call", "a2a.delegate"}

uc := snapshot.use_cases[input.use_case_id]

default allow := false

# ---------- identity / lifecycle ----------
raw_reasons contains "use_case_unknown" if not snapshot.use_cases[input.use_case_id]

raw_reasons contains "use_case_not_active" if {
	uc
	not uc.state in active_states
}

raw_reasons contains "use_case_expired" if {
	time.parse_rfc3339_ns(uc.expires_at) < time.parse_rfc3339_ns(input.now)
}

raw_reasons contains "action_not_permitted" if not input.action in known_actions

# ---------- model calls ----------
paired_models contains m if {
	some e in snapshot.pairings[input.use_case_id]
	m := e.model
}

raw_reasons contains "no_pairing" if {
	input.action == "model.chat"
	count(paired_models) == 0
}

raw_reasons contains "model_not_in_pairing" if {
	input.action == "model.chat"
	input.requested_model
	input.requested_model != "auto"
	not input.requested_model in paired_models
}

# Example of a newly introduced rule; snapshot.policy_modes decides observation vs enforce.
raw_reasons contains "max_tokens_cap" if {
	input.action == "model.chat"
	input.max_tokens > 4096
}

# ---------- MCP tools ----------
allowed_tools := {t | some t in snapshot.allowlists[input.use_case_id].tools[input.server]}

registered_tool(name) := snapshot.tools[input.server].tools[name]

quarantined := {t | some t in object.get(input, "quarantined", [])}

current_hashes := object.get(input, "current_hashes", {})

changed_tools := sort({t |
	some t, h in current_hashes
	h != registered_tool(t).sha256
})

raw_reasons contains "server_not_allowed" if {
	input.action in {"mcp.tools_list", "mcp.tools_call"}
	count(allowed_tools) == 0
}

raw_reasons contains "tool_not_registered" if {
	input.action == "mcp.tools_call"
	not registered_tool(input.tool)
}

raw_reasons contains "tool_not_allowed" if {
	input.action == "mcp.tools_call"
	not input.tool in allowed_tools
}

raw_reasons contains "tool_definition_changed" if {
	input.action == "mcp.tools_call"
	input.tool in allowed_tools
	current_hashes[input.tool] != registered_tool(input.tool).sha256
}

raw_reasons contains "tool_quarantined" if {
	input.action == "mcp.tools_call"
	input.tool in quarantined
}

visible_tools := sort({t |
	some t in allowed_tools
	registered_tool(t)
	current_hashes[t] == registered_tool(t).sha256
	not t in quarantined
})

# ---------- A2A delegation ----------
raw_reasons contains "agent_not_registered" if {
	input.action == "a2a.delegate"
	not snapshot.agents[input.agent]
}

raw_reasons contains "agent_not_allowed" if {
	input.action == "a2a.delegate"
	not input.agent in {a | some a in snapshot.allowlists[input.use_case_id].agents}
}

raw_reasons contains "hop_limit_exceeded" if {
	input.action == "a2a.delegate"
	input.hop > snapshot.settings.a2a_hop_limit
}

# ---------- enforcement vs observation ----------
mode(r) := object.get(object.get(snapshot, "policy_modes", {}), r, "enforce")

reasons := {r | some r in raw_reasons; mode(r) != "observation"}

would_deny := {r | some r in raw_reasons; mode(r) == "observation"}

allow if count(reasons) == 0

decision := {
	"allow": allow,
	"reasons": sort(reasons),
	"would_deny": sort(would_deny),
	"visible_tools": visible_tools,
	"changed_tools": changed_tools,
}
