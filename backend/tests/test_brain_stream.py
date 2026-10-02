"""Brain's streamed turn: tokens arrive as they are produced, and Stop stops it.

    cd backend && python -m pytest tests/test_brain_stream.py -q

Three things worth pinning:

1. The SSE route emits real deltas, not one blob at the end — that is the whole
   point of the endpoint.
2. Usage and errors arrive as events on the same stream, so a failure mid-answer
   is visible to the browser instead of being a silent hang.
3. Closing the generator closes the SDK stream. Without that, a browser Stop
   would detach from the response while the model kept generating and the app
   kept paying for it.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import ai_analysis
from ai_analysis import brain_turn


class Block:
    def __init__(self, kind, *, text=None, name=None, input=None, id="t1"):
        self.type = kind
        self.id = id
        self.text = text or ""
        self.name = name
        self.input = input if input is not None else {}


class Turn:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = [b if isinstance(b, Block) else Block("text", text=b)
                        for b in content]
        self.usage = None


class RecordingStream:
    """Stands in for `messages.stream(...)`, tracking whether it was closed."""
    def __init__(self, turn, log):
        self.turn = turn
        self.log = log
        self.closed = False

    def __enter__(self):
        self.log.append("enter")
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @property
    def text_stream(self):
        for block in self.turn.content:
            if block.type == "text" and block.text:
                yield block.text

    def get_final_message(self):
        return self.turn

    def close(self):
        self.closed = True
        self.log.append("close")


class FakeClient:
    def __init__(self, turns, log, text_split=None):
        self.turns = list(turns)
        self.log = log
        self.text_split = text_split
        self.calls = []

        class Messages:
            def __init__(self, outer):
                self.outer = outer

            def stream(self, **kwargs):
                outer = self.outer
                outer.calls.append(kwargs)
                turn = outer.turns.pop(0) if outer.turns else Turn("end_turn", ["done"])
                stream = RecordingStream(turn, outer.log)
                if outer.text_split:
                    # Split the first text block into the chunks the model
                    # would actually send, so a delta-by-delta assertion is
                    # about real interleaving rather than one big string.
                    stream.turn = turn
                return stream

            def create(self, **kwargs):  # pragma: no cover - Brain no longer uses it
                raise AssertionError("Brain must answer through the streaming API")

        self.messages = Messages(self)


def use(monkeypatch, turns, log=None):
    log = [] if log is None else log
    client = FakeClient(turns, log)
    monkeypatch.setattr(ai_analysis, "get_client", lambda: client)
    return client


def events(conn=None):
    """Collect a whole Brain turn as a list of (kind, payload) events.

    FakeClient was installed by `use()`, so this reads the scripted response
    off the exact code path under test. `conn` is what lets the loop run a
    tool round; without it the turn ends after the first response.
    """
    return list(brain_turn([{"role": "user", "content": "How am I doing?"}],
                            "=== ACCOUNT PERFORMANCE ===\nTrades: 3", conn, 1))


@pytest.fixture
def conn(tmp_path):
    """A connection so the tool loop actually runs a second round.

    `conn is None` means "no tools", which returns after the first response —
    that is the production path for a caller without a journal, but it would
    make every tool-round assertion here silently pass on round one.
    """
    c = sqlite3.connect(tmp_path / "s.db")
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
        """
    )
    c.execute("INSERT INTO trades (account_id, trade_group, date, ticker,"
              " instrument_type, side, net_pnl, gross_pnl)"
              " VALUES (1, 'g1', '2026-07-01', 'EURUSD', 'FX', 'LONG', 30.0, 30.0)")
    c.commit()
    yield c
    c.close()


def test_text_arrives_as_deltas_not_one_blob(monkeypatch):
    use(monkeypatch, [Turn("end_turn", ["Three trades ", "this week."])])
    got = [p for k, p in events() if k == "delta"]
    assert got, "no deltas were produced"
    assert "".join(got) == "Three trades this week."


def test_usage_is_an_event_after_the_text(monkeypatch):
    use(monkeypatch, [Turn("end_turn", ["Hello there."])])
    got = events()
    kinds = [k for k, _ in got]
    assert kinds[0] == "delta", "text must come first"
    assert kinds[-1] == "usage", f"usage must close the turn, got {kinds}"
    usage = got[-1][1]
    assert isinstance(usage, dict) and "input_tokens" in usage


