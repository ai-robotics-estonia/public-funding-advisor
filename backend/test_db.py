"""Tests for SQLite vestlus schema and basic CRUD."""

import json
import os
from pathlib import Path

import pytest

from backend import db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point db module at a temp directory for every test."""
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    yield


def test_create_and_get_vestlus():
    vid = db.create_vestlus()
    assert len(vid) == db._ID_LEN
    assert vid.isalnum()
    assert vid.isupper() or any(c.isdigit() for c in vid)

    row = db.get_vestlus(vid)
    assert row is not None
    assert row["vestluse_id"] == vid
    assert row["intake"] == {}
    assert row["clarifications"] is None


def test_update_intake_and_previously_applied():
    vid = db.create_vestlus()
    intake = {"company_size": "VKE", "previously_applied": ["Innovatsiooniosak"]}
    assert db.update_vestlus_intake(vid, intake=intake) is True
    row = db.get_vestlus(vid)
    assert row["intake"]["previously_applied"] == ["Innovatsiooniosak"]


def test_recommendation_immutable():
    vid = db.create_vestlus()
    db.save_recommendation(vid, ["A"], [], [{"name": "A", "score": 1}])
    assert db.has_recommendation(vid) is True
    rec = db.get_recommendation(vid)
    assert rec["candidates"] == ["A"]
    assert rec["ranked_cards"][0]["name"] == "A"

    with pytest.raises(ValueError, match="already exists"):
        db.save_recommendation(vid, ["B"], [], [])


def test_delete_vestlus_removes_upload_dir():
    vid = db.create_vestlus()
    upload_dir = db.UPLOADS_DIR / vid
    upload_dir.mkdir(parents=True)
    (upload_dir / "note.txt").write_text("hello", encoding="utf-8")

    assert db.delete_vestlus(vid) is True
    assert db.get_vestlus(vid) is None
    assert not upload_dir.exists()


def test_tables_exist():
    with db.connect() as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "vestlused",
        "user_files",
        "recommendations",
        "measure_threads",
        "messages",
    } <= names
