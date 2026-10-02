"""Recording what an import touched, and putting it back.

    cd backend && python -m pytest tests/test_import_undo.py -q

An import is several statements inside one commit, and one of its steps —
`_replace_regrouped_trades` — deletes rows outright. Undo therefore has to
restore a saved copy of the rows rather than infer what the journal looked like
before: the only reliable witness of "what was there" is a copy taken at the
time.

Which rows get copied comes from the preview's own diff (`main._classify_import`),
so the snapshot is exactly the set of rows the import will change and nothing
else. That matters twice over: a batch against a journal whose 1,346 trades are
all `source='mt5'` snapshots none of them (the parse never touches those rows),
and a 500-trade statement snapshots its own trades rather than the whole
journal.

Scope is deliberately journal data only — trades, analysis, tags and attachments,
per the decision recorded in this project's scope note. Diary entries and M1 bars
are separate workflows, not written by a CSV import, and are left alone.
"""
import json
import sqlite3


SNAPSHOT_TABLES = (
    # (key in the snapshot, table) — every one of them joins on trade_group.
    ("trades", "trades"),
    ("analysis", "trade_analysis"),
    ("tags", "trade_tags"),
    ("attachments", "trade_attachments"),
)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _rows_for(conn: sqlite3.Connection, table: str, groups: list[str]) -> list[dict]:
    """Every row of `table` keyed to one of `groups`, as plain dicts.

    Built from the table's own columns rather than a hand-written SELECT: a
    column added later (imported_at, batch_id) has to be part of a restore or
    the restored row would not be the row that was deleted.
    """
    if not groups:
        return []
    cols = _columns(conn, table)
    marks = ",".join("?" * len(groups))
    # trade_group is the join key in all four tables.
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM {table} WHERE trade_group IN ({marks})", groups
    ).fetchall()
    return [{c: r[i] for i, c in enumerate(cols)} for r in rows]


def capture(conn: sqlite3.Connection, account_id: int, groups: list[str],
            overrides: dict | None = None) -> dict:
    """The pre-import state of every row the import is about to touch.

    Built from the table's own columns rather than a hand-written SELECT: a
    column added later (imported_at, batch_id) has to be part of a restore or
    the restored row would not be the row that was deleted.

    `overrides` is an earlier snapshot of the same shape, and it wins for the
    groups it contains. That is the ordering problem this whole module exists to
    solve: the group list only appears *after* the parse, but the parse has
    already rewritten some rows by then — it merges fills into open option
    positions and commits — so by the time the groups are known, the row we want
    a picture of no longer exists in its original form. Whoever knows about
    those rows sooner passes their earlier picture in here.
    """
    groups = sorted(set(groups) | set((overrides or {}).get("groups") or []))
    snapshot = {"groups": groups}
    for key, table in SNAPSHOT_TABLES:
        cols = _columns(conn, table)
        if table == "trades":
            marks = ",".join("?" * len(groups))
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM trades "
                f"WHERE account_id = ? AND trade_group IN ({marks})",
                [account_id, *groups],
            ).fetchall() if groups else []
            snapshot[key] = [{c: r[i] for i, c in enumerate(cols)} for r in rows]
        else:
            # _rows_for already builds dicts — dict(row) by another name — so
            # these rows are assigned as-is rather than indexed by column number.
            snapshot[key] = _rows_for(conn, table, groups)

    if overrides:
        snapshot = merge(overrides, snapshot)
    return snapshot


def merge(earlier: dict, later: dict) -> dict:
    """Rows from `earlier` win for their groups; everything else comes from `later`."""
    earlier_groups = set(earlier.get("groups") or [])
    out = {"groups": sorted(earlier_groups | set(later.get("groups") or []))}
    for key, _ in SNAPSHOT_TABLES:
        old = {r.get("trade_group"): r for r in (earlier.get(key) or [])
               if r.get("trade_group") in earlier_groups}
        out[key] = list(old.values()) + [
            r for r in (later.get(key) or [])
            if r.get("trade_group") not in earlier_groups
        ]
    return out


def only(snapshot: dict, groups: list[str]) -> dict:
    """Drop everything the import did not actually touch, keeping the rows that are in `groups`."""
    keep = set(groups)
    out = {"groups": sorted(keep & set(snapshot.get("groups") or []))}
    for key, _ in SNAPSHOT_TABLES:
        out[key] = [r for r in (snapshot.get(key) or [])
                    if r.get("trade_group") in keep]
    return out


def open_option_groups(conn: sqlite3.Connection, account_id: int) -> list[str]:
    """Groups whose rows the *parse* can rewrite before anyone knows the file's group list.

    `build_trades_from_executions` merges fills into open option positions and
    commits, so a snapshot taken after the parse would hold the merged row
    instead of the original. Options only: that path is guarded on
    instrument_type, and stock positions are explicitly not auto-merged.
    """
    rows = conn.execute(
        "SELECT trade_group, executions FROM trades "
        "WHERE account_id = ? AND instrument_type = 'OPTION'",
        (account_id,),
    ).fetchall()
    out = []
    for group, raw in rows:
        try:
            fills = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        if sum(f.get("qty", 0) for f in fills if f.get("action") == "BOT") != \
           sum(f.get("qty", 0) for f in fills if f.get("action") == "SOLD"):
            out.append(group)
    return out