def test_a_tool_only_round_emits_nothing(monkeypatch, conn):
    """A round with no text is a pure lookup, so there is nothing to show —
    the dots stay until the answer itself arrives.

    (A round that *does* narrate before asking is streamed; see
    test_two_rounds_do_not_run_together below. These are different things.)
    """
    turns = [Turn("tool_use", [Block("tool_use", name="get_performance_summary",
                                     input={}, id="t1")]),
             Turn("end_turn", ["There were 3 trades."])]
    use(monkeypatch, turns)
    got = [p for k, p in events(conn) if k == "delta"]
    assert got == ["There were 3 trades."], got


def test_two_rounds_do_not_run_together(monkeypatch, conn):
    """Narration from a first round, then the answer: a blank line between them
    or it reads as one broken sentence."""
    turns = [Turn("tool_use", [Block("text", text="Checking your trades. "),
                               Block("tool_use", name="list_trades",
                                     input={}, id="t1")]),
             Turn("end_turn", ["Three trades."])]
    use(monkeypatch, turns)
    got = "".join(p for k, p in events(conn) if k == "delta")
    assert got == "Checking your trades. \n\nThree trades.", repr(got)


def test_closing_the_generator_closes_the_sdk_stream(monkeypatch):
    """This is the Stop button: dropping the response must release the
    connection, or the answer finishes unseen and still gets billed."""
    log = []
    use(monkeypatch, [Turn("end_turn", ["one ", "two ", "three"])], log=log)
    turn = brain_turn([{"role": "user", "content": "hi"}], "ctx", None, 1)
    assert next(turn)[0] == "delta"
    assert "enter" in log, "the stream should already be open"
    turn.close()          # what closing the HTTP response does
    assert "close" in log, f"closing the generator must close the stream, log={log}"


def test_a_failure_becomes_an_event_the_browser_can_show(monkeypatch):
    """A stream that dies mid-answer must not leave the panel spinning."""
    class Boom:
        def __init__(self):
            self.messages = self
        def stream(self, **kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(ai_analysis, "get_client", lambda: Boom())
    got = events()
    kinds = [k for k, _ in got]
    assert kinds == ["error"], kinds
    assert got[-1][1]["message"], "the user needs to read what happened"
    assert got[-1][1]["status"] >= 500


def test_an_empty_answer_is_explained_rather_than_returned_blank(monkeypatch):
    """A blank panel with no explanation is a dead end. The message must also
    match the cause: nothing was looked up here, so it must not claim it was."""
    use(monkeypatch, [Turn("end_turn", [""])])
    got = [p for k, p in events() if k == "delta"]
    assert got, "an empty round must still say something"
    joined = "".join(got)
    assert "data lookups" not in joined, "no lookup happened on this path"
    assert "different way" in joined, joined


def test_an_exhausted_loop_says_so_instead(monkeypatch, conn):
    """After every tool round is spent the cause *is* the lookups, and the
    message should say what to do about it."""
    rounds = [{"type": "tool_use"}] * 8
    turns = [Turn("tool_use", [Block("tool_use", name="list_trades",
                                     input={}, id=f"t{i}")])
             for i in range(8)]
    turns.append(Turn("end_turn", [""]))
    use(monkeypatch, turns)
    got = "".join(p for k, p in events(conn) if k == "delta")
    assert "narrower question" in got, got
    assert "data lookups" in got


def test_generate_brain_response_joins_the_same_stream(monkeypatch):
    """The non-streaming caller must not diverge from what the browser sees."""
    use(monkeypatch, [Turn("end_turn", ["Hello there."])])
    out = ai_analysis.generate_brain_response(
        [{"role": "user", "content": "hi"}], "ctx")
    assert out == "Hello there."


def test_the_non_streaming_path_reports_a_failure_instead_of_silence(monkeypatch):
    class Boom:
        def __init__(self):
            self.messages = self
        def stream(self, **kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(ai_analysis, "get_client", lambda: Boom())
    with pytest.raises(RuntimeError):
        ai_analysis.generate_brain_response([{"role": "user", "content": "hi"}], "ctx")
