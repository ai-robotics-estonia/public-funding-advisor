"""Tests for saving a vestlus, listing your own, and sharing one by its code.

The rule under test: saving is what makes a vestlus listable by its owner and
readable by anyone holding the code. Unsaved work stays private, and a shared
code never grants more than reading.
"""

import pytest
from fastapi.testclient import TestClient

from backend import auth, db
from backend.main import app


def _client():
    """A client with its own cookie jar, speaking https (the session cookie is Secure)."""
    return TestClient(app, base_url="https://testserver")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    yield


def _tester(name):
    """Register a tester and return a logged-in client for them."""
    code = auth.generate_code()
    db.add_tester(name, auth.hash_code(code))
    c = _client()
    assert c.post("/login", json={"code": code}).status_code == 200
    return c


def _new_vestlus(client):
    r = client.post("/vestlused")
    assert r.status_code == 200, r.text
    return r.json()["vestluse_id"]


# --- saving ---------------------------------------------------------------


def test_new_vestlus_starts_unsaved():
    mari = _tester("Mari")
    vid = _new_vestlus(mari)
    assert mari.get(f"/vestlused/{vid}").json()["saved"] is False


def test_save_names_the_vestlus_and_marks_it_saved():
    mari = _tester("Mari")
    vid = _new_vestlus(mari)

    r = mari.post(f"/vestlused/{vid}/save", json={"title": "Tehisaru projekt"})
    assert r.status_code == 200, r.text

    body = mari.get(f"/vestlused/{vid}").json()
    assert body["saved"] is True
    assert body["title"] == "Tehisaru projekt"


def test_save_rejects_an_empty_title():
    mari = _tester("Mari")
    vid = _new_vestlus(mari)
    assert mari.post(f"/vestlused/{vid}/save", json={"title": "   "}).status_code == 400


def test_save_truncates_an_overlong_title():
    mari = _tester("Mari")
    vid = _new_vestlus(mari)
    r = mari.post(f"/vestlused/{vid}/save", json={"title": "x" * 500})
    assert len(r.json()["title"]) == 120


def test_another_tester_cannot_save_your_vestlus():
    mari = _tester("Mari")
    juri = _tester("Jüri")
    vid = _new_vestlus(mari)
    mari.post(f"/vestlused/{vid}/save", json={"title": "Minu oma"})

    r = juri.post(f"/vestlused/{vid}/save", json={"title": "Kaaperdatud"})
    assert r.status_code == 403


# --- listing --------------------------------------------------------------


def test_list_returns_only_saved_vestlused():
    mari = _tester("Mari")
    saved = _new_vestlus(mari)
    _new_vestlus(mari)  # left unsaved
    mari.post(f"/vestlused/{saved}/save", json={"title": "Alles"})

    items = mari.get("/vestlused").json()["vestlused"]
    assert [v["vestluse_id"] for v in items] == [saved]
    assert items[0]["title"] == "Alles"


def test_list_never_shows_another_testers_vestlused():
    mari = _tester("Mari")
    juri = _tester("Jüri")
    vid = _new_vestlus(mari)
    mari.post(f"/vestlused/{vid}/save", json={"title": "Mari oma"})

    assert juri.get("/vestlused").json()["vestlused"] == []


# --- sharing by code ------------------------------------------------------


def test_shared_code_opens_a_saved_vestlus_for_another_tester():
    mari = _tester("Mari")
    juri = _tester("Jüri")
    vid = _new_vestlus(mari)
    mari.post(f"/vestlused/{vid}/save", json={"title": "Jagatud"})

    r = juri.get(f"/vestlused/{vid}")
    assert r.status_code == 200
    assert r.json()["title"] == "Jagatud"
    # The reader is told it is not theirs, so the UI can hide the write actions.
    assert r.json()["owned"] is False


def test_owner_sees_their_own_vestlus_as_owned():
    mari = _tester("Mari")
    vid = _new_vestlus(mari)
    assert mari.get(f"/vestlused/{vid}").json()["owned"] is True


def test_unsaved_vestlus_stays_private_even_with_the_code():
    mari = _tester("Mari")
    juri = _tester("Jüri")
    vid = _new_vestlus(mari)

    assert juri.get(f"/vestlused/{vid}").status_code == 403


def test_sharing_grants_reading_but_not_deleting():
    mari = _tester("Mari")
    juri = _tester("Jüri")
    vid = _new_vestlus(mari)
    mari.post(f"/vestlused/{vid}/save", json={"title": "Jagatud"})

    assert juri.get(f"/vestlused/{vid}").status_code == 200
    assert juri.delete(f"/vestlused/{vid}").status_code == 403
    assert mari.get(f"/vestlused/{vid}").status_code == 200  # still there


def test_sharing_grants_reading_but_not_uploading():
    mari = _tester("Mari")
    juri = _tester("Jüri")
    vid = _new_vestlus(mari)
    mari.post(f"/vestlused/{vid}/save", json={"title": "Jagatud"})

    r = juri.post(f"/vestlused/{vid}/files", files={"file": ("a.txt", b"tekst", "text/plain")})
    assert r.status_code == 403


def test_sharing_grants_reading_but_not_recommending():
    mari = _tester("Mari")
    juri = _tester("Jüri")
    vid = _new_vestlus(mari)
    mari.post(f"/vestlused/{vid}/save", json={"title": "Jagatud"})

    r = juri.post("/recommend", json={"vestluse_id": vid, "intake": {}})
    assert r.status_code == 403


def test_sharing_grants_reading_but_not_refining():
    """The second pass fans out to several LLM calls — it is not a reader's to spend."""
    mari = _tester("Mari")
    juri = _tester("Jüri")
    vid = _new_vestlus(mari)
    mari.post(f"/vestlused/{vid}/save", json={"title": "Jagatud"})
    db.save_recommendation(
        vid,
        ["Innovatsiooniosak"],
        [],
        [{"name": "Innovatsiooniosak", "measure_id": "02", "score": 5,
          "explanation": "Sobib.", "checks": [], "fit": {}}],
    )

    assert juri.get(f"/vestlused/{vid}").status_code == 200
    r = juri.post(f"/vestlused/{vid}/refine", json={"measure_names": ["Innovatsiooniosak"]})
    assert r.status_code == 403


def test_missing_vestlus_is_404_not_403():
    mari = _tester("Mari")
    assert mari.get("/vestlused/PUUDUB99").status_code == 404


# --- retention ------------------------------------------------------------


def test_purge_removes_unsaved_vestlused_and_keeps_saved_ones():
    mari = _tester("Mari")
    kept = _new_vestlus(mari)
    dropped = _new_vestlus(mari)
    mari.post(f"/vestlused/{kept}/save", json={"title": "Alles"})

    # Every vestlus is younger than the cutoff, so nothing goes yet.
    assert db.delete_unsaved_vestlused("2000-01-01T00:00:00+00:00") == []

    future = "2999-01-01T00:00:00+00:00"
    assert db.count_unsaved_vestlused(future) == 1
    assert db.delete_unsaved_vestlused(future) == [dropped]

    assert db.get_vestlus(kept) is not None
    assert db.get_vestlus(dropped) is None
