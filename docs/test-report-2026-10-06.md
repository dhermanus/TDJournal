# TDJournal — comprehensive test report

**Date:** 2026-10-06 · **Scope:** local only (working tree + an isolated clone of
the journal), full write access with restore verification.

**Live journal untouched throughout:** `2268bb9148de4c260a1cf92d61938c38` before
and after every phase. All writes ran against `/tmp/ui-clone`, a byte copy of the
live DB, and every journal table ended byte-identical to its baseline.

---

## Verdict

**The app works.** 470 backend tests, 119 frontend tests, 51/51 files parse,
lint clean on every changed file, and 25 browser assertions passing across
read and write flows.

**One real defect found**, plus three dead endpoints the API keeps alive with
nothing behind them. None were introduced by the recent precision/what-if work.

---

## Test matrix

| Phase | Result |
|---|---|
| Backend `pytest` (journal md5 guard) | **470 passed** |
| Frontend unit batch | **119 passed, 0 failed** |
| Babel parse, all 51 `src` files | **51/51 OK** |
| Lint, fatal rules on 9 changed files | **clean** (unfiltered output) |
| Browser: read pass — 8 pages, nav, precision, what-if colour + caveat | **14 passed, 0 failed** |
| Browser: write pass — add trade, import/undo, fill add/remove, restore, live journal, console | **11 passed, 1 failed** |
| D1 fix: full suite after repair (was 470) | **492 passed** |
| D1 fix: browser repro (FX 0.01 through the form) | **pass** — form valid, both fills kept, `gross 0.44` |

### Read pass specifics

- 8 nav items; all 8 pages render with no `NaN` / `undefined` / `[object Object]`
- **Precision fix works on real data:** EURUSD trade shows `Avg entry $1.14798`,
  `Avg exit $1.14818` — previously both collapsed to `$1.15`
- What-if table: 6 horizons, 5-decimal prices, `At` column populated, price cells
  coloured (this short trade correctly shows price-up as red, price-down as green)
- Caveat note present **directly above the table** in both render sites
- No console, page, or request errors during the read pass

### Write pass specifics

- **Add Trade (UI):** creates the row; `entry_price 10.5` stored exactly
- **Import:** preview → `Import Trades` → undo. Every journal table restored;
  the `import_batches` row is *retained* and stamped `undone_at`, which is the
  documented audit behaviour, not a leak
- **Fill add/remove:** `2 → 3 → 2` fills, DB byte-identical after removal
- **Cleanup:** after removing test rows, all 11 journal tables byte-identical
  to baseline

---

## D1 — **FIXED** (2026-10-06, 492 backend tests)

Manual entry could not record an FX lot; four layers refused `0.01`. Each is
repaired, and the reproduction below now succeeds end to end.

| Layer | Was | Now |
|---|---|---|
| `AddTradeModal` input | `min="1"` in a `<form>` → submit refused | `min/step 0.001` |
| `AddTradeModal` payload | `parseInt("0.01") → 0` | `Number(...)` |
| `TradeCreate.quantity` | `int` → `422 int_from_float` | `float = Field(1, gt=0)` |
| `_parse_exec_body` | bare `int()` → `0.01` silently became `0` | `float` + `> 0` guard (400, readable) |

Verified against a fresh clone of the live journal:

```
quantity=1 / 0.01 / 0.001 / 14.76 / 1.5  → HTTP 201, stored qty exact
quantity=0 / -1                           → 422 greater_than
execution qty=0.1                         → 200, fill stored as 0.1
execution qty=0                           → 400 "Quantity must be greater than zero."
```

### D1b — the door D1 opened

Widening the type let an FX trade save with **`gross_pnl = 0.00`** where the
import path would report `0.44`: `compute_manual_pnl` multiplied by nothing.
Two numbers for the same fill, differing by how it entered the journal. It now
takes instrument + ticker and multiplies by `units_per_lot` — exactly the rule
`_recalculate_and_save` applies — and refuses an unknown futures point value
rather than writing `0.0` that reads as a flat trade.

```
FX EURUSD 0.01 lot  (1.15298 → 1.15342)  →  gross 0.44   (was 0.00)
OPTION 2 @ +0.75 with $1.30 commission    →  (150.0, 148.7)
STOCK 100 @ +$0.10                       →  (100.0, 100.0)  unchanged
```

Browser repro: form `checkValidity() → true` with `0.01`, both fills stored at
`0.01` with their exact FX prices, `gross 0.44`, row deleted afterwards, clone
journal tables byte-identical, live md5 unchanged.

---

## D1 — Manual trade entry cannot record a fractional quantity *(as found)*

**Severity: high.** You cannot enter an FX lot through the UI.

| Layer | Code | Effect |
|---|---|---|
| Frontend input | `AddTradeModal.js:141` `min="1"` | browser rejects `0.01` |
| Frontend payload | `AddTradeModal.js:52` `quantity: parseInt(...)` | `parseInt("0.01") → 0` |
| Backend schema | `main.py:1251` `quantity: int = 1` | `422 int_from_float` |
| Execution editor | `main.py:1490` `'qty': int(body['qty'])` | `2 → 0` |

