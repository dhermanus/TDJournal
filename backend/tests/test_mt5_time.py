"""UTC conversion for raw MT5 server timestamps; include seasonal edge cases."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from mt5_time import (  # noqa: E402
    TimezoneError,
    convert_executions,
    dst_summary,
    load_zone,
    parse_server_timestamp,
    resolve_timezone,
    server_to_utc,
)


def test_ic_markets_style_zone_converts_winter_and_summer_with_different_offsets():
    winter = server_to_utc("2026.01.22 16:04:46", "Europe/Athens")
    summer = server_to_utc("2025.07.29 06:24:17", "Europe/Athens")

    assert (winter.iso_date, winter.time, winter.offset_hours) == (
        "2026-01-22", "14:04:46", 2.0
    )
    assert (summer.iso_date, summer.time, summer.offset_hours) == (
        "2025-07-29", "03:24:17", 3.0
    )


def test_conversion_can_roll_into_previous_utc_day():
    result = server_to_utc("2026.01.01 01:00:00", "Europe/Athens")
    assert (result.iso_date, result.time) == ("2025-12-31", "23:00:00")


def test_europe_athens_spring_transition_uses_standard_offset_before_transition():
    # Europe/Athens changes from UTC+2 to UTC+3 on 2026-03-29.
    before = server_to_utc("2026.03.29 02:59:00", "Europe/Athens")
    after = server_to_utc("2026.03.29 04:00:00", "Europe/Athens")
    assert before.offset_hours == 2.0
    assert (before.iso_date, before.time) == ("2026-03-29", "00:59:00")
    assert after.offset_hours == 3.0
    assert (after.iso_date, after.time) == ("2026-03-29", "01:00:00")


def test_europe_athens_fall_transition_uses_standard_offset_after_transition():
    # Europe/Athens changes from UTC+3 to UTC+2 on 2026-10-25.
    before = server_to_utc("2026.10.25 03:59:00", "Europe/Athens")
    after = server_to_utc("2026.10.25 04:00:00", "Europe/Athens")
    assert before.offset_hours == 3.0
    assert after.offset_hours == 2.0


def test_missing_or_invalid_timezone_never_silently_guesses():
    with pytest.raises(TimezoneError, match="No MT5 server timezone"):
        load_zone("")
    with pytest.raises(TimezoneError, match="not a known timezone"):
        load_zone("Not/A_Real_Zone")


def test_parse_server_timestamp_accepts_export_formats_and_rejects_bad_values():
    assert parse_server_timestamp("2026.06.15 09:00:00") == datetime(2026, 6, 15, 9)
    assert parse_server_timestamp("2026-06-15 09:00:00") == datetime(2026, 6, 15, 9)
    with pytest.raises(ValueError, match="missing timestamp"):
        parse_server_timestamp("")
    with pytest.raises(ValueError, match="unrecognised"):
        parse_server_timestamp("yesterday morning")


def test_convert_executions_updates_utc_date_and_time_before_grouping():
    executions = [{
        "date": "2026.01.22",
        "time": "16:04:46",
        "server_time": "2026.01.22 16:04:46",
        "ticker": "EURUSD",
    }]
    got = convert_executions(executions, "Europe/Athens")
    assert got is executions
    assert executions[0]["date"] == "2026-01-22"
    assert executions[0]["iso_date"] == "2026-01-22"
    assert executions[0]["time"] == "14:04:46"
    assert "server_time" not in executions[0]


def test_convert_executions_leaves_original_on_malformed_timestamp():
    executions = [{"date": "not-a-date", "time": "noon", "ticker": "EURUSD"}]
    convert_executions(executions, "Europe/Athens")
    assert executions[0]["date"] == "not-a-date"
    assert executions[0]["time"] == "noon"


def test_zone_key_is_ianna_not_fixed_offset():
    assert server_to_utc("2026-07-10 12:00:00", "Europe/Athens").offset_hours == 3.0
    assert server_to_utc("2026-01-10 12:00:00", "Europe/Athens").offset_hours == 2.0


def test_nonexistent_local_time_is_converted_and_flagged_not_silently_picked():
    # Athens springs forward 03:00 -> 04:00 on 2026-03-29, so 03:30 never
    # existed on the server clock. Import it, but say so.
    result = server_to_utc("2026.03.29 03:30:00", "Europe/Athens")
    assert result.nonexistent is True
    assert result.ambiguous is False
    # 03:30 folded back to UTC+2 yields 01:30Z, which is 04:30 Athens — the
    # wrong wall time. Flagging is what tells the report to check the row.
    assert (result.iso_date, result.time) == ("2026-03-29", "01:30:00")


def test_ambiguous_local_time_keeps_first_occurrence_and_is_flagged():
    # Athens falls back 04:00 -> 03:00 on 2026-10-25, so 03:30 happened twice.
    result = server_to_utc("2026.10.25 03:30:00", "Europe/Athens")
    assert result.ambiguous is True
    assert result.nonexistent is False
    # fold=0 -> the first occurrence, still on the daylight offset (+3).
    assert (result.time, result.offset_hours) == ("00:30:00", 3.0)


def test_normal_timestamps_are_never_flagged():
    for text in ("2026.01.22 16:04:46", "2025.07.29 06:24:17", "2026.03.29 02:59:00"):
        result = server_to_utc(text, "Europe/Athens")
        assert (result.ambiguous, result.nonexistent) == (False, False), text


def test_convert_executions_reports_dst_flags_and_offset_used():
    executions = [
        {"date": "2026.03.29", "time": "03:30:00", "ticker": "EURUSD"},
        {"date": "2026.10.25", "time": "03:30:00", "ticker": "EURUSD"},
        {"date": "2026.01.22", "time": "16:04:46", "ticker": "EURUSD"},
    ]
    convert_executions(executions, "Europe/Athens")
    assert [e.get("time_dst_flag") for e in executions] == [
        "nonexistent", "ambiguous", None,
    ]
    assert [e["server_offset_hours"] for e in executions] == [2.0, 3.0, 2.0]


def test_resolve_prefers_saved_setting_then_environment_then_blank():
    assert resolve_timezone("Europe/London", "America/New_York") == "Europe/London"
    assert resolve_timezone("", "America/New_York") == "America/New_York"
    assert resolve_timezone("  ", "  ") == ""


def test_dst_summary_lists_both_transitions_per_year_with_opposite_directions():
    transitions = dst_summary("Europe/Athens", 2026, 2027)
    assert len(transitions) == 2
    spring, fall = transitions
    assert spring["at_utc"].startswith("2026-03-29")
    assert (spring["offset_hours_before"], spring["offset_hours_after"]) == (2.0, 3.0)
    assert fall["at_utc"].startswith("2026-10-25")
    assert (fall["offset_hours_before"], fall["offset_hours_after"]) == (3.0, 2.0)
