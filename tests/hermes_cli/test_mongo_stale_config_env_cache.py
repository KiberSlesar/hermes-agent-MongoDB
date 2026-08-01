"""Same-process stale-read regressions for Mongo config/env caches."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest

import hermes_cli.config as cfg


@pytest.fixture(autouse=True)
def _clear_caches():
    cfg._invalidate_load_config_cache()
    cfg.invalidate_env_cache()
    yield
    cfg._invalidate_load_config_cache()
    cfg.invalidate_env_cache()


def test_mongo_load_config_ignores_leftover_yaml_mtime(tmp_path, monkeypatch):
    """Leftover config.yaml must not pin a stale Mongo merge in-process."""
    home = tmp_path / ".hermes"
    home.mkdir()
    leftover = home / "config.yaml"
    leftover.write_text("model:\n  default: leftover-local\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cfg, "get_hermes_home", lambda: home)
    monkeypatch.setattr(cfg, "get_config_path", lambda: leftover)
    monkeypatch.setattr(cfg, "ensure_hermes_home", lambda: None)
    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)

    storage = MagicMock()
    storage.load_effective_config.side_effect = lambda base: {
        **(base or {}),
        "model": {"default": "mongo-v1"},
    }
    monkeypatch.setattr("hermes_storage.require_storage", lambda: storage)

    first = cfg.load_config()
    assert first["model"]["default"] == "mongo-v1"

    # Simulate overlay/profile write that does not touch leftover yaml.
    storage.load_effective_config.side_effect = lambda base: {
        **(base or {}),
        "model": {"default": "mongo-v2"},
    }
    cfg._invalidate_load_config_cache()

    second = cfg.load_config()
    assert second["model"]["default"] == "mongo-v2"


def test_mongo_load_config_stale_without_invalidate_is_the_bug_we_fixed(
    tmp_path, monkeypatch
):
    """Without generation bump, leftover-yaml-keyed cache would stick (guard)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    leftover = home / "config.yaml"
    leftover.write_text("display:\n  skin: ares\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cfg, "get_hermes_home", lambda: home)
    monkeypatch.setattr(cfg, "get_config_path", lambda: leftover)
    monkeypatch.setattr(cfg, "ensure_hermes_home", lambda: None)
    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)

    storage = MagicMock()
    storage.load_effective_config.side_effect = lambda base: {
        **copy.deepcopy(base or {}),
        "display": {"skin": "mongo-old"},
    }
    monkeypatch.setattr("hermes_storage.require_storage", lambda: storage)

    assert cfg.load_config()["display"]["skin"] == "mongo-old"

    storage.load_effective_config.side_effect = lambda base: {
        **copy.deepcopy(base or {}),
        "display": {"skin": "mongo-new"},
    }
    # Generation still old → cache hit on (_MONGO_CONFIG_CACHE_GEN, …).
    # After our fix the gen is part of the sig; without invalidate we still
    # expect the OLD value (proves the cache is active). With invalidate → new.
    stale = cfg.load_config()
    assert stale["display"]["skin"] == "mongo-old"

    cfg._invalidate_load_config_cache()
    fresh = cfg.load_config()
    assert fresh["display"]["skin"] == "mongo-new"


def test_get_env_value_prefers_mongo_over_stale_os_environ(monkeypatch):
    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)
    monkeypatch.setenv("DEMO_API_KEY", "stale-from-shell")

    storage = MagicMock()
    storage.get_effective_secrets.return_value = {"DEMO_API_KEY": "fresh-from-mongo"}
    monkeypatch.setattr("hermes_storage.require_storage", lambda: storage)
    monkeypatch.setattr("hermes_storage.get_storage", lambda: storage)

    assert cfg.get_env_value("DEMO_API_KEY") == "fresh-from-mongo"
    # Runtime-only keys still fall through to os.environ.
    monkeypatch.setenv("HERMES_RUNTIME_ONLY", "1")
    storage.get_effective_secrets.return_value = {}
    assert cfg.get_env_value("HERMES_RUNTIME_ONLY") == "1"
