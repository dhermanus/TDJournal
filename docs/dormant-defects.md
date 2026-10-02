# Dormant defects — proven, unreachable on current data

Each of these was found by running the code, not by reading it, and each is
reproduced against a live probe rather than asserted. None can fire on the
journal as it stands: **every one of the 1,346 trades is `source='mt5'` and
`instrument_type='FX'`.**

**Two of the four were fixed on 2026-10-02**, when item 3 of the enhancement list
("import correctness hardening") was worked. What is left is listed below; the
probes for the fixed ones now live as tests in
`backend/tests/test_import_invariants.py`, which is the better home for them —
a defect with a test that passes is no longer a note, it is a regression guard.

## Fixed

### 1. Cross-import duplicate drop — fixed

`execution_fingerprint` has no multiplicity and the lookup was a `set`, so a set
match meant "drop every later occurrence" rather than "drop the number the
database already holds". A statement carrying an identical row twice while the
database held one would lose both copies of the second one, and the position
would never balance (probe: buy 80, then a file with that row twice plus a sell
of 120 → bot 80 / sold 120 instead of 160 / 120).

Now `get_existing_fingerprints` returns a `collections.Counter` and the loop
consumes one claim per stored occurrence, in two passes. The second pass is
deliberate: going one row at a time would match the second identical row of the
*incoming* file against the first row of the *same* file, which is the intra-file
dedup Thinkorswim's split orders forbid.

Not the MT5 path either way: MT5 dedups by `deal_ticket`, and it never calls
`build_trades_from_executions`.

### 2. Trade History futures priced as stock — fixed

`parse_trade_history_section` assigned a multiplier only inside
`if type_str in ('CALL', 'PUT')` and hard-coded `STOCK` / `1` otherwise. A `/ES`
row 5 index points wide booked `gross 5.0` where the E-mini point value says
`250.0` — **50× understated**, on a row that looks ordinary in the UI.

There is now a futures branch keyed on the TYPE column (`FUT` and friends) or a
`/`-prefixed symbol, resolving through `_point_value` — the same map the Futures
Statements section uses. An unknown root is skipped rather than priced at 1,
because understating by an unknown factor is worse than declining the row.

Same class of bug as the lot-matching function in
[PR #1](https://github.com/simonro/Trading-Journal-AI/pull/1).

### New: futures rows in Cash Balance vanished silently — fixed

Not in this doc before. `parse_cash_description` tried option, then stock, never
futures, so `BOT +1 /ES @5000.00` returned `None` and hit the section's
`continue`. The row disappeared with no counter and no `errors` entry, and both
legs of a round trip produced an import of nothing that still reported success.
The Cash Balance AMOUNT column is already multiplied, so the fix only had to
classify the row — but the classification was missing entirely.

**Related, still open:** rows dropped while parsing are reported nowhere, by any
parser. `errors` in the import response collects only database insert failures.
This was how the futures row above stayed invisible for so long. A count of
dropped rows in the response is the fix; it was scoped out of this change.

## Still open

### 3. Latent 100× on options — one deleted line from firing

`_rebuild_fill_from_db_exec` multiplies the amount by 100 for `OPTION`, then
`overlapping_db_fills` applies `_point_value()`, which *also* returns 100 for
`OPTION`. Safe today only because `if instr == 'OPTION': continue` skips options
before reaching the multiply. Removing that guard double-counts every option by
100×.

Worth a comment at the guard, or a test that pins the guard itself.

### 4. Attachments are not migrated by a regrouping import

`_replace_regrouped_trades` (`main.py`) migrates `trade_analysis` and
`trade_tags` to the new key — but **not `trade_attachments`**. `attachments.py`
documents the exact risk in its module docstring ("a *regrouping* import deletes
and recreates trades, which would strand anything keyed on the integer id") and
then defends only the other two tables.

Unreachable twice over today: the MT5 path emits `"replaces": []` so the function
returns immediately, and MT5 keys (`MT5_{symbol}_{type}_{position_id}`) contain no
date, so they never re-key on re-import.

Fix is roughly one `UPDATE ... WHERE trade_group IN (...)` alongside the existing
tag migration. Do it in the same change that touches `_replace_regrouped_trades`.

## Deliberately not tracked

**Corporate actions** — splits, option assignment, expiry, rolls, multi-leg
spreads: no handling exists anywhere, and none applies to FX. Recorded here only
so the absence is known to be a choice rather than an oversight.
