"""``hermes mongo status`` — human-readable inventory of Mongo durable state."""

from __future__ import annotations

from typing import Any, Callable, Optional


def build_mongo_parser(subparsers, *, cmd_mongo: Callable) -> None:
    parser = subparsers.add_parser(
        "mongo",
        help="MongoDB inventory (hermes mongo status)",
        description="Show what Hermes has stored in MongoDB",
    )
    mongo_sub = parser.add_subparsers(dest="mongo_command")
    try:
        mongo_sub.required = False
    except (AttributeError, TypeError):
        pass

    status = mongo_sub.add_parser(
        "status",
        help="Human-readable summary of skills, memory, sessions, soul, …",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the compact table",
    )
    status.set_defaults(func=cmd_mongo)

    seed = mongo_sub.add_parser(
        "seed-skills",
        help="If Mongo skills are empty, upload bundled skills then materialize cache",
    )
    seed.set_defaults(func=cmd_mongo)

    inspect_skill = mongo_sub.add_parser(
        "inspect-skill",
        help="Show Mongo skill metadata (hash, revision, updated_by, files)",
    )
    inspect_skill.add_argument("name", help="Skill name")
    inspect_skill.set_defaults(func=cmd_mongo)

    # Bare `hermes mongo` → status
    parser.set_defaults(func=cmd_mongo, mongo_command="status")


def _count(collection, query: Optional[dict] = None) -> int:
    try:
        return int(collection.count_documents(query or {}))
    except Exception:
        return 0


def _chars(text: Any) -> int:
    if text is None:
        return 0
    return len(str(text))


def collect_mongo_inventory(storage) -> dict[str, Any]:
    """Gather compact inventory stats from an open HermesStorage."""
    boot = storage.bootstrap
    profile_db = storage.profile_db
    shared_db = storage.shared_db

    skills_n = 0
    try:
        skills_n = len(storage.skills.list_skills())
    except Exception:
        skills_n = _count(shared_db["skills"])

    soul = ""
    try:
        soul = storage.load_soul() or ""
    except Exception:
        pass

    memory = ""
    user_mem = ""
    try:
        memory = storage.memories.load("memory") or ""
        user_mem = storage.memories.load("user") or ""
    except Exception:
        pass

    sessions_n = _count(profile_db["sessions"])
    messages_n = _count(profile_db["messages"])

    secrets_raw: dict[str, Any] = {}
    try:
        if hasattr(storage, "get_effective_secrets"):
            secrets_raw = storage.get_effective_secrets() or {}
        else:
            secrets_raw = storage.secrets.get_all() or {}
    except Exception:
        pass
    secret_keys = [
        k for k in secrets_raw
        if not str(k).startswith("__") and secrets_raw.get(k) not in (None, "")
    ]
    has_auth = bool(secrets_raw.get("__auth_json__"))

    config_doc: dict[str, Any] = {}
    try:
        if hasattr(storage, "load_profile_config"):
            config_doc = storage.load_profile_config() or {}
        else:
            config_doc = storage.config.get("default") or {}
    except Exception:
        pass
    model = config_doc.get("model") if isinstance(config_doc, dict) else {}
    if not isinstance(model, dict):
        model = {}

    machines_n = 0
    try:
        machines_n = len(storage.machines.list_machines())
    except Exception:
        machines_n = _count(profile_db["machines"])

    wiki_n = 0
    try:
        if getattr(storage, "wiki", None) is not None:
            wiki_n = len(storage.wiki.list_pages(limit=5000))
        else:
            wiki_n = _count(shared_db["wiki_pages"])
    except Exception:
        wiki_n = _count(shared_db["wiki_pages"])

    nodes_n = _count(shared_db["cluster_nodes"])
    online_n = 0
    try:
        online = storage.cluster.list_nodes(online_within_s=120.0)
        online_n = len(online)
    except Exception:
        pass

    cron_n = 0
    try:
        # Cron jobs live in profile ledgers when using Mongo cron store
        cron_n = _count(profile_db["cron_jobs"])
        if cron_n == 0:
            cron_n = _count(profile_db["ledgers"], {"kind": "cron_jobs"})
    except Exception:
        pass

    outbox_pending = 0
    try:
        from hermes_storage.outbox import pending_count

        outbox_pending = pending_count()
    except Exception:
        pass

    messaging_owner = ""
    try:
        state = storage.cluster.get_state() or {}
        messaging_owner = state.get("messaging_owner") or state.get("active_node_id") or ""
    except Exception:
        pass

    return {
        "mongo_mode": True,
        "profile": getattr(boot, "profile", "default"),
        "profile_db": getattr(boot, "profile_db", ""),
        "shared_db": getattr(boot, "shared_db", ""),
        "machine_id": getattr(storage, "machine_id", ""),
        "skills": skills_n,
        "soul_chars": _chars(soul.strip()),
        "memory_chars": _chars(memory.strip()),
        "user_memory_chars": _chars(user_mem.strip()),
        "sessions": sessions_n,
        "messages": messages_n,
        "secrets": len(secret_keys),
        "auth_store": has_auth,
        "config_keys": len(config_doc) if isinstance(config_doc, dict) else 0,
        "model_default": model.get("default") or model.get("model") or "",
        "model_provider": model.get("provider") or "",
        "machines": machines_n,
        "wiki_pages": wiki_n,
        "cluster_nodes": nodes_n,
        "cluster_online": online_n,
        "cron_jobs": cron_n,
        "outbox_pending": outbox_pending,
        "messaging_owner": messaging_owner,
    }


