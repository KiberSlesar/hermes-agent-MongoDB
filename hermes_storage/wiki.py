"""Fleet wiki — technical reference pages in Mongo (not skills, not MEMORY).

Skills teach *how* to do work. MEMORY/USER hold curated personal notes.
Wiki holds durable reference facts: hostnames, nginx addresses, runbooks,
service URLs — things the agent should look up, not embed in a skill body.

Storage: ``hermes_shared.wiki_pages`` (shared across the fleet profile's
Mongo shared DB). Writes go through the outbox when Mongo is down.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from hermes_storage.backend import utcnow

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    raw = (title or "").strip().lower().replace("ё", "е")
    slug = _SLUG_RE.sub("-", raw).strip("-")
    return slug[:120] or "page"


class MongoWikiStore:
    """Page store: one document per slug in ``wiki_pages``."""

    def __init__(self, collection):
        self._col = collection

    def list_pages(
        self,
        *,
        tag: Optional[str] = None,
        limit: int = 200,
        include_body: bool = False,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if tag:
            query["tags"] = str(tag).strip().lower()
        projection = None if include_body else {"body": 0}
        cursor = (
            self._col.find(query, projection)
            .sort("updated_at", -1)
            .limit(max(1, int(limit)))
        )
        return [_strip(d) for d in cursor]

    def get_page(self, slug: str) -> Optional[dict[str, Any]]:
        return _strip(self._col.find_one({"slug": slugify(slug)}))

    def put_page(
        self,
        *,
        title: str,
        body: str,
        slug: Optional[str] = None,
        tags: Optional[list[str]] = None,
        updated_by: str = "",
        status: str = "ready",
    ) -> dict[str, Any]:
        import hashlib

        page_slug = slugify(slug or title)
        tag_list = sorted(
            {
                str(t).strip().lower()
                for t in (tags or [])
                if str(t).strip()
            }
        )
        text = str(body or "")
        content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        now = utcnow()
        doc = {
            "slug": page_slug,
            "title": str(title or page_slug).strip() or page_slug,
            "body": text,
            "tags": tag_list,
            "status": status or "ready",
            "content_hash": content_hash,
            "updated_by": updated_by or "",
            "updated_at": now,
        }
        existing = self._col.find_one({"slug": page_slug})
        if existing and not existing.get("created_at"):
            doc["created_at"] = existing.get("updated_at") or now
        elif not existing:
            doc["created_at"] = now
        else:
            doc["created_at"] = existing.get("created_at") or now

        self._col.update_one(
            {"slug": page_slug},
            {"$set": doc, "$inc": {"revision": 1}},
            upsert=True,
        )
        return self.get_page(page_slug) or doc

    def delete_page(self, slug: str) -> bool:
        result = self._col.delete_one({"slug": slugify(slug)})
        return bool(result.deleted_count)

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        try:
            cursor = (
                self._col.find(
                    {"$text": {"$search": q}, "status": {"$ne": "archived"}},
                    {"score": {"$meta": "textScore"}, "body": 0},
                )
                .sort([("score", {"$meta": "textScore"})])
                .limit(max(1, int(limit)))
            )
            return [_strip(d) for d in cursor]
        except Exception:
            pattern = re.escape(q)
            cursor = (
                self._col.find(
                    {
                        "$or": [
                            {"title": {"$regex": pattern, "$options": "i"}},
                            {"body": {"$regex": pattern, "$options": "i"}},
                            {"tags": {"$regex": pattern, "$options": "i"}},
                            {"slug": {"$regex": pattern, "$options": "i"}},
                        ],
                        "status": {"$ne": "archived"},
                    },
                    {"body": 0},
                )
                .limit(max(1, int(limit)))
            )
            return [_strip(d) for d in cursor]


def _strip(doc: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out
