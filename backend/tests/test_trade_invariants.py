"""The three invariants that make every KPI trustworthy.

    cd backend && python -m pytest tests -q

Trade grouping is the join key for the whole journal: KPIs, the AI analysis,
tags and attachments all hang off a trade_group that a grouping bug can silently
rewrite. These are the properties that must hold no matter how fills arrive, and
they are cheap to check — so they are pinned as assertions rather than left to
a reviewer's eye.

    1. gross_pnl - commissions == net_pnl          (the headline number reconciles)
    2. sum of the trade's own legs == gross_pnl    (the number comes from the fills)
    3. legs net to zero quantity at a trade's close (a position actually closed)

All three hold across the live journal's 1,346 trades today; these tests are
what keeps that true after the next change to grouping.

The third is only asserted for *closed* trades — an open position is expected
not to be flat, and is flagged rather than treated as a failure.

Writing these surfaced one genuine divergence between the two pipelines, pinned
here rather than silently reconciled: for a position still open, the broker-CSV
pipeline leaves net at 0 (the fee is realized only on close) while the MT5
pipeline deducts the fee at once (net = -fee). Both are defensible; they agree
for every closed trade, which is where all three invariants apply.
"""
import json
import random
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import csv_parser  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "mt5_deals.csv"
ATHENS = "Europe/Athens"

# aggregate_executions rounds each figure to 2dp, so a chain of fills can drift
# by a cent or two before the invariant is judged. Anything beyond this is a
# real disagreement, not rounding.
TOL = 0.03


def mt5_trades():
    content = FIXTURE.read_text(encoding="utf-8")
    trades, _ = csv_parser.parse_mt5_csv(content, 1, None, tz_name=ATHENS)
    return trades


def legs_of(trade):
    ex = trade["executions"]
    return json.loads(ex) if isinstance(ex, str) else list(ex or [])


def signed_legs(trades, field):
    """Sum one numeric field across every leg of a trade."""
    return [sum(float(e.get(field) or 0) for e in legs_of(t)) for t in trades]


# ── MT5 pipeline (the one the live journal uses) ──────────────────────────────

def test_gross_minus_commissions_equals_net():
    for t in mt5_trades():
        got = (t["gross_pnl"] or 0) - (t["commissions"] or 0)
        assert abs(got - (t["net_pnl"] or 0)) < TOL, t["trade_group"]


def test_the_trade_total_is_the_sum_of_its_own_fills():
    """Not the broker's restated total — the fills stored on the row itself."""
    for t in mt5_trades():
        assert abs(sum(float(e.get("reported_profit") or 0) for e in legs_of(t))
                   - (t["gross_pnl"] or 0)) < TOL, t["trade_group"]


def test_a_closed_mt5_position_returns_to_flat():
    """A trade only exists because the position closed; its legs must net to 0."""
    for t in mt5_trades():
        if t.get("is_open"):
            continue
        net_qty = sum(
            float(e["qty"]) * (1 if e["action"] == "BOT" else -1)
            for e in legs_of(t)
        )
        assert abs(net_qty) < 1e-9, f"{t['trade_group']} still holds {net_qty}"


def test_an_open_mt5_position_is_flagged_rather_than_counted_as_closed():
    open_trades = [t for t in mt5_trades() if t.get("is_open")]
    assert open_trades, "fixture should contain one still-open position"
    for t in open_trades:
        # An open position books no realized P&L, so gross must not be invented.
        assert (t["gross_pnl"] or 0) == 0, t["trade_group"]


# ── Broker-CSV pipeline (dormant on current data, exercised anyway) ───────────

def make_fill(rng, *, ticker, day, seq, action, qty, price, commission=0.0):
    """One fill in the common shape every broker parser ends in."""
    sign = -1 if action == "BOT" else 1
    amount = sign * qty * price
    return {
        "ticker": ticker,
        "instrument_type": "STOCK",
        "action": action,
        "qty": qty,
        "price": price,
        "commission": commission,
        "amount": round(amount, 2),
        "date": day,
        "iso_date": day,
        "time": f"{9 + (seq // 60):02d}:{seq % 60:02d}:{seq % 60:02d}",
    }


