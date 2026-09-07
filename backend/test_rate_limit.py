"""Tests for the in-memory vestlus rate limiter."""

from fastapi.testclient import TestClient

from backend import db, rate_limit
from backend.main import app
from backend.rate_limit import MAX_REQUESTS

client = TestClient(app)


def test_rate_limit_returns_429_after_max(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    rate_limit.reset()

    # Exhaust the budget with GET /vestlused/{id} lookups (404 still counts).
    last = None
    for _ in range(MAX_REQUESTS):
        last = client.get("/vestlused/ZZZZZ")
        assert last.status_code in (404, 429)
    assert last.status_code == 404

    blocked = client.get("/vestlused/ZZZZZ")
    assert blocked.status_code == 429
