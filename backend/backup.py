"""Local backup, restore, and export for TDJournal.

Three jobs, all local to this machine (network destinations stay deferred with
the security baseline):

  * `create_backup` — a consistent archive of a running journal.
  * `extract_archive` — restore it, refusing any member that would land
    outside the destination.
  * `write_export` — CSV/JSON, so the data is not locked into SQLite.

Why the SQLite backup API instead of copying the file: the journal runs in WAL
mode, so committed rows live in the `-wal` sidecar and copying only
`trading_journal.db` silently loses them, while copying a database and its
sidecars concurrently can produce a torn copy. `Connection.backup()` reads
through a transaction, which is the correct way to snapshot a live SQLite
database.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

# The archive layout. These names are the complete closed set of members a
# restore accepts, which is what makes validation enforceable: anything else in
# an archive is a refusal rather than an unknown to be trusted later.
DB_MEMBER = "journal.db"
MANIFEST_MEMBER = "manifest.json"
UPLOADS_PREFIX = "uploads/"
EXPORTS_PREFIX = "exports/"
MANIFEST_VERSION = 1
BACKUP_FORMAT = "tdjournal-backup"


class BackupError(ValueError):
    """A refused backup/restore/export. Maps to HTTP 400 via the ValueError handler."""


class RestoreError(BackupError):
    """A refused restore, kept distinct so creation and extraction can differ."""


# ── Creating ──────────────────────────────────────────────────────────────────


def default_backup_name(now: datetime | None = None) -> str:
    """A timestamped archive name, unique within this folder.

    Microseconds plus `unique_path()` rather than seconds alone: two backups in
    the same second would otherwise land on the same path and the earlier one
    would be overwritten silently — which for an automatic pre-migration
    snapshot is worse than a duplicate name in a list, because nothing reports it.
    """
    return (now or datetime.now()).strftime("tdjournal-backup-%Y%m%d-%H%M%S-%f.zip")


def unique_path(folder: Path, name: str) -> Path:
    """`name` in `folder`, suffixing -2, -3... until it is unused."""
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while True:
        candidate = folder / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def manifest(*, db_path: str | Path, uploads: Path) -> dict:
    """What the archive claims to contain, re-checked after a restore."""
    counts: dict[str, int] = {}
    revision = None
    with sqlite3.connect(db_path) as conn:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name"
        ):
            counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        # The version table does not exist before Alembic is adopted, and
        # `has_user_tables` databases may predate it entirely.
        try:
            row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            revision = row[0] if row else None
        except sqlite3.Error:
            revision = None

    return {
        "format": BACKUP_FORMAT,
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "alembic_revision": revision,
        "tables": counts,
        "attachment_files": sum(1 for p in Path(uploads).rglob("*") if p.is_file()),
    }


def create_backup(*, db_path: str | Path, upload_dir: str | Path,
                  destination: str | Path) -> Path:
    """Write a backup archive to `destination`, a .zip path or a folder.

    The database is snapshotted through SQLite's backup API into a temporary
    file *before* archiving, so the bytes stored are a coherent transaction even
    if a write lands mid-run; the live database file is never opened by the
    archiver directly.
    """
    dest = Path(destination).expanduser()
    if dest.exists() and dest.is_dir():
        dest = unique_path(dest, default_backup_name())
    if dest.suffix.lower() != ".zip":
        raise BackupError("Backup destination must be a .zip file or a folder.")
    if dest.exists() and not dest.is_file():
        raise BackupError(f"Backup destination is not a file: {dest}")

    uploads = Path(upload_dir)
    uploads.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Staged in the system temp dir, not the destination: a crash mid-backup
    # must not leave a half-written `.snapshot` file in a folder the user chose
    # for their backups.
    with tempfile.TemporaryDirectory() as scratch:
        staged = Path(scratch) / DB_MEMBER
        with sqlite3.connect(str(db_path)) as source:
            with sqlite3.connect(str(staged)) as snapshot:
                source.backup(snapshot)

        data = manifest(db_path=db_path, uploads=uploads)
        partial = dest.parent / f".{dest.name}.partial"
        partial.unlink(missing_ok=True)
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.write(staged, DB_MEMBER)
                bundle.writestr(MANIFEST_MEMBER, json.dumps(data, indent=2))
                for path in sorted(uploads.rglob("*")):
                    if path.is_file():
                        arcname = f"{UPLOADS_PREFIX}{path.relative_to(uploads).as_posix()}"
                        bundle.write(path, arcname)
            # Rename only once the archive is complete, so a reader never sees
            # a truncated .zip at the final name.
            os.replace(partial, dest)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            dest.unlink(missing_ok=True)
            raise BackupError(f"Backup could not be written: {exc}") from exc

    if not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise BackupError("Backup could not be written. Check the destination folder.")
    return dest


# ── Validating and extracting ─────────────────────────────────────────────────


def member_target(name: str, destination: Path) -> Path | None:
    """Resolve an archive member inside `destination`, or None if it escapes.

    Checked from the *name* before any resolution of the destination itself:
    absolute paths, `..` segments, backslashes (separators on Windows) and
    drive/UNC prefixes are refused outright rather than normalized away,
    because `PurePosixPath` would treat `C:/Windows` as relative and quietly
    extract it. `resolve()` afterwards catches the remaining case — a member
    that would write through a symlink created by an earlier entry.
    """
    if not name:
        return None
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        return None
    if "\\" in name:
        return None

    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts:
        return None

    resolved = destination.joinpath(*member.parts)
    root = destination.resolve()
    target = resolved.resolve()
    if target != root and root not in target.parents:
        return None
    if resolved.exists() and resolved.is_symlink():
        return None
    return resolved


def inspect_archive(archive: str | Path) -> dict:
    """Validate a backup's structure and every member's path, without extracting."""
    path = Path(archive)
    if not path.is_file():
        raise RestoreError(f"Backup not found: {path}")
    if path.suffix.lower() != ".zip":
        raise RestoreError("Only .zip backups can be restored.")

    try:
        with zipfile.ZipFile(path) as bundle:
            bad = bundle.testzip()
            if bad is not None:
                raise RestoreError(f"Backup is corrupt: {bad} failed its CRC check.")
            entries = bundle.infolist()
    except zipfile.BadZipFile as exc:
        raise RestoreError(f"That file is not a valid backup archive: {exc}") from exc

    files = [e.filename for e in entries if not e.is_dir() and e.filename]
    if DB_MEMBER not in files:
        raise RestoreError(f"Backup is missing {DB_MEMBER}; it is not a TDJournal backup.")

    # Every path check happens here, against an arbitrary stand-in root, so the
    # archive is refused as a whole before a single byte is written anywhere.
    for filename in files:
        if member_target(filename, Path(os.sep, "stand-in")) is None:
            raise RestoreError(f"Backup contains an unsafe path: {filename}")

    stray = [
        n for n in files
        if n not in (DB_MEMBER, MANIFEST_MEMBER)
        and not n.startswith((UPLOADS_PREFIX, EXPORTS_PREFIX))
    ]
    if stray:
        raise RestoreError(
            "Backup contains unexpected members: " + ", ".join(sorted(stray)[:5])
        )

    manifest_data: dict = {}
    try:
        with zipfile.ZipFile(path) as bundle:
            if MANIFEST_MEMBER in files:
                manifest_data = json.loads(bundle.read(MANIFEST_MEMBER))
    except (json.JSONDecodeError, zipfile.BadZipFile):
        manifest_data = {}

    return {"manifest": manifest_data, "members": files, "member_count": len(files)}


def extract_archive(archive: str | Path, destination: str | Path) -> dict:
    """Extract a validated backup into `destination`.

    Every member is resolved against the destination and refused if it lands
    outside before anything is written. The existing database is copied aside
    first and restored if extraction fails part-way — a failed restore must not
    leave the journal without a database — and the incoming database passes an
    integrity check before it is swapped in.
    """
    target = Path(destination).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()

    inspected = inspect_archive(archive)

    # Resolve the whole extraction plan first: no partial write on a bad archive.
    plan: list[tuple[zipfile.ZipInfo, Path]] = []
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir() or not member.filename:
                continue
            resolved = member_target(member.filename, target)
            if resolved is None:
                raise RestoreError(f"Backup contains an unsafe path: {member.filename}")
            plan.append((member, resolved))

    rollback: Path | None = None
    existing = target / DB_MEMBER
    if existing.is_file():
        rollback = target / f"{DB_MEMBER}.pre-restore"
        shutil.copy2(existing, rollback)

    written: list[str] = []
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member, resolved in plan:
                resolved.parent.mkdir(parents=True, exist_ok=True)
                if member.filename == DB_MEMBER:
                    # Stage beside the target and swap atomically, so a
                    # truncated read can never replace a usable database.
                    incoming = target / f"{DB_MEMBER}.incoming"
                    incoming.unlink(missing_ok=True)
                    with bundle.open(member) as source, open(incoming, "wb") as out:
                        shutil.copyfileobj(source, out)
                    with sqlite3.connect(str(incoming)) as check:
                        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise RestoreError("Restored database failed its integrity check.")
                    os.replace(incoming, existing)
                else:
                    with bundle.open(member) as source, open(resolved, "wb") as out:
                        shutil.copyfileobj(source, out)
                written.append(member.filename)
    except Exception:
        if rollback is not None:
            shutil.copy2(rollback, existing)
        raise
    finally:
        if rollback is not None:
            rollback.unlink(missing_ok=True)
        (target / f"{DB_MEMBER}.incoming").unlink(missing_ok=True)

    return {
        "restored_members": len(written),
        "manifest": inspected["manifest"],
        "tables": inspected["manifest"].get("tables", {}),
        "destination": str(root),
    }


# ── Export ────────────────────────────────────────────────────────────────────


def table_names(conn: sqlite3.Connection) -> list[str]:
    """The journal's data tables, in a stable order.

    `alembic_version` is excluded deliberately: it is migration metadata, not
    journal data, and both the manifest and the export must agree on what a
    backup contains — reporting 12 tables in the manifest while the CSV zip
    carried a thirteenth file would make the counts unverifiable at a glance.
    """
    return [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name"
        )
    ]


def csv_table(conn: sqlite3.Connection, table: str) -> str:
    """One table as CSV text with a header row. None becomes an empty field."""
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([d[0] for d in cursor.description])
    for row in cursor:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue()


def write_export(conn: sqlite3.Connection, destination: Path, *, fmt: str) -> Path:
    """Write every table to `destination` as 'csv' (a zip) or 'json'.

    JSON is one object keyed by table name — the journal's data in a form
    anything else can read, which is the point of exporting at all. CSV is a
    zip with one file per table, so a spreadsheet can open the case you care
    about instead of the whole journal.
    """
    if fmt not in ("csv", "json"):
        raise BackupError("Export format must be 'csv' or 'json'.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tables = table_names(conn)

    if fmt == "json":
        # Built from cursor.description rather than dict(row): callers pass both
        # a plain sqlite3.Connection (tuples) and get_db()'s Row connection, and
        # the export must work with either instead of failing on the first row.
        payload: dict[str, list] = {}
        for table in tables:
            cursor = conn.execute(f'SELECT * FROM "{table}"')
            columns = [d[0] for d in cursor.description]
            payload[table] = [dict(zip(columns, row)) for row in cursor]
        destination.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return destination

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for table in tables:
            bundle.writestr(f"{EXPORTS_PREFIX}{table}.csv", csv_table(conn, table))
    return destination