Against the isolated clone:

```
quantity=1    → HTTP 201
quantity=0.01 → HTTP 422 {"detail":[{"type":"int_from_float",...,"Input should be a valid integer"}]}
```

**Why it matters here:** your journal holds **2,640 fractional fills vs 63 whole
ones** — `0.01` × 376, `0.1` × 388, and dozens of other lot sizes. Every FX trade
in the system came from the MT5 importer, which casts correctly
(`csv_parser.py:1532` keeps a fractional qty as float). So the import path and the
manual path disagree about what a quantity is: one half of the app can write the
data, the other half cannot.

Also latent: `_parse_exec_body` uses bare `int()`, so adding a fill to an FX trade
through the *Add Execution* editor silently converts `0.1 → 0`.

**Origin:** initial commit `de5fd92` (`quantity: int = 1`). Pre-existing.

---

## D2 — No UI to delete a trade

**Severity: medium.**

- Backend `DELETE /api/trades/{id}` exists and works (verified: HTTP 200, row gone)
- `tradesApi.delete` is defined in `api.js`
- **Nothing calls it** — `grep` over all of `frontend/src` finds zero invocations
- Trade detail exposes: Back / Previous / Next / chart / tabs / **Edit** — no delete
- `Help.js:176` still says *"If you deleted a bad trade and need to re-import it…"*

**Origin:** the caller was a `handleDelete` in `TradeRow.js` at the initial commit;
**`267ea70` (V3: the structural redesign) deleted it** and never replaced it. So a
documented workflow has had no UI for the last several redesigns.

**Consequence for this test:** the only way to remove a test row was
`DELETE /api/trades/{id}` over HTTP.

---

## D3 — `PUT /api/trades/{id}` has never been callable

**Severity: low (unreachable code, not user-visible).**

`tradesApi.update` is defined, the endpoint exists, and **`git log -S` finds no
commit where a caller ever existed**. `AddTradeModal` takes only
`{accounts, defaultAccountId, onClose, onSaved}` — no edit mode, never had one.

**Result:** core trade fields — ticker, date, side, entry price, quantity — cannot
be edited through the UI at all. You can edit *analysis* fields (Stop Loss, target,
R-multiple, strategy) via `PATCH .../analysis`, but not the trade itself.

---

## D4 — `tradesApi.deleteCustomSetup` also has no caller

**Severity: low.** The backend route `DELETE /api/setups/custom/{id}` exists;
`api.js` wraps it; nothing invokes it. Custom setups can be created
(`TradeRow.js:49`) but not removed from the UI. Settings → Strategies manages a
*different* store (`libraryApi`), which is intact.

**Origin:** same era as D2; never wired.

---

## Retracted (checked, not defects)

- **`/favicon.ico` 404** — an artifact of my harness. `frontend/public/index.html`
  declares `<link rel="icon" href="data:image/svg+xml,…">`; my synthetic test
  `index.html` omitted it. Re-run with the real file: **zero favicon requests**.
- **Import undo "incomplete"** — the retained, `undone_at`-stamped batch row is
  documented behaviour (`undo_import` docstring: *"Journal data only"*). All
  journal tables did restore.
- **Fill delete "2 → 1"** — my harness's `/Delete/` locator also matched the
  remaining `Delete execution N` buttons and fired a second deletion. The app has
  no confirmation dialog for a fill delete (noted as an observation, not a defect).
- **`sqlite_sequence` differs after insert+delete** — an autoincrement high-water
  mark cannot decrease; excluded from equality checks.

---

## Pre-existing vs introduced

| | Origin |
|---|---|
| D1 fractional quantity | `de5fd92` (initial commit) |
| D2 no delete UI | `267ea70` (V3 redesign) |
| D3 no trade edit UI | never existed |
| D4 no custom-setup delete | never wired |
| CI `Run frontend tests` red | before my first push; 0 successes in 14 runs |

**None of the four defects come from the precision, what-if or CI-lint commits.**
That work verified clean: precision fix confirmed live in the read pass, colour +
caveat in both render sites, fatal lint rules clean, 119 unit tests passing.

---

## Reproducing

Artifacts in `outputs/ui-check/`: `comprehensive.js` (read pass),
`write_check.js` (write + restore), `dbstate.py` (per-table count + digest),
`seed_check.py` (clone, asserting the live md5), `serve.py` (isolated server).

```bash
python3 seed_check.py && python3 dbstate.py /tmp/ui-clone/journal.db > before.json
UI_CLONE=/tmp/ui-clone FRONTEND_DIR=/tmp/build/build python3 serve.py &
node comprehensive.js          # read pass
UI_CLONE=/tmp/ui-clone BASE=http://127.0.0.1:8010 node write_check.js   # write pass
```
