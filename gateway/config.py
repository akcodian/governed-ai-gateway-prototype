"""Gateway settings (environment variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    snapshot_dir: Path = field(default_factory=lambda: Path(os.environ.get("GOV_SNAPSHOT_DIR", ROOT / "control-plane" / "build")))
    opa_url: str = field(default_factory=lambda: os.environ.get("OPA_URL", "http://localhost:8181"))
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    approval_url: str = field(default_factory=lambda: os.environ.get("APPROVAL_URL", "http://localhost:8090"))
    evidence_dir: Path = field(default_factory=lambda: Path(os.environ.get("GOV_EVIDENCE_DIR", ROOT / "evidence")))
    region: str = field(default_factory=lambda: os.environ.get("GATEWAY_REGION", "eu-west"))
    cb_failure_threshold: int = field(default_factory=lambda: int(os.environ.get("CB_FAILURE_THRESHOLD", "3")))
    cb_reset_seconds: float = field(default_factory=lambda: float(os.environ.get("CB_RESET_SECONDS", "30")))
    upstream_timeout_s: float = field(default_factory=lambda: float(os.environ.get("UPSTREAM_TIMEOUT_S", "30")))
    redis_timeout_s: float = field(default_factory=lambda: float(os.environ.get("REDIS_TIMEOUT_S", "0.25")))
    default_max_tokens: int = 256
