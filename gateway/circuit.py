"""Per-model circuit breaker: open after N consecutive failures, half-open after a cool-down."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _State:
    failures: int = 0
    opened_at: float | None = None


class CircuitBreakers:
    def __init__(self, threshold: int = 3, reset_seconds: float = 30.0):
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self._s: dict[str, _State] = {}

    def state(self, model: str) -> str:
        s = self._s.get(model)
        if not s or s.opened_at is None:
            return "closed"
        if time.monotonic() - s.opened_at >= self.reset_seconds:
            return "half_open"
        return "open"

    def available(self, model: str) -> bool:
        return self.state(model) != "open"

    def record_success(self, model: str) -> None:
        self._s[model] = _State()

    def record_failure(self, model: str) -> None:
        s = self._s.setdefault(model, _State())
        s.failures += 1
        if s.failures >= self.threshold:
            s.opened_at = time.monotonic()  # (re)open; a failed half-open trial re-opens

    def snapshot(self) -> dict:
        return {m: {"state": self.state(m), "failures": s.failures} for m, s in self._s.items()}

    def reset(self) -> None:
        self._s.clear()
