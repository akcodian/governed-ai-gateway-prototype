"""Load, verify and activate signed control-plane snapshots; keep the last known good one."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from govlib import keys
from govlib.canonical import canonical_json, sha256_hex
from gateway.opa import OpaClient

log = logging.getLogger("gateway.snapshot")


class SnapshotRejected(Exception):
    pass


class SnapshotManager:
    def __init__(self, opa: OpaClient, emit_evidence):
        self.opa = opa
        self.emit = emit_evidence
        self.current: dict | None = None
        self.current_hash: str | None = None
        self._pub = keys.load_public("control_plane")

    @property
    def version(self) -> int | None:
        return self.current["config_version"] if self.current else None

    def verify(self, directory: Path) -> dict:
        snap_path, sig_path = directory / "snapshot.json", directory / "snapshot.sig"
        try:
            snap = json.loads(snap_path.read_text())
            sig = sig_path.read_text().strip()
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotRejected(f"unreadable_snapshot: {exc}") from exc
        if not keys.verify_b64(self._pub, canonical_json(snap), sig):
            raise SnapshotRejected("bad_signature")
        return snap

    async def load(self, directory: Path) -> dict:
        """Verify and activate. On any rejection the last known good snapshot stays active."""
        try:
            snap = self.verify(directory)
            h = sha256_hex(canonical_json(snap))
            if self.current is not None:
                if snap["config_version"] < self.current["config_version"]:
                    raise SnapshotRejected("stale_config_version")
                if snap["config_version"] == self.current["config_version"]:
                    if h == self.current_hash:
                        return {"loaded": False, "reason": "unchanged", "config_version": self.version}
                    raise SnapshotRejected("config_version_not_increased")
            await self._push_to_opa(snap)
        except SnapshotRejected as exc:
            log.warning("snapshot rejected (%s) from %s; serving last known good version %s", exc, directory, self.version)
            self.emit("config_rejected", {"reason": str(exc), "source": str(directory),
                                          "serving_config_version": self.version})
            raise
        previous = self.version
        self.current, self.current_hash = snap, h
        self.emit("config_loaded", {"config_version": snap["config_version"], "previous_config_version": previous,
                                    "policy_version": snap["policy_version"], "snapshot_sha256": h})
        log.info("activated snapshot config_version=%s", snap["config_version"])
        return {"loaded": True, "config_version": snap["config_version"]}

    async def _push_to_opa(self, snap: dict) -> None:
        data = {k: v for k, v in snap.items() if k not in ("policies", "compile_warnings")}
        await self.opa.put_data("snapshot", data)
        for name, source in snap["policies"].items():
            await self.opa.put_policy(name.removesuffix(".rego"), source)

    async def ensure_opa(self) -> None:
        """Re-push the active snapshot (e.g. after an OPA restart)."""
        if self.current:
            await self._push_to_opa(self.current)
