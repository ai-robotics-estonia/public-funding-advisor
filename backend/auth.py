"""Per-tester access gate for the shared pilot deployment.

Until now the only "identity" was knowledge of a 5-char vestluse_id, which is
guessable in principle and anonymous in practice — the audit trail could not say
who did anything. Each tester now gets a personal code from the CLI
(tools/testers.py); logging in exchanges it for a signed cookie.

Off by default (`AUTH_ENABLED`), mirroring how the scraper is gated: local
development and the whole test suite behave exactly as before, and the container
turns it on. When it is off, require_tester() returns None and every route stays
open, so nothing here can silently half-apply.

Codes are stored as a keyed HMAC, never in plaintext and never as a bare hash:
without SESSION_SECRET a stolen database yields nothing to attack offline.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time

from fastapi import HTTPException, Request, Response

from backend import db

log = logging.getLogger(__name__)

COOKIE_NAME = "fa_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600

# Login codes: 10 chars from a 32-symbol alphabet ≈ 50 bits. Excludes the
# characters people mistype when reading a code aloud (0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 10

# Dev fallback secret, generated per process. Sessions then die on restart,
# which is a safe failure: it never silently accepts forged cookies.
_EPHEMERAL_SECRET = secrets.token_hex(32)
_warned_missing_secret = False


def enabled() -> bool:
    """True when the access gate should be enforced."""
    return os.environ.get("AUTH_ENABLED", "").strip() not in ("", "0", "false", "False")


def _secret() -> bytes:
    global _warned_missing_secret
    configured = os.environ.get("SESSION_SECRET", "").strip()
    if configured:
        return configured.encode("utf-8")
    if enabled() and not _warned_missing_secret:
        log.warning(
            "SESSION_SECRET is not set; using a per-process secret. "
            "Every restart will log all testers out."
        )
        _warned_missing_secret = True
    return _EPHEMERAL_SECRET.encode("utf-8")


def generate_code() -> str:
    """A fresh login code to hand to one tester."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


def hash_code(code: str) -> str:
    """Keyed hash of a login code, for storage and lookup."""
    return hmac.new(_secret(), code.strip().upper().encode("utf-8"), hashlib.sha256).hexdigest()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_cookie_value(tester_id: str, now: float | None = None) -> str:
    """Build a signed 'tester_id.expiry.signature' cookie value."""
    expires = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
    payload = f"{tester_id}.{expires}"
    return f"{payload}.{_sign(payload)}"


def read_cookie_value(value: str, now: float | None = None) -> str | None:
    """Return the tester_id from a cookie value, or None if invalid or expired."""
    parts = (value or "").split(".")
    if len(parts) != 3:
        return None
    tester_id, expires, signature = parts
    if not hmac.compare_digest(signature, _sign(f"{tester_id}.{expires}")):
        return None
    try:
        if int(expires) < (now if now is not None else time.time()):
            return None
    except ValueError:
        return None
    return tester_id


def login(code: str) -> dict | None:
    """Exchange a login code for its tester row, or None if unknown/inactive."""
    tester = db.get_tester_by_code_hash(hash_code(code))
    if tester is None:
        return None
    db.touch_tester(tester["tester_id"])
    return tester


def cookie_secure() -> bool:
    """Whether to mark the session cookie Secure. On by default.

    The deployment is HTTPS-only behind Tailscale, so Secure is correct there.
    Running uvicorn directly over http would otherwise lock you out — browsers
    accept a Secure cookie but never send it back over a plain connection — so
    COOKIE_SECURE=0 exists for that case only.
    """
    return os.environ.get("COOKIE_SECURE", "1").strip() not in ("0", "false", "False")


def set_session_cookie(response: Response, tester_id: str) -> None:
    """Attach the signed session cookie."""
    response.set_cookie(
        COOKIE_NAME,
        make_cookie_value(tester_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def cookie_tester(request: Request) -> str | None:
    """tester_id from a validly-signed cookie, or None. Pure crypto, no database.

    The middleware calls this on the event loop, so it deliberately does no I/O.
    Confirming that the tester still exists and is active is require_tester's
    job, which runs in the threadpool.
    """
    if not enabled():
        return None
    value = request.cookies.get(COOKIE_NAME)
    if not value:
        return None
    return read_cookie_value(value)


def require_tester(request: Request) -> str | None:
    """FastAPI dependency: 401 unless a valid, still-active session is present.

    Returns None (and allows the request) when the gate is disabled, so the
    handlers can use the same dependency in both modes.
    """
    if not enabled():
        return None
    tester_id = getattr(request.state, "tester_id", None)
    if tester_id is None:
        raise HTTPException(status_code=401, detail="logi sisse")
    tester = db.get_tester(tester_id)
    if tester is None or not tester["active"]:
        raise HTTPException(status_code=401, detail="logi sisse")
    return tester_id


def require_owner(request: Request, owner_id: str | None) -> None:
    """403 unless the caller owns this resource.

    Rows created before the gate existed have owner_id None; those stay readable
    so the pilot does not start by orphaning the developer's own test sessions.
    """
    if not enabled() or owner_id is None:
        return
    if getattr(request.state, "tester_id", None) != owner_id:
        raise HTTPException(status_code=403, detail="see vestlus ei kuulu sulle")
