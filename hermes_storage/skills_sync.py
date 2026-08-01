"""Skills materialization from Mongo GridFS into a local cache directory."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Process-level fingerprint of the last successful Mongo→cache sync.
# Avoids re-pulling GridFS on every get_skills_dir() within one process.
_SYNC_ONCE: Optional[str] = None


def mongo_skills_cache_dir() -> Path:
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def writable_skills_dir() -> Path:
    """Directory skill writers should use.

    Mongo mode (this fork's product path): ephemeral cache under
    ``cache/skills`` (durable store is Mongo).
    Classic mode: only when ``HERMES_ALLOW_CLASSIC`` is set (tests).
    """
    from hermes_storage import classic_allowed, is_mongo_mode

    if is_mongo_mode() or not classic_allowed():
        return mongo_skills_cache_dir()
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def invalidate_skills_sync_cache() -> None:
    """Force the next ``sync_skills_from_mongo`` to re-check the cache."""
    global _SYNC_ONCE
    _SYNC_ONCE = None


def _fmt_updated_at(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _skills_collection_fingerprint(listed: list[dict[str, Any]]) -> str:
    parts = [
        f"{s.get('name') or ''}:{_fmt_updated_at(s.get('updated_at'))}"
        for s in listed
        if s.get("name")
    ]
    return "|".join(sorted(parts))


def _cache_looks_complete(dest: Path, listed: list[dict[str, Any]]) -> bool:
    """True when every listed skill has a SKILL.md under the cache root."""
    if not listed:
        return True
    for skill in listed:
        name = skill.get("name")
        if not name:
            continue
        if not (dest / name / "SKILL.md").is_file():
            return False
    return True


def sync_skills_from_mongo() -> Optional[Path]:
    """Materialize remote skills into the local cache. Returns cache root.

    Incremental: skips skills whose local ``.mongo_updated_at`` matches Mongo,
    and skips the whole pass when the collection fingerprint matches the
    process memo or on-disk ``.mongo_skills_stamp``.

    In Mongo mode failures raise — never silently return the classic skills dir.
    If the shared skills collection is empty, seed bundled skills once, then
    materialize (so a freshly connected agent is never stuck with zero skills).
    """
    global _SYNC_ONCE
    from hermes_storage import is_mongo_mode, require_storage

    if not is_mongo_mode():
        return None
    storage = require_storage()

    dest = mongo_skills_cache_dir()
    stamp_path = dest / ".mongo_skills_stamp"
    try:
        listed = storage.skills.list_skills()
        if not listed:
            # First-run / wiped local tree: push bundled into Mongo, then pull.
            try:
                seed = seed_shared_skills_if_empty(storage)
                logger.info(
                    "Mongo skills empty — seed result: uploaded=%s source=%s",
                    seed.get("uploaded"),
                    seed.get("source"),
                )
            except Exception as seed_exc:
                logger.warning("Auto-seed of Mongo skills failed: %s", seed_exc)
            listed = storage.skills.list_skills()

        fingerprint = _skills_collection_fingerprint(listed)
        if _SYNC_ONCE == fingerprint and _cache_looks_complete(dest, listed):
            return dest
        try:
            if (
                stamp_path.is_file()
                and stamp_path.read_text(encoding="utf-8").strip() == fingerprint
                and _cache_looks_complete(dest, listed)
            ):
                _SYNC_ONCE = fingerprint
                return dest
        except OSError:
            pass

        pulled = 0
        skipped = 0
        for skill in listed:
            name = skill.get("name")
            if not name:
                continue
            remote_ts = _fmt_updated_at(skill.get("updated_at"))
            skill_root = dest / name
            local_stamp = skill_root / ".mongo_updated_at"
            try:
                if (
                    (skill_root / "SKILL.md").is_file()
                    and local_stamp.is_file()
                    and local_stamp.read_text(encoding="utf-8").strip() == remote_ts
                    and remote_ts
                ):
                    skipped += 1
                    continue
            except OSError:
                pass
            try:
                storage.skills.materialize(name, dest)
                try:
                    local_stamp.write_text(remote_ts, encoding="utf-8")
                except OSError:
                    pass
                pulled += 1
            except Exception as exc:
                logger.warning("Failed to materialize skill %s: %s", name, exc)
        try:
            stamp_path.write_text(fingerprint, encoding="utf-8")
        except OSError:
            pass
        _SYNC_ONCE = fingerprint
        if pulled:
            logger.info(
                "Mongo skills sync: pulled=%s skipped=%s total=%s",
                pulled,
                skipped,
                len(listed),
            )
    except Exception as exc:
        from hermes_storage.errors import raise_mongo_unavailable
        raise_mongo_unavailable(f"skills list failed: {exc}", cause=exc)
    return dest


def upload_local_skill_tree(skill_dir: Path, *, name: Optional[str] = None) -> None:
    """Push a local skill directory into Mongo (shared skills DB)."""
    from hermes_storage import require_storage

    storage = require_storage()

    skill_dir = Path(skill_dir)
    skill_name = name or skill_dir.name
    skill_md = skill_dir / "SKILL.md"
    body = skill_md.read_text(encoding="utf-8") if skill_md.is_file() else ""
    files: dict[str, bytes] = {}
    for f in skill_dir.rglob("*"):
        if f.is_file():
            # Skip sync metadata / stamps — not part of the skill payload
            if f.name in {".mongo_updated_at", ".mongo_skills_stamp", ".bundled_manifest"}:
                continue
            if f.name.startswith(".") and f.suffix == ".lock":
                continue
            rel = str(f.relative_to(skill_dir)).replace("\\", "/")
            files[rel] = f.read_bytes()
    storage.skills.put_skill(
        {"name": skill_name, "skill_md": body, "path": skill_name},
        files=files,
    )


def _refresh_skill_cache_entry(skill_name: str, skill_dir: Path) -> None:
    """Rematerialize *skill_name* into the tree the writer just edited.

    Mongo materialize defaults to a flat ``cache/skills/<name>/`` path. Agent
    edits may land under a category (``cache/skills/<cat>/<name>/``). Refresh
    the edited directory's parent so skill_view cannot keep reading a stale
    sibling copy, and keep the flat cache entry in sync when it differs.
    """
    from hermes_storage import require_storage

    storage = require_storage()
    cache_root = mongo_skills_cache_dir()
    skill_dir = Path(skill_dir).resolve()
    targets: list[Path] = []

    try:
        skill_dir.relative_to(cache_root.resolve())
        parent = skill_dir.parent
        if parent.resolve() != cache_root.resolve() or skill_dir.name == skill_name:
            targets.append(parent)
    except (ValueError, OSError):
        pass

    flat_parent = cache_root
    if not any(t.resolve() == flat_parent.resolve() for t in targets):
        targets.append(flat_parent)

    meta = storage.skills.get_skill(skill_name) or {}
    remote_ts = _fmt_updated_at(meta.get("updated_at"))
    for parent in targets:
        storage.skills.materialize(skill_name, parent)
        try:
            stamp = Path(parent) / skill_name / ".mongo_updated_at"
            stamp.write_text(remote_ts, encoding="utf-8")
        except OSError:
            pass

    # Collection fingerprint must match list_skills() or the next
    # sync_skills_from_mongo will treat the cache as stale and may race.
    try:
        listed = storage.skills.list_skills()
        fingerprint = _skills_collection_fingerprint(listed)
        (cache_root / ".mongo_skills_stamp").write_text(fingerprint, encoding="utf-8")
        global _SYNC_ONCE
        _SYNC_ONCE = fingerprint
    except OSError:
        pass


def commit_skill_tree(skill_dir: Path, *, name: Optional[str] = None) -> None:
    """Persist a skill directory to Mongo and refresh its cache entry.

    No-op outside Mongo mode (classic FS remains durable).
    On transient Mongo failure, spool into the local outbox and keep the
    cache tree (agent continues); flush happens on reconnect.
    """
    from hermes_storage import is_mongo_mode, require_storage
    from hermes_storage.outbox import KIND_SKILL_PUT, run_or_enqueue

    if not is_mongo_mode():
        return
    skill_dir = Path(skill_dir)
    skill_name = name or skill_dir.name
    skill_md_path = skill_dir / "SKILL.md"
    body = skill_md_path.read_text(encoding="utf-8") if skill_md_path.is_file() else ""
    files: dict[str, bytes] = {}
    for f in skill_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.name in {".mongo_updated_at", ".mongo_skills_stamp", ".bundled_manifest"}:
            continue
        if f.name.startswith(".") and f.suffix == ".lock":
            continue
        rel = str(f.relative_to(skill_dir)).replace("\\", "/")
        files[rel] = f.read_bytes()

    def _apply() -> None:
        upload_local_skill_tree(skill_dir, name=skill_name)
        invalidate_skills_sync_cache()
        _refresh_skill_cache_entry(skill_name, skill_dir)

    status = run_or_enqueue(
        KIND_SKILL_PUT,
        {"name": skill_name, "skill_md": body},
        _apply,
        blob_files=files,
    )
    if status.get("queued"):
        logger.warning(
            "Skill %r queued in Mongo outbox (cache kept locally until flush)",
            skill_name,
        )


def delete_remote_skill(name: str) -> bool:
    """Delete a skill from Mongo and drop its local cache dir.

    No-op outside Mongo mode (returns False). Transient Mongo failures spool
    a delete into the outbox and still drop the local cache.
    """
    from hermes_storage import is_mongo_mode, require_storage
    from hermes_storage.outbox import KIND_SKILL_DELETE, run_or_enqueue

    if not is_mongo_mode():
        return False
    name = str(name or "").strip()
    if not name:
        return False
    result = {"deleted": False}

    def _apply() -> None:
        result["deleted"] = bool(require_storage().skills.delete_skill(name))

    status = run_or_enqueue(
        KIND_SKILL_DELETE,
        {"name": name},
        _apply,
    )
    invalidate_skills_sync_cache()
    cache_dir = mongo_skills_cache_dir() / name
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    try:
        (mongo_skills_cache_dir() / ".mongo_skills_stamp").unlink(missing_ok=True)
    except OSError:
        pass
    if status.get("queued"):
        return True
    return bool(result["deleted"])


def commit_all_skill_trees(root: Optional[Path] = None) -> int:
    """Upload every SKILL.md tree under *root* (default: writable skills dir)."""
    from hermes_storage import is_mongo_mode

    if not is_mongo_mode():
        return 0
    root = Path(root) if root else writable_skills_dir()
    count = 0
    for skill_dir in _iter_skill_dirs(root):
        try:
            rel_parts = skill_dir.relative_to(root).parts
        except ValueError:
            continue
        # Skip hub staging / archive / restore metadata trees
        if any(p.startswith(".") for p in rel_parts):
            continue
        commit_skill_tree(skill_dir, name=skill_dir.name)
        count += 1
    return count


def _iter_skill_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for skill_md in root.rglob("SKILL.md"):
        found.append(skill_md.parent)
    return found


def seed_shared_skills_if_empty(
    storage: Optional[object] = None,
    *,
    home: Optional[Path] = None,
) -> dict[str, int]:
    """If Mongo has zero skills, push from local cache/classic then bundled tree.

    Returns counts: {existing, uploaded, source}.
    """
    from hermes_constants import get_hermes_home
    from hermes_storage import require_storage

    st = storage or require_storage()
    existing = len(st.skills.list_skills())
    if existing > 0:
        return {"existing": existing, "uploaded": 0, "source": "mongo"}

    home = Path(home) if home else get_hermes_home()
    uploaded = 0
    source = "none"

    # Prefer already-materialized / legacy local trees, then bundled checkout.
    candidates = [
        home / "cache" / "skills",
        home / "skills",
        home / "hermes-agent" / "skills",
        Path(__file__).resolve().parents[1] / "skills",
    ]
    for cand in candidates:
        dirs = _iter_skill_dirs(cand)
        if not dirs:
            continue
        source = str(cand)
        for d in dirs:
            # Skip hidden staging trees under cache/skills
            try:
                rel = d.relative_to(cand)
                if any(p.startswith(".") for p in rel.parts):
                    continue
            except ValueError:
                pass
            upload_local_skill_tree(d, name=d.name)
            uploaded += 1
        break

    # If still empty, run classic bundled sync into the writable cache, then upload.
    # Pass mongo_commit=False so sync_skills does not re-enter commit_all.
    if uploaded == 0:
        try:
            from tools.skills_sync import sync_skills

            sync_skills(quiet=True, mongo_commit=False)
            cache = mongo_skills_cache_dir()
            for d in _iter_skill_dirs(cache):
                try:
                    rel = d.relative_to(cache)
                    if any(p.startswith(".") for p in rel.parts):
                        continue
                except ValueError:
                    pass
                upload_local_skill_tree(d, name=d.name)
                uploaded += 1
            if uploaded:
                source = f"sync_skills→{cache}"
        except Exception as exc:
            logger.warning("bundled sync_skills seed failed: %s", exc)

    invalidate_skills_sync_cache()
    return {"existing": 0, "uploaded": uploaded, "source": source}


def seed_profile_defaults_if_empty(
    storage: Optional[object] = None,
    *,
    home: Optional[Path] = None,
) -> dict[str, int]:
    """Fill missing profile pieces from leftover local files.

    Config, secrets, soul, and memories are seeded **independently** — having
    a soul document must not skip importing API keys / providers from a local
    ``.env`` / ``config.yaml`` left after enroll.
    """
    from hermes_constants import get_hermes_home
    from hermes_storage import require_storage
    from hermes_storage.local.migrate import export_local_home

    st = storage or require_storage()
    home = Path(home) if home else get_hermes_home()
    counts = {
        "config": 0,
        "secrets": 0,
        "soul": 0,
        "memories": 0,
        "skipped": 0,
    }

    try:
        payload = export_local_home(home)
    except Exception as exc:
        logger.warning("seed_profile_defaults: export_local_home failed: %s", exc)
        return counts

    cfg: dict = {}
    if hasattr(st, "load_profile_config"):
        cfg = st.load_profile_config() or {}
    elif hasattr(st, "config"):
        cfg = st.config.get("default") or {}
    if not cfg and payload.get("config"):
        try:
            st.save_profile_config(payload["config"])
            if hasattr(st, "save_machine_overlay_from_config"):
                st.save_machine_overlay_from_config(payload["config"])
            counts["config"] = 1
        except Exception as exc:
            logger.warning("seed_profile_defaults: config import failed: %s", exc)

    existing_secrets: dict = {}
    try:
        existing_secrets = dict(st.secrets.get_all() or {})
    except Exception:
        existing_secrets = {}
    usable = {
        k: v
        for k, v in existing_secrets.items()
        if not str(k).startswith("__") and v not in (None, "")
    }
    if not usable:
        secrets = dict(payload.get("secrets") or {})
        auth = payload.get("auth")
        if auth is not None:
            import json

            secrets["__auth_json__"] = (
                auth if isinstance(auth, str) else json.dumps(auth)
            )
        if secrets:
            try:
                merged = {**existing_secrets, **secrets}
                if hasattr(st, "set_secrets_many"):
                    st.set_secrets_many(merged)
                else:
                    st.secrets.set_many(merged)
                counts["secrets"] = len(secrets)
            except Exception as exc:
                logger.warning("seed_profile_defaults: secrets import failed: %s", exc)

    soul = ""
    try:
        soul = st.load_soul() if hasattr(st, "load_soul") else ""
    except Exception:
        soul = ""
    if not (soul or "").strip() and payload.get("soul"):
        try:
            st.save_soul(payload["soul"])
            counts["soul"] = 1
        except Exception as exc:
            logger.warning("seed_profile_defaults: soul import failed: %s", exc)

    memories = payload.get("memories") or {}
    for key in ("memory", "user"):
        try:
            current = st.memories.load(key) if hasattr(st, "memories") else ""
        except Exception:
            current = ""
        if not (current or "").strip() and memories.get(key):
            try:
                st.memories.save(key, memories[key])
                counts["memories"] += 1
            except Exception as exc:
                logger.warning(
                    "seed_profile_defaults: memory %s import failed: %s", key, exc
                )

    if not any(counts[k] for k in ("config", "secrets", "soul", "memories")):
        counts["skipped"] = 1
    return counts
