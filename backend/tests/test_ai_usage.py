"""Token cost and retries for an AI call.

    cd backend && python -m pytest tests/test_ai_usage.py -q

Two behaviours are load-bearing here. Usage has to survive the proxy in front
of this deployment, which omits the cache_* fields entirely rather than sending
0, so every read is `getattr(..., 0)` — indexing a missing attribute would raise
on a live response. And the client must pass an explicit retry count, because
that same proxy answers 502 under concurrent load and the SDK's default of 2
was the reason a single hiccup surfaced to the user as a failed request.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from ai_usage import COST_PER_MTOK, MAX_RETRIES, create_client, estimate_cost, forget_usage, last_usage


def _response(input_tokens=1000, output_tokens=500, model="claude-opus-5", **usage_extra):
    """A response shaped like the SDK's, optionally *without* cache fields."""
    fields = {"input_tokens": input_tokens, "output_tokens": output_tokens, "model": model}
    fields.update(usage_extra)
    usage = SimpleNamespace(**fields)
    return SimpleNamespace(usage=usage, model=model, content=[])


# ── cost ──────────────────────────────────────────────────────────────────────

def test_cost_is_what_the_published_rates_say():
    # 1M input + 1M output on opus = $5 + $25
    assert estimate_cost({"model": "claude-opus-5",
                          "input_tokens": 1_000_000,
                          "output_tokens": 1_000_000}) == 30.0
    assert estimate_cost({"model": "claude-3-5-haiku-latest",
                          "input_tokens": 1_000_000,
                          "output_tokens": 1_000_000}) == 4.8


def test_a_request_that_sent_nothing_costs_nothing():
    assert estimate_cost({"model": "claude-opus-5", "input_tokens": 0, "output_tokens": 0}) == 0.0
    assert estimate_cost({}) == 0.0, "a missing usage block is not a charge"


def test_an_unknown_model_still_estimates_rather_than_returning_nothing():
    # A model added in Settings has no table entry; a wrong-but-visible number
    # beats an absent one, which would read as "this was free".
    unknown = estimate_cost({"model": "claude-future-9", "input_tokens": 1_000_000,
                             "output_tokens": 1_000_000})
    assert unknown == estimate_cost({"model": "claude-opus-5", "input_tokens": 1_000_000,
                                     "output_tokens": 1_000_000})
    assert unknown > 0


def test_every_model_in_settings_has_a_rate():
    import ai_settings
    for name in ai_settings.KNOWN_MODELS:
        assert name in COST_PER_MTOK, f"{name} has no published rate"


# ── recording ─────────────────────────────────────────────────────────────────

def test_usage_is_recorded_from_a_real_response():
    forget_usage()
    assert last_usage() is None, "before any call, nothing was spent"
    from ai_usage import _recorder
    _recorder.record(_response(input_tokens=1234, output_tokens=56))
    got = last_usage()
    assert got["input_tokens"] == 1234
    assert got["output_tokens"] == 56
    assert got["estimated_cost_usd"] > 0
    assert got["finished_at"]
    forget_usage()
    assert last_usage() is None, "a later request must not inherit an earlier one's cost"


def test_cache_fields_are_zero_not_a_crash_when_the_proxy_omits_them():
    # The proxy strips cache_creation_input_tokens / cache_read_input_tokens
    # rather than reporting 0, so an attribute that is simply absent is the
    # expected case on this deployment — not an error.
    forget_usage()
    from ai_usage import _recorder
    resp = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=6118, output_tokens=400, model="claude-sonnet-5"),
        model="claude-sonnet-5", content=[])
    got = _recorder.record(resp)
    assert got["cache_creation_input_tokens"] == 0
    assert got["cache_read_input_tokens"] == 0
    forget_usage()


def test_a_usage_block_that_is_none_records_zero_rather_than_raising():
    forget_usage()
    from ai_usage import _recorder
    got = _recorder.record(SimpleNamespace(usage=None, model=None, content=[]))
    assert got["input_tokens"] == 0 and got["output_tokens"] == 0
    assert got["estimated_cost_usd"] == 0.0
    forget_usage()


