"""Local Mongo write outbox — spool durable writes while Mongo is down.

When a durable write fails (connectivity / timeout), the mutation is stored
under ``HERMES_HOME/cache/mongo_outbox/`` (allowed local cache, not classic
SoT). On reconnect, ``flush_outbox()`` replays entries into Mongo in order.

Coalescing: later entries of the same logical key replace earlier pending
ones (e.g. repeated ``soul`` / ``secret_set:KEY`` / ``profile_config``).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_FLUSH_LOCK = threading.Lock()

# Process-level overlay for secrets queued while Mongo is down — so
# get_env_value / get_effective_secrets still see the agent's last write.
_SECRET_OVERLAY: dict[str, Optional[str]] = {}

KIND_PROFILE_CONFIG = "profile_config"
KIND_MACHINE_OVERLAY = "machine_overlay"
KIND_SECRET_SET = "secret_set"
KIND_SECRET_REMOVE = "secret_remove"
KIND_SECRETS_MANY = "secrets_many"
KIND_SOUL = "soul"
KIND_MEMORY = "memory"
KIND_CRON_JOBS = "cron_jobs"
KIND_SKILL_PUT = "skill_put"
KIND_SKILL_DELETE = "skill_delete"


def outbox_root() -> Path:
    from hermes_constants import get_hermes_home

    root = get_hermes_home() / "cache" / "mongo_outbox"
    (root / "entries").mkdir(parents=True, exist_ok=True)
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    return root


def _entries_dir() -> Path:
    return outbox_root() / "entries"


def _blobs_dir() -> Path:
    return outbox_root() / "blobs"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coalesce_key(kind: str, payload: dict[str, Any]) -> Optional[str]:
    if kind == KIND_PROFILE_CONFIG:
        return "profile_config"
    if kind == KIND_MACHINE_OVERLAY:
        return "machine_overlay"
    if kind == KIND_SOUL:
        return "soul"
    if kind == KIND_CRON_JOBS:
        return "cron_jobs"
    if kind in {KIND_SECRET_SET, KIND_SECRET_REMOVE}:
        # Same logical key so set→remove (or reverse) coalesce correctly.
        return f"secret:{payload.get('key')}"
    if kind == KIND_SECRETS_MANY:
        return "secrets_many"
    if kind == KIND_MEMORY:
        return f"memory:{payload.get('target')}"
    if kind in {KIND_SKILL_PUT, KIND_SKILL_DELETE}:
        return f"skill:{payload.get('name')}"
    return None


def _list_entry_paths() -> list[Path]:
    d = _entries_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"), key=lambda p: p.name)


def _read_entry(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Corrupt outbox entry %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not data.get("kind"):
        return None
    return data


def note_secret_overlay(key: str, value: Optional[str]) -> None:
    """Remember a secret mutation that was queued (value None = deleted)."""
    with _LOCK:
        _SECRET_OVERLAY[str(key)] = value


def apply_secret_overlay(secrets: dict[str, str]) -> dict[str, str]:
    """Merge queued secret mutations onto a Mongo secrets map."""
    with _LOCK:
        if not _SECRET_OVERLAY:
            return secrets
        out = dict(secrets)
        for key, value in _SECRET_OVERLAY.items():
            if value is None:
                out.pop(key, None)
            else:
                out[key] = value
        return out


def clear_secret_overlay_keys(keys: Optional[list[str]] = None) -> None:
    with _LOCK:
        if keys is None:
            _SECRET_OVERLAY.clear()
            return
        for key in keys:
            _SECRET_OVERLAY.pop(str(key), None)


def pending_count() -> int:
    with _LOCK:
        return len(_list_entry_paths())


def list_pending() -> list[dict[str, Any]]:
    with _LOCK:
        out: list[dict[str, Any]] = []
        for path in _list_entry_paths():
            entry = _read_entry(path)
            if entry:
                out.append(entry)
        return out


def enqueue(
    kind: str,
    payload: dict[str, Any],
    *,
    coalesce: bool = True,
    blob_files: Optional[dict[str, bytes]] = None,
) -> dict[str, Any]:
    """Append a durable mutation to the outbox. Returns the entry dict."""
    kind = str(kind or "").strip()
    if not kind:
        raise ValueError("outbox kind required")
    payload = dict(payload or {})

    with _LOCK:
        key = _coalesce_key(kind, payload) if coalesce else None
        if key:
            for path in _list_entry_paths():
                existing = _read_entry(path)
                if not existing:
                    continue
                if _coalesce_key(existing.get("kind", ""), existing.get("payload") or {}) == key:
                    blob_id = existing.get("id")
                    path.unlink(missing_ok=True)
                    if blob_id:
                        shutil.rmtree(_blobs_dir() / blob_id, ignore_errors=True)

        entry_id = uuid.uuid4().hex
        if blob_files:
            blob_root = _blobs_dir() / entry_id
            blob_root.mkdir(parents=True, exist_ok=True)
            stored: dict[str, str] = {}
            for rel, data in blob_files.items():
                rel_norm = str(rel).replace("\\", "/").lstrip("/")
                if not rel_norm or ".." in rel_norm.split("/"):
                    continue
                target = blob_root / rel_norm
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                stored[rel_norm] = rel_norm
            payload = {**payload, "blob_files": stored}

        entry = {
            "id": entry_id,
            "kind": kind,
            "payload": payload,
            "created_at": _utcnow(),
            "attempts": 0,
            "last_error": None,
        }
        if kind == KIND_SECRET_SET:
            note_secret_overlay(str(payload.get("key") or ""), str(payload.get("value") or ""))
        elif kind == KIND_SECRET_REMOVE:
            note_secret_overlay(str(payload.get("key") or ""), None)
        elif kind == KIND_SECRETS_MANY:
            for k, v in (payload.get("values") or {}).items():
                note_secret_overlay(str(k), str(v))
        path = _entries_dir() / f"{int(time.time() * 1000):013d}-{entry_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        logger.warning(
            "Mongo unavailable — queued durable write kind=%s id=%s (pending=%s)",
            kind,
            entry_id,
            pending_count(),
        )
        return entry


def _is_transient_mongo_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__
    if name in {
        "ServerSelectionTimeoutError",
        "NetworkTimeout",
        "AutoReconnect",
        "ConnectionFailure",
        "PyMongoError",
    }:
        return True
    try:
        from hermes_storage.errors import MongoStorageError

        if isinstance(exc, MongoStorageError):
            msg = str(exc).lower()
            return any(
                token in msg
                for token in (
                    "unavailable",
                    "timeout",
                    "connection",
                    "network",
                    "refused",
                    "reset",
                    "not available",
                )
            )
    except Exception:
        pass
    text = str(exc).lower()
    return any(
        token in text
        for token in ("timed out", "timeout", "connection", "network", "unreachable")
    )


def run_or_enqueue(
    kind: str,
    payload: dict[str, Any],
    apply_fn: Callable[[], Any],
    *,
    blob_files: Optional[dict[str, bytes]] = None,
    coalesce: bool = True,
) -> dict[str, Any]:
    """Try *apply_fn*; on transient Mongo failure enqueue and return status.

    Returns ``{"ok": True, "queued": False}`` on success, or
    ``{"ok": True, "queued": True, "entry": ...}`` when spooled.
    Re-raises non-transient errors.
    """
    try:
        apply_fn()
        return {"ok": True, "queued": False}
    except Exception as exc:
        if not _is_transient_mongo_error(exc):
            raise
        entry = enqueue(kind, payload, coalesce=coalesce, blob_files=blob_files)
        return {"ok": True, "queued": True, "entry": entry, "error": str(exc)}


def _apply_entry(storage: Any, entry: dict[str, Any]) -> None:
    kind = entry["kind"]
    payload = entry.get("payload") or {}
    entry_id = entry.get("id") or ""

    if kind == KIND_PROFILE_CONFIG:
        from hermes_storage.overlay import strip_machine_local

        storage.config.put(strip_machine_local(payload.get("config") or {}))
        storage._invalidate_config_readers()
        return
    if kind == KIND_MACHINE_OVERLAY:
        storage.machines.set_overlay(
            storage.machine_id, payload.get("overlay") or {}
        )
        storage._invalidate_config_readers()
        return
    if kind == KIND_SECRET_SET:
        storage._set_secret_direct(
            str(payload.get("key") or ""), str(payload.get("value") or "")
        )
        return
    if kind == KIND_SECRET_REMOVE:
        storage._remove_secret_direct(str(payload.get("key") or ""))
        return
    if kind == KIND_SECRETS_MANY:
        storage._set_secrets_many_direct(
            payload.get("values") or {},
            replace_profile=bool(payload.get("replace_profile", True)),
        )
        return
    if kind == KIND_SOUL:
        storage.soul.put({"content": str(payload.get("content") or "")})
        return
    if kind == KIND_MEMORY:
        target = str(payload.get("target") or "memory")
        storage.memories.save(target, str(payload.get("content") or ""))
        return
    if kind == KIND_CRON_JOBS:
        storage.ledgers.replace_one(
            "cron_jobs",
            {"key": "default"},
            {"key": "default", "data": payload.get("doc") or {"jobs": []}},
        )
        return
    if kind == KIND_SKILL_PUT:
        name = str(payload.get("name") or "").strip()
        skill_md = str(payload.get("skill_md") or "")
        files: dict[str, bytes] = {}
        blob_map = payload.get("blob_files") or {}
        blob_root = _blobs_dir() / entry_id
        for rel in blob_map:
            path = blob_root / str(rel)
            if path.is_file():
                files[str(rel)] = path.read_bytes()
        if "SKILL.md" not in files and skill_md:
            files["SKILL.md"] = skill_md.encode("utf-8")
        storage.skills.put_skill(
            {"name": name, "skill_md": skill_md, "path": name},
            files=files or None,
        )
        return
    if kind == KIND_SKILL_DELETE:
        storage.skills.delete_skill(str(payload.get("name") or ""))
        return
    raise ValueError(f"unknown outbox kind: {kind!r}")


def flush_outbox(*, limit: Optional[int] = None) -> dict[str, int]:
    """Replay pending entries into Mongo. Returns counts."""
    from hermes_storage import is_mongo_mode, require_storage

    if not is_mongo_mode():
        return {"flushed": 0, "failed": 0, "remaining": pending_count()}

    if not _FLUSH_LOCK.acquire(blocking=False):
        return {"flushed": 0, "failed": 0, "remaining": pending_count(), "busy": 1}

    flushed = 0
    failed = 0
    try:
        try:
            storage = require_storage()
            # Connectivity probe
            storage.skills.list_skills()
        except Exception as exc:
            logger.info("Outbox flush skipped — Mongo still unavailable: %s", exc)
            return {"flushed": 0, "failed": 0, "remaining": pending_count()}

        paths = _list_entry_paths()
        if limit is not None:
            paths = paths[: max(0, int(limit))]

        for path in paths:
            with _LOCK:
                entry = _read_entry(path)
                if not entry:
                    path.unlink(missing_ok=True)
                    continue
            try:
                _apply_entry(storage, entry)
                with _LOCK:
                    path.unlink(missing_ok=True)
                    blob_id = entry.get("id")
                    if blob_id:
                        shutil.rmtree(_blobs_dir() / blob_id, ignore_errors=True)
                    # Drop overlay for secrets we just flushed
                    kind = entry.get("kind")
                    payload = entry.get("payload") or {}
                    if kind == KIND_SECRET_SET:
                        _SECRET_OVERLAY.pop(str(payload.get("key") or ""), None)
                    elif kind == KIND_SECRET_REMOVE:
                        _SECRET_OVERLAY.pop(str(payload.get("key") or ""), None)
                    elif kind == KIND_SECRETS_MANY:
                        for k in (payload.get("values") or {}):
                            _SECRET_OVERLAY.pop(str(k), None)
                flushed += 1
            except Exception as exc:
                failed += 1
                entry["attempts"] = int(entry.get("attempts") or 0) + 1
                entry["last_error"] = str(exc)
                entry["last_attempt_at"] = _utcnow()
                try:
                    path.write_text(
                        json.dumps(entry, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                logger.warning(
                    "Outbox flush failed for %s (%s): %s",
                    entry.get("id"),
                    entry.get("kind"),
                    exc,
                )
                # Stop on first failure to preserve order for dependent writes
                break

        if flushed:
            logger.info(
                "Outbox flush: flushed=%s failed=%s remaining=%s",
                flushed,
                failed,
                pending_count(),
            )
        return {"flushed": flushed, "failed": failed, "remaining": pending_count()}
    finally:
        _FLUSH_LOCK.release()


def try_flush_outbox_best_effort() -> None:
    """Best-effort flush for heartbeat/startup — never raises."""
    try:
        if pending_count() <= 0:
            return
        flush_outbox()
    except Exception as exc:
        logger.debug("best-effort outbox flush failed: %s", exc)
