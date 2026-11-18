"""Stage 3 - Budget (R5).

Atomic reservation (one Lua script) along the budget path user > use_case > team > business_unit > org,
against per-model tokens-per-minute limits, and against the root-request token budget carried in a
gateway-signed token. Reconciled with actual usage after the response. When Redis is unreachable
each limit's fail_mode decides (via OPA policy gov.budget) and the outcome is logged.

Money is stored as integer micro-USD. Keys:
  spend:{YYYY-MM}:{node}            monthly spend per budget node
  tpm:{epoch_minute}:{node}:{model} tokens reserved in the current minute
  root:{root_request_id}            tokens charged to a root request
"""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from gateway.context import GovRefusal, RequestContext
from gateway.opa import OpaClient

MICRO = 1_000_000

RESERVE_LUA = """
local req = cjson.decode(ARGV[1])
local guaranteed = false
for _, m in ipairs(req.money) do
  local cur = tonumber(redis.call('GET', m.key) or '0')
  local new = cur + m.amount
  if (not guaranteed) and new > m.ceiling then
    return cjson.encode({ok=false, reason='budget_exceeded', node=m.node})
  end
  if new <= m.floor then guaranteed = true end
end
for _, t in ipairs(req.tpm) do
  local cur = tonumber(redis.call('GET', t.key) or '0')
  if cur + t.amount > t.limit then
    return cjson.encode({ok=false, reason='rate_limited', node=t.node})
  end
end
if req.root then
  local cur = tonumber(redis.call('GET', req.root.key) or '0')
  if cur + req.root.amount > req.root.limit then
    return cjson.encode({ok=false, reason='root_budget_exceeded', used=cur, limit=req.root.limit})
  end
end
for _, m in ipairs(req.money) do
  redis.call('INCRBY', m.key, m.amount)
  redis.call('EXPIRE', m.key, 5184000)
end
for _, t in ipairs(req.tpm) do
  redis.call('INCRBY', t.key, t.amount)
  redis.call('EXPIRE', t.key, 120)
end
if req.root then
  local v = redis.call('INCRBY', req.root.key, req.root.amount)
  redis.call('EXPIRE', req.root.key, 86400)
  return cjson.encode({ok=true, root_used=v})
end
return cjson.encode({ok=true})
"""


@dataclass
class Reservation:
    money: list[dict] = field(default_factory=list)
    tpm: list[dict] = field(default_factory=list)
    root: dict | None = None
    tokens: int = 0
    cost_micro: int = 0
    fail_open: bool = False
    settled: bool = False


