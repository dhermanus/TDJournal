"""Fractional quantity on the manual entry path.

    cd backend && python3 -m pytest tests -q

The bug this pins (D1 of the 2026-10-06 report): a manual trade could not
record an FX lot. Three layers each refused `0.01` independently —
`TradeCreate.quantity: int` returned 422 `int_from_float`, `_parse_exec_body`
cast with bare `int()` (so `0.01 → 0`, corrupting silently rather than failing),
and the UI's `min="1"` blocked the form submit. The journal it was written
against holds 2,640 fractional fills against 63 whole ones, because the MT5
importer has always stored a float — so the import path and the manual path
disagreed about what a quantity *is*.

Widening the type alone would have been half a fix: `0` and negatives are just
as wrong and would have become writable. Both are asserted rejected here, which
is why the constraints live in the schema (gt=0) and the parser rather than in
a guard only one caller remembers to call.
"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import main  # noqa: E402


# ── the request schema ────────────────────────────────────────────────────────

def body(qty):
    return dict(account_id=1, date="2026-10-06", ticker="EURUSD",
                instrument_type="FX", side="LONG",
                entry_price=1.15, exit_price=1.16, quantity=qty)


@pytest.mark.parametrize("qty", [1, 0.01, 0.001, 14.76, 1.5])
def test_a_fractional_quantity_is_accepted(qty):
    """An FX lot, a micro-lot and a whole share all have to fit the same field."""
    trade = main.TradeCreate(**body(qty))
    assert trade.quantity == qty
    assert isinstance(trade.quantity, float)


@pytest.mark.parametrize("qty", [0, -1, -0.01])
def test_a_quantity_that_is_not_a_position_is_rejected(qty):
    """Zero is a fill that never happened; negative flips the sign of the P&L.

    Both are rejected by the schema rather than by a check in the endpoint, so
    no caller can forget them.
    """
    with pytest.raises(Exception) as exc:
        main.TradeCreate(**body(qty))
    assert "quantity" in str(exc.value)


def test_an_integer_still_parses_as_a_quantity():
    """Existing payloads send 100; nothing about that changes."""
    assert main.TradeCreate(**body(100)).quantity == 100


# ── the execution editor ─────────────────────────────────────────────────────

def test_a_fill_keeps_its_fraction():
    """`int(body['qty'])` was turning a 0.1-lot fill into 0 — written, not
    refused, so the corruption only surfaced in the invariant test."""
    parsed = main._parse_exec_body({"action": "BOT", "qty": 0.1, "price": 1.15}, "2026-10-06")
    assert parsed["qty"] == 0.1
    assert isinstance(parsed["qty"], float)


@pytest.mark.parametrize("qty", [0, -0.5])
def test_a_fill_without_a_size_is_refused_with_a_readable_message(qty):
    """400 with this text, not a zero-sized fill written to the journal."""
    with pytest.raises(ValueError, match="greater than zero"):
        main._parse_exec_body({"action": "BOT", "qty": qty, "price": 1.15}, "2026-10-06")


# ── the arithmetic these feed ────────────────────────────────────────────────

def test_manual_pnl_accepts_a_fractional_quantity():
    """The type widens; the arithmetic does not change.

    Measured from the function rather than asserted from arithmetic: money
    rounds to cents, so a 0.01-lot move worth $0.001 reports 0.0 — the same
    rounding every currency figure in the app gets. What matters is that the
    call accepts the float and scales it, instead of refusing it outright as
    `int` used to.
    """
    # 0.10 move on 10 units is a dollar: the float goes through
    assert main.compute_manual_pnl("LONG", 1.00, 1.10, 10, 0.0) == (1.0, 1.0)
    # a cent-sized move on one unit lands exactly on the rounding boundary
    assert main.compute_manual_pnl("LONG", 1.00, 1.01, 1, 0.0) == (0.01, 0.01)
    # sub-cent on a micro-lot rounds to zero rather than raising
    assert main.compute_manual_pnl("LONG", 1.00, 1.10, 0.01, 0.0) == (0.0, 0.0)


def test_short_manual_pnl_still_signs_the_other_way():
    gross, _ = main.compute_manual_pnl("SHORT", 1.10, 1.00, 1, 0.0)
    assert gross == pytest.approx(0.1)


def test_whole_quantity_pnl_is_unchanged_by_the_widening():
    """Regression guard: 100 shares at a $0.10 move is still $10."""
    assert main.compute_manual_pnl("LONG", 1.00, 1.10, 100, 0.0) == (10.0, 10.0)


# ── one price unit is not one dollar ────────────────────────────────────────
#
# The second half of D1. Widening the type made an FX trade savable, and then
# the first one that saved showed gross_pnl 0.0 where the import path would have
# reported 0.44: the manual path multiplied by nothing. Two numbers for the same
# fill, differing by how it entered the journal.

def test_an_fx_manual_trade_is_priced_in_contract_size():
    """(1.15342 - 1.15298) * 0.01 lot * 100,000 units = $0.44, not $0.00."""
    gross, net = main.compute_manual_pnl("LONG", 1.15298, 1.15342, 0.01, 0.0,
                                         "FX", "EURUSD")
    assert gross == pytest.approx(0.44)
    assert net == pytest.approx(0.44)


def test_a_short_fx_manual_trade_signs_the_other_way():
    gross, _ = main.compute_manual_pnl("SHORT", 1.15342, 1.15298, 0.01, 0.0,
                                       "FX", "EURUSD")
    assert gross == pytest.approx(0.44)


def test_an_option_manual_trade_uses_the_contract_multiplier():
    """2 contracts moving $0.75 at 100 share-equivalents is $150 gross, $148.70
    net of the $1.30 commission — the same arithmetic the importer does."""
    assert main.compute_manual_pnl("LONG", 3.40, 4.15, 2, 1.30,
                                   "OPTION", "SPY") == (150.0, 148.7)


def test_stock_and_index_stay_at_one_dollar_per_unit():
    """The default still describes a share, so nothing about equities moves."""
    assert main.compute_manual_pnl("LONG", 100.0, 101.0, 100, 0.0,
                                   "STOCK", "AAPL") == (100.0, 100.0)
    assert main.compute_manual_pnl("LONG", 5000.0, 5010.0, 1, 0.0,
                                   "INDEX", "SPX") == (10.0, 10.0)


def test_a_known_future_is_priced_by_its_point_value():
    """Covers the other side of the guard: a contract we *do* know prices
    normally, so the refusal only ever fires for the unknown ones."""
    # /ES is 50 index points per contract
    gross, net = main.compute_manual_pnl("LONG", 6412.25, 6420.50, 1, 2.10,
                                         "FUTURE", "/ES")
    assert gross == pytest.approx((6420.50 - 6412.25) * 50)
    assert net == pytest.approx((6420.50 - 6412.25) * 50 - 2.10)


def test_an_unknown_point_value_is_refused_rather_than_reported_as_zero():
    """/MXYZ has no entry in FUTURES_MULTIPLIERS, so there is nothing to multiply
    by. Reporting 0.0 would read as a flat trade rather than as a number we do
    not have — the importer's rule, now applied to manual entry too."""
    with pytest.raises(ValueError, match="point value"):
        main.compute_manual_pnl("LONG", 6412.25, 6420.50, 1, 2.10, "FUTURE", "/MXYZ")


def test_no_exit_still_reports_nothing_without_the_commission_that_is_real():
    """An open trade has no P&L yet; the commission already paid is still money."""
    assert main.compute_manual_pnl("LONG", 100.0, None, 100, 0.5,
                                   "STOCK", "AAPL") == (0.0, -0.5)
