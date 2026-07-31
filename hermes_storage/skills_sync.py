"""Skills materialization from Mongo GridFS into a local cache directory."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def mongo_skills_cache_dir() -> Path:
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def writable_skills_dir() -> Path:
    """Directory skill writers should use.

    Mongo mode: ephemeral cache under ``cache/skills`` (durable store is Mongo).
    Classic mode: ``~/.hermes/skills``.
    """
    from hermes_storage import is_mongo_mode

    if is_mongo_mode():
        return mongo_skills_cache_dir()
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sync_skills_from_mongo() -> Optional[Path]:
    """Materialize all remote skills into the local cache. Returns cache root.

    In Mongo mode failures raise — never silently return the classic skills dir.
    """
    from hermes_storage import is_mongo_mode, require_storage

    if not is_mongo_mode():
        return None
    storage = require_storage()

    dest = mongo_skills_cache_dir()
    try:
        for skill in storage.skills.list_skills():
            name = skill.get("name")
            if not name:
                continue
            try:
                storage.skills.materialize(name, dest)
            except Exception as exc:
                logger.warning("Failed to materialize skill %s: %s", name, exc)
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
            rel = str(f.relative_to(skill_dir)).replace("\\", "/")
            files[rel] = f.read_bytes()
    storage.skills.put_skill(
        {"name": skill_name, "skill_md": body, "path": skill_name},
        files=files,
    )


def commit_skill_tree(skill_dir: Path, *, name: Optional[str] = None) -> None:
    """Persist a skill directory to Mongo and refresh its cache entry.

    No-op outside Mongo mode (classic FS remains durable).
    Fail-hard in Mongo mode — never leave cache-only skills.
    """
    from hermes_storage import is_mongo_mode, require_storage

    if not is_mongo_mode():
        return
    skill_dir = Path(skill_dir)
    skill_name = name or skill_dir.name
    try:
        upload_local_skill_tree(skill_dir, name=skill_name)
        require_storage().skills.materialize(skill_name, mongo_skills_cache_dir())
    except Exception as exc:
        from hermes_storage.errors import raise_mongo_unavailable
        raise_mongo_unavailable(
            f"failed to commit skill {skill_name!r} to Mongo: {exc}",
            cause=exc,
        )


def delete_remote_skill(name: str) -> bool:
    """Delete a skill from Mongo and drop its local cache dir.

    No-op outside Mongo mode (returns False). Fail-hard on Mongo errors.
    """
    from hermes_storage import is_mongo_mode, require_storage

    if not is_mongo_mode():
        return False
    name = str(name or "").strip()
    if not name:
        return False
    try:
        deleted = require_storage().skills.delete_skill(name)
    except Exception as exc:
        from hermes_storage.errors import raise_mongo_unavailable
        raise_mongo_unavailable(
            f"failed to delete skill {name!r} from Mongo: {exc}",
            cause=exc,
        )
    cache_dir = mongo_skills_cache_dir() / name
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    return bool(deleted)


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
    """If Mongo has zero skills, push from local ~/.hermes/skills then bundled tree.

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

    local_root = home / "skills"
    dirs = _iter_skill_dirs(local_root)
    if dirs:
        source = str(local_root)
        for d in dirs:
            upload_local_skill_tree(d, name=d.name)
            uploaded += 1
    else:
        # Fall back to checkout / package bundled skills
        candidates = [
            home / "hermes-agent" / "skills",
            Path(__file__).resolve().parents[1] / "skills",
        ]
        for cand in candidates:
            dirs = _iter_skill_dirs(cand)
            if not dirs:
                continue
            source = str(cand)
            for d in dirs:
                upload_local_skill_tree(d, name=d.name)
                uploaded += 1
            break

    return {"existing": 0, "uploaded": uploaded, "source": source}


def seed_profile_defaults_if_empty(
    storage: Optional[object] = None,
    *,
    home: Optional[Path] = None,
) -> dict[str, int]:
    """Push local config/soul/memories/secrets into Mongo when profile is empty."""
    from hermes_constants import get_hermes_home
    from hermes_storage import require_storage
    from hermes_storage.local.migrate import export_local_home, import_payload_to_storage

    st = storage or require_storage()
    home = Path(home) if home else get_hermes_home()

    # Only import "identity" bits if profile looks empty
    cfg = {}
    if hasattr(st, "load_profile_config"):
        cfg = st.load_profile_config() or {}
    elif hasattr(st, "config"):
        cfg = st.config.get("default") or {}
    soul = st.load_soul() if hasattr(st, "load_soul") else ""
    if cfg or (soul or "").strip():
        return {"skipped": 1, "reason": "profile_already_set"}

    payload = export_local_home(home)
    # Don't re-upload skills here (handled separately); still OK if duplicated
    counts = import_payload_to_storage(st, payload)
    return counts
