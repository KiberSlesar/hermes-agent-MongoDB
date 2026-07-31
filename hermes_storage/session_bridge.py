"""Mongo-backed SessionDB adapter preserving the public SessionDB surface."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_TIMESTAMP_FIELDS = frozenset(
    {"started_at", "ended_at", "last_active", "updated_at", "created_at"}
)


def _session_doc_to_sessiondb_shape(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert Mongo BSON datetimes to the float timestamp SessionDB exposes.

    SQLite-backed ``SessionDB`` returns Unix timestamps. Dashboard and gateway
    callers therefore perform arithmetic and comparisons on these fields. Mongo
    returns timezone-aware ``datetime`` instances, so normalize at the adapter
    boundary rather than teaching every consumer about BSON types.
    """
    if doc is None:
        return None
    out = dict(doc)
    for field in _TIMESTAMP_FIELDS:
        value = out.get(field)
        if not isinstance(value, datetime):
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        out[field] = value.timestamp()
    # Mongo's write path maintains updated_at; SessionDB list consumers expect
    # last_active, so use it when older Mongo documents do not carry that key.
    if out.get("last_active") is None and out.get("updated_at") is not None:
        out["last_active"] = out["updated_at"]
    if out.get("id") is None and out.get("session_id") is not None:
        out["id"] = out["session_id"]
    return out


def _mongo_message_id(session_id: str, message_index: Any) -> int:
    """Stable integer message id for SessionDB-compatible recall APIs."""
    key = f"{session_id}:{message_index}".encode("utf-8")
    return int.from_bytes(
        hashlib.blake2b(key, digest_size=8).digest(), "big"
    ) & ((1 << 63) - 1)


