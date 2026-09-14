# True Friend dashboard

Built on the dashboard template (`../dashboard-template`): **the owner's Excel
report → a validated build → parquet → Streamlit**. The build is a gate. It
recomputes the report's By Shop and Summary sheets from their raw tabs and
refuses to publish if they disagree with the workbook's own cached values.

```
data/raw/Report True Friend V3.1 DD-MM-YYYY[-Final].xlsx
        │
        │  Data Total (month × shop)  +  Target Truefriend Submit 10 (roster)
        ▼
   make build ── recompute By Shop (per shop) + Summary (per province, region)
        │        reconcile every cell against the sheet's cached values
        │        FAIL ⇒ nothing written
        ▼
data/processed/{shop,province,region,monthly}.parquet + meta.json ──► app.py
```

The template's playbook is in [ARCHITECTURE.md](ARCHITECTURE.md). Everything
below is specific to this workbook.

## Run it

```bash
make setup                                                     # venv + deps
cp .streamlit/secrets.toml.example .streamlit/secrets.toml     # set a long random password
make build                                                     # both snapshots in data/raw
make app                                                       # http://localhost:8501
```

## Weekly runbook

```bash
make refresh FILE="~/Downloads/Report True Friend V3.1 18-09-2026.xlsx"
```

Keep the owner's filename. The date is a label only: the **period comes from
`Summary!H16`**, and the build checks it against the newest month in Data
Total. Name the month-end copy `...-Final.xlsx`. The build publishes every
`-Final` plus the newest working copy, one period per month. Anything not
matching the convention (e.g. `... Aug 2026 Visibility 1.xlsx`) is skipped and
named in the log.

## What the build reads, and what it ignores

| sheet | how | why |
|---|---|---|
| `Data Total` | feed, by header name (row 1) | the monthly rows every report formula sums |
| `Target Truefriend Submit 10` | feed, by header name (row 4) | roster: zone, province, MOU targets, visibility |
| `By Shop` | guarded positions, row 4 labels | cached values to reconcile against |
| `Summary` | guarded positions, rows 4 and 18; A1, A2, H16 | cached values; day counts; report month |
| `Pivot Base` | **not read** | the baseline is recomputed from Data Total, which is how a stale pivot is caught |
| `Data Current`, `Data Last`, `Sheet5`, `Target … (2)` | forbidden | hidden staging / old copies pointing at external workbooks |

The reasons for every ignored sheet are in `src/config.py → IGNORED_SHEETS`.

## The arithmetic being mirrored

- **Run-rate** = actual ÷ `Summary!A1` (days of data) × `Summary!A2` (days in month)
- **Baseline GA** = (Jan'26 + Feb'26) ÷ 2, summing only Data Total rows whose
  Phase starts `Yes` (Pivot Base's page filter). Phase is a VLOOKUP into the
  current roster, so shops that have left drop out.
- **AP1D200 non-tourist** = Mass + Migrant. **RR vs MOU** = its run-rate ÷ `Target Month`.
- Excel text matching is case-insensitive (`UDON THANI…` = `Udon Thani…`), and a
  number stored as text counts as 0. Both are mirrored.

## What the current snapshots turned up

The build warns (it doesn't fail) about these. Each warning names the cell.

| finding | effect | fix upstream |
|---|---|---|
| `By Shop!V33` (Aug) / `V38` (Sep) is `=V32-U32`, the row above, not `=R33-J33` | one shop's migrant growth, total growth and % growth are wrong in the Excel | retype the formula |
| Sep: `Pivot Base` not refreshed after shop `79032307-17260061` left | Udon Thani / NE-1 / Total baseline is 176 too high in the Excel, so its growth is understated | Refresh All |
| 27 numbers stored as **text** in Data Total (e.g. `H7` GA Mass `'101'`) | Excel ignores them; so does the dashboard | convert to numbers |
| Roster row with a code but no Zone (`71323941-17552017`) | the shop is on no report row | complete or delete it |
| `By Shop!AD8/AD9` has no formula | harmless today (value is 0) | restore the SUMIFS |
| Aug `-Final` has data through day 26 of 31 | "final" run-rates still project ×1.19 | set A1 = A2 at month close if actuals are complete |

Where the dashboard shows a different number from the Excel (the broken
formula, the stale pivot), a small ⓘ note on that page says so.

## Layout

| path | what it is |
|---|---|
| `src/config.py` | the contract: filename regex, sheet names, header maps, guarded layouts |
| `src/ingest.py` | xlsx → frames; formula scan that finds deviant cells |
| `src/metrics.py` | recomputes By Shop, Summary province and region grids, plus the monthly history |
| `src/validate.py` | the gate: period, structure, lookups, coverage, reconciliation, roll-ups |
| `src/build.py` | snapshot selection, supersession, parquet + meta.json |
| `app.py` | Overview · Province · Shop · Trend · Visibility. Reads parquet only |
| `admin.py` | local upload → build → commit → push page (`make admin`) |
| `assets/` | logo and login background |

## Before deploying

This folder is not a git repository yet. `git init`, then `make setup` again to
enable the pre-commit gate, and push to a **private** repo. The raw workbooks
carry shop names and staff names (RS/PBH), and the git history keeps them forever.
