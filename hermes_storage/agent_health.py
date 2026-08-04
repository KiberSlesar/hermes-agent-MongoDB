"""Agent self-diagnostics for Mongo fleet failover priority.

Each online gateway publishes a weighted ``health_score`` (0–100) via presence.
Critical checks (LLM provider, Telegram API) outweigh optional vision/STT/TTS.
Unset optional services are neutral (excluded from the denominator).
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

HEALTH_INTERVAL_S = 300.0
PROBE_TIMEOUT_S = 4.0

# Relative weights — only applicable checks enter the denominator.
WEIGHTS: dict[str, int] = {
    "llm_provider": 35,
    "telegram_api": 35,
    "mongo": 15,
    "api_base": 10,
    "vision": 5,
    "stt": 5,
    "tts": 5,
}

# Messaging ownership only treats Telegram as hard-critical. LLM probe noise
# (AuthError mid-reload) must not flip health_critical_failed and thrash.
CRITICAL_CHECKS = frozenset({"telegram_api"})

_CACHE: dict[str, Any] = {}
_CACHE_AT = 0.0
_LOCK = __import__("threading").Lock()


def health_interval_s() -> float:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        cluster = cfg.get("cluster") or {}
        raw = cluster.get("health_interval_s")
        if raw is not None:
            return max(30.0, float(raw))
    except Exception:
        logger.debug("health_interval_s config read failed", exc_info=True)
    env = (os.environ.get("HERMES_HEALTH_INTERVAL_S") or "").strip()
    if env:
        try:
            return max(30.0, float(env))
        except ValueError:
            pass
    return HEALTH_INTERVAL_S


def _now() -> float:
    return time.time()


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_result(
    ok: bool,
    *,
    latency_ms: Optional[float] = None,
    detail: str = "",
    applicable: bool = True,
) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": bool(ok), "applicable": bool(applicable)}
    if latency_ms is not None:
        out["latency_ms"] = round(float(latency_ms), 1)
    if detail:
        out["detail"] = str(detail)[:160]
    return out


def _http_get(
    url: str,
    *,
    timeout_s: float = PROBE_TIMEOUT_S,
    proxy_url: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[bool, float, str]:
    started = _now()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "hermes-agent-health")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        handlers = []
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
        with opener.open(req, timeout=timeout_s) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            _ = resp.read(256)
            ms = (_now() - started) * 1000.0
            ok = 200 <= int(code) < 500  # 401/403 still means endpoint reachable
            return ok, ms, f"http_{code}"
    except Exception as exc:
        ms = (_now() - started) * 1000.0
        return False, ms, type(exc).__name__


def probe_llm_provider() -> dict[str, Any]:
    """Resolve the same provider/runtime path as chat and light-probe it."""
    started = _now()
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="auto") or {}
        provider = str(
            runtime.get("requested_provider")
            or runtime.get("provider")
            or ""
        ).strip()
        api_key = str(runtime.get("api_key") or "").strip()
        base_url = str(runtime.get("base_url") or "").strip()
        if provider:
            if not api_key:
                # OAuth / process-backed runtimes may not expose a long-lived key.
                ms = (_now() - started) * 1000.0
                return _check_result(True, latency_ms=ms, detail=f"runtime={provider}")
            if not base_url:
                ms = (_now() - started) * 1000.0
                return _check_result(
                    True,
                    latency_ms=ms,
                    detail=f"runtime={provider}:key_ok",
                )
            models_url = base_url.rstrip("/") + "/models"
            ok, ms, detail = _http_get(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if not ok:
                # Some providers reject /models — key present still counts as soft-ok.
                return _check_result(
                    True,
                    latency_ms=ms,
                    detail=f"runtime={provider}:key_ok:{detail}",
                )
            return _check_result(True, latency_ms=ms, detail=f"runtime={provider}:{detail}")
    except Exception:
        logger.debug("probe_llm_provider runtime resolution failed", exc_info=True)

    # Fallback for older/partial paths that only expose auth-level resolution.
    provider = ""
    try:
        from hermes_cli.auth import resolve_provider

        provider = resolve_provider("auto")
    except Exception as exc:
        # AuthError during resolve is common mid-reload / OAuth edge cases.
        # Fall back to "any known provider secret present" so we don't thrash
        # messaging ownership while chat still works.
        if _env_any(
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "DEEPSEEK_API_KEY",
            "XAI_API_KEY",
            "NOUS_API_KEY",
        ):
            ms = (_now() - started) * 1000.0
            return _check_result(
                True,
                latency_ms=ms,
                detail=f"resolve:{type(exc).__name__}:keys_present",
            )
        return _check_result(False, detail=f"resolve:{type(exc).__name__}")

    if not provider:
        return _check_result(False, detail="no_provider")

    # Prefer a cheap models list when we can resolve a base URL + key.
    base_url = ""
    api_key = ""
    try:
        from hermes_cli.auth import resolve_api_key_provider_credentials

        creds = resolve_api_key_provider_credentials(provider) or {}
        api_key = str(creds.get("api_key") or "").strip()
        base_url = str(creds.get("base_url") or "").strip()
    except Exception:
        api_key = ""
        base_url = ""

    if not api_key:
        # OAuth / custom providers — treat successful resolve as ok.
        ms = (_now() - started) * 1000.0
        return _check_result(True, latency_ms=ms, detail=f"provider={provider}")

    if not base_url:
        ms = (_now() - started) * 1000.0
        return _check_result(True, latency_ms=ms, detail=f"provider={provider}:key_ok")

    models_url = base_url.rstrip("/") + "/models"
    ok, ms, detail = _http_get(
        models_url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if not ok:
        # Some providers reject /models — key present still counts as soft-ok.
        return _check_result(
            True,
            latency_ms=ms,
            detail=f"provider={provider}:key_ok:{detail}",
        )
    return _check_result(True, latency_ms=ms, detail=f"provider={provider}:{detail}")


def probe_telegram_api() -> dict[str, Any]:
    proxy = None
    try:
        from gateway.platforms.base import resolve_proxy_url

        proxy = resolve_proxy_url(
            "TELEGRAM_PROXY", target_hosts=["api.telegram.org"]
        )
    except Exception:
        proxy = (os.environ.get("TELEGRAM_PROXY") or "").strip() or None
    ok, ms, detail = _http_get(
        "https://api.telegram.org/",
        proxy_url=proxy,
    )
    return _check_result(ok, latency_ms=ms, detail=detail)


def probe_mongo() -> dict[str, Any]:
    try:
        from hermes_storage import get_storage, is_mongo_mode

        if not is_mongo_mode():
            return _check_result(True, applicable=False, detail="not_mongo_mode")
        storage = get_storage()
        if storage is None or getattr(storage, "client", None) is None:
            return _check_result(False, detail="no_storage")
        started = _now()
        storage.client.admin.command("ping")
        ms = (_now() - started) * 1000.0
        return _check_result(True, latency_ms=ms, detail="ping_ok")
    except Exception as exc:
        return _check_result(False, detail=type(exc).__name__)


def probe_api_base() -> dict[str, Any]:
    try:
        from hermes_storage.api_base import (
            probe_chat_ready,
            resolve_advertise_api_base,
        )

        base = (resolve_advertise_api_base() or "").strip()
        if not base:
            # Messaging gateways often run without hermes serve — neutral.
            return _check_result(True, applicable=False, detail="no_serve_advertised")
        result = probe_chat_ready(base, timeout_s=PROBE_TIMEOUT_S)
        if result.get("ok"):
            return _check_result(True, detail="chat_ready")
        return _check_result(True, detail=f"advertised:{result.get('reason') or 'probe_fail'}")
    except Exception as exc:
        return _check_result(False, detail=type(exc).__name__)


def _env_any(*names: str) -> bool:
    for name in names:
        if (os.environ.get(name) or "").strip():
            return True
    return False


def probe_vision() -> dict[str, Any]:
    # Common vision-capable keys; unset → neutral.
    if not _env_any(
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        return _check_result(True, applicable=False, detail="not_configured")
    return _check_result(True, detail="configured")


def probe_stt() -> dict[str, Any]:
    if not _env_any(
        "OPENAI_API_KEY",
        "VOICE_TOOLS_OPENAI_KEY",
        "DEEPGRAM_API_KEY",
        "GROQ_API_KEY",
    ):
        # Local faster-whisper may still work — treat unset cloud keys as neutral.
        return _check_result(True, applicable=False, detail="not_configured")
    return _check_result(True, detail="configured")


def probe_tts() -> dict[str, Any]:
    if not _env_any(
        "OPENAI_API_KEY",
        "VOICE_TOOLS_OPENAI_KEY",
        "ELEVENLABS_API_KEY",
        "GOOGLE_API_KEY",
    ):
        return _check_result(True, applicable=False, detail="not_configured")
    return _check_result(True, detail="configured")


_PROBES: dict[str, Callable[[], dict[str, Any]]] = {
    "llm_provider": probe_llm_provider,
    "telegram_api": probe_telegram_api,
    "mongo": probe_mongo,
    "api_base": probe_api_base,
    "vision": probe_vision,
    "stt": probe_stt,
    "tts": probe_tts,
}


def compute_health_score(checks: dict[str, Any]) -> int:
    """Weighted score 0–100 from check results (non-applicable excluded)."""
    earned = 0
    total = 0
    for name, weight in WEIGHTS.items():
        row = checks.get(name) or {}
        if row.get("applicable") is False:
            continue
        total += weight
        if row.get("ok"):
            earned += weight
    if total <= 0:
        return 0
    return int(round(100.0 * earned / total))


def critical_checks_failed(checks: dict[str, Any]) -> bool:
    for name in CRITICAL_CHECKS:
        row = checks.get(name) or {}
        if row.get("applicable") is False:
            continue
        if not row.get("ok"):
            return True
    return False


def run_agent_health_checks(
    *,
    probes: Optional[dict[str, Callable[[], dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Run all probes and return score + per-check details."""
    table = probes or _PROBES
    checks: dict[str, Any] = {}
    budget_deadline = _now() + 8.0
    for name in WEIGHTS:
        fn = table.get(name)
        if fn is None:
            continue
        if _now() > budget_deadline:
            checks[name] = _check_result(False, detail="budget_exceeded")
            continue
        try:
            checks[name] = fn()
        except Exception as exc:
            checks[name] = _check_result(False, detail=type(exc).__name__)
    score = compute_health_score(checks)
    # Compact summary for presence (ok flags only + score).
    compact = {
        name: {
            "ok": bool((checks.get(name) or {}).get("ok")),
            "applicable": (checks.get(name) or {}).get("applicable", True),
        }
        for name in checks
    }
    return {
        "health_score": score,
        "health_checks": compact,
        "health_checked_at": _utc_iso(),
        "checks": checks,
        "critical_failed": critical_checks_failed(checks),
    }


