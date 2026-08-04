"""Behavior tests for Mongo fleet ownership transitions."""

from __future__ import annotations

from copy import deepcopy

import pytest


class _Result:
    def __init__(self, modified_count=1):
        self.modified_count = modified_count


def _matches(doc, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(doc, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(doc, item) for item in expected):
                return False
            continue
        actual = doc.get(key)
        if isinstance(expected, dict):
            if "$exists" in expected and (key in doc) != expected["$exists"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


class _Collection:
    def __init__(self, docs=()):
        self.docs = [deepcopy(doc) for doc in docs]

    def update_one(self, query, update, upsert=False):
        doc = next((doc for doc in self.docs if _matches(doc, query)), None)
        if doc is None:
            if not upsert:
                return _Result(0)
            doc = {"_id": query.get("_id")}
            self.docs.append(doc)
            if "$setOnInsert" in update:
                doc.update(deepcopy(update["$setOnInsert"]))
        if "$set" in update:
            doc.update(deepcopy(update["$set"]))
        if "$push" in update:
            for key, value in update["$push"].items():
                doc.setdefault(key, []).extend(value.get("$each", []))
        return _Result()

    def find_one(self, query, *_args):
        doc = next((doc for doc in self.docs if _matches(doc, query)), None)
        return deepcopy(doc) if doc else None

    def find(self):
        return _Cursor(self.docs)


class _Cursor(list):
    def sort(self, *_args):
        return self


class _Database:
    def __init__(self, state, nodes):
        self.collections = {
            "cluster_state": _Collection([state]),
            "cluster_nodes": _Collection(nodes),
        }

    def __getitem__(self, name):
        return self.collections[name]


def test_first_live_node_claims_preseeded_empty_cluster_state():
    from hermes_storage.mongo.stores import MongoClusterStore

    db = _Database(
        {
            "_id": "default",
            "active_node_id": None,
            "messaging_owner": None,
            "handoff_state": "idle",
        },
        [],
    )
    cluster = MongoClusterStore(db)

    cluster.heartbeat({"node_id": "first", "machine_id": "first"})

    state = cluster.get_state()
    assert state["active_node_id"] == "first"
    assert state["messaging_owner"] == "first"


def test_active_switch_rejects_source_with_running_turn():
    from hermes_storage.mongo.stores import MongoClusterStore

    db = _Database(
        {
            "_id": "default",
            "active_node_id": "old",
            "messaging_owner": "old",
            "handoff_state": "idle",
        },
        [
            {"node_id": "old", "active_turns": 1},
            {"node_id": "new", "active_turns": 0},
        ],
    )
    cluster = MongoClusterStore(db)

    with pytest.raises(RuntimeError, match="busy"):
        cluster.set_active("new")


def test_active_switch_records_handoff_and_session_keys():
    from hermes_storage.mongo.stores import MongoClusterStore

    db = _Database(
        {
            "_id": "default",
            "active_node_id": "old",
            "messaging_owner": "old",
            "handoff_state": "idle",
        },
        [
            {"node_id": "old", "active_turns": 0, "active_session_keys": ["telegram:1"]},
            {"node_id": "new", "active_turns": 0},
        ],
    )
    cluster = MongoClusterStore(db)

    state = cluster.set_active("new")

    assert state["handoff_state"] == "releasing"
    assert state["handoff_from"] == "old"
    assert state["handoff_to"] == "new"
    assert state["handoff_session_keys"] == ["telegram:1"]
    # Active/owner stay on source until acquire completes — otherwise UIs and
    # the agent claim tools already run on the target mid-handoff.
    assert state["active_node_id"] == "old"
    assert state["messaging_owner"] == "old"
    assert state["pending_active_node_id"] == "new"


def test_begin_handoff_merges_announce_session_keys():
    from hermes_storage.mongo.stores import MongoClusterStore

    db = _Database(
        {
            "_id": "default",
            "active_node_id": "old",
            "messaging_owner": "old",
            "handoff_state": "idle",
        },
        [
            {"node_id": "old", "active_turns": 0, "active_session_keys": ["agent:main:telegram:dm:1"]},
            {"node_id": "new", "active_turns": 0},
        ],
    )
    cluster = MongoClusterStore(db)
    state = cluster.set_active(
        "new",
        announce_session_keys=["agent:main:telegram:dm:99"],
    )
    assert "agent:main:telegram:dm:1" in state["handoff_session_keys"]
    assert "agent:main:telegram:dm:99" in state["handoff_session_keys"]


def test_format_cluster_move_notice_mentions_target():
    from hermes_storage.cluster import format_cluster_move_notice

    class Cluster:
        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "win", "hostname": "R2D2"},
                {"node_id": "linux", "hostname": "hermes-mongo"},
            ]

    storage = type("S", (), {
        "node_id": "linux",
        "cluster": Cluster(),
    })()
    text = format_cluster_move_notice(
        storage, from_node_id="win", to_node_id="linux"
    )
    assert "Системное сообщение" in text
    assert "hermes-mongo" in text
    assert "R2D2" in text


