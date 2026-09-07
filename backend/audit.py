"""Audit trail: one row per HTTP request, one row per LLM call.

Before this existed the app logged nothing — a successful /recommend left no
trace that a model had been called at all, so there was no way to answer "who
used this", "why did it recommend that", or "what did it cost".

Correlation works through context variables rather than threading a request_id
through every function signature. FastAPI runs the sync route handlers in the
AnyIO threadpool, and AnyIO copies the caller's context into the worker thread,
so a value set by the middleware is visible inside rank()/chat() and down in
llm.complete_chat(). Nothing else in the codebase has to know about it.

Every write is best-effort: an audit failure must never break the request it is
describing, so all of them are wrapped and downgraded to a log line.
"""

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

from backend import db

log = logging.getLogger(__name__)

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
tester_id: ContextVar[str | None] = ContextVar("tester_id", default=None)
vestluse_id: ContextVar[str | None] = ContextVar("vestluse_id", default=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value) -> str | None:
    """JSON-encode for storage, never raising on an odd object."""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"unserialisable": repr(value)[:500]}, ensure_ascii=False)


def log_event(
    *,
    ip: str | None = None,
    method: str | None = None,
    path: str | None = None,
    action: str | None = None,
    vestlus: str | None = None,
    thread_id: str | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
    detail: dict | None = None,
) -> None:
    """Record one request (or one notable action) in the events table.

    `vestlus` is an explicit override for the context variable. The middleware
    needs it: a route handler runs in a threadpool worker whose context is a
    *copy*, so a vestluse_id set there never travels back out. Handlers pass it
    via request.state instead, and the middleware forwards it here.
    """
    try:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO events (ts, request_id, tester_id, ip, method, path,
                                       action, vestluse_id, thread_id, status_code,
                                       duration_ms, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now(), request_id.get(), tester_id.get(), ip, method, path,
                    action, vestlus or vestluse_id.get(), thread_id, status_code,
                    duration_ms, _dump(detail),
                ),
            )
    except Exception as exc:  # audit must never break the request it describes
        log.warning("audit: could not write event: %s", exc)


def log_llm_call(
    *,
    purpose: str,
    provider: str | None,
    model: str | None,
    system_text: str | None,
    messages: list | None,
    response_text: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cost_usd: float | None,
    latency_ms: int | None,
    ok: bool,
    error: str | None = None,
) -> None:
    """Record one model call, successful or not, with its full prompt and reply."""
    try:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO llm_calls (ts, request_id, tester_id, vestluse_id, purpose,
                                          provider, model, system_text, messages_json,
                                          response_text, prompt_tokens, completion_tokens,
                                          cost_usd, latency_ms, ok, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now(), request_id.get(), tester_id.get(), vestluse_id.get(), purpose,
                    provider, model, system_text, _dump(messages),
                    response_text, prompt_tokens, completion_tokens,
                    cost_usd, latency_ms, 1 if ok else 0, error,
                ),
            )
    except Exception as exc:
        log.warning("audit: could not write llm_call: %s", exc)
