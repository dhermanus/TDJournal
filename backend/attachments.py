"""Files attached to a trade review.

Storage is deliberately split the same way the diary feature splits it: bytes
go to the local uploads directory as plain files, and only metadata is stored
in SQLite. The journal stays one SQLite file, so the database must not absorb
50 MB screenshots — that would slow every query for a benefit nobody gets.

Two rules make this safe:

  * the name used on disk is generated here, never taken from the upload. A
    filename is user-controlled and may contain separators, traversal segments
    or shell-hostile characters, so only the bytes and the extension survive
    the trip in; the stored name is a random token plus a validated extension.
  * reads resolve the path and require it to stay inside the uploads
    directory, so a row tampered with by hand cannot point the download
    endpoint at an arbitrary file.

Attachments are keyed by `trade_group`, not by `trades.id`: a re-import upserts
on (trade_group, account_id) and keeps the row id, but a *regrouping* import
(see `_replace_regrouped_trades`) deletes and recreates trades, which would
strand anything keyed on the integer id. `trade_analysis` and `trade_tags` use
the same group key for the same reason.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

# An allowlist, not a blocklist: refusing an unknown extension is the safe
# default, so a new executable suffix is rejected without a code change.
ALLOWED_EXTENSIONS = {
    # images — the common case is a screenshot or a phone photo of a chart
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif",
    # documents
    ".pdf", ".txt", ".csv", ".tsv", ".md", ".xlsx", ".xlsm", ".doc", ".docx",
}

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024   # 50 MB, agreed with the user

# What a browser can show in a page: images, PDFs, and text the preview box can
# read. Office formats are deliberately left out — rendering .docx/.xlsx means
# handing a macro-bearing file to a document suite, and HEIC is left out because
# no mainstream browser can draw it, so both keep the plain download link
# instead of a preview box that fails to paint.
INLINE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",           # images
    ".pdf",
    ".txt", ".csv", ".tsv", ".md",
}

# The whole of UPLOAD_DIR is mounted at `/uploads` for diary screenshots, which
# anyone can fetch by name. Trade attachments are private to one trade's review
# and are only meant to leave the server through the download route below, so
# they live in a subdirectory the static mount does not reach.
SUBDIR = "trade_attachments"

# How much of the original filename is kept for display. Control characters and
# path separators are dropped so the name is safe in a header, in a list, and
# when downloaded again.
_NAME_STRIP = re.compile(r"[\x00-\x1f\x7f/\\]+")


class AttachmentError(ValueError):
    """Raised for a refused upload; maps to HTTP 400 via the ValueError handler."""


def is_inline_preview(filename: str | None) -> bool:
    """Whether the extension is safe and supported for an in-page preview."""
    return extension_of(filename) in INLINE_EXTENSIONS


def sanitize_display_name(filename: str | None) -> str:
    """A printable version of the uploaded name, for display and download."""
    name = _NAME_STRIP.sub("_", (filename or "").strip()).strip(" .")
    return name[:180] or "attachment"


def extension_of(filename: str | None) -> str:
    return Path(sanitize_display_name(filename)).suffix.lower()


def make_stored_name(filename: str | None) -> str:
    """A unique on-disk name built from a validated extension only.

    The original filename never reaches the filesystem: `uuid4` supplies the
    name, so two uploads of `report.pdf` cannot collide and no part of the
    caller's path can escape the uploads directory.
    """
    ext = extension_of(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise AttachmentError(
            f"{ext or 'that'} files cannot be attached. Allowed: "
            + ", ".join(sorted(e.lstrip(".") for e in ALLOWED_EXTENSIONS))
        )
    return f"{uuid.uuid4().hex}{ext}"


def check_size(raw: bytes) -> None:
    if len(raw) > MAX_ATTACHMENT_BYTES:
        mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        raise AttachmentError(
            f"That file is {len(raw) / (1024 * 1024):.1f} MB; attachments are "
            f"limited to {mb} MB."
        )
    if not raw:
        raise AttachmentError("That file is empty.")


def attachment_path(upload_dir: str, stored_name: str) -> Path:
    """Resolve where a stored file lives, refusing anything outside the dir."""
    base = (Path(upload_dir) / SUBDIR).resolve()
    # `stored_name` comes from our own table, but treat it as untrusted anyway:
    # a hand-edited row must not turn the download route into a file read.
    target = (base / stored_name).resolve()
    if target.parent != base:
        raise FileNotFoundError("Attachment file is missing or out of range")
    return target


def save(conn, *, upload_dir: str, trade_group: str, filename: str | None,
         content_type: str | None, raw: bytes) -> dict:
    """Validate, write the file, record the row, and return the new metadata."""
    # A pasted screenshot may arrive with no name at all; default it so the
    # allowlist and the download filename both have something to work with.
    original = sanitize_display_name(filename) if filename else "pasted-image.png"
    stored = make_stored_name(original)   # raises AttachmentError if refused
    check_size(raw)                       # raises before anything is written

    path = attachment_path(upload_dir, stored)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)

    try:
        cur = conn.execute(
            "INSERT INTO trade_attachments "
            "(trade_group, original_name, stored_name, content_type, size_bytes) "
            "VALUES (?,?,?,?,?)",
            (trade_group, original, stored, content_type or "application/octet-stream", len(raw)),
        )
        conn.commit()
    except Exception:
        # The file must not outlive the record that names it, or every retry
        # after a failed insert leaves an orphan on disk.
        path.unlink(missing_ok=True)
        raise

    row = conn.execute(
        "SELECT * FROM trade_attachments WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return _to_dict(row)


def list_for(conn, trade_group: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trade_attachments WHERE trade_group=? ORDER BY created_at, id",
        (trade_group,),
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get(conn, attachment_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM trade_attachments WHERE id=?", (attachment_id,)
    ).fetchone()
    if not row:
        raise FileNotFoundError(f"Attachment {attachment_id} not found")
    return _to_dict(row)


def remove(conn, *, upload_dir: str, attachment_id: int) -> None:
    meta = get(conn, attachment_id)
    conn.execute("DELETE FROM trade_attachments WHERE id=?", (attachment_id,))
    conn.commit()
    # Delete the file only after the row is gone: a crash between the two leaves
    # a missing file (cosmetic), not a row pointing at a file that no longer
    # exists (a download that 404s with no way to clean it up).
    try:
        attachment_path(upload_dir, meta["stored_name"]).unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def remove_for_trade(conn, *, upload_dir: str, trade_group: str) -> int:
    """Delete every attachment of a trade, files included. Used on trade delete."""
    metas = list_for(conn, trade_group)
    conn.execute("DELETE FROM trade_attachments WHERE trade_group=?", (trade_group,))
    conn.commit()
    removed = 0
    for meta in metas:
        try:
            attachment_path(upload_dir, meta["stored_name"]).unlink(missing_ok=True)
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def _to_dict(row) -> dict:
    d = dict(row)
    d["kind"] = "image" if d.get("content_type", "").startswith("image/") else "file"
    d["previewable"] = is_inline_preview(d.get("original_name"))
    return d
