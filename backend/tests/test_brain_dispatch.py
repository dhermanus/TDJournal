"""Brain's tool dispatch loop.

    cd backend && python -m pytest tests/test_brain_dispatch.py -q

The loop is where an agent goes wrong quietly: it can call a tool the model
invented, answer with a number no tool produced, or spin forever on a rejected
call. These tests stub `get_client` so each shape runs without touching the API.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import ai_analysis
from brain_tools import TOOL_SCHEMAS


class Block:
    """A stand-in for a content block in an assistant turn."""
    def __init__(self, kind, *, text=None, name=None, input=None, id="t1"):
        self.type = kind
        self.id = id
        self.text = text or ""
        self.name = name
        self.input = input if input is not None else {}

    def as_content(self):
        return {"type": self.type, "text": self.text,
                "name": self.name, "input": self.input, "id": self.id}


class Turn:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content
        self.content = [b if isinstance(b, Block) else Block("text", text=b)
                        for b in content]


class FakeClient:
    """Returns each scripted turn in order, recording what it was asked."""
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

        class Messages:
            def __init__(self, outer):
                self.outer = outer
            def create(self, **kwargs):
                self.outer.calls.append(kwargs)
                return self.outer.turns.pop(0) if self.outer.turns else Turn("end_turn", ["done"])
        self.messages = Messages(self)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "j.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, type TEXT,
            color TEXT, broker TEXT, created_at TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL, trade_group TEXT NOT NULL, date TEXT NOT NULL,
            ticker TEXT NOT NULL, instrument_type TEXT, side TEXT, gross_pnl REAL,
            net_pnl REAL, commissions REAL DEFAULT 0, executions TEXT DEFAULT '[]',
            setup TEXT);
        CREATE TABLE trade_analysis (id INTEGER PRIMARY KEY,
            trade_group TEXT UNIQUE, strategy TEXT, r_multiple REAL,
            emotional_state TEXT, mistakes TEXT, notes TEXT);
        """
    )
    for i, (ticker, net) in enumerate([("EURUSD", 40.0), ("EURUSD", -10.0),
                                       ("USDJPY", 5.0)], start=1):
        c.execute("INSERT INTO trades (account_id, trade_group, date, ticker,"
                  " instrument_type, side, net_pnl, gross_pnl)"
                  " VALUES (1,?,?,?,?,?,?,?)",
                  (f"g{i}", f"2026-07-0{i}", ticker, "FX", "LONG", net, net))
    c.commit()
    yield c
    c.close()


def ask(monkeypatch, conn, turns, messages=None, account_id=1):
    fake = FakeClient(turns)
    monkeypatch.setattr(ai_analysis, "get_client", lambda: fake)
    out = ai_analysis.generate_brain_response(
        messages or [{"role": "user", "content": "How am I doing?"}],
        "=== ACCOUNT PERFORMANCE ===\nTrades: 3",
        conn, account_id)
    return out, fake


def test_the_tools_are_offered_on_the_wire(monkeypatch, conn):
    _, fake = ask(monkeypatch, conn, [Turn("end_turn", ["hi"])])
    offered = fake.calls[0].get("tools")
    assert offered is not None, "tools must be sent or the model cannot ask"
    assert offered == TOOL_SCHEMAS


def test_a_tool_round_feeds_the_result_back_then_answers(monkeypatch, conn):
    turns = [
        Turn("tool_use", [Block("tool_use", name="get_performance_summary",
                               input={"ticker": "EURUSD"})]),
        Turn("end_turn", ["Three trades, net +$30."]),
    ]
    out, fake = ask(monkeypatch, conn, turns)
    assert out == "Three trades, net +$30."
    assert len(fake.calls) == 2, "one tool round, then the answer"

    history = fake.calls[1]["messages"]
    # the tool_result carries the real figure, not a paraphrase
    tool_results = [c for m in history if isinstance(m.get("content"), list)
                    for c in m["content"]
                    if isinstance(c, dict) and c.get("type") == "tool_result"]
    assert len(tool_results) == 1
    payload = json.loads(tool_results[0]["content"])
    assert payload["trades"] == 2, payload      # EURUSD only
    assert payload["net_pnl"] == pytest.approx(30.0)
    assert tool_results[0]["is_error"] is False


def test_an_unknown_tool_is_reported_as_an_error_result(monkeypatch, conn):
    turns = [
        Turn("tool_use", [Block("tool_use", name="delete_everything", input={})]),
        Turn("end_turn", ["I cannot do that."]),
    ]
    out, fake = ask(monkeypatch, conn, turns)
    assert out == "I cannot do that."
    result = [c for m in fake.calls[1]["messages"] if isinstance(m.get("content"), list)
              for c in m["content"]
              if isinstance(c, dict) and c.get("type") == "tool_result"][0]
    assert result["is_error"] is True
    assert "unknown tool" in json.loads(result["content"])["error"]
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3


def test_a_rejected_call_does_not_end_the_conversation(monkeypatch, conn):
    turns = [
        Turn("tool_use", [Block("tool_use", name="get_performance_summary",
                               input={"date_from": "nope"})]),
        Turn("tool_use", [Block("tool_use", name="get_performance_summary", input={})]),
        Turn("end_turn", ["Try YYYY-MM-DD dates."]),
    ]
    out, fake = ask(monkeypatch, conn, turns)
    assert len(fake.calls) == 3
    first_result = [c for m in fake.calls[1]["messages"]
                    if isinstance(m.get("content"), list)
                    for c in m["content"]
                    if isinstance(c, dict) and c.get("type") == "tool_result"][0]
    assert first_result["is_error"] is True
    assert "YYYY-MM-DD" in json.loads(first_result["content"])["error"]


def test_the_loop_stops_spinning(monkeypatch, conn):
    """A model that only ever asks for tools must not run forever."""
    endless = [Turn("tool_use", [Block("tool_use", name="get_performance_summary",
                                       input={})])
               for _ in range(ai_analysis.BRAIN_MAX_TOOL_ROUNDS + 6)]
    out, fake = ask(monkeypatch, conn, endless)
    assert len(fake.calls) <= ai_analysis.BRAIN_MAX_TOOL_ROUNDS + 1, "loop must be bounded"
    assert out, "there must be a final answer even when the model will not stop"


def test_without_a_connection_the_loop_never_sends_tools(monkeypatch):
    _, fake = ask(monkeypatch, None, [Turn("end_turn", ["no db today"])])
    assert fake.calls[0].get("tools") is None


def test_no_conn_context_is_still_in_the_first_user_message(monkeypatch, conn):
    _, fake = ask(monkeypatch, conn, [Turn("end_turn", ["ok"])],
                  messages=[{"role": "user", "content": "How many trades?"}])
    first = fake.calls[0]["messages"][0]["content"]
    assert "[Trading data]" in first and "Trades: 3" in first
    assert "[Question]" in first and "How many trades?" in first


def test_only_the_first_user_message_carries_the_context(monkeypatch, conn):
    _, fake = ask(monkeypatch, conn, [Turn("end_turn", ["ok"])],
                  messages=[{"role": "user", "content": "First?"},
                            {"role": "assistant", "content": "Answer 1."},
                            {"role": "user", "content": "Second?"}])
    contents = [m["content"] for m in fake.calls[0]["messages"] if m["role"] == "user"]
    assert contents.count(contents[0]) == contents.count(
        "[Trading data]\n=== ACCOUNT PERFORMANCE ===\nTrades: 3\n\n[Question]\nFirst?")
    assert sum("[Trading data]" in c for c in contents) == 1