def random_round_trips(seed, trades=40):
    """Random round trips: every trade closes, so all three invariants apply."""
    rng = random.Random(seed)
    fills = []
    for i in range(trades):
        ticker = rng.choice(["AAA", "BBB", "CCC"])
        day = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        qty = rng.choice([1, 5, 10, 25, 100])
        # Prices need two decimals so rounding is deterministic.
        entry = round(rng.uniform(2, 400), 2)
        exit_ = round(entry * rng.uniform(0.9, 1.1), 2)
        commission = round(rng.choice([0.0, 0.5, 1.25, 3.0]), 2)
        fills.append(make_fill(rng, ticker=ticker, day=day, seq=i * 7,
                               action="BOT", qty=qty, price=entry))
        fills.append(make_fill(rng, ticker=ticker, day=day, seq=i * 7 + 3,
                               action="SOLD", qty=qty, price=exit_,
                               commission=commission))
    return fills


@pytest.mark.parametrize("seed", range(8))
def test_random_fill_sets_close_flat(seed):
    groups, _ = csv_parser.group_executions_by_position(random_round_trips(seed))
    assert groups, "generated fills must produce at least one trade"
    for key, fills in groups.items():
        net_qty = sum(
            f["qty"] * (1 if f["action"] == "BOT" else -1) for f in fills
        )
        assert abs(net_qty) < 1e-6, f"{key} did not return to flat: {net_qty}"


def random_open_positions(seed, trades=20):
    """Positions still open at the end of the file: entry legs only.

    An open position has no realized P&L. If grouping ever counts its signed
    entry amounts as a result, an unrealized entry books a large fake loss —
    which is why this case needs its own generator rather than being folded
    into the round-trip one.
    """
    rng = random.Random(seed)
    fills = []
    for i in range(trades):
        ticker = rng.choice(["DDD", "EEE", "FFF"])
        day = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        qty = rng.choice([1, 10, 100])
        price = round(rng.uniform(5, 300), 2)
        action = rng.choice(["BOT", "SOLD"])
        fills.append(make_fill(rng, ticker=ticker, day=day, seq=i * 5,
                               action=action, qty=qty, price=price,
                               commission=round(rng.choice([0.0, 2.0]), 2)))
    return fills


@pytest.mark.parametrize("seed", range(6))
def test_an_open_position_books_no_realized_pnl(seed):
    fills = random_open_positions(seed)
    trades, _ = csv_parser.build_trades_from_executions(fills, 1, conn=None)
    groups, _ = csv_parser.group_executions_by_position(fills)
    assert trades and groups
    for t in trades:
        legs = json.loads(t["executions"])
        net_qty = sum(f["qty"] * (1 if f["action"] == "BOT" else -1) for f in legs)
        if abs(net_qty) > 1e-6:
            # Still holding. gross must stay 0 — entry amounts are not a result.
            assert (t["gross_pnl"] or 0) == 0, (
                f"{t['trade_group']} booked {t['gross_pnl']} of unrealized P&L"
            )
            # The broker-CSV pipeline charges the fee only once the position
            # closes, so an open trade's net is 0 and the fee sits in
            # `commissions` waiting to be realized. The MT5 pipeline does the
            # opposite (it deducts the fee immediately, see below) — the two
            # agree only for closed trades, which is what invariant 1 covers.
            assert (t["net_pnl"] or 0) == 0, t["trade_group"]
        else:
            # Closed after all: the invariants apply in full.
            assert abs((t["gross_pnl"] or 0) - (t["commissions"] or 0)
                       - (t["net_pnl"] or 0)) < TOL


@pytest.mark.parametrize("seed", range(8))
def test_random_fill_sets_reconcile(seed):
    fills = random_round_trips(seed)
    trades, skipped = csv_parser.build_trades_from_executions(fills, 1, conn=None)
    assert skipped == 0, "a fresh in-memory import must skip nothing"
    assert trades

    # Group the same fills directly so the total can be checked against the
    # source amounts — aggregate_executions drops `amount` from the serialized
    # legs, so the JSON alone cannot prove where gross came from.
    groups, _ = csv_parser.group_executions_by_position(fills)
    expected = {
        key: round(sum(f.get("amount", 0.0) for f in group), 2)
        for key, group in groups.items()
    }

    for t in trades:
        gross = t["gross_pnl"] or 0
        net = t["net_pnl"] or 0
        comm = t["commissions"] or 0
        assert abs((gross - comm) - net) < TOL, t["trade_group"]

        legs = json.loads(t["executions"])
        net_qty = sum(f["qty"] * (1 if f["action"] == "BOT" else -1) for f in legs)
        assert abs(net_qty) < 1e-6, f"{t['trade_group']} did not close"

        key = t["trade_group"]
        assert key in expected, key
        assert abs(expected[key] - gross) < TOL, key
