"""Read-only query tools for the Brain assistant.

Why tools instead of a 300-trade dump in the prompt: `build_brain_context`
stringified up to 300 rows into the system prompt on every message, which grows
with history, never reuses a cache, and still cannot answer a filtered question
("my losses on EURUSD in July") without loading everything.

Three rules hold here, and the tests assert each:

1. **The model never writes SQL.** Tools are fixed functions with JSON-Schema
   parameters. `run_tool` rejects an unknown name, so a prompt cannot introduce
   a query.
2. **Every statement is a SELECT.** `run_tool` refuses SQL that does not start
   with SELECT, and values are bound — never interpolated — so a ticker of
   `x'; DROP TABLE trades; --` is just a ticker.
3. **The account scope is not negotiable.** Each tool applies the caller's
   account_id itself; a parameter cannot widen the scope.

Numbers come from SQL or Python and arrive in the tool result, so the model
reports them rather than computing them.
"""
import json
import re
from datetime import datetime

__all__ = ["TOOL_SCHEMAS", "run_tool", "TOOL_NAMES"]

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_LIST = 50
_MAX_GROUP = 50


def _date(value, field):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _DATE.match(value):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{field} is not a real date: {value!r}") from None
    return value


def _choice(value, allowed, field):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be one of {sorted(allowed)}")
    norm = value.upper() if field in ("instrument_type", "side") else value
    if norm not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}")
    return norm


def _limit(value, default, cap):
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("limit must be a positive integer")
    return min(value, cap)


def _text(value, field, max_length):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if not value or len(value) > max_length or "\x00" in value:
        raise ValueError(f"{field} must be 1–{max_length} characters")
    return value


def _filters(body, account_id):
    """Shared WHERE fragment. Returns (clause, params) with account always bound."""
    clause = " WHERE (? IS NULL OR t.account_id = ?)"
    params = [account_id, account_id]
    if body.get("date_from"):
        clause += " AND t.date >= ?"
        params.append(_date(body["date_from"], "date_from"))
    if body.get("date_to"):
        clause += " AND t.date <= ?"
        params.append(_date(body["date_to"], "date_to"))
    if body.get("ticker"):
        clause += " AND UPPER(t.ticker) = UPPER(?)"
        params.append(_text(body["ticker"], "ticker", 32))
    instr = _choice(body.get("instrument_type"), {"STOCK", "OPTION", "FUTURE", "FX", "METAL", "INDEX"}, "instrument_type")
    if instr:
        clause += " AND t.instrument_type = ?"
        params.append(instr)
    side = _choice(body.get("side"), {"LONG", "SHORT"}, "side")
    if side:
        clause += " AND t.side = ?"
        params.append(side)
    if body.get("strategy"):
        clause += " AND ta.strategy = ?"
        params.append(_text(body["strategy"], "strategy", 120))
    if body.get("setup"):
        clause += " AND t.setup = ?"
        params.append(_text(body["setup"], "setup", 120))
    return clause, params


def _select(conn, sql, params):
    """Run a SELECT and refuse anything else, however it was assembled."""
    first = sql.lstrip().split(None, 1)[0].upper()
    if first != "SELECT":
        raise ValueError("brain tools are read-only: only SELECT is allowed")
    if ";" in sql.rstrip().rstrip(";"):
        raise ValueError("brain tools are read-only: one statement only")
    return conn.execute(sql, params).fetchall()


