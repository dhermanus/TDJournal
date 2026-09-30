"""Recompute trade excursions (MFE / MAE / exit efficiency) from M1 bars.

MFE is the best unrealised gain reached while the position was open, MAE the
worst unrealised loss; exit_efficiency is the share of the favourable move that
was actually banked. All three are measured off imported 1-minute candles, never
taken from an export — a broker's own excursion columns cannot be checked, the
high/low a bar printed can.

Sign conventions, which the frontend already renders:
  mfe_pct          >= 0   a plus-signed "opportunity" figure
  mae_pct          <= 0   a negative "heat taken" figure
  exit_efficiency  signed percentage of MFE captured; NULL when undefined

The percentage base is the entry price in both directions, so a LONG and a SHORT
of the same magnitude compare directly. Efficiency is computed from *gross price*
move over MFE — both sides of the ratio are price, so costs cannot make a trade
that captured its whole move look like it captured none. Commission and swap are
already reported through net_pnl.

Absent bars are NULL, never zero. Zero asserts "the price never moved", which is
a claim about the market; NULL says only "nothing was measured", which is what a
gap in the export, an unimported symbol or an open position with no later bars
actually means.
"""
from __future__ import annotations

import json
import sqlite3


def _stamp(execution: dict) -> str | None:
    """Naive-UTC 'YYYY-MM-DD HH:00:00' for a fill, or None if it has no date.

    Floored to the minute, because a bar is stamped at its open: the 12:31 bar
    is the one a fill at 12:31:57 happened in, and comparing the fill's seconds
    against `time >= '... 12:31:57'` would exclude that bar — dropping exactly
    the candle that contains the entry. Both bounds are floored, so entry and
    exit fill-minute bars are always included.

    Ordering is a plain string compare, correct because the date and time are
    zero-padded fixed width — the same assumption the rest of the code makes
    when it sorts executions.
    """
    date = execution.get("date")
    if not date:
        return None
    time = (execution.get("time") or "").strip() or "00:00:00"
    parts = time.split(":")
    while len(parts) < 3:
        parts.append("00")
    hh, mm = (p.zfill(2) for p in parts[:2])
    return f"{date} {hh}:{mm}:00"


def _is_entry(execution: dict, side: str) -> bool:
    """Entry side of the round trip. An execution with no action is not an entry."""
    action = (execution.get("action") or "").upper()
    if not action:
        return False
    return action == ("BOT" if side.upper() == "LONG" else "SOLD")


def _weighted_price(executions: list[dict]) -> float | None:
    """Volume-weighted average fill, or None when there is nothing to weight.

    Averaging prices instead would misprice a trade scaled into twice: the
    second fill is half the position and should not count as much as the first.
    """
    qty_total = 0.0
    notional = 0.0
    for ex in executions:
        qty = float(ex.get("qty") or 0)
        price = ex.get("price")
        if qty <= 0 or price is None:
            continue
        qty_total += qty
        notional += qty * float(price)
    return notional / qty_total if qty_total > 0 else None


def hold_window(executions: list[dict]) -> tuple[str, str] | None:
    """(first fill stamp, last fill stamp), or None for an execution-less trade."""
    stamps = [s for s in (_stamp(e) for e in executions) if s]
    if not stamps:
        return None
    return stamps[0], stamps[-1]


def measure(bars: list[dict], side: str) -> dict | None:
    """Extreme high and low over the hold window, or None without bars.

    The window is bounded by floored fill minutes (see `_stamp`), so the bars
    given here always include the candles entry and exit happened in. At M1
    resolution that leaves up to a minute of the bar before the fill on the
    entry side and after it on the exit side — the finest granularity the data
    offers, and for the same-minute trades that dominate this book the whole bar
    is the best evidence there is.
    """
    if not bars:
        return None
    long_side = side.upper() != "SHORT"
    highs = [float(b["high"]) for b in bars if b.get("high") is not None]
    lows = [float(b["low"]) for b in bars if b.get("low") is not None]
    if not highs or not lows:
        return None
    best = max(highs) if long_side else min(lows)
    worst = min(lows) if long_side else max(highs)
    return {
        "mfe": best, "worst": worst, "long": long_side,
    }


