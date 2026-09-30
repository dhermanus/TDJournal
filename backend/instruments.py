"""Canonical instrument types and how an MT5 symbol becomes one.

The `trades.instrument_type` CHECK constraint used to allow only STOCK, OPTION
and FUTURE, which covers US broker statements but nothing an MT5 account
trades. Every type the journal understands lives here so the schema, the
parser and the API cannot drift apart.
"""
from __future__ import annotations

# Order matters only for display; the CHECK constraint is built from this tuple.
INSTRUMENT_TYPES = ("STOCK", "OPTION", "FUTURE", "FX", "METAL", "INDEX")

# Used to build the table definition: 'STOCK','OPTION','FUTURE','FX','METAL','INDEX'
CHECK_EXPR = ",".join(f"'{t}'" for t in INSTRUMENT_TYPES)

# Labels the Add Trade and filter menus show. Every type needs an entry so a
# newly-imported type never renders as a bare code.
INSTRUMENT_LABELS = {
    "STOCK": "Stock",
    "OPTION": "Option",
    "FUTURE": "Future",
    "FX": "Forex",
    "METAL": "Metal",
    "INDEX": "Index",
}

# Alternate spellings a statement may use. Checked before the canonical list.
ALIASES = {
    "STK": "STOCK", "EQUITY": "STOCK", "EQ": "STOCK", "SHARE": "STOCK",
    "SHARES": "STOCK", "ETF": "STOCK", "SHARES CFD": "STOCK",
    "OPT": "OPTION", "OPTIONS": "OPTION",
    "FUT": "FUTURE", "FUTURES": "FUTURE", "FUTURE CONTRACT": "FUTURE",
    "FX": "FX", "FOREX": "FX", "CURRENCY": "FX", "SPOT": "FX",
    "METALS": "METAL", "METAL": "METAL", "GOLD": "METAL", "SILVER": "METAL",
    "IDX": "INDEX", "INDICES": "INDEX", "INDEX": "INDEX",
    "CFD": "INDEX", "CFDS": "INDEX",
}

# MT5 `symbol_path` first segment -> instrument type. The exporter writes this
# column, so classification uses the broker's own grouping rather than a guess
# made from the ticker spelling.
PATH_CLASSES = {
    "FOREX": "FX",
    "MAJORS": "FX",
    "MINORS": "FX",
    "EXOTICS": "FX",
    "METALS": "METAL",
    "GOLD": "METAL",
    "SILVER": "METAL",
    "INDICES": "INDEX",
    "INDEX": "INDEX",
    "STOCKS": "STOCK",
    "SHARES": "STOCK",
    "EQUITIES": "STOCK",
    "FUTURES": "FUTURE",
    "CFD_FUTURES": "FUTURE",
}


def normalize(value: str | None) -> str | None:
    """Map a statement's wording onto a canonical type, or None if unusable."""
    if value is None:
        return None
    raw = str(value).strip().upper()
    if not raw:
        return None
    if raw in INSTRUMENT_TYPES:
        return raw
    return ALIASES.get(raw)


def classify_mt5(symbol: str, symbol_path: str | None = None) -> str:
    """Classify an MT5 deal row.

    The broker's `symbol_path` wins: it names the group the symbol sits in, so
    XAUUSD under Metals is METAL and US500 under Indices is INDEX even though
    neither looks like a ticker you could recognise blind. Fallbacks cover
    exports that omitted the path column.
    """
    path = (symbol_path or "").strip()
    if path:
        head = path.replace("/", "\\").split("\\")[0].strip().upper()
        hit = PATH_CLASSES.get(head)
        if hit:
            return hit
        # Path segments are sometimes the group name deeper in, e.g.
        # "CFD\Indices\US500" — scan the rest before giving up.
        for seg in (s.strip().upper() for s in path.replace("/", "\\").split("\\")[1:]):
            hit = PATH_CLASSES.get(seg)
            if hit:
                return hit

    sym = (symbol or "").strip().upper()
    if not sym:
        return "STOCK"
    if sym.startswith("/"):          # TDJournal's own futures convention, /ES
        return "FUTURE"
    if "." in sym:
        suffix = sym.rsplit(".", 1)[-1]
        if suffix in ("M", "MICRO"):
            return "FX"
    if sym.endswith(("USD", "EUR", "GBP", "JPY", "AUD", "CHF", "NZD", "CAD")) and len(sym) <= 7:
        # EURUSD, USDJPY, GBPJPY… six-letter pairs, and not XAUUSD/XAGUSD which
        # are metals — those are caught by path first, but guard for their
        # absence too.
        if sym[:3] in ("XAU", "XAG", "XPT", "XPD"):
            return "METAL"
        return "FX"
    if sym[:3] in ("XAU", "XAG", "XPT", "XPD"):
        return "METAL"
    return "STOCK"


def label(instrument_type: str | None) -> str:
    """Display name for a type, falling back to the raw code if unknown."""
    return INSTRUMENT_LABELS.get((instrument_type or "").upper(), instrument_type or "")


# Units of value per one unit of quantity, used when P&L is derived from prices.
#
# These are per-lot defaults good enough for a hand-entered trade: EURUSD moving
# 25 pips on 1 lot lands on 250.00, matching what MT5 reports. An MT5 export
# carries the symbol's real `contract_size` and passes it explicitly, so this
# table is never the source of truth for imported fills — it only stops a
# manually-typed FX trade from being priced like a stock share.
UNITS_PER_LOT = {
    "STOCK": 1,        # a share is a share
    "OPTION": 100,     # standard equity-option multiplier
    "FUTURE": None,    # depends on the contract; looked up per ticker
    "FX": 100_000,     # the usual 1-lot size for a major pair
    "METAL": 100,      # XAUUSD: 100 troy ounces per lot
    "INDEX": 1,        # CFD quoted in index points
}


def units_per_lot(instrument_type: str | None, ticker: str | None = None,
                  futures_multipliers: dict | None = None) -> float | None:
    """Value of one unit of quantity, or None when it cannot be known.

    None is deliberate: for a futures contract with no known point value we
    refuse to guess, because understating /ES by 50x is worse than declining.
    """
    instr = normalize(instrument_type) or "STOCK"
    if instr != "FUTURE":
        value = UNITS_PER_LOT.get(instr)
        return float(value) if value is not None else None
    if not futures_multipliers:
        return None
    sym = (ticker or "").upper()
    for root in sorted(futures_multipliers, key=len, reverse=True):
        if sym.startswith(root.upper()):
            return float(futures_multipliers[root])
    return None