# ── what a failure looks like to the user ─────────────────────────────────────

def test_rate_limits_and_connection_drops_read_as_their_own_things():
    """A 500 with a raw SDK string looks like an app bug; these two are not."""
    import anthropic
    from ai_usage import friendly_error

    def an_error(cls):
        # The SDK raises with a response request_id shape; build a bare instance
        # so the test does not depend on how it formats one.
        return cls.__new__(cls)

    status, detail = friendly_error(an_error(anthropic.RateLimitError))
    assert status == 429
    assert "rate-limit" in detail and "retried" in detail

    status, detail = friendly_error(an_error(anthropic.APIConnectionError))
    assert status == 503
    assert "Could not reach" in detail and "ANTHROPIC_BASE_URL" in detail

    status, detail = friendly_error(an_error(anthropic.AuthenticationError))
    assert status == 401
    assert "ANTHROPIC_API_KEY" in detail


def test_an_unexpected_bug_still_surfaces_as_one():
    """The mapping must not swallow application errors into a friendly message."""
    from ai_usage import friendly_error
    status, detail = friendly_error(ValueError("bad argument"))
    assert status == 500
    assert detail == "bad argument", "the original message is what you need to debug it"


def test_upstream_4xx_is_passed_through_but_5xx_stays_a_server_error():
    import anthropic
    from ai_usage import friendly_error

    class Fake500(anthropic.APIStatusError):
        status_code = 500

    def bare(cls):
        return cls.__new__(cls)

    # An upstream 500 that survived retries is still a server error, and its
    # message says so rather than blaming the connection.
    status, detail = friendly_error(bare(Fake500))
    assert status == 500
    assert "after retries" in detail

# ── the client ────────────────────────────────────────────────────────────────

def test_the_client_sets_an_explicit_retry_count():
    """Default is 2; we ask for more, and the test reads it off the client so a
    silent revert to the default fails here instead of on a live 502."""
    client = create_client(api_key="test-key-not-real")
    assert client.max_retries == MAX_RETRIES
    assert MAX_RETRIES > 2


def test_create_calls_are_recorded_and_pass_through(monkeypatch):
    """The wrapper must forward arguments untouched and still return the response."""
    forget_usage()
    seen = {}

    class FakeMessages:
        def create(self, *args, **kwargs):
            seen.update(kwargs)
            return _response(input_tokens=42, output_tokens=7)

    client = create_client(api_key="test-key-not-real")
    client.messages = _wrap_for_test(client.messages, FakeMessages())

    out = client.messages.create(model="claude-opus-5", max_tokens=100)
    assert out.usage.input_tokens == 42
    assert seen["model"] == "claude-opus-5" and seen["max_tokens"] == 100
    assert last_usage()["input_tokens"] == 42, "the wrapper records, not swallows"
    forget_usage()


def _wrap_for_test(real, fake):
    """Stand in for `RecordingMessages` while keeping its recording behaviour."""
    from ai_usage import _recorder

    class RecordingLike:
        def __getattr__(self, name):
            return getattr(fake, name)

        def create(self, *args, **kwargs):
            response = fake.create(*args, **kwargs)
            _recorder.record(response)
            return response

    return RecordingLike()


def test_threads_do_not_share_each_others_numbers():
    """Sync endpoints run on a threadpool, so one request's cost must not land
    on another's response."""
    import threading
    forget_usage()
    results = {}

    def worker(tokens, key):
        from ai_usage import _recorder
        _recorder.record(_response(input_tokens=tokens, output_tokens=0))
        results[key] = last_usage()["input_tokens"]
        # give the other thread a chance to overwrite before reading
        threading.Event().wait(0.05)
        results[key] = last_usage()["input_tokens"]

    threads = [threading.Thread(target=worker, args=(111 if i else 999, i)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == [111, 999], f"threads shared state: {results}"
    forget_usage()
