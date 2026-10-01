"""Broker-server timestamp -> UTC conversion for MT5 imports.

MT5 deal and bar exports carry raw broker-server time with no timezone
attached. Whether `2026.01.22 16:04:46` means 14:04 or 13:04 UTC depends on
the server's daylight-saving rule *on that date*, not on today's offset, so
the conversion cannot be a fixed number of hours.

The rule is held as an IANA zone name (e.g. `Europe/Athens` for a server that
runs EET/EEST) and applied through zoneinfo, which carries the full historical
transition table. One setting therefore covers a history that spans several
seasons, and changing it re-derives every timestamp without re-exporting from
MT5.

Timestamps are stored in TDJournal as naive UTC strings: `date` is YYYY-MM-DD
and `time` is HH:MM:SS. Conversion happens before grouping, because trade
groups are ordered by date and an unconverted winter fill would sort into the
wrong session.

zoneinfo reads its transition tables from the OS on Linux and macOS; the
Windows installer ships none. `tzdata` supplies them there, so it is a required
dependency rather than an optional one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import tzdata as _tzdata  # noqa: F401  (registers the package for zoneinfo)
except ImportError:  # pragma: no cover - depends on install, not code
    _tzdata = None

# MT5 writes `2026.06.15 09:00:00` (TimeToString with TIME_DATE|TIME_SECONDS).
MT5_DEAL_TIME_FORMATS = ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")

SETTINGS_KEY_TIMEZONE = "mt5_server_timezone"
CANDIDATE_ZONES = [
    "Africa/Johannesburg", "America/Chicago", "America/New_York",
    "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney",
    "Europe/Athens", "Europe/London", "Europe/Moscow", "Etc/GMT-3",
]


class TimezoneError(ValueError):
    """The configured zone name is unusable — the import must not guess."""


@dataclass(frozen=True)
class ConvertedTime:
    """One converted timestamp, kept with its provenance for the import report."""
    iso_date: str          # YYYY-MM-DD, UTC
    time: str              # HH:MM:SS, UTC
    server_text: str       # the raw string as exported
    offset_hours: float    # UTC offset that applied on that date (signed)
    # Daylight-saving edge cases. A clock stepping forward skips one hour of
    # local time, a clock stepping back repeats one. A fill inside either window
    # is still imported, but flagged so the import report can name the rows to
    # eyeball instead of silently picking one of the two possible instants.
    ambiguous: bool = False     # this local time happened twice
    nonexistent: bool = False   # this local time never happened


def load_zone(tz_name: str) -> ZoneInfo:
    """Resolve an IANA name. Raises TimezoneError rather than falling back."""
    name = (tz_name or "").strip()
    if not name:
        raise TimezoneError(
            "No MT5 server timezone is set. Set it in Settings, or import "
            "timestamps will not be converted."
        )
    if _tzdata is None and not _has_system_tz_database():
        raise TimezoneError(
            "The 'tzdata' package is not installed, so this machine has no "
            "daylight-saving database. Run: pip install -r backend/requirements.txt"
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TimezoneError(
            f"{name!r} is not a known timezone. Use an IANA name such as "
            "Europe/Athens or America/New_York."
        ) from exc


def _has_system_tz_database() -> bool:
    """True when the OS can resolve a zone without the tzdata package."""
    try:
        ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 - any failure means no usable database
        return False
    return True


def parse_server_timestamp(text: str) -> datetime:
    """Parse an MT5 server timestamp into a naive datetime (server local time)."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("missing timestamp")
    for fmt in MT5_DEAL_TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp {raw!r}")


def _dst_flags(server_local: datetime, zone: ZoneInfo) -> tuple[bool, bool]:
    """Detect the two clock-skew windows around a daylight-saving transition.

    Ambiguous: fold=0 and fold=1 disagree, so the wall time occurred twice
    (the clock was set back). Nonexistent: the UTC instant the clock would
    represent lands in a different wall time on the way back (it fell in the
    hour the clock jumped over).

    Nonexistent is checked first because it also makes the two folds disagree —
    a spring-forward gap looks like a duplicate if you test fold alone. They
    are mutually exclusive in practice: an hour cannot both be skipped and
    repeated by the same clock change.
    """
    standard = server_local.replace(tzinfo=zone, fold=0)
    back = standard.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if back != server_local:
        return False, True

    folded = server_local.replace(tzinfo=zone, fold=1)
    return folded.utcoffset() != standard.utcoffset(), False


