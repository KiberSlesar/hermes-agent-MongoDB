"""Tests for fleet api_base advertise + proxy auth."""

from __future__ import annotations

import pytest


def test_normalize_and_resolve_api_base(monkeypatch, tmp_path):
    from hermes_storage import api_base as ab

    monkeypatch.delenv("HERMES_API_BASE", raising=False)
    monkeypatch.delenv("HERMES_SERVE_URL", raising=False)
    ab.set_process_api_base(None)

    assert ab.normalize_api_base("http://192.168.1.10:9119/") == "http://192.168.1.10:9119"
    assert ab.http_to_ws_base("https://edge.example:443") == "wss://edge.example:443"

    monkeypatch.setenv("HERMES_API_BASE", "http://10.0.0.5:9119")
    assert ab.resolve_advertise_api_base() == "http://10.0.0.5:9119"

    monkeypatch.delenv("HERMES_API_BASE", raising=False)
    ab.set_process_api_base("http://127.0.0.1:9120")
    assert ab.resolve_advertise_api_base() == "http://127.0.0.1:9120"


def test_fleet_proxy_secret_verify(monkeypatch):
    from hermes_storage import fleet_proxy_auth as fpa

    monkeypatch.setenv("HERMES_FLEET_PROXY_SECRET", "s3cret-value")
    assert fpa.fleet_proxy_configured() is True
    assert fpa.verify_fleet_proxy_credential("s3cret-value") is True
    assert fpa.verify_fleet_proxy_credential("wrong") is False
    assert fpa.authorization_header_value().startswith("Bearer fleet1.")


def test_fleet_proxy_ticket_roundtrip(monkeypatch):
    from hermes_storage import fleet_proxy_auth as fpa

    monkeypatch.setenv("HERMES_FLEET_PROXY_SECRET", "ticket-secret")
    ticket = fpa.mint_fleet_proxy_ticket(owner_node_id="home", api_base="http://10.0.0.1:9119")
    assert ticket.startswith("fleet1.")
    assert fpa.verify_fleet_proxy_ticket(ticket) is True
    assert fpa.verify_fleet_proxy_credential(ticket) is True
    assert fpa.verify_fleet_proxy_ticket(ticket + "x") is False

    # Expired ticket
    import time

    old = fpa.mint_fleet_proxy_ticket(ttl_s=15)
    # Force expiry by rewriting payload — easier: mint with past clock via monkeypatch
    monkeypatch.setattr(fpa.time, "time", lambda: time.time() + 120)
    assert fpa.verify_fleet_proxy_ticket(old) is False


def test_cluster_status_marks_chat_ready():
    from hermes_storage.factory import HermesStorage

    class _Cluster:
        def get_state(self):
            return {"messaging_owner": "n1", "active_node_id": "n1", "handoff_state": "idle"}

        def list_nodes(self):
            return [
                {"node_id": "n1", "api_base": "http://10.0.0.1:9119", "online": True},
                {"node_id": "n2", "api_base": "", "online": True},
            ]

    storage = HermesStorage(
        bootstrap=type("B", (), {"profile": "default"})(),
        client=None,
        shared_db=None,
        profile_db=None,
        settings=None,
        knowledge=None,
        config=None,
        secrets=None,
        soul=None,
        memories=None,
        skills=None,
        machines=None,
        sessions=None,
        ledgers=None,
        cluster=_Cluster(),
        machine_id="m1",
        node_id="n1",
    )
    status = storage.cluster_status()
    by_id = {n["node_id"]: n for n in status["nodes"]}
    assert by_id["n1"]["chat_ready"] is True
    assert by_id["n2"]["chat_ready"] is False


def test_resolve_active_chat_target(monkeypatch):
    from hermes_cli.web_routers import fleet as fleet_mod

    class _Storage:
        def cluster_status(self):
            return {
                "state": {
                    "messaging_owner": "home",
                    "active_node_id": "home",
                    "handoff_state": "idle",
                },
                "nodes": [
                    {
                        "node_id": "home",
                        "hostname": "pc",
                        "api_base": "http://192.168.0.10:9119",
                        "online": True,
                    }
                ],
            }

    monkeypatch.setattr(
        "hermes_storage.api_base.probe_chat_ready",
        lambda *a, **k: {"ok": True},
    )
    monkeypatch.setenv("HERMES_FLEET_PROXY_SECRET", "x")
    target = fleet_mod.resolve_active_chat_target(_Storage())
    assert target["owner_node_id"] == "home"
    assert target["api_base"] == "http://192.168.0.10:9119"
    assert target["chat_ready"] is True
    assert target["fleet_proxy_configured"] is True
