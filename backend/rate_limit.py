"""Simple in-memory rate limiters for the API.

Keyed by tester when one is logged in, by client IP otherwise. The tester key
matters behind the Tailscale proxy: every request arrives from the proxy's own
address, so an IP-only key would give all testers one shared budget and they
would 429 each other.

Two independent sliding windows over that same caller key:

- The request limiter (check_rate_limit) guards every endpoint. It stops casual
  code-guessing against the short readable vestluse_id without adding Redis or
  similar.
- The LLM limiter (check_llm_rate_limit) counts model calls, not requests. It
  exists because /refine fans out: one request becomes ceil(n/BATCH_SIZE) calls,
  so a per-request cap does not bound how much work a single caller can start.

State is per-process, which is why the app runs a single uvicorn worker — see
the deployment notes. Good enough for a pilot; a second worker would need Redis
or a shared table.
"""

import time
from collections import defaultdict

from fastapi import HTTPException, Request

# Max requests per window per caller. Generous for normal use, tight against
# brute-forcing the code space.
MAX_REQUESTS = 60
WINDOW_SECONDS = 60

# Max LLM calls per window per caller, counted across every endpoint that
# reaches the model. Well below MAX_REQUESTS because each call is slow and paid for.
LLM_MAX_CALLS = 30

# (bucket, caller key) -> list of timestamps within the current window
_hits: dict[tuple[str, str], list[float]] = defaultdict(list)


def client_ip(request: Request) -> str:
    """Best-effort client IP; falls back to 'unknown' when the socket is gone."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def caller_key(request: Request) -> str:
    """Identify the caller: the authenticated tester if there is one, else the IP.

    auth.require_tester() stashes the tester on request.state; this reads it
    defensively so unauthenticated routes (/login, /healthz) still rate-limit.
    """
    tester = getattr(request.state, "tester_id", None)
    if tester:
        return f"tester:{tester}"
    return f"ip:{client_ip(request)}"


def _check(bucket: str, request: Request, limit: int, cost: int) -> None:
    """Raise 429 if charging `cost` would exceed `limit` in this bucket's window."""
    key = (bucket, caller_key(request))
    now = time.time()
    cutoff = now - WINDOW_SECONDS

    # Drop keys that have gone quiet. Without this, _hits grew forever: the old
    # code pruned timestamps inside a key but never removed the key itself, so
    # every distinct caller ever seen stayed resident for the life of the process.
    for stale in [k for k, ts in _hits.items() if not ts or ts[-1] <= cutoff]:
        del _hits[stale]

    recent = [t for t in _hits[key] if t > cutoff]
    if len(recent) + cost > limit:
        _hits[key] = recent
        raise HTTPException(status_code=429, detail="liiga palju päringuid, proovi hiljem")
    recent.extend([now] * cost)
    _hits[key] = recent


def check_rate_limit(request: Request) -> None:
    """Raise 429 if this caller has exceeded MAX_REQUESTS in WINDOW_SECONDS."""
    _check("request", request, MAX_REQUESTS, 1)


def check_llm_rate_limit(request: Request, cost: int = 1) -> None:
    """Charge `cost` LLM calls to this caller's budget, or raise 429.

    Callers pass the number of calls they are about to make, so a request that
    fans out costs what it actually costs. Retries are not charged.
    """
    _check("llm", request, LLM_MAX_CALLS, max(cost, 1))


def reset() -> None:
    """Clear all counters — used by tests."""
    _hits.clear()
