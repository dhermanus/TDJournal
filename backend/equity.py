"""Equity: turning a starting balance plus cash flows into ratios.

    cd backend && python -m pytest tests/test_equity.py -q

Two inputs a trade can never supply — how much was put in, and what was taken
out — plus the P&L the journal already computes. Everything here is pure: it
takes numbers and returns numbers, so the ratios can be tested against
hand-worked cases without a database or an HTTP client.

Three rules hold throughout.

**NULL means unknown, not zero.** An account given no starting capital has no
denominator. Dividing by it would report "-100% return" for an account that is
flat, which is worse than reporting nothing, so every ratio here can be None and
the caller renders "—".

**Denominator follows the range.** KPIs are date-filtered, so the capital being
divided into is the capital *at the start of that range*: the account's starting
balance plus every cash flow before it. A deposit made mid-range funds trades
after it, not before, so counting it from day one would understate early returns
and overstate late ones.

**Drawdown is against that same base.** The cumulative curve the KPI builds
starts at zero and the code measures drawdown as the deepest dip from its own
peak — a dollar figure. Expressed against equity it means: of the money that was
at risk in this period, how much did the peak-to-trough give back.
"""
from __future__ import annotations

import sqlite3


def cash_for(conn: sqlite3.Connection, account_id: int | None,
             date_from: str | None = None) -> tuple[float | None, float, list[dict], int]:
    """(capital, net flows before the range, the flow rows, unknown accounts).

    `date_from` is exclusive: a deposit on the day the period starts already
    funded trades on that day, so it belongs to the base rather than to the
    period's flows.

    Capital is None when it cannot be known. Per account that is a missing or
    null `starting_capital`. For the all-accounts view it is summed across
    accounts, and returns None as soon as *any* account has not been given a
    figure — a total that silently omits one account is not the account's total,
    and the fourth value counts how many were missing so the caller can say so.
    """
    if account_id is None:
        return _all_accounts_cash(conn, date_from)

    row = conn.execute(
        "SELECT starting_capital FROM accounts WHERE id = ?", (account_id,)).fetchone()
    starting = None
    if row is not None and row[0] is not None:
        starting = float(row[0])

    flows, net = _flows(conn, account_id, date_from)
    return starting, net, flows, 0


def _all_accounts_cash(conn: sqlite3.Connection, date_from: str | None
                       ) -> tuple[float | None, float, list[dict], int]:
    rows = conn.execute("SELECT id, starting_capital FROM accounts").fetchall()
    if not rows:
        return None, 0.0, [], 0

    unknown = sum(1 for _id, capital in rows if capital is None)
    total = None if unknown else sum(float(c) for _id, c in rows)

    flows: list[dict] = []
    net = 0.0
    for account_id, _ in rows:
        account_flows = _flows(conn, account_id, date_from)[0]
        flows.extend(account_flows)
        net += sum(f["amount"] if f["kind"] == "deposit" else -f["amount"]
                   for f in account_flows)
    flows.sort(key=lambda f: (f["flow_date"], f["id"]))
    return total, round(net, 2), flows, unknown


def _flows(conn: sqlite3.Connection, account_id: int, date_from: str | None
           ) -> tuple[list[dict], float]:
    if date_from:
        rows = conn.execute(
            "SELECT id, kind, amount, flow_date, note FROM account_cash_flows "
            "WHERE account_id = ? AND flow_date < ? ORDER BY flow_date, id",
            (account_id, date_from)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, kind, amount, flow_date, note FROM account_cash_flows "
            "WHERE account_id = ? ORDER BY flow_date, id",
            (account_id,)).fetchall()
    flows = [{"id": r[0], "kind": r[1], "amount": r[2], "flow_date": r[3], "note": r[4]}
             for r in rows]
    net = sum(f["amount"] if f["kind"] == "deposit" else -f["amount"] for f in flows)
    return flows, round(net, 2)


def base_equity(starting_capital: float | None, net_flows: float) -> float | None:
    """The denominator, or None when it cannot be known.

    Net flows alone are not a substitute for an unknown starting balance: an
    account that took $10,000 out has capital 0 and a real $10,000 of exposure
    has never been given, so no percentage of it means anything.
    """
    if starting_capital is None:
        return None
    return round(starting_capital + net_flows, 2)


def ratios(*, total_net_pnl: float, max_drawdown: float,
           daily_pnl: list[dict] | None,
           starting_capital: float | None, net_flows: float) -> dict:
    """Return, drawdown and risk as percentages of equity.

    `daily_pnl` is what the KPI builds: `[{date, net_pnl, cumulative}, ...]`,
    cumulative starting at 0 and running over the range. Peak equity inside the
    period is base + the best cumulative point, which is what a drawdown on top
    of a funded account is actually a drawdown *from*.
    """
    base = base_equity(starting_capital, net_flows)
    if base is None or base <= 0:
        # Unknown or no money put in: every percentage below would be a division
        # by zero or by something that was never true. The dollar figures stand,
        # the ratios do not.
        return {
            "equity_base": base,
            "return_pct": None,
            "max_drawdown_pct": None,
            "peak_equity": None,
            "avg_loss_pct": None,
            "largest_loss_pct": None,
        }

    peak_cumulative = max((d.get("cumulative") or 0.0) for d in (daily_pnl or [])) \
        if daily_pnl else 0.0
    peak_equity = round(base + max(peak_cumulative, 0.0), 2)

    losses = [abs(d["net_pnl"]) for d in (daily_pnl or []) if (d.get("net_pnl") or 0) < 0]
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    largest_loss = max(losses) if losses else 0.0

    return {
        "equity_base": base,
        "return_pct": round(total_net_pnl / base * 100, 2),
        # Drawdown against the money that was at risk, not against the peak
        # figure alone: a 50% dip off a peak of 2× the account is a 100% loss.
        "max_drawdown_pct": round(abs(max_drawdown) / base * 100, 2),
        "peak_equity": peak_equity,
        # Average losing *day* as a share of equity. The bullet's "risk as a
        # share of account" wants per-trade planned risk, which item 5 does not
        # have yet — this is the honest stand-in that exists today, labelled so
        # nobody mistakes it for stop-distance risk.
        "avg_loss_pct": round(avg_loss / base * 100, 2),
        "largest_loss_pct": round(largest_loss / base * 100, 2),
    }


def summarize(conn: sqlite3.Connection, *, account_id: int | None,
              date_from: str | None, total_net_pnl: float, max_drawdown: float,
              daily_pnl: list[dict]) -> dict:
    """The block KPIs and Reports share, built from the numbers they already have.

    `unknown_accounts` only ever matters for the all-accounts view: it is how
    many accounts have never been given a capital figure, so the caller can say
    *why* the percentages are absent instead of leaving them mysteriously blank.
    """
    starting, net_flows, flows, unknown = cash_for(conn, account_id, date_from)
    out = ratios(
        total_net_pnl=total_net_pnl,
        max_drawdown=max_drawdown,
        daily_pnl=daily_pnl,
        starting_capital=starting,
        net_flows=net_flows,
    )
    out.update({
        "starting_capital": starting,
        "net_flows": net_flows,
        "flow_count": len(flows),
        "unknown_accounts": unknown,
    })
    return out
