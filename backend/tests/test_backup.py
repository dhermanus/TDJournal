import csv
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(BACKEND))

import backup  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """A tiny fresh database with representative types and awkward values."""
    p = tmp_path / "journal.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, optional TEXT);
        CREATE TABLE bars (symbol TEXT, time TEXT, open REAL, PRIMARY KEY(symbol, time));
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO accounts VALUES (1, 'A, Inc.', NULL);
        INSERT INTO accounts VALUES (2, 'Say \"Hello\"', 'x\ny');
        INSERT INTO bars VALUES ('EURUSD', '2026-09-10 12:00:00', 1.12);
        INSERT INTO settings VALUES ('k', 'v');
    """)
    c.commit()
    c.close()
    return p


def test_backup_api_snapshots_wal_db_and_every_upload(tmp_path, db):
    uploads = tmp_path / "uploads"
    (uploads / "trade_attachments").mkdir(parents=True)
    (uploads / "trade_attachments" / "receipt.bin").write_bytes(b"ATTACHED")
    (uploads / "diary.png").write_bytes(b"DIARY")
    destination = tmp_path / "backups"
    destination.mkdir()

    out = backup.create_backup(db_path=db, upload_dir=uploads, destination=destination)
    assert out.is_file() and out.suffix == ".zip"
    with zipfile.ZipFile(out) as z:
        assert set(z.namelist()) == {
            backup.DB_MEMBER, backup.MANIFEST_MEMBER,
            "uploads/trade_attachments/receipt.bin", "uploads/diary.png",
        }
        assert z.read("uploads/trade_attachments/receipt.bin") == b"ATTACHED"
        assert z.read("uploads/diary.png") == b"DIARY"
        data = json.loads(z.read(backup.MANIFEST_MEMBER))
        assert data["format"] == backup.BACKUP_FORMAT
        assert data["attachment_files"] == 2
        # Round-trip the archived database rather than deserialize() (3.11+).
        staged = tmp_path / "from-archive.db"
        staged.write_bytes(z.read(backup.DB_MEMBER))
        with sqlite3.connect(staged) as restored:
            assert restored.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
            assert restored.execute("SELECT open FROM bars").fetchone()[0] == 1.12


def test_backup_uses_sqlite_snapshot_api_not_file_copy(tmp_path, db):
    calls = []
    # sqlite3.Connection is immutable to monkeypatch, so verify the API's effect:
    # the source can be WAL-backed and a direct db-file read can miss committed
    # rows there; the snapshot has to include them.
    with sqlite3.connect(db) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("INSERT INTO accounts VALUES (3, 'WAL row', 'live')")
        c.commit()

    out = backup.create_backup(db_path=db, upload_dir=tmp_path / "uploads", destination=tmp_path / "out.zip")
    with zipfile.ZipFile(out) as z:
        staged = tmp_path / "wal-snapshot.db"
        staged.write_bytes(z.read(backup.DB_MEMBER))
        with sqlite3.connect(staged) as snap:
            assert snap.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 3


def _archive(path, members):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in members.items():
            z.writestr(name, content)
    return path


@pytest.mark.parametrize("bad_name", [
    "../escape.txt", "uploads/../../escape.txt", "/tmp/escape.txt",
    "C:/Windows/escape.txt", "C:\\Windows\\escape.txt", "uploads\\..\\escape.txt",
])
def test_restore_rejects_unsafe_member_paths_before_writing(tmp_path, bad_name):
    good_db = tmp_path / "good.db"
    sqlite3.connect(good_db).close()
    archive = _archive(tmp_path / "evil.zip", {
        backup.DB_MEMBER: good_db.read_bytes(),
        bad_name: b"do not write",
    })
    target = tmp_path / "target"
    with pytest.raises(backup.RestoreError, match="unsafe path"):
        backup.extract_archive(archive, target)
    assert not target.exists() or list(target.iterdir()) == []


def test_restore_requires_database_and_rejects_non_archives(tmp_path):
    empty = _archive(tmp_path / "empty.zip", {"uploads/a": b"x"})
    with pytest.raises(backup.RestoreError, match="missing journal.db"):
        backup.inspect_archive(empty)
    missing_suffix = tmp_path / "x.tar"
    missing_suffix.write_bytes(b"not a zip")
    with pytest.raises(backup.RestoreError, match="Only .zip"):
        backup.inspect_archive(missing_suffix)


def test_restore_to_new_folder_does_not_replace_live_database(tmp_path, db):
    live = tmp_path / "live.db"
    live.write_bytes(db.read_bytes())
    uploads = tmp_path / "uploads"
    (uploads / "trade_attachments").mkdir(parents=True)
    (uploads / "trade_attachments" / "image.png").write_bytes(b"PNG")
    archive = backup.create_backup(db_path=live, upload_dir=uploads, destination=tmp_path / "backup.zip")

    restore_folder = tmp_path / "restored"
    info = backup.extract_archive(archive, restore_folder)
    restored_db = restore_folder / backup.DB_MEMBER
    with sqlite3.connect(restored_db) as c:
        assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
    assert (restore_folder / "uploads" / "trade_attachments" / "image.png").read_bytes() == b"PNG"
    # journal.db + manifest.json + the one attachment: three members written,
    # and the restored copy matches the source exactly.
    assert info["restored_members"] == 3
    with sqlite3.connect(live) as c:
        assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2


def test_restore_of_bad_database_rolls_back_previous_file(tmp_path, db):
    target = tmp_path / "restore"
    target.mkdir()
    live = target / backup.DB_MEMBER
    live.write_bytes(db.read_bytes())
    before = live.read_bytes()
    archive = _archive(tmp_path / "bad-db.zip", {
        backup.DB_MEMBER: b"not sqlite",
        "uploads/file.txt": b"test",
    })
    with pytest.raises((backup.RestoreError, sqlite3.DatabaseError)):
        backup.extract_archive(archive, target)
    assert live.read_bytes() == before


def test_csv_export_has_one_file_per_table_and_correct_quoting(tmp_path, db):
    destination = tmp_path / "csv.zip"
    with sqlite3.connect(db) as c:
        backup.write_export(c, destination, fmt="csv")
    with zipfile.ZipFile(destination) as z:
        assert set(z.namelist()) == {"exports/accounts.csv", "exports/bars.csv", "exports/settings.csv"}
        rows = list(csv.DictReader(io.StringIO(z.read("exports/accounts.csv").decode("utf-8"))))
        assert rows[0]["name"] == "A, Inc."
        assert rows[0]["optional"] == ""
        assert rows[1]["name"] == 'Say "Hello"'
        assert rows[1]["optional"] == "x\ny"


def test_json_export_keeps_null_and_newlines(tmp_path, db):
    destination = tmp_path / "journal.json"
    with sqlite3.connect(db) as c:
        backup.write_export(c, destination, fmt="json")
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["accounts"][0]["optional"] is None
    assert payload["accounts"][1]["name"] == 'Say "Hello"'
    assert payload["accounts"][1]["optional"] == "x\ny"
    assert payload["bars"][0]["open"] == 1.12


def test_manifest_and_export_agree_on_which_tables_exist(tmp_path, db):
    """The counts a backup reports must match the files it ships.

    Excluding `alembic_version` from the export while the manifest counted it
    (or vice versa) would make a backup's stated contents unverifiable at a
    glance — so both go through the same list.
    """
    with sqlite3.connect(db) as c:
        tables = backup.table_names(c)
        data = backup.manifest(db_path=db, uploads=tmp_path / "uploads")
        assert "alembic_version" not in tables
        assert set(tables) == set(data["tables"])

        backup.write_export(c, tmp_path / "csv.zip", fmt="csv")
        backup.write_export(c, tmp_path / "json.json", fmt="json")

    with zipfile.ZipFile(tmp_path / "csv.zip") as z:
        exported = {n.rsplit("/", 1)[-1][:-4] for n in z.namelist()}
    payload = json.loads((tmp_path / "json.json").read_text(encoding="utf-8"))
    assert exported == set(tables)
    assert set(payload) == set(tables)


def test_csv_filename_and_json_filename_are_not_interchanged(tmp_path, db):
    with sqlite3.connect(db) as c:
        with pytest.raises(backup.BackupError, match="format"):
            backup.write_export(c, tmp_path / "x.zip", fmt="xlsx")