def format_mongo_inventory(inv: dict[str, Any]) -> str:
    """Compact human-readable table (Russian labels, as requested)."""
    lines = [
        "MongoDB status",
        "-" * 55,
        f"Профиль: {inv.get('profile') or '—'}  ({inv.get('profile_db') or '—'})",
        f"Shared DB: {inv.get('shared_db') or '—'}",
        f"Machine: {inv.get('machine_id') or '—'}",
        "-" * 55,
        f"Скиллы: {inv.get('skills', 0)}",
        f"Wiki: {inv.get('wiki_pages', 0)} страниц",
        f"Soul: {inv.get('soul_chars', 0)} символов",
        f"Память (MEMORY): {inv.get('memory_chars', 0)} символов",
        f"Память (USER): {inv.get('user_memory_chars', 0)} символов",
        f"История сессий: {inv.get('sessions', 0)} сессий / {inv.get('messages', 0)} сообщений",
        f"Секреты (API keys): {inv.get('secrets', 0)}",
        f"Auth store: {'да' if inv.get('auth_store') else 'нет'}",
        f"Config: {inv.get('config_keys', 0)} секций",
    ]
    provider = inv.get("model_provider") or ""
    model = inv.get("model_default") or ""
    if provider or model:
        lines.append(f"Модель: {provider or '—'} / {model or '—'}")
    lines.append(
        f"Машины (overlays): {inv.get('machines', 0)}"
    )
    lines.append(
        f"Кластер: {inv.get('cluster_online', 0)} online / {inv.get('cluster_nodes', 0)} nodes"
    )
    if inv.get("messaging_owner"):
        lines.append(f"Messaging owner: {inv.get('messaging_owner')}")
    if inv.get("cron_jobs"):
        lines.append(f"Cron jobs: {inv.get('cron_jobs', 0)}")
    lines.append(f"Outbox pending: {inv.get('outbox_pending', 0)}")
    lines.append("-" * 55)
    return "\n".join(lines)


def cmd_mongo_inspect_skill(name: str) -> int:
    """Print Mongo metadata + GridFS file list for one skill."""
    import json

    from hermes_storage import get_storage, is_mongo_mode

    if not is_mongo_mode():
        print("Mongo mode: OFF")
        return 1
    storage = get_storage(force=True)
    skill = storage.skills.get_skill(name)
    if not skill:
        print(f"Skill not found in Mongo: {name}")
        return 1
    files = sorted((skill.get("file_ids") or {}).keys())
    payload = {
        "name": skill.get("name"),
        "status": skill.get("status") or "ready",
        "revision": skill.get("revision"),
        "content_hash": skill.get("content_hash"),
        "updated_at": skill.get("updated_at"),
        "updated_by": skill.get("updated_by"),
        "skill_md_chars": len(str(skill.get("skill_md") or "")),
        "files": files,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_mongo_seed_skills() -> int:
    """Push bundled/local skills into Mongo when empty, then materialize cache."""
    from hermes_storage import get_storage, is_mongo_mode
    from hermes_storage.skills_sync import seed_shared_skills_if_empty, sync_skills_from_mongo

    if not is_mongo_mode():
        print("Mongo mode: OFF — connect first: hermes db connect")
        return 1
    storage = get_storage(force=True)
    result = seed_shared_skills_if_empty(storage)
    cache = sync_skills_from_mongo()
    print(
        f"Seed: uploaded={result.get('uploaded', 0)} "
        f"existing={result.get('existing', 0)} "
        f"source={result.get('source')}"
    )
    print(f"Cache: {cache}")
    print(f"Mongo skills now: {len(storage.skills.list_skills())}")
    return 0


def cmd_mongo_status(*, as_json: bool = False) -> int:
    """Entry used by ``hermes mongo status``. Returns process exit code."""
    import json

    from hermes_storage import get_storage, is_mongo_mode, load_bootstrap

    if not is_mongo_mode():
        print("Mongo mode: OFF")
        print("  Нет bootstrap.yaml / HERMES_MONGO_URI.")
        print("  Подключитесь: hermes db connect")
        return 1

    boot = load_bootstrap(force=True)
    try:
        storage = get_storage(force=True)
        storage.client.admin.command("ping")
    except Exception as exc:
        print("MongoDB status")
        print("-" * 55)
        print(f"Соединение: ОШИБКА ({exc})")
        if boot:
            print(f"Профиль: {boot.profile} → {boot.profile_db}")
        return 2

    inv = collect_mongo_inventory(storage)
    if as_json:
        print(json.dumps(inv, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_mongo_inventory(inv))
    return 0
