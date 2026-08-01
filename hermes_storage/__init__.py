"""Remote MongoDB-backed storage for Hermes Agent.

When bootstrap.yaml (or HERMES_MONGO_URI) is present, durable state lives in
MongoDB rather than local files / SQLite. Local disk keeps only the bootstrap
file plus optional caches (skill materialize, logs, PIDs).
"""

from __future__ import annotations

from hermes_storage.bootstrap import (
    BootstrapConfig,
    get_bootstrap,
    is_mongo_mode,
    load_bootstrap,
    reset_bootstrap_cache,
)
from hermes_storage.errors import MongoStorageError
from hermes_storage.factory import get_storage, require_storage, reset_storage
from hermes_storage.mongo_only import (
    classic_allowed,
    ensure_mongo_durable,
    require_mongo_mode,
    scrub_classic_durable_home,
)

__all__ = [
    "BootstrapConfig",
    "MongoStorageError",
    "classic_allowed",
    "ensure_mongo_durable",
    "get_bootstrap",
    "is_mongo_mode",
    "load_bootstrap",
    "require_mongo_mode",
    "reset_bootstrap_cache",
    "scrub_classic_durable_home",
    "get_storage",
    "require_storage",
    "reset_storage",
]
