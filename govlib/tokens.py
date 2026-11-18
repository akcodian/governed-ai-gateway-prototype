"""JWT helpers (EdDSA / Ed25519).

* Workload tokens are issued by a simulated identity provider (key ``idp``).
* Root-budget tokens and downstream scoped tokens are issued by the gateway (key ``gateway_tokens``).
  The downstream token simulates an RFC 8693 token exchange: the gateway exchanges the caller's
  credential for a token whose audience is one downstream server and which carries the
  originating use case and root request ID.
"""
from __future__ import annotations

import time
import uuid
from functools import lru_cache

import jwt

from govlib import keys

IDP_ISSUER = "synthetic-idp"
GATEWAY_ISSUER = "governed-gateway"
GATEWAY_AUDIENCE = "governed-gateway"


@lru_cache(maxsize=None)
def _priv(name: str) -> bytes:
    return keys.load_private_pem(name)


@lru_cache(maxsize=None)
def _pub(name: str) -> bytes:
    return keys.load_public_pem(name)


def issue_workload_token(client_id: str, ttl_s: int = 3600, extra: dict | None = None) -> str:
    now = int(time.time())
    claims = {"iss": IDP_ISSUER, "sub": client_id, "aud": GATEWAY_AUDIENCE, "iat": now, "exp": now + ttl_s,
              "jti": uuid.uuid4().hex}
    if extra:
        claims.update(extra)
    return jwt.encode(claims, _priv("idp"), algorithm="EdDSA")


def verify_workload_token(token: str) -> dict:
    return jwt.decode(token, _pub("idp"), algorithms=["EdDSA"], audience=GATEWAY_AUDIENCE, issuer=IDP_ISSUER)


def issue_root_token(root_request_id: str, use_case_id: str, root_budget_tokens: int | None, ttl_s: int = 3600) -> str:
    now = int(time.time())
    claims = {"iss": GATEWAY_ISSUER, "aud": "gateway:root", "typ": "root_budget", "rrid": root_request_id,
              "uc": use_case_id, "budget": root_budget_tokens, "iat": now, "exp": now + ttl_s}
    return jwt.encode(claims, _priv("gateway_tokens"), algorithm="EdDSA")


def verify_root_token(token: str) -> dict:
    return jwt.decode(token, _pub("gateway_tokens"), algorithms=["EdDSA"], audience="gateway:root",
                      issuer=GATEWAY_ISSUER)


def issue_downstream_token(*, audience: str, use_case_id: str, root_request_id: str, root_budget_tokens: int | None,
                           hop: int, scope: str, actor: str, ttl_s: int = 300) -> str:
    """Simulated RFC 8693 token exchange result, scoped to one downstream server."""
    now = int(time.time())
    claims = {"iss": GATEWAY_ISSUER, "aud": audience, "typ": "downstream", "uc": use_case_id,
              "rrid": root_request_id, "budget": root_budget_tokens, "hop": hop, "scope": scope,
              "act": {"sub": actor}, "iat": now, "exp": now + ttl_s, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, _priv("gateway_tokens"), algorithm="EdDSA")


def verify_downstream_token(token: str, audience: str) -> dict:
    return jwt.decode(token, _pub("gateway_tokens"), algorithms=["EdDSA"], audience=audience,
                      issuer=GATEWAY_ISSUER)
