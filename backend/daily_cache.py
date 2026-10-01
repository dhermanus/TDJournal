"""Does a cached Day Review still describe the day it was written from?

    cd backend && python -m pytest tests/test_daily_cache.py -q

The cache used to serve any stored review for a date until someone pressed
Regenerate, so editing a trade's mistake or adding a diary note left the coaching
text describing a day that no longer existed. This computes a fingerprint of the
inputs the prompt actually reads — the day's trades with their joined analysis,
and the diary entry for that date — and the endpoint regenerates when it differs.

All-time account KPIs are deliberately *not* part of it: they are background
context, and including them would mean trading AAPL in July invalidated a May
review.

Everything is read into memory first and hashed as one blob, so the value does
not depend on the order SQLite happens to return rows in.
"""
from __future__ import annotations

import hashlib
import json


# The columns the Day Review prompt quotes. `executions` is included because the
# first entry time goes into the text; `net_pnl` because it is in every trade
# line; the analysis columns because each one is printed when present.
TRADE_COLUMNS = (
    "id", "trade_group", "ticker", "side", "instrument_type", "net_pnl", "gross_pnl",
    "commissions", "executions", "option_type", "option_strike", "option_expiry",
    "strategy", "r_multiple", "stop_loss", "target_price", "risk_per_trade",
    "risk_reward", "mistakes", "emotional_state", "entry_reason", "exit_reason",
    "ai_feedback", "idea_source", "match_confidence",
)


def daily_input_fingerprint(conn, date: str, account_id=None) -> str:
    """A short digest of everything the Day Review for `date` is written from.

    Stable across process restarts and across row order; changes the moment one
    of those inputs does. Costs a few microseconds, so it is safe to compute on
    every request rather than cached itself.
    """
    sql = ("SELECT " + ", ".join(f"t.{c}" if c in {
        "id", "trade_group", "ticker", "side", "instrument_type", "net_pnl",
        "gross_pnl", "commissions", "executions", "option_type", "option_strike",
        "option_expiry",
    } else f"ta.{c}" for c in TRADE_COLUMNS)
        + " FROM trades t LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group"
          " WHERE t.date = ?")
    params: list = [date]
    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    # Explicit order: a digest must not depend on SQLite's row order, which can
    # change after a VACUUM or an index rebuild without any input changing.
    sql += " ORDER BY t.id"
    trades = [tuple(row) for row in conn.execute(sql, params).fetchall()]

    diary_sql = ("SELECT ai_analysis FROM diary_entries WHERE entry_date = ?"
                 + (" AND account_id = ?" if account_id is not None else "")
                 + " ORDER BY id DESC LIMIT 1")
    diary_params: list = [date] + ([account_id] if account_id is not None else [])
    diary_row = conn.execute(diary_sql, diary_params).fetchone()
    diary = diary_row[0] if diary_row else None

    blob = json.dumps(
        {"trades": trades, "diary": diary},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8", "replace")
    return hashlib.sha256(blob).hexdigest()[:16]


def date_range_fingerprint(conn, date_from: str, date_to: str, account_id=None) -> str:
    """The same fingerprint over an inclusive date range, for the weekly summary.

    Composed from the daily fingerprints rather than one big query so both
    caches agree: a change on a single day inside the week invalidates the week
    for exactly the same reason it invalidates the day.
    """
    per_day = []
    from datetime import date as _date, timedelta as _timedelta
    try:
        start = _date.fromisoformat(date_from)
        end = _date.fromisoformat(date_to)
    except ValueError:
        return ""
    if end < start or (end - start).days > 366:
        return ""
    for offset in range((end - start).days + 1):
        day = (start + _timedelta(days=offset)).isoformat()
        per_day.append(daily_input_fingerprint(conn, day, account_id))
    return hashlib.sha256("|".join(per_day).encode()).hexdigest()[:16]
