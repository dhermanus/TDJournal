"""What an AI call cost: token accounting, retries and the client factory.

    cd backend && python -m pytest tests/test_ai_usage.py -q

The app sends no telemetry, but a local token cost is still worth showing: every
AI endpoint that goes through `create_client()` records the last call's usage,
and the response carries an `ai_usage` block so the UI can say what an action
spent.

Two things worth knowing about the proxy in `ANTHROPIC_BASE_URL`: it strips the
`cache_creation_input_tokens` / `cache_read_input_tokens` fields rather than
reporting zeros, so those read as 0 here whatever happened upstream, and it
intermittently answers 502 under concurrent load. Both are handled instead of
assumed away — usage reads through `getattr(..., 0)`, and the retry policy is
declared below rather than inherited silently from SDK defaults.
"""
from __future__ import annotations

import threading
import time

import anthropic

# USD per million tokens. The real rate is whatever the configured endpoint
# charges, so this is an estimate and the response labels it as one. "Input"
# below is uncached input; cached input is billed separately and, on this
# proxy, not reported at all.
COST_PER_MTOK = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-5": (15.0, 75.0),
    "claude-3-5-haiku-latest": (0.8, 4.0),
}
# Fallback for a model added in Settings without a table entry, so a new name
# cannot make the estimate silently wrong by returning nothing.
DEFAULT_COST_PER_MTOK = (5.0, 25.0)

# Explicit rather than the SDK default of 2: one 502 from the proxy should not
# reach the user as a failed Day Review when the next retry would have answered.
# The SDK backs off between attempts and retries 408/429/5xx and connection
# errors; this only widens how many chances it gets.
MAX_RETRIES = 4


def estimate_cost(record: dict) -> float:
    """USD at the published rate for this model.

    Returns 0 when nothing was sent, so a refused or empty request never shows
    a charge it did not incur.
    """
    inp, out = COST_PER_MTOK.get(record.get("model") or "", DEFAULT_COST_PER_MTOK)
    total = (record.get("input_tokens", 0) * inp
             + record.get("output_tokens", 0) * out) / 1_000_000
    return round(total, 4)


class _UsageRecorder:
    """Per-thread storage: FastAPI runs sync endpoints on a threadpool, so two
    concurrent requests must not overwrite each other's numbers."""

    def __init__(self):
        self._local = threading.local()

    def record(self, response) -> dict:
        raw = getattr(response, "usage", None)
        record = {
            # getattr with 0: the proxy omits these keys entirely rather than
            # sending 0, and indexing a missing attribute would raise.
            "input_tokens": int(getattr(raw, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(raw, "output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
            "cache_read_input_tokens": int(getattr(raw, "cache_read_input_tokens", 0) or 0),
            "model": getattr(raw, "model", None) or getattr(response, "model", None),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        record["estimated_cost_usd"] = estimate_cost(record)
        self._local.record = record
        return record

    def last(self) -> dict | None:
        return getattr(self._local, "record", None)

    def clear(self) -> None:
        if hasattr(self._local, "record"):
            del self._local.record


_recorder = _UsageRecorder()


def last_usage() -> dict | None:
    """Usage recorded by the most recent AI call on this thread, or None."""
    return _recorder.last()


def forget_usage() -> None:
    """Drop the last call's numbers, so an endpoint can tell 'spent nothing'
    apart from 'spent whatever the previous endpoint spent'."""
    _recorder.clear()


def friendly_error(exc: BaseException) -> tuple[int, str]:
    """(status, message) for an SDK failure, in words the UI can show.

    Without this every AI endpoint turns a rate limit or a dropped connection
    into a 500 carrying the raw SDK string, which reads as a bug in the app
    rather than the two things it actually is: "busy, wait" and "could not
    reach the endpoint". Retries are attempted first (see MAX_RETRIES) — this
    only describes the failure that outlived them.
    """
    import anthropic
    if isinstance(exc, anthropic.RateLimitError):
        return 429, ("The AI endpoint is rate-limiting requests. Wait a moment and "
                     "try again — the app already retried a few times.")
    if isinstance(exc, anthropic.APIConnectionError):
        return 503, ("Could not reach the AI endpoint. Check ANTHROPIC_BASE_URL and "
                     "your connection, then try again.")
    if isinstance(exc, anthropic.AuthenticationError):
        return 401, "The AI API key was rejected. Update ANTHROPIC_API_KEY in backend/.env."
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 502)
        # 4xx from upstream are not the app's fault either; 5xx stays 500 so an
        # unexpected failure is still visible as one.
        if 400 <= status < 500:
            return status, f"The AI endpoint rejected the request ({status})."
        return 500, "The AI request failed after retries."
    if isinstance(exc, anthropic.APIError):
        return 502, "The AI request failed."
    # Anything else is an application bug; keep the original detail.
    return 500, str(exc)


def create_client(**kwargs) -> anthropic.Anthropic:
    """A client with the retry policy above and usage recording enabled.

    Subclassing the client would not work: call sites do
    `client.messages.create(...)`, so the recording has to sit on that handle.
    """
    kwargs.setdefault("max_retries", MAX_RETRIES)
    client = anthropic.Anthropic(**kwargs)
    real_messages = client.messages

    class RecordingMessages:
        def __getattr__(self, name):
            return getattr(real_messages, name)

        def create(self, *args, **kwargs):
            response = real_messages.create(*args, **kwargs)
            _recorder.record(response)
            return response

    client.messages = RecordingMessages()
    return client
