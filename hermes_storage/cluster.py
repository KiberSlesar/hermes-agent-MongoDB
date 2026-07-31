"""Cluster presence, handoff watcher helpers, and agent-facing API."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from hermes_storage.factory import get_storage

logger = logging.getLogger(__name__)

_HEARTBEAT_THREAD: Optional[threading.Thread] = None
_HEARTBEAT_STOP = threading.Event()
_RUNTIME_STATE_CB: Optional[Callable[[], dict[str, Any]]] = None
_RELEASE_CB: Optional[Callable[[], None]] = None
_ACQUIRE_CB: Optional[Callable[[], bool]] = None
_NOTIFY_CB: Optional[Callable[[str], None]] = None
# True after a successful local acquire (or when release has not cleared it).
# Used so a gateway that started deferred can connect once it becomes owner
# even if handoff_state is already idle (missed the acquiring tick).
_LOCAL_MESSAGING_HELD: bool = False

# Platforms that stay up on every fleet node (not Telegram/Discord lease).
NON_MESSAGING_PLATFORMS = frozenset({"api_server", "local", "webhook"})


def is_messaging_platform(platform: Any) -> bool:
    name = getattr(platform, "value", None) or str(platform)
    return str(name).strip().lower() not in NON_MESSAGING_PLATFORMS


def should_connect_messaging(storage: Any = None) -> bool:
    """Whether this node should hold live Telegram/Discord adapters now."""
    storage = storage or get_storage()
    if storage is None:
        return True
    state = storage.cluster.get_state() or {}
    node_id = storage.node_id
    handoff = state.get("handoff_state") or "idle"
    if handoff == "acquiring" and state.get("handoff_to") == node_id:
        return True
    if handoff == "releasing" and state.get("handoff_from") == node_id:
        # Still the live owner until release completes.
        return True
    owner = state.get("messaging_owner")
    if owner:
        return owner == node_id
    active = state.get("active_node_id")
    return active in (None, "", node_id)


def ensure_local_gateway_service() -> dict[str, Any]:
    """Best-effort start of the local messaging gateway process/service.

    Used when this node is selected as active but the gateway process has not
    registered acquire/release callbacks yet. Safe to call repeatedly.
    """
    result: dict[str, Any] = {"started": False, "already_running": False}
    try:
        from hermes_cli.gateway import find_gateway_pids

        pids = find_gateway_pids() or []
        if pids:
            result["already_running"] = True
            result["pids"] = list(pids)
            return result
    except Exception as exc:
        logger.debug("Could not inspect gateway PIDs: %s", exc)

    import shutil
    import subprocess
    import sys

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        cmd = [hermes_bin, "gateway", "start"]
    else:
        cmd = [sys.executable, "-m", "hermes_cli.main", "gateway", "start"]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        result["started"] = True
        result["cmd"] = cmd
        logger.info("Started local gateway for active-agent handoff: %s", cmd)
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("Failed to auto-start local gateway: %s", exc)
    return result


def format_cluster_prompt_block(status: Optional[dict[str, Any]] = None) -> str:
    """Compact system-prompt block describing the agent fleet."""
    storage = get_storage()
    if storage is None:
        return ""
    status = status or storage.cluster_status()
    state = status.get("state") or {}
    nodes = status.get("nodes") or []
    lines = [
        "CLUSTER (Hermes multi-PC fleet)",
        f"- This node: {status.get('this_node_id')} (machine {status.get('this_machine_id')})",
        f"- Active node: {state.get('active_node_id') or 'none'}",
        f"- Messaging owner: {state.get('messaging_owner') or 'none'}",
        f"- Handoff: {state.get('handoff_state') or 'idle'}",
        "- Online instances:",
    ]
    online = [n for n in nodes if n.get("online")]
    if not online:
        lines.append("  (none)")
    else:
        for n in online:
            caps = ",".join(n.get("capabilities") or []) or "-"
            marker = " *" if n.get("node_id") == state.get("active_node_id") else ""
            lines.append(
                f"  - {n.get('hostname') or n.get('machine_id')} "
                f"[{n.get('node_id')}] caps={caps}{marker}"
            )
    lines.append(
        "Use cluster_status / cluster_activate tools (or /cluster) to inspect "
        "or switch the active agent. Messaging gateway moves with a lease "
        "handoff so only one Telegram/Discord gateway is live."
    )
    return "\n".join(lines)


def start_heartbeat_loop(
    *,
    interval_s: float = 15.0,
    api_base: Optional[str] = None,
) -> None:
    """Background presence heartbeat for the current process."""
    global _HEARTBEAT_THREAD
    storage = get_storage()
    if storage is None:
        return
    _HEARTBEAT_STOP.clear()

    def _loop() -> None:
        while not _HEARTBEAT_STOP.is_set():
            try:
                runtime = {}
                if _RUNTIME_STATE_CB:
                    try:
                        runtime = _RUNTIME_STATE_CB() or {}
                    except Exception:
                        logger.debug("Cluster runtime-state callback failed", exc_info=True)
                storage.register_presence(
                    api_base=api_base,
                    active_turns=runtime.get("active_turns", 0),
                    active_session_keys=runtime.get("active_session_keys"),
                )
                # Also publish presence over mTLS orchestrator when configured
                try:
                    from hermes_storage.orchestrator_client import (
                        orch_heartbeat,
                        orchestrator_configured,
                    )
                    if orchestrator_configured():
                        orch_heartbeat({
                            "node_id": storage.node_id,
                            "machine_id": storage.machine_id,
                            "profile": storage.bootstrap.profile,
                            "api_base": api_base or "",
                            "status": "online",
                            "active_turns": runtime.get("active_turns", 0),
                            "active_session_keys": runtime.get("active_session_keys") or [],
                        })
                except Exception as orch_exc:
                    logger.debug("Orchestrator heartbeat skipped: %s", orch_exc)
                _maybe_handle_handoff(storage)
                _maybe_reconcile_messaging(storage)
                _maybe_failover(storage)
            except Exception as exc:
                logger.warning("Cluster heartbeat failed: %s", exc)
            _HEARTBEAT_STOP.wait(interval_s)

    if _HEARTBEAT_THREAD and _HEARTBEAT_THREAD.is_alive():
        return
    _HEARTBEAT_THREAD = threading.Thread(target=_loop, name="hermes-cluster-hb", daemon=True)
    _HEARTBEAT_THREAD.start()


def stop_heartbeat_loop() -> None:
    _HEARTBEAT_STOP.set()


def _maybe_handle_handoff(storage: Any) -> None:
    """Drive messaging lease release/acquire via gateway callbacks."""
    global _LOCAL_MESSAGING_HELD
    state = storage.cluster.get_state()
    handoff = state.get("handoff_state")
    node_id = storage.node_id

    if handoff == "releasing" and state.get("handoff_from") == node_id:
        source = next(
            (
                node for node in storage.cluster.list_nodes(online_within_s=60.0)
                if node.get("node_id") == node_id
            ),
            {},
        )
        if int(source.get("active_turns") or 0) > 0:
            logger.info(
                "Deferring cluster handoff while %d foreground task(s) finish",
                int(source.get("active_turns") or 0),
            )
            return
        cb = _RELEASE_CB
        if cb is None:
            # No local gateway — treat messaging as already released so the
            # target can acquire.
            _LOCAL_MESSAGING_HELD = False
            logger.info(
                "No local messaging gateway; marking release complete for handoff"
            )
            storage.cluster.mark_messaging_released(node_id)
            return
        try:
            cb()
        except Exception as exc:
            logger.error("Messaging release callback failed: %s", exc)
            storage.cluster.rollback_messaging_handoff(reason=str(exc))
            return
        storage.cluster.mark_messaging_released(node_id)
        state = storage.cluster.get_state()
        handoff = state.get("handoff_state")

    # Dead source never runs the release path — force-release so the target
    # can acquire (VM powered off, process killed, etc.).
    if handoff == "releasing":
        source_id = state.get("handoff_from")
        target_id = state.get("handoff_to")
        if source_id and source_id != node_id:
            nodes = {
                n["node_id"]: n
                for n in storage.cluster.list_nodes(online_within_s=45.0)
            }
            source = nodes.get(source_id) or {}
            if not source.get("online"):
                logger.warning(
                    "Handoff source %s is offline; force-releasing messaging "
                    "lease toward %s",
                    source_id,
                    target_id,
                )
                storage.cluster.mark_messaging_released(source_id)
                state = storage.cluster.get_state()
                handoff = state.get("handoff_state")

    if handoff == "acquiring" and state.get("handoff_to") == node_id:
        _run_acquire_for_handoff(storage, node_id)


def _run_acquire_for_handoff(storage: Any, node_id: str) -> None:
    """Complete an in-progress acquiring handoff via the gateway callback."""
    global _LOCAL_MESSAGING_HELD
    cb = _ACQUIRE_CB
    if cb is None:
        # Becoming active without a live gateway: start it and wait for
        # the next heartbeat once acquire callbacks are registered.
        info = ensure_local_gateway_service()
        logger.info(
            "Active agent handoff waiting for local gateway acquire "
            "(auto-start=%s already_running=%s)",
            info.get("started"),
            info.get("already_running"),
        )
        return
    ok = True
    err = None
    try:
        ok = bool(cb())
    except Exception as exc:
        ok = False
        err = str(exc)
    if ok:
        _LOCAL_MESSAGING_HELD = True
        storage.cluster.complete_messaging_handoff(node_id)
        notify = _NOTIFY_CB
        if notify:
            try:
                notify(
                    f"Active Hermes agent switched to this machine "
                    f"({storage.machine_id}). Messaging gateway is here now."
                )
            except Exception:
                pass
    else:
        _LOCAL_MESSAGING_HELD = False
        storage.cluster.rollback_messaging_handoff(
            reason=err or "messaging health-check failed"
        )
        notify = _NOTIFY_CB
        if notify:
            try:
                notify(
                    f"Failed to move messaging gateway here "
                    f"({err or 'health-check failed'}). Rolled back."
                )
            except Exception:
                pass


def _maybe_reconcile_messaging(storage: Any) -> None:
    """Connect messaging if we already own the lease but never acquired.

    A gateway that started as passive prepares Telegram/Discord deferred.
    If the lease lands here while handoff_state is already idle (missed
    acquiring, presence claim, or complete without a live connect), the
    acquiring branch never runs again — reconnect here.
    """
    global _LOCAL_MESSAGING_HELD
    if _LOCAL_MESSAGING_HELD:
        return
    if not should_connect_messaging(storage):
        return
    state = storage.cluster.get_state() or {}
    handoff = state.get("handoff_state") or "idle"
    if handoff not in (None, "idle", "done"):
        return
    if state.get("messaging_owner") != storage.node_id:
        return
    cb = _ACQUIRE_CB
    if cb is None:
        return
    logger.info(
        "This node owns messaging lease but adapters are not held locally; "
        "connecting deferred messaging platforms"
    )
    try:
        ok = bool(cb())
    except Exception as exc:
        logger.warning("Messaging reconcile acquire failed: %s", exc)
        return
    if ok:
        _LOCAL_MESSAGING_HELD = True
    else:
        logger.warning(
            "Messaging reconcile acquire returned failure; will retry next heartbeat"
        )


def _maybe_failover(storage: Any) -> None:
    state = storage.cluster.get_state()
    if (state.get("failover") or "auto") != "auto":
        return
    owner = state.get("messaging_owner")
    if not owner:
        return
    nodes = {n["node_id"]: n for n in storage.cluster.list_nodes(online_within_s=45.0)}
    owner_node = nodes.get(owner)
    if owner_node and owner_node.get("online"):
        return
    # Owner unhealthy — try next online node (prefer this node if online)
    candidates = [n for n in nodes.values() if n.get("online") and n["node_id"] != owner]
    if not candidates:
        return
    # Prefer self
    self_node = nodes.get(storage.node_id)
    target = self_node if self_node and self_node.get("online") else candidates[0]
    if target["node_id"] == storage.node_id:
        logger.warning(
            "Messaging owner %s unhealthy; initiating failover to %s",
            owner,
            target["node_id"],
        )
        storage.cluster.set_active(target["node_id"], reason="failover")
        # Failover uses set_active directly (not HermesStorage.activate), so
        # kick the local gateway here — otherwise handoff stalls without CB.
        ensure_local_gateway_service()


def register_messaging_callbacks(
    *,
    on_release: Optional[Callable[[], None]] = None,
    on_acquire: Optional[Callable[[], bool]] = None,
    on_notify: Optional[Callable[[str], None]] = None,
    runtime_state: Optional[Callable[[], dict[str, Any]]] = None,
) -> None:
    """Gateway registers stop/start/notify hooks for messaging handoff."""
    global _RELEASE_CB, _ACQUIRE_CB, _NOTIFY_CB, _RUNTIME_STATE_CB, _LOCAL_MESSAGING_HELD
    if on_release is not None:
        def _wrapped_release() -> None:
            global _LOCAL_MESSAGING_HELD
            try:
                on_release()
            finally:
                _LOCAL_MESSAGING_HELD = False

        _RELEASE_CB = _wrapped_release
    if on_acquire is not None:
        def _wrapped_acquire() -> bool:
            global _LOCAL_MESSAGING_HELD
            ok = bool(on_acquire())
            _LOCAL_MESSAGING_HELD = bool(ok)
            return ok

        _ACQUIRE_CB = _wrapped_acquire
    if on_notify is not None:
        _NOTIFY_CB = on_notify
    if runtime_state is not None:
        _RUNTIME_STATE_CB = runtime_state
