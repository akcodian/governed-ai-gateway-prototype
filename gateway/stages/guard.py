"""Stage 5 - Guard: redact synthetic sensitive patterns; record counts, never values."""
from __future__ import annotations

import re
from collections import Counter

PATTERNS = {
    "account_number": re.compile(r"\bACCT-\d{4}-\d{4}-\d{4}\b"),
    "national_id": re.compile(r"\bNID-\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"\b[\w.+-]+@example\.(?:com|org|net)\b"),
}


def redact_text(text: str, counts: Counter) -> str:
    for name, rx in PATTERNS.items():
        text, n = rx.subn(f"[REDACTED:{name}]", text)
        if n:
            counts[name] += n
    return text


def redact_messages(messages: list[dict]) -> tuple[list[dict], Counter]:
    counts: Counter = Counter()
    out = []
    for m in messages:
        m = dict(m)
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = redact_text(c, counts)
        elif isinstance(c, list):
            m["content"] = [{**p, "text": redact_text(p["text"], counts)} if isinstance(p, dict) and "text" in p else p
                            for p in c]
        out.append(m)
    return out, counts


def redact_json(value, counts: Counter):
    """Recursively redact every string inside a JSON value."""
    if isinstance(value, str):
        return redact_text(value, counts)
    if isinstance(value, list):
        return [redact_json(v, counts) for v in value]
    if isinstance(value, dict):
        return {k: redact_json(v, counts) for k, v in value.items()}
    return value


def record(span, ctx, phase: str, counts: Counter) -> None:
    total = sum(counts.values())
    span.set_attribute("gov.guard.phase", phase)
    span.set_attribute("gov.guard.redactions", total)
    key = f"gov.guard.redactions.{phase}"
    ctx.attrs[key] = ctx.attrs.get(key, 0) + total
    if total:
        ctx.evidence.setdefault("redactions", {})[phase] = dict(counts)
