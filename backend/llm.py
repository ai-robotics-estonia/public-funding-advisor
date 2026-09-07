"""Shared LLM transport for recommendation ranking and chat."""

import json
import logging
import os
import threading
import time
from typing import Any
from urllib import error, request

from backend import audit

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODELS = {
    "openrouter": "anthropic/claude-3.5-sonnet",
    "anthropic": "claude-3-5-sonnet-latest",
}

# Cap on LLM calls in flight at once. Each one holds a threadpool worker for up
# to 60 s, so without a cap a handful of simultaneous /recommend requests would
# starve the pool and stall unrelated endpoints — and hit the provider's own
# rate limit. Sized for the pilot (2-5 concurrent testers); raise with the pool.
MAX_CONCURRENT_CALLS = int(os.environ.get("LLM_MAX_CONCURRENCY", "4"))
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)


def _provider() -> str:
    configured = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if configured:
        return configured
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openrouter"


def _model() -> str:
    return os.environ.get("CLAUDE_MODEL", DEFAULT_MODELS.get(_provider(), DEFAULT_MODELS["openrouter"]))


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed ({exc.code}): {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)


def _usage(response: dict[str, Any], provider: str) -> tuple[int | None, int | None, float | None]:
    """Pull (prompt_tokens, completion_tokens, cost_usd) out of a provider reply.

    Anthropic spells them input/output_tokens and reports no cost. OpenRouter
    returns cost only when the request asked for it, and older gateways omit
    the block entirely — hence every field is optional.
    """
    usage = response.get("usage") or {}
    if provider == "anthropic":
        return usage.get("input_tokens"), usage.get("output_tokens"), None
    return usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cost")


def complete_chat(system: str, messages: list[dict[str, str]], max_tokens: int,
                  purpose: str = "unknown") -> str:
    """Run one chat completion call and return the assistant text.

    `purpose` ("rank" | "chat" | "scraper_extract") is recorded in the audit
    trail so cost and latency can be attributed per feature afterwards.
    """
    provider = _provider()
    model = _model()
    started = time.monotonic()

    def _record(response_text, prompt_tok, completion_tok, cost, ok, err=None):
        audit.log_llm_call(
            purpose=purpose, provider=provider, model=model,
            system_text=system, messages=messages, response_text=response_text,
            prompt_tokens=prompt_tok, completion_tokens=completion_tok,
            cost_usd=cost, latency_ms=int((time.monotonic() - started) * 1000),
            ok=ok, error=err,
        )

    # Acquired around the network call only; audit writes stay outside so a slow
    # SQLite write never occupies a slot another caller is waiting for.
    with _slots:
        try:
            if provider == "anthropic":
                response = _post_json(
                    ANTHROPIC_URL,
                    {
                        "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    {
                        "model": model,
                        "max_tokens": max_tokens,
                        "system": system,
                        "messages": messages,
                    },
                )
                # response["content"] is a list of content blocks, e.g. [{"type": "text", "text": "..."}] -
                # read the block's "text" field directly, not via _message_content (that helper is for a
                # message object shaped like {"role": ..., "content": [...]}, not a bare content block).
                text = response["content"][0].get("text", "")

            elif provider == "openrouter":
                response = _post_json(
                    OPENROUTER_URL,
                    {
                        "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
                        "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "http://localhost"),
                        "X-Title": os.environ.get("OPENROUTER_TITLE", "AI Funding Advisor"),
                        "content-type": "application/json",
                    },
                    {
                        "model": model,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "system", "content": system}, *messages],
                        # Asks OpenRouter to report token counts and cost back in
                        # the response, so the audit row is complete without a
                        # second /generation lookup.
                        "usage": {"include": True},
                    },
                )
                text = _message_content(response["choices"][0]["message"])

            else:
                raise RuntimeError(f"Unsupported LLM provider: {provider}")

        except Exception as exc:
            _record(None, None, None, None, ok=False, err=str(exc)[:2000])
            raise

    prompt_tok, completion_tok, cost = _usage(response, provider)
    _record(text, prompt_tok, completion_tok, cost, ok=True)
    return text
