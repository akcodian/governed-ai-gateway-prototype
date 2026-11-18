"""JSON Schemas for the control-plane YAML files."""
from __future__ import annotations

TS = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"}
TS_OR_NULL = {"anyOf": [TS, {"type": "null"}]}
STR_LIST = {"type": "array", "items": {"type": "string"}}

USE_CASES = {
    "type": "object",
    "required": ["use_cases"],
    "properties": {"use_cases": {"type": "array", "items": {
        "type": "object",
        "required": ["id", "name", "owner", "business_unit", "team", "risk_tier", "data_classes", "interaction",
                     "state", "approved_by", "approved_at", "expires_at", "workloads"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "pattern": r"^UC-\d{2}$"},
            "name": {"type": "string"},
            "owner": {"type": ["string", "null"]},
            "business_unit": {"type": "string"},
            "team": {"type": "string"},
            "risk_tier": {"enum": ["low", "medium", "high"]},
            "data_classes": STR_LIST,
            "interaction": {"enum": ["interactive", "batch"]},
            "agentic": {"type": "boolean"},
            "state": {"enum": ["requested", "sandbox", "approved", "production", "suspended", "retired"]},
            "approved_by": {"type": ["string", "null"]},
            "approved_at": TS_OR_NULL,
            "expires_at": TS,
            "risk_review": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["ref", "tier", "date"]}]},
            "workloads": STR_LIST,
        },
    }}},
}

_DATED_REF = {"type": "object", "required": ["ref", "expires_at"], "properties": {"ref": {"type": "string"}, "expires_at": TS}}

MODELS = {
    "type": "object",
    "required": ["models"],
    "properties": {"models": {"type": "array", "items": {
        "type": "object",
        "required": ["id", "provider", "region", "endpoint", "capabilities", "price_per_1k_input",
                     "price_per_1k_output", "governance_record"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "provider": {"type": "string"},
            "region": {"type": "string"},
            "endpoint": {"type": "string"},
            "capabilities": {"type": "object", "required": ["context_length", "tool_calling", "structured_output"],
                             "properties": {"context_length": {"type": "integer", "minimum": 1},
                                            "tool_calling": {"type": "boolean"},
                                            "structured_output": {"type": "boolean"}}},
            "price_per_1k_input": {"type": "number", "minimum": 0},
            "price_per_1k_output": {"type": "number", "minimum": 0},
            "governance_record": {
                "type": "object",
                "required": ["version", "agreement", "data_processing_terms", "third_party_risk_assessment",
                             "approved_data_classes", "approved_regions", "evaluations"],
                "properties": {
                    "version": {"type": "string"},
                    "agreement": {"type": "object", "required": ["ref", "signed_at", "expires_at"],
                                  "properties": {"signed_at": TS, "expires_at": TS}},
                    "data_processing_terms": _DATED_REF,
                    "third_party_risk_assessment": {"type": "object", "required": ["ref", "rating", "valid_until"],
                                                    "properties": {"valid_until": TS}},
                    "approved_data_classes": STR_LIST,
                    "approved_regions": STR_LIST,
                    "evaluations": {"type": "array", "items": {"type": "object",
                                                                "required": ["use_case", "suite", "score", "date"]}},
                },
            },
        },
    }}},
}

PAIRINGS = {
    "type": "object",
    "required": ["pairings"],
    "properties": {"pairings": {"type": "object", "additionalProperties": {"type": "array", "items": {
        "type": "object",
        "required": ["model", "rank", "data_classes", "regions", "evaluation_ref", "expires_at", "approval"],
        "additionalProperties": False,
        "properties": {
            "model": {"type": "string"}, "rank": {"type": "integer", "minimum": 1},
            "data_classes": STR_LIST, "regions": STR_LIST, "evaluation_ref": {"type": "string"},
            "expires_at": TS,
            "approval": {"type": "object", "required": ["approved_by"],
                         "properties": {"approved_by": {"type": "string"}, "signature": {"type": ["string", "null"]}}},
        },
    }}}},
}

TOOLS = {
    "type": "object",
    "required": ["mcp_servers"],
    "properties": {"mcp_servers": {"type": "array", "items": {
        "type": "object", "required": ["id", "url", "tools"],
        "properties": {"tools": {"type": "array", "items": {
            "type": "object", "required": ["name", "class", "sha256"],
            "properties": {"class": {"enum": ["read", "write"]},
                           "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}}}},
    }}},
}

AGENTS = {
    "type": "object",
    "required": ["agents"],
    "properties": {"agents": {"type": "array", "items": {
        "type": "object", "required": ["id", "base_url", "jsonrpc_url", "protocol_version", "card_sha256"],
    }}},
}

ALLOWLISTS = {
    "type": "object",
    "required": ["settings", "allowlists"],
    "properties": {
        "settings": {"type": "object", "required": ["a2a_hop_limit"],
                     "properties": {"a2a_hop_limit": {"type": "integer", "minimum": 0}}},
        "allowlists": {"type": "object", "additionalProperties": {
            "type": "object", "properties": {"tools": {"type": "object", "additionalProperties": STR_LIST},
                                             "agents": STR_LIST}}},
    },
}

_LIMITS = {
    "monthly_usd": {"type": "number", "minimum": 0},
    "floor_usd": {"type": "number", "minimum": 0},
    "ceiling_usd": {"type": "number", "minimum": 0},
    "tokens_per_minute": {"type": "object", "required": ["default"],
                          "additionalProperties": {"type": "integer", "minimum": 0}},
    "fail_mode": {"enum": ["open", "closed"]},
}

BUDGETS = {
    "type": "object",
    "required": ["defaults", "nodes"],
    "properties": {
        "defaults": {"type": "object", "required": ["root_request_budget_tokens", "user"],
                     "properties": {"root_request_budget_tokens": {"type": "integer", "minimum": 1},
                                    "user": {"type": "object", "required": list(_LIMITS), "properties": _LIMITS}}},
        "nodes": {"type": "array", "items": {
            "type": "object",
            "required": ["id", "level", "parent", *_LIMITS],
            "properties": {"id": {"type": "string"},
                           "level": {"enum": ["org", "business_unit", "team", "use_case"]},
                           "parent": {"type": ["string", "null"]},
                           "root_request_budget_tokens": {"type": "integer", "minimum": 1},
                           **_LIMITS},
        }},
    },
}

POLICY_MODES = {"type": "object", "required": ["policy_modes"],
                "properties": {"policy_modes": {"type": "object",
                                                "additionalProperties": {"enum": ["enforce", "observation"]}}}}

SCHEMAS = {
    "use_cases.yaml": USE_CASES,
    "models.yaml": MODELS,
    "pairings.yaml": PAIRINGS,
    "tools.yaml": TOOLS,
    "agents.yaml": AGENTS,
    "allowlists.yaml": ALLOWLISTS,
    "budgets.yaml": BUDGETS,
    "policy_modes.yaml": POLICY_MODES,
}
