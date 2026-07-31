"""Unit tests for hermes_storage (no live Mongo required)."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml


def test_mongo_session_doc_uses_sessiondb_timestamp_shape():
    """BSON datetimes must not leak through the SessionDB adapter."""
    from hermes_storage.session_bridge import _session_doc_to_sessiondb_shape

    moment = datetime(2026, 7, 31, 8, 33, 17, tzinfo=timezone.utc)
    row = _session_doc_to_sessiondb_shape(
        {"session_id": "s1", "started_at": moment, "updated_at": moment}
    )

    assert row is not None
    assert row["started_at"] == moment.timestamp()
    assert row["updated_at"] == moment.timestamp()
    assert row["last_active"] == moment.timestamp()


def test_mongo_get_resume_conversations_accepts_session_id():
    """CLI resume passes session_id positionally and unpacks a 2-tuple."""
    from hermes_storage.session_bridge import MongoSessionAdapter

    class _Store:
        def get_session(self, session_id):
            return {"session_id": session_id}

        def get_messages(self, session_id, include_inactive=False):
            return [
                {
                    "session_id": session_id,
                    "role": "user",
                    "content": "hi",
                    "message_index": 0,
                    "active": True,
                },
                {
                    "session_id": session_id,
                    "role": "assistant",
                    "content": "hello",
                    "message_index": 1,
                    "active": True,
                },
            ]

        def list_sessions(self, **_kwargs):
            return []

    adapter = MongoSessionAdapter.__new__(MongoSessionAdapter)
    adapter._store = _Store()
    adapter._storage = type("S", (), {})()
    adapter._message_state_cache = {}
    adapter._lock = threading.Lock()
    adapter._mongo_mode = True

    model_history, display_history = adapter.get_resume_conversations("sess-1")
    assert isinstance(model_history, list) and isinstance(display_history, list)
    assert model_history[0]["role"] == "user"
    assert display_history[-1]["content"] == "hello"


def test_peer_cert_cn():
    from hermes_storage.mtls import peer_cert_cn

    cert = {
        "subject": (((("countryName", "US"),), (("commonName", "home-pc"),))),
    }
    # getpeercert subject is nested tuples of ((key, value),)
    cert = {"subject": ((("commonName", "home-pc"),),)}
    assert peer_cert_cn(cert) == "home-pc"
    assert peer_cert_cn(None) is None


def test_enroll_code_roundtrip(tmp_path):
    from hermes_storage.enroll_flow import (
        create_enroll_code,
        load_pending,
        mark_used,
        normalize_code,
        redeem_code,
    )

    pending = create_enroll_code(profile="default", name="pc1", ttl_seconds=120, cp=tmp_path)
    assert "-" in pending.code
    loaded = load_pending(pending.code, tmp_path)
    assert loaded is not None
    assert loaded.is_valid()
    redeem_code(pending.code, machine_name="pc1", cp=tmp_path)
    mark_used(pending.code, used_by="pc1", cp=tmp_path)
    used = load_pending(normalize_code(pending.code), tmp_path)
    assert used is not None and used.used is True
    try:
        redeem_code(pending.code, machine_name="x", cp=tmp_path)
        assert False, "should reject used code"
    except ValueError:
        pass


def test_bootstrap_x509_fields(tmp_path, monkeypatch):
    from hermes_storage.bootstrap import load_bootstrap, reset_bootstrap_cache

    reset_bootstrap_cache()
    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": "mongodb://db:27017/?replicaSet=rs0&tls=true&authMechanism=MONGODB-X509&authSource=%24external",
            "profile": "default",
            "machine_id": "home-pc",
            "auth_mode": "x509",
            "tls": {
                "ca_file": "certs/ca.crt",
                "cert_key_file": "certs/agent.pem",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    cfg = load_bootstrap(force=True)
    assert cfg is not None
    assert cfg.auth_mode == "x509"
    assert cfg.resolved_tls_ca() == (tmp_path / "certs" / "ca.crt").resolve()
    assert cfg.resolved_tls_cert_key() == (tmp_path / "certs" / "agent.pem").resolve()
    reset_bootstrap_cache()


def test_bootstrap_loads_from_file(tmp_path, monkeypatch):
    from hermes_storage.bootstrap import load_bootstrap, reset_bootstrap_cache

    reset_bootstrap_cache()
    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": "mongodb://localhost:27017",
            "profile": "coder",
            "machine_id": "pc-home",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    cfg = load_bootstrap(force=True)
    assert cfg is not None
    assert cfg.profile == "coder"
    assert cfg.profile_db == "hermes_profile_coder"
    assert cfg.machine_id == "pc-home"
    reset_bootstrap_cache()


def test_is_mongo_mode_false_without_uri(monkeypatch, tmp_path):
    from hermes_storage.bootstrap import is_mongo_mode, reset_bootstrap_cache

    reset_bootstrap_cache()
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert is_mongo_mode() is False
    reset_bootstrap_cache()


def test_machine_id_stable():
    from hermes_storage.machine_id import compute_machine_id

    a = compute_machine_id()
    b = compute_machine_id()
    assert a == b
    assert compute_machine_id(override="My PC!") == "My_PC"


def test_overlay_merge_and_extract():
    from hermes_storage.overlay import (
        deep_merge,
        extract_machine_overlay,
        strip_machine_local,
    )

    base = {"model": {"default": "x"}, "terminal": {"timeout": 30, "cwd": "/old"}}
    overlay = {"terminal": {"cwd": "/new"}, "browser": {"cdp_url": "http://127.0.0.1:9222"}}
    merged = deep_merge(base, overlay)
    assert merged["model"]["default"] == "x"
    assert merged["terminal"]["cwd"] == "/new"
    assert merged["terminal"]["timeout"] == 30
    assert merged["browser"]["cdp_url"].startswith("http")

    full = {
        "model": {"default": "y"},
        "terminal": {"cwd": "/home/me", "backend": "local", "timeout": 60},
        "browser": {"headed": True},
        "mcp_servers": {"fs": {"command": "npx"}},
        "proxy": {"enabled": True},
        "platforms": {
            "telegram": {"enabled": True, "proxy_url": "socks5://127.0.0.1:1080"},
            "api_server": {"host": "127.0.0.1", "port": 8642, "enabled": True},
        },
    }
    extracted = extract_machine_overlay(full)
    assert "cwd" in extracted["terminal"]
    assert "timeout" not in extracted["terminal"]
    assert extracted["proxy"]["enabled"] is True
    assert extracted["platforms"]["telegram"]["proxy_url"].startswith("socks5://")
    assert extracted["platforms"]["api_server"]["port"] == 8642
    shared = strip_machine_local(full)
    assert "cwd" not in shared.get("terminal", {})
    assert "proxy" not in shared
    assert "proxy_url" not in shared.get("platforms", {}).get("telegram", {})
    assert shared["platforms"]["telegram"]["enabled"] is True
    assert shared["model"]["default"] == "y"


def test_split_machine_local_proxy_secrets():
    from hermes_storage.overlay import split_machine_local_secrets

    shared, local = split_machine_local_secrets({
        "OPENAI_API_KEY": "sk-x",
        "TELEGRAM_BOT_TOKEN": "bot",
        "TELEGRAM_PROXY": "socks5://127.0.0.1:1080",
        "HTTPS_PROXY": "http://corp:8080",
    })
    assert shared == {
        "OPENAI_API_KEY": "sk-x",
        "TELEGRAM_BOT_TOKEN": "bot",
    }
    assert local["TELEGRAM_PROXY"].startswith("socks5://")
    assert local["HTTPS_PROXY"].startswith("http://")


def test_storage_routes_proxy_secrets_to_machine_overlay():
    """TELEGRAM_PROXY must not stay in shared profile secrets."""
    from hermes_storage.factory import HermesStorage

    class _Secrets:
        def __init__(self):
            self.values = {"OPENAI_API_KEY": "sk", "TELEGRAM_PROXY": "socks5://old"}

        def get_all(self):
            return dict(self.values)

        def set_many(self, values):
            self.values = dict(values)

        def set(self, key, value):
            self.values[key] = value

    class _Machines:
        def __init__(self):
            self.overlay = {}

        def get_overlay(self, _machine_id):
            return dict(self.overlay)

        def set_overlay(self, _machine_id, overlay):
            self.overlay = dict(overlay)

    secrets = _Secrets()
    machines = _Machines()
    storage = HermesStorage(
        bootstrap=type("B", (), {"profile": "default"})(),
        client=None,
        shared_db=None,
        profile_db=None,
        settings=type("S", (), {"get": lambda *a, **k: {}})(),
        knowledge=None,
        config=type("C", (), {"get": lambda *a, **k: {}, "put": lambda *a, **k: None})(),
        secrets=secrets,
        soul=None,
        memories=None,
        skills=None,
        machines=machines,
        sessions=None,
        ledgers=None,
        cluster=None,
        machine_id="pc1",
        node_id="node1",
    )

    storage.set_secret("TELEGRAM_PROXY", "socks5://127.0.0.1:1080")
    assert "TELEGRAM_PROXY" not in secrets.values
    assert machines.overlay["secrets"]["TELEGRAM_PROXY"] == "socks5://127.0.0.1:1080"

    effective = storage.get_effective_secrets()
    assert effective["OPENAI_API_KEY"] == "sk"
    assert effective["TELEGRAM_PROXY"] == "socks5://127.0.0.1:1080"

    # Config overlay merge must not leak the secrets bag into YAML config.
    machines.overlay["terminal"] = {"cwd": "/tmp"}
    cfg = storage.load_effective_config({"model": {"default": "x"}})
    assert cfg["terminal"]["cwd"] == "/tmp"
    assert "secrets" not in cfg


def test_cluster_prompt_empty_without_mongo(monkeypatch, tmp_path):
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.cluster import format_cluster_prompt_block
    from hermes_storage.factory import reset_storage

    reset_bootstrap_cache()
    reset_storage()
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(tmp_path / "nope.yaml"))
    assert format_cluster_prompt_block() == ""


def test_export_local_home(tmp_path):
    from hermes_storage.local.migrate import export_local_home

    (tmp_path / "memories").mkdir()
    (tmp_path / "SOUL.md").write_text("I am Hermes.", encoding="utf-8")
    (tmp_path / "memories" / "MEMORY.md").write_text("fact one", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("model:\n  default: test\n", encoding="utf-8")
    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Demo\n", encoding="utf-8")

    payload = export_local_home(tmp_path)
    assert "I am Hermes" in payload["soul"]
    assert payload["memories"]["memory"] == "fact one"
    assert payload["config"]["model"]["default"] == "test"
    assert any(s["name"] == "demo" for s in payload["skills"])


def test_cluster_tools_require_mongo(monkeypatch, tmp_path):
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage
    from tools.cluster_tools import cluster_activate_tool, cluster_status_tool

    reset_bootstrap_cache()
    reset_storage()
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import json

    assert json.loads(cluster_status_tool())["success"] is False
    assert json.loads(cluster_activate_tool(target="x"))["success"] is False


def test_require_storage_fails_without_mongo(monkeypatch, tmp_path):
    from hermes_storage import MongoStorageError, require_storage
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage

    reset_bootstrap_cache()
    reset_storage()
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(tmp_path / "missing.yaml"))
    with pytest.raises(MongoStorageError):
        require_storage()


def test_sessiondb_mongo_mode_no_sqlite_fallback(monkeypatch, tmp_path):
    """Mongo mode must not open local state.db when the bridge fails."""
    from hermes_storage import MongoStorageError
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage

    reset_bootstrap_cache()
    reset_storage()
    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=1",
            "profile": "default",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)

    from hermes_storage.bootstrap import load_bootstrap
    assert load_bootstrap(force=True) is not None

    # Force bridge construction to fail after mode is detected.
    import hermes_storage.session_bridge as bridge

    def _boom(*_a, **_k):
        raise RuntimeError("simulated mongo bridge failure")

    monkeypatch.setattr(bridge, "MongoSessionAdapter", _boom)

    from hermes_state import SessionDB

    with pytest.raises(MongoStorageError) as ei:
        SessionDB()
    assert "split" in str(ei.value).lower() or "unavailable" in str(ei.value).lower()
    assert not (tmp_path / "state.db").exists()


def test_mongo_adapter_unknown_method_fail_loud():
    from hermes_storage.session_bridge import MongoSessionAdapter

    adapter = object.__new__(MongoSessionAdapter)
    adapter._mongo_mode = True
    with pytest.raises(AttributeError, match="not ported"):
        adapter.totally_fake_method_xyz()


def test_mongo_adapter_cleans_only_empty_ended_sessions():
    """The Dashboard empty-session controls must work in Mongo mode."""
    from hermes_storage.session_bridge import MongoSessionAdapter

    class Result:
        deleted_count = 1

    class Sessions:
        def __init__(self):
            self.updated = None
            self.deleted = None

        def aggregate(self, pipeline):
            if pipeline[-1] == {"$count": "count"}:
                return [{"count": 1}]
            return [{"session_id": "empty-ended"}]

        def update_many(self, query, update):
            self.updated = (query, update)

        def delete_many(self, query):
            self.deleted = query
            return Result()

    class Messages:
        name = "messages"

        def __init__(self):
            self.deleted = None

        def delete_many(self, query):
            self.deleted = query

    class Store:
        _sessions = Sessions()
        _messages = Messages()

    adapter = object.__new__(MongoSessionAdapter)
    adapter._store = Store()

    assert adapter.count_empty_sessions() == 1
    assert adapter.delete_empty_sessions() == 1
    assert adapter._store._sessions.updated == (
        {"parent_session_id": {"$in": ["empty-ended"]}},
        {"$set": {"parent_session_id": None}},
    )
    assert adapter._store._messages.deleted == {
        "session_id": {"$in": ["empty-ended"]}
    }


def test_mongo_adapter_exposes_session_search_contract():
    """Mongo rows expose the identifiers and windows session_search consumes."""
    from hermes_storage.session_bridge import MongoSessionAdapter

    class Store:
        def __init__(self):
            self.session = {
                "session_id": "history-1",
                "source": "cli",
                "started_at": 1,
            }
            self.messages = [
                {
                    "session_id": "history-1",
                    "message_index": 0,
                    "role": "user",
                    "content": "first request",
                },
                {
                    "session_id": "history-1",
                    "message_index": 1,
                    "role": "assistant",
                    "content": "first reply",
                },
            ]

        def list_sessions(self, **_kwargs):
            return [self.session]

        def get_session(self, session_id):
            return self.session if session_id == "history-1" else None

        def get_messages(self, session_id, **_kwargs):
            return self.messages if session_id == "history-1" else []

        def search_messages(self, _query, **_kwargs):
            return [self.messages[1]]

    adapter = object.__new__(MongoSessionAdapter)
    adapter._store = Store()
    adapter._message_state_cache = {}

    session = adapter.list_sessions_rich()[0]
    assert session["id"] == "history-1"
    assert session["message_count"] == 2
    assert session["preview"] == "first request"

    hit = adapter.search_messages("reply")[0]
    view = adapter.get_anchored_view("history-1", hit["id"], window=1)
    assert view["window"][0]["content"] == "first request"
    assert view["window"][1]["content"] == "first reply"
    assert adapter.get_message_storage_state(hit["id"]) == {
        "session_id": "history-1",
        "active": True,
        "compacted": False,
    }


def test_fail_hard_unreachable_uri(monkeypatch, tmp_path):
    from hermes_storage import MongoStorageError
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage
    from hermes_state import SessionDB

    reset_bootstrap_cache()
    reset_storage()
    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=200",
            "profile": "down",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    reset_bootstrap_cache()

    with pytest.raises(MongoStorageError):
        SessionDB()
    assert not (tmp_path / "state.db").exists()


def test_load_user_config_for_gateway_uses_mongo_effective(monkeypatch, tmp_path):
    """Fleet gateway settings come from Mongo, not a local config.yaml."""
    from hermes_cli.config import load_user_config_for_gateway

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Stale local yaml must not win over Mongo.
    (tmp_path / "config.yaml").write_text(
        "platforms:\n  telegram:\n    enabled: false\n",
        encoding="utf-8",
    )

    class _FakeStorage:
        def load_effective_config(self, base=None):
            return {
                "gateway": {"unauthorized_dm_behavior": "ignore"},
                "platforms": {"telegram": {"enabled": True}},
            }

    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)
    monkeypatch.setattr("hermes_storage.require_storage", lambda: _FakeStorage())

    cfg = load_user_config_for_gateway(tmp_path)
    assert cfg["platforms"]["telegram"]["enabled"] is True
    assert cfg["gateway"]["unauthorized_dm_behavior"] == "ignore"


def test_load_gateway_config_reads_mongo_platforms(monkeypatch, tmp_path):
    """GatewayConfig.platforms must reflect Mongo SoT without local yaml."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # No local config.yaml at all.

    class _FakeStorage:
        def load_effective_config(self, base=None):
            return {
                "gateway": {"unauthorized_dm_behavior": "ignore"},
                "platforms": {
                    "telegram": {
                        "enabled": True,
                        "home_channel": {"platform": "telegram", "chat_id": "1", "name": "Home"},
                    }
                },
            }

    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)
    monkeypatch.setattr("hermes_storage.require_storage", lambda: _FakeStorage())

    from gateway.config import Platform, load_gateway_config

    gw = load_gateway_config()
    assert Platform.TELEGRAM in gw.platforms
    assert gw.platforms[Platform.TELEGRAM].enabled is True
    assert gw.unauthorized_dm_behavior == "ignore"
