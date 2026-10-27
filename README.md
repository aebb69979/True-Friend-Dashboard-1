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
| `By Shop Ph1` (before Oct'26: `By Shop`) | guarded positions, row 4 labels | cached values to reconcile against |
| `Summary Ph1` (before Oct'26: `Summary`) | guarded positions, rows 4 and 18; A1, A2, H16 | cached values; day counts; report month |
| `Data Total PH2` | Phase 2 feed, by header name (row 1) | the current month's Phase 2 rows; AP1D250 M&M is its MOU measure |
| `Target Truefriend PH2` | Phase 2 feed, by header name (row 1) | Phase 2 MOU baseline (`AVG AP1D200`, or `AVG AP1D250` once renamed) and target (`Commission Recomment Target 1M`) |
| `By Shop Ph2` | guarded positions, row 4 labels | the Phase 2 shop list, zone, province and **typed** GA baseline (I:K, published as given); the rest reconciled |
| `Summary Ph2`, `Summary ` (PH1 + PH2) | guarded positions, rows 4 and 18 | cached values to reconcile against; the roll-up adds AP1D200 and AP1D250 as-is |
| `Pivot Base` | **not read** | the baseline is recomputed from Data Total, which is how a stale pivot is caught |
| `Data Current`, `Data Last`, `Sheet5`, `Target … (2)` | forbidden | hidden staging / old copies pointing at external workbooks |

The reasons for every ignored sheet are in `src/config.py → IGNORED_SHEETS`.

## The arithmetic being mirrored

- **Run-rate** = actual ÷ `Summary!A1` (days of data) × `Summary!A2` (days in month)
- **Baseline GA** = (Jan'26 + Feb'26) ÷ 2, summing only Data Total rows whose
  Phase starts `Yes` (Pivot Base's page filter). Phase is a VLOOKUP into the
  current roster, so shops that have left drop out.
- **AP1D200 non-tourist** = Mass + Migrant. **RR vs MOU** = its run-rate ÷ `Target Month`.
- **Phase 2** (from Oct'26): the same GA arithmetic on its own feed; MOU is
  AP1D250 Mass + Migrant (one column, no segment split). By Shop Ph2's SUMIFS
  have no month filter, so the build **fails** if Data Total PH2 holds any
  month but the report month. The dashboard's Phase selector shows Phase 1,
  Phase 2, or PH1 + PH2; Trend and Visibility are Phase 1 only (no Phase 2
  history yet).
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
| `app.py` | Overview · Province · Shop · Trend · Visibility. Reads parquet only. TH (default) / EN: names (tabs, columns, metrics, chart titles, labels) stay English in both; only explanations (notes, help, messages) go through `L(en, th)` |
| `admin.py` | local upload → build → commit → push page (`make admin`) |
| `assets/` | logo and login background |
| `web/` | the Cloudflare Pages masking shell (see above) |

## The pages.dev address (masking shell)

`web/index.html` is a single static file: a full-page `<iframe>` of
`https://true-friend-dashboard.streamlit.app/?embed=true`, served by Cloudflare
Pages at **true-friend-dashboard.pages.dev**. It's an iframe, not a redirect,
so the address bar keeps showing `pages.dev` while Streamlit serves the app.
`?embed=true` is Streamlit's documented embedding mode and only works because
the Community Cloud app is public.

**Why not a real custom domain:** Community Cloud only serves `*.streamlit.app`,
so DNS pointed at it fails on SSL. Hence the shell.

**Badge crop:** Community Cloud pins a "Built with Streamlit" bar to the bottom
of an embedded app. It's cross-origin, so it can't be selected or styled away.
The frame is `--badge-crop` (3rem) taller than the window, and the body clips
the overflow. If a sliver of the bar reappears, raise `--badge-crop`.

**Deploy (direct upload, deliberately not the Git integration):**

```bash
npx wrangler pages deploy web --project-name true-friend-dashboard --branch main
```

**The gotcha:** the repo copy of `web/index.html` and the deployed copy are
**not linked**. Editing it and running `git push` changes nothing live until
you run the deploy command again. Nothing warns you. The weekly data runbook
needs no Cloudflare step: the shell holds no data, only the pointer.

**Testing it:** headless Chrome renders the nested Community Cloud frame at 150px
tall, so an automated screenshot looks broken. Check it in a normal browser
window.

## Before deploying

This folder is not a git repository yet. `git init`, then `make setup` again to
enable the pre-commit gate, and push to a **private** repo. The raw workbooks
carry shop names and staff names (RS/PBH), and the git history keeps them forever.
