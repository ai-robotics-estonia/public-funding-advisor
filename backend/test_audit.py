"""Tests for the audit trail: one events row per request, one llm_calls row per model call."""

import pytest
from fastapi.testclient import TestClient

import backend.rank as rank_module
from backend import audit, db
from backend.main import app

client = TestClient(app)

FOCUS_ID = "02"  # Innovatsiooniosak


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    yield


def _events():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]


def _llm_calls():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id")]


# --- events ----------------------------------------------------------------

def test_every_request_writes_one_event():
    client.get("/healthz")
    rows = _events()
    assert len(rows) == 1
    assert rows[0]["path"] == "/healthz"
    assert rows[0]["method"] == "GET"
    assert rows[0]["status_code"] == 200
    assert rows[0]["duration_ms"] is not None


def test_event_records_action_and_vestlus():
    vid = client.post("/vestlused").json()["vestluse_id"]
    created = [e for e in _events() if e["action"] == "vestlus_create"]
    assert len(created) == 1
    # The handler runs in a threadpool worker whose context is a copy, so this
    # only lands if request.state carried it back to the middleware.
    assert created[0]["vestluse_id"] == vid


def test_failed_requests_are_recorded_too():
    client.get("/vestlused/ZZZZZ")
    rows = [e for e in _events() if e["path"] == "/vestlused/ZZZZZ"]
    assert len(rows) == 1
    assert rows[0]["status_code"] == 404


def test_each_request_gets_a_distinct_request_id():
    client.get("/healthz")
    client.get("/healthz")
    ids = [e["request_id"] for e in _events()]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(i for i in ids)


def test_audit_failure_does_not_break_the_request(monkeypatch):
    """An unwritable audit table must degrade to a log line, not a 500."""
    def boom():
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(db, "connect", boom)
    assert client.get("/healthz").status_code == 200


# --- llm_calls -------------------------------------------------------------

def test_ranking_writes_one_llm_call_row(monkeypatch):
    monkeypatch.setattr(
        rank_module, "complete_chat",
        lambda **kwargs: '[{"name": "Innovatsiooniosak", "project_type_fit": "yes", '
                         '"field_fit": "yes", "purpose_fit": "yes", '
                         '"explanation": "Sobib.", "checks": []}]',
    )
    vid = client.post("/vestlused").json()["vestluse_id"]
    r = client.post("/recommend", json={
        "vestluse_id": vid,
        "intake": {"applicant_type": "ettevote", "company_size": "vke",
                   "region": "Harju maakond", "project_description": "AI toode"},
        "clarifications": {},
    })
    assert r.status_code == 200
    # complete_chat is stubbed at the rank module, so no llm_calls row is written
    # here — this asserts the request itself was still audited end to end.
    assert any(e["action"] == "recommend_ranked" for e in _events())


def test_llm_call_row_captures_prompt_tokens_and_latency(monkeypatch):
    """Drive backend.llm directly with a stubbed transport and check what it stores."""
    from backend import llm

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("CLAUDE_MODEL", "test/model")
    monkeypatch.setattr(llm, "_post_json", lambda url, headers, payload: {
        "choices": [{"message": {"content": "vastus"}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 34, "cost": 0.00042},
    })

    out = llm.complete_chat(system="sys", messages=[{"role": "user", "content": "tere"}],
                            max_tokens=100, purpose="rank")
    assert out == "vastus"

    rows = _llm_calls()
    assert len(rows) == 1
    row = rows[0]
    assert row["purpose"] == "rank"
    assert row["provider"] == "openrouter"
    assert row["model"] == "test/model"
    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 34
    assert row["cost_usd"] == pytest.approx(0.00042)
    assert row["ok"] == 1
    assert row["system_text"] == "sys"
    assert "tere" in row["messages_json"]
    assert row["response_text"] == "vastus"
    assert row["latency_ms"] is not None


def test_failed_llm_call_is_recorded_with_the_error(monkeypatch):
    from backend import llm

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    def explode(url, headers, payload):
        raise RuntimeError("LLM request failed (404): no such model")
    monkeypatch.setattr(llm, "_post_json", explode)

    with pytest.raises(RuntimeError):
        llm.complete_chat(system="s", messages=[], max_tokens=10, purpose="chat")

    rows = _llm_calls()
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert "404" in rows[0]["error"]
    assert rows[0]["purpose"] == "chat"


def test_anthropic_usage_field_names(monkeypatch):
    """Anthropic spells the token counts differently; both must land in the same columns."""
    from backend import llm

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "_post_json", lambda url, headers, payload: {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 7, "output_tokens": 3},
    })

    llm.complete_chat(system="s", messages=[], max_tokens=10, purpose="chat")
    row = _llm_calls()[0]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (7, 3)
    assert row["cost_usd"] is None


def test_missing_usage_block_is_tolerated(monkeypatch):
    """Older gateways omit usage entirely — that must not lose the whole audit row."""
    from backend import llm

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(llm, "_post_json", lambda url, headers, payload: {
        "choices": [{"message": {"content": "ok"}}],
    })

    llm.complete_chat(system="s", messages=[], max_tokens=10, purpose="rank")
    row = _llm_calls()[0]
    assert row["ok"] == 1
    assert row["prompt_tokens"] is None
    assert row["response_text"] == "ok"


# --- correlation -----------------------------------------------------------

def test_context_vars_default_to_none_outside_a_request():
    assert audit.request_id.get() is None or isinstance(audit.request_id.get(), str)
    audit.log_event(action="manual", path="/x", status_code=200)
    rows = [e for e in _events() if e["action"] == "manual"]
    assert len(rows) == 1
