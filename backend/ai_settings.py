"""AI settings: which model is used, and which AI features are allowed to send.

Stored in the existing `settings` table under `ai_config` as JSON:

    {"model": "claude-opus-5", "features": {"brain": true, ...}}

Two things this exists to guarantee:

* **A toggle means "sends nothing".** The gate is checked in the endpoint
  before any AI context is built, not as a UI flag — so a disabled feature
  cannot build a prompt and then quietly send it.
* **Privacy is visible where the data leaves.** `feature_info` describes, per
  feature, what actually crosses the network, so Settings can state it next to
  the switch instead of in a policy page nobody opens.

Features default to enabled so installing this file changes nobody's current
behaviour; the notice is what makes the sending visible for the first time.
"""
import json
import re
import sqlite3

SETTINGS_KEY = "ai_config"

DEFAULT_MODEL = "claude-opus-5"

# What the app has actually been verified to run against the configured
# endpoint. A model outside this list is still accepted if it is shaped like a
# model id, so a new one can be used without a code change.
KNOWN_MODELS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-opus-4-5",
    "claude-3-5-haiku-latest",
)

_MODEL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$")

# label  — what the switch is called.
# sends  — the exact data that crosses the network when it is on. This is the
#          disclosure the settings page prints beside the toggle.
FEATURES = {
    "brain": {
        "label": "Brain chat",
        "sends": "Your question, the account totals, your last 8 trades, and any trades, "
                 "breakdowns or diary summaries the chat queries.",
    },
    "diary": {
        "label": "Diary analysis",
        "sends": "The note, screenshot or text you upload, and that date's trades so it can "
                 "be matched to them.",
    },
    "daily_summary": {
        "label": "Day Review report",
        "sends": "That day's trades and your notes on them.",
    },
    "insights": {
        "label": "Insights",
        "sends": "Your aggregated performance metrics as JSON — counts, P&L, win rates, "
                 "streaks and breakdowns.",
    },
    "weekly": {
        "label": "Weekly summary",
        "sends": "The week's trades grouped by day, with P&L, R multiples, emotions and "
                 "mistakes.",
    },
}

# Process-global model, set once at startup (and again when settings change)
# because the call sites read it at request time without a database handle.
_model = DEFAULT_MODEL


def get_model() -> str:
    return _model


def set_model(name: str) -> None:
    global _model
    _model = validate_model(name)


def validate_model(name) -> str:
    if not isinstance(name, str) or not _MODEL_ID.match(name.strip()):
        raise ValueError(
            "That is not a model name. Use an id like claude-opus-5 or claude-sonnet-5.")
    return name.strip()


def default_config() -> dict:
    return {"model": DEFAULT_MODEL,
            "features": {name: True for name in FEATURES}}


def load_config(conn: sqlite3.Connection) -> dict:
    """Current config with every known feature present, whatever is stored."""
    cfg = default_config()
    row = conn.execute(
        "SELECT value FROM settings WHERE account_id = 0 AND key = ?",
        (SETTINGS_KEY,)).fetchone()
    if not row:
        return cfg
    try:
        stored = json.loads(row["value"])
    except (ValueError, TypeError):
        return cfg
    if isinstance(stored.get("model"), str):
        try:
            cfg["model"] = validate_model(stored["model"])
        except ValueError:
            pass  # an unusable stored model falls back to the default
    features = stored.get("features")
    if isinstance(features, dict):
        for name in FEATURES:
            if name in features:
                cfg["features"][name] = bool(features[name])
    return cfg


def save_config(conn: sqlite3.Connection, model=None, features=None) -> dict:
    """Merge a patch into the stored config and return the result."""
    cfg = load_config(conn)
    if model is not None:
        cfg["model"] = validate_model(model)
    if features is not None:
        if not isinstance(features, dict):
            raise ValueError("features must be an object of booleans")
        for name, value in features.items():
            if name not in FEATURES:
                raise ValueError(f"unknown AI feature: {name}")
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
            cfg["features"][name] = value
    conn.execute(
        "INSERT INTO settings (account_id, key, value) VALUES (0, ?, ?) "
        "ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value",
        (SETTINGS_KEY, json.dumps(cfg)))
    conn.commit()
    set_model(cfg["model"])
    return cfg


def is_enabled(conn: sqlite3.Connection, feature: str) -> bool:
    """True when this feature may build a prompt at all. Unknown names are off."""
    if feature not in FEATURES:
        return False
    return bool(load_config(conn)["features"].get(feature, True))
