"""Run the whole stack as local processes, without Docker (used on Code Ocean, where a capsule run
is itself a single container).

  python -m scripts.native_stack start   # start everything, wait until healthy
  python -m scripts.native_stack stop    # stop everything started by `start`

Same services, ports and configuration as docker-compose.yml. Binaries come from the environment:
  REDIS_SERVER (default: redis-server on PATH), OPA_BIN (default: opa / .tools/opa),
  OTELCOL_BIN (default: otelcol-contrib on PATH).
Logs and process IDs go to .native/ in the repository.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".native"
PIDS = STATE / "pids.json"
LOGS = STATE / "logs"
PY = sys.executable

HOST_OVERRIDES = "model-server=127.0.0.1,mcp-server=127.0.0.1,agent-b=127.0.0.1,agent-c=127.0.0.1"


def _bin(env: str, *candidates: str) -> str:
    if os.environ.get(env):
        return os.environ[env]
    for c in candidates:
        p = Path(c) if os.sep in c or "/" in c else None
        if p is not None and p.exists():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    raise SystemExit(f"{env} not set and none of {candidates} found on PATH")


def _opa() -> str:
    local = ROOT / ".tools" / ("opa.exe" if platform.system() == "Windows" else "opa")
    return _bin("OPA_BIN", "opa", str(local))


def _base_env() -> dict:
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(ROOT), "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                "GOV_KEY_DIR": os.environ.get("GOV_KEY_DIR", str(ROOT / "keys")), "PYTHONUNBUFFERED": "1"})
    return env


def _services() -> list[tuple[str, list[str], dict]]:
    py = [("model-server", "mocks.model_server.server", {"PORT": "8100"}),
          ("mcp-server", "mocks.mcp_server.server", {"PORT": "8200"}),
          ("agent-b", "mocks.a2a_agents.agent", {"PORT": "8301", "AGENT_ID": "agent-b",
                                                 "PUBLIC_URL": "http://agent-b:8301", "GATEWAY_URL": "http://127.0.0.1:8080"}),
          ("agent-c", "mocks.a2a_agents.agent", {"PORT": "8302", "AGENT_ID": "agent-c",
                                                 "PUBLIC_URL": "http://agent-c:8302", "GATEWAY_URL": "http://127.0.0.1:8080"}),
          ("approval-service", "approval_service.app", {"PORT": "8090", "GATEWAY_URL": "http://127.0.0.1:8080",
                                                        "APPROVAL_DB": str(STATE / "approvals.db"),
                                                        "GOV_EVIDENCE_DIR": str(ROOT / "evidence"),
                                                        "HOLD_TIMEOUT_SECONDS": "600"})]
    out = [("opa", [_opa(), "run", "--server", "--addr=127.0.0.1:8181", "--log-level=error"], {}),
           ("otel-collector", [_bin("OTELCOL_BIN", "otelcol-contrib"), "--config",
                               str(ROOT / "telemetry" / "otel-collector.yaml")],
            {"OTEL_OUT_DIR": str(ROOT / "telemetry" / "out")})]
    out += [(name, [PY, "-m", module], env) for name, module, env in py]
    out.append(("gateway", [PY, "-m", "gateway.app"], {
        "PORT": "8080", "OPA_URL": "http://127.0.0.1:8181", "REDIS_URL": "redis://127.0.0.1:6379/0",
        "APPROVAL_URL": "http://127.0.0.1:8090", "GOV_SNAPSHOT_DIR": str(ROOT / "control-plane" / "build"),
        "GOV_EVIDENCE_DIR": str(ROOT / "evidence"), "GATEWAY_REGION": "eu-west", "CB_RESET_SECONDS": "30",
        "LOG_LEVEL": "WARNING", "GOV_HOST_OVERRIDES": HOST_OVERRIDES}))
    return out


def _load() -> dict:
    return json.loads(PIDS.read_text()) if PIDS.exists() else {}


def _save(pids: dict) -> None:
    STATE.mkdir(exist_ok=True)
    PIDS.write_text(json.dumps(pids, indent=2))


def _spawn(name: str, cmd: list[str], extra_env: dict) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    env = _base_env()
    env.update(extra_env)
    log = open(LOGS / f"{name}.log", "ab")
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, **kwargs)
    return proc.pid


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def _redis_ping(timeout: float) -> bool:
    import redis
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if redis.Redis(host="127.0.0.1", port=6379, socket_timeout=0.5).ping():
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def start_redis() -> None:
    cmd = [_bin("REDIS_SERVER", "redis-server"), "--port", "6379", "--bind", "127.0.0.1",
           "--save", "", "--appendonly", "no", "--protected-mode", "yes"]
    pids = _load()
    pids["redis"] = _spawn("redis", cmd, {})
    _save(pids)
    if not _redis_ping(15):
        raise RuntimeError("redis-server did not become ready (see .native/logs/redis.log)")


def stop_redis() -> None:
    pids = _load()
    pid = pids.pop("redis", None)
    if pid:
        _kill(pid)
    _save(pids)


def start() -> int:
    stop(quiet=True)
    (ROOT / "telemetry" / "out").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence").mkdir(exist_ok=True)
    start_redis()
    pids = _load()
    for name, cmd, env in _services():
        pids[name] = _spawn(name, cmd, env)
        _save(pids)
        if name == "approval-service":
            time.sleep(2)  # gateway expects OPA and the approval service to be listening
    from scripts.wait_stack import main as wait
    rc = wait(timeout_s=180)
    if rc != 0:
        for f in sorted(LOGS.glob("*.log")):
            tail = f.read_text(errors="replace").splitlines()[-15:]
            print(f"--- {f.name}\n" + "\n".join(tail))
    return rc


def stop(quiet: bool = False) -> None:
    pids = _load()
    for name, pid in reversed(list(pids.items())):
        _kill(pid)
        if not quiet:
            print(f"  stopped {name}")
    _save({})


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "start":
        sys.exit(start())
    if action == "stop":
        stop()
        sys.exit(0)
    raise SystemExit("usage: python -m scripts.native_stack start|stop")