def get_cached_health() -> dict[str, Any]:
    with _LOCK:
        return dict(_CACHE) if _CACHE else {}


def refresh_agent_health_if_due(*, force: bool = False) -> dict[str, Any]:
    """Refresh probes at most every health_interval_s; return presence fields."""
    global _CACHE, _CACHE_AT
    interval = health_interval_s()
    now = _now()
    with _LOCK:
        if (
            not force
            and _CACHE
            and (now - _CACHE_AT) < interval
        ):
            return {
                "health_score": int(_CACHE.get("health_score") or 0),
                "health_checks": dict(_CACHE.get("health_checks") or {}),
                "health_checked_at": str(_CACHE.get("health_checked_at") or ""),
                "critical_failed": bool(_CACHE.get("critical_failed")),
            }
    result = run_agent_health_checks()
    with _LOCK:
        _CACHE = dict(result)
        _CACHE_AT = now
    return {
        "health_score": int(result.get("health_score") or 0),
        "health_checks": dict(result.get("health_checks") or {}),
        "health_checked_at": str(result.get("health_checked_at") or ""),
        "critical_failed": bool(result.get("critical_failed")),
    }


def presence_health_fields(*, force: bool = False) -> dict[str, Any]:
    """Subset safe to embed in cluster heartbeat documents."""
    data = refresh_agent_health_if_due(force=force)
    checks = data.get("health_checks") or {}
    return {
        "health_score": int(data.get("health_score") or 0),
        "health_checks": checks,
        "health_checked_at": str(data.get("health_checked_at") or ""),
        "health_critical_failed": bool(data.get("critical_failed")),
    }


def reset_health_cache_for_tests() -> None:
    global _CACHE, _CACHE_AT
    with _LOCK:
        _CACHE = {}
        _CACHE_AT = 0.0
