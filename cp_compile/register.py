"""Register MCP tools and A2A agents from the running mocks (R4).

* For each MCP server in tools.yaml: list tools with the official MCP client and store the
  SHA-256 of each tool's canonical definition (name, description, inputSchema).
* For each agent in agents.yaml: fetch the Agent Card from /.well-known/agent-card.json,
  validate it against the A2A SDK's protobuf AgentCard, and store the JSON-RPC URL and card hash.

Usage: python -m cp_compile.register [--mcp-url-override mcp-docs=http://localhost:8200/mcp]
                                     [--agent-base-override agent-b=http://localhost:8301]
Writes tools.yaml and agents.yaml in place.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
import yaml

from govlib.canonical import canonical_json, sha256_hex, tool_definition_hash

CP = Path(__file__).resolve().parents[1] / "control-plane"
AGENT_CARD_PATH = "/.well-known/agent-card.json"


async def list_mcp_tools(url: str) -> list[dict]:
    from mcp import Client

    async with Client(url, cache=None) as client:
        result = await client.list_tools()
    return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in result.tools]


def validate_agent_card(card: dict) -> None:
    from a2a.types import a2a_pb2
    from google.protobuf import json_format

    json_format.ParseDict(card, a2a_pb2.AgentCard(), ignore_unknown_fields=False)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mcp-url-override", action="append", default=[])
    p.add_argument("--agent-base-override", action="append", default=[])
    a = p.parse_args()
    mcp_over = dict(x.split("=", 1) for x in a.mcp_url_override)
    agent_over = dict(x.split("=", 1) for x in a.agent_base_override)

    tools_doc = yaml.safe_load((CP / "tools.yaml").read_text())
    for server in tools_doc["mcp_servers"]:
        url = mcp_over.get(server["id"], server["url"])
        live = {t["name"]: t for t in await list_mcp_tools(url)}
        for tool in server["tools"]:
            t = live[tool["name"]]
            tool["sha256"] = tool_definition_hash(t["name"], t["description"], t["inputSchema"])
            print(f"{server['id']}/{tool['name']}: {tool['sha256'][:16]}...")
    (CP / "tools.yaml").write_text(_header(CP / "tools.yaml") + yaml.safe_dump(tools_doc, sort_keys=False))

    agents_doc = yaml.safe_load((CP / "agents.yaml").read_text())
    async with httpx.AsyncClient(timeout=10) as client:
        for agent in agents_doc["agents"]:
            base = agent_over.get(agent["id"], agent["base_url"])
            card = (await client.get(base + AGENT_CARD_PATH)).json()
            validate_agent_card(card)
            iface = next(i for i in card["supportedInterfaces"] if i["protocolBinding"] == "JSONRPC")
            agent.update({"name": card["name"], "jsonrpc_url": iface["url"],
                          "protocol_version": iface["protocolVersion"],
                          "skills": [s["id"] for s in card.get("skills", [])],
                          "card_sha256": sha256_hex(canonical_json(card))})
            print(f"agent {agent['id']}: {iface['url']} (A2A {iface['protocolVersion']})")
    (CP / "agents.yaml").write_text(_header(CP / "agents.yaml") + yaml.safe_dump(agents_doc, sort_keys=False))


def _header(path: Path) -> str:
    lines = []
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            lines.append(line)
        else:
            break
    return "\n".join(lines) + "\n" if lines else ""


if __name__ == "__main__":
    asyncio.run(main())
