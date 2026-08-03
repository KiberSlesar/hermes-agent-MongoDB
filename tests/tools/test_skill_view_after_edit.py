"""Regression: skill_view must see skill_manage edits in the same session.

Covers the stale-cache class from the Mongo fork (GridFS SKILL.md overwriting
a just-committed skill_md, plus discovery-cache invalidation after manage).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools.skill_manager_tool as sm
import tools.skills_tool as st


@pytest.fixture(autouse=True)
def _isolate_skills(monkeypatch, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    st._SKILLS_CACHE.clear()
    monkeypatch.setattr(st, "SKILLS_DIR", skills)
    monkeypatch.setattr(st, "_SKILLS_DIR_AT_IMPORT", skills)
    monkeypatch.setattr(sm, "SKILLS_DIR", skills)
    monkeypatch.setattr(sm, "_SKILLS_DIR_AT_IMPORT", skills)
    monkeypatch.setattr(st, "_skills_dir", lambda: skills)
    monkeypatch.setattr(sm, "_skills_dir", lambda: skills)
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs", lambda: []
    )
    monkeypatch.setattr(
        "agent.skill_utils.get_all_skills_dirs", lambda: [skills]
    )
    monkeypatch.setattr(st, "_get_disabled_skill_names", lambda: set())
    # Classic mode: no Mongo commit path.
    monkeypatch.setattr(
        sm,
        "_commit_skill_dir",
        lambda *a, **k: None,
    )
    yield
    st._SKILLS_CACHE.clear()


def _skill_md(name: str, body: str, description: str = "test skill") -> str:
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    )


def test_edit_then_skill_view_sees_new_content_and_matching_hash(tmp_path):
    name = "cache-bust-skill"
    old = _skill_md(name, "# old body")
    new = _skill_md(name, "# new body after edit")

    create = json.loads(
        sm.skill_manage(action="create", name=name, content=old)
    )
    assert create["success"] is True
    assert create.get("content_hash")

    before = json.loads(st.skill_view(name, preprocess=False))
    assert before["success"] is True
    assert "# old body" in before["content"]
    assert before["content_hash"] == create["content_hash"]

    edited = json.loads(
        sm.skill_manage(action="edit", name=name, content=new)
    )
    assert edited["success"] is True
    assert edited["content_hash"] != before["content_hash"]

    after = json.loads(st.skill_view(name, preprocess=False))
    assert after["success"] is True
    assert "# new body after edit" in after["content"]
    assert "# old body" not in after["content"]
    assert after["content_hash"] == edited["content_hash"]


def test_patch_clears_discovery_cache_descriptions(tmp_path):
    name = "desc-cache-skill"
    create = json.loads(
        sm.skill_manage(
            action="create",
            name=name,
            content=_skill_md(name, "# v1", description="old description"),
        )
    )
    assert create["success"] is True

    listed = json.loads(st.skills_list())
    assert any(
        s["name"] == name and s["description"] == "old description"
        for s in listed["skills"]
    )

    patched = json.loads(
        sm.skill_manage(
            action="patch",
            name=name,
            old_string="description: old description",
            new_string="description: fresh description",
        )
    )
    assert patched["success"] is True
    assert st._SKILLS_CACHE == {}

    listed2 = json.loads(st.skills_list())
    match = next(s for s in listed2["skills"] if s["name"] == name)
    assert match["description"] == "fresh description"


def test_materialize_prefers_document_skill_md_over_stale_gridfs(tmp_path):
    """Stale GridFS SKILL.md must not clobber the Mongo document body."""
    from hermes_storage.mongo.stores import MongoSkillsStore

    store = MongoSkillsStore.__new__(MongoSkillsStore)
    name = "stale-grid"
    dest = tmp_path / "cache"
    dest.mkdir()

    stale = b"---\nname: stale-grid\n---\n\n# STALE FROM GRIDFS\n"
    fresh = "---\nname: stale-grid\n---\n\n# FRESH FROM DOCUMENT\n"

    grid_file = SimpleNamespace(
        filename=f"{name}/SKILL.md",
        read=lambda: stale,
    )
    support = SimpleNamespace(
        filename=f"{name}/references/note.md",
        read=lambda: b"ok",
    )
    store._fs = MagicMock()
    store._fs.find.return_value = [grid_file, support]
    store.get_skill = MagicMock(
        return_value={"name": name, "skill_md": fresh}
    )

    out = store.materialize(name, dest)
    assert (out / "SKILL.md").read_text(encoding="utf-8") == fresh
    assert (out / "references" / "note.md").read_bytes() == b"ok"
    assert "STALE FROM GRIDFS" not in (out / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_ensure_skill_fresh_rematerializes_when_mongo_newer(tmp_path, monkeypatch):
    """skill_view path: remote content_hash beats stale local cache stamps."""
    from hermes_storage import skills_sync as ss

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cache = tmp_path / "cache" / "skills"
    skill = cache / "remote-newer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# old local\n", encoding="utf-8")
    (skill / ".mongo_updated_at").write_text(
        "2020-01-01T00:00:00+00:00", encoding="utf-8"
    )
    (skill / ".mongo_content_hash").write_text("sha256:old", encoding="utf-8")

    body = "---\nname: remote-newer\n---\n\n# NEW FROM MONGO\n"
    remote = {
        "name": "remote-newer",
        "skill_md": body,
        "content_hash": "sha256:new",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "status": "ready",
        "revision": 3,
    }

    storage = MagicMock()
    storage.skills.get_skill.return_value = remote

    def _materialize(name, dest_dir):
        root = Path(dest_dir) / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(body, encoding="utf-8")
        return root

    storage.skills.materialize.side_effect = _materialize
    monkeypatch.setattr("hermes_storage.is_mongo_mode", lambda: True)
    monkeypatch.setattr("hermes_storage.require_storage", lambda: storage)
    monkeypatch.setattr(ss, "mongo_skills_cache_dir", lambda: cache)

    assert ss.ensure_skill_fresh_from_mongo("remote-newer") is True
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == body
    assert (skill / ".mongo_content_hash").read_text(encoding="utf-8") == "sha256:new"
    assert ss.ensure_skill_fresh_from_mongo("remote-newer") is False
