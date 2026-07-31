"""Agent tools for multi-PC cluster status and activate."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

CLUSTER_STATUS_SCHEMA = {
    "name": "cluster_status",
    "description": (
        "List Hermes agent instances across PCs: who is online, who is the "
        "active agent, who owns the messaging gateway (Telegram/etc), and "
        "handoff state. Use before switching or when the user asks which PCs "
        "are available."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

CLUSTER_ACTIVATE_SCHEMA = {
    "name": "cluster_activate",
    "description": (
        "Switch the active Hermes agent to another PC/instance. Accepts "
        "node_id, machine_id, or hostname. Starts a messaging lease handoff "
        "(old PC releases Telegram after the current turn finishes; target "
        "acquires next). IMPORTANT: this turn's tools keep running on the "
        "CURRENT PC — do not claim you already execute on the target. Tell "
        "the user handoff started and that their NEXT message will run there "
        "once messaging_owner flips (check cluster_status)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "node_id, machine_id, or hostname to activate",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for the switch",
            },
        },
        "required": ["target"],
    },
}


def cluster_status_tool(**kwargs) -> str:
    from hermes_storage import get_storage, is_mongo_mode
    from hermes_storage.orchestrator_client import orch_cluster_status, orchestrator_configured

    if not is_mongo_mode():
        return json.dumps({
            "success": False,
            "error": "Mongo/cluster mode is not enabled (no bootstrap.yaml / HERMES_MONGO_URI).",
        })

    # Prefer mTLS orchestrator when configured — plaintext / no-cert is refused.
    if orchestrator_configured():
        try:
            status = orch_cluster_status()
            if status.get("ok") is False:
                return json.dumps({"success": False, **status}, default=str)
            return json.dumps({"success": True, "via": "orchestrator_mtls", **status}, default=str)
        except Exception as exc:
            return json.dumps({
                "success": False,
                "error": str(exc),
                "hint": "Orchestrator requires a valid agent client certificate (mTLS).",
            })

    storage = get_storage()
    if storage is None:
        return json.dumps({"success": False, "error": "Storage unavailable"})
    status = storage.cluster_status()
    return json.dumps({"success": True, "via": "mongo", **status}, default=str)


def cluster_activate_tool(target: str = "", reason: str = "agent", **kwargs) -> str:
    from hermes_storage import get_storage, is_mongo_mode
    from hermes_storage.orchestrator_client import orch_cluster_activate, orchestrator_configured

    if not is_mongo_mode():
        return json.dumps({
            "success": False,
            "error": "Mongo/cluster mode is not enabled.",
        })

    if not target or not str(target).strip():
        return json.dumps({"success": False, "error": "target is required"})

    if orchestrator_configured():
        try:
            result = orch_cluster_activate(str(target).strip(), reason=reason or "agent")
            ok = result.get("ok", True) and not result.get("error")
            return json.dumps({
                "success": bool(ok),
                "via": "orchestrator_mtls",
                "message": f"Activation via orchestrator toward {target}.",
                **result,
            }, default=str)
        except Exception as exc:
            return json.dumps({
                "success": False,
                "error": str(exc),
                "hint": "Orchestrator requires a valid agent client certificate (mTLS).",
            })

    storage = get_storage()
    if storage is None:
        return json.dumps({"success": False, "error": "Storage unavailable"})

    # Honor agent_can_activate policy from shared settings
    settings = storage.settings.get("default") or {}
    cluster_cfg = settings.get("cluster") if isinstance(settings, dict) else None
    if isinstance(cluster_cfg, dict) and cluster_cfg.get("agent_can_activate") is False:
        return json.dumps({
            "success": False,
            "error": "cluster.agent_can_activate is disabled by policy.",
        })

    announce_keys = []
    try:
        from gateway.session_context import get_session_env

        sk = (get_session_env("HERMES_SESSION_KEY", "") or "").strip()
        if sk:
            announce_keys.append(sk)
    except Exception:
        pass

    try:
        state = storage.activate(
            str(target).strip(),
            reason=reason or "agent",
            announce_session_keys=announce_keys or None,
        )
        handoff = state.get("handoff_state") or "idle"
        owner = state.get("messaging_owner")
        pending = state.get("pending_active_node_id")
        # Activate always *starts* a handoff; completion is async via heartbeat.
        return json.dumps({
            "success": True,
            "via": "mongo",
            "handoff_started": True,
            "handoff_complete": False,
            "message": (
                f"Handoff started toward {target}. "
                f"messaging_owner is still {owner!r}; pending={pending!r}; "
                f"handoff_state={handoff!r}. "
                "This turn continues on the current node. After handoff "
                "completes, a system message is posted in chat and the user's "
                "next Telegram message runs on the target."
            ),
            "state": state,
            "announce_session_keys": announce_keys,
            "execution_stays_here_until_next_message": True,
        }, default=str)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def check_cluster_requirements() -> bool:
    from hermes_storage import is_mongo_mode
    return is_mongo_mode()


# --- Registry ---
from tools.registry import registry

registry.register(
    name="cluster_status",
    toolset="cluster",
    schema=CLUSTER_STATUS_SCHEMA,
    handler=lambda args, **kw: cluster_status_tool(**(args or {})),
    check_fn=check_cluster_requirements,
    emoji="🖥️",
)

registry.register(
    name="cluster_activate",
    toolset="cluster",
    schema=CLUSTER_ACTIVATE_SCHEMA,
    handler=lambda args, **kw: cluster_activate_tool(
        target=(args or {}).get("target", ""),
        reason=(args or {}).get("reason", "agent"),
    ),
    check_fn=check_cluster_requirements,
    emoji="🔀",
)
