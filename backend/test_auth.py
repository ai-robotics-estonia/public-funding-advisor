"""Tests for the per-tester access gate and resource ownership.

The gate is env-driven (AUTH_ENABLED), so each test states which mode it is in.
The default-off tests matter as much as the default-on ones: local development
and every other test module rely on the app staying wide open when unset.
"""

import time

import pytest
from fastapi.testclient import TestClient

from backend import auth, db
from backend.main import app

client = TestClient(app)

FOCUS_ID = "02"  # Innovatsiooniosak


def _client():
    """A client with its own cookie jar, speaking https.

    The session cookie is issued Secure — the real deployment is HTTPS-only
    behind Tailscale — and httpx will not send a Secure cookie back over plain
    http, so the default TestClient base_url of http://testserver would silently
    drop it and every authenticated request would look anonymous.
    """
    return TestClient(app, base_url="https://testserver")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    yield


@pytest.fixture
def gate_on(monkeypatch):
    """Turn the gate on with a fixed secret so cookies are stable across calls."""
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    yield


def _add_tester(name="Mari"):
    code = auth.generate_code()
    tester_id = db.add_tester(name, auth.hash_code(code))
    return tester_id, code


def _login(code):
    """Log in on a fresh client so each tester has its own cookie jar."""
    c = _client()
    r = c.post("/login", json={"code": code})
    assert r.status_code == 200, r.text
    return c


# --- cookie mechanics ------------------------------------------------------

def test_cookie_round_trip(gate_on):
    value = auth.make_cookie_value("abc123")
    assert auth.read_cookie_value(value) == "abc123"


def test_tampered_cookie_is_rejected(gate_on):
    value = auth.make_cookie_value("abc123")
    tester_id, expires, sig = value.split(".")
    forged = f"someone-else.{expires}.{sig}"
    assert auth.read_cookie_value(forged) is None


def test_expired_cookie_is_rejected(gate_on):
    # Issued far enough in the past that its embedded expiry has already passed.
    value = auth.make_cookie_value("abc123", now=time.time() - auth.SESSION_TTL_SECONDS - 10)
    assert auth.read_cookie_value(value) is None


def test_malformed_cookie_is_rejected(gate_on):
    assert auth.read_cookie_value("") is None
    assert auth.read_cookie_value("not-a-cookie") is None
    assert auth.read_cookie_value("a.b") is None
    assert auth.read_cookie_value("a.notanumber.c") is None


def test_code_hash_depends_on_secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "secret-one")
    first = auth.hash_code("ABCDEFGHJK")
    monkeypatch.setenv("SESSION_SECRET", "secret-two")
    assert auth.hash_code("ABCDEFGHJK") != first


def test_code_lookup_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    assert auth.hash_code("  abcdefghjk ") == auth.hash_code("ABCDEFGHJK")


# --- gate off (the default) ------------------------------------------------

def test_endpoints_are_open_when_gate_is_off(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    r = client.post("/vestlused")
    assert r.status_code == 200
    assert client.get(f"/vestlused/{r.json()['vestluse_id']}").status_code == 200


def test_me_reports_disabled_when_gate_is_off(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    assert client.get("/me").json() == {"authenticated": True, "auth": "disabled"}


# --- gate on ---------------------------------------------------------------

def test_anonymous_request_is_401(gate_on):
    assert _client().post("/vestlused").status_code == 401


def test_healthz_stays_open(gate_on):
    assert _client().get("/healthz").status_code == 200


def test_login_with_unknown_code_is_401(gate_on):
    _add_tester()
    assert _client().post("/login", json={"code": "WRONGCODE1"}).status_code == 401


def test_login_then_use_session(gate_on):
    _, code = _add_tester()
    c = _login(code)
    assert c.get("/me").json()["authenticated"] is True
    assert c.post("/vestlused").status_code == 200


def test_disabled_tester_cannot_log_in(gate_on):
    tester_id, code = _add_tester()
    db.set_tester_active(tester_id, False)
    assert _client().post("/login", json={"code": code}).status_code == 401


def test_disabling_a_tester_invalidates_the_live_session(gate_on):
    tester_id, code = _add_tester()
    c = _login(code)
    assert c.post("/vestlused").status_code == 200
    db.set_tester_active(tester_id, False)
    assert c.post("/vestlused").status_code == 401


def test_logout_clears_the_session(gate_on):
    _, code = _add_tester()
    c = _login(code)
    assert c.post("/logout").status_code == 200
    assert c.post("/vestlused").status_code == 401


def test_login_records_last_seen(gate_on):
    tester_id, code = _add_tester()
    assert db.get_tester(tester_id)["last_seen_at"] is None
    _login(code)
    assert db.get_tester(tester_id)["last_seen_at"] is not None


# --- ownership -------------------------------------------------------------

def test_another_tester_cannot_read_your_vestlus(gate_on):
    _, code_a = _add_tester("Mari")
    _, code_b = _add_tester("Jaan")
    a, b = _login(code_a), _login(code_b)

    vid = a.post("/vestlused").json()["vestluse_id"]
    assert a.get(f"/vestlused/{vid}").status_code == 200
    assert b.get(f"/vestlused/{vid}").status_code == 403


def test_another_tester_cannot_delete_your_vestlus(gate_on):
    _, code_a = _add_tester("Mari")
    _, code_b = _add_tester("Jaan")
    a, b = _login(code_a), _login(code_b)

    vid = a.post("/vestlused").json()["vestluse_id"]
    assert b.delete(f"/vestlused/{vid}").status_code == 403
    assert a.get(f"/vestlused/{vid}").status_code == 200


def test_another_tester_cannot_read_your_thread(gate_on):
    """The thread endpoints keyed off thread_id alone and never asked whose it was."""
    _, code_a = _add_tester("Mari")
    _, code_b = _add_tester("Jaan")
    a, b = _login(code_a), _login(code_b)

    vid = a.post("/vestlused").json()["vestluse_id"]
    db.save_recommendation(vid, ["Innovatsiooniosak"], [], [
        {"name": "Innovatsiooniosak", "measure_id": FOCUS_ID, "score": 5,
         "explanation": "Sobib.", "checks": []},
    ])
    thread_id = a.post(f"/vestlused/{vid}/threads", json={"measure_id": FOCUS_ID}).json()["thread_id"]

    assert a.get(f"/threads/{thread_id}/messages").status_code == 200
    assert b.get(f"/threads/{thread_id}/messages").status_code == 403
    assert b.delete(f"/threads/{thread_id}/messages").status_code == 403


def test_pre_gate_vestlused_stay_readable(gate_on):
    """Rows created before the gate existed have no owner and must not be orphaned."""
    vid = db.create_vestlus()  # tester_id is NULL
    _, code = _add_tester()
    assert _login(code).get(f"/vestlused/{vid}").status_code == 200
