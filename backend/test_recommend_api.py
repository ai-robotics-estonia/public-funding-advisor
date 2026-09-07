"""Tests for POST /recommend: vestluse_id scoping and the immutable snapshot lock."""

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend import db, files
from backend.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    yield


def _intake(**overrides):
    intake = {
        "applicant_type": "ettevote",
        "company_size": "VKE",
        "region": "",
        "project_description": "Test projekt",
    }
    intake.update(overrides)
    return intake


def test_recommend_requires_vestluse_id():
    r = client.post("/recommend", json={"intake": _intake()})
    assert r.status_code == 422


def test_recommend_404_for_unknown_vestlus():
    r = client.post("/recommend", json={"vestluse_id": "ZZZZZ", "intake": _intake()})
    assert r.status_code == 404


def test_first_call_returns_questions_and_persists_nothing():
    vid = client.post("/vestlused").json()["vestluse_id"]
    r = client.post("/recommend", json={"vestluse_id": vid, "intake": _intake()})
    assert r.status_code == 200
    body = r.json()
    assert body["ranked_cards"] == []
    assert isinstance(body["candidates"], list)
    assert db.has_recommendation(vid) is False

    # Intake is saved even on the first call, so GET reflects it while browsing.
    saved = db.get_vestlus(vid)
    assert saved["intake"]["project_description"] == "Test projekt"


def test_second_call_locks_snapshot_and_third_call_is_rejected(monkeypatch):
    fake_cards = [{"name": "Innovatsiooniosak", "score": 5, "explanation": "", "checks": []}]
    monkeypatch.setattr(main_module, "rank", lambda *a, **kw: fake_cards)

    vid = client.post("/vestlused").json()["vestluse_id"]
    r1 = client.post("/recommend", json={"vestluse_id": vid, "intake": _intake(), "clarifications": {}})
    assert r1.status_code == 200
    assert r1.json()["ranked_cards"] == fake_cards
    assert db.has_recommendation(vid) is True

    r2 = client.post("/recommend", json={"vestluse_id": vid, "intake": _intake(), "clarifications": {}})
    assert r2.status_code == 409

    # Even a "first call" shape is rejected once locked.
    r3 = client.post("/recommend", json={"vestluse_id": vid, "intake": _intake()})
    assert r3.status_code == 409


def test_recommend_passes_uploaded_file_context_to_rank(monkeypatch):
    captured = {}

    def fake_rank(candidates, intake, clarifications, files_context=""):
        captured["files_context"] = files_context
        return []

    monkeypatch.setattr(main_module, "rank", fake_rank)

    vid = client.post("/vestlused").json()["vestluse_id"]
    files.save_upload(vid, "note.txt", b"unikaalne ettevotte kontekst")

    r = client.post("/recommend", json={"vestluse_id": vid, "intake": _intake(), "clarifications": {}})
    assert r.status_code == 200
    assert "unikaalne ettevotte kontekst" in captured["files_context"]