def performance_summary(conn, account_id, body):
    clause, params = _filters(body, account_id)
    rows = _select(conn,
        "SELECT t.date, t.net_pnl, t.gross_pnl, t.commissions FROM trades t "
        "LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group" + clause,
        params)
    pnl = [r["net_pnl"] for r in rows if r["net_pnl"] is not None]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    scrapes = [p for p in pnl if p == 0]
    by_day = {}
    for r in rows:
        if r["net_pnl"] is None:
            continue
        by_day[r["date"]] = by_day.get(r["date"], 0.0) + r["net_pnl"]
    best = max(by_day.items(), key=lambda kv: kv[1]) if by_day else None
    worst = min(by_day.items(), key=lambda kv: kv[1]) if by_day else None
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    return {
        "trades": len(pnl),
        "winning_days": sum(1 for v in by_day.values() if v > 0),
        "losing_days": sum(1 for v in by_day.values() if v < 0),
        "days": len(by_day),
        "wins": len(wins), "losses": len(losses), "scratch_trades": len(scrapes),
        "win_rate_pct": round(len(wins) / len(pnl) * 100, 1) if pnl else 0.0,
        "net_pnl": round(sum(pnl), 2),
        "gross_pnl": round(sum(r["gross_pnl"] or 0 for r in rows), 2),
        "commissions": round(sum(r["commissions"] or 0 for r in rows), 2),
        "avg_win": round(gross_wins / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses else None,
        "best_day": {"date": best[0], "net_pnl": round(best[1], 2)} if best else None,
        "worst_day": {"date": worst[0], "net_pnl": round(worst[1], 2)} if worst else None,
        "first_date": min(by_day) if by_day else None,
        "last_date": max(by_day) if by_day else None,
    }


def list_trades(conn, account_id, body):
    clause, params = _filters(body, account_id)
    limit = _limit(body.get("limit"), 25, _MAX_LIST)
    sort = _choice(body.get("sort_by"), {"date_desc", "date_asc", "pnl_desc", "pnl_asc"}, "sort_by") or "date_desc"
    order = {
        "date_desc": "t.date DESC, t.id DESC", "date_asc": "t.date ASC, t.id ASC",
        "pnl_desc": "t.net_pnl DESC", "pnl_asc": "t.net_pnl ASC",
    }[sort]
    rows = _select(conn,
        "SELECT t.trade_group, t.date, t.ticker, t.instrument_type, t.side, t.net_pnl, "
        "t.setup, ta.strategy, ta.r_multiple, ta.emotional_state, ta.mistakes, ta.notes "
        "FROM trades t LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group"
        + clause + " ORDER BY " + order + " LIMIT ?", (*params, limit))
    return {"sort_by": sort, "limit": limit, "trades": [dict(r) for r in rows]}


# Only columns whose values are already normalised, so a grouping cannot be
# redirected into an expression or a table it should not touch.
GROUPABLE = {
    "strategy": "COALESCE(ta.strategy, 'No strategy')",
    "setup": "COALESCE(t.setup, 'No setup')",
    "ticker": "t.ticker",
    "instrument_type": "t.instrument_type",
    "side": "t.side",
    "emotional_state": "COALESCE(ta.emotional_state, 'Unrecorded')",
    "date": "t.date",
    "weekday": "CASE CAST(strftime('%w', t.date) AS INTEGER) "
               "WHEN 0 THEN 'Sunday' WHEN 1 THEN 'Monday' WHEN 2 THEN 'Tuesday' "
               "WHEN 3 THEN 'Wednesday' WHEN 4 THEN 'Thursday' WHEN 5 THEN 'Friday' "
               "ELSE 'Saturday' END",
}


def breakdown(conn, account_id, body):
    group_by = body.get("group_by")
    if group_by not in GROUPABLE:
        raise ValueError(f"group_by must be one of {sorted(GROUPABLE)}")
    clause, params = _filters(body, account_id)
    expr = GROUPABLE[group_by]
    rows = _select(conn,
        f"SELECT {expr} AS bucket, t.net_pnl FROM trades t "
        f"LEFT JOIN trade_analysis ta ON t.trade_group = ta.trade_group{clause} "
        f"AND t.net_pnl IS NOT NULL", params)
    buckets = {}
    for r in rows:
        b = buckets.setdefault(r["bucket"], {"group": r["bucket"], "trades": 0,
                                             "wins": 0, "net_pnl": 0.0, "_pnl": []})
        p = r["net_pnl"]
        b["trades"] += 1
        b["wins"] += 1 if p > 0 else 0
        b["net_pnl"] += p
        b["_pnl"].append(p)
    out = []
    for b in buckets.values():
        out.append({
            "group": b["group"], "trades": b["trades"], "wins": b["wins"],
            "win_rate_pct": round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0,
            "net_pnl": round(b["net_pnl"], 2),
            "avg_pnl": round(b["net_pnl"] / b["trades"], 2) if b["trades"] else 0.0,
        })
    out.sort(key=lambda x: -x["net_pnl"])
    return {"group_by": group_by, "groups": out[:_MAX_GROUP]}


def get_diary(conn, account_id, body):
    clause = " WHERE (? IS NULL OR account_id = ?)"
    params = [account_id, account_id]
    if body.get("date_from"):
        clause += " AND entry_date >= ?"
        params.append(_date(body["date_from"], "date_from"))
    if body.get("date_to"):
        clause += " AND entry_date <= ?"
        params.append(_date(body["date_to"], "date_to"))
    rows = _select(conn,
        "SELECT entry_date, ai_analysis FROM diary_entries" + clause +
        " ORDER BY entry_date DESC LIMIT ?", (*params, _limit(body.get("limit"), 10, 30)))
    out = []
    for r in rows:
        try:
            a = json.loads(r["ai_analysis"] or "{}")
        except (ValueError, TypeError):
            a = {}
        out.append({
            "date": r["entry_date"],
            "summary": a.get("overall_summary", ""),
            "patterns": a.get("patterns_identified", []),
            "mistakes": a.get("common_mistakes", []),
        })
    return {"entries": out}


def _schema(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


_FILTERS = {
    "date_from": {"type": "string", "description": "Earliest trade date, YYYY-MM-DD."},
    "date_to": {"type": "string", "description": "Latest trade date, YYYY-MM-DD."},
    "ticker": {"type": "string", "description": "Symbol, e.g. EURUSD or AAPL."},
    "instrument_type": {"type": "string", "enum": ["STOCK", "OPTION", "FUTURE", "FX", "METAL", "INDEX"]},
    "side": {"type": "string", "enum": ["LONG", "SHORT"]},
    "strategy": {"type": "string", "description": "Strategy name exactly as recorded."},
    "setup": {"type": "string", "description": "Setup tag exactly as recorded."},
}

TOOL_SCHEMAS = [
    {"name": "get_performance_summary",
     "description": "Totals for the selected trades: count, win rate, net P&L, average win "
                    "and loss, profit factor, best and worst day. Use for any 'how did I do' "
                    "question before anything else.",
     "input_schema": _schema(_FILTERS)},
    {"name": "list_trades",
     "description": "The individual trades matching the filters, newest first, with strategy, "
                    "R multiple, emotional state, mistakes and notes. Use to see which trades "
                    "make up a number, or to answer about specific dates or symbols.",
     "input_schema": _schema({**_FILTERS, "sort_by": {"type": "string",
            "enum": ["date_desc", "date_asc", "pnl_desc", "pnl_asc"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}})},
    {"name": "breakdown",
     "description": "Group the matching trades by one dimension and get per-group trade "
                    "counts, win rate and net P&L. Answers 'where do I win', 'which strategy "
                    "pays', 'do I lose on Mondays'.",
     "input_schema": _schema({**_FILTERS, "group_by": {"type": "string",
            "enum": sorted(GROUPABLE)}})},
    {"name": "get_diary",
     "description": "Diary entries in the date range, with their summary, identified patterns "
                    "and mistakes. Use when the question is about how the trader felt or what "
                    "they wrote.",
     "input_schema": _schema({
        "date_from": _FILTERS["date_from"], "date_to": _FILTERS["date_to"],
        "limit": {"type": "integer", "minimum": 1, "maximum": 30}})},
]

TOOL_NAMES = [t["name"] for t in TOOL_SCHEMAS]

_DISPATCH = {
    "get_performance_summary": performance_summary,
    "list_trades": list_trades,
    "breakdown": breakdown,
    "get_diary": get_diary,
}


def run_tool(name, arguments, conn, account_id):
    """Run one tool and return JSON. Unknown names and bad input raise ValueError."""
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    body = arguments if isinstance(arguments, dict) else {}
    result = fn(conn, account_id, body)
    return json.dumps(result, default=str)