def server_to_utc(text: str, tz_name: str) -> ConvertedTime:
    """Convert one server-local timestamp to naive UTC, recording the offset used.

    For an ambiguous wall time the first occurrence (fold=0) is taken, so a
    fall-back transition converts a duplicate hour with the daylight offset
    that was in force before the clock changed. That is flagged, not hidden.
    """
    server_local = parse_server_timestamp(text)
    zone = load_zone(tz_name)

    # Attach the zone, then drop to UTC. zoneinfo picks the transition that was
    # in force on this date, so a winter deal and a summer deal get different
    # offsets without the caller having to know about daylight saving.
    aware = server_local.replace(tzinfo=zone, fold=0)
    as_utc = aware.astimezone(timezone.utc)

    offset = aware.utcoffset()
    offset_hours = offset.total_seconds() / 3600.0 if offset else 0.0
    ambiguous, nonexistent = _dst_flags(server_local, zone)

    return ConvertedTime(
        iso_date=as_utc.strftime("%Y-%m-%d"),
        time=as_utc.strftime("%H:%M:%S"),
        server_text=text,
        offset_hours=offset_hours,
        ambiguous=ambiguous,
        nonexistent=nonexistent,
    )


def convert_executions(executions: list[dict], tz_name: str) -> list[dict]:
    """Convert every execution's date/time to UTC in place.

    Executions without a usable timestamp are left untouched so a later stage
    can report them; they are never stamped with a guessed time. Rows that land
    in a daylight-saving edge case are marked with `time_dst_flag` so the import
    report can surface them.
    """
    if not executions:
        return executions

    load_zone(tz_name)   # validate once, up front
    for ex in executions:
        source = ex.get("server_time") or ex.get("iso_date") or ex.get("date")
        if not source:
            continue
        if ex.get("time") and not str(source).endswith(str(ex["time"])):
            source = f"{source} {ex['time']}"
        try:
            converted = server_to_utc(source, tz_name)
        except ValueError:
            continue
        ex["date"] = converted.iso_date
        ex["iso_date"] = converted.iso_date
        ex["time"] = converted.time
        ex["server_offset_hours"] = converted.offset_hours
        if converted.nonexistent:
            ex["time_dst_flag"] = "nonexistent"
        elif converted.ambiguous:
            ex["time_dst_flag"] = "ambiguous"
        else:
            ex.pop("time_dst_flag", None)
        ex.pop("server_time", None)
    return executions


def utc_to_server_hhmm(date_text: str, time_text: str, tz_name: str) -> str:
    """Format a stored naive-UTC timestamp as broker-server `HH:MM`, or "".

    The report buckets fills by the clock the broker ran on, which is the one a
    journal entry was actually written against. The offset is looked up on the
    fill's own date, so a summer fill and a winter fill land in different hours
    without the caller knowing the server's daylight-saving rule.

    Unparseable input returns "" rather than guessing: the caller drops the
    trade from the chart and says so, instead of placing it at midnight.
    """
    zone = load_zone(tz_name)
    try:
        day = (date_text or "").strip()
        clock = (time_text or "").strip()
        if not day or not clock:
            return ""
        # Stored time may be HH:MM:SS or already HH:MM; accept both.
        if len(clock) == 5:
            clock = f"{clock}:00"
        utc_naive = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    server = utc_naive.replace(tzinfo=timezone.utc).astimezone(zone)
    return server.strftime("%H:%M")


def resolve_timezone(setting_value: str | None, env_value: str | None = None) -> str:
    """Pick the zone to use: database setting first, then environment, else blank."""
    for candidate in (setting_value, env_value):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def dst_summary(zone_name: str, from_year: int, to_year: int) -> list[dict]:
    """List the daylight-saving transitions in a range, for the settings screen.

    Lets a user confirm a candidate zone against the broker's stated rule before
    trusting it with a history of fills.
    """
    zone = load_zone(zone_name)
    out: list[dict] = []
    # Step hourly across the range; Athens changes on a known date, and an
    # hourly scan finds any transition without assuming which Sunday it is.
    probe = datetime(from_year, 1, 1, tzinfo=timezone.utc)
    end = datetime(to_year, 1, 1, tzinfo=timezone.utc)
    previous_offset = probe.astimezone(zone).utcoffset()
    while probe < end:
        current = probe.astimezone(zone)
        offset = current.utcoffset()
        if offset != previous_offset:
            out.append({
                "at_utc": probe.strftime("%Y-%m-%d %H:%M:%S"),
                "offset_hours_before": (
                    previous_offset.total_seconds() / 3600.0 if previous_offset else 0.0
                ),
                "offset_hours_after": offset.total_seconds() / 3600.0 if offset else 0.0,
            })
            previous_offset = offset
        probe += timedelta(hours=1)
    return out
