"""Authentication: opt-in request gate, scrypt passwords, signed cookie.

    cd backend && python -m pytest tests/test_auth.py -q

The principle: when `TDJ_AUTH` is unset, this module is deliberately a no-op —
that is what lets `launch.bat`, pytest, and a localhost dev server behave exactly
as before. When required, absent/bad/malformed/expired session is never treated
as success. A signed HMAC cookie is what crosses requests; the password never
leaves the login request and is stored only as a salted scrypt hash in settings.
"""
import hashlib
import hmac
import json
import time

import auth


def test_auth_is_off_by_default_and_requires_opt_in(monkeypatch):
    monkeypatch.delenv("TDJ_AUTH", raising=False)
    assert auth.is_required() is False
    monkeypatch.setenv("TDJ_AUTH", "required")
    assert auth.is_required() is True
    monkeypatch.setenv("TDJ_AUTH", "REQUIRED")
    assert auth.is_required() is True


def test_missing_password_raises_only_when_auth_is_required(monkeypatch):
    monkeypatch.delenv("TDJ_AUTH", raising=False)
    monkeypatch.delenv("TDJ_PASSWORD", raising=False)
    auth.require_configured()
    monkeypatch.setenv("TDJ_AUTH", "required")
    try:
        auth.require_configured()
    except RuntimeError as e:
        assert "TDJ_PASSWORD" in str(e)
    else:
        assert False, "must fail closed at startup"


def test_scrypt_hash_is_salted_and_verifies():
    a = auth.hash_password("secret")
    b = auth.hash_password("secret")
    assert a != b, "each hash gets a fresh random salt"
    assert auth.verify_password("secret", a)
    assert not auth.verify_password("wrong", a)
    assert not auth.verify_password("secret", "plaintext")
    assert not auth.verify_password("secret", "scrypt$garbage")


def test_verify_password_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    hashed = auth.hash_password("pw")
    assert auth.verify_password("pw", hashed)
    assert calls, "the actual digest comparison goes through compare_digest"


def test_signed_session_round_trip_and_expiry():
    secret = b"x" * 32
    token = auth.make_session(secret, now=1000)
    parsed = auth.verify(token, secret, now=1001)
    assert parsed and parsed["iat"] == 1000 and parsed["exp"] == 1000 + auth.SESSION_TTL
    assert auth.verify(token, secret, now=1000 + auth.SESSION_TTL + 1) is None


def test_tampered_payload_or_signature_is_rejected():
    secret = b"x" * 32
    token = auth.make_session(secret, now=1000)
    body, mac = token.rsplit(".", 1)
    assert auth.verify(body + "x." + mac, secret, now=1001) is None
    assert auth.verify(body + "." + ("0" if mac[0] != "0" else "1") + mac[1:], secret, now=1001) is None


def test_unknown_or_non_numeric_expiry_is_refused():
    secret = b"x" * 32
    for payload in ({}, {"exp": "far future"}, {"exp": None}, []):
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
        assert auth.verify(f"{body}.{sig}", secret, now=0) is None


def test_cookie_signature_does_not_verify_under_another_secret():
    token = auth.make_session(b"x" * 32, now=1000)
    assert auth.verify(token, b"y" * 32, now=1001) is None


def test_session_secret_is_created_once_and_persists(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "auth.db")
    db.execute("CREATE TABLE settings (account_id INTEGER, key TEXT, value TEXT, UNIQUE(account_id,key))")
    db.commit()
    first = auth.store_secret(db)
    second = auth.store_secret(db)
    assert len(first) == 32
    assert second == first
    count = db.execute("SELECT COUNT(*) FROM settings WHERE key=?", (auth.SECRET_KEY_NAME,)).fetchone()[0]
    assert count == 1
    db.close()


def test_password_hash_is_persisted_and_env_password_can_be_removed(tmp_path, monkeypatch):
    import sqlite3
    db = sqlite3.connect(tmp_path / "pw.db")
    db.execute("CREATE TABLE settings (account_id INTEGER, key TEXT, value TEXT, UNIQUE(account_id,key))")
    db.commit()
    monkeypatch.setenv("TDJ_AUTH", "required")
    monkeypatch.setenv("TDJ_PASSWORD", "hunter2")
    auth.require_configured(db)
    encoded = auth.get_or_create_password_hash(db)
    assert auth.verify_password("hunter2", encoded)
    monkeypatch.delenv("TDJ_PASSWORD")
    auth.require_configured(db)                    # existing install still boots
    assert auth.get_or_create_password_hash(db) == encoded
    db.close()


def test_persisted_hash_wins_over_a_changed_env_password(tmp_path, monkeypatch):
    import sqlite3
    db = sqlite3.connect(tmp_path / "rotate.db")
    db.execute("CREATE TABLE settings (account_id INTEGER, key TEXT, value TEXT, UNIQUE(account_id,key))")
    db.commit()
    monkeypatch.setenv("TDJ_AUTH", "required")
    monkeypatch.setenv("TDJ_PASSWORD", "first")
    stored = auth.get_or_create_password_hash(db)
    monkeypatch.setenv("TDJ_PASSWORD", "second")
    assert auth.get_or_create_password_hash(db) == stored
    assert auth.verify_password("first", stored)
    assert not auth.verify_password("second", stored)
    db.close()
