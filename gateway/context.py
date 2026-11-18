"""Per-request context and the refusal exception that ends the pipeline at a stage."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


class GovRefusal(Exception):
    """Raised by a stage to refuse the request; the pipeline stops at that stage."""

    def __init__(self, status: int, stage: str, reason: str, decision: str = "deny", detail: dict | None = None):
        super().__init__(reason)
        self.status = status
        self.stage = stage
        self.reason = reason
        self.decision = decision
        self.detail = detail or {}


@dataclass
class RequestContext:
    operation: str                      # chat | mcp.tools_list | mcp.tools_call | a2a.delegate | chat.resume
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str = ""
    client_id: str | None = None
    use_case_id: str | None = None
    use_case: dict | None = None
    end_user: str = "anonymous"
    root_request_id: str | None = None
    root_budget_tokens: int | None = None
    root_token_issued: str | None = None   # set when this request created the root
    hop: int = 0
    config_version: int | None = None
    policy_version: str | None = None
    decision: str = "allow"
    reason: str | None = None
    stage: str | None = None
    # attributes copied to the request span and the evidence record
    attrs: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
