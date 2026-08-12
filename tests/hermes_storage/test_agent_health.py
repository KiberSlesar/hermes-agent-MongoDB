"""Tests for agent health scoring used in fleet failover priority."""

from __future__ import annotations


def test_compute_score_weights_critical_and_skips_neutral():
    from hermes_storage.agent_health import compute_health_score, critical_checks_failed

    # All critical ok, optional unset (not applicable)
    checks = {
        "llm_provider": {"ok": True, "applicable": True},
        "telegram_api": {"ok": True, "applicable": True},
        "mongo": {"ok": True, "applicable": True},
        "api_base": {"ok": True, "applicable": True},
        "vision": {"ok": True, "applicable": False},
        "stt": {"ok": True, "applicable": False},
        "tts": {"ok": True, "applicable": False},
    }
    assert compute_health_score(checks) == 100
    assert critical_checks_failed(checks) is False

    # TG down → critical; LLM-only miss is score damage, not messaging-critical
    bad = dict(checks)
    bad["llm_provider"] = {"ok": False, "applicable": True}
    bad["telegram_api"] = {"ok": False, "applicable": True}
    score = compute_health_score(bad)
    # 15+10 = 25 of 95 → ~26
    assert score < 40
    assert critical_checks_failed(bad) is True

    llm_only = dict(checks)
    llm_only["llm_provider"] = {"ok": False, "applicable": True}
    assert critical_checks_failed(llm_only) is False


def test_optional_unset_does_not_penalize(monkeypatch):
    from hermes_storage import agent_health as ah

    ah.reset_health_cache_for_tests()

    def _llm():
        return ah._check_result(True, detail="ok")

    def _tg():
        return ah._check_result(True, detail="ok")

    def _mongo():
        return ah._check_result(True, applicable=False, detail="not_mongo")

    def _api():
        return ah._check_result(True, detail="adv")

    def _vision():
        return ah._check_result(True, applicable=False, detail="not_configured")

    def _stt():
        return ah._check_result(True, applicable=False, detail="not_configured")

    def _tts():
        return ah._check_result(True, applicable=False, detail="not_configured")

    probes = {
        "llm_provider": _llm,
        "telegram_api": _tg,
        "mongo": _mongo,
        "api_base": _api,
        "vision": _vision,
        "stt": _stt,
        "tts": _tts,
    }
    result = ah.run_agent_health_checks(probes=probes)
    assert result["health_score"] == 100
    assert result["health_checks"]["vision"]["applicable"] is False


def test_refresh_caches_until_interval(monkeypatch):
    from hermes_storage import agent_health as ah

    ah.reset_health_cache_for_tests()
    calls = {"n": 0}

    def _run():
        calls["n"] += 1
        return {
            "health_score": 77,
            "health_checks": {"telegram_api": {"ok": True, "applicable": True}},
            "health_checked_at": "t",
            "critical_failed": False,
        }

    monkeypatch.setattr(ah, "run_agent_health_checks", _run)
    monkeypatch.setattr(ah, "health_interval_s", lambda: 300.0)

    a = ah.refresh_agent_health_if_due()
    b = ah.refresh_agent_health_if_due()
    assert calls["n"] == 1
    assert a["health_score"] == 77
    assert b["health_score"] == 77
    c = ah.refresh_agent_health_if_due(force=True)
    assert calls["n"] == 2
    assert c["health_score"] == 77


def test_probe_llm_provider_uses_runtime_provider_for_named_custom(monkeypatch):
    from hermes_storage import agent_health as ah

    calls = {"requested": None}

    def _resolve_requested(requested=None):
        # Config-driven path: None → custom:codex.sale (not literal "auto").
        assert requested is None
        return "custom:codex.sale"

    def _resolve_runtime(requested=None):
        calls["requested"] = requested
        return {
            "provider": "custom",
            "requested_provider": "custom:codex.sale",
            "api_key": "secret",
            "base_url": "https://codex.sale/v1",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_requested_provider",
        _resolve_requested,
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        _resolve_runtime,
    )
    monkeypatch.setattr(
        ah,
        "_http_get",
        lambda url, **kwargs: (False, 12.5, "http_405"),
    )

    result = ah.probe_llm_provider()
    assert calls["requested"] == "custom:codex.sale"
    assert result["ok"] is True
    assert result["applicable"] is True
    assert result["detail"] == "runtime=custom:codex.sale:resolved:http_405"


def test_probe_llm_provider_must_not_pass_literal_auto(monkeypatch):
    """Passing requested='auto' skips config model.provider and false-fails."""
    from hermes_storage import agent_health as ah

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_requested_provider",
        lambda requested=None: "custom:codex.sale",
    )

    def _boom(requested=None):
        raise AssertionError(f"should use config provider, got {requested!r}")

    # If probe wrongly passes requested="auto", resolve_runtime is never reached
    # with the config id — guard the call args instead.
    seen = {}

    def _resolve_runtime(requested=None):
        seen["requested"] = requested
        return {
            "provider": "custom",
            "requested_provider": requested,
            "api_key": "k",
            "base_url": "https://example.test/v1",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        _resolve_runtime,
    )
    monkeypatch.setattr(ah, "_http_get", lambda *a, **k: (True, 1.0, "http_200"))
    result = ah.probe_llm_provider()
    assert seen["requested"] == "custom:codex.sale"
    assert seen["requested"] != "auto"
    assert result["ok"] is True
