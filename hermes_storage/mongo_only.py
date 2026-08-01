"""Mongo-only fork helpers: require mode, scrub classic durable leftovers.

This fork does not support classic local durable state as a product mode.
Local disk keeps bootstrap/certs/logs/cache only.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Iterable, Optional

from hermes_constants import get_hermes_home
from hermes_storage.bootstrap import is_mongo_mode
from hermes_storage.errors import MongoStorageError

logger = logging.getLogger(__name__)

# Test-only escape hatch for the upstream suite that still exercises classic
# paths under an isolated HERMES_HOME. Never set in production installs.
_CLASSIC_ALLOW_ENV = "HERMES_ALLOW_CLASSIC"

_SCRUB_FILES = (
    "config.yaml",
    ".env",
    "SOUL.md",
    "auth.json",
)
_SCRUB_DIRS = (
    "memories",
    "skills",  # classic tree — not cache/skills
    "pairing",
    "hooks",
)
_SCRUB_NESTED_FILES = (
    ("cron", "jobs.json"),
    ("sessions", "sessions.json"),
)


def classic_allowed() -> bool:
    """True when classic durable FS is explicitly allowed (tests only)."""
    return os.environ.get(_CLASSIC_ALLOW_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def require_mongo_mode(*, surface: str = "runtime") -> None:
    """Fail hard unless Mongo bootstrap is present (or classic is test-allowed)."""
    if is_mongo_mode():
        return
    if classic_allowed():
        return
    raise MongoStorageError(
        f"This Hermes MongoDB fork requires Mongo mode for {surface}. "
        "Connect with `hermes db connect` or run "
        "`hermes storage init-bootstrap --uri ...`. "
        "Classic local durable storage (config.yaml / .env / skills / memories) "
        "is not supported."
    )


def ensure_mongo_durable(*, surface: str) -> None:
    """Call at the top of durable writers: Mongo required unless tests allow classic."""
    require_mongo_mode(surface=surface)


def scrub_classic_durable_home(home: Optional[Path] = None) -> dict[str, int]:
    """Move leftover classic durable paths under ``.orphan/`` after Mongo seed.

    Keeps: bootstrap.yaml, certs/, logs/, cache/, sqlite working copies
    (kanban/projects), enroll material.
    """
    root = Path(home) if home is not None else get_hermes_home()
    orphan = root / ".orphan"
    moved = 0
    skipped = 0

    def _quarantine(src: Path) -> None:
        nonlocal moved, skipped
        if not src.exists() and not src.is_symlink():
            skipped += 1
            return
        try:
            orphan.mkdir(parents=True, exist_ok=True)
            dest = orphan / src.name
            if dest.exists():
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink(missing_ok=True)
            shutil.move(str(src), str(dest))
            moved += 1
            logger.info("Scrubbed classic durable path → %s", dest)
        except OSError as exc:
            skipped += 1
            logger.warning("Could not scrub %s: %s", src, exc)

    for name in _SCRUB_FILES:
        _quarantine(root / name)

    for name in _SCRUB_DIRS:
        _quarantine(root / name)

    for parts in _SCRUB_NESTED_FILES:
        path = root.joinpath(*parts)
        if not path.exists() and not path.is_symlink():
            skipped += 1
            continue
        try:
            rel_parent = path.parent.relative_to(root)
            dest_dir = orphan.joinpath(rel_parent)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / path.name
            if dest.exists():
                dest.unlink(missing_ok=True)
            shutil.move(str(path), str(dest))
            moved += 1
            logger.info("Scrubbed classic durable path → %s", dest)
        except (OSError, ValueError) as exc:
            skipped += 1
            logger.warning("Could not scrub %s: %s", path, exc)

    return {"moved": moved, "skipped": skipped}


def is_classic_durable_path(path: Path, *, home: Optional[Path] = None) -> bool:
    """True when *path* is a classic durable HERMES_HOME location (not cache)."""
    root = Path(home) if home is not None else get_hermes_home()
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        rel = resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return False

    parts = rel.parts
    if not parts:
        return False

    # Ephemeral / allowed local
    if parts[0] in {
        "bootstrap.yaml",
        "certs",
        "logs",
        "cache",
        "image_cache",
        "audio_cache",
        ".orphan",
    }:
        return False
    if parts[0] == "cache":
        return False

    name = parts[0]
    if name in _SCRUB_FILES or name in _SCRUB_DIRS:
        return True
    if name == "cron" and len(parts) >= 2 and parts[1] == "jobs.json":
        return True
    if name == "sessions" and len(parts) >= 2 and parts[1] == "sessions.json":
        return True
    if name == "state.db":
        return True
    return False


def durable_write_blocked_message(path: Path) -> str:
    return (
        f"Refusing to write {path}: this Hermes MongoDB fork stores durable "
        "state in MongoDB, not on disk. Use skill_manage / memory / "
        "`hermes config` / `hermes config set` / secret APIs instead. "
        "Local durable files are scrubbed and are not the source of truth."
    )
