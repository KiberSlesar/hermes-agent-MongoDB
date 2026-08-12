"""Tests for fleet Mongo wiki (slugify, store, outbox kinds)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hermes_storage.wiki import MongoWikiStore, slugify


def test_slugify_basic():
    assert slugify("Nginx addresses") == "nginx-addresses"
    assert slugify("  API / Hosts  ") == "api-hosts"


def test_wiki_put_and_get_roundtrip():
    store = MongoWikiStore.__new__(MongoWikiStore)
    docs: dict = {}

    class _Col:
        def find_one(self, q, *a, **k):
            return docs.get(q.get("slug"))

        def update_one(self, q, update, upsert=False):
            slug = q["slug"]
            existing = docs.get(slug) or {"slug": slug, "revision": 0}
            set_doc = dict(update.get("$set") or {})
            existing.update(set_doc)
            existing["revision"] = int(existing.get("revision") or 0) + int(
                (update.get("$inc") or {}).get("revision") or 0
            )
            docs[slug] = existing

        def find(self, query=None, projection=None):
            class _C:
                def sort(self, *a, **k):
                    return self

                def limit(self, n):
                    return list(docs.values())[:n]

                def __iter__(self):
                    return iter(list(docs.values()))

            return _C()

        def delete_one(self, q):
            slug = q.get("slug")
            existed = slug in docs
            docs.pop(slug, None)
            return MagicMock(deleted_count=1 if existed else 0)

    store._col = _Col()
    page = store.put_page(
        title="Nginx addresses",
        body="## edge\n- 10.0.0.1\n",
        tags=["nginx", "network"],
        updated_by="pc1",
    )
    assert page["slug"] == "nginx-addresses"
    assert page["content_hash"].startswith("sha256:")
    assert page["revision"] == 1
    got = store.get_page("nginx-addresses")
    assert "10.0.0.1" in got["body"]
    assert store.delete_page("nginx-addresses") is True


def test_storage_put_wiki_queues_on_transient(monkeypatch, tmp_path):
    from hermes_storage.errors import MongoStorageError
    from hermes_storage.factory import HermesStorage
    from hermes_storage.outbox import clear_secret_overlay_keys, pending_count

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clear_secret_overlay_keys()

    wiki = MagicMock()
    wiki.put_page.side_effect = MongoStorageError("MongoDB unavailable: timeout")

    storage = HermesStorage(
        bootstrap=type("B", (), {"profile": "default"})(),
        client=None,
        shared_db=None,
        profile_db=None,
        settings=None,
        knowledge=None,
        config=None,
        secrets=MagicMock(),
        soul=None,
        memories=None,
        skills=None,
        machines=MagicMock(),
        sessions=None,
        ledgers=None,
        cluster=None,
        machine_id="pc1",
        node_id="pc1",
        wiki=wiki,
    )

    page = storage.put_wiki_page(
        title="Nginx addresses",
        body="- 10.0.0.1",
        tags=["nginx"],
    )
    assert page.get("queued") is True
    assert page["slug"] == "nginx-addresses"
    assert pending_count() >= 1
