"""Serving the built frontend from the API — container mode.

    cd backend && python -m pytest tests/test_spa_static.py -q

The two modes must not bleed into each other:

* **Local/Windows (no FRONTEND_DIR)** — `GET /` is still `{"status": "ok"}`,
  because that is what has answered there all along, and nothing in the tree
  asserted it except by convention. `launch.bat` never sets the variable.
* **Container (FRONTEND_DIR set)** — `GET /` is the built `index.html`, hashed
  assets live under `/static/`, and a build that was not copied into the image
  answers 503 rather than a blank page that looks like an empty journal.

Also pinned: a missing API path stays 404 even with a SPA mounted, so JSON
callers never receive HTML.
"""
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

INDEX = """<!doctype html><html><head><meta charset="utf-8">
<link rel="manifest" href="/manifest.json"><link rel="icon" href="/favicon.ico">
<title>TDJournal</title></head><body><div id="root"></div>
<script src="/static/js/main.abc123.js" defer></script></body></html>"""


@pytest.fixture
def spa_dir(tmp_path):
    root = tmp_path / "frontend-build"
    (root / "static" / "js").mkdir(parents=True)
    (root / "static" / "css").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "static" / "js" / "main.abc123.js").write_text("console.log('hi')")
    (root / "static" / "css" / "main.abc123.css").write_text("body{}")
    (root / "manifest.json").write_text('{"name":"TDJournal"}')
    (root / "robots.txt").write_text("User-agent: *\nDisallow:\n")
    (root / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    return root


def _client(monkeypatch, frontend_dir=None, tmp_path=None):
    import sqlite3
    for var in ("TDJ_AUTH", "TDJ_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    if frontend_dir is None:
        monkeypatch.delenv("FRONTEND_DIR", raising=False)
    else:
        monkeypatch.setenv("FRONTEND_DIR", str(frontend_dir))
    if tmp_path is not None:
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "journal.db"))
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    for name in ("database", "main"):
        sys.modules.pop(name, None)
    import database
    database.DB_PATH = os.environ["DATABASE_PATH"]
    import main
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    c.__enter__()
    return c


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    """The Windows/local default: no FRONTEND_DIR at all."""
    c = _client(monkeypatch, None, tmp_path)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def spa_client(tmp_path, monkeypatch, spa_dir):
    c = _client(monkeypatch, spa_dir, tmp_path)
    yield c
    c.__exit__(None, None, None)


def test_local_mode_keeps_the_health_json(local_client):
    """launch.bat path: unchanged behaviour, no build required."""
    r = local_client.get("/")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}, r.text


def test_spa_mode_serves_index_html(spa_client):
    r = spa_client.get("/")
    assert r.status_code == 200, r.text
    assert 'id="root"' in r.text and "main.abc123.js" in r.text, r.text[:200]
    # and the same document at index.html, which is what the browser requests
    # after a hard reload of a hashed asset URL.
    assert spa_client.get("/index.html").text == r.text


def test_spa_assets_are_served(spa_client):
    js = spa_client.get("/static/js/main.abc123.js")
    assert js.status_code == 200 and js.text == "console.log('hi')"
    css = spa_client.get("/static/css/main.abc123.css")
    assert css.status_code == 200 and css.text == "body{}"
    assert spa_client.get("/manifest.json").status_code == 200
    assert spa_client.get("/robots.txt").status_code == 200


def test_missing_api_path_stays_404_not_html(spa_client):
    """Mounted-last: a JSON caller must never receive the SPA as an answer."""
    r = spa_client.get("/api/does-not-exist")
    assert r.status_code == 404, (r.status_code, r.text[:120])
    assert 'id="root"' not in r.text, "HTML leaked into a 404 for an API path"
    assert r.headers.get("content-type", "").startswith("application/json"), r.headers.get("content-type")


def test_api_routes_still_work_in_spa_mode(spa_client):
    """The mount must not shadow anything the app depends on."""
    assert spa_client.get("/api/accounts").status_code == 200
    assert spa_client.get("/api/trades").status_code == 200
    assert spa_client.get("/api/kpis").status_code == 200


def test_missing_build_fails_visibly(tmp_path, monkeypatch):
    """A container whose build step was skipped must not look like an empty app."""
    empty = tmp_path / "no-build"
    empty.mkdir()
    c = _client(monkeypatch, empty, tmp_path)
    try:
        r = c.get("/")
        assert r.status_code == 503, (r.status_code, r.text[:200])
        assert "Frontend build not found" in r.text, r.text[:200]
        assert 'id="root"' not in r.text, "an empty shell would look like an empty journal"
    finally:
        c.__exit__(None, None, None)