class MongoSessionAdapter:
    """Drop-in SessionDB backed by MongoSessionStore.

    Unknown public methods raise :class:`AttributeError` (fail-loud) — never
    silently return ``None`` stubs, which would hide split-brain bugs.
    """

    MAX_TITLE_LENGTH = 200

    def __init__(self, read_only: bool = False):
        from hermes_storage.factory import require_storage

        storage = require_storage()
        self._store = storage.sessions
        self._storage = storage
        self.read_only = read_only
        self.db_path = Path("<mongo>")
        self._lock = threading.Lock()
        self._wal_active = False
        self._write_count = 0
        self._mongo_mode = True
        self._message_state_cache: Dict[int, Dict[str, Any]] = {}

    def close(self) -> None:
        return None

    def create_session(self, session_id: str, source: str, **kwargs) -> str:
        return self._store.create_session(session_id, source, **kwargs)

    def ensure_session(self, session_id: str, source: str = "unknown", **kwargs) -> str:
        existing = self._store.get_session(session_id)
        if existing:
            return session_id
        return self.create_session(session_id, source, **kwargs)

    def end_session(self, session_id: str, end_reason: str) -> None:
        self._store.end_session(session_id, end_reason)

    def reopen_session(self, session_id: str) -> None:
        self._store.update_session(session_id, ended_at=None, end_reason=None)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = _session_doc_to_sessiondb_shape(self._store.get_session(session_id))
        if session is not None:
            session["message_count"] = len(
                self._store.get_messages(session_id, include_inactive=True)
            )
        return session

    def delete_session(self, session_id: str) -> bool:
        return self._store.delete_session(session_id)

    def delete_sessions(self, session_ids: List[str]) -> int:
        return sum(1 for sid in session_ids if self.delete_session(sid))

    def list_sessions_rich(self, *, limit: int = 50, source: Optional[str] = None, **kwargs):
        offset = max(0, int(kwargs.get("offset") or 0))
        exclude_sources = set(kwargs.get("exclude_sources") or [])
        include_children = bool(kwargs.get("include_children", False))
        min_message_count = max(0, int(kwargs.get("min_message_count") or 0))
        rows = self._store.list_sessions(
            limit=limit + offset + len(exclude_sources) + 20,
            source=source,
        )
        result = []
        for raw in rows:
            if raw.get("source") in exclude_sources:
                continue
            if not include_children and raw.get("parent_session_id"):
                continue
            session = _session_doc_to_sessiondb_shape(raw) or {}
            session_id = session.get("session_id")
            if not session_id:
                continue
            messages = self._store.get_messages(session_id, include_inactive=True)
            session["message_count"] = len(messages)
            if session["message_count"] < min_message_count:
                continue
            first_user = next(
                (m.get("content") for m in messages if m.get("role") == "user"),
                None,
            )
            if first_user is not None:
                session["preview"] = str(first_user)[:60]
            result.append(session)
        return result[offset:offset + limit]

    def search_sessions(self, query: str, *, limit: int = 20, **kwargs):
        msgs = self._store.search_messages(query, limit=limit * 3)
        seen = set()
        out = []
        for m in msgs:
            sid = m.get("session_id")
            if sid and sid not in seen:
                seen.add(sid)
                sess = self.get_session(sid)
                if sess:
                    out.append(sess)
            if len(out) >= limit:
                break
        return out

    def session_count(self, source: str = None, sources: List[str] = None, **kwargs) -> int:
        sessions = self._store.list_sessions(limit=10_000, source=source)
        if sources:
            allowed = set(sources)
            sessions = [s for s in sessions if s.get("source") in allowed]
        if kwargs.get("archived_only"):
            sessions = [s for s in sessions if s.get("archived")]
        elif not kwargs.get("include_archived", False):
            sessions = [s for s in sessions if not s.get("archived")]
        return len(sessions)

    def session_count_by_source(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for sess in self._store.list_sessions(limit=10_000):
            src = str(sess.get("source") or "unknown")
            counts[src] = counts.get(src, 0) + 1
        return counts

    def _empty_sessions_pipeline(self) -> List[Dict[str, Any]]:
        """Build the Mongo aggregation shared by empty-session operations."""
        message_collection = getattr(self._store._messages, "name", "messages")
        return [
            {
                "$match": {
                    "ended_at": {"$exists": True, "$ne": None},
                    "archived": {"$ne": True},
                }
            },
            {
                "$lookup": {
                    "from": message_collection,
                    "let": {"session_id": "$session_id"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$eq": ["$session_id", "$$session_id"]
                                }
                            }
                        },
                        {"$limit": 1},
                    ],
                    "as": "_messages",
                }
            },
            {"$match": {"_messages": {"$eq": []}}},
        ]

    def _empty_session_ids(self) -> List[str]:
        """Return ended, non-archived sessions without persisted messages."""
        pipeline = self._empty_sessions_pipeline() + [
            {"$project": {"_id": 0, "session_id": 1}},
        ]
        return [
            str(row["session_id"])
            for row in self._store._sessions.aggregate(pipeline)
            if row.get("session_id")
        ]

    def count_empty_sessions(self) -> int:
        """Count ended, non-archived sessions with no persisted messages."""
        rows = self._store._sessions.aggregate(
            self._empty_sessions_pipeline() + [{"$count": "count"}]
        )
        return int(next(iter(rows), {}).get("count", 0))

    def delete_empty_sessions(self, sessions_dir: Optional[Path] = None) -> int:
        """Delete empty ended sessions while preserving children and archives."""
        session_ids = self._empty_session_ids()
        if not session_ids:
            return 0

        # Keep descendants, matching SessionDB's bulk-delete semantics.
        self._store._sessions.update_many(
            {"parent_session_id": {"$in": session_ids}},
            {"$set": {"parent_session_id": None}},
        )
        self._store._messages.delete_many({"session_id": {"$in": session_ids}})
        result = self._store._sessions.delete_many(
            {
                "session_id": {"$in": session_ids},
                "ended_at": {"$exists": True, "$ne": None},
                "archived": {"$ne": True},
            }
        )
        return int(result.deleted_count)

    def message_count(self, session_id: str = None) -> int:
        if session_id:
            return len(self._store.get_messages(session_id, include_inactive=True))
        total = 0
        for sess in self._store.list_sessions(limit=10_000):
            sid = sess.get("session_id")
            if sid:
                total += len(self._store.get_messages(sid, include_inactive=True))
        return total

    def append_message(self, session_id: str, role: str, content=None, **kwargs) -> int:
        return self._store.append_message(session_id, role, content, **kwargs)

    def get_messages(self, session_id: str, include_inactive: bool = False, **kwargs):
        messages = []
        for raw in self._store.get_messages(
            session_id, include_inactive=include_inactive
        ):
            message = _session_doc_to_sessiondb_shape(raw) or {}
            message["id"] = _mongo_message_id(
                session_id, message.get("message_index", 0)
            )
            self._message_state_cache[message["id"]] = {
                "session_id": session_id,
                "active": message.get("active", True),
                "compacted": message.get("compacted", False),
            }
            messages.append(message)
        return messages

    def get_messages_as_conversation(self, session_id: str, **kwargs):
        messages = self.get_messages(session_id)
        conversation = []
        for m in messages:
            item = {"role": m.get("role"), "content": m.get("content")}
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                item["tool_call_id"] = m["tool_call_id"]
            conversation.append(item)
        return conversation

    def get_messages_around(
        self, session_id: str, around_message_id: int, window: int = 10, **kwargs
    ) -> Dict[str, Any]:
        messages = self.get_messages(session_id, include_inactive=True)
        anchor = next(
            (i for i, message in enumerate(messages) if message.get("id") == around_message_id),
            None,
        )
        if anchor is None:
            return {"window": [], "messages_before": 0, "messages_after": 0}
        window = max(0, window)
        start = max(0, anchor - window)
        end = min(len(messages), anchor + window + 1)
        return {
            "window": messages[start:end],
            "messages_before": anchor - start,
            "messages_after": end - anchor - 1,
        }

    def get_anchored_view(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
        bookend: int = 3,
        keep_roles=("user", "assistant"),
    ) -> Dict[str, Any]:
        view = self.get_messages_around(session_id, around_message_id, window)
        messages = self.get_messages(session_id, include_inactive=True)
        window_rows = view["window"]
        if not window_rows:
            return {**view, "bookend_start": [], "bookend_end": []}

        keep = set(keep_roles) if keep_roles is not None else None
        filtered_window = [
            message for message in window_rows
            if keep is None
            or message.get("id") == around_message_id
            or message.get("role") in keep
        ]
        first_id, last_id = window_rows[0]["id"], window_rows[-1]["id"]
        eligible = lambda message: (
            (keep is None or message.get("role") in keep)
            and bool(message.get("content"))
        )
        before = [
            message for message in messages
            if message["id"] < first_id and eligible(message)
        ][:bookend]
        after = [
            message for message in messages
            if message["id"] > last_id and eligible(message)
        ][-bookend:]
        return {
            **view,
            "window": filtered_window,
            "bookend_start": before,
            "bookend_end": after,
        }

    def get_message_storage_state(self, message_id: int) -> Optional[Dict[str, Any]]:
        return self._message_state_cache.get(message_id)

    def search_messages(self, query: str, limit: int = 20, **kwargs):
        role_filter = set(kwargs.get("role_filter") or [])
        excluded_sources = set(kwargs.get("exclude_sources") or [])
        offset = max(0, int(kwargs.get("offset") or 0))
        candidates = self._store.search_messages(query, limit=(limit + offset) * 3)
        results = []
        session_cache: Dict[str, Dict[str, Any]] = {}
        for raw in candidates:
            if role_filter and raw.get("role") not in role_filter:
                continue
            session_id = raw.get("session_id")
            if not session_id:
                continue
            session = session_cache.setdefault(
                session_id, self._store.get_session(session_id) or {}
            )
            if session.get("source") in excluded_sources:
                continue
            message = _session_doc_to_sessiondb_shape(raw) or {}
            message_id = _mongo_message_id(session_id, message.get("message_index", 0))
            self._message_state_cache[message_id] = {
                "session_id": session_id,
                "active": message.get("active", True),
                "compacted": message.get("compacted", False),
            }
            results.append({
                **message,
                "id": message_id,
                "session_id": session_id,
                "session_started": session.get("started_at"),
                "source": session.get("source"),
                "model": session.get("model"),
                "snippet": str(message.get("content") or "")[:400],
            })
            if len(results) >= limit + offset:
                break
        return results[offset:offset + limit]

    def replace_messages(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        active_only: bool = False,
    ) -> None:
        existing = self.get_messages(session_id, include_inactive=True)
        if active_only:
            keep = [m for m in existing if m.get("active") is False]
        else:
            keep = []
        # Drop then reinsert via store primitives
        self._store._messages.delete_many({"session_id": session_id})
        for m in keep:
            doc = dict(m)
            doc.pop("_id", None)
            self._store._messages.insert_one(doc)
        for msg in messages:
            role = msg.get("role") or "assistant"
            content = msg.get("content")
            extra = {k: v for k, v in msg.items() if k not in ("role", "content")}
            self._store.append_message(session_id, role, content, **extra)

    def has_platform_message_id(self, session_id: str, platform_message_id: str) -> bool:
        if not platform_message_id:
            return False
        doc = self._store._messages.find_one({
            "session_id": session_id,
            "platform_message_id": str(platform_message_id),
        })
        return doc is not None

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        """Walk parent→child compression chain (best-effort on Mongo docs)."""
        current = session_id
        seen = {current} if current else set()
        for _ in range(100):
            children = [
                s for s in self._store.list_sessions(limit=500)
                if s.get("parent_session_id") == current
            ]
            parent = self.get_session(current) or {}
            if parent.get("end_reason") != "compression":
                # Prefer any child that continues compression
                cont = [
                    c for c in children
                    if c.get("end_reason") == "compression" or not c.get("ended_at")
                ]
                if not cont:
                    return current
                children = cont
            if not children:
                return current
            children.sort(key=lambda s: str(s.get("started_at") or ""), reverse=True)
            nxt = children[0].get("session_id")
            if not nxt or nxt in seen:
                return current
            seen.add(nxt)
            current = nxt
        return current

    def resolve_resume_session_id(self, session_id: str) -> str:
        tip = self.get_compression_tip(session_id)
        return tip or session_id

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        if not title:
            return None
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", title)
        cleaned = re.sub(r"[\u200b-\u200f\ufeff\u202a-\u202e\u2066-\u2069]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return None
        if len(cleaned) > MongoSessionAdapter.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {MongoSessionAdapter.MAX_TITLE_LENGTH})"
            )
        return cleaned

    def maybe_auto_archive(self, **kwargs) -> int:
        """Retention not yet ported to Mongo — no-op (0 archived)."""
        return 0

    def list_prune_candidates(self, **kwargs) -> List[Dict[str, Any]]:
        return []

    def get_next_title_in_lineage(self, base: str, **kwargs) -> str:
        existing = [
            (s.get("title") or "")
            for s in self._store.list_sessions(limit=500)
            if (s.get("title") or "").startswith(base)
        ]
        max_num = 1
        for t in existing:
            m = re.match(r"^.* #(\d+)$", t)
            if m:
                max_num = max(max_num, int(m.group(1)))
        return f"{base} #{max_num + 1}" if existing else base

    def get_resume_conversations(self, **kwargs) -> List[Dict[str, Any]]:
        return self.list_sessions_rich(limit=kwargs.get("limit", 50))

    def get_telegram_topic_binding(self, *args, **kwargs) -> Optional[Dict[str, Any]]:
        key = ":".join(str(a) for a in args) if args else str(kwargs)
        rows = self._storage.ledgers.find(
            "telegram_topic_bindings", {"key": key}, limit=1
        )
        return rows[0] if rows else None

    def claim_handoff(self, session_id: str) -> bool:
        existing = self._storage.ledgers.find(
            "session_handoffs", {"session_id": session_id}, limit=1
        )
        if existing and existing[0].get("claimed"):
            return False
        self._storage.ledgers.replace_one(
            "session_handoffs",
            {"session_id": session_id},
            {"session_id": session_id, "claimed": True},
        )
        return True

    def complete_handoff(self, session_id: str, **kwargs) -> None:
        self._storage.ledgers.replace_one(
            "session_handoffs",
            {"session_id": session_id},
            {"session_id": session_id, "claimed": False, "done": True, **kwargs},
        )

    def fail_handoff(self, session_id: str, **kwargs) -> None:
        self._storage.ledgers.replace_one(
            "session_handoffs",
            {"session_id": session_id},
            {"session_id": session_id, "claimed": False, "failed": True, **kwargs},
        )

    def get_handoff_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        rows = self._storage.ledgers.find(
            "session_handoffs", {"session_id": session_id}, limit=1
        )
        return rows[0] if rows else None

    def save_gateway_routing_entry(self, key: str, value: str, *, scope: str = "") -> None:
        self._store.save_gateway_routing_entry(key, value, scope=scope)

    def replace_gateway_routing_entries(self, entries: Dict[str, str], *, scope: str = "") -> None:
        for key, value in entries.items():
            self.save_gateway_routing_entry(key, value, scope=scope)

    def load_gateway_routing_entries(self, *, scope: str = "") -> Dict[str, str]:
        return self._store.load_gateway_routing_entries(scope=scope)

    def delete_gateway_routing_entries(self, keys=None, *, scope: str = "") -> None:
        if keys is None:
            for key in list(self.load_gateway_routing_entries(scope=scope)):
                self._storage.ledgers.delete(
                    "gateway_routing", {"scope": scope, "key": key}
                )
            return
        for key in keys:
            self._storage.ledgers.delete(
                "gateway_routing", {"scope": scope, "key": key}
            )

    def get_meta(self, key: str, default=None):
        rows = self._storage.ledgers.find("state_meta", {"key": key}, limit=1)
        if not rows:
            return default
        return rows[0].get("value", default)

    def set_meta(self, key: str, value) -> None:
        self._storage.ledgers.replace_one(
            "state_meta", {"key": key}, {"key": key, "value": value}
        )

    def update_token_counts(self, session_id: str, **kwargs) -> None:
        self._store.update_session(session_id, **{k: v for k, v in kwargs.items()})

    def queue_token_counts(self, session_id: str, **kwargs) -> None:
        self.update_token_counts(session_id, **kwargs)

    def flush_token_counts(self, timeout: float = 5.0) -> bool:
        return True

    def update_session_meta(self, session_id: str, **kwargs) -> None:
        self._store.update_session(session_id, **kwargs)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        self._store.update_session(session_id, system_prompt=system_prompt)

    def update_session_model(self, session_id: str, model: str) -> None:
        self._store.update_session(session_id, model=model)

    def update_session_cwd(self, session_id: str, cwd: str = None, **kwargs) -> None:
        fields = dict(kwargs)
        if cwd is not None:
            fields["cwd"] = cwd
        self._store.update_session(session_id, **fields)

    def set_session_title(self, session_id: str, title: str) -> bool:
        self._store.update_session(session_id, title=title)
        return True

    def get_session_title(self, session_id: str) -> Optional[str]:
        sess = self.get_session(session_id)
        return (sess or {}).get("title")

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        self._store.update_session(session_id, archived=archived)
        return True

    def set_session_pinned(self, session_id: str, pinned: bool) -> bool:
        self._store.update_session(session_id, pinned=pinned)
        return True

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return session_id_or_prefix
        for sess in self._store.list_sessions(limit=200):
            sid = sess.get("session_id") or ""
            if sid.startswith(session_id_or_prefix):
                return sid
        return None

    def try_acquire_compression_lock(self, session_id: str, holder: str, **kwargs) -> bool:
        existing = self._storage.ledgers.find(
            "compression_locks", {"session_id": session_id}, limit=1
        )
        if existing and existing[0].get("holder") not in (None, holder):
            return False
        self._storage.ledgers.replace_one(
            "compression_locks",
            {"session_id": session_id},
            {"session_id": session_id, "holder": holder},
        )
        return True

    def release_compression_lock(self, session_id: str, holder: str) -> None:
        self._storage.ledgers.delete(
            "compression_locks", {"session_id": session_id, "holder": holder}
        )

    def refresh_compression_lock(self, session_id: str, holder: str, **kwargs) -> bool:
        return self.try_acquire_compression_lock(session_id, holder)

    def get_compression_lock_holder(self, session_id: str) -> Optional[str]:
        rows = self._storage.ledgers.find(
            "compression_locks", {"session_id": session_id}, limit=1
        )
        return rows[0].get("holder") if rows else None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        raise AttributeError(
            f"MongoSessionAdapter has no attribute {name!r}. "
            "This SessionDB method is not ported to Mongo yet — refusing silent "
            "no-op (would hide fleet bugs). Use classic mode or implement the method."
        )


def open_session_db(db_path: Path = None, read_only: bool = False):
    """Factory: Mongo adapter when in mongo mode, else classic SessionDB.

    In Mongo mode this never opens local state.db (fail-hard on bridge errors).
    """
    from hermes_storage import is_mongo_mode

    if is_mongo_mode() and db_path is None:
        return MongoSessionAdapter(read_only=read_only)
    from hermes_state import SessionDB

    return SessionDB(db_path=db_path, read_only=read_only)
