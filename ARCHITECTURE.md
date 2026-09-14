# Dashboard architecture playbook

Generalized from a working B2B performance dashboard: a weekly Excel workbook
turned into validated parquet and served as Streamlit. This document is the
reasoning; the code beside it is one worked example.

---

## 1. The shape

```
source files  ──►  data/raw/*.xlsx  ──[ build ]──►  data/processed/*.parquet  ──►  app
 (Drive, email,          the                │                                       │
  export, …)      reproducibility      VALIDATION GATE                     reads parquet only,
                      record          (fails ⇒ nothing                      never the source
                                        is written)                                 │
                                              │                                     ▼
                                          git push ──────────────────────────► hosted deploy
```

Four properties fall out of that shape, and they are the reason to use it:

**The build is a gate, not a converter.** Its job is to refuse. A converter that
always succeeds moves the failure downstream into a chart nobody can check.

**The serving path needs no credentials.** The app reads parquet out of its own
checkout. No database, no secret, no network call, nothing to rotate, nothing
that can be down at 9am on a Monday.

**Git is the deploy channel and the audit log at once.** A push redeploys; the
history holds every snapshot and every number ever published, so "what did this
say last month?" is `git show`, not an archaeology project.

**The raw files are committed.** They are the reproducibility record. `make
check` replays every snapshot ever received through today's code, which is the
only way to answer "did the number change, or did we change how we read it?"

### When this is the wrong architecture

Be honest about the fit before copying it:

| | |
|---|---|
| **Data volume** | Parquet in git works to roughly tens of MB. A repo that grows 50 MB a week will strangle every clone. Past that, move `data/processed` to object storage and keep everything else. |
| **Freshness** | The floor is one build. If someone needs sub-minute data, this is the wrong shape — you want a query layer, not a snapshot. |
| **Source of truth** | This design assumes a source that *churns* and *cannot be fixed upstream*. If you own a clean database table, most of the machinery here is ceremony: keep the validation gate and the period model, drop the layout guards. |
| **Sensitivity** | Committed raw files mean the git history holds the data forever. If it contains named individuals' performance, the repo is private and stays private — and that is now load-bearing, not hygiene. |

---

## 2. Five rules that carry the weight

### Rule 1 — Recompute; do not scrape

The source sheet already has the answers cached. Compute them again from the raw
feed anyway, then **assert the two agree**.

A scrape cannot tell a changed *number* from a changed *layout*. Recompute plus
reconcile can, and it fails the build before anything is published. There are two
further dividends: you can produce grains the source does not have (per category,
per region, per partner) that provably roll up to the published figure; and
reconciliation is a test of comprehension — if your reimplementation matches
across every field and every row, you have genuinely understood the source.

The cost is real. You must model the source's arithmetic exactly, **including
the parts you believe are wrong**. Mirror them, document them, and fix them
upstream. Never quietly "correct" a published number, or the reconciliation that
protects you becomes a thing you route around.

> In the origin project this reconciles 30 rows across 16 fields to ~1e-11,
> including exact rank agreement. That number is the whole licence to publish.

### Rule 2 — Bind by name; guard every position

Machine exports get read **by header name**. Column order is not part of the
contract and never becomes part of it by accident.

Hand-edited sheets have multi-row banner headers that no name lookup can
resolve, so they get read **positionally — with every position guarded by an
expected label**. A moved column then fails the build naming the cell, instead
of silently summing the wrong column into a published total.

Guards are cheap to write and are the difference between "the build broke" and
"the dashboard was wrong for five weeks".

### Rule 3 — The source declares its own generation

Hand-edited sheets get restructured. Somebody inserts a banner row and the grid
moves down one.

Do **not** edit your constants in place to follow it. Add a new layout to the
front of the list. The ingest tries each in order and takes the first whose
guards all pass — so the sheet identifies its own generation, and every
historical snapshot still rebuilds under the layout it was actually written in.

The moment you edit a layout in place you lose `make check`, and with it the
ability to distinguish a real change from a reading change — exactly when you
most need to.

### Rule 4 — Periods come from the data, not the filename

A file named for the day it was exported *will* eventually carry the previous
month's rows. Read the period from the data's own month column. The data cannot
be wrong about which month it describes; the filename repeatedly has been.

Publish one period per month: the `-Final` snapshot once a month closes, plus
the newest working copy for the month in progress. A `-Final` wins its month and
supersedes any working copy that resolves to it. Every table carries `period` and
`is_final`, so the app is one selection away from any month and adding a period
never changes a schema.

**Pick the period once, app-wide.** Two tabs showing different months with no
indication is worse than having no filter at all.

### Rule 5 — Fail on wrong, warn on moved

