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
# True while GatewayRunner.start() is still wiring adapters. Blocks reconcile
# acquire so it cannot race the normal connect loop and open a second
# getUpdates session against the same bot token.
_GATEWAY_BOOTSTRAPPING: bool = False

# Platforms that stay up on every fleet node (not Telegram/Discord lease).
NON_MESSAGING_PLATFORMS = frozenset({"api_server", "local", "webhook"})


def set_gateway_bootstrapping(active: bool) -> None:
    global _GATEWAY_BOOTSTRAPPING
    _GATEWAY_BOOTSTRAPPING = bool(active)


def mark_local_messaging_held(held: bool = True) -> None:
    global _LOCAL_MESSAGING_HELD
    _LOCAL_MESSAGING_HELD = bool(held)


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
    pending = state.get("pending_active_node_id")
    lines = [
        "CLUSTER (Hermes multi-PC fleet)",
        f"- This node: {status.get('this_node_id')} (machine {status.get('this_machine_id')})",
        f"- Active node: {state.get('active_node_id') or 'none'}",
        f"- Messaging owner: {state.get('messaging_owner') or 'none'}",
        f"- Handoff: {state.get('handoff_state') or 'idle'}"
        + (f" → pending {pending}" if pending else ""),
        "- Tools and Telegram for gateway chats run on the messaging owner.",
        "- After cluster_activate, THIS turn stays on the current node; the "
        "user's next message runs on the target only after handoff completes.",
        "- Online instances:",
    ]
    online = [n for n in nodes if n.get("online")]
    if not online:
        lines.append("  (none)")
    else:
        for n in online:
            caps = ",".join(n.get("capabilities") or []) or "-"
            marker = " *" if n.get("node_id") == state.get("active_node_id") else ""
            api = (n.get("api_base") or "").strip()
            chat = "chat_ready" if n.get("chat_ready") else ("no_api_base" if not api else "api_base_set")
            score = n.get("health_score")
            score_s = f" health={score}" if score is not None else ""
            lines.append(
                f"  - {n.get('hostname') or n.get('machine_id')} "
                f"[{n.get('node_id')}] caps={caps} {chat}{score_s}"
                f"{(' ' + api) if api else ''}{marker}"
            )
    lines.append(
        "Use cluster_status / cluster_activate tools (or /cluster) to inspect "
        "or switch the active agent. Messaging gateway moves with a lease "
        "handoff so only one Telegram/Discord gateway is live. Web chat on "
        "the control plane follows messaging_owner's advertised api_base."
    )
    return "\n".join(lines)


def start_heartbeat_loop(
    *,
    interval_s: float = 15.0,
    api_base: Optional[str] = None,
) -> None:
    """Background presence heartbeat for the current process.

    ``api_base`` may be omitted; each tick re-resolves via
    :func:`hermes_storage.api_base.resolve_advertise_api_base` so serve can
    advertise its bind URL after startup.
    """
    global _HEARTBEAT_THREAD
    storage = get_storage()
    if storage is None:
        return
    _HEARTBEAT_STOP.clear()

    def _loop() -> None:
        while not _HEARTBEAT_STOP.is_set():
            try:
                from hermes_storage.api_base import resolve_advertise_api_base

                runtime = {}
                if _RUNTIME_STATE_CB:
                    try:
                        runtime = _RUNTIME_STATE_CB() or {}
                    except Exception:
                        logger.debug("Cluster runtime-state callback failed", exc_info=True)
                advertised = (api_base or "").strip() or resolve_advertise_api_base()
                storage.register_presence(
                    api_base=advertised or None,
                    active_turns=runtime.get("active_turns", 0),
                    active_session_keys=runtime.get("active_session_keys"),
                )
                # Also publish presence over mTLS orchestrator when configured.
                # Idle auto-update is intentionally OFF — agents run `hermes update`.
                try:
                    from hermes_storage.fleet_update import presence_version_fields
                    from hermes_storage.orchestrator_client import (
                        orch_heartbeat,
                        orchestrator_configured,
                    )

                    health_fields = {}
                    try:
                        from hermes_storage.agent_health import presence_health_fields

                        health_fields = presence_health_fields()
                    except Exception:
                        logger.debug("orch health fields failed", exc_info=True)
                    if orchestrator_configured():
                        orch_heartbeat({
                            "node_id": storage.node_id,
                            "machine_id": storage.machine_id,
                            "profile": storage.bootstrap.profile,
                            "hostname": __import__("socket").gethostname(),
                            "api_base": advertised or "",
                            "status": "online",
                            "active_turns": runtime.get("active_turns", 0),
                            "active_session_keys": runtime.get("active_session_keys") or [],
                            **presence_version_fields(),
                            **health_fields,
                        })
                except Exception as orch_exc:
                    logger.debug("Orchestrator heartbeat skipped: %s", orch_exc)
                _maybe_handle_handoff(storage)
                _maybe_reconcile_messaging(storage)
                _maybe_failover(storage)
                _maybe_failback(storage)
                _maybe_health_rebalance(storage)
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


