"""Thin async client for the OPA REST API (policy upload, data upload, decisions)."""
from __future__ import annotations

import httpx


class OpaError(Exception):
    pass


class OpaClient:
    def __init__(self, base_url: str):
        self.client = httpx.AsyncClient(base_url=base_url, timeout=5.0,
                                        limits=httpx.Limits(max_keepalive_connections=50, max_connections=100))

    async def put_policy(self, policy_id: str, source: str) -> None:
        r = await self.client.put(f"/v1/policies/{policy_id}", content=source.encode(),
                                  headers={"Content-Type": "text/plain"})
        if r.status_code != 200:
            raise OpaError(f"policy {policy_id} rejected by OPA: {r.text}")

    async def put_data(self, path: str, data: dict) -> None:
        r = await self.client.put(f"/v1/data/{path}", json=data)
        if r.status_code not in (200, 204):
            raise OpaError(f"data upload failed: {r.text}")

    async def decide(self, path: str, input_: dict) -> dict:
        r = await self.client.post(f"/v1/data/{path}", json={"input": input_})
        if r.status_code != 200:
            raise OpaError(f"OPA decision {path} failed: {r.status_code} {r.text}")
        body = r.json()
        if "result" not in body:
            raise OpaError(f"OPA decision {path} undefined (policies not loaded?)")
        return body["result"]

    async def health(self) -> bool:
        try:
            return (await self.client.get("/health")).status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self.client.aclose()
