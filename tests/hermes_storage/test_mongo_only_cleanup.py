"""Mongo-only fork: scrub leftovers, skills root, durable write blocks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_storage.mongo_only import (
    classic_allowed,
    is_classic_durable_path,
    require_mongo_mode,
    scrub_classic_durable_home,
)
from hermes_storage.errors import MongoStorageError


def test_require_mongo_mode_raises_without_bootstrap(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_ALLOW_CLASSIC", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_storage.mongo_only.is_mongo_mode", lambda: False)
    with pytest.raises(MongoStorageError, match="requires Mongo mode"):
        require_mongo_mode(surface="test")


def test_require_mongo_mode_allows_classic_escape(monkeypatch):
    monkeypatch.setenv("HERMES_ALLOW_CLASSIC", "1")
    monkeypatch.setattr("hermes_storage.mongo_only.is_mongo_mode", lambda: False)
    require_mongo_mode(surface="test")  # does not raise
    assert classic_allowed() is True


def test_scrub_classic_durable_home_quarantines_leftovers(tmp_path):
    (tmp_path / "config.yaml").write_text("model: x\n", encoding="utf-8")
    (tmp_path / ".env").write_text("K=v\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# soul\n", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "SKILL.md").write_text("x", encoding="utf-8")
    (tmp_path / "memories").mkdir()
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text("{}", encoding="utf-8")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "skills").mkdir()
    (tmp_path / "bootstrap.yaml").write_text("mongo_uri: x\n", encoding="utf-8")

    result = scrub_classic_durable_home(tmp_path)
    assert result["moved"] >= 5
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "skills").exists()
    assert (tmp_path / "bootstrap.yaml").exists()
    assert (tmp_path / "cache" / "skills").is_dir()
    assert (tmp_path / ".orphan" / "config.yaml").exists()
    assert (tmp_path / ".orphan" / "skills").exists()


def test_skills_sync_client_uses_writable_skills_dir(monkeypatch, tmp_path):
    cache = tmp_path / "cache" / "skills"
    cache.mkdir(parents=True)
    monkeypatch.setattr(
        "hermes_storage.skills_sync.writable_skills_dir",
        lambda: cache,
    )
    from tools.skills_sync_client import _skills_dir

    assert _skills_dir() == cache


def test_is_classic_durable_path_detects_soul_and_skills(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    assert is_classic_durable_path(home / "SOUL.md", home=home)
    assert is_classic_durable_path(home / "skills" / "x" / "SKILL.md", home=home)
    assert not is_classic_durable_path(home / "cache" / "skills" / "x", home=home)
    assert not is_classic_durable_path(home / "bootstrap.yaml", home=home)


def test_check_sensitive_path_blocks_classic_durable_in_mongo(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    soul = home / "SOUL.md"
    soul.write_text("x", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_ALLOW_CLASSIC", raising=False)
    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)
    monkeypatch.setattr("hermes_storage.classic_allowed", lambda: False)

    import tools.file_tools as ft

    monkeypatch.setattr(ft, "_resolve_path_for_task", lambda p, t="default": Path(p))
    err = ft._check_sensitive_path(str(soul))
    assert err is not None
    assert "MongoDB fork" in err or "Mongo" in err
