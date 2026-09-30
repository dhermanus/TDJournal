"""Backup/restore/export endpoints as the app actually serves them.

`test_backup.py` covers the storage layer; this covers the HTTP surface — which
is where a path typo, a wrong dependency, or a response shape the UI does not
expect would show up.
"""
import io
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """An app on a scratch database with its own uploads and backup folder."""
    db = tmp_path / "journal.db"
    uploads = tmp_path / "uploads"
    backups = tmp_path / "backups"
    uploads.mkdir()
    backups.mkdir()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    for name in ("database", "main"):
        sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    main.UPLOAD_DIR = str(uploads)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.db = str(db)
        c.uploads = str(uploads)
        # Kept as a Path: several assertions compare against it directly, and
        # set_folder() str()s it on the way to the API.
        c.backups = backups
        yield c


def set_folder(client, folder):
    return client.put("/api/backup/destination", json={"folder": str(folder)})


def test_destination_starts_empty_and_is_remembered(client):
    first = client.get("/api/backup/destination")
    assert first.status_code == 200
    assert first.json() == {"folder": "", "set": False, "exists": False}

    ok = set_folder(client, client.backups)
    assert ok.status_code == 200
    assert ok.json()["set"] is True
    assert ok.json()["exists"] is True
    # Remembered across requests, not just echoed back.
    assert client.get("/api/backup/destination").json()["folder"] == str(client.backups)


def test_a_missing_or_non_writable_folder_is_refused_immediately(client):
    missing = set_folder(client, client.backups / "never-created")
    assert missing.status_code == 400
    assert "does not exist" in missing.json()["error"]
    # Not saved, so a later backup cannot try to use it.
    assert client.get("/api/backup/destination").json()["set"] is False

    not_a_folder = client.backups / "a-file.zip"
    not_a_folder.write_bytes(b"x")
    assert set_folder(client, not_a_folder).status_code == 400

    assert set_folder(client, "").status_code == 400


def test_backup_is_refused_before_a_folder_is_chosen(client):
    """No silent default folder: an archive has to land somewhere chosen."""
    r = client.post("/api/backup")
    assert r.status_code == 400
    assert "Choose a folder" in r.json()["error"]
    assert list(Path(client.backups).glob("*.zip")) == []


def test_backup_endpoint_writes_db_and_attachments_to_the_chosen_folder(client):
    client.post("/api/accounts", json={"name": "P", "type": "day_trading"})
    t = client.post("/api/trades", json={
        "account_id": 1, "date": "2026-09-10", "ticker": "TSLA",
        "instrument_type": "STOCK", "side": "LONG",
        "entry_price": 366.09, "quantity": 100, "time": "09:54:10",
    })
    group = t.json()["trade_group"]
    client.post(f"/api/trades/{group}/attachments",
                files={"file": ("chart.png", b"\x89PNG-data", "image/png")})
    set_folder(client, client.backups)

    r = client.post("/api/backup")
    assert r.status_code == 200, r.text
    archive = Path(r.json()["created"])
    assert archive.is_file() and archive.parent == client.backups

    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        assert "journal.db" in names
        assert any(n.startswith("uploads/") and n.endswith(".png") for n in names)
        assert z.read([n for n in names if n.startswith("uploads/") and n.endswith(".png")][0]) == b"\x89PNG-data"

    listed = client.get("/api/backup/archives").json()["archives"]
    assert [a["name"] for a in listed] == [archive.name]
    assert listed[0]["error"] is None
    assert listed[0]["tables"]["trades"] == 1
    assert listed[0]["attachment_files"] == 1


def test_restore_extracts_into_a_new_folder_and_leaves_the_journal_alone(client):
    client.post("/api/accounts", json={"name": "P", "type": "day_trading"})
    set_folder(client, client.backups)
    archive = Path(client.post("/api/backup").json()["created"])

    live_before = Path(client.db).read_bytes()
    r = client.post("/api/backup/restore", json={"name": archive.name})
    assert r.status_code == 200, r.text
    folder = Path(r.json()["folder"])
    assert folder.parent == client.backups and folder != client.backups
    assert (folder / "journal.db").is_file()
    # The whole point of restoring to a new folder: the journal is untouched.
    assert Path(client.db).read_bytes() == live_before

    with sqlite3.connect(folder / "journal.db") as c:
        assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_restore_refuses_a_name_that_is_not_plain(client):
    set_folder(client, client.backups)
    for bad in ("../secrets.zip", "/etc/passwd.zip", "sub/dir.zip"):
        r = client.post("/api/backup/restore", json={"name": bad})
        assert r.status_code == 400, f"{bad}: {r.text}"


def test_restore_refuses_a_non_zip_and_an_unlisted_name(client):
    set_folder(client, client.backups)
    (client.backups / "notabackup.zip").write_bytes(b"nonsense")
    assert client.post("/api/backup/restore", json={"name": "notabackup.zip"}).status_code == 400
    assert client.post("/api/backup/restore", json={"name": "never-created.zip"}).status_code == 400


def test_export_streams_every_table_as_csv_and_json(client):
    client.post("/api/accounts", json={"name": "P", "type": "day_trading"})
    client.post("/api/trades", json={
        "account_id": 1, "date": "2026-09-10", "ticker": "TSLA",
        "instrument_type": "STOCK", "side": "LONG",
        "entry_price": 366.09, "quantity": 100, "time": "09:54:10",
    })

    as_json = client.get("/api/export", params={"fmt": "json"})
    assert as_json.status_code == 200
    assert "json" in as_json.headers.get("content-type", "")
    payload = __import__("json").loads(as_json.content)
    assert payload["accounts"][0]["name"] == "P"
    assert len(payload["trades"]) == 1

    as_csv = client.get("/api/export", params={"fmt": "csv"})
    assert as_csv.status_code == 200
    with zipfile.ZipFile(io.BytesIO(as_csv.content)) as z:
        names = z.namelist()
        assert "exports/trades.csv" in names
        header = z.read("exports/trades.csv").decode().splitlines()[0]
        assert "trade_group" in header

    assert client.get("/api/export", params={"fmt": "xlsx"}).status_code == 400