| | |
|---|---|
| **FAIL** | a published number could be wrong: reconciliation breaks, a rollup does not tie, a join drops material value, a duplicate key appears |
| **WARN** | the source moved but the numbers still reconcile: a sheet appeared or vanished, the entity count changed, a cap started binding, the file is byte-identical to last week's |

Get this split wrong in the strict direction and the check is disabled within a
month — after which it protects nothing. A warning is a message to whoever runs
the build, *not* to dashboard viewers: leave it in the build log and in
`meta.json`, off the screen.

---

## 3. Ten failures this design is shaped by

Every one of these actually happened. They are why the code looks paranoid.

1. **`sorted(glob())[-1]` picked the wrong "latest".** A scratch copy named
   `..._2026-08-old-27.xlsx` sorts *after* `..._2026-08-27.xlsx`. It parsed
   fine and just carried older numbers. → Anchor the filename convention in a
   regex; skip and *name* anything that does not match.
2. **A sheet renamed with a trailing space.** `"Scorecard - Final "` failed an
   exact match and the search fell through to a stale sheet with a similar
   name, binding the whole dashboard to last quarter. → Match on the trimmed
   name; keep an explicit forbidden list; raise on two sheets that trim to the
   same name rather than guessing.
3. **A file dated September carried August rows** and published as September.
   → Rule 4.
4. **A cap was introduced mid-quarter.** Achievement clamped at 300% before
   weighting, which changed scores and therefore *rank* — one entity at 470%
   uncapped ranked 7th instead of 2nd. Nothing announced it. → Record caps in
   `meta.json`, warn every time one binds, and publish the uncapped figure
   alongside.
5. **A target definition switched between snapshots** (top-down plan → bottom-up
   feed), reshuffling 26 of 30 rankings. Week-over-week comparison across it was
   meaningless and looked fine. → Detect which definition the source used, record
   it per snapshot, publish both, and fail if it is neither.
6. **Attribution by the wrong key.** Joining actuals on *region* alone when the
   real key was *(entity, region)* effectively swapped two people's numbers. It
   stayed invisible because the grand total still tied — a swap cancels. → Assert
   rollups **per entity**, never only in aggregate.
7. **A cached app served stale data after a redeploy.** `@st.cache_data` on a
   zero-argument loader never invalidates. New code, last week's numbers. → Key
   the cache on a fingerprint of the data directory.
8. **A column that looked numeric was a label.** A per-unit rate column held
   `"150,200,250"` meaning three price variants; summed, five rows accounted for
   83% of the total. → Know what a column *means* before you sum it, and write
   the reason down where the next person will sum it.
9. **Blank and zero were conflated.** `fillna(0)` erased the difference between
   "upstream sent nothing" and "upstream sent zero", which made a projection
   fall below actuals for no visible reason. → Record missingness *before*
   filling, and carry it far enough to explain the anomaly.
10. **Hidden junk rows inflated every count.** Interleaved subtotal rows
    double-counted totals; drag-filled dropdowns created hundreds of rows with
    one cell and no data. → Require a key column to be non-blank; detect
    subtotal rows structurally and drop them with a recorded count.

---

## 4. The layers

| file | owns | you will edit it |
|---|---|---|
| `src/config.py` | **the contract.** Sheet names, header maps, layout generations, tolerances. Nothing else hardcodes a column letter. | constantly, then never |
| `src/ingest.py` | reading the file into tidy frames. Knows about Excel; knows nothing about the metric. | rarely |
| `src/metrics.py` | **recomputing the metric** and deriving every published grain. Knows the arithmetic; knows nothing about files. | whenever the metric changes |
| `src/validate.py` | the gate. `Report` collects FAIL/WARN; `run_all` is the checklist. | whenever you learn a new way to be wrong |
| `src/build.py` | orchestration: select snapshots → build → validate → write parquet + `meta.json`. Period selection and supersession live here. | rarely |
| `app.py` | presentation only. Reads parquet, never the source. | constantly |
| `admin.py` | the weekly runbook as a local page. Shells out to the same `make build` and the same hook. | rarely |

**`meta.json` is part of the contract.** It carries what the app needs to
describe itself honestly — which source file, which layout generation, which
caps, which period, which checks passed. Anything the app would otherwise have
to *assume* belongs there.

**The dependency arrow only points one way**: `app` → parquet ← `build` →
`metrics` → `ingest` → `config`. The app never imports `src/`. That is what lets
you rewrite every page without being able to break a number.

---

## 5. Adapting it: the checklist

1. **`make setup && make demo && make build && make app`.** Watch a working
   pipeline first — the build log shows layout selection, reconciliation,
   supersession and a cap warning on synthetic data.
2. **Rewrite `src/config.py`.** Every `[DOMAIN]` marker. Sheet names, the header
   map, one layout for the sheet as it is *today* — do not model history you do
   not have.