def excursions_for_trade(trade: dict, bars: list[dict]) -> dict | None:
    """mfe_pct / mae_pct / exit_efficiency for one trade, or None without bars.

    Efficiency is deliberately None — not 0 — when the position never moved into
    profit: there is no favourable excursion to take a share of, and writing 0
    would average in as a real measurement and drag the winners-only figure down.
    """
    executions = trade.get("executions")
    if isinstance(executions, str):
        try:
            executions = json.loads(executions)
        except (ValueError, TypeError):
            return None
    executions = [e for e in (executions or []) if isinstance(e, dict)]
    if not executions:
        return None

    side = (trade.get("side") or "LONG").upper()
    entries = [e for e in executions if _is_entry(e, side)]
    entry_price = _weighted_price(entries)
    if not entry_price:
        return None

    hit = measure(bars, side)
    if hit is None:
        return None

    # Percent of the entry price, so a LONG and a SHORT of the same magnitude
    # compare directly. `* 100` converts the ratio to a percentage — the entry
    # itself is the denominator, not a percentage of it.
    base = entry_price
    mfe = (hit["mfe"] - entry_price) / base * 100.0 if hit["long"] else \
        (entry_price - hit["mfe"]) / base * 100.0
    mae = (hit["worst"] - entry_price) / base * 100.0 if hit["long"] else \
        (entry_price - hit["worst"]) / base * 100.0
    # Clamped to the conventional ranges: a position that never went into profit
    # has no favourable excursion (0, not a negative one), and one that never
    # went against the position took no heat (0, not a positive one).
    mfe = max(0.0, mfe)
    mae = min(0.0, mae)

    exits = [e for e in executions if not _is_entry(e, side)]
    exit_price = _weighted_price(exits) if exits else None
    efficiency = None
    if exit_price is not None and mfe > 0:
        realised = (exit_price - entry_price) / base * 100.0 if hit["long"] else \
            (entry_price - exit_price) / base * 100.0
        efficiency = realised / mfe * 100.0

    # Six places: far finer than any UI renders (the tightest is two), so the
    # stored number never becomes the thing that disagrees with the candles it
    # was measured from.
    return {
        "mfe_pct": round(mfe, 6),
        "mae_pct": round(mae, 6),
        "exit_efficiency": None if efficiency is None else round(efficiency, 4),
    }


def _is_open(executions: list[dict], side: str) -> bool:
    """True while entry quantity is not fully matched by exit quantity.

    Same rule as `main._is_open_position` — that function lives in main, which
    imports this module, so it cannot be reused here. A test pins the two
    together so they cannot drift apart: an MT5 `out_by` hedge leg and a partial
    close must resolve identically in both.
    """
    side = (side or "LONG").upper()
    entry_action = "BOT" if side == "LONG" else "SOLD"
    exit_action = "SOLD" if side == "LONG" else "BOT"
    entered = sum(float(e.get("qty") or 0) for e in executions
                  if (e.get("action") or "").upper() == entry_action)
    exited = sum(float(e.get("qty") or 0) for e in executions
                 if (e.get("action") or "").upper() == exit_action)
    return entered > 0 and entered != exited


def recompute(conn: sqlite3.Connection, symbols: list[str] | None = None) -> dict:
    """Recompute excursions for trades whose ticker is in `symbols`.

    Returns a report: which symbols were refreshed, how many trades were measured
    and how many were left without bars. A trade with no matching bars has its
    three columns cleared, so a value measured against an earlier, thinner export
    cannot survive as if it still held.
    """
    where = ""
    params: list = []
    if symbols:
        where = f" AND ticker IN ({','.join('?' for _ in symbols)})"
        params.extend(symbols)

    trades = conn.execute(
        "SELECT id, ticker, side, executions FROM trades WHERE 1=1" + where,
        params,
    ).fetchall()

    measured = 0
    without_bars = 0
    seen: set[str] = set()
    for trade in trades:
        ticker = trade["ticker"]
        seen.add(ticker)
        executions = _executions(trade)
        window = hold_window(executions)
        if window is None:
            _write(conn, trade["id"], None)
            without_bars += 1
            continue
        start, end = window

        if _is_open(executions, trade["side"]):
            # Still open: measure to the newest candle this symbol has. The
            # export may end before today, so take MAX(time) from the data
            # rather than "now" — asking for bars past the end of the file
            # would quietly truncate the measurement at the last bar anyway.
            last = conn.execute(
                "SELECT MAX(time) FROM bars WHERE symbol = ?", (ticker,)
            ).fetchone()[0]
            if not last:
                _write(conn, trade["id"], None)
                without_bars += 1
                continue
            if last > end:
                end = last

        # The window is floored to minutes, so the upper bound is widened to
        # the end of that minute: a fill at 03:10:03 happened inside the 03:10
        # bar, and `time <= '03:10:00'` would exclude a bar stamped 03:10:03.
        # Comparing to :59 covers every instant in the minute without pulling in
        # the next one.
        rows = conn.execute(
            "SELECT high, low FROM bars WHERE symbol = ? AND time >= ? AND time <= ? "
            "ORDER BY time",
            (ticker, start, end[:16] + ":59"),
        ).fetchall()
        values = excursions_for_trade(dict(trade), [dict(r) for r in rows])
        if values is None:
            _write(conn, trade["id"], None)
            without_bars += 1
            continue
        _write(conn, trade["id"], values)
        measured += 1

    conn.commit()
    return {
        "symbols": sorted(seen),
        "trades": len(trades),
        "measured": measured,
        "without_bars": without_bars,
    }


def _executions(trade) -> list[dict]:
    raw = trade["executions"]
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _write(conn: sqlite3.Connection, trade_id: int, values: dict | None) -> None:
    conn.execute(
        "UPDATE trades SET mfe_pct = ?, mae_pct = ?, exit_efficiency = ? WHERE id = ?",
        (
            values["mfe_pct"] if values else None,
            values["mae_pct"] if values else None,
            values["exit_efficiency"] if values else None,
            trade_id,
        ),
    )
