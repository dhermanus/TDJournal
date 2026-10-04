"""Local-only authentication for the API.

    cd backend && python -m pytest tests/test_auth.py -q

Two rules govern this module, and both come from the design the user approved:

1. **Auth is opt-in.** Nothing here runs unless `TDJ_AUTH=required` is set, so
   `launch.bat`, the venv and every existing test behave exactly as before —
   the whole point of "local development is preserved".
2. **Failing closed.** `TDJ_AUTH=required` with no usable password is a startup
   error, not a server that answers "no password, so open". A deployment whose
   auth silently does nothing is worse than one that refuses to boot.

No third-party dependency: `hashlib.scrypt` and `hmac` are standard library, so
the container adds nothing to requirements.txt and Windows keeps working.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

# 32 MiB of memory, 8 rounds, one thread: slow enough to make an offline attack
# on a leaked hash expensive, fast enough for a login on a Raspberry Pi running
# the same image. Not tunable per-call — a password check must not vary by input.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
_SCRYPT_DKLEN = 64
_SALT_BYTES = 16
# OpenSSL caps scrypt at 32 MiB by default and treats the cap as *exclusive*,
# so the parameters above (128 * N * r = 32 MiB exactly) are rejected with
# "memory limit exceeded" unless maxmem is raised here.
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 4
SESSION_TTL = 12 * 60 * 60       # 12 hours: long enough for a session, short
COOKIE_NAME = "tdj_session"       # to outlive a browser staying open all week
SECRET_KEY_NAME = "auth_secret"
PASSWORD_HASH_KEY = "auth_password_hash"


def is_required() -> bool:
    """Whether the API must gate requests. Default is off, by design."""
    return os.getenv("TDJ_AUTH", "").strip().lower() == "required"


def configured_password() -> str | None:
    return os.getenv("TDJ_PASSWORD") or None


def hash_password(password: str, salt: bytes | None = None) -> str:
    """`scrypt$<n>$<r>$<p>$<salt-hex>$<dk-hex>` — self-describing, so cost
    parameters can change later without invalidating old hashes."""
    if salt is None:
        salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                        r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
                        maxmem=_SCRYPT_MAXMEM)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, n, r, p, salt_hex, dk_hex = stored.split("$")
        if _algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"),
                            salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(dk_hex)),
                            maxmem=_SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def sign(payload: dict, secret: bytes) -> str:
    """HMAC-signed compact token: `<body>.<mac>`.

    Signature first-class: a tampered body or a forged expiry both fail the
    compare_digest in verify().
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    mac = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def verify(token: str, secret: bytes, now: float | None = None) -> dict | None:
    if not token or "." not in token:
        return None
    body, _, mac = token.rpartition(".")
    expected = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    # A signed body can still be a JSON array or scalar; only an object carries
    # exp/iat. Guard before .get() or a forged non-dict token crashes the gate.
    if not isinstance(payload, dict):
        return None
    if now is None:
        now = time.time()
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < now:
        return None
    return payload


def make_session(secret: bytes, now: float | None = None) -> str:
    if now is None:
        now = time.time()
    return sign({"exp": int(now) + SESSION_TTL, "iat": int(now)}, secret)


def require_configured(conn: sqlite3.Connection | None = None) -> None:
    """Raise at startup if auth is on but unusable.

    Called from the lifespan, before any request, so a container that starts with
    `TDJ_AUTH=required` and no way to verify a password fails visibly instead of
    serving an API that claims to be protected.

    Accepts the persisted hash as an alternative to TDJ_PASSWORD, so a volume-
    mounted journal keeps working across container rebuilds once the password
    has been established once.
    """
    if not is_required():
        return
    if configured_password():
        return
    if conn is not None:
        try:
            if conn.execute(
                "SELECT value FROM settings WHERE account_id=0 "
                "AND key='auth_password_hash'"
            ).fetchone():
                return
        except sqlite3.Error:
            pass
    raise RuntimeError(
        "TDJ_AUTH=required but neither TDJ_PASSWORD nor a stored password hash "
        "is available. Set TDJ_PASSWORD in backend/.env or unset TDJ_AUTH."
    )


def store_secret(conn: sqlite3.Connection) -> bytes:
    """Read, or create, the per-installation signing key.

    Persisted in the journal (settings, account_id=0 — the same slot the backup
    folder uses) rather than derived from the password, so rotating the password
    does not log every browser out and a backup restores the same key.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE account_id = 0 AND key = ?", (SECRET_KEY_NAME,)
    ).fetchone()
    if row and row[0]:
        return bytes.fromhex(row[0])
    secret = secrets.token_bytes(32)
    conn.execute(
        """INSERT INTO settings (account_id, key, value) VALUES (0, ?, ?)
           ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value""",
        (SECRET_KEY_NAME, secret.hex()),
    )
    conn.commit()
    return secret


def get_or_create_password_hash(conn: sqlite3.Connection) -> str:
    """Persist the environment password's derived hash once, then verify against
    it on login without scrypt-running on every request.

    First boot with env config hashes the password into settings. Subsequent
    boots prefer the stored hash, so a container env edit cannot silently change
    the password out from under users — rotation is an explicit Settings action.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE account_id=0 AND key='auth_password_hash'"
    ).fetchone()
    if row and row[0]:
        return row[0]
    password = configured_password()
    if not password:
        raise RuntimeError("TDJ_AUTH=required but TDJ_PASSWORD is not set.")
    stored = hash_password(password)
    conn.execute(
        """INSERT INTO settings (account_id, key, value) VALUES (0, 'auth_password_hash', ?)
           ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value""",
        (stored,),
    )
    conn.commit()
    return stored