3. **Rewrite `build_scorecard` in `src/metrics.py`** to mirror your source's
   arithmetic. Mirror it exactly, oddities included.
4. **Point `RECONCILE_*` at the fields your source caches**, then run the build
   and grind the diffs to zero. *This is the work.* Expect it to take longer than
   everything else combined, and expect it to teach you things about your source
   that nobody at the company currently knows.
5. **Delete `tools/make_demo_source.py`** and `make demo` once real snapshots
   land in `data/raw/`.
6. **Add checks as you learn.** Every "how did nobody notice that?" becomes a
   `rep.fail` or a `rep.warn` the same day.
7. **Then, and only then, build pages.** The app layer is fast to write and easy
   to change precisely because nothing it does can make a number wrong.

### If your source is not Excel

Replace `ingest.py` and keep everything else. The `Source` dataclass is the
boundary: produce one and the rest of the pipeline does not care where it came
from. For a database, `read_guarded_sheet` and the layout generations mostly
evaporate — but keep `read_feed`'s "bind by name, fail loudly on a missing
column" posture, because a `SELECT *` into a rename is the same failure wearing
different clothes.

---

## 6. Patterns in the origin project, not in this template

Deliberately omitted to keep the scaffold small. Reach for them when the
matching problem shows up:

- **Parse the rule out of the sheet.** Where a block's definition lived in a
  `SUMIFS` formula, the ingest read the *formula* (a second `data_only=False`
  load) and extracted which categories it counted — so an upstream redefinition
  was followed automatically and recorded in `meta.json`, rather than being
  pinned in config and silently diverging. Powerful, and the first thing to
  reach for when a definition keeps moving.
- **A second, softer source.** Ten hand-maintained files feeding a forward
  pipeline view, read by a separate module whose failures are **non-fatal** —
  the core build must never depend on files ten different teams edit by hand.
  Note the asymmetry: the metric is gated, the supplementary view degrades.
- **Per-period archives for a source that has no history.** Live files that get
  overwritten every upload cannot describe a closed month, so closing a month
  copies them into `Archive/YYYY-MM/`. A period with no archive simply has no
  such page, rather than pairing a closed month's actuals with today's open
  deals.
- **Variant scorecards from one function.** The source published two sheets
  differing in exactly one selector. Both are built by the *same* function with
  an override, so the two pages cannot drift in any other respect, and both
  reconcile against their own sheet.
- **Normalising both sides of a join key.** Names in two scripts, NFKC
  decomposition, and an alias map where each entry carries the evidence that
  justifies it. A join key is a contract; normalise it once, in one place.
- **Bilingual UI** via a single `L(en, th)` helper, so translation is a
  two-argument call and never a parallel template.

---

## 7. Serving and access

**Streamlit renders server-side.** Rows removed before display never reach the
browser, so a filter applied server-side is genuine enforcement. This is the
single most useful property of the stack and the reason a password check can be
real access control rather than decoration.

The gate in `app.py` runs **before** any parquet is read. Order matters: an
unauthenticated visitor must not cause a data read.

**Know what a shared password is.** No per-viewer identity: you cannot revoke one
person without rotating for everyone, and you cannot scope data per viewer. On
the open internet with no rate limiting, the passphrase's entropy is the whole
defence — long and random, never memorable, and distribute the URL and the
password separately, since one forwarded message containing both is the entire
access-control story in one artifact.

If you need per-person identity or per-viewer scoping, that is OIDC
(`st.login()` + an auth block in secrets), not a bigger password. It replaces
both problems at once. If you build it, scope the **filter option lists** too —
leaving every region in a dropdown leaks the org structure even when the numbers
are hidden — and never wire a download button to an unfiltered frame.

**The secret is never in the repo**, and a missing secret **stops the app**
rather than falling through to an open dashboard. Do not "fix" that with a
`.get()` default.

---

## 8. Operations

**The pre-commit hook is the load-bearing piece.** `make build` already exits
non-zero and writes nothing on failure — but that only protects you if you
notice before committing anyway. The hook rebuilds whenever source data is
staged, stages the outputs on success, and blocks the commit on failure. A
failed or skipped build cannot reach a push, and from there a deploy.

**`make check` is the drift regression.** Rebuild every snapshot ever received,
through today's code. Run it after any change to `config.py` or `metrics.py`.
Note that it leaves `data/processed/` holding whatever it built *last* — run
`make build` again before serving.

**`--force` exists to inspect a broken snapshot, never to ship one.** If it is
ever used to ship, the gate is gone and everything above it was theatre.

**Write down why, next to the thing.** Nearly every comment in this template is
load-bearing: it records a decision someone would otherwise "helpfully" undo.
The half-life of *why* is about three weeks; the code outlives it by years.
