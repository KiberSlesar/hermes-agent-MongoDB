"""Storage facade factory — single entry point for all Hermes stores."""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from typing import Any, Optional

from hermes_storage.bootstrap import BootstrapConfig, get_bootstrap, is_mongo_mode
from hermes_storage.errors import MongoStorageError, raise_mongo_unavailable
from hermes_storage.machine_id import compute_machine_id, detect_capabilities
from hermes_storage.overlay import deep_merge

logger = logging.getLogger(__name__)

_STORAGE: Optional["HermesStorage"] = None


@dataclass
class HermesStorage:
    bootstrap: BootstrapConfig
    client: Any
    shared_db: Any
    profile_db: Any
    settings: Any
    knowledge: Any
    config: Any
    secrets: Any
    soul: Any
    memories: Any
    skills: Any
    machines: Any
    sessions: Any
    ledgers: Any
    cluster: Any
    machine_id: str
    node_id: str
    wiki: Any = None

    def load_effective_config(self, base: Optional[dict] = None) -> dict:
        """shared settings ⊕ profile config ⊕ machine overlay."""
        shared = self.settings.get("default") or {}
        profile = self.config.get("default") or {}
        merged = deep_merge(base or {}, shared)
        merged = deep_merge(merged, profile)
        overlay = self.machines.get_overlay(self.machine_id)
        # ``secrets`` in the overlay is the machine-local env bag — not config.
        overlay_cfg = {
            key: value for key, value in (overlay or {}).items()
            if key != "secrets"
        }
        return deep_merge(merged, overlay_cfg)

    def load_profile_config(self) -> dict:
        """Raw profile config document (no machine overlay)."""
        doc = self.config.get("default") or {}
        return doc if isinstance(doc, dict) else {}

    def save_profile_config(self, config: dict) -> None:
        from hermes_storage.outbox import KIND_PROFILE_CONFIG, run_or_enqueue
        from hermes_storage.overlay import strip_machine_local

        cleaned = strip_machine_local(config)

        def _apply() -> None:
            self.config.put(cleaned)
            self._invalidate_config_readers()

        run_or_enqueue(
            KIND_PROFILE_CONFIG,
            {"config": cleaned},
            _apply,
        )

    def save_machine_overlay(self, machine_id: str, overlay: dict) -> None:
        """Persist a machine overlay through the outbox-aware path."""
        from hermes_storage.outbox import KIND_MACHINE_OVERLAY, run_or_enqueue

        mid = str(machine_id or self.machine_id)
        data = dict(overlay or {})

        def _apply() -> None:
            self.machines.set_overlay(mid, data)
            self._invalidate_config_readers()

        run_or_enqueue(
            KIND_MACHINE_OVERLAY,
            {"overlay": data, "machine_id": mid},
            _apply,
        )

    def save_machine_overlay_from_config(self, config: dict) -> None:
        from hermes_storage.outbox import KIND_MACHINE_OVERLAY, run_or_enqueue
        from hermes_storage.overlay import extract_machine_overlay

        existing = self.machines.get_overlay(self.machine_id) or {}
        overlay = extract_machine_overlay(config)
        # Config saves must not wipe per-PC proxy secrets stored alongside.
        secrets = existing.get("secrets")
        if isinstance(secrets, dict) and secrets:
            overlay["secrets"] = dict(secrets)

        def _apply() -> None:
            self.machines.set_overlay(self.machine_id, overlay)
            self._invalidate_config_readers()

        run_or_enqueue(
            KIND_MACHINE_OVERLAY,
            {"overlay": overlay},
            _apply,
        )

    @staticmethod
    def _invalidate_config_readers() -> None:
        """Best-effort drop hermes_cli load_config cache after Mongo writes."""
        try:
            from hermes_cli.config import _invalidate_load_config_cache

            _invalidate_load_config_cache()
        except Exception:
            pass

    @staticmethod
    def _sync_process_env_after_secret_write(values: dict[str, str]) -> None:
        """Keep os.environ + load_env memo aligned with Mongo secret writes."""
        import os

        try:
            from hermes_cli.config import invalidate_env_cache

            for key, value in values.items():
                k = str(key)
                if k.startswith("__"):
                    continue
                os.environ[k] = str(value)
            invalidate_env_cache()
        except Exception:
            pass

    def get_machine_secrets(self) -> dict[str, str]:
        overlay = self.machines.get_overlay(self.machine_id) or {}
        raw = overlay.get("secrets") or {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in raw.items()
            if v is not None and not str(k).startswith("__")
        }

    def _put_machine_secrets(self, secrets: dict[str, str]) -> None:
        overlay = dict(self.machines.get_overlay(self.machine_id) or {})
        overlay["secrets"] = {
            str(k): str(v)
            for k, v in (secrets or {}).items()
            if v is not None
        }
        self.machines.set_overlay(self.machine_id, overlay)

    def get_effective_secrets(self) -> dict[str, str]:
        """Profile secrets ⊕ this machine's proxy/network secrets (machine wins)."""
        from hermes_storage.overlay import MACHINE_LOCAL_SECRET_KEYS
        from hermes_storage.outbox import apply_secret_overlay

        profile = {
            str(k): str(v)
            for k, v in (self.secrets.get_all() or {}).items()
            if v is not None
        }
        machine = self.get_machine_secrets()
        out = dict(profile)
        for key in MACHINE_LOCAL_SECRET_KEYS:
            if key in machine:
                out[key] = machine[key]
        return apply_secret_overlay(out)

    def _set_secret_direct(self, key: str, value: str) -> None:
        """Write secret without outbox (used by flush + internal paths)."""
        from hermes_storage.overlay import is_machine_local_secret

        if is_machine_local_secret(key):
            local = self.get_machine_secrets()
            local[str(key)] = str(value)
            self._put_machine_secrets(local)
            profile = dict(self.secrets.get_all() or {})
            if key in profile:
                if hasattr(self.secrets, "unset"):
                    self.secrets.unset(key)
                else:
                    profile.pop(key, None)
                    self.secrets.set_many(profile)
            self._sync_process_env_after_secret_write({str(key): str(value)})
            return
        self.secrets.set(key, value)
        self._sync_process_env_after_secret_write({str(key): str(value)})

    def set_secret(self, key: str, value: str) -> None:
        """Persist one secret; proxy/network keys go to this machine only."""
        from hermes_storage.outbox import KIND_SECRET_SET, run_or_enqueue

        run_or_enqueue(
            KIND_SECRET_SET,
            {"key": str(key), "value": str(value)},
            lambda: self._set_secret_direct(key, value),
        )
        # Keep process env usable even if Mongo was down and write was queued.
        self._sync_process_env_after_secret_write({str(key): str(value)})

    def _remove_secret_direct(self, key: str) -> bool:
        from hermes_storage.overlay import is_machine_local_secret
        import os

        found = False
        if is_machine_local_secret(key):
            local = self.get_machine_secrets()
            if key in local:
                del local[key]
                self._put_machine_secrets(local)
                found = True
        profile = dict(self.secrets.get_all() or {})
        if key in profile:
            if hasattr(self.secrets, "unset"):
                self.secrets.unset(key)
            else:
                profile.pop(key, None)
                self.secrets.set_many(profile)
            found = True
        if found:
            try:
                from hermes_cli.config import invalidate_env_cache

                os.environ.pop(str(key), None)
                invalidate_env_cache()
            except Exception:
                pass
        return found

    def remove_secret(self, key: str) -> bool:
        from hermes_storage.outbox import KIND_SECRET_REMOVE, run_or_enqueue
        import os

        result = {"found": False}

        def _apply() -> None:
            result["found"] = self._remove_secret_direct(key)

        status = run_or_enqueue(
            KIND_SECRET_REMOVE,
            {"key": str(key)},
            _apply,
        )
        os.environ.pop(str(key), None)
        try:
            from hermes_cli.config import invalidate_env_cache

            invalidate_env_cache()
        except Exception:
            pass
        # Queued removes still "succeed" from the caller's POV — durable
        # once Mongo is back.
        if status.get("queued"):
            return True
        return bool(result["found"])

    def save_memory_entry(self, target: str, content: str) -> None:
        from hermes_storage.outbox import KIND_MEMORY, run_or_enqueue

        tgt = str(target or "memory")
        text = str(content or "")
        run_or_enqueue(
            KIND_MEMORY,
            {"target": tgt, "content": text},
            lambda: self.memories.save(tgt, text),
        )

    def _set_secrets_many_direct(
        self, values: dict[str, str], *, replace_profile: bool = True
    ) -> None:
        from hermes_storage.overlay import (
            MACHINE_LOCAL_SECRET_KEYS,
            split_machine_local_secrets,
        )

        shared, local = split_machine_local_secrets(values)
        if replace_profile:
            self.secrets.set_many(shared)
        else:
            profile = {
                str(k): str(v)
                for k, v in (self.secrets.get_all() or {}).items()
                if v is not None and str(k) not in MACHINE_LOCAL_SECRET_KEYS
            }
            profile.update(shared)
            self.secrets.set_many(profile)
        machine = self.get_machine_secrets()
        machine.update(local)
        self._put_machine_secrets(machine)
        self._sync_process_env_after_secret_write(
            {str(k): str(v) for k, v in values.items() if v is not None}
        )

    def set_secrets_many(self, values: dict[str, str], *, replace_profile: bool = True) -> None:
        """Write secrets, routing proxy/network keys to this machine.

        ``replace_profile=True`` (default) replaces the shared profile secrets
        document with the non-local keys from *values* (migrate / full import).
        Machine-local keys are merged into this PC's overlay.
        """
        from hermes_storage.outbox import KIND_SECRETS_MANY, run_or_enqueue

        payload_values = {
            str(k): str(v) for k, v in (values or {}).items() if v is not None
        }
        run_or_enqueue(
            KIND_SECRETS_MANY,
            {"values": payload_values, "replace_profile": bool(replace_profile)},
            lambda: self._set_secrets_many_direct(
                payload_values, replace_profile=replace_profile
            ),
        )
        self._sync_process_env_after_secret_write(payload_values)

    def load_soul(self) -> str:
        doc = self.soul.get("default") or {}
        return str(doc.get("content") or "")

    def save_soul(self, content: str) -> None:
        from hermes_storage.outbox import KIND_SOUL, run_or_enqueue

        text = str(content or "")
        run_or_enqueue(
            KIND_SOUL,
            {"content": text},
            lambda: self.soul.put({"content": text}),
        )

    def put_wiki_page(
        self,
        *,
        title: str,
        body: str,
        slug: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict:
        from hermes_storage.outbox import KIND_WIKI_PUT, run_or_enqueue
        from hermes_storage.wiki import slugify

        page_slug = slugify(slug or title)
        tag_list = list(tags or [])
        text = str(body or "")
        title_s = str(title or page_slug)
        result: dict = {}

        def _apply() -> None:
            store = self.wiki
            if store is None:
                raise RuntimeError("wiki store not available")
            result["page"] = store.put_page(
                title=title_s,
                body=text,
                slug=page_slug,
                tags=tag_list,
                updated_by=self.machine_id,
            )

        status = run_or_enqueue(
            KIND_WIKI_PUT,
            {
                "slug": page_slug,
                "title": title_s,
                "body": text,
                "tags": tag_list,
                "updated_by": self.machine_id,
            },
            _apply,
        )
        if status.get("queued"):
            return {
                "slug": page_slug,
                "title": title_s,
                "body": text,
                "tags": tag_list,
                "queued": True,
            }
        return result.get("page") or {"slug": page_slug}

    def delete_wiki_page(self, slug: str) -> bool:
        from hermes_storage.outbox import KIND_WIKI_DELETE, run_or_enqueue
        from hermes_storage.wiki import slugify

        page_slug = slugify(slug)
        result = {"deleted": False}

        def _apply() -> None:
            if self.wiki is None:
                raise RuntimeError("wiki store not available")
            result["deleted"] = bool(self.wiki.delete_page(page_slug))

        status = run_or_enqueue(
            KIND_WIKI_DELETE,
            {"slug": page_slug},
            _apply,
        )
        if status.get("queued"):
            return True
        return bool(result["deleted"])

    def register_presence(
        self,
        *,
        api_base: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
        hostname: Optional[str] = None,
        active_turns: int = 0,
        active_session_keys: Optional[list[str]] = None,
    ) -> None:
        self.cluster.heartbeat({
            "node_id": self.node_id,
            "machine_id": self.machine_id,
            "profile": self.bootstrap.profile,
            "hostname": hostname or socket.gethostname(),
            "api_base": api_base or "",
            "capabilities": capabilities or detect_capabilities(),
            "status": "online",
            "active_turns": max(0, int(active_turns)),
            "active_session_keys": list(active_session_keys or []),
        })
        self.machines.upsert_machine(self.machine_id, {
            "hostname": hostname or socket.gethostname(),
            "capabilities": capabilities or detect_capabilities(),
            "node_id": self.node_id,
            "last_seen": True,
        })
        try:
            from hermes_storage.outbox import try_flush_outbox_best_effort

            try_flush_outbox_best_effort()
        except Exception:
            pass

    def cluster_status(self) -> dict[str, Any]:
        state = self.cluster.get_state()
        nodes = self.cluster.list_nodes()
        # Annotate chat readiness from advertised api_base (no live probe here —
        # control plane probes on /api/fleet/active-chat).
        enriched = []
        for node in nodes:
            row = dict(node)
            api = str(row.get("api_base") or "").strip()
            row["chat_ready"] = bool(api)
            enriched.append(row)
        return {
            "profile": self.bootstrap.profile,
            "this_node_id": self.node_id,
            "this_machine_id": self.machine_id,
            "state": state,
            "nodes": enriched,
        }

    def activate(
        self,
        target: str,
        *,
        reason: str = "manual",
        announce_session_keys: Optional[list] = None,
    ) -> dict[str, Any]:
        """Activate a node by node_id, machine_id, or hostname."""
        nodes = self.cluster.list_nodes(online_within_s=120.0)
        match = None
        needle = target.strip().lower()
        for node in nodes:
            for field in ("node_id", "machine_id", "hostname"):
                val = str(node.get(field) or "").lower()
                if val == needle or val.startswith(needle):
                    match = node
                    break
            if match:
                break
        if not match:
            raise ValueError(f"No cluster node matched {target!r}")
        state = self.cluster.set_active(
            match["node_id"],
            reason=reason,
            announce_session_keys=announce_session_keys,
        )
        # If we just selected THIS machine, make sure the local gateway is up
        # so messaging acquire can complete.
        if match.get("node_id") == self.node_id:
            try:
                from hermes_storage.cluster import ensure_local_gateway_service

                ensure_local_gateway_service()
            except Exception:
                logger.debug("ensure_local_gateway_service failed", exc_info=True)
        return state


def _build_storage(boot: BootstrapConfig) -> HermesStorage:
    from hermes_storage.mongo import ensure_indexes, get_client
    from hermes_storage.mongo.stores import (
        MongoClusterStore,
        MongoDocumentStore,
        MongoLedgerStore,
        MongoMachineStore,
        MongoMemoryEntriesStore,
        MongoSecretsStore,
        MongoSessionStore,
        MongoSkillsStore,
    )
    from hermes_storage.wiki import MongoWikiStore

    client = get_client(boot.mongo_uri)
    shared = client[boot.shared_db]
    profile = client[boot.profile_db]
    ensure_indexes(shared, profile)

    machine_id = compute_machine_id(override=boot.machine_id)
    # Stable per machine — random suffix caused duplicate ghost nodes on every restart
    node_id = machine_id

    return HermesStorage(
        bootstrap=boot,
        client=client,
        shared_db=shared,
        profile_db=profile,
        settings=MongoDocumentStore(shared["settings"]),
        knowledge=MongoDocumentStore(shared["knowledge"]),
        config=MongoDocumentStore(profile["config"]),
        secrets=MongoSecretsStore(profile["secrets"]),
        soul=MongoDocumentStore(profile["soul"]),
        memories=MongoMemoryEntriesStore(profile["memories"]),
        skills=MongoSkillsStore(shared),
        machines=MongoMachineStore(profile),
        sessions=MongoSessionStore(profile),
        ledgers=MongoLedgerStore(profile),
        cluster=MongoClusterStore(shared),
        machine_id=machine_id,
        node_id=node_id,
        wiki=MongoWikiStore(shared["wiki_pages"]),
    )


def get_storage(*, force: bool = False) -> Optional[HermesStorage]:
    """Return the process-wide storage facade, or None if not in Mongo mode.

    When Mongo mode is on, connection/build failures propagate (no silent
    ``None``). Callers that need a guarantee should use :func:`require_storage`.
    """
    global _STORAGE
    if not is_mongo_mode():
        return None
    if _STORAGE is not None and not force:
        return _STORAGE
    boot = get_bootstrap()
    if boot is None:
        raise_mongo_unavailable("bootstrap missing while is_mongo_mode() is true")
    try:
        _STORAGE = _build_storage(boot)
    except MongoStorageError:
        raise
    except Exception as exc:
        raise_mongo_unavailable(str(exc), cause=exc)
    return _STORAGE


def require_storage(*, force: bool = False) -> HermesStorage:
    """Return storage or raise :class:`MongoStorageError` (never local fallback)."""
    if not is_mongo_mode():
        raise MongoStorageError(
            "Mongo mode is not enabled (no bootstrap.yaml / HERMES_MONGO_URI)"
        )
    storage = get_storage(force=force)
    if storage is None:
        raise_mongo_unavailable("get_storage() returned None")
    return storage


def reset_storage() -> None:
    global _STORAGE
    from hermes_storage.mongo import close_client

    _STORAGE = None
    close_client()
