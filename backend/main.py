import os
import json
import re
import sqlite3
import tempfile
import aiofiles
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
from dotenv import load_dotenv

import httpx

from database import init_db, get_db, row_to_dict
import database
import attachments
from csv_parser import (
    parse_broker_csv, parse_mt5_bars_csv, symbol_from_bar_filename, FUTURES_MULTIPLIERS,
    detect_broker,
)
import excursions
from ai_analysis import (
    analyze_diary_entry,
    analyze_diary_text,
    save_analysis_to_db,
    build_trades_context,
    generate_insights,
    build_brain_context,
    generate_brain_response,
    brain_turn,
    generate_weekly_summary,
)
from daily_summary import build_daily_context, generate_daily_summary
from diary_matches import analyse_confidence, queued_for_review
from daily_cache import daily_input_fingerprint, date_range_fingerprint
from ai_usage import last_usage, forget_usage, friendly_error
import ai_settings
from library import router as library_router, init_library_tables, apply_aliases, library_names
import instruments
import equity
import mt5_time
import backup
import import_batch

load_dotenv()

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _conn = get_db()
    try:
        init_library_tables(_conn)
        # The selected model is read at request time by the AI call sites, which
        # have no connection, so it is loaded once here (and again on save).
        ai_settings.set_model(ai_settings.load_config(_conn)["model"])
    finally:
        _conn.close()
    Path(UPLOAD_DIR).mkdir(exist_ok=True)
    yield


def require_feature(conn: sqlite3.Connection, feature: str):
    """Refuse an AI request whose feature is switched off, before anything is built.

    The check sits first in every AI endpoint, ahead of context construction,
    because a disabled toggle has to mean "this feature sends nothing" — not
    "build the prompt and drop the answer".
    """
    if not ai_settings.is_enabled(conn, feature):
        raise HTTPException(
            status_code=403,
            detail=f"{ai_settings.FEATURES[feature]['label']} is turned off in Settings → AI. "
                   "Nothing was sent.")


def _ai_usage_payload(usage: dict | None, reason: str | None = None) -> dict:
    """What an AI action spent, in the shape every AI endpoint returns.

    `spent` is false for a cache hit or a refusal, so the UI never attributes a
    cost to a request that was not made. `estimated_cost_usd` is a published-rate
    estimate rather than an invoice — the configured endpoint bills its own
    rates — and `cache_*` tokens read as 0 because the proxy in front of this
    deployment strips those fields instead of reporting them.
    """
    if not usage:
        return {"spent": False, "reason": reason or "no request was made"}
    payload = {
        "spent": True,
        "model": usage.get("model"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "estimated_cost_usd": usage.get("estimated_cost_usd", 0.0),
        "estimate": True,
        "finished_at": usage.get("finished_at"),
    }
    if reason:
        payload["reason"] = reason
    return payload


app = FastAPI(title="TDJournal API", lifespan=lifespan)

# This runs on your own machine, so any localhost port is accepted: when 3010 is
# busy the dev server offers 3011, and the app should still work. FRONTEND_ORIGINS
# (comma separated) adds non-localhost origins, e.g. another machine on your LAN.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("FRONTEND_ORIGINS", "").split(",") if o.strip()]
LOCALHOST_ANY_PORT = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=LOCALHOST_ANY_PORT,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve uploaded diary screenshots (create the folder on first run)
Path(UPLOAD_DIR).mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Settings > Library (strategies, sources, tags)
app.include_router(library_router)


# ── Dependency ─────────────────────────────────────────────────────────────────

def get_connection():
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


# ── Exception handlers ─────────────────────────────────────────────────────────

@app.exception_handler(ValueError)
async def value_error_handler(request, exc):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(FileNotFoundError)
async def not_found_handler(request, exc):
    return JSONResponse(status_code=404, content={"error": str(exc)})


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "type": type(exc).__name__}
    )


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok"}


# ── Goals ──────────────────────────────────────────────────────────────────────

GOAL_DEFAULTS = {
    "win_rate": 65.0,
    "profit_factor": 1.5,
    "day_win_rate": 75.0,
    "expectancy": 50.0,
    "avg_win_loss_ratio": 1.5,
    "exit_efficiency": 50.0,
}


class GoalsBody(BaseModel):
    account_id: int | None = None
    win_rate: float = 65.0
    profit_factor: float = 1.5
    day_win_rate: float = 75.0
    expectancy: float = 50.0
    avg_win_loss_ratio: float = 1.5
    exit_efficiency: float = 50.0


