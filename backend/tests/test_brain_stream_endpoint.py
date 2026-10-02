"""The Brain stream endpoint over HTTP.

    cd backend && python -m pytest tests/test_brain_stream_endpoint.py -q

The route is what makes Stop possible, so it is tested through the real ASGI
stack rather than by calling the generator: the assertions are about the wire
format a browser will parse and about the two gate conditions (feature off,
no messages) that must refuse before any streaming begins.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

DATE = "2026-07-01"


@pytest.fixture
def app(tmp_path, monkeypatch):
    import os
    db = tmp_path / "journal.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    for name in ("database", "main", "ai_settings", "ai_analysis", "daily_summary"):
        if name in sys.modules:
            sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.db = str(db)
        yield c, main


def _events(text):
    """Parse newline-delimited JSON, ignoring any trailing partial line."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def test_the_stream_returns_deltas_as_they_happen(app):
    """NDJSON, not one blob: the point of the endpoint is that the browser can
    render each line as it arrives."""
    client, main = app

    def scripted(messages, context, conn=None, account_id=None):
        yield ("delta", "Your first ")
        yield ("delta", "entry won.")
        yield ("usage", {"input_tokens": 900, "output_tokens": 40,
                         "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0, "model": "claude-opus-5",
                         "estimated_cost_usd": 0.01, "finished_at": "t"})

    monkey = main.brain_turn
    main.brain_turn = scripted
    try:
        with client.stream("POST", "/api/brain/stream",
                           json={"messages": [{"role": "user", "content": "hi"}]}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("application/x-ndjson")
            body = "".join(r.iter_text())
    finally:
        main.brain_turn = monkey

    events = _events(body)
    assert [e["type"] for e in events] == ["delta", "delta", "usage"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Your first entry won."
    usage = [e for e in events if e["type"] == "usage"][0]["ai_usage"]
    assert usage["spent"] is True
    assert usage["input_tokens"] == 900


def test_an_error_arrives_as_an_event_rather_than_a_hung_socket(app):
    client, main = app

    def failing(messages, context, conn=None, account_id=None):
        yield ("delta", "Half an answer")
        yield ("error", {"status": 429, "message": "The AI endpoint is rate-limiting requests."})

    monkey = main.brain_turn
    main.brain_turn = failing
    try:
        with client.stream("POST", "/api/brain/stream",
                           json={"messages": [{"role": "user", "content": "hi"}]}) as r:
            body = "".join(r.iter_text())
    finally:
        main.brain_turn = monkey

    events = _events(body)
    errors = [e for e in events if e["type"] == "error"]
    assert errors, "the browser must be told the answer stopped"
    assert errors[0]["status"] == 429
    assert "rate-limit" in errors[0]["detail"]
    assert any(e["type"] == "delta" for e in events), "text sent before the failure is kept"


def test_a_disabled_feature_refuses_before_streaming(app):
    client, main = app
    client.put("/api/ai-settings", json={"features": {"brain": False}})
    seen = []
    monkey = main.brain_turn
    main.brain_turn = lambda *a, **k: seen.append("built") or iter(())
    try:
        r = client.post("/api/brain/stream",
                        json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 403, r.text
        assert "turned off" in r.json()["detail"]
        assert seen == [], "a refusal must send nothing"
    finally:
        main.brain_turn = monkey
        client.put("/api/ai-settings", json={"features": {"brain": True}})


def test_no_messages_is_still_a_400(app):
    client, main = app
    assert client.post("/api/brain/stream", json={"messages": []}).status_code == 400


def test_the_plain_route_still_answers_in_one_piece(app):
    """The non-streaming path is not going away — callers and tests rely on it."""
    client, main = app

    def scripted(messages, context, conn=None, account_id=None):
        yield ("delta", "Everything is fine.")
        yield ("usage", {"input_tokens": 10, "output_tokens": 5,
                         "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0, "model": "claude-opus-5",
                         "estimated_cost_usd": 0.0, "finished_at": "t"})

    # The JSON route joins the stream inside ai_analysis, so it reads the name
    # from that module's globals rather than main's — patch where it is used.
    import ai_analysis
    monkey = ai_analysis.brain_turn
    ai_analysis.brain_turn = scripted
    try:
        r = client.post("/api/brain", json={"messages": [{"role": "user", "content": "hi"}]})
    finally:
        ai_analysis.brain_turn = monkey
    assert r.status_code == 200, r.text
    assert r.json()["response"] == "Everything is fine."
    assert r.json()["ai_usage"]["spent"] is True
