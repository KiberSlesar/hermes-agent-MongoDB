"""Auth for control-plane → agent ``hermes serve`` WebSocket proxy.

The browser never sees these credentials. Control plane attaches
``Authorization: Bearer <ticket-or-secret>`` when dialing the active
agent's ``/api/ws``. Agents accept:

1. Short-lived HMAC tickets minted by the control plane (preferred)
2. The shared secret itself (fallback / same-secret ops)

Configured via ``HERMES_FLEET_PROXY_SECRET`` or Mongo secret of the same name.
Never inject this into the SPA — that would expose home-PC tools to anyone
with the dashboard URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

SECRET_ENV = "HERMES_FLEET_PROXY_SECRET"
SECRET_MONGO_KEY = "HERMES_FLEET_PROXY_SECRET"
TICKET_PREFIX = "fleet1."
DEFAULT_TICKET_TTL_S = 60


def get_fleet_proxy_secret() -> str:
    env = os.environ.get(SECRET_ENV, "").strip()
    if env:
        return env
    try:
        from hermes_storage import get_storage, is_mongo_mode

        if not is_mongo_mode():
            return ""
        storage = get_storage()
        if storage is None:
            return ""
        secrets = {}
        if hasattr(storage, "get_effective_secrets"):
            secrets = storage.get_effective_secrets() or {}
        else:
            secrets = storage.secrets.get_all() or {}
        return str(secrets.get(SECRET_MONGO_KEY) or "").strip()
    except Exception:
        logger.debug("fleet proxy secret lookup failed", exc_info=True)
        return ""


def fleet_proxy_configured() -> bool:
    return bool(get_fleet_proxy_secret())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def mint_fleet_proxy_ticket(
    *,
    ttl_s: int = DEFAULT_TICKET_TTL_S,
    owner_node_id: str = "",
    api_base: str = "",
) -> str:
    """Mint a short-lived ticket for one control-plane → agent dial."""
    secret = get_fleet_proxy_secret()
    if not secret:
        return ""
    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + max(15, int(ttl_s)),
        "aud": "hermes-fleet-ws",
        "owner": owner_node_id or "",
        "api_base": (api_base or "")[:200],
    }
    body = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{TICKET_PREFIX}{body}.{_b64url(sig)}"


def verify_fleet_proxy_ticket(presented: str) -> bool:
    secret = get_fleet_proxy_secret()
    if not secret or not presented or not presented.startswith(TICKET_PREFIX):
        return False
    raw = presented[len(TICKET_PREFIX) :]
    if "." not in raw:
        return False
    body, sig_b64 = raw.rsplit(".", 1)
    try:
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return False
        payload = json.loads(_b64url_decode(body))
        exp = int(payload.get("exp") or 0)
        if exp < int(time.time()):
            return False
        if payload.get("aud") != "hermes-fleet-ws":
            return False
        return True
    except Exception:
        return False


def verify_fleet_proxy_credential(presented: Optional[str]) -> bool:
    """Accept either a short-lived ticket or the raw shared secret."""
    if not presented:
        return False
    if verify_fleet_proxy_ticket(presented):
        return True
    expected = get_fleet_proxy_secret()
    if not expected:
        return False
    return hmac.compare_digest(str(presented).encode(), expected.encode())


def authorization_header_value(*, owner_node_id: str = "", api_base: str = "") -> str:
    """Bearer value for upstream dial — prefers a fresh ticket."""
    ticket = mint_fleet_proxy_ticket(owner_node_id=owner_node_id, api_base=api_base)
    if ticket:
        return f"Bearer {ticket}"
    secret = get_fleet_proxy_secret()
    if not secret:
        return ""
    return f"Bearer {secret}"