def format_cluster_move_notice(
    storage: Any,
    *,
    from_node_id: Optional[str] = None,
    to_node_id: Optional[str] = None,
) -> str:
    """User-facing system line when messaging ownership moves."""
    nodes = {
        n.get("node_id"): n
        for n in (storage.cluster.list_nodes(online_within_s=300.0) or [])
    }

    def _label(node_id: Optional[str]) -> str:
        if not node_id:
            return "unknown"
        node = nodes.get(node_id) or {}
        host = node.get("hostname") or node.get("machine_id") or node_id
        return f"{host} [{node_id}]"

    to_id = to_node_id or storage.node_id
    text = (
        "🔀 Системное сообщение: активный агент переехал на "
        f"{_label(to_id)}"
        + (f" (было: {_label(from_node_id)})" if from_node_id else "")
        + ". Telegram и инструменты теперь выполняются на этой машине."
    )

    try:
        from hermes_storage.fleet_update import (
            format_version_skew_warning,
            normalize_release,
        )

        desired = {}
        if getattr(storage, "fleet_release", None) is not None:
            desired = normalize_release(storage.fleet_release.get())
        to_node = nodes.get(to_id) or {}
        skew = format_version_skew_warning(
            agent_version=str(to_node.get("agent_version") or ""),
            install_ref=str(to_node.get("install_ref") or ""),
            desired=desired,
        )
        if skew:
            text = text + " " + skew
    except Exception:
        logger.debug("version skew notice append failed", exc_info=True)
    return text


def _node_is_online(
    storage: Any,
    node_id: Optional[str],
    *,
    within_s: float = 45.0,
) -> bool:
    if not node_id:
        return False
    nodes = {
        n.get("node_id"): n
        for n in (storage.cluster.list_nodes(online_within_s=within_s) or [])
    }
    return bool((nodes.get(node_id) or {}).get("online"))


def _notify_cluster(notice: str, session_keys: Optional[list] = None) -> None:
    notify = _NOTIFY_CB
    if not notify:
        return
    keys = list(session_keys or [])
    try:
        notify(notice, keys)
    except TypeError:
        try:
            notify(notice)
        except Exception:
            logger.debug("Cluster notify failed", exc_info=True)
    except Exception:
        logger.debug("Cluster notify failed", exc_info=True)


