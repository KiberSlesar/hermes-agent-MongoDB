"""Mongo offline write outbox — spool + flush without live Mongo."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_storage.errors import MongoStorageError
from hermes_storage.outbox import (
    KIND_PROFILE_CONFIG,
    KIND_SECRET_REMOVE,
    KIND_SECRET_SET,
    KIND_SOUL,
    apply_secret_overlay,
    clear_secret_overlay_keys,
    enqueue,
    flush_outbox,
    pending_count,
    run_or_enqueue,
)


@pytest.fixture(autouse=True)
def _outbox_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clear_secret_overlay_keys()
    yield
    clear_secret_overlay_keys()


def test_enqueue_persists_under_cache_mongo_outbox(tmp_path):
    entry = enqueue(KIND_SOUL, {"content": "# hello"})
    assert entry["kind"] == KIND_SOUL
    entries = list((tmp_path / "cache" / "mongo_outbox" / "entries").glob("*.json"))
    assert len(entries) == 1
    assert pending_count() == 1


def test_coalesce_replaces_same_logical_key(tmp_path):
    enqueue(KIND_SOUL, {"content": "v1"})
    enqueue(KIND_SOUL, {"content": "v2"})
    assert pending_count() == 1
    from hermes_storage.outbox import list_pending

    [entry] = list_pending()
    assert entry["payload"]["content"] == "v2"


def test_secret_set_and_remove_coalesce(tmp_path):
    enqueue(KIND_SECRET_SET, {"key": "API_KEY", "value": "a"})
    enqueue(KIND_SECRET_REMOVE, {"key": "API_KEY"})
    assert pending_count() == 1
    from hermes_storage.outbox import list_pending

    [entry] = list_pending()
    assert entry["kind"] == KIND_SECRET_REMOVE
    assert apply_secret_overlay({}) == {}  # removed in overlay


def test_secret_overlay_visible_while_queued(tmp_path):
    enqueue(KIND_SECRET_SET, {"key": "TOKEN", "value": "secret"})
    merged = apply_secret_overlay({"OTHER": "1"})
    assert merged["TOKEN"] == "secret"
    assert merged["OTHER"] == "1"


def test_run_or_enqueue_queues_on_transient_error(tmp_path):
    def _fail():
        raise MongoStorageError("MongoDB unavailable: connection refused")

    status = run_or_enqueue(KIND_PROFILE_CONFIG, {"config": {"model": "x"}}, _fail)
    assert status["ok"] is True
    assert status["queued"] is True
    assert pending_count() == 1


def test_run_or_enqueue_reraises_non_transient(tmp_path):
    def _fail():
        raise ValueError("bad schema")

    with pytest.raises(ValueError, match="bad schema"):
        run_or_enqueue(KIND_SOUL, {"content": "x"}, _fail)
    assert pending_count() == 0


def test_run_or_enqueue_succeeds_without_spool(tmp_path):
    calls = []

    def _ok():
        calls.append(1)

    status = run_or_enqueue(KIND_SOUL, {"content": "x"}, _ok)
    assert status == {"ok": True, "queued": False}
    assert calls == [1]
    assert pending_count() == 0


def test_flush_outbox_replays_and_clears(tmp_path, monkeypatch):
    enqueue(KIND_SOUL, {"content": "persisted soul"})
    enqueue(KIND_PROFILE_CONFIG, {"config": {"display": {"skin": "mono"}}})

    soul_puts = []
    config_puts = []

    storage = MagicMock()
    storage.skills.list_skills.return_value = []
    storage.soul.put.side_effect = lambda doc: soul_puts.append(doc)
    storage.config.put.side_effect = lambda doc: config_puts.append(doc)

    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)
    monkeypatch.setattr("hermes_storage.require_storage", lambda: storage)

    result = flush_outbox()
    assert result["flushed"] == 2
    assert result["failed"] == 0
    assert result["remaining"] == 0
    assert soul_puts == [{"content": "persisted soul"}]
    assert config_puts == [{"display": {"skin": "mono"}}]
    assert pending_count() == 0


def test_flush_stops_on_first_failure_preserves_order(tmp_path, monkeypatch):
    enqueue(KIND_SOUL, {"content": "a"})
    enqueue(KIND_PROFILE_CONFIG, {"config": {"x": 1}})

    storage = MagicMock()
    storage.skills.list_skills.return_value = []
    storage.soul.put.side_effect = RuntimeError("still down")

    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)
    monkeypatch.setattr("hermes_storage.require_storage", lambda: storage)

    result = flush_outbox()
    assert result["flushed"] == 0
    assert result["failed"] == 1
    assert result["remaining"] == 2
    storage.config.put.assert_not_called()


def test_flush_skipped_when_mongo_probe_fails(tmp_path, monkeypatch):
    enqueue(KIND_SOUL, {"content": "a"})

    storage = MagicMock()
    storage.skills.list_skills.side_effect = MongoStorageError("unavailable")

    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)
    monkeypatch.setattr("hermes_storage.require_storage", lambda: storage)

    result = flush_outbox()
    assert result["flushed"] == 0
    assert result["remaining"] == 1


def test_skill_put_stores_blobs(tmp_path):
    from hermes_storage.outbox import KIND_SKILL_PUT, list_pending

    enqueue(
        KIND_SKILL_PUT,
        {"name": "demo", "skill_md": "# Demo\n"},
        blob_files={"SKILL.md": b"# Demo\n", "scripts/run.py": b"print(1)\n"},
    )
    [entry] = list_pending()
    blob_root = tmp_path / "cache" / "mongo_outbox" / "blobs" / entry["id"]
    assert (blob_root / "SKILL.md").read_text(encoding="utf-8") == "# Demo\n"
    assert (blob_root / "scripts" / "run.py").is_file()