def test_acquire_notifies_chat_with_captured_session_keys(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.state = {
                "handoff_state": "acquiring",
                "handoff_from": "old",
                "handoff_to": "new",
                "handoff_session_keys": ["agent:main:telegram:dm:42"],
            }
            self.completed = False

        def get_state(self):
            return dict(self.state)

        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "old", "hostname": "old-host"},
                {"node_id": "new", "hostname": "new-host"},
            ]

        def complete_messaging_handoff(self, node_id):
            self.completed = True
            self.state = {
                "handoff_state": "idle",
                "messaging_owner": node_id,
                "active_node_id": node_id,
                "handoff_session_keys": [],
            }

    notes = []
    cluster = Cluster()
    monkeypatch.setattr(cluster_module, "_ACQUIRE_CB", lambda: True)
    monkeypatch.setattr(
        cluster_module,
        "_NOTIFY_CB",
        lambda msg, keys=None: notes.append((msg, list(keys or []))),
    )
    storage = type("Storage", (), {
        "node_id": "new",
        "machine_id": "new-pc",
        "cluster": cluster,
    })()

    cluster_module._run_acquire_for_handoff(storage, "new")

    assert cluster.completed is True
    assert notes
    assert "Системное сообщение" in notes[0][0]
    assert notes[0][1] == ["agent:main:telegram:dm:42"]


