"""Tests for fleet version compare, skew notice, cluster update helpers."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path


def test_versions_in_sync_and_skew_warning():
    from hermes_storage.fleet_update import (
        format_version_skew_warning,
        versions_in_sync,
    )

    desired = {"version": "0.19.1", "ref": "main", "repo": "KiberSlesar/hermes-agent-MongoDB"}
    assert versions_in_sync(
        agent_version="0.19.1", install_ref="main", desired=desired
    )
    assert not versions_in_sync(
        agent_version="0.19.0", install_ref="main", desired=desired
    )
    assert versions_in_sync(
        agent_version="0.19.1", install_ref="abcd", desired=desired
    )
    assert versions_in_sync(agent_version="0.1", install_ref="x", desired={})

    warn = format_version_skew_warning(
        agent_version="0.19.0", install_ref="main", desired=desired
    )
    assert "Внимание" in warn
    assert "hermes update" in warn
    assert "0.19.0" in warn
    assert "0.19.1" in warn
    assert "auto-update" not in warn
    assert (
        format_version_skew_warning(
            agent_version="0.19.1", install_ref="main", desired=desired
        )
        == ""
    )


def test_enrich_cluster_nodes_marks_stale():
    from hermes_storage.fleet_update import enrich_cluster_nodes

    nodes = enrich_cluster_nodes(
        [
            {"node_id": "a", "agent_version": "0.19.0", "install_ref": "main"},
            {"node_id": "b", "agent_version": "0.19.1", "install_ref": "main"},
        ],
        {"version": "0.19.1", "ref": "main"},
    )
    by = {n["node_id"]: n for n in nodes}
    assert by["a"]["update_stale"] is True
    assert by["a"]["version_in_sync"] is False
    assert by["b"]["update_stale"] is False


def test_maybe_schedule_defers_when_busy(monkeypatch):
    """Idle scheduler still exists for tests but is not wired from heartbeat."""
    from hermes_storage import fleet_update as fu

    class Cluster:
        def get_state(self):
            return {"handoff_state": "idle"}

        def heartbeat(self, _doc):
            pass

    storage = type(
        "S",
        (),
        {
            "node_id": "n1",
            "machine_id": "m1",
            "cluster": Cluster(),
            "fleet_release": type("R", (), {"get": lambda self: {
                "version": "9.9.9", "ref": "main", "repo": "x/y"
            }})(),
            "shared_db": None,
        },
    )()

    monkeypatch.setattr(fu, "local_agent_version", lambda: "0.0.1")
    monkeypatch.setattr(fu, "local_install_ref", lambda: "main")
    fu._LAST_APPLY_ATTEMPT = 0.0
    fu._APPLY_THREAD = None

    result = fu.maybe_schedule_fleet_update(storage, active_turns=2, force=False)
    assert result["needed"] is True
    assert result["scheduled"] is False
    assert result.get("deferred") == "active_turns"


def test_format_cluster_move_notice_appends_skew():
    from hermes_storage.cluster import format_cluster_move_notice

    class Release:
        def get(self):
            return {"version": "0.20.0", "ref": "main"}

    class Cluster:
        def list_nodes(self, **_kwargs):
            return [
                {
                    "node_id": "linux",
                    "hostname": "hermes-mongo",
                    "agent_version": "0.19.0",
                    "install_ref": "main",
                },
                {"node_id": "win", "hostname": "R2D2", "agent_version": "0.20.0"},
            ]

    storage = type(
        "S",
        (),
        {
            "node_id": "linux",
            "cluster": Cluster(),
            "fleet_release": Release(),
        },
    )()
    text = format_cluster_move_notice(
        storage, from_node_id="win", to_node_id="linux"
    )
    assert "Системное сообщение" in text
    assert "Внимание" in text
    assert "hermes update" in text
    assert "0.19.0" in text
    assert "0.20.0" in text


def test_refresh_control_plane_scripts_from_tarball(tmp_path):
    from hermes_storage.fleet_update import refresh_control_plane_scripts_from_tarball

    tgz = tmp_path / "src.tgz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        content = b'print("orch")\n'
        info = tarfile.TarInfo(
            name="repo-abc/deploy/control-plane/scripts/orchestrator_standalone.py"
        )
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
        junk = b"nope"
        jinfo = tarfile.TarInfo(name="repo-abc/README.md")
        jinfo.size = len(junk)
        tar.addfile(jinfo, io.BytesIO(junk))
    tgz.write_bytes(buf.getvalue())

    scripts = tmp_path / "scripts"
    updated = refresh_control_plane_scripts_from_tarball(tgz, scripts_dir=scripts)
    assert "orchestrator_standalone.py" in updated
    out = scripts / "orchestrator_standalone.py"
    assert out.read_text(encoding="utf-8") == 'print("orch")\n'


def test_apply_fleet_update_sync_already_in_sync(monkeypatch):
    from hermes_storage import fleet_update as fu

    monkeypatch.setattr(fu, "local_agent_version", lambda: "1.0.0")
    monkeypatch.setattr(fu, "local_install_ref", lambda: "main")
    called = {"n": 0}

    def _boom(_desired):
        called["n"] += 1
        return 1

    monkeypatch.setattr(fu, "run_mongo_fork_install", _boom)
    result = fu.apply_fleet_update_sync(
        {"version": "1.0.0", "ref": "main", "repo": "x/y"}
    )
    assert result["ok"] is True
    assert result["needed"] is False
    assert called["n"] == 0


def test_is_mongo_agent_install_with_bootstrap(tmp_path, monkeypatch):
    from hermes_storage import fleet_update as fu

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: tmp_path
    )
    monkeypatch.setattr(fu, "stamp_path", lambda: tmp_path / ".fleet_install_stamp")

    def _no_mongo():
        raise RuntimeError("no")

    monkeypatch.setattr(
        "hermes_storage.is_mongo_mode",
        lambda: False,
        raising=False,
    )
    # Patch import path used inside is_mongo_agent_install
    import hermes_storage as hs

    monkeypatch.setattr(hs, "is_mongo_mode", lambda: False)
    assert fu.is_mongo_agent_install() is False
    (tmp_path / "bootstrap.yaml").write_text("profile: default\n", encoding="utf-8")
    assert fu.is_mongo_agent_install() is True
