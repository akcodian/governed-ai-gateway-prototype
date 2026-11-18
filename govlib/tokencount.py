"""Deterministic token counting shared by the gateway and the mock model server.

Rule: one token per whitespace-separated word of each message's text content
(``len(text.split())``), summed over all messages. Output tokens of the mock model
equal the number of whitespace-separated words it returns.
"""
from __future__ import annotations


def count_text(text: str | None) -> int:
    return len((text or "").split())


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI content parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def count_messages(messages: list[dict]) -> int:
    return sum(count_text(message_text(m)) for m in messages or [])
