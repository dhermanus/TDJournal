# Dormant defects — proven, unreachable on current data

Each of these was found by running the code, not by reading it, and each is
reproduced against a live probe rather than asserted. None can fire on the
journal as it stands: **every one of the 1,346 trades is `source='mt5'` and
`instrument_type='FX'`.** They bite the day a US-broker statement is imported.

Fix them when that happens, not before — writing tests for brokers this journal
does not use costs more than it protects.

## 1. Cross-import duplicate drop (broker CSV only)

`execution_fingerprint` (`csv_parser.py`, `format date|time|ticker|action|qty|price`)
has no multiplicity, and `get_existing_fingerprints` returns a `set`. Two genuinely
identical fills arriving in a *later* file collapse into one; `existing_fps` is
checked with `if fp in existing_fps`.

Probe: import fills A (buy 80), then B (sell 120 on the next statement) →
`fills=3 qty_bot=80 qty_sold=120 net_pnl=0.0`, instead of the correct
`fills=4 qty_bot=120 qty_sold=120 net_pnl=597.0`. A closed round trip reports 0.

Intra-file duplicates are already handled correctly (the comment above
`build_trades_from_executions` explains why Thinkorswim emits them) — only the
cross-import path is wrong.

Not the MT5 path: MT5 dedups by `deal_ticket`, which is strictly more correct
here (Phase 4 found 149 colliding fingerprints / 201 extra rows in the real export).

## 2. Trade History futures priced as stock (50× too small)

`parse_trade_history_section` only assigns a multiplier inside
`if type_str in ('CALL', 'PUT')`; the `else` branch hardcodes
`instrument_type='STOCK'` and `multiplier=1`, with no FUTURE case.

End-to-end probe: a `/ES` row imports as `9/15/26_/ES_STOCK_1 gross=10.0` where
the correct figure is `500.0` — **50× understated**, on a row that looks entirely
ordinary in the UI. Same class of bug as the lot-matching function in
[PR #1](https://github.com/simonro/Trading-Journal-AI/pull/1).

## 3. Latent 100× on options — one deleted line from firing

`_rebuild_fill_from_db_exec` multiplies the amount by 100 for `OPTION`, then
`overlapping_db_fills` applies `_point_value()`, which *also* returns 100 for
`OPTION`. Safe today only because `if instr == 'OPTION': continue` skips options
before reaching the multiply. Removing that guard double-counts every option by 100×.

Worth a comment at the guard, or a test that pins the guard itself.

## 4. Attachments are not migrated by a regrouping import

`_replace_regrouped_trades` (`main.py`) migrates `trade_analysis` and `trade_tags`
to the new key — but **not `trade_attachments`**. `attachments.py` documents the
exact risk in its module docstring ("a *regrouping* import deletes and recreates
trades, which would strand anything keyed on the integer id") and then defends only
the other two tables.

Unreachable twice over today: the MT5 path emits `"replaces": []` so the function
returns immediately, and MT5 keys (`MT5_{symbol}_{type}_{position_id}`) contain no
date, so they never re-key on re-import.

Fix is roughly one `UPDATE ... WHERE trade_group IN (...)` alongside the existing
tag migration. Do it in the same change that touches `_replace_regrouped_trades`.

## Deliberately not tracked

**Corporate actions** — splits, option assignment, expiry, expiries, rolls,
multi-leg spreads: no handling exists anywhere, and none applies to FX. Recorded
here only so the absence is known to be a choice rather than an oversight.