class BudgetManager:
    def __init__(self, redis_url: str, opa: OpaClient, timeout_s: float):
        self.redis = aioredis.Redis.from_url(
            redis_url, socket_timeout=timeout_s, socket_connect_timeout=timeout_s,
            retry=Retry(NoBackoff(), 0), health_check_interval=0)
        self.opa = opa
        self._reserve = self.redis.register_script(RESERVE_LUA)

    @staticmethod
    def _limits_for(snapshot: dict, ctx: RequestContext) -> list[tuple[str, dict]]:
        nodes = snapshot["budgets"]["nodes"]
        path = [(f"user:{ctx.use_case_id}:{ctx.end_user}", snapshot["budgets"]["user_defaults"])]
        path += [(nid, nodes[nid]) for nid in snapshot["budgets"]["paths"][ctx.use_case_id]]
        return path

    def build(self, snapshot: dict, ctx: RequestContext, tokens: int, cost_usd: float, model: str) -> Reservation:
        period = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        minute = int(time.time() // 60)
        cost_micro = max(1, round(cost_usd * MICRO))
        res = Reservation(tokens=tokens, cost_micro=cost_micro)
        for node_id, lim in self._limits_for(snapshot, ctx):
            res.money.append({"key": f"spend:{period}:{node_id}", "node": node_id, "amount": cost_micro,
                              "floor": round(lim["floor_usd"] * MICRO), "ceiling": round(lim["ceiling_usd"] * MICRO),
                              "fail_mode": lim["fail_mode"]})
            tpm = lim["tokens_per_minute"]
            res.tpm.append({"key": f"tpm:{minute}:{node_id}:{model}", "node": node_id, "amount": tokens,
                            "limit": int(tpm.get(model, tpm["default"])), "fail_mode": lim["fail_mode"]})
        if ctx.root_budget_tokens is not None:
            res.root = {"key": f"root:{ctx.root_request_id}", "amount": tokens, "limit": int(ctx.root_budget_tokens),
                        "fail_mode": "closed"}
        return res

    async def reserve(self, snapshot: dict, ctx: RequestContext, tokens: int, cost_usd: float, model: str,
                      span) -> Reservation:
        res = self.build(snapshot, ctx, tokens, cost_usd, model)
        payload = {"money": [{k: m[k] for k in ("key", "node", "amount", "floor", "ceiling")} for m in res.money],
                   "tpm": [{k: t[k] for k in ("key", "node", "amount", "limit")} for t in res.tpm]}
        if res.root:
            payload["root"] = {k: res.root[k] for k in ("key", "amount", "limit")}
        span.set_attribute("gov.budget.estimated_tokens", tokens)
        span.set_attribute("gov.budget.estimated_cost", cost_usd)
        try:
            out = json.loads(await self._reserve(keys=[], args=[json.dumps(payload)]))
        except (RedisError, OSError) as exc:
            return await self._fail_mode(res, ctx, span, exc)
        if not out["ok"]:
            ctx.evidence["budget"] = out
            span.set_attribute("gov.budget.refused_node", str(out.get("node", "root")))
            raise GovRefusal(429, "budget", out["reason"], detail={k: v for k, v in out.items() if k != "ok"})
        if "root_used" in out:
            ctx.attrs["gov.root_request.used_tokens"] = int(out["root_used"])
        span.set_attribute("gov.budget.path", ",".join(m["node"] for m in res.money))
        return res

    async def _fail_mode(self, res: Reservation, ctx: RequestContext, span, exc: Exception) -> Reservation:
        limits = [{"name": f"money:{m['node']}", "fail_mode": m["fail_mode"]} for m in res.money]
        limits += [{"name": f"tpm:{t['node']}", "fail_mode": t["fail_mode"]} for t in res.tpm]
        if res.root:
            limits.append({"name": f"root:{ctx.root_request_id}", "fail_mode": res.root["fail_mode"]})
        decision = await self.opa.decide("gov/budget/decision", {"limits": limits})
        span.set_attribute("gov.budget.store_error", type(exc).__name__)
        span.set_attribute("gov.budget.fail_mode", decision["mode"])
        ctx.attrs["gov.budget.fail_mode"] = decision["mode"]
        ctx.evidence["budget_fail_mode"] = {"mode": decision["mode"], "closed_limits": decision["closed_limits"],
                                            "error": type(exc).__name__}
        if not decision["allow"]:
            raise GovRefusal(429, "budget", "budget_store_unavailable",
                             detail={"fail_mode": "closed", "closed_limits": decision["closed_limits"]})
        res.fail_open = True
        return res

    async def reconcile(self, res: Reservation, actual_tokens: int, actual_cost_usd: float) -> None:
        if res.settled or res.fail_open:
            return
        res.settled = True
        d_tokens = actual_tokens - res.tokens
        d_cost = max(0, round(actual_cost_usd * MICRO)) - res.cost_micro
        if d_tokens == 0 and d_cost == 0:
            return
        try:
            async with self.redis.pipeline(transaction=True) as p:
                for m in res.money:
                    p.incrby(m["key"], d_cost)
                for t in res.tpm:
                    p.incrby(t["key"], d_tokens)
                if res.root:
                    p.incrby(res.root["key"], d_tokens)
                await p.execute()
        except (RedisError, OSError):
            pass  # store went away after reservation; the reservation stands (conservative)

    async def release(self, res: Reservation | None) -> None:
        if res is not None:
            await self.reconcile(res, 0, 0.0)

    async def preload(self) -> None:
        """Load the Lua script into Redis' script cache so the first EVALSHA does not miss."""
        try:
            await self.redis.script_load(RESERVE_LUA)
        except (RedisError, OSError):
            pass

    async def ping(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except (RedisError, OSError):
            return False