def test_handoff_does_not_release_gateway_while_source_turn_is_active(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def get_state(self):
            return {
                "handoff_state": "releasing",
                "handoff_from": "old",
                "handoff_to": "new",
            }

        def list_nodes(self, **_kwargs):
            return [{"node_id": "old", "active_turns": 1, "online": True}]

        def mark_messaging_released(self, _node_id):
            raise AssertionError("busy owner must not release its gateway")

    released = []
    storage = type("Storage", (), {"node_id": "old", "cluster": Cluster()})()
    monkeypatch.setattr(cluster_module, "_RELEASE_CB", lambda: released.append(True))

    cluster_module._maybe_handle_handoff(storage)

    assert released == []


def test_should_connect_messaging_for_owner_and_handoff():
    from hermes_storage.cluster import should_connect_messaging

    class Cluster:
        def __init__(self, state):
            self._state = state

        def get_state(self):
            return self._state

    owner = type(
        "S",
        (),
        {
            "node_id": "a",
            "cluster": Cluster({"messaging_owner": "a", "handoff_state": "idle"}),
        },
    )()
    other = type(
        "S",
        (),
        {
            "node_id": "b",
            "cluster": Cluster({"messaging_owner": "a", "handoff_state": "idle"}),
        },
    )()
    acquiring = type(
        "S",
        (),
        {
            "node_id": "b",
            "cluster": Cluster({
                "messaging_owner": None,
                "handoff_state": "acquiring",
                "handoff_to": "b",
            }),
        },
    )()

    assert should_connect_messaging(owner) is True
    assert should_connect_messaging(other) is False
    assert should_connect_messaging(acquiring) is True


def test_acquiring_without_gateway_callback_starts_service_and_waits(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def get_state(self):
            return {
                "handoff_state": "acquiring",
                "handoff_from": "old",
                "handoff_to": "new",
            }

        def complete_messaging_handoff(self, _node_id):
            raise AssertionError("must not complete without acquire callback")

    calls = []
    monkeypatch.setattr(cluster_module, "_ACQUIRE_CB", None)
    monkeypatch.setattr(
        cluster_module,
        "ensure_local_gateway_service",
        lambda: calls.append("start") or {"started": True},
    )
    storage = type("Storage", (), {
        "node_id": "new",
        "machine_id": "new-pc",
        "cluster": Cluster(),
    })()

    cluster_module._maybe_handle_handoff(storage)

    assert calls == ["start"]


def test_force_release_when_handoff_source_is_offline(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.state = {
                "handoff_state": "releasing",
                "handoff_from": "dead",
                "handoff_to": "alive",
            }
            self.released = []

        def get_state(self):
            return dict(self.state)

        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "dead", "online": False},
                {"node_id": "alive", "online": True},
            ]

        def mark_messaging_released(self, node_id):
            self.released.append(node_id)
            self.state = {
                "handoff_state": "acquiring",
                "handoff_from": "dead",
                "handoff_to": "alive",
                "messaging_owner": None,
            }

        def complete_messaging_handoff(self, node_id):
            self.state = {
                "handoff_state": "idle",
                "messaging_owner": node_id,
                "active_node_id": node_id,
            }

    cluster = Cluster()
    monkeypatch.setattr(cluster_module, "_ACQUIRE_CB", lambda: True)
    monkeypatch.setattr(cluster_module, "_RELEASE_CB", None)
    monkeypatch.setattr(cluster_module, "_NOTIFY_CB", None)
    storage = type("Storage", (), {
        "node_id": "alive",
        "machine_id": "alive-pc",
        "cluster": cluster,
    })()

    cluster_module._maybe_handle_handoff(storage)

    assert cluster.released == ["dead"]
    assert cluster.state["messaging_owner"] == "alive"


def test_reconcile_connects_when_owner_but_adapters_not_held(monkeypatch):
    """Passive gateway that missed acquiring still connects once it is owner."""
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def get_state(self):
            return {
                "handoff_state": "idle",
                "messaging_owner": "win",
                "active_node_id": "win",
            }

    calls = []
    monkeypatch.setattr(cluster_module, "_LOCAL_MESSAGING_HELD", False)
    monkeypatch.setattr(cluster_module, "_ACQUIRE_CB", lambda: calls.append("acquire") or True)
    monkeypatch.setattr(cluster_module, "_RELEASE_CB", None)
    storage = type("Storage", (), {
        "node_id": "win",
        "machine_id": "win-pc",
        "cluster": Cluster(),
    })()

    cluster_module._maybe_reconcile_messaging(storage)

    assert calls == ["acquire"]
    assert cluster_module._LOCAL_MESSAGING_HELD is True


def test_reconcile_skips_when_not_owner(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def get_state(self):
            return {
                "handoff_state": "idle",
                "messaging_owner": "linux",
                "active_node_id": "linux",
            }

    calls = []
    monkeypatch.setattr(cluster_module, "_LOCAL_MESSAGING_HELD", False)
    monkeypatch.setattr(cluster_module, "_ACQUIRE_CB", lambda: calls.append("acquire") or True)
    storage = type("Storage", (), {
        "node_id": "win",
        "machine_id": "win-pc",
        "cluster": Cluster(),
    })()

    cluster_module._maybe_reconcile_messaging(storage)

    assert calls == []
    assert cluster_module._LOCAL_MESSAGING_HELD is False


def test_reconcile_skipped_during_gateway_bootstrap(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def get_state(self):
            return {
                "handoff_state": "idle",
                "messaging_owner": "win",
                "active_node_id": "win",
            }

    calls = []
    monkeypatch.setattr(cluster_module, "_GATEWAY_BOOTSTRAPPING", True)
    monkeypatch.setattr(cluster_module, "_LOCAL_MESSAGING_HELD", False)
    monkeypatch.setattr(cluster_module, "_ACQUIRE_CB", lambda: calls.append("acquire") or True)
    storage = type("Storage", (), {
        "node_id": "win",
        "machine_id": "win-pc",
        "cluster": Cluster(),
    })()

    cluster_module._maybe_reconcile_messaging(storage)

    assert calls == []


def test_drop_stale_messaging_when_lease_lost(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def get_state(self):
            return {
                "handoff_state": "idle",
                "messaging_owner": "other",
                "active_node_id": "other",
            }

    released = []
    monkeypatch.setattr(cluster_module, "_GATEWAY_BOOTSTRAPPING", False)
    monkeypatch.setattr(cluster_module, "_LOCAL_MESSAGING_HELD", True)
    monkeypatch.setattr(cluster_module, "_RELEASE_CB", lambda: released.append(True))
    storage = type("Storage", (), {
        "node_id": "me",
        "machine_id": "me-pc",
        "cluster": Cluster(),
    })()

    cluster_module._maybe_reconcile_messaging(storage)

    assert released == [True]
    assert cluster_module._LOCAL_MESSAGING_HELD is False


def test_acquire_fail_offline_previous_completes_degraded(monkeypatch):
    """Dead Windows + TG fail on Linux must NOT rollback ownership to dead."""
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.state = {
                "handoff_state": "acquiring",
                "handoff_from": "win",
                "handoff_to": "linux",
                "handoff_session_keys": ["agent:main:telegram:dm:1"],
            }
            self.completed = False
            self.rolled_back = False

        def get_state(self):
            return dict(self.state)

        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "win", "online": False, "hostname": "R2D2"},
                {"node_id": "linux", "online": True, "hostname": "hermes-mongo"},
            ]

        def complete_messaging_handoff(self, node_id):
            self.completed = True
            self.state = {
                "handoff_state": "idle",
                "messaging_owner": node_id,
                "active_node_id": node_id,
                "handoff_session_keys": [],
            }

        def rollback_messaging_handoff(self, *, reason):
            self.rolled_back = True
            raise AssertionError(f"must not rollback to dead owner: {reason}")

    notes = []
    cluster = Cluster()
    monkeypatch.setattr(cluster_module, "_ACQUIRE_CB", lambda: False)
    monkeypatch.setattr(
        cluster_module,
        "_NOTIFY_CB",
        lambda msg, keys=None: notes.append((msg, list(keys or []))),
    )
    storage = type("Storage", (), {
        "node_id": "linux",
        "machine_id": "linux-pc",
        "cluster": cluster,
    })()

    cluster_module._run_acquire_for_handoff(storage, "linux")

    assert cluster.completed is True
    assert cluster.rolled_back is False
    assert cluster.state["messaging_owner"] == "linux"
    assert cluster_module._LOCAL_MESSAGING_HELD is False
    assert notes
    assert "offline" in notes[0][0].lower() or "degraded" in notes[0][0].lower() or "failed" in notes[0][0].lower()
    assert notes[0][1] == ["agent:main:telegram:dm:1"]


def test_acquire_fail_online_previous_still_rolls_back(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.state = {
                "handoff_state": "acquiring",
                "handoff_from": "win",
                "handoff_to": "linux",
                "handoff_session_keys": [],
            }
            self.completed = False
            self.rolled_back = False

        def get_state(self):
            return dict(self.state)

        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "win", "online": True},
                {"node_id": "linux", "online": True},
            ]

        def complete_messaging_handoff(self, _node_id):
            self.completed = True

        def rollback_messaging_handoff(self, *, reason):
            self.rolled_back = True
            self.state = {
                "handoff_state": "idle",
                "messaging_owner": "win",
                "active_node_id": "win",
                "handoff_error": reason,
            }

    cluster = Cluster()
    monkeypatch.setattr(cluster_module, "_ACQUIRE_CB", lambda: False)
    monkeypatch.setattr(cluster_module, "_NOTIFY_CB", None)
    storage = type("Storage", (), {
        "node_id": "linux",
        "machine_id": "linux-pc",
        "cluster": cluster,
    })()

    cluster_module._run_acquire_for_handoff(storage, "linux")

    assert cluster.completed is False
    assert cluster.rolled_back is True
    assert cluster.state["messaging_owner"] == "win"
    assert cluster_module._LOCAL_MESSAGING_HELD is False


def test_failback_preferred_self_initiates(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.activated = []

        def get_state(self):
            return {
                "handoff_state": "idle",
                "failover": "auto",
                "messaging_owner": "linux",
                "active_node_id": "linux",
                "preferred_messaging_node": "win",
            }

        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "win", "online": True, "active_turns": 0},
                {"node_id": "linux", "online": True, "active_turns": 0},
            ]

        def set_active(self, node_id, *, reason="manual"):
            self.activated.append((node_id, reason))
            return self.get_state()

    started = []
    cluster = Cluster()
    monkeypatch.setattr(
        cluster_module,
        "ensure_local_gateway_service",
        lambda: started.append("start") or {},
    )
    storage = type("Storage", (), {
        "node_id": "win",
        "machine_id": "win-pc",
        "cluster": cluster,
    })()

    cluster_module._maybe_failback(storage)

    assert cluster.activated == [("win", "failback")]
    assert started == ["start"]


