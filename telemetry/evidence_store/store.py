"""Append-only, hash-chained JSONL evidence store (R8).

Each line is a JSON object with ``prev_hash`` = SHA-256 of the previous line's bytes
(``"0"*64`` for the first line) and ``seq`` = 0-based line number. A sidecar ``<file>.head``
holds the count and the hash of the last line, so truncation of the tail is also detected.

Records are redacted by construction: callers pass decision attributes only, never prompts
or responses.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

from govlib.canonical import sha256_hex

GENESIS = "0" * 64


class EvidenceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.head_path = self.path.with_suffix(self.path.suffix + ".head")
        self._lock = threading.Lock()
        self._seq, self._prev = self._load_tail()

    def _load_tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        count = 0
        with self.path.open("rb") as f:
            for line in f:
                line = line.rstrip(b"\n")
                if line:
                    last = line
                    count += 1
        return (count, sha256_hex(last)) if last is not None else (0, GENESIS)

    def append(self, kind: str, record: dict[str, Any]) -> dict:
        with self._lock:
            entry = {
                "seq": self._seq,
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
                "kind": kind,
                "record": record,
                "prev_hash": self._prev,
            }
            line = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
            self._prev = sha256_hex(line.encode("utf-8"))
            self._seq += 1
            self.head_path.write_text(json.dumps({"count": self._seq, "last_hash": self._prev}))
            return entry


class AsyncEvidenceWriter:
    """Writes evidence off the request's critical path, in order, via a background task."""

    def __init__(self, store: EvidenceStore):
        self.store = store
        self.queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            kind, record = await self.queue.get()
            try:
                await asyncio.to_thread(self.store.append, kind, record)
            finally:
                self.queue.task_done()

    def emit(self, kind: str, record: dict) -> None:
        self.queue.put_nowait((kind, record))

    async def flush(self) -> None:
        await self.queue.join()

    async def stop(self) -> None:
        await self.flush()
        if self._task:
            self._task.cancel()


def verify_chain(path: str | Path) -> tuple[bool, str]:
    """Return (ok, message). Detects edits, insertions, reordering, and deletions."""
    path = Path(path)
    prev = GENESIS
    count = 0
    with path.open("rb") as f:
        for i, raw in enumerate(f):
            line = raw.rstrip(b"\n")
            if not line:
                return False, f"line {i}: empty line"
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return False, f"line {i}: not valid JSON"
            if obj.get("prev_hash") != prev:
                return False, f"line {i}: prev_hash mismatch (chain broken; an earlier line was edited or removed)"
            if obj.get("seq") != i:
                return False, f"line {i}: seq {obj.get('seq')} != {i} (line inserted or removed)"
            prev = sha256_hex(line)
            count += 1
    head_path = path.with_suffix(path.suffix + ".head")
    if head_path.exists():
        head = json.loads(head_path.read_text())
        if head.get("count") != count or head.get("last_hash") != prev:
            return False, f"head mismatch: head says {head.get('count')} lines, file has {count} (tail edited or truncated)"
    else:
        return False, "head file missing"
    return True, f"ok ({count} records)"
