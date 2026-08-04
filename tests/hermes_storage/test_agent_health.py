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

    # TG + LLM down → low score
    bad = dict(checks)
    bad["llm_provider"] = {"ok": False, "applicable": True}
    bad["telegram_api"] = {"ok": False, "applicable": True}
    score = compute_health_score(bad)
    # 15+10 = 25 of 95 → ~26
    assert score < 40
    assert critical_checks_failed(bad) is True


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
