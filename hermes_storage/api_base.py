"""Resolve the URL this node advertises for remote chat (hermes serve).

Control-plane UI proxies browser WebSockets to the messaging owner's
``api_base``. Agents must publish a reachable URL via heartbeat.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Set by ``start_server`` once the dashboard/serve bind address is known.
_PROCESS_API_BASE: Optional[str] = None


def set_process_api_base(url: Optional[str]) -> None:
    """Record the URL this process is listening on (called from serve/dashboard)."""
    global _PROCESS_API_BASE
    cleaned = (url or "").strip().rstrip("/")
    _PROCESS_API_BASE = cleaned or None
    if _PROCESS_API_BASE:
        os.environ["HERMES_API_BASE"] = _PROCESS_API_BASE
        logger.info("Advertised api_base for fleet chat: %s", _PROCESS_API_BASE)


def get_process_api_base() -> Optional[str]:
    return _PROCESS_API_BASE


def normalize_api_base(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def resolve_advertise_api_base() -> str:
    """Best-effort public URL for this node's ``hermes serve``.

    Precedence:
    1. ``HERMES_API_BASE`` / ``HERMES_SERVE_URL`` env
    2. ``cluster.api_base`` in config.yaml
    3. Process bind URL recorded by ``set_process_api_base``
    """
    for key in ("HERMES_API_BASE", "HERMES_SERVE_URL"):
        val = os.environ.get(key, "").strip()
        if val:
            return normalize_api_base(val)

    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        cluster = cfg.get("cluster") if isinstance(cfg, dict) else None
        if isinstance(cluster, dict):
            val = str(cluster.get("api_base") or "").strip()
            if val:
                return normalize_api_base(val)
    except Exception:
        logger.debug("resolve_advertise_api_base: config read failed", exc_info=True)

    if _PROCESS_API_BASE:
        return normalize_api_base(_PROCESS_API_BASE)
    return ""


def http_to_ws_base(api_base: str) -> str:
    """``http://host:port`` → ``ws://host:port`` (https → wss)."""
    base = normalize_api_base(api_base)
    if not base:
        return ""
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return ""


def probe_chat_ready(api_base: str, *, timeout_s: float = 2.0) -> dict:
    """Lightweight health check against a node's serve HTTP API."""
    base = normalize_api_base(api_base)
    if not base:
        return {"ok": False, "reason": "missing_api_base"}
    url = base + "/api/health"
    try:
        import urllib.request

        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if int(code) < 500:
                return {"ok": True, "status": int(code), "url": url}
            return {"ok": False, "reason": f"http_{code}", "url": url}
    except Exception as exc:
        # /api/health may not exist — try root
        try:
            import urllib.request

            req = urllib.request.Request(base + "/", method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                return {"ok": True, "status": int(code), "url": base + "/", "fallback": True}
        except Exception:
            return {"ok": False, "reason": str(exc)[:200], "url": url}