def test_failback_non_preferred_does_not_initiate(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.activated = []

        def get_state(self):
            return {
                "handoff_state": "idle",
                "failover": "auto",
                "messaging_owner": "linux",
                "preferred_messaging_node": "win",
            }

        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "win", "online": True, "active_turns": 0},
                {"node_id": "linux", "online": True, "active_turns": 0},
            ]

        def set_active(self, node_id, *, reason="manual"):
            self.activated.append((node_id, reason))

    storage = type("Storage", (), {
        "node_id": "linux",
        "machine_id": "linux-pc",
        "cluster": Cluster(),
    })()

    cluster_module._maybe_failback(storage)

    assert storage.cluster.activated == []


def test_failover_ensures_preferred_before_switch(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.preferred = None
            self.activated = []

        def get_state(self):
            return {
                "handoff_state": "idle",
                "failover": "auto",
                "messaging_owner": "win",
                "preferred_messaging_node": None,
            }

        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "win", "online": False},
                {"node_id": "linux", "online": True},
            ]

        def ensure_preferred_messaging_node(self, node_id):
            self.preferred = node_id

        def set_active(self, node_id, *, reason="manual"):
            self.activated.append((node_id, reason))

    started = []
    cluster = Cluster()
    monkeypatch.setattr(
        cluster_module,
        "ensure_local_gateway_service",
        lambda: started.append(1) or {},
    )
    storage = type("Storage", (), {
        "node_id": "linux",
        "machine_id": "linux-pc",
        "cluster": cluster,
    })()

    cluster_module._maybe_failover(storage)

    assert cluster.preferred == "win"
    assert cluster.activated == [("linux", "failover")]
    assert started == [1]


