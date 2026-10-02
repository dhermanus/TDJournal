"""Diary uploads must never write outside UPLOAD_DIR.

    cd backend && python -m pytest tests/test_diary_upload_safety.py -q

`upload_diary` built its stored name as f"{date}_{account_id}_{file.filename}".
Both inputs came from the request, and `date` was the *first* component of the
path, so a form field containing "../../" placed the file outside the uploads
folder — proven by a probe that actually landed one. The extension allowlist
covered only the filename, and it happened to block the traversal suffixes; it
was never a substitute for validating a date or for containing the result.

The fix, and what these tests pin:

1. `date` is parsed as a date, so a path segment in it is rejected with 400.
   A browser sending the ISO string it already renders loses nothing.
2. The uploaded filename is stored as a sanitised stem plus the extension that
   was already checked, so no input can contribute a path separator.
3. The resolved path must be inside UPLOAD_DIR, matching what
   attachments.attachment_path() does for trade attachments.
4. The feature gate still comes first: a disabled toggle answers 403 for a bad
   date too, so "off" still means nothing was read, written or sent.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

DATE = "2026-07-01"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture
def app(tmp_path, monkeypatch):
    db = tmp_path / "journal.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    for name in ("database", "main", "ai_settings", "ai_analysis", "daily_summary"):
        if name in sys.modules:
            sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    # Both model entry points are stubbed: this file is about where bytes land
    # on disk, and no test here may reach the network.
    import sqlite3

    def _stub(*args, **kwargs):
        return {"diary_date": DATE, "overall_summary": "test",
                "trade_analyses": []}

    monkeypatch.setattr(main, "analyze_diary_text", _stub, raising=True)
    monkeypatch.setattr(main, "analyze_diary_entry", _stub, raising=True)

    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO accounts (id,name,type,color,broker) VALUES (1,'t','day_trading','#000','Thinkorswim')"
        )
        conn.commit()
        conn.close()
        c.upload_dir = upload_dir
        yield c


def _files(root: Path) -> set:
    return {p.resolve() for p in root.rglob("*") if p.is_file()}


def _created_outside(client, before: set) -> list:
    """Files the request added that are not inside the uploads folder.

    Diffed against a snapshot rather than filtered by name: the journal and the
    fixture's own rows are in the same temp tree, and a name filter would let a
    traversal named `journal.db` through.
    """
    uploads = client.upload_dir.resolve()
    return [p for p in (_files(client.upload_dir.parent) - before)
            if not str(p).startswith(str(uploads))]


def _post(client, date, filename, content=PNG, account_id="1"):
    return client.post("/api/upload-diary",
                       data={"date": date, "account_id": account_id},
                       files={"file": (filename, content, "image/png")})


def test_date_containing_path_segments_is_rejected(app):
    before = _files(app.upload_dir.parent)
    r = _post(app, "../../../escaped", "note.png")
    assert r.status_code == 400, r.text
    assert _created_outside(app, before) == [], "no file may land outside uploads"
    assert not list(app.upload_dir.iterdir()), "nothing should be written"


def test_date_must_be_a_real_date(app):
    # strptime rejects both a path and a calendar-impossible date, so the field
    # cannot be used to build a name from arbitrary text.
    before = _files(app.upload_dir.parent)
    # Path-like and calendar-impossible values reach the endpoint and are
    # refused there with 400; an empty form value never gets that far, because
    # `date: str = Form(...)` already rejects it as 422. Both stop the write.
    # `2026-7-1` is a valid date but not a stored one — entry_date is ISO, so
    # accepting it would file the upload where no trade can ever match it.
    for bad, status in (("not-a-date", 400), ("2026-02-30", 400),
                        ("2026-7-1", 400), ("2026-07-01/x", 400), ("", 422)):
        r = _post(app, bad, "note.png")
        assert r.status_code == status, f"{bad!r} -> {r.status_code} {r.text}"
    assert _created_outside(app, before) == []
    assert list(app.upload_dir.iterdir()) == []


def test_traversal_filename_stays_inside_uploads(app):
    before = _files(app.upload_dir.parent)
    r = _post(app, DATE, "../../../../../../escaped.png")
    assert r.status_code == 200, r.text
    assert _created_outside(app, before) == []
    files = list(app.upload_dir.iterdir())
    assert len(files) == 1
    # stored name keeps date_account_stem.ext and carries no separator
    name = files[0].name
    assert name.startswith(f"{DATE}_1_")
    assert "/" not in name and "\\" not in name
    assert name.endswith(".png")


def test_successful_upload_lands_directly_in_uploads(app):
    r = _post(app, DATE, "my notes.png")
    assert r.status_code == 200, r.text
    files = list(app.upload_dir.iterdir())
    assert len(files) == 1
    stored = files[0].resolve()
    assert stored.parent == app.upload_dir.resolve()
    # image_path is what the frontend builds its <img> src from
    row = app.get("/api/diary").json()
    assert row[0]["image_path"] == stored.name
    assert row[0]["image_path"].endswith(".png")


def test_bad_extension_is_refused_before_any_write(app):
    before = _files(app.upload_dir.parent)
    # The allowlist keys off the suffix, so a doubled one still passes and is
    # stored; an executable suffix does not, and refuses before any write.
    assert _post(app, DATE, "run.sh.exe.png").status_code == 200
    assert _post(app, DATE, "payload.exe").status_code == 400
    assert _created_outside(app, before) == []


def test_disabled_feature_still_wins_over_a_bad_date(app):
    """The gate is the first statement: off means nothing was read or sent."""
    app.put("/api/ai-settings", json={"features": {"diary": False}})
    before = _files(app.upload_dir.parent)
    r = _post(app, "../../../escaped", "note.png")
    assert r.status_code == 403, r.text
    assert "turned off" in r.json()["detail"]
    assert _created_outside(app, before) == []
    assert list(app.upload_dir.iterdir()) == []
