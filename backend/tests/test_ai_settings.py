"""AI settings and the privacy gates.

    cd backend && python -m pytest tests/test_ai_settings.py -q

The property worth protecting: a disabled feature sends nothing. So each test
asserts two things together — the endpoint refuses, *and* the generator it would
have called never ran.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import ai_settings
from ai_settings import default_config, load_config, save_config, validate_model


# ── the stored config ─────────────────────────────────────────────────────────

def test_defaults_cover_every_feature_and_are_on(tmp_path):
    cfg = default_config()
    assert set(cfg["features"]) == set(ai_settings.FEATURES)
    assert cfg["model"] == "claude-opus-5"
    assert all(cfg["features"].values()), "installing must not change current behaviour"


def test_load_is_sane_when_nothing_is_stored(tmp_path):
    conn = _conn(tmp_path)
    assert load_config(conn) == default_config()
    assert ai_settings.is_enabled(conn, "brain") is True


def test_unknown_feature_is_not_enabled(tmp_path):
    """A name nobody defined must never pass the gate — and cannot be stored."""
    conn = _conn(tmp_path)
    for name in ("attachments", "dailysummary", "", "BRAIN", " brain"):
        with pytest.raises(ValueError, match="unknown AI feature"):
            save_config(conn, features={name: True})
        assert ai_settings.is_enabled(conn, name) is False
    assert load_config(conn) == default_config(), "a rejected save must write nothing"


def test_a_toggle_can_be_turned_off_and_read_back(tmp_path):
    conn = _conn(tmp_path)
    cfg = save_config(conn, features={"brain": False})
    assert cfg["features"]["brain"] is False
    assert ai_settings.is_enabled(conn, "brain") is False
    assert ai_settings.is_enabled(conn, "diary") is True, "one toggle must not affect others"
    # survives a reload from disk
    assert load_config(conn)["features"]["brain"] is False


def test_saving_one_feature_leaves_the_model_alone(tmp_path):
    conn = _conn(tmp_path)
    save_config(conn, model="claude-sonnet-5")
    save_config(conn, features={"insights": False})
    cfg = load_config(conn)
    assert cfg["model"] == "claude-sonnet-5"
    assert cfg["features"]["insights"] is False
    assert cfg["features"]["brain"] is True


def test_unusable_config_is_rejected_not_quietly_applied(tmp_path):
    conn = _conn(tmp_path)
    # None means "leave the model alone", which is not the same as a bad value.
    before = load_config(conn)
    assert save_config(conn, model=None) == before
    for bad in ("", "   ", "model with spaces", "x", "a" * 99, 123, {"a": 1}):
        with pytest.raises(ValueError):
            save_config(conn, model=bad)
    # An empty patch is a valid no-op, not an error.
    assert save_config(conn, features={}) == load_config(conn)
    for bad in ({"brain": "yes"}, {"brain": 1}, {"nope": True}, "brain", ["brain"]):
        with pytest.raises(ValueError):
            save_config(conn, features=bad)
    # nothing partial was written
    assert load_config(conn) == default_config()


def test_a_corrupt_stored_value_falls_back_instead_of_failing(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO settings (account_id, key, value) VALUES (0, 'ai_config', ?)",
                 ("{not json",))
    conn.commit()
    assert load_config(conn) == default_config()

    conn.execute("UPDATE settings SET value = ? WHERE account_id = 0 AND key = 'ai_config'",
                 (json.dumps({"model": "???", "features": {"brain": False}}),))
    conn.commit()
    cfg = load_config(conn)
    assert cfg["model"] == "claude-opus-5", "an unusable stored model falls back"
    assert cfg["features"]["brain"] is False, "a valid stored toggle still applies"


def test_model_selection_reaches_the_call_sites(tmp_path):
    """The model is read at request time by six call sites with no connection."""
    conn = _conn(tmp_path)
    assert ai_settings.get_model() == "claude-opus-5"
    save_config(conn, model="claude-sonnet-5")
    assert ai_settings.get_model() == "claude-sonnet-5"
    # importing through ai_analysis must not reset it
    from ai_analysis import get_model
    assert get_model() == "claude-sonnet-5"
    ai_settings.set_model("claude-opus-5")


def _conn(tmp_path):
    import sqlite3
    c = sqlite3.connect(tmp_path / "s.db")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " account_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL,"
              " value TEXT NOT NULL, UNIQUE(account_id, key))")
    c.commit()
    return c


# ── the endpoint contract ─────────────────────────────────────────────────────

def _client(tmp_path, monkeypatch, db_path=None):
    import os
    os.environ["DATABASE_PATH"] = str(db_path or (tmp_path / "app.db"))
    monkeypatch.setenv("DATABASE_PATH", os.environ["DATABASE_PATH"])
    # uploads/ defaults to a path relative to the working directory; point it
    # at the temp dir so a gate that failed would write here, not live.
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(exist_ok=True)
    os.environ["UPLOAD_DIR"] = str(upload_dir)
    monkeypatch.setenv("UPLOAD_DIR", os.environ["UPLOAD_DIR"])
    for name in ("database", "main", "ai_settings", "ai_analysis", "daily_summary"):
        if name in sys.modules and name != "ai_settings":
            sys.modules.pop(name, None)
    import database
    database.DB_PATH = os.environ["DATABASE_PATH"]
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.db = os.environ["DATABASE_PATH"]
        return c, main


@pytest.fixture
def app(tmp_path, monkeypatch):
    c, main = _client(tmp_path, monkeypatch)
    c.upload_dir = tmp_path / "uploads"
    yield c, main


def test_settings_round_trip_exposes_the_disclosure(app):
    c, _ = app
    got = c.get("/api/ai-settings").json()
    assert got["model"] == "claude-opus-5"
    assert set(got["features"]) == set(ai_settings.FEATURES)
    assert set(got["feature_info"]) == set(ai_settings.FEATURES)
    for meta in got["feature_info"].values():
        assert meta["label"] and meta["sends"], "every toggle must state what it sends"
    assert got["sends_to"]
    assert "Turning it off" in got["notice"]

    put = c.put("/api/ai-settings", json={"features": {"brain": False}}).json()
    assert put["features"]["brain"] is False
    assert put["model"] == "claude-opus-5", "a partial update must not clear the rest"

    assert c.put("/api/ai-settings", json={"model": "not a model"}).status_code == 422
    assert c.put("/api/ai-settings", json={"features": {"nope": True}}).status_code == 422
    assert c.get("/api/ai-settings").json()["features"]["brain"] is False, "invalid PUT is ignored"


def test_disabled_brain_refuses_before_building_context(app):
    c, main = app
    seen = []
    monkey = {"original_context": main.build_brain_context, "original_generate": main.generate_brain_response}
    main.build_brain_context = lambda *a, **k: seen.append("context") or "{}"
    main.generate_brain_response = lambda *a, **k: seen.append("generate") or "answered"
    try:
        c.put("/api/ai-settings", json={"features": {"brain": False}})
        r = c.post("/api/brain", json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 403
        assert "turned off" in r.json()["detail"]
        assert seen == [], f"nothing may be built, but {seen} ran"

        # and it comes back when re-enabled
        c.put("/api/ai-settings", json={"features": {"brain": True}})
        r2 = c.post("/api/brain", json={"messages": [{"role": "user", "content": "hi"}]})
        assert r2.status_code == 200
        assert seen != []
    finally:
        main.build_brain_context = monkey["original_context"]
        main.generate_brain_response = monkey["original_generate"]


def test_disabled_features_refuse_their_endpoints(app):
    c, main = app
    cases = [
        ("insights", lambda: c.get("/api/insights")),
        ("weekly", lambda: c.get("/api/weekly-summary", params={"date": "2026-07-01"})),
        ("daily_summary", lambda: c.get("/api/daily-summary", params={"date": "2026-07-01"})),
    ]
    for feature, call in cases:
        c.put("/api/ai-settings", json={"features": {feature: False}})
        r = call()
        assert r.status_code == 403, f"{feature} still answered"
        assert "turned off" in r.json()["detail"]

    # one at a time: enabling the others must not unblock this one
    c.put("/api/ai-settings", json={"features": {"insights": True, "weekly": True,
                                                 "daily_summary": False}})
    assert c.get("/api/insights").status_code != 403
    assert c.get("/api/weekly-summary", params={"date": "2026-07-01"}).status_code != 403
    assert c.get("/api/daily-summary", params={"date": "2026-07-01"}).status_code == 403
    c.put("/api/ai-settings", json={"features": {"daily_summary": True}})


def test_disabled_diary_rejects_the_upload_before_saving_anything(app, tmp_path):
    c, _ = app
    c.put("/api/ai-settings", json={"features": {"diary": False}})
    files = {"file": ("notes.txt", b"i cut my loser early", "text/plain")}
    r = c.post("/api/upload-diary",
               data={"date": "2026-07-01", "account_id": "1"}, files=files)
    assert r.status_code == 403, r.text
    assert "turned off" in r.json()["detail"]
    # nothing was written: no file, no row
    assert c.get("/api/diary").json() == []
    assert not list(c.upload_dir.glob("*notes*")), "the file must not be saved at all"
