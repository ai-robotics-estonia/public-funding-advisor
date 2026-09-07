"""Tests for measure-thread creation and chat message endpoints."""

import pytest
from fastapi.testclient import TestClient

import backend.chat as chat_module
from backend import db
from backend.main import app

client = TestClient(app)

FOCUS_ID = "02"  # Innovatsiooniosak, per rahastusmeetmed/02 - Innovatsiooniosak.csv
OTHER_ID = "03"  # Arendusosak


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    yield


def _fake_cards():
    return [
        {
            "name": "Innovatsiooniosak",
            "measure_id": FOCUS_ID,
            "score": 5,
            "explanation": "Sobib hästi teie projektiga.",
            "checks": ["Kontrolli eelarvet", "Kontrolli tähtaega"],
        },
        {
            "name": "Arendusosak",
            "measure_id": OTHER_ID,
            "score": 3,
            "explanation": "Vähem sobiv.",
            "checks": [],
        },
    ]


def _new_vestlus_with_recommendation():
    vid = client.post("/vestlused").json()["vestluse_id"]
    db.save_recommendation(vid, ["Innovatsiooniosak", "Arendusosak"], [], _fake_cards())
    return vid


def test_create_thread_404_unknown_vestlus():
    r = client.post("/vestlused/ZZZZZ/threads", json={"measure_id": FOCUS_ID})
    assert r.status_code == 404


def test_create_thread_409_without_recommendation():
    vid = client.post("/vestlused").json()["vestluse_id"]
    r = client.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID})
    assert r.status_code == 409


def test_create_thread_404_unknown_measure_id():
    vid = _new_vestlus_with_recommendation()
    r = client.post(f"/vestlused/{vid}/threads", json={"measure_id": "does-not-exist"})
    assert r.status_code == 404


def test_create_thread_seeds_first_message():
    vid = _new_vestlus_with_recommendation()
    r = client.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    thread_id = body["thread_id"]

    messages = client.get(f"/threads/{thread_id}/messages").json()["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert "Sobib hästi" in messages[0]["content"]
    assert "Kontrolli eelarvet" in messages[0]["content"]


def test_create_thread_idempotent_same_measure():
    vid = _new_vestlus_with_recommendation()
    r1 = client.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID})
    r2 = client.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID})
    assert r1.json()["thread_id"] == r2.json()["thread_id"]
    assert r2.json()["created"] is False
    # Only one seed message, not one per create call.
    messages = client.get(f"/threads/{r1.json()['thread_id']}/messages").json()["messages"]
    assert len(messages) == 1


def test_get_messages_404_unknown_thread():
    r = client.get("/threads/doesnotexist/messages")
    assert r.status_code == 404


def test_post_message_saves_and_returns_history(monkeypatch):
    monkeypatch.setattr(chat_module, "send_message", lambda *a, **kw: "Vastus kasutajale.")

    vid = _new_vestlus_with_recommendation()
    thread_id = client.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID}).json()["thread_id"]

    r = client.post(f"/threads/{thread_id}/messages", json={"content": "Kas see sobib meile?"})
    assert r.status_code == 200
    messages = r.json()["messages"]
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant"]
    assert messages[1]["content"] == "Kas see sobib meile?"
    assert messages[2]["content"] == "Vastus kasutajale."


def test_post_message_rejects_empty_content():
    vid = _new_vestlus_with_recommendation()
    thread_id = client.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID}).json()["thread_id"]
    r = client.post(f"/threads/{thread_id}/messages", json={"content": "   "})
    assert r.status_code == 400


def test_post_message_404_unknown_thread():
    r = client.post("/threads/doesnotexist/messages", json={"content": "hi"})
    assert r.status_code == 404


def test_post_message_502_on_claude_failure_keeps_user_message(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("timeout")

    monkeypatch.setattr(chat_module, "send_message", boom)

    vid = _new_vestlus_with_recommendation()
    thread_id = client.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID}).json()["thread_id"]

    r = client.post(f"/threads/{thread_id}/messages", json={"content": "Palun selgita rohkem."})
    assert r.status_code == 502

    # Seed message + the user's message are kept; no assistant reply was written.
    stored = db.list_messages(thread_id)
    assert [m["role"] for m in stored] == ["assistant", "user"]
    assert stored[-1]["content"] == "Palun selgita rohkem."


def test_clear_history_then_reseed_on_next_message(monkeypatch):
    captured_history = {}

    def fake_send(vestluse_id, measure, recommendation, history, user_content, refine_card=None):
        captured_history["history"] = list(history)
        return "Vastus."

    monkeypatch.setattr(chat_module, "send_message", fake_send)

    vid = _new_vestlus_with_recommendation()
    thread_id = client.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID}).json()["thread_id"]

    clear = client.delete(f"/threads/{thread_id}/messages")
    assert clear.status_code == 200
    assert client.get(f"/threads/{thread_id}/messages").json()["messages"] == []

    r = client.post(f"/threads/{thread_id}/messages", json={"content": "Uus küsimus."})
    assert r.status_code == 200

    # The seed was re-added before this message, so send_message saw it as history.
    assert len(captured_history["history"]) == 1
    assert captured_history["history"][0]["role"] == "assistant"
    assert "Sobib hästi" in captured_history["history"][0]["content"]


def test_clear_history_on_unknown_thread_404():
    r = client.delete("/threads/doesnotexist/messages")
    assert r.status_code == 404