def _complete_handoff_as_owner(
    storage: Any,
    node_id: str,
    *,
    from_id: Optional[str],
    session_keys: Optional[list],
    messaging_held: bool,
    degraded_reason: Optional[str] = None,
) -> None:
    """Finish handoff onto ``node_id`` (optionally with degraded messaging)."""
    global _LOCAL_MESSAGING_HELD
    _LOCAL_MESSAGING_HELD = bool(messaging_held)
    notice = format_cluster_move_notice(
        storage, from_node_id=from_id, to_node_id=node_id
    )
    if degraded_reason:
        notice = (
            notice
            + " ⚠️ Messaging platforms failed to connect "
            f"({degraded_reason}); web/tools stay on this node — Telegram "
            "will retry. Ownership was NOT rolled back to an offline agent."
        )
    storage.cluster.complete_messaging_handoff(node_id)
    _notify_cluster(notice, session_keys)


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
    pre = storage.cluster.get_state() or {}
    session_keys = list(pre.get("handoff_session_keys") or [])
    from_id = pre.get("handoff_from")
    if ok:
        _complete_handoff_as_owner(
            storage,
            node_id,
            from_id=from_id,
            session_keys=session_keys,
            messaging_held=True,
        )
        return

    # Messaging health-check failed. Never roll ownership back onto a dead
    # previous owner — web UI / tools still need a live agent. Reconcile will
    # keep retrying Telegram/Discord while we hold the lease.
    _LOCAL_MESSAGING_HELD = False
    reason = err or "messaging health-check failed"
    if not _node_is_online(storage, from_id):
        logger.warning(
            "Messaging acquire failed (%s) but previous owner %s is offline; "
            "completing handoff to %s in degraded mode",
            reason,
            from_id,
            node_id,
        )
        _complete_handoff_as_owner(
            storage,
            node_id,
            from_id=from_id,
            session_keys=session_keys,
            messaging_held=False,
            degraded_reason=reason,
        )
        return

    storage.cluster.rollback_messaging_handoff(reason=reason)
    mark = getattr(storage.cluster, "mark_health_rebalance", None)
    if callable(mark):
        try:
            # Reuse cooldown field so failover doesn't immediately retry.
            mark()
        except Exception:
            logger.debug("mark cooldown after rollback failed", exc_info=True)
    _notify_cluster(
        "⚠️ Системное сообщение: не удалось перенести активного агента "
        f"сюда ({reason}). Handoff откатан.",
        [],
    )


def _maybe_reconcile_messaging(storage: Any) -> None:
    """Connect messaging if we already own the lease but never acquired.

    A gateway that started as passive prepares Telegram/Discord deferred.
    If the lease lands here while handoff_state is already idle (missed
    acquiring, presence claim, or complete without a live connect), the
    acquiring branch never runs again — reconnect here.
    """
    global _LOCAL_MESSAGING_HELD
    if _GATEWAY_BOOTSTRAPPING:
        return
    if not should_connect_messaging(storage):
        # Stale hold: we lost the lease but still think we own adapters.
        _maybe_drop_stale_messaging(storage)
        return
    if _LOCAL_MESSAGING_HELD:
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


def _maybe_drop_stale_messaging(storage: Any) -> None:
    """Disconnect messaging if we still hold it after losing the lease."""
    global _LOCAL_MESSAGING_HELD
    if not _LOCAL_MESSAGING_HELD:
        return
    state = storage.cluster.get_state() or {}
    handoff = state.get("handoff_state") or "idle"
    if handoff == "releasing" and state.get("handoff_from") == storage.node_id:
        return
    cb = _RELEASE_CB
    if cb is None:
        _LOCAL_MESSAGING_HELD = False
        return
    logger.warning(
        "This node is not messaging owner but still holds adapters; releasing"
    )
    try:
        cb()
    except Exception:
        logger.warning("Stale messaging release failed", exc_info=True)
    _LOCAL_MESSAGING_HELD = False


def _node_health_score(node: dict[str, Any]) -> int:
    try:
        return int(node.get("health_score") or 0)
    except (TypeError, ValueError):
        return 0


