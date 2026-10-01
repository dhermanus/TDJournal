"""Diary extraction: forced schema, and confidence Python checks itself.

    cd backend && python -m pytest tests/test_diary_matches.py -q

The claim under test: a match is only as good as the evidence for it, and the
stored confidence is the one the trade list supports — not the one the model
asserted. The model's own label survives alongside it as `model_match_confidence`
so the disagreement stays visible.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from diary_matches import (
    CONFIDENCE_LEVELS,
    DIARY_ANALYSIS_SCHEMA,
    analyse_confidence,
    match_confidence,
    queued_for_review,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def trade(group, ticker, avg_entry=100.0, first_entry_time="09:30:00", **extra):
    base = {"trade_group": group, "ticker": ticker, "instrument_type": "STOCK",
            "side": "LONG", "avg_entry": avg_entry, "avg_exit": None,
            "first_entry_time": first_entry_time, "last_exit_time": None,
            "net_pnl": 12.5}
    base.update(extra)
    return base


def claim(**extra):
    base = {"trade_group": None, "ticker": "NVDA", "match_confidence": "low",
            "match_notes": "only trade that day", "entry_price_diary": None,
            "diary_time": None, "stop_loss": None, "r_multiple": None}
    base.update(extra)
    return base


# ── the schema on the wire ────────────────────────────────────────────────────

def test_schema_requires_what_the_flow_needs():
    schema = DIARY_ANALYSIS_SCHEMA["input_schema"]
    assert set(schema["required"]) >= {"diary_date", "trade_analyses"}
    props = schema["properties"]["trade_analyses"]["items"]["properties"]
    assert set(props["match_confidence"]["enum"]) == set(CONFIDENCE_LEVELS)
    assert "trade_group" in props
    assert props["r_multiple"]["type"] == ["number", "null"], "R must be extractable as data"
    assert props["stop_loss"]["type"] == ["number", "null"]
    # A schema `enum` cannot express "one of these or null", so emotional_state
    # must not carry one — a null there would fail the whole upload.
    assert "enum" not in props["emotional_state"]
    assert DIARY_ANALYSIS_SCHEMA["name"] == "record_diary_analysis"


def test_schema_is_serialisable_for_the_api_call():
    # The SDK json-encodes this on the way out; a non-string enum member would
    # only fail there, in production, on a real upload.
    json.dumps(DIARY_ANALYSIS_SCHEMA)


# ── confidence computed from the trades, not claimed by the model ─────────────

def test_price_within_fifty_cents_is_high_even_when_the_model_said_low():
    c = trade("NVDA_1", "NVDA", avg_entry=107.67)
    got = match_confidence(c, claim(match_confidence="low", trade_group="NVDA_1",
                                    entry_price_diary=107.50))
    assert got["match_confidence"] == "high"
    assert "within $0.50" in got["match_notes"]
    # the model's own verdict is kept, not silently replaced
    assert got["model_match_confidence"] == "low"
    assert got["model_match_notes"] == "only trade that day"


def test_price_within_fifty_cents_is_still_low_on_a_000_s_dollar_stock():
    # $0.50 of a $4,000 name is meaningless: the diary's own digits must agree.
    c = trade("BRK_1", "BRK.B", avg_entry=4000.00)
    got = match_confidence(c, claim(trade_group="BRK_1", entry_price_diary=4000.30,
                                    match_confidence="high"))
    assert got["match_confidence"] != "high"
    assert got["model_match_confidence"] == "high"


def test_time_within_fifteen_minutes_is_medium():
    c = trade("NVDA_1", "NVDA", avg_entry=107.67, first_entry_time="09:46:00")
    got = match_confidence(c, claim(trade_group="NVDA_1", diary_time="09:55:00",
                                    match_confidence="high"))
    assert got["match_confidence"] == "medium"
    assert "min" in got["match_notes"]
    # and just past the window it is not
    far = match_confidence(c, claim(trade_group="NVDA_1", diary_time="10:30:00",
                                    match_confidence="high"))
    assert far["match_confidence"] != "medium"


def test_two_trades_for_one_ticker_without_price_or_time_is_ambiguous():
    c = trade("NVDA_1", "NVDA", first_entry_time="09:46:00")
    got = match_confidence(c, claim(trade_group="NVDA_1", match_confidence="high",
                                    candidates=2))
    assert got["match_confidence"] == "ambiguous"
    assert "2 trades" in got["match_notes"]


def test_single_trade_for_that_ticker_is_low():
    c = trade("NVDA_1", "NVDA", first_entry_time="09:46:00")
    got = match_confidence(c, claim(trade_group="NVDA_1", match_confidence="high",
                                    candidates=1))
    assert got["match_confidence"] == "low"


def test_a_ticker_the_day_does_not_contain_is_unmatched():
    assert match_confidence(None, claim(match_confidence="high"))["match_confidence"] == "unmatched"
    wrong = trade("NVDA_1", "TSLA")
    assert match_confidence(wrong, claim(trade_group="NVDA_1", ticker="NVDA"))["match_confidence"] == "unmatched"


def test_an_unusable_model_label_is_recorded_not_believed():
    c = trade("NVDA_1", "NVDA", avg_entry=100.0)
    got = match_confidence(c, claim(trade_group="NVDA_1", match_confidence="sure"))
    assert got["model_match_confidence"] == "ambiguous"
    assert got["match_confidence"] == "low"


# ── a whole analysis ──────────────────────────────────────────────────────────

def _analysis(*claims):
    return {"diary_date": "2026-07-01", "overall_summary": "ok",
            "patterns_identified": [], "improvement_areas": [],
            "trade_analyses": [dict(c) for c in claims]}


def test_a_named_ticker_resolves_to_that_days_only_trade():
    ctx = [trade("NVDA_1", "NVDA", avg_entry=107.67, first_entry_time="09:46:00"),
           trade("AMD_1", "AMD", avg_entry=150.00, first_entry_time="10:05:00")]
    out = analyse_confidence(_analysis(claim(ticker="NVDA", trade_group=None,
                                             match_confidence="unmatched")), ctx)
    ta = out["trade_analyses"][0]
    assert ta["trade_group"] == "NVDA_1", "a lone ticker on that date is identified"
    assert ta["match_confidence"] == "low"
    assert ta["candidates"] == 1


def test_a_duplicated_trade_group_keeps_one_entry_and_says_why():
    ctx = [trade("NVDA_1", "NVDA")]
    out = analyse_confidence(_analysis(
        claim(ticker="NVDA", trade_group="NVDA_1"),
        claim(ticker="NVDA", trade_group="NVDA_1"),
    ), ctx)
    assert len(out["trade_analyses"]) == 1
    assert any("duplicate" in d for d in out["dropped"])


def test_a_trade_group_from_another_day_is_dropped_rather_than_attached():
    ctx = [trade("NVDA_1", "NVDA")]
    out = analyse_confidence(_analysis(
        claim(ticker="NVDA", trade_group="NVDA_999")), ctx)
    assert out["trade_analyses"] == []
    assert any("not a trade from this day" in d for d in out["dropped"])


def test_an_entry_with_no_ticker_is_dropped():
    out = analyse_confidence(_analysis({"trade_group": None, "ticker": "  ",
                                        "match_confidence": "low"}), [])
    assert out["trade_analyses"] == []
    assert any("no ticker" in d for d in out["dropped"])


def test_defaults_are_filled_in_when_the_model_omits_the_envelope():
    out = analyse_confidence({"trade_analyses": [dict(claim())]}, [])
    assert out["diary_date"] == ""
    assert out["patterns_identified"] == []
    assert out["improvement_areas"] == []


def test_an_analysis_that_is_not_an_object_cannot_crash_the_upload():
    out = analyse_confidence({"trade_analyses": "not a list"}, [])
    assert out["trade_analyses"] == []


# ── the review queue ──────────────────────────────────────────────────────────

def test_the_queue_is_weakest_first_and_leaves_confirmed_matches_alone():
    # analyse_confidence judges raw model output, which never says "manual"; a
    # confirmation arrives later, so the levels are set the way the endpoint
    # writes them.
    out = analyse_confidence(_analysis(
        claim(ticker="AAA", trade_group=None, match_confidence="unmatched"),
        claim(ticker="BBB", trade_group="B_1", match_confidence="high", entry_price_diary=1.0),
        claim(ticker="CCC", trade_group="C_1", match_confidence="low"),
        claim(ticker="DDD", trade_group="D_1", match_confidence="high", entry_price_diary=1.0),
    ), [trade("B_1", "BBB", avg_entry=1.0), trade("C_1", "CCC", avg_entry=1.0),
        trade("D_1", "DDD", avg_entry=1.0)])
    for ta in out["trade_analyses"]:
        if ta["ticker"] == "CCC":
            ta["match_confidence"] = "low"
        if ta["ticker"] == "DDD":
            ta["match_confidence"] = "manual"   # confirmed by the user
    levels = [t["match_confidence"] for t in queued_for_review(out)]
    assert levels == ["unmatched", "low", "high"]
    assert "manual" not in levels, "a confirmed match is out of the queue"
    assert queued_for_review({}) == []
    assert queued_for_review({"trade_analyses": "nope"}) == []


# ── reading the structured response ───────────────────────────────────────────

def _tool_response(payload):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=payload)],
                           stop_reason="tool_use")


def _text_response(text, stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason=stop_reason)


def test_the_forced_tool_call_is_read_straight_off_the_response():
    from ai_analysis import diary_tool_input
    payload = {"diary_date": "2026-07-01", "trade_analyses": []}
    assert diary_tool_input(_tool_response(payload)) == payload


def test_a_thinking_block_before_the_tool_call_does_not_hide_it():
    # On Opus 5 thinking precedes the answer, so content[0] is not the payload.
    from ai_analysis import diary_tool_input
    payload = {"diary_date": "2026-07-01", "trade_analyses": []}
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking="…"),
                 SimpleNamespace(type="tool_use", input=payload)],
        stop_reason="tool_use")
    assert diary_tool_input(resp) == payload


def test_a_fenced_text_answer_is_still_readable_as_a_fallback():
    from ai_analysis import diary_tool_input
    raw = '```json\n{"diary_date": "2026-07-01", "trade_analyses": []}\n```'
    assert diary_tool_input(_text_response(raw))["diary_date"] == "2026-07-01"


def test_an_empty_answer_reports_the_real_problem():
    from ai_analysis import diary_tool_input
    with pytest.raises(ValueError, match="empty"):
        diary_tool_input(_text_response(""))


def test_unreadable_json_is_named_not_swallowed():
    from ai_analysis import diary_tool_input
    with pytest.raises(ValueError, match="not readable as JSON"):
        diary_tool_input(_text_response("nope"))


def test_a_truncated_response_fails_before_it_is_parsed():
    from ai_analysis import diary_tool_input
    with pytest.raises(ValueError, match="limit"):
        diary_tool_input(_text_response("x", stop_reason="max_tokens"))