@app.get("/api/goals")
def get_goals(
    account_id: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    acct_key = account_id if account_id is not None else 0
    row = conn.execute(
        "SELECT value FROM settings WHERE account_id = ? AND key = 'goals'",
        (acct_key,),
    ).fetchone()
    if row:
        return json.loads(row["value"])
    # If account-specific not found, try global (0)
    if acct_key != 0:
        row = conn.execute(
            "SELECT value FROM settings WHERE account_id = 0 AND key = 'goals'",
        ).fetchone()
        if row:
            return json.loads(row["value"])
    return GOAL_DEFAULTS


@app.put("/api/goals")
def put_goals(
    body: GoalsBody,
    conn: sqlite3.Connection = Depends(get_connection),
):
    acct_key = body.account_id if body.account_id is not None else 0
    payload = json.dumps({
        "win_rate": body.win_rate,
        "profit_factor": body.profit_factor,
        "day_win_rate": body.day_win_rate,
        "expectancy": body.expectancy,
        "avg_win_loss_ratio": body.avg_win_loss_ratio,
        "exit_efficiency": body.exit_efficiency,
    })
    conn.execute(
        """INSERT INTO settings (account_id, key, value) VALUES (?, 'goals', ?)
           ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value""",
        (acct_key, payload),
    )
    conn.commit()
    return json.loads(payload)


# ── MT5 server timezone ────────────────────────────────────────────────────────
# MT5 exports raw broker-server time with no offset. This setting names the
# IANA zone that server runs on, so a historical fill is converted with the
# daylight-saving rule that applied *on that date* rather than today's offset.

class Mt5TimezoneBody(BaseModel):
    timezone: str


def _mt5_timezone(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE account_id = 0 AND key = ?",
        (mt5_time.SETTINGS_KEY_TIMEZONE,),
    ).fetchone()
    stored = row["value"] if row else ""
    return mt5_time.resolve_timezone(stored, os.getenv("MT5_SERVER_TIMEZONE"))


@app.get("/api/mt5/timezone")
def get_mt5_timezone(conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute(
        "SELECT value FROM settings WHERE account_id = 0 AND key = ?",
        (mt5_time.SETTINGS_KEY_TIMEZONE,),
    ).fetchone()
    effective = _mt5_timezone(conn)
    try:
        zone = mt5_time.load_zone(effective)
        error = None
    except mt5_time.TimezoneError as exc:
        zone = None
        error = str(exc)
    return {
        "timezone": row["value"] if row else "",
        "effective": effective,
        "source": "setting" if row else (
            "env" if os.getenv("MT5_SERVER_TIMEZONE") else "unset"
        ),
        "valid": zone is not None,
        "error": error,
        # This is a live sanity-check only; imports use the historical
        # transition table, not this offset.
        "current_offset_hours": (
            datetime.now(zone).utcoffset().total_seconds() / 3600.0
            if zone else None
        ),
        "candidates": mt5_time.CANDIDATE_ZONES,
        "transitions": (
            mt5_time.dst_summary(effective, 2025, 2028) if zone else []
        ),
    }


@app.put("/api/mt5/timezone")
def put_mt5_timezone(
    body: Mt5TimezoneBody,
    conn: sqlite3.Connection = Depends(get_connection),
):
    name = body.timezone.strip()
    try:
        mt5_time.load_zone(name)
    except mt5_time.TimezoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    conn.execute(
        """INSERT INTO settings (account_id, key, value) VALUES (0, ?, ?)
           ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value""",
        (mt5_time.SETTINGS_KEY_TIMEZONE, name),
    )
    conn.commit()
    return get_mt5_timezone(conn=conn)


# ── AI settings ───────────────────────────────────────────────────────────────
#
# "Your data stays local" is true of the app and false of the AI features the
# moment they run, so the switches and the disclosure live next to each other.

class AiSettingsBody(BaseModel):
    model: str | None = None
    features: dict[str, bool] | None = None


def _ai_settings_response() -> dict:
    cfg = ai_settings.load_config(get_db())
    from urllib.parse import urlparse
    configured = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if configured:
        host = urlparse(configured).netloc or configured
        destination = f"the endpoint set in backend/.env ({host})"
    else:
        destination = "Anthropic's API (api.anthropic.com)"
    return {
        "model": cfg["model"],
        "models": list(ai_settings.KNOWN_MODELS),
        "features": cfg["features"],
        # label + sends are kept separate so the UI can render them without the
        # payload shapes colliding (booleans here, prose there).
        "feature_info": {name: dict(meta) for name, meta in ai_settings.FEATURES.items()},
        "api_key_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "sends_to": destination,
        "notice": f"Turning a feature on sends the data listed beside it to {destination}. "
                  "Turning it off means that feature sends nothing.",
    }


@app.get("/api/ai-settings")
def get_ai_settings():
    return _ai_settings_response()


@app.put("/api/ai-settings")
def put_ai_settings(body: AiSettingsBody, conn: sqlite3.Connection = Depends(get_connection)):
    try:
        ai_settings.save_config(conn, model=body.model, features=body.features)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _ai_settings_response()


# ── Accounts ───────────────────────────────────────────────────────────────────
class AccountCreate(BaseModel):
    name: str
    type: str
    color: str = "#6366f1"
    broker: str | None = None
    starting_capital: float | None = None


@app.get("/api/accounts")
def list_accounts(conn: sqlite3.Connection = Depends(get_connection)):
    rows = conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
    return [row_to_dict(r) for r in rows]


class AccountUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    color: str | None = None
    broker: str | None = None
    starting_capital: float | None = None


def _check_starting_capital(value: float | None) -> None:
    """Capital is a denominator, so it has to be a non-negative number.

    A negative starting balance has no meaning against these ratios (return on
    what?) and would flip every percentage, so it is refused rather than stored
    and interpreted later. None clears it back to "unknown".
    """
    if value is None:
        return
    if not isinstance(value, (int, float)) or value < 0:
        raise ValueError("Starting capital must be a number of 0 or more.")


@app.put("/api/accounts/{account_id}")
def update_account(account_id: int, data: AccountUpdate, conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")

    # exclude_unset distinguishes "not mentioned" from "explicitly set to null":
    # clearing capital back to unknown has to be expressible, while the other
    # fields keep the existing behaviour where a null is ignored.
    updates = {k: v for k, v in data.model_dump(exclude_unset=True).items()
               if v is not None or k == "starting_capital"}
    if not updates:
        return row_to_dict(row)

    if 'type' in updates:
        valid_types = {'day_trading', 'swing_trading', 'investment'}
        if updates['type'] not in valid_types:
            raise ValueError(f"type must be one of {valid_types}")
    if 'starting_capital' in updates:
        _check_starting_capital(updates['starting_capital'])

    set_clause = ', '.join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE accounts SET {set_clause} WHERE id=?", list(updates.values()) + [account_id])
    conn.commit()

    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return row_to_dict(row)


@app.post("/api/accounts", status_code=201)
def create_account(data: AccountCreate, conn: sqlite3.Connection = Depends(get_connection)):
    valid_types = {'day_trading', 'swing_trading', 'investment'}
    if data.type not in valid_types:
        raise ValueError(f"type must be one of {valid_types}")
    _check_starting_capital(data.starting_capital)

    cursor = conn.execute(
        "INSERT INTO accounts (name, type, color, broker, starting_capital) VALUES (?,?,?,?,?)",
        (data.name, data.type, data.color, data.broker, data.starting_capital)
    )
    conn.commit()

    row = conn.execute("SELECT * FROM accounts WHERE id=?", (cursor.lastrowid,)).fetchone()
    return row_to_dict(row)


# ── CSV Import ─────────────────────────────────────────────────────────────────

class SetupOverride(BaseModel):
    setup: str | None = None      # a playbook setup name, 'NONE', or None to clear
    note: str | None = None


@app.patch("/api/trades/{trade_id}/setup")
def override_setup(
    trade_id: int,
    body: SetupOverride,
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Tag a trade with one of your playbook setups (or clear the tag)."""
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    if body.setup is None:
        conn.execute(
            "UPDATE trades SET setup=NULL, setup_notes=NULL, setup_source='manual' "
            "WHERE id=?", (trade_id,))
        conn.commit()
        return {"id": trade_id, "setup": None, "setup_grade": row['setup_grade'],
                "setup_source": "manual", "message": "Setup tag cleared."}

    if body.setup != 'NONE':
        known = conn.execute(
            "SELECT 1 FROM custom_setups WHERE name=? AND active=1", (body.setup,)
        ).fetchone()
        if not known:
            raise HTTPException(
                status_code=400,
                detail=f"'{body.setup}' is not in your playbook. "
                       f"Add it first (POST /api/setups/custom or the + option in the UI).")

    notes = {
        "manual": True,
        "note": body.note,
        "notes": [f"Manually set to {body.setup}"],
        "violations": [],
    }
    conn.execute(
        "UPDATE trades SET setup=?, setup_notes=?, setup_source='manual' WHERE id=?",
        (body.setup, json.dumps(notes), trade_id))
    conn.commit()
    return {"id": trade_id, "setup": body.setup, "setup_grade": row['setup_grade'],
            "setup_source": "manual", "message": f"Setup set to {body.setup}."}



# ── Playbook setups ───────────────────────────────────────────────────────────
# The playbook is the trader's own list of named setups. Trades are tagged with
# one of them by hand, so tags always carry setup_source='manual'.

class CustomSetupBody(BaseModel):
    name: str
    side: str | None = None      # LONG | SHORT | None (either)
    notes: str | None = None


@app.get("/api/setups/custom")
def list_custom_setups(conn: sqlite3.Connection = Depends(get_connection)):
    rows = conn.execute(
        "SELECT cs.*, "
        " (SELECT COUNT(*) FROM trades t WHERE t.setup = cs.name) AS trade_count, "
        " (SELECT ROUND(SUM(t.net_pnl),2) FROM trades t WHERE t.setup = cs.name) AS net_pnl "
        "FROM custom_setups cs WHERE cs.active = 1 ORDER BY cs.name"
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/setups/custom")
def create_custom_setup(body: CustomSetupBody,
                        conn: sqlite3.Connection = Depends(get_connection)):
    name = (body.name or '').strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if len(name) > 60:
        raise HTTPException(status_code=400, detail="Name must be 60 characters or fewer")
    if name.upper() == 'NONE':
        raise HTTPException(status_code=400, detail="'NONE' is reserved")
    side = (body.side or '').upper() or None
    if side not in (None, 'LONG', 'SHORT'):
        raise HTTPException(status_code=400, detail="side must be LONG, SHORT or empty")
    try:
        conn.execute(
            "INSERT INTO custom_setups (name, side, notes) VALUES (?,?,?)",
            (name, side, body.notes))
        conn.commit()
    except sqlite3.IntegrityError:
        # Already exists — reactivate rather than erroring, so re-adding is harmless.
        conn.execute("UPDATE custom_setups SET active=1 WHERE name=?", (name,))
        conn.commit()
    row = conn.execute("SELECT * FROM custom_setups WHERE name=?", (name,)).fetchone()
    return dict(row)


@app.delete("/api/setups/custom/{setup_id}")
def delete_custom_setup(setup_id: int,
                        conn: sqlite3.Connection = Depends(get_connection)):
    """Soft-delete: trades already tagged with it keep their label."""
    row = conn.execute("SELECT * FROM custom_setups WHERE id=?", (setup_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Setup not found")
    n = conn.execute("SELECT COUNT(*) c FROM trades WHERE setup=?", (row['name'],)).fetchone()['c']
    conn.execute("UPDATE custom_setups SET active=0 WHERE id=?", (setup_id,))
    conn.commit()
    return {"deleted": row['name'], "trades_keeping_label": n}


@app.get("/api/setups")
def setup_stats(
    account_id: int = Query(1),
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Performance grouped by setup and by grade, for the Edge view."""
    def agg(group_col):
        rows = conn.execute(f"""
            SELECT {group_col} AS k,
                   COUNT(*) AS n,
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(SUM(net_pnl), 2) AS total,
                   ROUND(AVG(net_pnl), 2) AS avg,
                   SUM(CASE WHEN net_pnl < -500 THEN 1 ELSE 0 END) AS big_losses
            FROM trades
            WHERE account_id = ?
              AND net_pnl IS NOT NULL AND net_pnl != 0 AND {group_col} IS NOT NULL
            GROUP BY {group_col} ORDER BY total DESC
        """, (account_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['win_rate'] = round(d['wins'] / d['n'] * 100, 1) if d['n'] else 0
            out.append(d)
        return out

    return {"by_setup": agg('setup'), "by_grade": agg('setup_grade'), "labels": {}}


def _replace_regrouped_trades(conn, account_id: int, trades: list[dict]) -> None:
    """Clear stored trades that an import regrouped with new fills (see overlapping_db_fills).

    The review data (analysis and tags) of each old trade follows its fills: it stays put
    when a new trade keeps the old name, otherwise it moves to the new trade holding most of
    those fills, unless that trade already has its own.
    """
    old_groups = {g for t in trades for g in t.get('replaces', [])}
    if not old_groups:
        return
    new_keys = {t['trade_group'] for t in trades}
    moves = {}
    for g in old_groups - new_keys:
        holders = [t for t in trades if g in t.get('replaces', [])]
        fps = {(e['date'], e['time'], e['action'], e['qty'], e['price'])
               for e in json.loads(conn.execute(
                   "SELECT executions FROM trades WHERE trade_group=? AND account_id=?",
                   (g, account_id)).fetchone()[0] or '[]')}
        best = max(holders, key=lambda t: sum(
            (e['date'], e['time'], e['action'], e['qty'], e['price']) in fps
            for e in json.loads(t['executions'])))
        moves[g] = best['trade_group']
    for g in old_groups - new_keys:
        conn.execute("DELETE FROM trades WHERE trade_group=? AND account_id=?", (g, account_id))
    for g, target in moves.items():
        has_own = conn.execute("SELECT 1 FROM trade_analysis WHERE trade_group=?", (target,)).fetchone()
        if not has_own:
            conn.execute("UPDATE trade_analysis SET trade_group=? WHERE trade_group=?", (target, g))
            conn.execute("UPDATE trade_tags SET trade_group=? WHERE trade_group=?", (target, g))


# How many trade_group names a preview ships in each list. The counts are what
# the screen is built around; the names are so the user can spot-check a few,
# and a 5,000-row statement must not put 5,000 strings through the UI.
PREVIEW_LIST_CAP = 50


def _persist_import(conn: sqlite3.Connection, account_id: int,
                    trades: list[dict]) -> tuple[int, list[dict]]:
    """Write parsed trades into `conn`. Returns (how many were written, failures).

    Shared by the import and by the preview, deliberately: the preview replays
    exactly this function against its copy, so the counts it reports come from
    the same statements the real import runs. A second implementation of the
    insert would drift, and the two would disagree about what an import does —
    the one thing a preview must never do.

    Per-trade failures are collected rather than raised: one malformed row must
    not cost the rest of the file. A failure outside the loop rolls back, because
    a half-written import is worse than none.
    """
    imported = 0
    errors = []
    try:
        _replace_regrouped_trades(conn, account_id, trades)
        for trade in trades:
            try:
                conn.execute("""
                    INSERT INTO trades
                        (account_id, trade_group, date, ticker, instrument_type, side,
                         gross_pnl, net_pnl, commissions, executions,
                         option_expiry, option_strike, option_type, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(trade_group, account_id) DO UPDATE SET
                        date=excluded.date,
                        side=excluded.side,
                        gross_pnl=excluded.gross_pnl,
                        net_pnl=excluded.net_pnl,
                        commissions=excluded.commissions,
                        executions=excluded.executions,
                        imported_at=datetime('now')
                """, (
                    trade['account_id'], trade['trade_group'], trade['date'],
                    trade['ticker'], trade['instrument_type'], trade['side'],
                    trade['gross_pnl'], trade['net_pnl'], trade['commissions'],
                    trade['executions'], trade['option_expiry'],
                    trade['option_strike'], trade['option_type'], trade['source'],
                ))
                imported += 1
            except Exception as e:
                errors.append({"trade_group": trade.get('trade_group'), "error": str(e)})

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return imported, errors


@contextmanager
def _preview_database():
    """A throwaway database holding a copy of the live journal, or raise.

    A dry run has to run *somewhere*, because parsing is not read-only:
    `build_trades_from_executions` updates stored rows and commits when new fills
    land on an open option position — measured as 0.0/1 fill becoming 149.3/2
    fills before the import returned a single trade to insert. A savepoint cannot
    contain that, because the parser calls `conn.commit()` itself, and a commit
    escapes the savepoint.

    Copying can. It takes ~10ms for this journal and, unlike a backup taken
    through the app's own connection, it succeeds while another connection holds
    an open write transaction — the naive form hangs there (that probe timed out).
    The source is opened `mode=ro` so taking the copy cannot take the write lock
    and stall whatever the app is doing.
    """
    path = Path(database.DB_PATH).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Journal not found at {path}")
    fd, dest = tempfile.mkstemp(suffix=".db", prefix="tdjournal-preview-")
    os.close(fd)
    # Both connections are closed before anything unlinks the file. If the parse
    # raised and the preview connection were still open, the unlink would fail on
    # Windows — where the file is locked until the last handle goes — and every
    # failed preview would leak a temp database.
    copy = None
    preview = None
    try:
        source = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        copy = sqlite3.connect(dest)
        try:
            source.backup(copy)
        finally:
            source.close()
        copy.close()
        copy = None

        preview = sqlite3.connect(dest)
        preview.row_factory = sqlite3.Row
        preview.execute("PRAGMA journal_mode=WAL")
        preview.execute("PRAGMA foreign_keys=ON")
        try:
            yield preview
        finally:
            preview.close()
            preview = None
    finally:
        if preview is not None:
            preview.close()
        if copy is not None:
            copy.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(dest + suffix).unlink(missing_ok=True)
            except OSError:
                pass


def _journal_trades(conn: sqlite3.Connection, account_id: int) -> dict:
    """trade_group -> (date, side, gross, net, commissions, executions) for an account.

    The key shape is deliberately `(date, executions)` plus the money: two
    statements can produce identical P&L and still be different rows, so
    comparing on P&L alone would call an update "unchanged".
    """
    rows = conn.execute(
        "SELECT trade_group, date, side, gross_pnl, net_pnl, commissions, executions "
        "FROM trades WHERE account_id=?", (account_id,))
    return {r[0]: (r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows}


def _classify_import(before: dict, after: dict, trades: list[dict]) -> dict:
    """What happened to the journal across a parse, from the two snapshots.

    Not from `trades`. An open option position whose fills arrive later is
    rewritten *inside the parse* — measured as net_pnl 0.0/1 fill becoming
    149.3/2 fills — and `build_trades_from_executions` then returns **zero**
    trades, because the work was already done. Classifying only the returned
    trades called that `imported: 0`, and a preview built on it reported
    "nothing to do" for a file that rewrites a stored row.

    Diffing the copy against the journal reads the same fact the import will
    produce: a group only after is created, a group in both whose row changed is
    updated, and a group only before is one `_replace_regrouped_trades` deleted.
    """
    created = sorted(set(after) - set(before))
    updated = sorted(g for g in set(after) & set(before) if after[g] != before[g])
    replaced = sorted(set(before) - set(after))
    return {
        "create": created,
        "update": updated,
        "replaced": replaced,
        # The sum over `trades` still: rows changed by absorption carry no
        # returned trade, and the preview's job is to report what will be added
        # to the journal, which is exactly the parse's return value.
        "net_pnl": round(sum(t.get("net_pnl") or 0 for t in trades), 2),
    }


@app.post("/api/import-csv/preview")
async def import_csv_preview(
    account_id: int = Form(...),
    file: UploadFile = File(...),
    broker: str = Form('auto'),
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Parse a statement and report what importing it would do, writing nothing.

    Deliberately not a flag on /api/import-csv: the two have different failure
    modes to prove, and a flag makes "dry run" one `true` away from "wrote the
    journal" if someone misses it. This handler has no INSERT, UPDATE or DELETE
    against the live connection at all.
    """
    if not file.filename.lower().endswith('.csv'):
        raise ValueError("Only .csv files are accepted")

    account = conn.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not account:
        raise ValueError(f"Account {account_id} not found")

    raw = await file.read()
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('latin-1')

    # Same MT5 timezone resolution as the real import: reading it must not be
    # the thing that makes a preview disagree with the import it predicts.
    tz_name = _mt5_timezone(conn)
    line_errors: list[str] = []

    # Before and after are snapshots: `before` is a SELECT against the journal
    # (never a write), `after` the copy once parsing has run. The diff is the
    # answer, because some of the work happens inside the parse and never shows
    # up in the trades it returns.
    before = _journal_trades(conn, account_id)
    with _preview_database() as preview:
        trades, skipped = parse_broker_csv(
            content, broker, account_id, preview, tz_name=tz_name,
            problems=line_errors,
        )
        # Replay the write step on the copy as well. Without it the diff would
        # only ever see what the parse did on its own, and would report a file
        # that merely returns trades as having changed nothing.
        # Its written count is not the diff's: absorption lets the parse return
        # zero trades while still changing a row, so the two answer different
        # questions and must not be added together.
        _, replay_errors = _persist_import(preview, account_id, trades)
        after = _journal_trades(preview, account_id)
    would = _classify_import(before, after, trades)

    report = skipped if isinstance(skipped, dict) else None
    skipped_deals = report.get("skipped_deals", skipped) if report else skipped
    detect = detect_broker(content)
    broker_key = (broker or 'auto').strip().lower()
    if broker_key == 'auto':
        broker_key = detect or ''

    # Nothing to do means no row of the journal would be touched. Neither input
    # decides this alone: `skipped` counts fills already imported, so a file
    # imported twice has skipped > 0 and still does nothing, while `trades` can be
    # empty for a file that rewrites a row through the absorption path.
    nothing_to_do = not (would["create"] or would["update"] or would["replaced"])
    if nothing_to_do:
        message = "Nothing to import: every fill in this file is already in the journal."
    else:
        # These are trade *groups*, not fills — which is the unit both this
        # screen and the import's own summary work in.
        message = (
            f"{len(would['create'])} new trade(s), {len(would['update'])} to update, "
            f"{len(would['replaced'])} to replace"
            + (f", {skipped_deals} fill(s) already imported" if skipped_deals else "")
        )
        if line_errors:
            message += f"; {len(line_errors)} row(s) not imported"
        if replay_errors:
            # The write step failed on the copy, so it will fail the same way on
            # the journal — worth saying before the user commits to it.
            message += f"; {len(replay_errors)} trade(s) could not be written"

    return {
        # Cap the listing: a 5,000-row statement must not ship 5,000 names to
        # render, and the counts above are what the screen is built around.
        "create": would["create"][:PREVIEW_LIST_CAP],
        "update": would["update"][:PREVIEW_LIST_CAP],
        "replaced": would["replaced"][:PREVIEW_LIST_CAP],
        "create_count": len(would["create"]),
        "update_count": len(would["update"]),
        "replaced_count": len(would["replaced"]),
        "skipped": skipped_deals,
        "write_errors": replay_errors,
        "net_pnl": would["net_pnl"],
        "line_errors": line_errors,
        "broker": broker_key,
        "detected_broker": detect,
        "nothing_to_do": nothing_to_do,
        "message": message,
        "details": report,
    }


@app.post("/api/import-csv")
async def import_csv(
    account_id: int = Form(...),
    file: UploadFile = File(...),
    broker: str = Form('auto'),   # 'thinkorswim' | 'ibkr' | 'auto' (sniff the file)
    conn: sqlite3.Connection = Depends(get_connection),
):
    if not file.filename.lower().endswith('.csv'):
        raise ValueError("Only .csv files are accepted")

    account = conn.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not account:
        raise ValueError(f"Account {account_id} not found")

    raw = await file.read()
    try:
        content = raw.decode('utf-8-sig')  # strips BOM
    except UnicodeDecodeError:
        content = raw.decode('latin-1')

    # MT5 timestamps are broker-server local time; every other parser writes
    # dates the file already states. Resolved here so the timezone setting is
    # read from the same connection the import writes through.
    # `line_errors` collects trade rows that could not be read. Without it the
    # Thinkorswim and IBKR sections dropped such rows on `continue` and the
    # import still reported success — a statement could lose a fill and nothing
    # said so. The generic template and MT5 refuse the whole file instead, so
    # they add nothing here.
    line_errors: list[str] = []

    # Snapshot before parsing, because parsing writes: it merges fills into open
    # option positions and commits, so anything captured afterwards would hold the
    # merged row rather than the original. Only options are at risk (the merge is
    # guarded on instrument_type), and stock positions are never auto-merged.
    # The rest of the picture has to wait — the group list the import will touch
    # only exists once parsing has produced it — so it is taken after the parse
    # and merged, with this earlier one winning for the groups it already knows.
    early = import_batch.capture(
        conn, account_id, import_batch.open_option_groups(conn, account_id))
    # The whole account, before the parse touches it: the diff of this against
    # the same query after _persist_import is the only answer to "did anything
    # change?", because both halves of the import can write on their own.
    journal_before = _journal_trades(conn, account_id)

    trades, skipped = parse_broker_csv(
        content, broker, account_id, conn, tz_name=_mt5_timezone(conn),
        problems=line_errors,
    )
    report = skipped if isinstance(skipped, dict) else None
    skipped_deals = report.get("skipped_deals", skipped) if report else skipped

    # Taken here, not inside _persist_import: that function also deletes the rows
    # `_replace_regrouped_trades` is about to move, and a snapshot taken after a
    # delete is a picture of the thing we want to restore.
    touched = ({t["trade_group"] for t in trades}
               | {g for t in trades for g in (t.get("replaces") or [])}
               | set(early["groups"]))
    pre = import_batch.merge(early, import_batch.capture(conn, account_id, touched))

    imported, errors = _persist_import(conn, account_id, trades)
    changed = _journal_trades(conn, account_id) != journal_before

    # `pre` holds only rows that existed before: created trades contribute an
    # empty list, and their group still has to be in the snapshot, because that
    # is what undo deletes. So it is stored as-is — no filtering, or a brand-new
    # trade would be undone not at all.
    batch_id = import_batch.record(
        conn, account_id,
        filename=(file.filename or ""),
        broker=(broker or "auto"),
        snapshot=pre,
        imported=imported, skipped=skipped_deals,
        line_error_count=len(line_errors),
        write_error_count=len(errors),
        changed=changed,
    )

    return {
        "imported": imported,
        "skipped": skipped_deals,
        "errors": errors,
        "line_errors": line_errors,
        "batch_id": batch_id,
        "message": (
            f"Imported {imported} MT5 position(s). "
            f"Skipped {skipped_deals} previously imported deal(s). "
            f"{report['open_positions']} position(s) remain open."
            + _dst_note(report)
            if report else
            f"Imported {imported} trade group(s). "
            f"Skipped {skipped_deals} duplicate execution(s)."
            + (f" {len(line_errors)} row(s) in the file were not imported."
               if line_errors else "")
        ),
        "details": report,
    }


@app.get("/api/import-batches")
def get_import_batches(
    account_id: int,
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Recent imports for an account, newest first, without their snapshots."""
    if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        raise ValueError(f"Account {account_id} not found")
    return {"batches": import_batch.list_batches(conn, account_id)}


@app.post("/api/import-batches/{batch_id}/undo")
def undo_import(
    batch_id: int,
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Put the journal back to how it looked before that import.

    Journal data only — trades, analysis, tags, attachments. Diary entries and M1
    bars are separate workflows and are not part of an import's blast radius.
    Refuses (400) rather than guessing when the batch is already undone or when
    a later import sits on top of it, because restoring an older snapshot over a
    newer one would silently discard that newer work.
    """
    return import_batch.undo(conn, batch_id, upload_dir=UPLOAD_DIR)


def _dst_note(report: dict) -> str:
    """Warn when a deal landed on a clock change, where the offset is ambiguous.

    On the spring-forward gap the local time does not exist and on the autumn
    fallback it maps to two instants an hour apart; either way the converted
    timestamp can be an hour off, which is worth saying out loud rather than
    burying in `details`.
    """
    parts = []
    if report.get("dst_nonexistent"):
        parts.append(f"{report['dst_nonexistent']} deal(s) fell in a skipped clock hour")
    if report.get("dst_ambiguous"):
        parts.append(f"{report['dst_ambiguous']} deal(s) fell in a repeated clock hour")
    if not parts:
        return ""
    return " Note: " + " and ".join(parts) + \
        f" during a daylight-saving change of {report.get('tz', 'the server zone')} — check those times."


@app.post("/api/import-bars")
async def import_bars(
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Store one M1 bar file and recompute the excursions it can cover.

    Bars are market data, not account data: two accounts trading EURUSD read the
    same candles, so there is no account to attach them to. What does change per
    import is which trades get measured — those whose ticker the file names.
    """
    filename = (file.filename or "").lower()
    if not filename.endswith('.csv'):
        raise ValueError("Only .csv files are accepted")

    raw = await file.read()
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('latin-1')

    # Same resolution path as the deal import, so bars and fills land on one
    # timeline even if the zone is set after trades were already imported.
    tz_name = _mt5_timezone(conn)
    bars, report = parse_mt5_bars_csv(content, tz_name)

    symbol = symbol_from_bar_filename(file.filename)
    if not symbol:
        raise ValueError(
            "Could not tell which symbol this file is for. Name it "
            "TDJournal_bars_<SYMBOL>_M1.csv, the name ExportBarsCSV.mq5 writes."
        )

    # Existing times are read once for the whole symbol. Checking per row would
    # be 74,000 queries for a single EURUSD file, and SQLite reports rowcount 1
    # for the DO UPDATE path anyway, so neither loop-local signal is free.
    existing = {r[0] for r in conn.execute(
        "SELECT time FROM bars WHERE symbol = ?", (symbol,))}
    stored = 0
    refreshed = 0
    for bar in bars:
        conn.execute("""
            INSERT INTO bars (symbol, time, open, high, low, close, tick_volume)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(symbol, time) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, tick_volume=excluded.tick_volume
        """, (symbol, bar["time"], bar["open"], bar["high"],
              bar["low"], bar["close"], bar["tick_volume"]))
        if bar["time"] in existing:
            refreshed += 1
        else:
            stored += 1

    # One symbol in, so only that symbol's trades are re-measured. Clearing
    # first means a trade whose bars just vanished cannot keep a stale number.
    excursion = excursions.recompute(conn, [symbol])
    conn.commit()

    return {
        "symbol": symbol,
        "bars": report["bars"],
        "stored": stored,
        "refreshed": refreshed,
        "first": report["first"],
        "last": report["last"],
        "measured": excursion["measured"],
        "without_bars": excursion["without_bars"],
        "message": (
            f"Imported {report['bars']} {symbol} M1 bar(s) "
            f"({stored} new, {refreshed} refreshed). "
            f"Recomputed MFE/MAE for {excursion['measured']} trade(s)."
            + (f" {excursion['without_bars']} trade(s) have no bars to measure."
               if excursion["without_bars"] else "")
            + _dst_note(report)
        ),
        "details": report,
    }


# ── Trades ─────────────────────────────────────────────────────────────────────

class TradeCreate(BaseModel):
    account_id: int
    date: str
    ticker: str
    instrument_type: str = "STOCK"
    side: str
    entry_price: float
    exit_price: float | None = None
    quantity: int = 1
    commissions: float = 0.0
    strategy: str | None = None
    stop_loss: float | None = None
    risk_per_trade: str | None = None
    notes: str | None = None
    option_expiry: str | None = None
    option_strike: float | None = None
    option_type: str | None = None
    time: str | None = None


def compute_manual_pnl(side: str, entry: float, exit_price: float | None, qty: int, commissions: float) -> tuple[float, float]:
    if exit_price is None:
        return 0.0, -commissions
    if side.upper() == 'LONG':
        gross = (exit_price - entry) * qty
    else:
        gross = (entry - exit_price) * qty
    return round(gross, 2), round(gross - commissions, 2)


def _is_open_position(trade: dict) -> bool:
    execs = trade.get('executions') or []
    side = (trade.get('side') or 'LONG').upper()
    entry_action = 'BOT' if side == 'LONG' else 'SOLD'
    exit_action  = 'SOLD' if side == 'LONG' else 'BOT'
    entry_qty = sum(e.get('qty', 0) for e in execs if e.get('action') == entry_action)
    exit_qty  = sum(e.get('qty', 0) for e in execs if e.get('action') == exit_action)
    return entry_qty > 0 and entry_qty != exit_qty


@app.get("/api/trades")
def list_trades(
    account_id: int | None = Query(None),
    instrument_type: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    ticker: str | None = Query(None),
    open_only: bool = Query(False),
    limit: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    sql = """
        SELECT t.*, ta.strategy, ta.stop_loss, ta.r_multiple, ta.match_confidence, ta.emotional_state,
               ta.entry_reason, ta.exit_reason, ta.ai_feedback, ta.mistakes, ta.notes as analysis_notes
        FROM trades t
        LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group
        WHERE 1=1
    """
    params = []

    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    if instrument_type:
        sql += " AND t.instrument_type = ?"
        params.append(instrument_type.upper())
    if date_from:
        sql += " AND t.date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND t.date <= ?"
        params.append(date_to)
    if ticker:
        sql += " AND t.ticker LIKE ?"
        params.append(f"%{ticker.upper()}%")

    sql += " ORDER BY t.date DESC, t.imported_at DESC"
    if limit is not None and not open_only:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        d = row_to_dict(row)
        try:
            d['executions'] = json.loads(d.get('executions') or '[]')
        except Exception:
            d['executions'] = []
        if open_only and not _is_open_position(d):
            continue
        result.append(d)

    if limit is not None and open_only:
        result = result[:limit]

    return result


@app.post("/api/trades", status_code=201)
def create_trade(data: TradeCreate, conn: sqlite3.Connection = Depends(get_connection)):
    account = conn.execute("SELECT id FROM accounts WHERE id=?", (data.account_id,)).fetchone()
    if not account:
        raise ValueError(f"Account {data.account_id} not found")

    gross_pnl, net_pnl = compute_manual_pnl(
        data.side, data.entry_price, data.exit_price, data.quantity, data.commissions
    )

    # Build a manual trade group key
    trade_time = data.time or datetime.now().strftime("%H:%M:%S")
    trade_group = f"{data.date}_{data.ticker}_{data.instrument_type}_{trade_time.replace(':', '')}"

    execution = {
        'time': trade_time,
        'action': 'BOT' if data.side.upper() == 'LONG' else 'SOLD',
        'qty': data.quantity,
        'price': data.entry_price,
        'commission': data.commissions / 2,
    }
    if data.exit_price:
        execution2 = {
            'time': trade_time,
            'action': 'SOLD' if data.side.upper() == 'LONG' else 'BOT',
            'qty': data.quantity,
            'price': data.exit_price,
            'commission': data.commissions / 2,
        }
        executions = json.dumps([execution, execution2])
    else:
        executions = json.dumps([execution])

    cursor = conn.execute("""
        INSERT INTO trades
            (account_id, trade_group, date, ticker, instrument_type, side,
             gross_pnl, net_pnl, commissions, executions,
             option_expiry, option_strike, option_type, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data.account_id, trade_group, data.date, data.ticker.upper(),
        data.instrument_type.upper(), data.side.upper(),
        gross_pnl, net_pnl, data.commissions, executions,
        data.option_expiry, data.option_strike, data.option_type, 'manual'
    ))
    conn.commit()

    if data.strategy or data.stop_loss or data.notes:
        conn.execute("""
            INSERT INTO trade_analysis (trade_group, ticker, date, strategy, stop_loss, notes)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(trade_group) DO UPDATE SET
                strategy=excluded.strategy, stop_loss=excluded.stop_loss, notes=excluded.notes
        """, (trade_group, data.ticker.upper(), data.date, data.strategy, data.stop_loss, data.notes))
        conn.commit()

    row = conn.execute("SELECT * FROM trades WHERE id=?", (cursor.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.put("/api/trades/{trade_id}")
def update_trade(trade_id: int, data: dict, conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")

    trade = row_to_dict(row)
    # Only update allowed fields
    allowed = {'ticker', 'side', 'gross_pnl', 'net_pnl', 'commissions', 'date',
               'instrument_type', 'option_expiry', 'option_strike', 'option_type'}
    updates = {k: v for k, v in data.items() if k in allowed}

    if trade.get('source') == 'imported':
        updates['source'] = 'edited'

    if updates:
        set_clause = ', '.join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE trades SET {set_clause} WHERE id=?",
            list(updates.values()) + [trade_id]
        )
        conn.commit()

    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    return row_to_dict(row)


def _recalculate_and_save(trade: dict, execs: list, conn, trade_id: int):
    """Recalculate P&L from executions and persist. Returns updated trade row dict."""
    side = trade['side']
    instrument = trade['instrument_type']
    ticker = trade['ticker']

    entry_fills = [e for e in execs if e['action'] == ('BOT' if side == 'LONG' else 'SOLD')]
    exit_fills  = [e for e in execs if e['action'] == ('SOLD' if side == 'LONG' else 'BOT')]

    entry_qty = sum(e['qty'] for e in entry_fills)
    exit_qty  = sum(e['qty'] for e in exit_fills)
    is_open   = (entry_qty != exit_qty) or exit_qty == 0

    if is_open:
        gross_pnl, net_pnl = 0.0, 0.0
    else:
        # Broker-reported profit wins when the export carried one. MT5 settles a
        # pair's legs into account currency itself — a JPY price difference times
        # contract size lands on 50,000, but the statement says 332.23 — so
        # recomputing from prices would contradict the broker's own figures.
        reported = [e for e in exit_fills if e.get('reported_profit') is not None]
        if exit_fills and len(reported) == len(exit_fills):
            gross_pnl = sum(e['reported_profit'] for e in reported)
            commissions_total = sum(e.get('commission', 0) for e in execs)
            net_pnl = round(gross_pnl - commissions_total, 2)
            gross_pnl = round(gross_pnl, 2)
        else:
            avg_entry = sum(e['qty'] * e['price'] for e in entry_fills) / entry_qty
            avg_exit  = sum(e['qty'] * e['price'] for e in exit_fills)  / exit_qty
            multiplier = instruments.units_per_lot(instrument, ticker, FUTURES_MULTIPLIERS)
            if multiplier is None:
                # Unknown futures point value: refusing beats understating /ES 50x.
                raise ValueError(
                    f"no known point value for {ticker}; add a multiplier before "
                    f"this trade can be recomputed"
                )
            gross_pnl = (avg_entry - avg_exit if side == 'SHORT' else avg_exit - avg_entry) * entry_qty * multiplier
            commissions_total = sum(e.get('commission', 0) for e in execs)
            net_pnl   = round(gross_pnl - commissions_total, 2)
            gross_pnl = round(gross_pnl, 2)

    commissions = round(sum(e.get('commission', 0) for e in execs), 2)

    # Attribute closed trade to the last exit fill's date
    trade_date = trade['date']
    if not is_open and exit_fills:
        sorted_exits = sorted(exit_fills, key=lambda e: (e.get('date', ''), e.get('time', '')))
        trade_date = sorted_exits[-1].get('date', trade['date'])

    conn.execute(
        "UPDATE trades SET executions=?, gross_pnl=?, net_pnl=?, commissions=?, date=? WHERE id=?",
        (json.dumps(execs), gross_pnl, net_pnl, commissions, trade_date, trade_id)
    )
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())


def _parse_exec_body(body: dict, fallback_date: str) -> dict:
    return {
        'date': body.get('date', fallback_date),
        'time': body.get('time', ''),
        'action': body['action'].upper(),
        'qty': int(body['qty']),
        'price': float(body['price']),
        'commission': float(body.get('commission', 0)),
    }


@app.post("/api/trades/{trade_id}/executions")
def add_execution(trade_id: int, body: dict, conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")
    trade = row_to_dict(row)
    execs = json.loads(trade.get('executions') or '[]')
    execs.append(_parse_exec_body(body, trade['date']))
    return _recalculate_and_save(trade, execs, conn, trade_id)


@app.put("/api/trades/{trade_id}/executions/{exec_idx}")
def update_execution(trade_id: int, exec_idx: int, body: dict, conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")
    trade = row_to_dict(row)
    execs = json.loads(trade.get('executions') or '[]')
    if exec_idx < 0 or exec_idx >= len(execs):
        raise HTTPException(status_code=404, detail="Execution index out of range")
    execs[exec_idx] = _parse_exec_body(body, trade['date'])
    return _recalculate_and_save(trade, execs, conn, trade_id)


@app.delete("/api/trades/{trade_id}/executions/{exec_idx}")
def delete_execution(trade_id: int, exec_idx: int, conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")
    trade = row_to_dict(row)
    execs = json.loads(trade.get('executions') or '[]')
    if exec_idx < 0 or exec_idx >= len(execs):
        raise HTTPException(status_code=404, detail="Execution index out of range")
    execs.pop(exec_idx)
    return _recalculate_and_save(trade, execs, conn, trade_id)


@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int, conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")

    trade = row_to_dict(row)
    trade_group = trade['trade_group']

    conn.execute("DELETE FROM trade_tags WHERE trade_group=?", (trade_group,))
    conn.execute("DELETE FROM trade_analysis WHERE trade_group=?", (trade_group,))
    # Files go too — leaving them would orphan bytes on disk that no row names.
    attachments.remove_for_trade(conn, upload_dir=UPLOAD_DIR, trade_group=trade_group)
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()

    return {"deleted": True, "id": trade_id}


# ── Trade attachments ─────────────────────────────────────────────────────────
# Files attached to one trade's review. Storage rules (allowlist, generated
# filename, size cap, path containment) live in attachments.py.

def _attachment_trade(conn, trade_group: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, trade_group FROM trades WHERE trade_group=? LIMIT 1", (trade_group,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")
    return row


@app.get("/api/trades/{trade_group:path}/attachments")
def list_trade_attachments(trade_group: str, conn: sqlite3.Connection = Depends(get_connection)):
    _attachment_trade(conn, trade_group)
    return {"attachments": attachments.list_for(conn, trade_group)}


@app.post("/api/trades/{trade_group:path}/attachments", status_code=201)
async def upload_trade_attachment(
    trade_group: str,
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_connection),
):
    _attachment_trade(conn, trade_group)
    # Read the whole file rather than streaming it: the limit must be enforced
    # before a byte reaches disk, so an oversized upload is rejected outright
    # rather than written and then trimmed.
    raw = await file.read()
    return attachments.save(
        conn,
        upload_dir=UPLOAD_DIR,
        trade_group=trade_group,
        filename=file.filename,
        content_type=file.content_type,
        raw=raw,
    )


@app.get("/api/attachments/{attachment_id}/download")
def download_trade_attachment(
    attachment_id: int,
    inline: bool = False,
    conn: sqlite3.Connection = Depends(get_connection),
):
    meta = attachments.get(conn, attachment_id)
    path = attachments.attachment_path(UPLOAD_DIR, meta["stored_name"])
    if not path.is_file():
        raise FileNotFoundError(f"Attachment file for {meta['original_name']} is missing")

    # `inline` is a request hint, not a licence: only extensions the browser can
    # actually render may leave this route inside a page. Everything else falls
    # back to an attachment, which is the safe default (and the download dialog
    # the user expects for a spreadsheet or a Word file).
    show_inline = inline and attachments.is_inline_preview(meta["original_name"])
    headers = {"X-Content-Type-Options": "nosniff"}
    if show_inline and (meta.get("content_type") or "").startswith("text/"):
        # Rendered inside a sandboxed iframe; pin that down server-side too, so
        # a .csv or .md that begins with markup still cannot run script.
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    return FileResponse(
        path,
        media_type=meta["content_type"],
        filename=meta["original_name"],          # also drives media_type fallback
        headers=headers,
        content_disposition_type="inline" if show_inline else "attachment",
    )


@app.delete("/api/attachments/{attachment_id}")
def delete_trade_attachment(attachment_id: int, conn: sqlite3.Connection = Depends(get_connection)):
    attachments.remove(conn, upload_dir=UPLOAD_DIR, attachment_id=attachment_id)
    return {"deleted": True, "id": attachment_id}


@app.get("/api/trades/{trade_group:path}/analysis")
def get_trade_analysis(trade_group: str, conn: sqlite3.Connection = Depends(get_connection)):
    analysis = conn.execute(
        "SELECT * FROM trade_analysis WHERE trade_group=?", (trade_group,)
    ).fetchone()

    tags = conn.execute(
        "SELECT * FROM trade_tags WHERE trade_group=?", (trade_group,)
    ).fetchall()

    return {
        "analysis": row_to_dict(analysis) if analysis else None,
        "tags": [row_to_dict(t) for t in tags],
    }


class AnalysisUpdate(BaseModel):
    strategy: str | None = None
    idea_source: str | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    r_multiple: float | None = None
    emotional_state: str | None = None
    entry_reason: str | None = None
    exit_reason: str | None = None
    mistakes: str | None = None
    notes: str | None = None
    # Match review: setting a confidence is how the user vouches for (or clears)
    # a diary-to-trade match, so `manual` is accepted here although nothing in
    # the AI path ever writes it.
    match_confidence: str | None = None
    match_notes: str | None = None


# The model's own verdict on a match, so it survives an override for auditing.
MATCH_LEVELS = {"high", "medium", "low", "ambiguous", "unmatched", "manual"}


@app.patch("/api/trades/{trade_group:path}/analysis")
def update_trade_analysis(trade_group: str, data: AnalysisUpdate, conn: sqlite3.Connection = Depends(get_connection)):
    trade = conn.execute("SELECT trade_group, ticker, date FROM trades WHERE trade_group=?", (trade_group,)).fetchone()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    updates = data.model_dump(exclude_unset=True)
    if updates.get("match_confidence") is not None and updates["match_confidence"] not in MATCH_LEVELS:
        raise HTTPException(status_code=422, detail="Unknown match confidence")

    existing = conn.execute("SELECT id FROM trade_analysis WHERE trade_group=?", (trade_group,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO trade_analysis (trade_group, ticker, date) VALUES (?,?,?)",
            (trade_group, trade["ticker"], trade["date"])
        )

    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE trade_analysis SET {set_clause} WHERE trade_group=?",
            list(updates.values()) + [trade_group]
        )

    conn.commit()
    row = conn.execute("SELECT * FROM trade_analysis WHERE trade_group=?", (trade_group,)).fetchone()
    return row_to_dict(row) if row else {}


class TagCreate(BaseModel):
    tag_type: str
    tag_value: str


@app.post("/api/trades/{trade_group:path}/tags", status_code=201)
def add_trade_tag(trade_group: str, data: TagCreate, conn: sqlite3.Connection = Depends(get_connection)):
    trade = conn.execute("SELECT trade_group FROM trades WHERE trade_group=?", (trade_group,)).fetchone()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    cursor = conn.execute(
        "INSERT INTO trade_tags (trade_group, tag_type, tag_value, source) VALUES (?,?,?,'manual')",
        (trade_group, data.tag_type, data.tag_value)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM trade_tags WHERE id=?", (cursor.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.get("/api/analysis-options")
def get_analysis_options(conn: sqlite3.Connection = Depends(get_connection)):
    strategies = conn.execute(
        "SELECT DISTINCT strategy FROM trade_analysis WHERE strategy IS NOT NULL ORDER BY strategy"
    ).fetchall()
    idea_sources = conn.execute(
        "SELECT DISTINCT idea_source FROM trade_analysis WHERE idea_source IS NOT NULL ORDER BY idea_source"
    ).fetchall()
    used_strategies = [r["strategy"] for r in strategies]
    used_sources = [r["idea_source"] for r in idea_sources]
    return {
        "strategies": sorted(set(used_strategies) | set(library_names(conn, "strategy")), key=str.lower),
        "idea_sources": sorted(set(used_sources) | set(library_names(conn, "source")), key=str.lower),
    }


@app.delete("/api/trade-tags/{tag_id}")
def delete_trade_tag(tag_id: int, conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute("SELECT id FROM trade_tags WHERE id=?", (tag_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tag not found")
    conn.execute("DELETE FROM trade_tags WHERE id=?", (tag_id,))
    conn.commit()
    return {"deleted": True, "id": tag_id}


# ── Backup, restore, export ───────────────────────────────────────────────────
# Everything here stays local: the archive is written to a folder you choose on
# this machine, and restore only ever extracts into a *new* folder so the running
# journal is never replaced by an operation a button could have fumbled.

SETTINGS_KEY_BACKUP_FOLDER = "backup_folder"
EXPORT_TEMP_DIR = Path(tempfile.gettempdir()) / "tdjournal-export"


class BackupDestinationBody(BaseModel):
    folder: str


def _backup_folder(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE account_id = 0 AND key = ?",
        (SETTINGS_KEY_BACKUP_FOLDER,),
    ).fetchone()
    return row["value"] if row and row["value"] else ""


def _resolve_backup_folder(folder: str) -> Path:
    """Validate a user-entered path. Raises ValueError so it maps to a 400."""
    if not folder or not folder.strip():
        raise ValueError("Choose a folder for backups first.")
    path = Path(folder.strip()).expanduser()
    if path.exists() and not path.is_dir():
        raise ValueError(f"Not a folder: {path}")
    if not path.is_dir():
        raise ValueError(f"Folder does not exist: {path}")
    # The folder must be writable, checked by trying rather than guessing —
    # a destination that will fail only at the end of a long archive is worse
    # than one that refuses immediately.
    try:
        probe = backup.unique_path(path, ".tdjournal-write-probe")
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"That folder is not writable: {exc}") from exc
    return path


@app.get("/api/backup/destination")
def get_backup_destination(conn: sqlite3.Connection = Depends(get_connection)):
    folder = _backup_folder(conn)
    return {
        "folder": folder,
        "set": bool(folder),
        "exists": bool(folder) and Path(folder).expanduser().is_dir(),
    }


@app.put("/api/backup/destination")
def put_backup_destination(
    body: BackupDestinationBody,
    conn: sqlite3.Connection = Depends(get_connection),
):
    # Validated here rather than only on use: a stale path remembered from a
    # deleted folder should be reported when it is entered, not at backup time.
    _resolve_backup_folder(body.folder)
    conn.execute(
        """INSERT INTO settings (account_id, key, value) VALUES (0, ?, ?)
           ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value""",
        (SETTINGS_KEY_BACKUP_FOLDER, body.folder.strip()),
    )
    conn.commit()
    return get_backup_destination(conn=conn)


@app.get("/api/backup/archives")
def list_backup_archives(conn: sqlite3.Connection = Depends(get_connection)):
    folder = _backup_folder(conn)
    if not folder or not Path(folder).expanduser().is_dir():
        return {"folder": folder, "archives": []}
    target = Path(folder).expanduser()
    archives = []
    for path in sorted(target.glob("*.zip"), reverse=True):
        try:
            info = backup.inspect_archive(path)
            manifest = info["manifest"]
        except backup.RestoreError as exc:
            # Listed anyway, marked unreadable: silently hiding a corrupt file
            # would leave the user believing they have a backup they cannot use.
            manifest, corrupt = {}, str(exc)
        else:
            corrupt = None
        archives.append({
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "created_at": manifest.get("created_at"),
            "alembic_revision": manifest.get("alembic_revision"),
            "tables": manifest.get("tables", {}),
            "attachment_files": manifest.get("attachment_files"),
            "error": corrupt,
        })
    return {"folder": str(target), "archives": archives}


@app.post("/api/backup")
def create_backup(conn: sqlite3.Connection = Depends(get_connection)):
    folder = _resolve_backup_folder(_backup_folder(conn))
    path = backup.create_backup(
        db_path=database.DB_PATH, upload_dir=UPLOAD_DIR, destination=folder,
    )
    return {
        "created": str(path),
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "folder": str(folder),
    }


class RestoreBody(BaseModel):
    name: str


@app.post("/api/backup/restore")
def restore_backup(body: RestoreBody, conn: sqlite3.Connection = Depends(get_connection)):
    """Extract an archive into a *new* folder beside the backup.

    Deliberately never writes over the live journal: restoring is something you
    do to inspect or recover a copy, and replacing the running database in the
    same action that could have been a mis-click is not a trade worth making.
    The path is matched by name against the listing rather than joined to user
    input, so `../../x` cannot select a file outside the backup folder.
    """
    folder = _resolve_backup_folder(_backup_folder(conn))
    folder_resolved = folder.resolve()
    if ".." in Path(body.name).parts or Path(body.name).name != body.name:
        raise ValueError("Backup name must be a plain file name.")

    archive = (folder / body.name).resolve()
    if folder_resolved not in archive.parents or archive.suffix.lower() != ".zip":
        raise ValueError("That backup is not in your backup folder.")

    target = backup.unique_path(folder, archive.stem + "-restored")
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise ValueError(f"Restore folder already exists and is not empty: {target.name}")
    if target.resolve() == Path(database.DB_PATH).resolve().parent.resolve():
        raise ValueError("Refusing to restore over the running journal folder.")

    result = backup.extract_archive(archive, target)
    result["folder"] = str(target)
    result["name"] = target.name
    return result


@app.get("/api/export")
def export_journal(fmt: str = "csv", conn: sqlite3.Connection = Depends(get_connection)):
    """Every table as CSV (a zip) or JSON, written then streamed out."""
    if fmt not in ("csv", "json"):
        raise ValueError("Format must be csv or json.")
    EXPORT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = EXPORT_TEMP_DIR / f"tdjournal-export-{stamp}.{'zip' if fmt == 'csv' else 'json'}"
    try:
        backup.write_export(conn, destination, fmt=fmt)
    except OSError as exc:
        raise ValueError(f"Export could not be written: {exc}") from exc
    media = "application/zip" if fmt == "csv" else "application/json"
    return FileResponse(
        destination, media_type=media, filename=destination.name,
        headers={"X-Content-Type-Options": "nosniff"},
        background=BackgroundTask(lambda: destination.unlink(missing_ok=True)),
    )


# ── KPIs ───────────────────────────────────────────────────────────────────────


def _excursion_kpis(conn, account_id=None, date_from=None, date_to=None) -> dict:
    """Aggregate trade-quality metrics: how much of the move was captured, and
    how much heat was taken to get it.

    exit_efficiency is averaged over WINNERS only — a loser has no favourable
    excursion to capture, so including them would measure something else.
    MAE is reported separately for winners and losers because the gap between
    them is what calibrates the stop.
    """
    sql = ("SELECT net_pnl, mfe_pct, mae_pct, exit_efficiency FROM trades "
           "WHERE mfe_pct IS NOT NULL "
           "AND net_pnl IS NOT NULL AND net_pnl <> 0")
    params = []
    if account_id is not None:
        sql += " AND account_id = ?"; params.append(account_id)
    if date_from:
        sql += " AND date >= ?"; params.append(date_from)
    if date_to:
        sql += " AND date <= ?"; params.append(date_to)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return {}
    wins = [r for r in rows if r['net_pnl'] > 0]
    losses = [r for r in rows if r['net_pnl'] <= 0]

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def med(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        n = len(vals)
        return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 2)

    return {
        "exit_efficiency": avg([r['exit_efficiency'] for r in wins]),
        "exit_efficiency_median": med([r['exit_efficiency'] for r in wins]),
        "avg_mfe": avg([r['mfe_pct'] for r in rows]),
        "avg_mae": avg([r['mae_pct'] for r in rows]),
        "avg_mae_win": avg([r['mae_pct'] for r in wins]),
        "avg_mae_loss": avg([r['mae_pct'] for r in losses]),
        "excursion_n": len(rows),
    }


@app.get("/api/kpis")
def get_kpis(
    account_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    sql = "SELECT * FROM trades WHERE 1=1"
    params = []

    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    if date_from:
        sql += " AND date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND date <= ?"
        params.append(date_to)

    rows = conn.execute(sql + " ORDER BY date", params).fetchall()
    trades = [row_to_dict(r) for r in rows]

    total_net_pnl = sum(t.get('net_pnl') or 0 for t in trades)
    total_gross_pnl = sum(t.get('gross_pnl') or 0 for t in trades)
    total_commissions = sum(t.get('commissions') or 0 for t in trades)

    winners = [t for t in trades if (t.get('net_pnl') or 0) > 0]
    losers = [t for t in trades if (t.get('net_pnl') or 0) < 0]
    total_trades = len(trades)
    win_rate = round(len(winners) / total_trades * 100, 2) if total_trades else 0

    avg_win = round(sum(t['net_pnl'] for t in winners) / len(winners), 2) if winners else 0
    avg_loss = round(sum(t['net_pnl'] for t in losers) / len(losers), 2) if losers else 0

    gross_wins = sum(t.get('gross_pnl') or 0 for t in winners)
    gross_losses = abs(sum(t.get('gross_pnl') or 0 for t in losers))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses else None

    # Expectancy = win_rate * avg_win + loss_rate * avg_loss (avg_loss is negative)
    if total_trades > 0:
        expectancy = round(
            (len(winners) / total_trades) * avg_win + (len(losers) / total_trades) * avg_loss, 2
        )
    else:
        expectancy = 0.0

    # Daily P&L
    daily: dict[str, float] = {}
    for t in trades:
        d = t.get('date', '')
        daily[d] = daily.get(d, 0) + (t.get('net_pnl') or 0)

    trading_days = len(daily)
    positive_days = sum(1 for v in daily.values() if v > 0)
    day_win_rate = round(positive_days / trading_days * 100, 1) if trading_days else 0

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    daily_pnl = []
    for date in sorted(daily.keys()):
        cumulative += daily[date]
        if cumulative > peak:
            peak = cumulative
        dd = cumulative - peak
        if dd < max_drawdown:
            max_drawdown = dd
        daily_pnl.append({
            "date": date,
            "net_pnl": round(daily[date], 2),
            "cumulative": round(cumulative, 2),
        })

    # By instrument type
    by_instrument: dict[str, dict] = {}
    for t in trades:
        inst = t.get('instrument_type', 'STOCK')
        if inst not in by_instrument:
            by_instrument[inst] = {'net_pnl': 0, 'count': 0, 'wins': 0}
        by_instrument[inst]['net_pnl'] += t.get('net_pnl') or 0
        by_instrument[inst]['count'] += 1
        if (t.get('net_pnl') or 0) > 0:
            by_instrument[inst]['wins'] += 1

    # By strategy (join with trade_analysis)
    # Strategy breakdown = playbook setup tag first, diary strategy as fallback.
    # The label is resolved in an inner query so GROUP BY cannot bind to the
    # underlying ta.strategy column instead of the resolved alias.
    strategy_sql = """
        SELECT label as strategy,
               COUNT(*) as count,
               SUM(net_pnl) as total_pnl,
               SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
               AVG(r_multiple) as avg_r
        FROM (
            SELECT COALESCE(
                       CASE WHEN t.setup IS NOT NULL AND t.setup <> 'NONE'
                            THEN t.setup END,
                       ta.strategy
                   ) as label,
                   t.net_pnl as net_pnl,
                   ta.r_multiple as r_multiple,
                   t.account_id as account_id
            FROM trades t
            LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group
            WHERE t.net_pnl IS NOT NULL AND t.net_pnl != 0
        ) sub
        WHERE label IS NOT NULL
    """
    strat_params = []
    if account_id is not None:
        strategy_sql += " AND account_id = ?"
        strat_params.append(account_id)
    strategy_sql += " GROUP BY label ORDER BY total_pnl DESC"

    strat_rows = conn.execute(strategy_sql, strat_params).fetchall()
    by_strategy = []
    for r in strat_rows:
        r = dict(r)
        count = r['count']
        by_strategy.append({
            "strategy": r['strategy'],
            "net_pnl": round(r['total_pnl'] or 0, 2),
            "win_rate": round(r['wins'] / count * 100, 1) if count else 0,
            "count": count,
            "avg_r": round(r['avg_r'] or 0, 2),
        })

    return {
        "total_net_pnl": round(total_net_pnl, 2),
        "total_gross_pnl": round(total_gross_pnl, 2),
        "total_commissions": round(total_commissions, 2),
        "total_trades": total_trades,
        "winning_trades": len(winners),
        "losing_trades": len(losers),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "trading_days": trading_days,
        "positive_days": positive_days,
        "day_win_rate": day_win_rate,
        "daily_pnl": daily_pnl,
        "by_instrument": by_instrument,
        "by_strategy": by_strategy,
        "expectancy": expectancy,
        "max_drawdown": round(max_drawdown, 2),
        # Equity ratios (item 13): capital + cash flows against the P&L above.
        # Percentages are None when the account has no starting balance, so the
        # caller shows "—" instead of inventing a denominator.
        **equity.summarize(conn, account_id=account_id, date_from=date_from,
                           total_net_pnl=total_net_pnl, max_drawdown=max_drawdown,
                           daily_pnl=daily_pnl),
        **_excursion_kpis(conn, account_id, date_from, date_to),
    }


class CashFlowCreate(BaseModel):
    kind: str
    amount: float
    flow_date: str
    note: str | None = None


class CashFlowUpdate(BaseModel):
    kind: str | None = None
    amount: float | None = None
    flow_date: str | None = None
    note: str | None = None


def _checked_iso_date(value: str) -> str:
    if not _DIARY_DATE_RE.fullmatch(value):
        raise ValueError("flow_date must be a calendar date as YYYY-MM-DD.")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("flow_date must be a real calendar date.") from exc
    return value


def _checked_cash_flow(kind: str | None, amount: float | None,
                       flow_date: str | None) -> None:
    if kind is not None and kind not in ("deposit", "withdrawal"):
        raise ValueError("kind must be 'deposit' or 'withdrawal'.")
    if amount is not None and amount <= 0:
        raise ValueError("amount must be greater than 0; the kind sets the direction.")
    if flow_date is not None:
        _checked_iso_date(flow_date)


@app.get("/api/accounts/{account_id}/cash-flows")
def list_cash_flows(account_id: int, conn: sqlite3.Connection = Depends(get_connection)):
    if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Account not found")
    rows = conn.execute(
        "SELECT id, account_id, kind, amount, flow_date, note, created_at "
        "FROM account_cash_flows WHERE account_id=? ORDER BY flow_date, id",
        (account_id,),
    ).fetchall()
    flows = [row_to_dict(r) for r in rows]
    return {"flows": flows,
            "net_flows": round(sum(f["amount"] if f["kind"] == "deposit" else -f["amount"]
                                    for f in flows), 2)}


@app.post("/api/accounts/{account_id}/cash-flows", status_code=201)
def create_cash_flow(account_id: int, data: CashFlowCreate,
                     conn: sqlite3.Connection = Depends(get_connection)):
    if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Account not found")
    _checked_cash_flow(data.kind, data.amount, data.flow_date)
    cur = conn.execute(
        "INSERT INTO account_cash_flows (account_id, kind, amount, flow_date, note) "
        "VALUES (?,?,?,?,?)",
        (account_id, data.kind, data.amount, data.flow_date, data.note),
    )
    conn.commit()
    return row_to_dict(conn.execute(
        "SELECT id, account_id, kind, amount, flow_date, note, created_at "
        "FROM account_cash_flows WHERE id=?", (cur.lastrowid,)).fetchone())


@app.put("/api/accounts/{account_id}/cash-flows/{flow_id}")
def update_cash_flow(account_id: int, flow_id: int, data: CashFlowUpdate,
                     conn: sqlite3.Connection = Depends(get_connection)):
    row = conn.execute(
        "SELECT * FROM account_cash_flows WHERE id=? AND account_id=?", (flow_id, account_id)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Cash flow not found")
    updates = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        return row_to_dict(row)
    merged = {k: updates.get(k, row[k]) for k in ("kind", "amount", "flow_date")}
    _checked_cash_flow(merged["kind"], merged["amount"], merged["flow_date"])
    conn.execute(
        "UPDATE account_cash_flows SET " + ", ".join(f"{k}=?" for k in updates) + " WHERE id=?",
        [*updates.values(), flow_id],
    )
    conn.commit()
    return row_to_dict(conn.execute(
        "SELECT id, account_id, kind, amount, flow_date, note, created_at "
        "FROM account_cash_flows WHERE id=?", (flow_id,)).fetchone())


@app.delete("/api/accounts/{account_id}/cash-flows/{flow_id}")
def delete_cash_flow(account_id: int, flow_id: int,
                     conn: sqlite3.Connection = Depends(get_connection)):
    cur = conn.execute(
        "DELETE FROM account_cash_flows WHERE id=? AND account_id=?", (flow_id, account_id)
    )
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="Cash flow not found")
    conn.commit()
    return {"deleted": True, "id": flow_id}


# ── Diary Upload ───────────────────────────────────────────────────────────────

# .heic/.heif are what an iPhone produces by default — a photo of handwritten
# notes taken on the phone lands here. They are converted to JPEG on upload
# because the vision API does not accept HEIC.
ALLOWED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.heic', '.heif'}
ALLOWED_TEXT_EXTENSIONS = {'.txt', '.csv'}
ALLOWED_DIARY_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_TEXT_EXTENSIONS

# ISO date, zero-padded and fixed width, so it cannot carry a path segment and
# cannot be stored in a form entry_date would never match against.
_DIARY_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@app.post("/api/upload-diary")
async def upload_diary(
    date: str = Form(...),
    account_id: int = Form(...),
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_connection),
):
    # Checked before the file is read or written: with diary analysis off, the
    # endpoint's whole purpose (upload → Claude → entry) must not happen at all,
    # so the notice can honestly say nothing was saved and nothing was sent.
    require_feature(conn, "diary")
    # `date` is the first component of the stored path, so it is validated like
    # the value it is rather than trusted because a browser sent it. Before this,
    # date="../../x" wrote outside UPLOAD_DIR — proven by a test upload that
    # landed a file in the temp dir's parent. The exact-width match is there for
    # more than the path: entry_date is stored verbatim and compared to trade
    # dates, so `2026-7-1` would import fine and then match no trade. strptime
    # adds the calendar check (2026-02-30 is refused); both raise → 400.
    if not _DIARY_DATE_RE.fullmatch(date):
        raise ValueError("date must be a calendar date as YYYY-MM-DD.")
    datetime.strptime(date, "%Y-%m-%d")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_DIARY_EXTENSIONS:
        raise ValueError(f"File must be one of {ALLOWED_DIARY_EXTENSIONS}")

    account = conn.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not account:
        raise ValueError(f"Account {account_id} not found")

    # Save image file. The uploaded name is data, not a path: strip anything
    # that could act as one (same helper attachments.py uses for its own display
    # names) and re-append the extension already checked against the allowlist,
    # so the frontend can still recognise an image from image_path.
    stem = Path(attachments.sanitize_display_name(file.filename)).stem or "image"
    safe_name = f"{date}_{account_id}_{stem}{ext}"
    save_path = Path(UPLOAD_DIR) / safe_name

    raw = await file.read()

    # iPhone photos arrive as HEIC, which the vision API cannot read. Convert to
    # JPEG on the way in so a phone snap of handwritten notes just works.
    if ext in {'.heic', '.heif'}:
        try:
            import io
            import pillow_heif
            from PIL import Image as PILImage
            pillow_heif.register_heif_opener()
            img = PILImage.open(io.BytesIO(raw)).convert('RGB')
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=90)
            raw = buf.getvalue()
            ext = '.jpg'
            safe_name = str(Path(safe_name).with_suffix('.jpg'))
            save_path = Path(UPLOAD_DIR) / safe_name
        except Exception as exc:
            raise ValueError(
                "Could not convert this HEIC photo. On iPhone, Settings > Camera > "
                f"Formats > Most Compatible saves as JPEG instead. ({exc})")

    # Belt and braces: resolve and require the result to still be inside
    # UPLOAD_DIR, exactly as attachments.attachment_path() does for its own
    # files. Both inputs are already validated, so this can only fire if a
    # future edit re-introduces a path segment — and then it fires as a
    # ValueError (400) instead of a silent write or an open() ENOENT (404).
    uploads_root = Path(UPLOAD_DIR).resolve()
    resolved = save_path.resolve()
    if resolved.parent != uploads_root:
        raise ValueError("That filename cannot be stored safely.")

    async with aiofiles.open(save_path, 'wb') as f:
        await f.write(raw)

    # Insert diary entry row
    cursor = conn.execute(
        "INSERT INTO diary_entries (account_id, entry_date, image_path) VALUES (?,?,?)",
        (account_id, date, safe_name)
    )
    conn.commit()
    diary_entry_id = cursor.lastrowid

    # Build trades context for Claude
    trades_context = build_trades_context(conn, date, account_id)

    # Call Claude — image vision or text depending on file type
    analysis_error = None
    analysis = None
    try:
        if ext in ALLOWED_TEXT_EXTENSIONS:
            text_content = raw.decode('utf-8', errors='replace')
            analysis = analyze_diary_text(text_content, date, trades_context)
        else:
            analysis = analyze_diary_entry(str(save_path.absolute()), date, trades_context)
        analysis = apply_aliases(conn, analysis)
        # Judge the matches against this date's actual trades and store the
        # fields as data — both have to happen before the analysis is saved,
        # because they change what gets written.
        analysis = analyse_confidence(analysis, trades_context)
        # Persist analysis
        conn.execute(
            "UPDATE diary_entries SET ai_analysis=? WHERE id=?",
            (json.dumps(analysis), diary_entry_id)
        )
        conn.commit()
        save_analysis_to_db(conn, diary_entry_id, analysis)
    except Exception as e:
        analysis_error = str(e)

    diary_row = conn.execute("SELECT * FROM diary_entries WHERE id=?", (diary_entry_id,)).fetchone()
    result = row_to_dict(diary_row)

    if analysis_error:
        result['analysis_error'] = analysis_error
    else:
        result['trade_count'] = len(analysis.get('trade_analyses', [])) if analysis else 0

    return result


# ── Diary List ─────────────────────────────────────────────────────────────────

@app.get("/api/diary")
def list_diary(
    account_id: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    sql = "SELECT * FROM diary_entries WHERE 1=1"
    params = []
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " ORDER BY entry_date DESC"

    rows = conn.execute(sql, params).fetchall()
    result = []

    # The stored diary JSON is a record of what the model said at upload time;
    # trade_analysis is the live row a user edits in TradeDetail (and a diary
    # upload writes it too). The live copy is projected over the record only when
    # the row belongs to this entry — a row created some other way, or already
    # rewritten by a later diary, says nothing about this analysis, and taking
    # its NULLs at face value would erase what the model found. Within its own
    # entry it wins outright, NULL included: a field you cleared stays cleared,
    # and the model's own match verdict survives as model_match_confidence.
    EDITED_FIELDS = ("strategy", "idea_source", "stop_loss", "target_price", "r_multiple",
                     "emotional_state", "entry_reason", "exit_reason", "mistakes", "notes")
    MATCH_FIELDS = ("match_confidence", "match_notes")
    current: dict[str, dict] = {}
    if rows:
        cols = [f"{f} AS live_{f}" for f in EDITED_FIELDS] + list(MATCH_FIELDS)
        cols.append("diary_entry_id")
        for r in conn.execute(
            "SELECT trade_group, " + ", ".join(cols) + " FROM trade_analysis"
        ).fetchall():
            current[r["trade_group"]] = dict(r)

    for row in rows:
        d = row_to_dict(row)
        try:
            analysis = json.loads(d['ai_analysis']) if d.get('ai_analysis') else None
        except Exception:
            analysis = None
        if isinstance(analysis, dict):
            for ta in analysis.get('trade_analyses') or []:
                if not isinstance(ta, dict) or not ta.get('trade_group'):
                    continue
                live = current.get(ta['trade_group'])
                if not live or live.get('diary_entry_id') != d['id']:
                    continue
                if live.get("match_confidence") and live["match_confidence"] != ta.get("match_confidence"):
                    ta.setdefault('model_match_confidence', ta.get('match_confidence'))
                    ta.setdefault('model_match_notes', ta.get('match_notes'))
                    ta['match_confidence'] = live['match_confidence']
                    ta['match_notes'] = live.get('match_notes') or ta.get('match_notes')
                for field in EDITED_FIELDS:
                    ta[field] = live[f'live_{field}']
        d['ai_analysis'] = analysis
        queue = queued_for_review(analysis) if analysis else []
        d['review_queue'] = [
            {k: ta.get(k) for k in
             ('ticker', 'trade_group', 'match_confidence', 'match_notes',
              'model_match_confidence', 'model_match_notes')}
            for ta in queue
        ]
        d['needs_review'] = len(queue)
        result.append(d)

    return result


@app.delete("/api/diary/by-date/{date}")
def delete_diary_by_date(
    date: str,
    account_id: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    where = "entry_date=?"
    params: list = [date]
    if account_id is not None:
        where += " AND account_id=?"
        params.append(account_id)
    # trade_analysis.diary_entry_id points back here, so unlink first: the
    # analysis (including anything edited by hand) stays on the trade.
    conn.execute(
        f"UPDATE trade_analysis SET diary_entry_id = NULL WHERE diary_entry_id IN "
        f"(SELECT id FROM diary_entries WHERE {where})", params)
    conn.execute(f"DELETE FROM diary_entries WHERE {where}", params)
    conn.commit()
    return {"ok": True}


@app.delete("/api/diary/{entry_id}")
def delete_diary_entry(entry_id: int, conn: sqlite3.Connection = Depends(get_connection)):
    # Unlink the analyses this entry produced, otherwise the foreign key blocks
    # the delete with a 500. The analysis stays on the trade.
    conn.execute("UPDATE trade_analysis SET diary_entry_id = NULL WHERE diary_entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM diary_entries WHERE id=?", (entry_id,))
    conn.commit()
    return {"ok": True}


# ── Chart Proxy ────────────────────────────────────────────────────────────────

ALPACA_KEY = os.getenv("APCA_API_KEY_ID", "")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
# "iex" works on a free Alpaca account; "sip" needs a paid market-data subscription.
# Default to iex so the chart works out of the box, regardless of which tier the
# viewer's key is on. Override with ALPACA_DATA_FEED=sip if you have the subscription.
ALPACA_DATA_FEED = os.getenv("ALPACA_DATA_FEED", "iex")

FUTURES_CHART_MAP = {
    '/ES': 'SPY', '/MES': 'SPY',
    '/NQ': 'QQQ', '/MNQ': 'QQQ',
    '/YM': 'DIA', '/MYM': 'DIA',
    '/RTY': 'IWM', '/M2K': 'IWM',
}


ALLOWED_CHART_TIMEFRAMES = {
    "1Min", "3Min", "5Min", "10Min", "15Min", "30Min", "1Hour", "1Day", "1Week",
}
# Daily/Weekly are a wide-context view around the trade, not the single RTH session
# the intraday timeframes use, so they get their own start/end/limit below.
_WIDE_RANGE_TIMEFRAMES = {"1Day", "1Week"}


async def _fetch_alpaca_bars(client, url, base_params, headers, max_bars=5000):
    """Follow Alpaca's next_page_token until exhausted or max_bars is hit.

    A single page caps at 1000 bars — a multi-day intraday request (the chart's
    zoom-out lazy-load) can easily exceed that, and Alpaca returns bars oldest
    first, so an unpaginated request would silently drop the most recent bars.
    """
    bars = []
    page_token = None
    while True:
        params = dict(base_params)
        if page_token:
            params["page_token"] = page_token
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        bars.extend(data.get("bars", []))
        page_token = data.get("next_page_token")
        if not page_token or len(bars) >= max_bars:
            break
    return bars


def _local_chart_bars(conn, ticker: str, date: str, timeframe: str,
                      days_back: int) -> dict | None:
    """Read and optionally aggregate imported M1 bars for a chart request.

    Returns None when the symbol has no local history (the caller may try the
    configured remote provider) and a normal chart response when it does. A
    window with no bars returns an empty local response, not None: don't fall
    through to a different price source for a symbol that the user is charting
    from their own broker data.

    The chart's existing endpoint contract is `t` (ISO UTC), `o/h/l/c/v/vw`.
    Imported M1 times are naive UTC strings, so append `Z` and do no offset
    correction in the browser. Higher timeframes are aggregated from M1 bars
    in UTC buckets; no synthetic candles are filled across absent data.
    """
    symbol = ticker.strip().upper()
    exists = conn.execute(
        "SELECT 1 FROM bars WHERE symbol = ? COLLATE NOCASE LIMIT 1", (symbol,)
    ).fetchone()
    if not exists:
        return None

    tf_minutes = {
        "1Min": 1, "3Min": 3, "5Min": 5, "10Min": 10,
        "15Min": 15, "30Min": 30, "1Hour": 60,
    }
    wide_days = {"1Day": 3650, "1Week": 5475}
    days_back = min(days_back, wide_days.get(timeframe, 90))

    try:
        end_dt = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        raise ValueError("Chart date must be YYYY-MM-DD")
    start_dt = end_dt - timedelta(days=days_back)
    start, end = start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT time, open, high, low, close, tick_volume FROM bars "
        "WHERE symbol = ? COLLATE NOCASE AND time >= ? AND time < ? ORDER BY time",
        (symbol, start, end),
    ).fetchall()

    raw = [dict(r) for r in rows]
    if timeframe == "1Week":
        bucket = "week"
    elif timeframe == "1Day":
        bucket = "day"
    else:
        bucket = tf_minutes.get(timeframe, 5)

    grouped = {}
    for bar in raw:
        stamp = bar["time"]
        dt = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        if bucket == "week":
            monday = (dt - timedelta(days=dt.weekday())).date()
            key_dt = datetime.combine(monday, datetime.min.time())
        elif bucket == "day":
            key_dt = datetime.combine(dt.date(), datetime.min.time())
        else:
            minutes = int(bucket)
            day_start = dt.replace(hour=0, minute=0, second=0)
            elapsed = dt.hour * 60 + dt.minute
            key_dt = day_start + timedelta(minutes=(elapsed // minutes) * minutes)
        key = key_dt.strftime("%Y-%m-%d %H:%M:%S")
        if key not in grouped:
            grouped[key] = {
                "t": key.replace(" ", "T") + "Z",
                "o": float(bar["open"]), "h": float(bar["high"]),
                "l": float(bar["low"]), "c": float(bar["close"]),
                "v": int(bar["tick_volume"] or 0),
            }
        else:
            agg = grouped[key]
            agg["h"] = max(agg["h"], float(bar["high"]))
            agg["l"] = min(agg["l"], float(bar["low"]))
            agg["c"] = float(bar["close"])
            agg["v"] += int(bar["tick_volume"] or 0)

    return {
        "ticker": symbol,
        "original_ticker": ticker,
        "date": date,
        "bars": list(grouped.values()),
        "source": "local",
        "time_basis": "UTC",
        "warning": None if grouped else f"No imported {symbol} bars in this date window.",
    }


@app.get("/api/chart/{ticker}/{date}")
async def get_chart(
    ticker: str, date: str,
    timeframe: str = Query("5Min"),
    days_back: int = Query(1, ge=1),
    conn: sqlite3.Connection = Depends(get_connection),
):
    tf = timeframe if timeframe in ALLOWED_CHART_TIMEFRAMES else "5Min"
    local = _local_chart_bars(conn, ticker, date, tf, days_back)
    if local is not None:
        return local

    if not ALPACA_KEY or ALPACA_KEY == "your_alpaca_api_key_here":
        return {
            "ticker": ticker, "date": date, "bars": [],
            "warning": "Add APCA_API_KEY_ID and APCA_API_SECRET_KEY to backend/.env to enable price charts."
        }

    # Normalize ticker
    if ticker.upper().startswith('/'):
        # Map futures to proxy ETF for charting
        alpaca_ticker = FUTURES_CHART_MAP.get(ticker.upper(), 'SPY')
    else:
        alpaca_ticker = ticker.upper()

    # Same knob as the intraday branch below (days_back widens the window when
    # the chart is zoomed out past what's loaded) — daily/weekly just start
    # from a much bigger default and cap much further out, since a decade of
    # daily bars is still only ~2500 rows.
    _WIDE_DAYS_BACK_CAP = {"1Day": 3650, "1Week": 5475}
    days_back = min(days_back, _WIDE_DAYS_BACK_CAP.get(tf, days_back)) if tf in _WIDE_RANGE_TIMEFRAMES else min(days_back, 90)

    url = f"https://data.alpaca.markets/v2/stocks/{alpaca_ticker}/bars"
    if tf in _WIDE_RANGE_TIMEFRAMES:
        trade_day = datetime.strptime(date, "%Y-%m-%d").date()
        start = trade_day - timedelta(days=days_back - 1)
        end = min(trade_day + timedelta(days=10), datetime.utcnow().date())
        params = {
            "timeframe": tf,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
            "feed": ALPACA_DATA_FEED,
            "adjustment": "raw",
        }
    else:
        # days_back widens the window backward (calendar days, weekends just come
        # back empty) so zooming out on the chart can load real prior sessions
        # instead of running off the edge of a single day's data.
        trade_day = datetime.strptime(date, "%Y-%m-%d").date()
        start_day = trade_day - timedelta(days=days_back - 1)
        params = {
            "timeframe": tf,
            "start": f"{start_day.isoformat()}T09:30:00-04:00",
            "end": f"{date}T16:00:00-04:00",
            "limit": 1000,
            "feed": ALPACA_DATA_FEED,
            "adjustment": "raw",
        }
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                raw_bars = await _fetch_alpaca_bars(client, url, params, headers)
            except httpx.HTTPStatusError as e:
                # A 403 on a non-iex feed means the key's tier doesn't carry that
                # feed's subscription. Retry once on iex, which every Alpaca
                # account (free included) can read, instead of failing the chart.
                if e.response.status_code == 403 and params["feed"] != "iex":
                    fallback_params = dict(params, feed="iex")
                    raw_bars = await _fetch_alpaca_bars(client, url, fallback_params, headers)
                else:
                    raise

        bars = []
        for bar in raw_bars:
            bars.append({
                "t": bar.get("t", ""),
                "o": bar.get("o", 0),
                "h": bar.get("h", 0),
                "l": bar.get("l", 0),
                "c": bar.get("c", 0),
                "v": bar.get("v", 0),
                "vw": bar.get("vw"),
            })

        return {"ticker": alpaca_ticker, "original_ticker": ticker, "date": date, "bars": bars}

    except httpx.HTTPStatusError as e:
        detail = "subscription required for this feed" if e.response.status_code == 403 else str(e.response.status_code)
        return {
            "ticker": ticker, "date": date, "bars": [],
            "warning": f"Alpaca API error: {detail}"
        }
    except Exception as e:
        return {
            "ticker": ticker, "date": date, "bars": [],
            "warning": f"Chart unavailable: {str(e)}"
        }


# ── Calendar ───────────────────────────────────────────────────────────────────

@app.get("/api/calendar")
def get_calendar(
    account_id: int | None = Query(None),
    year: int | None = Query(None),
    month: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    sql = "SELECT date, net_pnl FROM trades WHERE 1=1"
    params = []

    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    if year and month:
        date_from = f"{year:04d}-{month:02d}-01"
        next_month_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        date_to = f"{next_month_year:04d}-{next_month:02d}-01"
        sql += " AND date >= ? AND date < ?"
        params.extend([date_from, date_to])

    rows = conn.execute(sql, params).fetchall()

    day_stats: dict[str, dict] = {}
    for row in rows:
        d = row['date']
        pnl = row['net_pnl'] or 0
        if d not in day_stats:
            day_stats[d] = {'net_pnl': 0.0, 'trade_count': 0, 'winners': 0, 'losers': 0}
        day_stats[d]['net_pnl'] += pnl
        day_stats[d]['trade_count'] += 1
        if pnl > 0:
            day_stats[d]['winners'] += 1
        elif pnl < 0:
            day_stats[d]['losers'] += 1

    # Which days have diary entries
    diary_sql = "SELECT entry_date FROM diary_entries WHERE 1=1"
    diary_params = []
    if account_id is not None:
        diary_sql += " AND account_id = ?"
        diary_params.append(account_id)
    diary_rows = conn.execute(diary_sql, diary_params).fetchall()
    diary_dates = {r['entry_date'] for r in diary_rows}

    result = []
    for date, stats in sorted(day_stats.items()):
        count = stats['trade_count']
        result.append({
            'date': date,
            'net_pnl': round(stats['net_pnl'], 2),
            'trade_count': count,
            'winners': stats['winners'],
            'losers': stats['losers'],
            'win_rate': round(stats['winners'] / count * 100, 1) if count else 0,
            'has_diary': date in diary_dates,
        })

    return result


# ── Yearly KPIs ────────────────────────────────────────────────────────────────

@app.get("/api/yearly-kpis")
def get_yearly_kpis(
    year: int = Query(...),
    account_id: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    sql = "SELECT date, net_pnl, gross_pnl FROM trades WHERE strftime('%Y', date) = ?"
    params = [str(year)]
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)

    rows = conn.execute(sql + " ORDER BY date", params).fetchall()

    # Bucket trades by month
    from collections import defaultdict
    months: dict[int, list] = defaultdict(list)
    for row in rows:
        m = int(row["date"][5:7])
        months[m].append({"net_pnl": row["net_pnl"] or 0, "gross_pnl": row["gross_pnl"] or 0, "date": row["date"]})

    result = []
    for m in range(1, 13):
        trades = months.get(m, [])
        if not trades:
            result.append({"month": m, "has_data": False})
            continue

        winners = [t for t in trades if t["net_pnl"] > 0]
        losers  = [t for t in trades if t["net_pnl"] < 0]
        total   = len(trades)

        net_pnl       = sum(t["net_pnl"] for t in trades)
        avg_win        = sum(t["net_pnl"] for t in winners) / len(winners) if winners else 0
        avg_loss       = sum(t["net_pnl"] for t in losers)  / len(losers)  if losers  else 0
        gross_wins     = sum(t["gross_pnl"] for t in winners)
        gross_losses   = abs(sum(t["gross_pnl"] for t in losers))
        profit_factor  = gross_wins / gross_losses if gross_losses else None
        win_rate       = len(winners) / total * 100 if total else 0
        trading_days   = len(set(t["date"] for t in trades))
        positive_days  = len({t["date"] for t in trades if t["net_pnl"] > 0})
        day_win_rate   = positive_days / trading_days * 100 if trading_days else 0

        result.append({
            "month": m,
            "has_data": True,
            "net_pnl": round(net_pnl, 2),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "total_trades": total,
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "trading_days": trading_days,
            "day_win_rate": round(day_win_rate, 1),
        })

    return result


# ── Edge Report ────────────────────────────────────────────────────────────────

# ── Reports ───────────────────────────────────────────────────────────────────
# The standard breakdowns a trading journal is expected to answer: when do I
# trade well, what do I trade well, and how well do I execute. Every bucket
# returns the same shape so one frontend component renders all of them.

_DOW_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
_SETUP_LABEL_MAP = {'NONE': 'No setup'}
_HOLD_ORDER = ['0-5 min', '5-15 min', '15-30 min', '30-60 min', '1-2 hrs', '2+ hrs']
_HOUR_ORDER = [f"{h:02d}:00" for h in range(24)]


def _mins_of(t):
    """'09:45:12' -> minutes since midnight. None when unparseable."""
    if not t:
        return None
    try:
        p = str(t).split(':')
        return int(p[0]) * 60 + int(p[1])
    except Exception:
        return None


def _span_minutes(entry, exit_, trade_date):
    """Elapsed entry→exit minutes, using leg dates where available.

    Older manually-entered legs may have no date; in that case a backwards
    clock is treated as crossing midnight once. The MT5 importer carries leg
    dates, so multi-day positions use their actual elapsed span.
    """
    if not entry or not exit_:
        return None
    try:
        from datetime import datetime, timedelta
        def stamp(leg, fallback_date):
            d = (leg.get('date') or fallback_date or '').strip()
            t = (leg.get('time') or '').strip()
            if not d or not t:
                return None
            if len(t) == 5:
                t += ':00'
            return datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
        start = stamp(entry, trade_date)
        end = stamp(exit_, trade_date)
        if start is None or end is None:
            return None
        if not exit_.get('date') and end < start:
            end += timedelta(days=1)
        return int((end - start).total_seconds() // 60)
    except (ValueError, TypeError):
        return None


def _bucket_stats(rows, key_fn, label_fn=None):
    """Group rows by key_fn and compute the standard per-bucket stats.

    exit_efficiency is averaged over winners only — a loser has no favourable
    excursion to capture, so mixing them would measure something else.
    """
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if k is None or k == '':
            continue
        buckets[k].append(r)

    out = []
    for k, group in buckets.items():
        pnls = sorted(g['net_pnl'] for g in group)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        effs = [g['exit_efficiency'] for g in group
                if g.get('exit_efficiency') is not None and g['net_pnl'] > 0]
        maes = [g['mae_pct'] for g in group if g.get('mae_pct') is not None]
        n = len(group)
        out.append({
            "key": str(k),
            "label": label_fn(k) if label_fn else str(k),
            "trades": n,
            "net_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / n, 2),
            "median_pnl": round(pnls[n // 2], 2),
            "win_rate": round(len(wins) / n * 100, 1),
            "wins": len(wins),
            "losses": len(losses),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
            "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                              if losses and sum(losses) != 0 else None),
            "big_losses": sum(1 for p in pnls if p < -500),
            "exit_efficiency": round(sum(effs) / len(effs), 1) if effs else None,
            "avg_mae": round(sum(maes) / len(maes), 2) if maes else None,
        })
    return sorted(out, key=lambda x: -x['net_pnl'])


def _ordered(buckets, order):
    idx = {k: i for i, k in enumerate(order)}
    return sorted(buckets, key=lambda b: idx.get(b['key'], 999))


def _zero_bucket(key):
    """An hour nobody traded in. Shown rather than omitted, so the table spans
    the same 24 hours as the dashboard chart and an empty hour reads as 'none'
    instead of disappearing."""
    return {
        "key": str(key), "label": str(key), "trades": 0,
        "net_pnl": 0, "avg_pnl": 0, "median_pnl": 0, "win_rate": 0,
        "wins": 0, "losses": 0, "avg_win": 0, "avg_loss": 0,
        "profit_factor": None, "big_losses": 0,
        "exit_efficiency": None, "avg_mae": None,
    }


def _fill_order(buckets, order):
    """Every key in `order` present, in order; keys outside it keep their place."""
    by_key = {b['key']: b for b in buckets}
    out = [_zero_bucket(k) if k not in by_key else by_key[k] for k in order]
    return out + [b for b in buckets if b['key'] not in set(order)]


@app.get("/api/reports")
def get_reports(
    account_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    from datetime import datetime as _dt
    from collections import OrderedDict

    sql = """
        SELECT t.id, t.trade_group, t.ticker, t.side, t.date, t.net_pnl,
               t.instrument_type, t.executions, t.setup, t.setup_grade,
               t.source, t.mfe_pct, t.mae_pct, t.exit_efficiency,
               ta.strategy, ta.r_multiple, ta.emotional_state, ta.mistakes,
               ta.idea_source
        FROM trades t
        LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group
        WHERE t.net_pnl IS NOT NULL
    """
    params: list = []
    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    if date_from:
        sql += " AND t.date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND t.date <= ?"
        params.append(date_to)
    sql += " ORDER BY t.date, t.id"

    raw = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not raw:
        return {"has_data": False, "time_of_day_coverage": {
            "entries": 0, "placed": 0, "unplaced": 0,
            "timezone": _mt5_timezone(conn), "timezone_error": None,
        }}

    # Resolve timezone once. An invalid setting falls back to the stored clock,
    # and is exposed in coverage instead of silently removing entries.
    configured_tz = _mt5_timezone(conn)
    tz_name = configured_tz
    tz_error = None
    if tz_name:
        try:
            mt5_time.load_zone(tz_name)
        except mt5_time.TimezoneError as exc:
            tz_error = str(exc)
            tz_name = ""
    coverage_entries = 0
    coverage_placed = 0

    # Derive entry time, hold duration and exit count once per trade.
    for r in raw:
        try:
            ex = json.loads(r['executions'] or '[]')
        except Exception:
            ex = []
        ea = 'BOT' if r['side'] == 'LONG' else 'SOLD'
        xa = 'SOLD' if r['side'] == 'LONG' else 'BOT'
        ent = sorted(
            [e for e in ex if e.get('action') == ea],
            key=lambda e: (e.get('date') or r.get('date', ''), e.get('time', '')),
        )
        xit = sorted(
            [e for e in ex if e.get('action') == xa],
            key=lambda e: (e.get('date') or r.get('date', ''), e.get('time', '')),
        )
        t_in = _mins_of(ent[0].get('time')) if ent else None
        if ent:
            coverage_entries += 1
            if (r.get('source') or '') == 'mt5' and tz_name:
                # Unconvertible input falls back to the stored clock rather than
                # dropping the trade — same rule as the dashboard chart.
                clock = mt5_time.utc_to_server_hhmm(
                    ent[0].get('date') or r.get('date', ''), ent[0].get('time', ''), tz_name
                ) or ent[0].get('time', '')
                t_in = _mins_of(clock)
            # Placed means 'will land in a bucket', counted here so coverage can
            # never claim a trade the table does not show.
            if t_in is not None and 0 <= t_in < 24 * 60:
                coverage_placed += 1
            else:
                t_in = None
        r['entry_min'] = t_in
        # Hold is first entry to last exit, in wall-clock minutes. Dates are part
        # of it: an FX position opened 22:00 and closed 06:00 the next morning
        # held for 8 hours, not -16. Doing this from clock time alone made every
        # overnight hold negative, and a negative hold was dropped from the
        # hold-time table entirely.
        r['hold_min'] = _span_minutes(ent[0] if ent else None, xit[-1] if xit else None, r.get('date'))
        r['n_exits'] = len({e.get('time') for e in xit}) if xit else 0
        try:
            r['dow'] = _dt.strptime(r['date'], '%Y-%m-%d').weekday()
        except Exception:
            r['dow'] = None

    def hold_bucket(r):
        m = r['hold_min']
        if m is None or m < 0:
            return None
        if m < 5:
            return '0-5 min'
        if m < 15:
            return '5-15 min'
        if m < 30:
            return '15-30 min'
        if m < 60:
            return '30-60 min'
        if m < 120:
            return '1-2 hrs'
        return '2+ hrs'

    def session_bucket(r):
        """Whole-hour floor on the entry clock. A 24h market has no sessions
        worth hard-coding, and every minute has exactly one home, so nothing
        can be caught by an 'everything else' fallback."""
        t = r['entry_min']
        if t is None or t < 0 or t >= 24 * 60:
            return None
        return f"{t // 60:02d}:00"

    def management_bucket(r):
        if r['n_exits'] > 1:
            return 'Scaled out'
        if r['n_exits'] == 1:
            return 'All-or-nothing'
        return None

    # Equity curve and drawdown, aggregated per trading day.
    by_day = OrderedDict()
    for r in raw:
        by_day[r['date']] = by_day.get(r['date'], 0.0) + r['net_pnl']

    curve, cum, peak, max_dd, max_dd_date = [], 0.0, 0.0, 0.0, None
    for d, p in by_day.items():
        cum += p
        peak = max(peak, cum)
        dd = cum - peak
        if dd < max_dd:
            max_dd, max_dd_date = dd, d
        curve.append({"date": d, "pnl": round(p, 2),
                       "cumulative": round(cum, 2), "drawdown": round(dd, 2)})

    # Streaks over trades in chronological order. A scratch (net P&L exactly 0)
    # ends either run without counting as a loss — it was not a winning trade,
    # but it was not a losing one either.
    cur = best_win = worst_loss = 0
    for r in raw:
        if r['net_pnl'] > 0:
            cur = cur + 1 if cur > 0 else 1
            best_win = max(best_win, cur)
        elif r['net_pnl'] < 0:
            cur = cur - 1 if cur < 0 else -1
            worst_loss = min(worst_loss, cur)
        else:
            cur = 0

    # Tags per trade (strategy and source tags mirror their fields, so they are left out).
    # A trade with several tags counts once under each of them.
    tags_by_group = {}
    for tr in conn.execute(
        "SELECT DISTINCT trade_group, tag_type, tag_value FROM trade_tags "
        "WHERE tag_type NOT IN ('strategy', 'source') AND TRIM(tag_value) <> ''"
    ).fetchall():
        tags_by_group.setdefault(tr['trade_group'], []).append((tr['tag_type'], tr['tag_value']))
    by_tag = {}
    for tag_type in ('setup', 'execution', 'mistake', 'emotion', 'outcome'):
        rows_for_type = [
            dict(r, _tag=value)
            for r in raw
            for (t, value) in tags_by_group.get(r['trade_group'], [])
            if t == tag_type
        ]
        if rows_for_type:
            by_tag[tag_type] = _bucket_stats(rows_for_type, lambda r: r['_tag'])

    day_pnls = list(by_day.values())
    green = [p for p in day_pnls if p > 0]
    red = [p for p in day_pnls if p < 0]

    return {
        "has_data": True,
        "trade_count": len(raw),
        # Guards the timing breakdown: `placed + unplaced` must equal `entries`.
        # Without this, an entry that fails to bucket disappears from the chart
        # with no trace — which is exactly how the old US-session grid hid 62%
        # of an FX journal.
        "time_of_day_coverage": {
            "entries": coverage_entries,
            "placed": coverage_placed,
            "unplaced": coverage_entries - coverage_placed,
            "timezone": tz_name if tz_name else "stored-as-is",
            "timezone_error": tz_error,
        },
        "equity_curve": curve,
        # The same ratios KPIs report, built on this endpoint's own curve: the
        # equity curve here carries `cumulative` per day, which is the shape
        # equity.ratios reads. Sharing it means Reports and the Dashboard cannot
        # disagree about what a percentage is a percentage of.
        "summary": {
            "net_pnl": round(sum(r['net_pnl'] for r in raw), 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_date": max_dd_date,
            **equity.summarize(conn, account_id=account_id, date_from=date_from,
                               total_net_pnl=round(sum(r['net_pnl'] for r in raw), 2),
                               max_drawdown=max_dd,
                               daily_pnl=[{"date": e["date"], "net_pnl": e["pnl"],
                                           "cumulative": e["cumulative"]} for e in curve]),
            "best_day": round(max(day_pnls), 2) if day_pnls else 0,
            "worst_day": round(min(day_pnls), 2) if day_pnls else 0,
            "trading_days": len(by_day),
            "green_days": len(green),
            "red_days": len(red),
            "avg_green_day": round(sum(green) / len(green), 2) if green else 0,
            "avg_red_day": round(sum(red) / len(red), 2) if red else 0,
            "longest_win_streak": best_win,
            "longest_loss_streak": abs(worst_loss),
            "avg_trades_per_day": round(len(raw) / len(by_day), 1) if by_day else 0,
        },
        "by_day_of_week": _ordered(
            _bucket_stats(raw, lambda r: r['dow'], lambda k: _DOW_NAMES[int(k)]),
            [str(i) for i in range(7)]),
        "by_session": _fill_order(_bucket_stats(raw, session_bucket), _HOUR_ORDER),
        "by_hold_time": _ordered(_bucket_stats(raw, hold_bucket), _HOLD_ORDER),
        "by_month": sorted(_bucket_stats(raw, lambda r: r['date'][:7]),
                           key=lambda b: b['key']),
        "by_setup": _bucket_stats(raw, lambda r: r['setup'],
                                  lambda k: _SETUP_LABEL_MAP.get(k, k)),
        "by_grade": _ordered(_bucket_stats(raw, lambda r: r['setup_grade']),
                             ['A++', 'A+', 'A', 'B', 'C', 'D', 'F']),
        "by_strategy": _bucket_stats(raw, lambda r: r['strategy']),
        "by_symbol": _bucket_stats(raw, lambda r: r['ticker'])[:40],
        "by_side": _bucket_stats(raw, lambda r: r['side']),
        "by_instrument": _bucket_stats(raw, lambda r: r['instrument_type']),
        "by_management": _bucket_stats(raw, management_bucket),
        "by_emotion": _bucket_stats(raw, lambda r: r['emotional_state']),
        "by_source": _bucket_stats(raw, lambda r: r['idea_source']),
        "by_tag": by_tag,
    }


@app.get("/api/edge-report")
def get_edge_report(
    account_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    sql = """
        SELECT t.trade_group, t.ticker, t.side, t.net_pnl, t.date, t.executions,
               t.source,
               ta.r_multiple, ta.emotional_state, ta.mistakes
        FROM trades t
        LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group
        WHERE 1=1
    """
    params: list = []
    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    if date_from:
        sql += " AND t.date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND t.date <= ?"
        params.append(date_to)
    sql += " ORDER BY t.date"

    rows = conn.execute(sql, params).fetchall()
    trades = [row_to_dict(r) for r in rows]

    # Mistake frequency from trade_tags
    tag_sql = """
        SELECT tt.tag_value, COUNT(*) as cnt
        FROM trade_tags tt
        JOIN trades t ON t.trade_group = tt.trade_group
        WHERE tt.tag_type = 'mistake'
    """
    tag_params: list = []
    if account_id is not None:
        tag_sql += " AND t.account_id = ?"
        tag_params.append(account_id)
    if date_from:
        tag_sql += " AND t.date >= ?"
        tag_params.append(date_from)
    if date_to:
        tag_sql += " AND t.date <= ?"
        tag_params.append(date_to)
    tag_sql += " GROUP BY tt.tag_value ORDER BY cnt DESC LIMIT 8"

    tag_rows = conn.execute(tag_sql, tag_params).fetchall()
    mistake_counts: dict[str, int] = {r["tag_value"]: r["cnt"] for r in tag_rows}

    # Also mine free-text mistakes field
    for trade in trades:
        text = (trade.get("mistakes") or "").strip()
        if not text:
            continue
        parts = [p.strip() for p in text.replace("\n", ",").replace(";", ",").split(",") if p.strip()]
        for part in parts:
            key = part[:60]
            if key not in mistake_counts:
                mistake_counts[key] = 1
            else:
                mistake_counts[key] += 1

    mistake_freq = sorted(
        [{"mistake": k, "count": v} for k, v in mistake_counts.items()],
        key=lambda x: -x["count"],
    )[:8]

    # Time-of-day buckets. FX trades around the clock, so the grid spans a full
    # day; an hour outside a US cash session is a normal entry for a 24h market
    # and must not be discarded. Buckets are 60 minutes because the dashboard
    # labels this column "Hour" and renders every row it is given.
    #
    # The clock used is the broker server's: MT5 fills are stored as UTC, and a
    # report against UTC shows hours nobody traded in. The conversion runs only
    # for source='mt5' rows — every other importer already stores wall-clock
    # time and shifting it again would move it by a whole day.
    tz_name = _mt5_timezone(conn)
    tz_note = None
    if tz_name:
        # Resolve the zone once, before any counting. An unusable name would
        # otherwise fail inside the per-trade conversion and be swallowed by its
        # handler, rendering a confident, entirely empty chart. Falling back to
        # the stored clock keeps the rest of the report usable and names the
        # problem, rather than failing every panel over one settings field.
        try:
            mt5_time.load_zone(tz_name)
        except mt5_time.TimezoneError as exc:
            tz_note = str(exc)
            tz_name = ""
    BUCKETS: list[str] = []
    for t_min in range(0, 24 * 60, 60):
        h, m = divmod(t_min, 60)
        BUCKETS.append(f"{h:02d}:{m:02d}")

    bucket_pnl: dict[str, float] = {b: 0.0 for b in BUCKETS}
    bucket_counts: dict[str, int] = {b: 0 for b in BUCKETS}
    entries_seen = 0
    entries_unplaced = 0

    DOW_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    dow_pnl: dict[str, float] = {d: 0.0 for d in DOW_ORDER}
    dow_counts: dict[str, int] = {d: 0 for d in DOW_ORDER}
    DOW_NAMES = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}

    winner_hold: list[float] = []
    loser_hold: list[float] = []

    r_bucket_counts: dict[float, int] = {}
    for i in range(-7, 8):
        r_bucket_counts[round(i * 0.5, 1)] = 0

    EMOTIONS = ["calm", "anxious", "overconfident", "disciplined", "frustrated", "revenge"]
    emo_data: dict[str, dict] = {
        e: {"count": 0, "wins": 0, "total_pnl": 0.0, "r_vals": []} for e in EMOTIONS
    }

    for trade in trades:
        pnl = trade.get("net_pnl") or 0.0
        date_str = trade.get("date", "")
        side = (trade.get("side") or "LONG").upper()

        try:
            execs = json.loads(trade.get("executions") or "[]")
        except Exception:
            execs = []

        entry_action = "BOT" if side == "LONG" else "SOLD"
        entry_exs = sorted(
            [e for e in execs if e.get("action") == entry_action and e.get("time")],
            # Position keys sort on (date, time): a position added to over two
            # days has two entries whose clock times do not order them.
            key=lambda e: (e.get("date") or date_str, e.get("time", "")),
        )
        entry_times = [e.get("time", "") for e in entry_exs]

        # Time-of-day bucket (entry time)
        if entry_times:
            entries_seen += 1
            try:
                raw = entry_times[0]
                clock = raw
                if (trade.get("source") or "") == "mt5" and tz_name:
                    # Stored time is UTC; report it on the broker's clock. The
                    # date comes from the entry fill itself — an overnight
                    # position can close on the far side of a DST change, and
                    # the close date would then apply the wrong offset.
                    clock = mt5_time.utc_to_server_hhmm(
                        entry_exs[0].get("date") or date_str, raw, tz_name
                    ) or raw
                h, m = (int(x) for x in clock.split(":")[:2])
                # Whole-hour floor: every minute in the hour shares one bucket.
                bkey = f"{h:02d}:{0:02d}"
                if bkey in bucket_pnl:
                    bucket_pnl[bkey] += pnl
                    bucket_counts[bkey] += 1
                else:
                    entries_unplaced += 1
            except Exception:
                entries_unplaced += 1

        # Day of week
        if date_str:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d")
                dow = d.weekday()
                if dow in DOW_NAMES:
                    day_name = DOW_NAMES[dow]
                    dow_pnl[day_name] += pnl
                    dow_counts[day_name] += 1
            except Exception:
                pass

        # Hold time. Subtracting clock times alone returns a negative number for
        # any position held past midnight, and `if hold >= 0` then dropped it —
        # measured on this journal, three trades (one of them held 21 days) were
        # excluded from the winner/loser hold averages. Use leg dates.
        if entry_exs:
            exit_action = "SOLD" if side == "LONG" else "BOT"
            exit_exs = sorted(
                [e for e in execs if e.get("action") == exit_action and e.get("time")],
                key=lambda e: (e.get("date") or date_str, e.get("time", "")),
            )
            if exit_exs:
                hold = _span_minutes(entry_exs[0], exit_exs[-1], date_str)
                if hold is not None and hold >= 0:
                    if pnl > 0:
                        winner_hold.append(hold)
                    elif pnl < 0:
                        loser_hold.append(hold)

        # R-multiple distribution
        r = trade.get("r_multiple")
        if r is not None:
            r_clipped = max(-3.5, min(3.5, float(r)))
            bucket_key = round(round(r_clipped * 2) / 2, 1)
            if bucket_key in r_bucket_counts:
                r_bucket_counts[bucket_key] += 1
            else:
                closest = min(r_bucket_counts.keys(), key=lambda x: abs(x - bucket_key))
                r_bucket_counts[closest] += 1

        # Emotion outcomes
        emo = (trade.get("emotional_state") or "").lower().strip()
        if emo in emo_data:
            emo_data[emo]["count"] += 1
            emo_data[emo]["total_pnl"] += pnl
            if pnl > 0:
                emo_data[emo]["wins"] += 1
            if r is not None:
                emo_data[emo]["r_vals"].append(float(r))

    time_of_day = [
        {"bucket": b, "net_pnl": round(bucket_pnl[b], 2), "trade_count": bucket_counts[b]}
        for b in BUCKETS
    ]
    day_of_week = [
        {"day": day, "net_pnl": round(dow_pnl[day], 2), "trade_count": dow_counts[day]}
        for day in DOW_ORDER
    ]
    r_multiple_dist = [
        {"bucket": str(k), "count": v}
        for k, v in sorted(r_bucket_counts.items())
    ]
    emotion_outcomes = []
    for emo in EMOTIONS:
        d = emo_data[emo]
        if d["count"] == 0:
            continue
        r_vals = d["r_vals"]
        emotion_outcomes.append({
            "state": emo,
            "trade_count": d["count"],
            "win_rate": round(d["wins"] / d["count"] * 100, 1),
            "avg_pnl": round(d["total_pnl"] / d["count"], 2),
            "avg_r": round(sum(r_vals) / len(r_vals), 2) if r_vals else None,
        })
    hold_time = {
        "winners_avg_min": round(sum(winner_hold) / len(winner_hold), 1) if winner_hold else None,
        "losers_avg_min": round(sum(loser_hold) / len(loser_hold), 1) if loser_hold else None,
    }

    # Expectancy for edge report
    all_pnl = [t.get("net_pnl") or 0 for t in trades]
    wins_er = [p for p in all_pnl if p > 0]
    losses_er = [p for p in all_pnl if p < 0]
    total_er = len(all_pnl)
    if total_er > 0 and wins_er and losses_er:
        er_expectancy = round(
            (len(wins_er) / total_er) * (sum(wins_er) / len(wins_er))
            + (len(losses_er) / total_er) * (sum(losses_er) / len(losses_er)),
            2,
        )
    else:
        er_expectancy = 0.0

    return {
        "time_of_day": time_of_day,
        # Coverage for the chart above: an entry the grid could not place used to
        # disappear silently, which is how 62% of an FX history went missing.
        # Non-zero here means the report is under-counting and says so.
        "time_of_day_coverage": {
            "entries": entries_seen,
            "placed": entries_seen - entries_unplaced,
            "unplaced": entries_unplaced,
            "timezone": tz_name if tz_name else "stored-as-is",
            # Non-null only when the configured server zone was unusable and the
            # chart is therefore showing stored (UTC) times instead.
            "timezone_error": tz_note,
        },
        "day_of_week": day_of_week,
        "r_multiple_dist": r_multiple_dist,
        "emotion_outcomes": emotion_outcomes,
        "hold_time": hold_time,
        "mistake_frequency": mistake_freq,
        "expectancy": er_expectancy,
        "total_trades": total_er,
    }


# ── AI Insights ────────────────────────────────────────────────────────────────

@app.get("/api/insights")
def get_insights(
    account_id: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_connection),
):
    require_feature(conn, "insights")
    forget_usage()
    # Reuse KPI data as input to insights. Pass explicit None for the date
    # filters: called as a plain function, get_kpis would otherwise receive
    # truthy Query() defaults and bind them into SQL.
    kpis = get_kpis(account_id=account_id, date_from=None, date_to=None, conn=conn)

    try:
        insights_text = generate_insights(kpis)
        return {"insights": insights_text, "ai_usage": _ai_usage_payload(last_usage())}
    except Exception as e:
        status, detail = friendly_error(e)
        raise HTTPException(status_code=status, detail=detail)


# ── Weekly Summary ─────────────────────────────────────────────────────────────

@app.get("/api/weekly-summary")
def get_weekly_summary(
    date: str = Query(...),
    account_id: int | None = Query(None),
    force: bool = Query(False),
    conn: sqlite3.Connection = Depends(get_connection),
):
    require_feature(conn, "weekly")
    forget_usage()
    from datetime import timedelta
    d = datetime.strptime(date, "%Y-%m-%d")
    week_start = d - timedelta(days=d.weekday())
    week_end = week_start + timedelta(days=4)
    week_label = f"{week_start.strftime('%Y')}-W{week_start.strftime('%V')}"
    week_from = week_start.strftime("%Y-%m-%d")
    week_to = week_end.strftime("%Y-%m-%d")
    cache_key = f"weekly_{week_label}"
    # Same staleness rule as a Day Review: the week is only reusable while its
    # trades and diary notes are the ones it was written from.
    week_hash = date_range_fingerprint(conn, week_from, week_to, account_id)
    stale_reason = None

    if not force:
        cached = conn.execute(
            "SELECT ai_content, input_hash FROM daily_summaries WHERE summary_date = ? AND (account_id = ? OR (account_id IS NULL AND ? IS NULL))",
            (cache_key, account_id, account_id),
        ).fetchone()
        if cached and cached[0]:
            stored_hash = cached[1]
            if stored_hash and stored_hash == week_hash:
                try:
                    payload = json.loads(cached[0])
                    payload["cached"] = True
                    payload["ai_usage"] = {"spent": False, "reason": "cached"}
                    return payload
                except Exception:
                    stale_reason = "the stored summary could not be read"
            elif stored_hash is None:
                stale_reason = "cached before staleness tracking existed"
            else:
                stale_reason = "trades or diary notes changed since this was written"

    sql = "SELECT trade_group FROM trades WHERE date BETWEEN ? AND ?"
    params = [week_from, week_to]
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    has_trades = conn.execute(sql + " LIMIT 1", params).fetchone()
    if not has_trades:
        return {"error": "No trades found for this week", "week_label": week_label,
                "week_from": week_from, "week_to": week_to}

    sql = """
        SELECT t.trade_group, t.ticker, t.side, t.net_pnl, t.date,
               ta.strategy, ta.r_multiple, ta.emotional_state, ta.mistakes,
               ta.entry_reason, ta.exit_reason
        FROM trades t
        LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group
        WHERE t.date >= ? AND t.date <= ?
    """
    params: list = [week_from, week_to]
    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    sql += " ORDER BY t.date, t.id"

    rows = conn.execute(sql, params).fetchall()
    trades = [row_to_dict(r) for r in rows]

    if not trades:
        return {"error": "No trades found for this week", "week_label": week_label,
                "week_from": week_from, "week_to": week_to}

    week_context = {
        "trades": trades,
        "week_label": week_label,
        "week_from": week_from,
        "week_to": week_to,
    }

    try:
        result = generate_weekly_summary(week_context)
    except Exception as e:
        status, detail = friendly_error(e)
        raise HTTPException(status_code=status, detail=detail)

    result["week_label"] = week_label
    result["week_from"] = week_from
    result["week_to"] = week_to
    result["ai_usage"] = _ai_usage_payload(last_usage(), stale_reason)

    # Always written, account or not: a summary left un-keyed was unreachable by
    # the next read, so every weekly summary was regenerated from scratch. Delete
    # first — SQLite treats NULLs as distinct in a UNIQUE index, so `REPLACE`
    # would stack a second row instead of overwriting when account_id is NULL.
    conn.execute(
        "DELETE FROM daily_summaries WHERE summary_date = ? AND account_id IS ?",
        (cache_key, account_id),
    )
    conn.execute(
        """INSERT INTO daily_summaries
           (account_id, summary_date, ai_content, generated_at, input_hash)
           VALUES (?, ?, ?, ?, ?)""",
        (account_id, cache_key, json.dumps(result), datetime.now().isoformat(), week_hash),
    )
    conn.commit()

    result["cached"] = False
    result["regenerated_reason"] = stale_reason
    return result


# ── Daily Summary ──────────────────────────────────────────────────────────────

@app.get("/api/daily-summary")
def get_daily_summary(
    date: str = Query(...),
    account_id: int | None = Query(None),
    force: bool = Query(False),
    conn: sqlite3.Connection = Depends(get_connection),
):
    require_feature(conn, "daily_summary")
    # A cached review is only usable if it still describes this day: the hash
    # covers the day's trades and its diary entry, so editing either marks it
    # stale. `force` stays as a manual override for "regenerate anyway".
    forget_usage()
    current_hash = daily_input_fingerprint(conn, date, account_id)
    stale_reason = None

    if not force:
        row = conn.execute(
            "SELECT ai_content, generated_at, input_hash FROM daily_summaries WHERE summary_date = ? AND (account_id = ? OR (account_id IS NULL AND ? IS NULL))",
            (date, account_id, account_id)
        ).fetchone()
        if row:
            stored_hash = row['input_hash']
            if stored_hash and stored_hash != current_hash:
                stale_reason = "trades or diary notes changed since this was written"
            elif stored_hash is None:
                stale_reason = "cached before staleness tracking existed"
            else:
                try:
                    content = json.loads(row['ai_content'])
                    content['date'] = date
                    content['cached'] = True
                    content['generated_at'] = row['generated_at']
                    content['ai_usage'] = {"spent": False, "reason": "cached"}
                    return content
                except Exception:
                    stale_reason = "the stored review could not be read"

    try:
        context = build_daily_context(conn, date, account_id)
        if not context['trades']:
            return {"date": date, "cached": False, "no_trades": True, "narrative": "No trades recorded for this date."}
        summary = generate_daily_summary(context)
    except Exception as e:
        # Without an API key this is the expected path, not a server fault.
        if not os.getenv("ANTHROPIC_API_KEY"):
            return {
                "date": date,
                "cached": False,
                "unavailable": True,
                "narrative": "Add ANTHROPIC_API_KEY to backend/.env to generate a review for this day.",
            }
        status, detail = friendly_error(e)
        raise HTTPException(status_code=status, detail=detail)

    summary['ai_usage'] = _ai_usage_payload(last_usage(), stale_reason)
    # Delete-then-insert: REPLACE does not match a NULL account_id in SQLite's
    # UNIQUE index, so a review with no account would have stacked a new row on
    # every regeneration instead of replacing the old one.
    conn.execute(
        "DELETE FROM daily_summaries WHERE summary_date = ? AND account_id IS ?",
        (date, account_id),
    )
    conn.execute(
        """INSERT INTO daily_summaries
           (summary_date, account_id, ai_content, generated_at, input_hash)
           VALUES (?, ?, ?, datetime('now'), ?)""",
        (date, account_id, json.dumps(summary), current_hash)
    )
    conn.commit()

    summary['date'] = date
    summary['cached'] = False
    summary['regenerated_reason'] = stale_reason
    return summary


# ── Brain AI Chatbot ────────────────────────────────────────────────────────────

from fastapi import Request as FastAPIRequest
from fastapi.responses import StreamingResponse


def _brain_request(body: dict, conn: sqlite3.Connection):
    """Parse and gate a Brain request. Raises before anything is built."""
    messages = body.get("messages", [])
    account_id = body.get("account_id")
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")
    require_feature(conn, "brain")
    forget_usage()
    return messages, account_id


@app.post("/api/brain")
async def brain_chat(
    req: FastAPIRequest,
    conn: sqlite3.Connection = Depends(get_connection),
):
    """One whole answer as JSON. Kept for callers that do not need tokens."""
    messages, account_id = _brain_request(await req.json(), conn)
    try:
        context = build_brain_context(conn, account_id)
        text = generate_brain_response(messages, context, conn, account_id)
        return {"response": text, "ai_usage": _ai_usage_payload(last_usage())}
    except Exception as e:
        status, detail = friendly_error(e)
        raise HTTPException(status_code=status, detail=detail)


@app.post("/api/brain/stream")
async def brain_chat_stream(
    req: FastAPIRequest,
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Newline-delimited JSON events: {"type": "delta"|"usage"|"error", ...}.

    The generator runs in the threadpool the way FastAPI runs sync generators,
    so the tool loop and the SDK call block a worker thread rather than the
    event loop. Aborting the fetch closes the response, which closes the
    generator, which closes the SDK stream — that chain is what makes Stop
    actually stop instead of letting the answer finish unseen.
    """
    messages, account_id = _brain_request(await req.json(), conn)
    context = build_brain_context(conn, account_id)

    def events():
        for kind, payload in brain_turn(messages, context, conn, account_id):
            if kind == "delta":
                yield json.dumps({"type": "delta", "text": payload}) + "\n"
            elif kind == "usage":
                yield json.dumps({"type": "usage",
                                  "ai_usage": _ai_usage_payload(payload)}) + "\n"
            elif kind == "error":
                yield json.dumps({"type": "error",
                                  "detail": payload["message"],
                                  "status": payload["status"]}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"X-Accel-Buffering": "no",
                                      "Cache-Control": "no-cache"})