def _pick_best_online_candidate(
    nodes: list[dict[str, Any]] | dict[str, dict[str, Any]],
    *,
    exclude: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Pick online node with highest health_score (hostname tie-break)."""
    if isinstance(nodes, dict):
        values = list(nodes.values())
    else:
        values = list(nodes or [])
    candidates = [
        n for n in values
        if n.get("online") and n.get("node_id") and n.get("node_id") != exclude
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda n: (
            _node_health_score(n),
            str(n.get("hostname") or n.get("node_id") or ""),
        ),
        reverse=True,
    )
    return candidates[0]


def _cluster_health_settings() -> dict[str, float]:
    hysteresis = 20.0
    min_score = 40.0
    cooldown = 600.0
    try:
        from hermes_cli.config import load_config

        cluster = (load_config() or {}).get("cluster") or {}
        if cluster.get("health_rebalance_hysteresis") is not None:
            hysteresis = float(cluster["health_rebalance_hysteresis"])
        if cluster.get("health_rebalance_min_score") is not None:
            min_score = float(cluster["health_rebalance_min_score"])
        if cluster.get("health_rebalance_cooldown_s") is not None:
            cooldown = float(cluster["health_rebalance_cooldown_s"])
    except Exception:
        logger.debug("cluster health settings read failed", exc_info=True)
    return {
        "hysteresis": hysteresis,
        "min_score": min_score,
        "cooldown_s": cooldown,
    }


def _owner_is_degraded(owner_node: dict[str, Any], *, min_score: float) -> bool:
    """True when messaging ownership should consider leaving this owner.

    Telegram reachability is the hard gate for rebalance. LLM-only probe
    failures (AuthError mid-reload, missing /models) must not yank the lease
    while Telegram still works — that caused the Windows↔Linux thrash loop.
    """
    checks = owner_node.get("health_checks") or {}
    tg = checks.get("telegram_api") or {}
    if tg.get("applicable") is not False and tg.get("ok") is False:
        return True
    # Very low overall score with TG unknown/missing can still qualify.
    if _node_health_score(owner_node) <= float(min_score):
        # But if TG is explicitly healthy, require score <= half the threshold.
        if tg.get("ok") is True:
            return _node_health_score(owner_node) <= float(min_score) / 2.0
        return True
    return False


def _node_is_updating(node: dict[str, Any]) -> bool:
    status = str(node.get("update_status") or "").strip().lower()
    return status in {"applying", "pending", "downloading", "installing"}


def _maybe_failover(storage: Any) -> None:
    state = storage.cluster.get_state()
    if (state.get("failover") or "auto") != "auto":
        return
    # Cool down after a failed acquire so we don't thrash every ~90s.
    if _health_rebalance_cooldown_active(state, 180.0) and state.get("handoff_error"):
        return
    owner = state.get("messaging_owner")
    if not owner:
        return
    nodes = {n["node_id"]: n for n in storage.cluster.list_nodes(online_within_s=45.0)}
    owner_node = nodes.get(owner)
    if owner_node and owner_node.get("online"):
        return
    # Owner unhealthy — pick the highest health_score among online peers.
    target = _pick_best_online_candidate(nodes, exclude=owner)
    if not target:
        return
    if _node_is_updating(target):
        return
    # Only the chosen best node initiates (single initiator, no race).
    if target["node_id"] != storage.node_id:
        return
    logger.warning(
        "Messaging owner %s unhealthy; initiating failover to %s (health=%s)",
        owner,
        target["node_id"],
        _node_health_score(target),
    )
    # Remember where to return when the preferred/home node wakes up.
    ensure = getattr(storage.cluster, "ensure_preferred_messaging_node", None)
    if callable(ensure):
        try:
            ensure(owner)
        except Exception:
            logger.debug("ensure_preferred_messaging_node failed", exc_info=True)
    storage.cluster.set_active(target["node_id"], reason="failover")
    # Failover uses set_active directly (not HermesStorage.activate), so
    # kick the local gateway here — otherwise handoff stalls without CB.
    ensure_local_gateway_service()


def _maybe_health_rebalance(storage: Any) -> None:
    """Move lease to a healthier online peer when the owner is degraded.

    Preferred failback still wins when the home node returns; this only
    reshuffles among temporary owners while preferred is away.
    """
    state = storage.cluster.get_state() or {}
    if (state.get("failover") or "auto") != "auto":
        return
    handoff = state.get("handoff_state") or "idle"
    if handoff not in (None, "idle", "done"):
        return
    owner = state.get("messaging_owner")
    if not owner:
        return
    settings = _cluster_health_settings()
    if _health_rebalance_cooldown_active(state, settings["cooldown_s"]):
        return
    nodes = {
        n["node_id"]: n
        for n in storage.cluster.list_nodes(online_within_s=45.0)
    }
    owner_node = nodes.get(owner) or {}
    if not owner_node.get("online"):
        return
    if _node_is_updating(owner_node):
        return
    if int(owner_node.get("active_turns") or 0) > 0:
        return
    preferred = (state.get("preferred_messaging_node") or "").strip()
    # Do not steal from preferred while it is online (failback owns that path).
    if preferred and preferred == owner and owner_node.get("online"):
        return
    if not _owner_is_degraded(owner_node, min_score=settings["min_score"]):
        return
    best = _pick_best_online_candidate(nodes, exclude=None)
    if not best or best.get("node_id") == owner:
        return
    if _node_is_updating(best):
        return
    owner_score = _node_health_score(owner_node)
    best_score = _node_health_score(best)
    if best_score < owner_score + settings["hysteresis"]:
        return
    # Only the best peer initiates.
    if best["node_id"] != storage.node_id:
        return
    logger.warning(
        "Health rebalance: owner %s score=%s degraded; moving to %s score=%s",
        owner,
        owner_score,
        best["node_id"],
        best_score,
    )
    mark = getattr(storage.cluster, "mark_health_rebalance", None)
    if callable(mark):
        try:
            mark()
        except Exception:
            logger.debug("mark_health_rebalance failed", exc_info=True)
    try:
        storage.cluster.set_active(best["node_id"], reason="health_rebalance")
    except RuntimeError as exc:
        logger.info("Health rebalance deferred: %s", exc)
        return
    ensure_local_gateway_service()


def _health_rebalance_cooldown_active(state: dict[str, Any], cooldown_s: float) -> bool:
    raw = state.get("last_health_rebalance_at")
    if raw is None:
        return False
    try:
        from datetime import datetime, timezone

        if isinstance(raw, datetime):
            ts = raw
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            return age < float(cooldown_s)
        # ISO string
        text = str(raw).replace("Z", "+00:00")
        ts = datetime.fromisoformat(text)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age < float(cooldown_s)
    except Exception:
        return False


def _maybe_failback(storage: Any) -> None:
    """Return messaging lease to preferred node once it is online again.

    Only the preferred node initiates (same "prefer self" pattern as failover)
    so two online agents do not race.
    """
    state = storage.cluster.get_state() or {}
    if (state.get("failover") or "auto") != "auto":
        return
    handoff = state.get("handoff_state") or "idle"
    if handoff not in (None, "idle", "done"):
        return
    preferred = (state.get("preferred_messaging_node") or "").strip()
    if not preferred:
        return
    owner = state.get("messaging_owner") or state.get("active_node_id")
    if not owner or preferred == owner:
        return
    if storage.node_id != preferred:
        return
    if not _node_is_online(storage, preferred):
        return
    # Prefer waiting until current owner is idle (no in-flight turns).
    nodes = {
        n.get("node_id"): n
        for n in (storage.cluster.list_nodes(online_within_s=45.0) or [])
    }
    current = nodes.get(owner) or {}
    if int(current.get("active_turns") or 0) > 0:
        return
    logger.info(
        "Preferred messaging node %s is back online; failback from %s",
        preferred,
        owner,
    )
    try:
        storage.cluster.set_active(preferred, reason="failback")
    except RuntimeError as exc:
        logger.info("Failback deferred: %s", exc)
        return
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
