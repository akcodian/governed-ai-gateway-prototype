"""Canonical JSON and hashing used for signatures and tool-definition hashes."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def tool_definition_hash(name: str, description: str | None, input_schema: dict | None) -> str:
    """SHA-256 of a tool's canonical definition (name, description, input schema)."""
    return sha256_hex(
        canonical_json({"name": name, "description": description or "", "inputSchema": input_schema or {}})
    )


def pairing_signing_payload(use_case_id: str, entry: dict) -> bytes:
    """Canonical bytes an approving function signs for one pairing entry.

    The signature itself is excluded; everything else that governs routing is covered.
    """
    approval = dict(entry.get("approval") or {})
    approval.pop("signature", None)
    body = {
        "use_case": use_case_id,
        "model": entry["model"],
        "rank": entry["rank"],
        "data_classes": sorted(entry.get("data_classes", [])),
        "regions": sorted(entry.get("regions", [])),
        "evaluation_ref": entry.get("evaluation_ref"),
        "expires_at": entry.get("expires_at"),
        "approved_by": approval.get("approved_by"),
    }
    return canonical_json(body)