def _insert(conn: sqlite3.Connection, table: str, cols: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    marks = ",".join("?" * len(cols))
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
        [[r.get(c) for c in cols] for r in rows],
    )


def record(conn: sqlite3.Connection, account_id: int, *, filename: str, broker: str,
           snapshot: dict, imported: int, skipped: int, line_error_count: int,
           write_error_count: int, changed: bool) -> int | None:
    """Save a batch and return its id, or None when the import changed nothing.

    Decided by `changed` rather than by what is in the snapshot, because the two
    cases that matter both break the obvious test: a *created* trade has no
    pre-existing row to snapshot, and an absorbed option rewrites a row while the
    import reports `imported: 0`. A duplicate file, by contrast, comes back with
    no groups at all.
    """
    if not changed:
        return None
    if not (snapshot.get("groups") or []):
        return None

    cur = conn.execute(
        "INSERT INTO import_batches "
        "(account_id, filename, broker, snapshot, imported, skipped, "
        " line_error_count, write_error_count) VALUES (?,?,?,?,?,?,?,?)",
        (account_id, filename, broker, json.dumps(snapshot, default=str),
         imported, skipped, line_error_count, write_error_count),
    )
    conn.commit()
    return cur.lastrowid


def list_batches(conn: sqlite3.Connection, account_id: int, limit: int = 20) -> list[dict]:
    """Recent batches for the account, newest first, without their snapshots.

    The snapshot can be megabytes; the list exists to pick one to undo.
    """
    rows = conn.execute(
        "SELECT id, filename, broker, imported, skipped, line_error_count, "
        "       write_error_count, created_at, undone_at, "
        "       LENGTH(snapshot) AS snapshot_bytes, "
        "       (SELECT COUNT(*) FROM json_each(import_batches.snapshot, '$.groups')) AS groups "
        "FROM import_batches WHERE account_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()
    return [
        {"id": r[0], "filename": r[1], "broker": r[2], "imported": r[3],
         "skipped": r[4], "line_error_count": r[5], "write_error_count": r[6],
         "created_at": r[7], "undone_at": r[8],
         "groups": r[10] if r[10] is not None else 0,
         "undoable": r[8] is None}
        for r in rows
    ]


def undo(conn: sqlite3.Connection, batch_id: int) -> dict:
    """Restore the journal to its pre-import state. Idempotent by refusal.

    Two rules keep this from making things worse than not undoing:

    * only the newest not-yet-undone batch of an account can be undone — an
      earlier batch's snapshot predates the later import, so restoring it would
      silently discard the later one; and

    * rows the import *created* are deleted rather than restored, because
      restoring "nothing" is what they were before.

    Returns a summary; raises ValueError with a message the UI can show.
    """
    batch = conn.execute(
        "SELECT id, account_id, snapshot, undone_at FROM import_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise ValueError(f"Import batch {batch_id} not found.")
    account_id = batch[1]
    if batch[3]:
        raise ValueError("That import has already been undone.")

    newer = conn.execute(
        "SELECT id FROM import_batches "
        "WHERE account_id = ? AND id > ? AND undone_at IS NULL LIMIT 1",
        (account_id, batch_id),
    ).fetchone()
    if newer:
        raise ValueError(
            f"A later import (batch {newer[0]}) has been applied to this account. "
            "Undo that one first — this batch's snapshot predates it."
        )

    snapshot = json.loads(batch[2] or "{}")
    if not isinstance(snapshot, dict):
        raise ValueError("That batch has no usable snapshot.")
    groups: list[str] = snapshot.get("groups") or []

    try:
        # 1. Remove everything the import wrote, keyed to the groups it touched.
        # `trades` is scoped to the account: trade_group is only unique *per
        # account*, so a bare delete would take out an unrelated account's row
        # that happened to share a name. The other three tables have no account
        # column at all and are keyed on trade_group alone — the same choice
        # `_replace_regrouped_trades` makes when it moves them.
        for group in groups:
            conn.execute(
                "DELETE FROM trades WHERE trade_group = ? AND account_id = ?",
                (group, account_id))
            for key, table in SNAPSHOT_TABLES:
                if table == "trades":
                    continue
                conn.execute(f"DELETE FROM {table} WHERE trade_group = ?", (group,))

        # 2. Put back what was there before.
        for key, table in SNAPSHOT_TABLES:
            saved = snapshot.get(key) or []
            if not saved:
                continue
            cols = _columns(conn, table)
            cols = [c for c in cols if c in saved[0]]
            _insert(conn, table, cols, saved)

        conn.execute(
            "UPDATE import_batches SET undone_at = datetime('now') WHERE id = ?",
            (batch_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    restored = sum(len(snapshot.get(key) or []) for key, _ in SNAPSHOT_TABLES)
    return {
        "batch_id": batch_id,
        "groups_removed": len(groups),
        "rows_restored": restored,
        "message": (f"Reverted import {batch_id}: {len(groups)} trade(s) removed, "
                    f"{restored} row(s) restored."),
    }
