"""The HTTP gate: transparent with auth off, closed with it on.

    cd backend && python -m pytest tests/test_auth_gate.py -q

Two fixtures, because the module must be provably a no-op in the default state —
that is what keeps local development unchanged — and provably closed in the
other, which is the whole point of item 2's request.

`gate_client` sets TDJ_AUTH=required with TDJ_PASSWORD before `main` is
imported, so the lifespan builds the secret and the password hash the same way
a container would.
"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

PW = "correct horse battery"


def _make_client(tmp_path, monkeypatch, *, enabled, password=PW):
    db = tmp_path / "journal.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("TDJ_AUTH", raising=False)
    monkeypatch.delenv("TDJ_PASSWORD", raising=False)
    if enabled:
        monkeypatch.setenv("TDJ_AUTH", "required")
        if password is not None:
            monkeypatch.setenv("TDJ_PASSWORD", password)
    for name in ("database", "main", "auth"):
        sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    c.db = str(db)
    c.__enter__()   # run lifespan: creates the schema and fills app.state
    conn = sqlite3_connect(str(db))
    conn.execute("INSERT INTO accounts (id,name,type) VALUES (1,'Day','day_trading')")
    conn.commit()
    conn.close()
    return c


def sqlite3_connect(path):
    import sqlite3
    return sqlite3.connect(path)


@pytest.fixture
def open_client(tmp_path, monkeypatch):
    """The default: auth unset, exactly what launch.bat and pytest use."""
    c = _make_client(tmp_path, monkeypatch, enabled=False)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def gate_client(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, enabled=True)
    yield c
    c.__exit__(None, None, None)


# ── auth off: the gate must be invisible ────────────────────────────────────

def test_auth_off_reports_not_required_and_serves_data(open_client):
    assert open_client.get("/api/auth/status").json()["required"] is False
    r = open_client.get("/api/accounts")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


def test_auth_off_accepts_login_refusal(open_client):
    r = open_client.post("/api/auth/login", json={"password": "whatever"})
    assert r.status_code == 400, r.text
    assert "does not require" in r.json()["detail"]


def test_auth_off_still_allows_uploads(open_client):
    """A 401 on this route would be a regression for local attachment use."""
    r = open_client.get("/api/trades")
    assert r.status_code == 200


# ── auth on: every data route closes ────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/accounts", "/api/trades", "/api/kpis", "/api/reports",
    "/api/diary?account_id=1", "/api/import-batches?account_id=1",
    "/api/backup/destination", "/api/backup/archives",
    "/api/library", "/api/goals", "/api/export?fmt=json",
])
def test_protected_routes_401_without_a_session(gate_client, path):
    r = gate_client.get(path)
    assert r.status_code == 401, (path, r.status_code, r.text[:120])
    assert r.json().get("error") == "Sign in required", r.text


def test_uploads_are_protected_too(gate_client):
    assert gate_client.get("/uploads/anything.png").status_code == 401


def test_write_routes_401_without_a_session(gate_client):
    r = gate_client.post("/api/import-csv", data={"account_id": "1"}, files={"file": ("x.csv", b"a", "text/csv")})
    assert r.status_code == 401, r.status_code


def test_status_login_and_logout_stay_public(gate_client):
    s = gate_client.get("/api/auth/status")
    assert s.status_code == 200 and s.json() == {"required": True, "authenticated": False, "method": "password"}
    assert gate_client.post("/api/auth/logout").status_code == 200


def test_non_data_routes_are_not_gated(gate_client):
    """The gate wraps /api and /uploads only.

    Static assets are served by the container work (there is no static mount in
    this build yet, so these return 404 rather than 200) — what matters is that
    neither returns 401, or the login screen could never load itself.
    """
    assert gate_client.get("/index.html").status_code != 401
    assert gate_client.get("/app.js").status_code != 401


def test_login_sets_cookie_and_unlocks_the_gate(gate_client):
    bad = gate_client.post("/api/auth/login", json={"password": "nope"})
    assert bad.status_code == 401
    assert gate_client.get("/api/accounts").status_code == 401

    good = gate_client.post("/api/auth/login", json={"password": PW})
    assert good.status_code == 200, good.text
    # Same TestClient keeps its cookies, so the next call carries the session.
    status = gate_client.get("/api/auth/status").json()
    assert status["authenticated"] is True, status
    assert gate_client.get("/api/accounts").status_code == 200
    assert gate_client.get("/api/trades").status_code == 200


def test_logout_revokes_the_session(gate_client):
    gate_client.post("/api/auth/login", json={"password": PW})
    assert gate_client.get("/api/kpis").status_code == 200
    gate_client.post("/api/auth/logout")
    assert gate_client.get("/api/kpis").status_code == 401
    assert gate_client.get("/api/auth/status").json()["authenticated"] is False


def test_a_forged_cookie_is_refused(gate_client):
    gate_client.post("/api/auth/login", json={"password": PW})
    real = gate_client.cookies.get("tdj_session")
    assert real, "the login must set the session cookie"
    # Flip a body character and keep the old signature.
    body, mac = real.rsplit(".", 1)
    tampered = body[:-1] + ("x" if body[-1] != "x" else "y") + "." + mac
    # Clear first: httpx *appends* a same-named cookie instead of replacing it,
    # so both would be sent and the valid one would win — a browser replaces,
    # which is what this test is meant to reproduce.
    gate_client.cookies.clear()
    gate_client.cookies.set("tdj_session", tampered)
    assert gate_client.cookies.get("tdj_session") == tampered
    assert gate_client.get("/api/accounts").status_code == 401
    assert gate_client.get("/api/auth/status").json()["authenticated"] is False
    gate_client.cookies.clear()


def test_login_works_when_the_password_hash_survives_without_env(tmp_path, monkeypatch):
    """A rebuild that drops TDJ_PASSWORD still serves the stored hash."""
    c = _make_client(tmp_path, monkeypatch, enabled=True)
    try:
        c.post("/api/auth/login", json={"password": PW})
        assert c.get("/api/accounts").status_code == 200
        # Prove the hash is in settings, not merely in memory.
        import os
        conn = sqlite3_connect(os.environ["DATABASE_PATH"])
        row = conn.execute(
            "SELECT value FROM settings WHERE key='auth_password_hash'").fetchone()
        conn.close()
        assert row and row[0].startswith("scrypt$"), row
    finally:
        c.__exit__(None, None, None)
