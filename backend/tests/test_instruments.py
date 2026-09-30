"""MT5 instrument types: classification, labels, and the trades CHECK widening.

    cd backend && python -m pytest tests -q

The old schema only accepted STOCK/OPTION/FUTURE, so none of the assets in an
MT5 sample (EURUSD, XAUUSD, US500) could be stored at all.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import database  # noqa: E402
import instruments  # noqa: E402


# ── What the schema accepts ────────────────────────────────────────────────────

def test_the_three_legacy_types_are_still_in_the_canonical_set():
    for legacy in ("STOCK", "OPTION", "FUTURE"):
        assert legacy in instruments.INSTRUMENT_TYPES


def test_mt5_asset_classes_are_part_of_the_canonical_set():
    for added in ("FX", "METAL", "INDEX"):
        assert added in instruments.INSTRUMENT_TYPES


def test_the_check_expression_parses_back_to_the_canonical_set():
    parsed = database.instrument_check_values(
        f"CREATE TABLE trades (instrument_type TEXT CHECK(instrument_type IN ({instruments.CHECK_EXPR})))"
    )
    assert parsed == set(instruments.INSTRUMENT_TYPES)


def test_a_sql_check_with_no_instrument_clause_reports_none():
    assert database.instrument_check_values(
        "CREATE TABLE trades (id INTEGER, side TEXT CHECK(side IN ('LONG')))"
    ) is None


def test_the_widened_ddl_keeps_the_conflict_target_imports_depend_on():
    """Grouping writes with ON CONFLICT(trade_group, account_id); a rebuild that
    dropped UNIQUE would silently turn every re-import into a duplicate."""
    widened = database._new_trades_sql(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, trade_group TEXT NOT NULL, "
        "account_id INTEGER NOT NULL, instrument_type TEXT NOT NULL "
        "CHECK(instrument_type IN ('STOCK','OPTION','FUTURE')), "
        "UNIQUE(trade_group, account_id))"
    )
    assert "UNIQUE(trade_group, account_id)" in widened
    assert database.instrument_check_values(widened) == set(instruments.INSTRUMENT_TYPES)


# ── Classification ─────────────────────────────────────────────────────────────

def test_symbol_path_decides_the_class_rather_than_ticker_spelling():
    # Straight from the exporter's sample rows.
    assert instruments.classify_mt5("EURUSD", "Forex\\Majors\\EURUSD") == "FX"
    assert instruments.classify_mt5("XAUUSD", "Metals\\XAUUSD") == "METAL"
    assert instruments.classify_mt5("US500", "Indices\\US500") == "INDEX"


def test_a_deeper_path_still_finds_the_group():
    assert instruments.classify_mt5("US500", "CFD\\Indices\\US500") == "INDEX"


def test_without_a_path_the_ticker_is_classified_by_shape():
    assert instruments.classify_mt5("EURUSD") == "FX"
    assert instruments.classify_mt5("USDJPY") == "FX"
    assert instruments.classify_mt5("XAUUSD") == "METAL"
    assert instruments.classify_mt5("TSLA") == "STOCK"


def test_a_micro_lot_suffix_is_fx():
    # EURUSD.m is a cent/minor lot on IC Markets, not a different asset.
    assert instruments.classify_mt5("EURUSD.m", "Forex\\Majors\\EURUSD.m") == "FX"
    assert instruments.classify_mt5("EURUSD.m") == "FX"


def test_an_empty_symbol_falls_back_to_stock_rather_than_raising():
    assert instruments.classify_mt5("", "Forex\\Majors\\EURUSD") == "FX"
    assert instruments.classify_mt5("") == "STOCK"


def test_common_statement_spellings_normalise():
    assert instruments.normalize("forex") == "FX"
    assert instruments.normalize("futures") == "FUTURE"
    assert instruments.normalize("STK") == "STOCK"
    assert instruments.normalize("METALS") == "METAL"
    assert instruments.normalize("indexes") is None
    assert instruments.normalize(None) is None
    assert instruments.normalize("BOND") is None


# ── Display labels ─────────────────────────────────────────────────────────────

def test_every_canonical_type_has_a_human_label():
    for t in instruments.INSTRUMENT_TYPES:
        assert instruments.label(t), f"{t} has no label"


# ── Value per unit of quantity ─────────────────────────────────────────────────

def test_each_type_prices_one_unit_of_quantity():
    assert instruments.units_per_lot("STOCK") == 1
    assert instruments.units_per_lot("OPTION") == 100
    assert instruments.units_per_lot("METAL") == 100
    assert instruments.units_per_lot("INDEX") == 1
    assert instruments.units_per_lot("FX") == 100_000


def test_fx_scales_like_the_broker_reports_for_a_major_pair():
    # 1 lot moving 25 pips: 0.00250 * 100,000 = 250.00, matching deal 5004.
    assert (1.08250 - 1.08000) * instruments.units_per_lot("FX") * 1.0 == pytest.approx(250.0)


def test_an_unknown_future_point_value_is_none_instead_of_one():
    # Guessing 1 understates /ES by fifty times.
    assert instruments.units_per_lot("FUTURE", "/ZZU26", {"/ES": 50}) is None
    assert instruments.units_per_lot("FUTURE", "/MESU26", {"/ES": 50, "/MES": 5}) == 5
    assert instruments.units_per_lot("FUTURE", "/ES") is None


def test_the_longest_matching_future_root_wins():
    # /MESU26 must not resolve through the /ES prefix.
    assert instruments.units_per_lot("FUTURE", "/MESH26", {"/ES": 50, "/MES": 5}) == 5
