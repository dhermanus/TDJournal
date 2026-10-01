"""Read a diary analysis as typed data, and judge its trade matches.

    cd backend && python -m pytest tests/test_diary_matches.py -q

Two problems this solves, both of which the old path had:

1. The analysis used to come back as prose that a regex scraped into a dict.
   `DIARY_ANALYSIS_SCHEMA` is now passed as a forced tool_use, so the shape is
   guaranteed by the API rather than hoped for.

2. The match confidence used to be whatever the model asserted. It is now
   recomputed in Python against the trades that actually exist for that date —
   the model still writes its own label and reason, but the stored value is the
   one the evidence supports, and `model_*` fields keep the claim for auditing.

Nothing here calls the network. The model produces text; this module checks it.
"""
from __future__ import annotations

import re

# The five levels, worst first. `manual` is what a human confirmation writes and
# is also allowed by the CHECK on trade_analysis.match_confidence.
CONFIDENCE_LEVELS = ("unmatched", "ambiguous", "low", "medium", "high")

_LEVEL_INDEX = {name: i for i, name in enumerate(CONFIDENCE_LEVELS)}

# The exact tool the model is forced to fill in. Putting the schema on the wire
# means the API rejects an answer that does not match it, so `json.loads` of the
# input cannot produce a half-shaped object.
DIARY_ANALYSIS_SCHEMA = {
    "name": "record_diary_analysis",
    "description": "Record one day's diary analysis and its per-trade extractions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "diary_date": {
                "type": "string",
                "description": "YYYY-MM-DD date of the diary entry.",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
            },
            "overall_summary": {
                "type": "string",
                "description": "2-3 sentence summary of the day and the trader's mindset.",
            },
            "patterns_identified": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recurring patterns seen in this entry.",
            },
            "improvement_areas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What to work on next.",
            },
            "trade_analyses": {
                "type": "array",
                "description": "One item per trade the diary mentions, matched or not.",
                "items": {
                    "type": "object",
                    "properties": {
                        "trade_group": {
                            "type": ["string", "null"],
                            "description": "trade_group key from the context list, or null if unmatched.",
                        },
                        "match_confidence": {
                            "type": "string",
                            "enum": list(CONFIDENCE_LEVELS),
                        },
                        "match_notes": {
                            "type": "string",
                            "description": "Brief explanation of the confidence level.",
                        },
                        "ticker": {"type": "string"},
                        "diary_time": {
                            "type": ["string", "null"],
                            "description": "Time of day the diary says the trade happened, "
                                           "HH:MM or HH:MM:SS, or null if it says none.",
                        },
                        "entry_price_diary": {"type": ["number", "null"]},
                        "target_price": {"type": ["number", "null"]},
                        "strategy": {"type": ["string", "null"]},
                        "stop_loss": {"type": ["number", "null"]},
                        "risk_per_trade": {"type": ["number", "null"]},
                        "risk_reward": {"type": ["number", "null"]},
                        "r_multiple": {"type": ["number", "null"]},
                        "entry_reason": {"type": ["string", "null"]},
                        "exit_reason": {"type": ["string", "null"]},
                        "mistakes": {"type": ["string", "null"]},
                        "emotional_state": {
                            # No `enum` here: a JSON Schema `enum` cannot express
                            # "one of these words or null", and the prompt already
                            # lists the vocabulary. A hard enum would fail the
                            # whole upload over a null.
                            "type": ["string", "null"],
                            "description": "calm | anxious | overconfident | disciplined | "
                                           "frustrated | revenge, or null if not mentioned.",
                        },
                        "idea_source": {"type": ["string", "null"]},
                        "notes": {"type": ["string", "null"]},
                        "tags": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                                "required": ["type", "value"],
                            },
                        },
                        "ai_feedback": {"type": "string"},
                    },
                    "required": ["ticker", "match_confidence", "trade_group"],
                },
            },
        },
        "required": ["diary_date", "trade_analyses"],
    },
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _number(value):
    """Coerce a diary price to a float, or None when it is not usable.

    Prices arrive from a vision model and from typed notes: "1,234.50", "about
    107", "$107.67" and plain nulls all show up. Anything unparseable must be
    None rather than a wrong number, because a wrong number silently downgrades
    a correct match.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value)
        if cleaned in ("", "-", ".", "-."):
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _seconds_of_day(time_text) -> float | None:
    """'09:46:16' -> 34576.0. None for anything that is not a clock time."""
    if not time_text or not isinstance(time_text, str):
        return None
    parts = time_text.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        s = float(parts[2]) if len(parts) > 2 else 0.0
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return None
    return h * 3600 + m * 60 + s


# ── confidence, computed rather than believed ─────────────────────────────────

def match_confidence(candidate: dict, claim: dict) -> dict:
    """Judge one diary line against one candidate trade.

    `candidate` comes from build_trades_context (trade_group, ticker, avg_entry,
    first_entry_time, ...); `claim` is the model's own label, notes and any
    price/time it read off the page.

    The rules are the ones already written into the diary prompt — a diary price
    within $0.50 of the average entry, a diary time within 15 minutes of the
    first fill — enforced here so the label depends on the trade list rather
    than on the model's opinion of it.
    """
    # The model's own words are kept either way: a disagreement between its
    # verdict and the evidence is the thing the reviewer needs to see.
    model_conf = claim.get("match_confidence")
    if model_conf not in _LEVEL_INDEX:
        model_conf = "ambiguous" if claim.get("trade_group") else "unmatched"
    base = {"model_match_confidence": model_conf,
            "model_match_notes": claim.get("match_notes")}

    def verdict(level, why):
        return {**base, "match_confidence": level, "match_notes": why}

    # No key, or a key that is not one of this day's trades: there is nothing to
    # vouch for, whatever ticker the diary named.
    if not claim.get("trade_group") or candidate is None:
        return verdict("unmatched", "no trade in this day's list matches it")

    ticker_ok = str(candidate.get("ticker") or "").upper() == str(claim.get("ticker") or "").upper()
    if not ticker_ok:
        return verdict("unmatched", "the trade it names is not one of this day's trades")

    entry = _number(candidate.get("avg_entry"))
    diary_entry = _number(claim.get("entry_price_diary"))
    if entry is not None and diary_entry is not None and entry != 0:
        delta = abs(diary_entry - entry)
        if delta <= 0.50:
            return verdict("high",
                           f"diary entry {diary_entry:g} is within $0.50 of the trade's {entry:g}")

    diary_time = _seconds_of_day(claim.get("diary_time"))
    trade_time = _seconds_of_day(candidate.get("first_entry_time"))
    if diary_time is not None and trade_time is not None:
        gap = abs(diary_time - trade_time) / 60.0
        if gap <= 15:
            return verdict("medium", f"diary time is {gap:.0f} min from the first fill")

    # A key the model chose is either the only trade for that ticker that day,
    # or a guess between several. `candidates` counts them for us.
    same_ticker = claim.get("candidates") or 1
    if same_ticker == 1:
        return verdict("low", "only trade for that ticker this day")
    return verdict("ambiguous",
                   f"{same_ticker} trades for that ticker today and no price or time "
                   "to tell them apart")


def _candidate_by_group(context: list[dict]) -> dict:
    return {c.get("trade_group"): c for c in context or []}


def _claim_key(claim: dict) -> str:
    return str(claim.get("trade_group") or claim.get("ticker") or "").upper()


def _validate(claim: dict, context: list[dict], seen: set) -> str | None:
    """Return the reason to drop this trade_analysis, or None to keep it."""
    if not isinstance(claim, dict):
        return "not an object"
    if not str(claim.get("ticker") or "").strip():
        return "no ticker"
    key = _claim_key(claim)
    if not key:
        return "no ticker or trade_group"
    if key in seen:
        return f"duplicate for {key}"
    seen.add(key)
    if claim.get("trade_group") is not None:
        groups = {c.get("trade_group") for c in context}
        if claim["trade_group"] not in groups:
            return f"trade_group {claim['trade_group']!r} is not a trade from this day"
    return None


def analyse_confidence(analysis: dict, context: list[dict]) -> dict:
    """Annotate every trade_analysis with a confidence Python can defend.

    The model's own label and reason survive as `model_match_confidence` and
    `model_match_notes`, so a disagreement is visible instead of silently
    overwritten. Entries the model could not place keep `trade_group: null`,
    which is how the review queue finds them.
    """
    by_group = _candidate_by_group(context)
    by_ticker: dict[str, int] = {}
    for c in context:
        tick = str(c.get("ticker") or "").upper()
        by_ticker[tick] = by_ticker.get(tick, 0) + 1

    entries = analysis.get("trade_analyses")
    if not isinstance(entries, list):
        analysis["trade_analyses"] = []
        entries = []

    # A diary line that only names a ticker gets pointed at that day's trade
    # before judging it, so "low confidence" and "pointing nowhere" stay
    # different things.
    for claim in entries:
        if not isinstance(claim, dict):
            continue
        if not claim.get("trade_group"):
            tick = str(claim.get("ticker") or "").upper()
            if by_ticker.get(tick) == 1:
                for c in context:
                    if str(c.get("ticker") or "").upper() == tick:
                        claim["trade_group"] = c.get("trade_group")
                        break

    kept, seen = [], set()
    rejected = []
    for claim in entries:
        reason = _validate(claim, context, seen)
        if reason:
            rejected.append(f"{_claim_key(claim) or 'unknown'}: {reason}")
            continue
        kept.append(claim)

    for claim in kept:
        if claim.get("trade_group") is None:
            claim.update(match_confidence(None, claim))
            continue
        candidate = by_group.get(claim["trade_group"])
        if candidate is None:
            # Should be impossible after _validate; keep it honest anyway.
            claim.update(match_confidence(None, claim))
            continue
        claim["candidates"] = by_ticker.get(str(candidate.get("ticker") or "").upper(), 1)
        claim.update(match_confidence(candidate, claim))

    analysis["trade_analyses"] = kept
    analysis.setdefault("diary_date", "")
    analysis.setdefault("overall_summary", "")
    analysis.setdefault("patterns_identified", [])
    analysis.setdefault("improvement_areas", [])
    if rejected:
        analysis["dropped"] = rejected
    return analysis


def queued_for_review(analysis: dict) -> list[dict]:
    """Trades the user has not confirmed, weakest first.

    High-confidence matches are ordered last on purpose: they are the ones a
    skim is most likely to be right about, so the list is worth reading top to
    bottom rather than skipping.
    """
    entries = analysis.get("trade_analyses") or []
    if not isinstance(entries, list):
        return []
    open_entries = [t for t in entries
                    if isinstance(t, dict) and t.get("match_confidence") != "manual"]

    def rank(claim):
        name = claim.get("match_confidence")
        return _LEVEL_INDEX.get(name, 0)

    return sorted(open_entries, key=rank)
