"""Control-plane fleet chat: active agent target + WS proxy.

When the dashboard runs as a control plane (near Mongo), browser chat must
not use the local agent loop — it proxies JSON-RPC ``/api/ws`` to the
messaging owner's advertised ``api_base``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_control_plane() -> bool:
    return os.environ.get("HERMES_CONTROL_PLANE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _storage():
    from hermes_storage import get_storage, is_mongo_mode

    if not is_mongo_mode():
        raise HTTPException(status_code=409, detail="Mongo mode required")
    return get_storage(force=True)


def resolve_active_chat_target(storage=None) -> dict[str, Any]:
    """Pick the node that should receive web chat (messaging owner)."""
    from hermes_storage.api_base import normalize_api_base, probe_chat_ready

    storage = storage or _storage()
    status = storage.cluster_status()
    state = status.get("state") or {}
    owner = state.get("messaging_owner") or state.get("active_node_id")
    nodes = status.get("nodes") or []
    match = None
    for node in nodes:
        if node.get("node_id") == owner:
            match = node
            break
    api_base = normalize_api_base(str((match or {}).get("api_base") or ""))
    health = probe_chat_ready(api_base) if api_base else {"ok": False, "reason": "missing_api_base"}
    return {
        "control_plane": _is_control_plane(),
        "owner_node_id": owner,
        "handoff_state": state.get("handoff_state") or "idle",
        "active_node_id": state.get("active_node_id"),
        "hostname": (match or {}).get("hostname"),
        "machine_id": (match or {}).get("machine_id"),
        "api_base": api_base,
        "chat_ready": bool(health.get("ok")),
        "health": health,
        "online": bool((match or {}).get("online")),
        "proxy_path": "/api/fleet/ws",
        "fleet_proxy_configured": _fleet_secret_ok(),
    }


def _fleet_secret_ok() -> bool:
    try:
        from hermes_storage.fleet_proxy_auth import fleet_proxy_configured

        return fleet_proxy_configured()
    except Exception:
        return False


@router.get("/api/fleet/active-chat")
async def fleet_active_chat():
    """Return the messaging owner + api_base for control-plane chat routing."""
    return resolve_active_chat_target()


@router.get("/api/fleet/status")
async def fleet_status():
    """Control-plane summary: cluster + wiki count + active chat target."""
    storage = _storage()
    status = storage.cluster_status()
    wiki_n = 0
    try:
        if getattr(storage, "wiki", None) is not None:
            wiki_n = len(storage.wiki.list_pages(limit=5000))
    except Exception:
        pass
    chat = resolve_active_chat_target(storage)
    return {
        "control_plane": _is_control_plane(),
        "cluster": status,
        "wiki_pages": wiki_n,
        "active_chat": chat,
    }


class WikiPutBody(BaseModel):
    title: str
    body: str = ""
    slug: Optional[str] = None
    tags: Optional[list[str]] = None


@router.get("/api/fleet/wiki")
async def fleet_wiki_list(tag: Optional[str] = None):
    storage = _storage()
    if storage.wiki is None:
        raise HTTPException(status_code=501, detail="wiki store unavailable")
    return {"pages": storage.wiki.list_pages(tag=tag)}


@router.get("/api/fleet/wiki/{slug}")
async def fleet_wiki_show(slug: str):
    storage = _storage()
    if storage.wiki is None:
        raise HTTPException(status_code=501, detail="wiki store unavailable")
    page = storage.wiki.get_page(slug)
    if not page:
        raise HTTPException(status_code=404, detail="page not found")
    return page


@router.post("/api/fleet/wiki")
async def fleet_wiki_put(body: WikiPutBody):
    storage = _storage()
    page = storage.put_wiki_page(
        title=body.title,
        body=body.body,
        slug=body.slug,
        tags=list(body.tags or []),
    )
    return {"ok": True, "page": page}


@router.websocket("/api/fleet/ws")
async def fleet_ws_proxy(ws: WebSocket):
    """Proxy browser JSON-RPC WS to the active agent's /api/ws."""
    import json as _json

    # Reuse dashboard session auth for the *browser* side.
    from hermes_cli import web_server as _ws_mod

    async def _notify(method: str, params: dict, *, code: int = 1013) -> None:
        await ws.accept()
        await ws.send_text(
            _json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        )
        await ws.close(code=code)

    if not _ws_mod._ws_auth_ok(ws):
        await ws.close(code=4401)
        return

    try:
        target = resolve_active_chat_target()
    except HTTPException:
        await ws.close(code=4409)
        return

    if target.get("handoff_state") not in {"idle", "done", None, ""}:
        # Allow proxy during handoff only if owner already has api_base;
        # otherwise ask client to retry.
        if not target.get("chat_ready"):
            await _notify("fleet.handoff", {"state": target.get("handoff_state")})
            return

    api_base = target.get("api_base") or ""
    if not api_base or not target.get("chat_ready"):
        await _notify(
            "fleet.error",
            {
                "error": (
                    "active agent has no reachable api_base; "
                    "set HERMES_API_BASE on the agent and run hermes serve"
                )
            },
        )
        return

    if not _fleet_secret_ok():
        await _notify(
            "fleet.error",
            {
                "error": (
                    "HERMES_FLEET_PROXY_SECRET not configured on control plane and agents"
                )
            },
            code=4403,
        )
        return

    from hermes_storage.api_base import http_to_ws_base
    from hermes_storage.fleet_proxy_auth import authorization_header_value

    ws_base = http_to_ws_base(api_base)
    # Prefer short-lived ticket over raw secret; never put credentials in the URL
    # (access logs). Browser auth stays on the control-plane session/ticket path.
    auth_hdr = authorization_header_value(
        owner_node_id=str(target.get("owner_node_id") or ""),
        api_base=api_base,
    )
    if not auth_hdr:
        await _notify(
            "fleet.error",
            {"error": "HERMES_FLEET_PROXY_SECRET not configured"},
            code=4403,
        )
        return
    upstream_url = f"{ws_base}/api/ws"

    try:
        import websockets
    except ImportError:
        await _notify(
            "fleet.error",
            {"error": "websockets package required for fleet proxy"},
        )
        return

    await ws.accept()
    try:
        async with websockets.connect(
            upstream_url,
            additional_headers={"Authorization": auth_hdr},
            open_timeout=8,
            max_size=16 * 1024 * 1024,
        ) as upstream:
            await _pipe_ws(ws, upstream)
    except Exception as exc:
        logger.warning("fleet ws proxy failed: %s", exc)
        try:
            await ws.send_text(
                _json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "fleet.error",
                        "params": {"error": str(exc)[:300]},
                    }
                )
                + "\n"
            )
        except Exception:
            pass
        try:
            await ws.close(code=1011)
        except Exception:
            pass


async def _pipe_ws(client: WebSocket, upstream: Any) -> None:
    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await client.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                text = msg.get("text")
                data = msg.get("bytes")
                if text is not None:
                    await upstream.send(text)
                elif data is not None:
                    await upstream.send(data)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("client→upstream closed", exc_info=True)

    async def upstream_to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await client.send_bytes(message)
                else:
                    await client.send_text(message)
        except Exception:
            logger.debug("upstream→client closed", exc_info=True)

    t1 = asyncio.create_task(client_to_upstream())
    t2 = asyncio.create_task(upstream_to_client())
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except Exception:
            pass