def test_failover_picks_highest_health_score(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.activated = []

        def get_state(self):
            return {
                "handoff_state": "idle",
                "failover": "auto",
                "messaging_owner": "dead",
            }

        def list_nodes(self, **_kwargs):
            return [
                {"node_id": "dead", "online": False, "health_score": 90},
                {
                    "node_id": "weak",
                    "online": True,
                    "hostname": "a-weak",
                    "health_score": 20,
                },
                {
                    "node_id": "strong",
                    "online": True,
                    "hostname": "b-strong",
                    "health_score": 95,
                },
            ]

        def ensure_preferred_messaging_node(self, _node_id):
            pass

        def set_active(self, node_id, *, reason="manual"):
            self.activated.append((node_id, reason))

    monkeypatch.setattr(cluster_module, "ensure_local_gateway_service", lambda: {})

    # Weak node must NOT initiate even if it runs the heartbeat first.
    weak = type("S", (), {
        "node_id": "weak",
        "machine_id": "w",
        "cluster": Cluster(),
    })()
    cluster_module._maybe_failover(weak)
    assert weak.cluster.activated == []

    strong = type("S", (), {
        "node_id": "strong",
        "machine_id": "s",
        "cluster": Cluster(),
    })()
    cluster_module._maybe_failover(strong)
    assert strong.cluster.activated == [("strong", "failover")]


def test_health_rebalance_with_hysteresis(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.activated = []
            self.marked = False
            self.state = {
                "handoff_state": "idle",
                "failover": "auto",
                "messaging_owner": "weak",
                "preferred_messaging_node": "home",
            }

        def get_state(self):
            return dict(self.state)

        def list_nodes(self, **_kwargs):
            return [
                {
                    "node_id": "weak",
                    "online": True,
                    "health_score": 25,
                    "health_checks": {
                        "telegram_api": {"ok": False, "applicable": True},
                        "llm_provider": {"ok": True, "applicable": True},
                    },
                    "active_turns": 0,
                    "hostname": "weak",
                },
                {
                    "node_id": "strong",
                    "online": True,
                    "health_score": 90,
                    "active_turns": 0,
                    "hostname": "strong",
                },
                {"node_id": "home", "online": False, "health_score": 99},
            ]

        def mark_health_rebalance(self):
            self.marked = True

        def set_active(self, node_id, *, reason="manual"):
            self.activated.append((node_id, reason))

    monkeypatch.setattr(cluster_module, "ensure_local_gateway_service", lambda: {})
    monkeypatch.setattr(
        cluster_module,
        "_cluster_health_settings",
        lambda: {"hysteresis": 20.0, "min_score": 40.0, "cooldown_s": 600.0},
    )

    # Non-best does not initiate
    weak_storage = type("S", (), {
        "node_id": "weak",
        "cluster": Cluster(),
    })()
    cluster_module._maybe_health_rebalance(weak_storage)
    assert weak_storage.cluster.activated == []

    strong_storage = type("S", (), {
        "node_id": "strong",
        "cluster": Cluster(),
    })()
    cluster_module._maybe_health_rebalance(strong_storage)
    assert strong_storage.cluster.activated == [("strong", "health_rebalance")]
    assert strong_storage.cluster.marked is True


def test_health_rebalance_skips_llm_only_failure_when_tg_ok(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.activated = []

        def get_state(self):
            return {
                "handoff_state": "idle",
                "failover": "auto",
                "messaging_owner": "win",
                "preferred_messaging_node": None,
            }

        def list_nodes(self, **_kwargs):
            return [
                {
                    "node_id": "win",
                    "online": True,
                    "health_score": 53,
                    "health_critical_failed": True,
                    "health_checks": {
                        "telegram_api": {"ok": True, "applicable": True},
                        "llm_provider": {"ok": False, "applicable": True},
                    },
                    "active_turns": 0,
                },
                {"node_id": "linux", "online": True, "health_score": 90},
            ]

        def set_active(self, node_id, *, reason="manual"):
            self.activated.append((node_id, reason))

    monkeypatch.setattr(
        cluster_module,
        "_cluster_health_settings",
        lambda: {"hysteresis": 20.0, "min_score": 40.0, "cooldown_s": 600.0},
    )
    storage = type("S", (), {"node_id": "linux", "cluster": Cluster()})()
    cluster_module._maybe_health_rebalance(storage)
    assert storage.cluster.activated == []


def test_health_rebalance_skipped_when_owner_healthy(monkeypatch):
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.activated = []

        def get_state(self):
            return {
                "handoff_state": "idle",
                "failover": "auto",
                "messaging_owner": "ok",
            }

        def list_nodes(self, **_kwargs):
            return [
                {
                    "node_id": "ok",
                    "online": True,
                    "health_score": 85,
                    "health_critical_failed": False,
                    "health_checks": {
                        "telegram_api": {"ok": True},
                        "llm_provider": {"ok": True},
                    },
                    "active_turns": 0,
                },
                {"node_id": "better", "online": True, "health_score": 99},
            ]

        def set_active(self, node_id, *, reason="manual"):
            self.activated.append((node_id, reason))

    monkeypatch.setattr(
        cluster_module,
        "_cluster_health_settings",
        lambda: {"hysteresis": 20.0, "min_score": 40.0, "cooldown_s": 600.0},
    )
    storage = type("S", (), {"node_id": "better", "cluster": Cluster()})()
    cluster_module._maybe_health_rebalance(storage)
    assert storage.cluster.activated == []


def test_health_rebalance_skipped_during_cooldown(monkeypatch):
    from datetime import datetime, timezone, timedelta
    from hermes_storage import cluster as cluster_module

    class Cluster:
        def __init__(self):
            self.activated = []

        def get_state(self):
            return {
                "handoff_state": "idle",
                "failover": "auto",
                "messaging_owner": "weak",
                "last_health_rebalance_at": datetime.now(timezone.utc)
                - timedelta(seconds=30),
            }

        def list_nodes(self, **_kwargs):
            return [
                {
                    "node_id": "weak",
                    "online": True,
                    "health_score": 10,
                    "health_critical_failed": True,
                    "active_turns": 0,
                },
                {"node_id": "strong", "online": True, "health_score": 90},
            ]

        def set_active(self, node_id, *, reason="manual"):
            self.activated.append((node_id, reason))

    monkeypatch.setattr(
        cluster_module,
        "_cluster_health_settings",
        lambda: {"hysteresis": 20.0, "min_score": 40.0, "cooldown_s": 600.0},
    )
    storage = type("S", (), {"node_id": "strong", "cluster": Cluster()})()
    cluster_module._maybe_health_rebalance(storage)
    assert storage.cluster.activated == []


def test_pick_best_online_candidate_orders_by_score():
    from hermes_storage.cluster import _pick_best_online_candidate

    best = _pick_best_online_candidate(
        [
            {"node_id": "a", "online": True, "health_score": 40, "hostname": "z"},
            {"node_id": "b", "online": True, "health_score": 80, "hostname": "a"},
            {"node_id": "c", "online": False, "health_score": 99, "hostname": "c"},
        ],
        exclude="x",
    )
    assert best["node_id"] == "b"
