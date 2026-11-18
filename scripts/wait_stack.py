"""Wait until every service of the stack answers its health endpoint."""
from __future__ import annotations

import sys
import time

import httpx

CHECKS = {
    "gateway": "http://localhost:8080/health",
    "approval-service": "http://localhost:8090/health",
    "model-server": "http://localhost:8100/health",
    "mcp-server": "http://localhost:8200/health",
    "agent-b": "http://localhost:8301/health",
    "agent-c": "http://localhost:8302/health",
    "opa": "http://localhost:8181/health",
    "otel-collector": "http://localhost:13133/",
}


def main(timeout_s: float = 180) -> int:
    deadline = time.time() + timeout_s
    pending = dict(CHECKS)
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            try:
                r = httpx.get(url, timeout=2)
                if r.status_code == 200 and (name != "gateway" or r.json().get("status") == "ok"):
                    print(f"  {name}: up")
                    pending.pop(name)
            except httpx.HTTPError:
                pass
        if pending:
            time.sleep(1)
    if pending:
        print(f"services not healthy after {timeout_s}s: {sorted(pending)}")
        return 1
    print("stack healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
