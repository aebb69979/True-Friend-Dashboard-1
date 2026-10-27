"""Assertions that gate the build.

    FAIL -> the build stops. Nothing is written, nothing ships.
    WARN -> printed and recorded in meta.json; the build continues.

Fail on anything that means a published number could be wrong. Warn on
anything that means the source moved, or the SHEET is wrong in a way we have
identified exactly and do not copy.

The centrepiece is reconciliation: every By Shop and Summary cell recomputed
from Data Total + the roster, compared against the sheet's cached value. A
disagreement is excused in exactly two cases, both named in the warning:

  * the sheet cell's formula deviates from its column (ingest found it), or a
    cell in the same row computed from one that does;
  * a province baseline differs by precisely the baseline of shops that have
    left the roster -- Pivot Base was not refreshed after the roster changed.

Anything else fails.

Phase 2 (from Oct'26) is reconciled the same way, grid by grid: By Shop Ph2,
Summary Ph2, and the PH1+PH2 "Summary ". Its GA baseline is typed on By Shop
Ph2 and has no source in the workbook, so it is published as given and every
build says so.
"""

from __future__ import annotations

import calendar
import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .ingest import Source, month_key
from .metrics import (SEG, baseline_rows, fold, joined_data, ph2_current,
                      report_month)


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: dict = field(default_factory=dict)   # structured, for meta.json

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> str:
        lines = [f"  WARN  {w}" for w in self.warnings]
        lines += [f"  FAIL  {f}" for f in self.failures]
        return "\n".join(lines) or "  all checks passed"


def _num(v) -> float | None:
    """A cached cell as a number: blank -> 0, error/text -> None."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 0.0
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(C.RECONCILE_ABS_TOL, C.RECONCILE_REL_TOL * abs(b))


# --- structural checks ------------------------------------------------------


def check_sheet_inventory(src: Source, rep: Report) -> None:
    got = {n.strip() for n in src.sheet_names}
    expected = {n.strip() for n in C.EXPECTED_SHEETS}
    if added := got - expected:
        rep.warn(f"new sheet(s) appeared: {sorted(added)}")
    if removed := expected - got:
        rep.warn(f"sheet(s) disappeared: {sorted(removed)}")
    if src.missing_optional:
        rep.warn(f"optional roster column(s) not found: {src.missing_optional}")


def check_period(src: Source, rep: Report, is_final: bool) -> None:
    """The reporting month and the two day counts every run-rate depends on."""
    label, key = report_month(src)
    de, dm = src.control.get("days_elapsed"), src.control.get("days_in_month")
    if key is None:
        rep.fail(f"Summary!H16 {label!r} is not a month label like \"Aug'26\".")
        return
    months = sorted(m for m in src.data["month"].dropna().unique())
    if not months:
        rep.fail("Data Total has no parseable month labels.")
        return
    if key != months[-1]:
        rep.fail(f"Summary!H16 reports {label} but Data Total's newest month is "
                 f"{months[-1]}. The report would publish the wrong month.")
    if not (_num(de) and _num(dm)):
        rep.fail(f"Summary!A1:A2 day counts are not numbers: {de!r}, {dm!r}.")
        return
    year, mon = int(key[:4]), int(key[5:])
    cal = calendar.monthrange(year, mon)[1]
    if int(dm) != cal:
        rep.fail(f"Summary!A2 says {label} has {dm} days; it has {cal}. Every "
                 f"run-rate is scaled by it.")
    if not 0 < de <= dm:
        rep.fail(f"Summary!A1 days elapsed = {de}, outside 1..{dm}.")
    if is_final and de < dm:
        rep.warn(f"-Final snapshot still projects: data through day {de:g} of "
                 f"{dm:g}, so run-rates are x{dm / de:.3f} of actuals.")
    sc = src.shop_control
    for k in ("days_elapsed", "days_in_month", "month_label"):
        if str(sc.get(k)) != str(src.control.get(k)):
            rep.fail(f"By Shop {k} = {sc.get(k)!r} but Summary has "
                     f"{src.control.get(k)!r}; the two sheets disagree.")


def check_data_total(src: Source, rep: Report) -> None:
    d = src.data
    bad_month = d[d["month"].isna()]
    if len(bad_month):
        rep.fail(f"{len(bad_month)} Data Total row(s) have an unparseable Mth, "
                 f"e.g. row {bad_month['xl_row'].iat[0]}: "
                 f"{bad_month['month_label'].iat[0]!r}")
    blank = d[d["code"].eq("")]
    if len(blank):
        rep.fail(f"{len(blank)} Data Total row(s) have no Combine Code "
                 f"(rows {blank['xl_row'].head(5).tolist()}).")
    dup = d[d.duplicated(["month_label", "code"], keep=False)]
    if len(dup):
        rep.fail(f"{len(dup)} Data Total rows duplicate (Mth, Combine Code), "
                 f"e.g. {dup[['month_label', 'code']].head(4).values.tolist()} "
                 f"-- SUMIFS would double count.")
    joined = (d["retailer_code"] + "-" + d["s2_code"]).map(fold)
    mism = d[joined.ne(d["code"].map(fold))]
    if len(mism):
        rep.warn(f"{len(mism)} Data Total row(s) where Combine Code is not "
                 f"Retailer Code_D-S2/L2 Code_T, e.g. row "
                 f"{mism['xl_row'].iat[0]}: {mism['code'].iat[0]!r}")
    if src.text_numbers:
        cells = ", ".join(f"{c} ({h}={t!r})" for c, h, t in src.text_numbers[:6])
        rep.warn(f"{len(src.text_numbers)} Data Total number(s) stored as TEXT "
                 f"count as 0 in every SUMIFS and pivot: {cells}"
                 + (" ..." if len(src.text_numbers) > 6 else ""))
        rep.notes["text_numbers"] = src.text_numbers
    seg_sum = sum(d[f"ga_{s}"] for s in SEG)
    off = d[(seg_sum - d["ga_total_sheet"]).abs() > 1e-9]
    if len(off):
        by_month = off["month_label"].value_counts().to_dict()
        rep.warn(f"Data Total 'GA' differs from Mass+Migrant+Tourist on "
                 f"{len(off)} row(s) {by_month}. No report formula uses 'GA'; "
                 f"the dashboard uses the segment sum.")


def check_roster(src: Source, rep: Report) -> None:
    t = src.target
    keys = t["code"].map(fold)
    if dup := sorted(t.loc[keys.duplicated(keep=False), "code"].unique()):
        rep.fail(f"duplicate Combine Code on the roster: {dup} -- VLOOKUP takes "
                 f"the first and SUMIFS adds both.")
    stub = t[t["zone"].eq("")]
    if len(stub):
        rep.warn(f"{len(stub)} roster row(s) have a Combine Code but no Zone, so "
                 f"appear on no report row: "
                 + ", ".join(f"row {r.xl_row} {r.code} {r.shop_name}"
                             for r in stub.itertuples()))
    noprov = t[t["zone"].ne("") & t["province"].eq("")]
    if len(noprov):
        rep.fail(f"{len(noprov)} roster shop(s) have a Zone but no Province, so "
                 f"Summary cannot count them: {noprov['code'].tolist()}")


def check_lookups(src: Source, rep: Report) -> None:
    """Our roster join must reproduce Data Total's cached Phase / Visibility.

    Those two columns are VLOOKUPs with a column INDEX (21, 25). If a column is
    inserted on the roster, the sheet silently starts returning a neighbouring
    column; our join is by header name, so it would disagree here first.
    """
    d = joined_data(src)
    for ours, sheet in (("phase", "phase_sheet"), ("visibility", "visibility_sheet")):
        a = d[ours].fillna("").astype(str)
        b = d[sheet].fillna("").astype(str)
        # Off-roster rows are #N/A in the sheet and missing in ours. A blank
        # roster cell comes back from VLOOKUP as 0.
        b = b.where(~b.isin(["#N/A", "0"]), "")
        bad = d[a.ne(b)]
        if len(bad):
            ex = bad[["xl_row", "code", ours, sheet]].head(3).values.tolist()
            rep.fail(f"Data Total {sheet[:-6].title()} (cached VLOOKUP) differs "
                     f"from the roster on {len(bad)} row(s), e.g. {ex}. Has a "
                     f"column been inserted on the roster?")


def check_coverage(src: Source, rep: Report) -> None:
    """Current-month actuals that land on no By Shop row."""
    label, _ = report_month(src)
    d = joined_data(src)
    cur = d[d["month_label"].map(fold) == fold(label)]
    total = cur["ga"].sum()
    off = cur[~cur["on_roster"]]
    if len(off) and total:
        share = off["ga"].sum() / total
        msg = (f"{share:.1%} of {label} GA ({off['ga'].sum():,.0f}) is on "
               f"{len(off)} shop(s) not on the roster: {off['code'].tolist()[:6]}"
               f" -- counted in Summary's province totals, on no By Shop row.")
        (rep.fail if share > C.OFF_ROSTER_GA_FAIL_SHARE else rep.warn)(msg)
    roster = src.target[src.target["zone"].ne("")]
    missing = roster[~roster["code"].map(fold).isin(cur["code"].map(fold))]
    if len(missing):
        rep.warn(f"{len(missing)} roster shop(s) have no {label} row in Data "
                 f"Total (shown as zero): {missing['code'].tolist()[:8]}")


# --- reconciliation ---------------------------------------------------------


def _excused_fields(src: Source, grid: str, key: str) -> dict[str, str]:
    """Fields of one row excused by a deviant formula, mapped to the reason."""
    out = {}
    deps = C.FIELD_DEPENDENTS_SHORT if grid in C.SHORT_GRIDS else C.FIELD_DEPENDENTS
    for (g, k, f), why in src.deviations.items():
        if g != grid or k != key:
            continue
        out[f] = why
        for dep in deps.get(f, []):
            out.setdefault(dep, f"computed from {f}: {why}")
    return out


def reconcile(ours: pd.DataFrame, sheet: pd.DataFrame, key: str, grid: str,
              src: Source, rep: Report, excuse=None) -> pd.DataFrame:
    """Compare every metric field, row by row, on `key`.

    `excuse(row_key, field, ours, sheet)` may return a reason string to turn a
    mismatch into a warning. Returns the per-field summary for the build log.
    """
    o = ours.set_index(ours[key].map(fold))
    s = sheet.set_index(sheet[key].map(fold))
    stats = {f: [0.0, 0, 0] for f in C.RECONCILE_FIELDS}   # max diff, bad, excused
    failures: dict[str, list] = {}
    excused: list[str] = []
    for k, srow in s.iterrows():
        if k not in o.index:
            # A sheet row we did not produce: fine only if it is all zero.
            nonzero = [f for f in C.RECONCILE_FIELDS
                       if (_num(srow.get(f)) or 0) != 0]
            if nonzero:
                rep.fail(f"{grid}: sheet row {srow[key]!r} (row {srow['xl_row']}) "
                         f"has values but no source rows: {nonzero[:5]}")
            continue
        orow = o.loc[k]
        row_excuses = _excused_fields(src, grid, srow[key])
        for f in C.RECONCILE_FIELDS:
            if f not in s.columns:
                continue
            a, raw = float(orow[f]), srow[f]
            b = _num(raw)
            if b is not None:
                diff = abs(a - b)
                stats[f][0] = max(stats[f][0], diff)
                if _close(a, b):
                    continue
            why = row_excuses.get(f) or (excuse(k, f, a, b) if excuse else None)
            if why and f in row_excuses and not why.startswith("computed from"):
                # The root cell, recorded for the app's "differs from the
                # Excel" note; its dependents ride along under the same row.
                cell, _, rest = why.partition(" is ")
                formula, _, canonical = rest.partition("; column is ")
                rep.notes.setdefault("recomputed_cells", []).append({
                    "grid": grid, "key": srow[key], "field": f, "cell": cell,
                    "sheet_formula": formula,
                    "column_formula": canonical.replace("{r}", cell.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
                    "ours": a, "sheet": b})
            if why:
                stats[f][2] += 1
                excused.append(f"{srow[key]} {f}: ours={a:,.4f} sheet={raw!r} "
                               f"({why})")
                continue
            stats[f][1] += 1
            failures.setdefault(f, []).append((srow[key], a, raw))
    for f, bad in failures.items():
        ex = ", ".join(f"{k}: ours={a:,.4f} sheet={b!r}" for k, a, b in bad[:3])
        rep.fail(f"{grid} reconciliation: {f} differs on {len(bad)} row(s). {ex}")
    if excused:
        rep.warn(f"{grid}: {len(excused)} cell(s) differ from the sheet for an "
                 f"identified reason and are published as recomputed: "
                 + " | ".join(excused[:6]) + (" ..." if len(excused) > 6 else ""))
        rep.notes.setdefault("excused", {})[grid] = excused
    return pd.DataFrame([{"grid": grid, "field": f, "max_abs_diff": v[0],
                          "n_mismatch": v[1], "n_excused": v[2]}
                         for f, v in stats.items()])


def stale_baseline_excuser(src: Source, rep: Report):
    """Explain a province baseline gap as shops that have left the roster.

    Pivot Base is a pivot: it keeps whatever the roster looked like when it was
    last refreshed. When a shop leaves, its Jan+Feb GA stays in its province's
    baseline until someone clicks Refresh. We recompute with today's roster; if
    the sheet's excess equals, on all three segments, the baseline of a small
    set of off-roster shops in that province, that is the explanation.
    """
    d = joined_data(src)
    months = {fold(m) for m in C.BASELINE_MONTHS}
    jf = d[d["month_label"].map(fold).isin(months)]
    in_base = set(baseline_rows(d)["code_key"])
    off = jf[~jf["code_key"].isin(in_base)]
    cand = (off.groupby(["province_key", "code"])[[f"ga_{s}" for s in SEG]].sum()
            / C.BASELINE_DIVISOR)
    sheet = src.province_sheet.assign(k=src.province_sheet["province"].map(fold)).set_index("k")
    from .metrics import build_province
    ours = build_province(src).set_index("province_key")
    found: dict[str, tuple] = {}

    def explain(k: str) -> tuple | None:
        if k in found:
            return found[k]
        found[k] = None
        if k not in sheet.index or k not in ours.index:
            return None
        gap = np.array([(_num(sheet.at[k, f"base_{s}"]) or 0) - ours.at[k, f"base_{s}"]
                        for s in SEG])
        if not np.any(np.abs(gap) > C.RECONCILE_ABS_TOL):
            return None
        if k not in cand.index.get_level_values(0):
            return None
        shops = cand.loc[k]
        for n in range(1, C.STALE_BASELINE_MAX_SHOPS + 1):
            for combo in itertools.combinations(shops.index, n):
                if np.allclose(shops.loc[list(combo)].sum().to_numpy(), gap,
                               atol=C.RECONCILE_ABS_TOL):
                    found[k] = (list(combo), gap)
                    return found[k]
        return None

    stale_fields = {"base_mass", "base_migrant", "base_tourist", "base_ga",
                    "growth_mass", "growth_migrant", "growth_tourist",
                    "growth_ga", "growth_pct"}

    def excuse(k, f, a, b):
        if f not in stale_fields:
            return None
        hit = explain(k)
        if not hit:
            return None
        rep.notes.setdefault("stale_baseline", {})[k] = {
            "shops": hit[0], "sheet_minus_ours": [float(x) for x in hit[1]]}
        return (f"Pivot Base not refreshed: sheet baseline still includes "
                f"off-roster {', '.join(hit[0])}")

    return excuse


def check_shop_grid(shop: pd.DataFrame, src: Source, rep: Report) -> pd.DataFrame:
    s = src.shop_sheet
    ours_keys, sheet_keys = set(shop["code"].map(fold)), set(s["code"].map(fold))
    if extra := sheet_keys - ours_keys:
        rep.fail(f"By Shop has shop(s) that are not on the roster: {sorted(extra)}")
    if absent := ours_keys - sheet_keys:
        rep.warn(f"roster shop(s) missing from By Shop, published anyway: "
                 f"{sorted(absent)}")
    if dup := sorted(s.loc[s["code"].map(fold).duplicated(), "code"]):
        rep.fail(f"By Shop lists a shop twice: {dup}")
    # By Shop's baseline is a VLOOKUP with no IFERROR: a roster shop missing
    # from the pivot shows #N/A. That is not a number we can publish as 0
    # silently.
    na = shop[shop["baseline_missing"]]
    if len(na):
        rep.warn(f"{len(na)} roster shop(s) have no Jan/Feb'26 baseline rows "
                 f"(By Shop shows #N/A); published as 0: {na['code'].tolist()}")

    def excuse(k, f, a, b):
        if f.startswith(("base_", "growth_")) and b is None:
            row = shop.set_index(shop["code"].map(fold)).loc[k]
            if row["baseline_missing"]:
                return "no baseline rows; sheet shows an error"
        return None

    rec = reconcile(shop, s, "code", "shop", src, rep, excuse)
    # Text columns: shown to people, so a disagreement is worth a line.
    o = shop.set_index(shop["code"].map(fold))
    for f in ("zone", "province", "phase"):
        diff = [(r.code, o.at[fold(r.code), f], getattr(r, f))
                for r in s.itertuples() if fold(r.code) in o.index
                and fold(o.at[fold(r.code), f]) != fold(getattr(r, f))]
        if diff:
            rep.warn(f"By Shop {f} differs from the roster for {len(diff)} "
                     f"shop(s) (roster value published): {diff[:3]}")
    if deviant := [f"{k} {f}: {w}" for (g, k, f), w in src.deviations.items()
                   if g == "shop"]:
        rep.notes.setdefault("formula_deviations", {})["shop"] = deviant
    return rec


def check_province_grid(province: pd.DataFrame, src: Source, rep: Report
                        ) -> pd.DataFrame:
    p = province
    material = p[~p["on_summary"] & ((p["n_shops"] > 0) | (p["ga"] > 0)
                                     | (p["ap_nontourist"] > 0))]
    if len(material):
        rep.fail(f"province(s) with shops or actuals have no Summary row: "
                 f"{material['province'].tolist()}")
    wrong = p[p["on_summary"] & (p["n_shops"] > 0)
              & (p["region"] != p["region_sheet"])]
    if len(wrong):
        rep.fail("Summary region disagrees with the roster Zone for: "
                 + ", ".join(f"{r.province} (Summary {r.region_sheet}, roster "
                             f"{r.region})" for r in wrong.itertuples()))
    rec = reconcile(p, src.province_sheet, "province", "province", src, rep,
                    stale_baseline_excuser(src, rep))
    rep.notes["summary_rank_scope"] = "Summary!AG ranks only rows 32:62"
    return rec


def _stale_gaps(rep: Report, province_sheet: pd.DataFrame | None
                ) -> dict[str, np.ndarray]:
    """The excused Pivot Base gap per province -- or, given a Summary province
    grid, per region of that grid plus Total."""
    gaps: dict[str, np.ndarray] = {}
    stale = rep.notes.get("stale_baseline", {})
    if province_sheet is None:
        return {k: np.array(v["sheet_minus_ours"]) for k, v in stale.items()}
    prov = province_sheet.assign(k=province_sheet["province"].map(fold))
    region_of = dict(zip(prov["k"], prov["region"]))
    for k, v in stale.items():
        g = np.array(v["sheet_minus_ours"])
        for r in (region_of.get(k), "Total"):
            gaps[fold(r)] = gaps.get(fold(r), np.zeros(3)) + g
    return gaps


def check_region_grid(region: pd.DataFrame, src: Source, rep: Report
                      ) -> pd.DataFrame:
    """Regions are sums of provinces, so a stale province baseline flows up.

    Excuse a region field only by the SAME amount the province grid excused.
    """
    return reconcile(region, src.region_sheet, "region", "region", src, rep,
                     _carried_excuser(region, "region",
                                      _stale_gaps(rep, src.province_sheet)))


def _carried_excuser(ours_frame: pd.DataFrame, key: str,
                     gaps: dict[str, np.ndarray]):
    """Excuse a field off by exactly a stale baseline carried up into it."""
    ours = ours_frame.set_index(ours_frame[key].map(fold))

    def excuse(k, f, a, b):
        if k not in gaps or b is None:
            return None
        g = gaps[k]
        want = {"base_mass": g[0], "base_migrant": g[1], "base_tourist": g[2],
                "base_ga": g.sum(), "growth_mass": -g[0], "growth_migrant": -g[1],
                "growth_tourist": -g[2], "growth_ga": -g.sum()}
        if f in want and _close(a + want[f], b):
            return "stale Pivot Base baseline carried up from its province(s)"
        if f == "growth_pct":
            row = ours.loc[k]
            base = row["base_ga"] + g.sum()
            sheet_pct = (row["growth_ga"] - g.sum()) / base if base else 0.0
            if _close(sheet_pct, b):
                return "stale Pivot Base baseline carried up from its province(s)"
        return None

    return excuse


def check_rollups(shop: pd.DataFrame, province: pd.DataFrame,
                  region: pd.DataFrame, rep: Report) -> None:
    """Our own grains must tie PER KEY, not only in aggregate."""
    body = region[region["region"] != "Total"]
    total = region[region["region"] == "Total"].iloc[0]
    for f in ("n_shops", "ga", "base_ga", "ap_nontourist", "mou_target"):
        if not _close(float(body[f].sum()), float(total[f])):
            rep.fail(f"region rows do not sum to Total on {f}")
        if not _close(float(province[f].sum()), float(total[f])):
            rep.fail(f"provinces do not sum to Total on {f}")
    sp = shop.assign(k=shop["province"].map(fold)).groupby("k")[["ga", "ap_nontourist"]].sum()
    pp = province.set_index("province_key")[["ga", "ap_nontourist", "n_shops"]]
    for f in ("ga", "ap_nontourist"):
        diff = (pp[f] - sp[f].reindex(pp.index).fillna(0)).abs()
        if (bad := diff[diff > C.RECONCILE_ABS_TOL]).size:
            rep.warn(f"province {f} is not the sum of its roster shops for "
                     f"{bad.size} province(s) (Data Total and roster disagree on "
                     f"a shop's province, or off-roster actuals): "
                     f"{bad.index.tolist()[:4]}")


def check_against_previous(meta: dict, meta_path: Path, rep: Report) -> None:
    if not meta_path.exists():
        return
    try:
        prev_all = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        rep.warn(f"could not read previous {meta_path.name}: {exc}")
        return
    prev = (prev_all.get("periods") or {}).get(meta.get("period")) or {}
    if not prev:
        return
    if prev.get("source_sha256") == meta.get("source_sha256"):
        rep.warn(f"{meta['source_file']} is byte-identical to the last build of "
                 f"{meta['period']}.")
    elif prev.get("source_file") == meta.get("source_file"):
        rep.warn(f"{meta['source_file']} has the same name as the last build "
                 f"but different contents -- revised in place.")
    if (p := prev.get("n_shops")) and p != meta.get("n_shops"):
        rep.warn(f"roster size for {meta['period']} changed since the last "
                 f"build: {p} -> {meta.get('n_shops')}")


# --- Phase 2 -----------------------------------------------------------------


def check_ph2_period(src: Source, rep: Report) -> None:
    """Every Phase 2 sheet must run on Summary Ph1's day counts and month."""
    p2 = src.ph2
    for name, ctl in ((p2.sheet_names["shop"], p2.shop_control),
                      (p2.sheet_names["summary"], p2.control),
                      (repr(p2.sheet_names["summary_all"]), p2.all_control)):
        for k in ("days_elapsed", "days_in_month", "month_label"):
            if str(ctl.get(k)) != str(src.control.get(k)):
                rep.fail(f"{name} {k} = {ctl.get(k)!r} but Summary Ph1 has "
                         f"{src.control.get(k)!r}; the phases disagree.")


def check_ph2_feeds(src: Source, rep: Report) -> None:
    p2, (label, key) = src.ph2, report_month(src)
    d = p2.data
    if len(bad := d[d["month"].isna()]):
        rep.fail(f"{len(bad)} Data Total PH2 row(s) have an unparseable Mth, e.g. "
                 f"row {bad['xl_row'].iat[0]}: {bad['month_label'].iat[0]!r}")
    if len(other := d[d["month"].notna() & d["month"].ne(key)]):
        rep.fail(f"Data Total PH2 holds {sorted(other['month_label'].unique())} "
                 f"besides {label} ({len(other)} rows). By Shop Ph2's SUMIFS have "
                 f"no month filter, so the Excel adds those rows into {label}. "
                 f"Keep only the current month in Data Total PH2.")
    if len(blank := d[d["code"].eq("")]):
        rep.fail(f"{len(blank)} Data Total PH2 row(s) have no join_code "
                 f"(rows {blank['xl_row'].head(5).tolist()}).")
    if len(dup := d[d.duplicated(["month_label", "code"], keep=False)]):
        rep.fail(f"{len(dup)} Data Total PH2 rows duplicate (Mth, join_code), e.g. "
                 f"{dup[['month_label', 'code']].head(4).values.tolist()}.")
    if p2.text_numbers:
        cells = ", ".join(f"{c} ({h}={t!r})" for c, h, t in p2.text_numbers[:6])
        rep.warn(f"{len(p2.text_numbers)} Data Total PH2 number(s) stored as TEXT "
                 f"count as 0: {cells}" + (" ..." if len(p2.text_numbers) > 6 else ""))
    t = p2.target
    if dup := sorted(t.loc[t["code"].map(fold).duplicated(keep=False), "code"].unique()):
        rep.fail(f"duplicate Combine Code on Target Truefriend PH2: {dup} -- "
                 f"By Shop Ph2's SUMIFS would add both.")
    if p2.baseline_header != C.TARGET_PH2_BASELINE_HEADERS[0]:
        rep.warn(f"Phase 2 MOU baseline is read from Target Truefriend PH2 column "
                 f"{p2.baseline_header!r} (what By Shop Ph2!Z sums) and published "
                 f"as AP1D250. Rename the header to "
                 f"{C.TARGET_PH2_BASELINE_HEADERS[0]!r} once confirmed.")


def check_ph2_shops(src: Source, shop1: pd.DataFrame, shop2: pd.DataFrame,
                    rep: Report) -> pd.DataFrame:
    p2, (label, _) = src.ph2, report_month(src)
    s = p2.shop_sheet
    keys = s["code"].map(fold)
    if dup := sorted(s.loc[keys.duplicated(), "code"]):
        rep.fail(f"By Shop Ph2 lists a shop twice: {dup}")
    roster = set(p2.target["code"].map(fold))
    if off := sorted(s.loc[~keys.isin(roster), "code"]):
        rep.warn(f"By Shop Ph2 shop(s) not on Target Truefriend PH2, so MOU "
                 f"baseline and target are 0: {off}")
    if absent := sorted(p2.target.loc[~p2.target["code"].map(fold).isin(set(keys)),
                                      "code"]):
        rep.warn(f"Target Truefriend PH2 shop(s) missing from By Shop Ph2, so on "
                 f"no report row and not published: {absent}")
    if len(nop := s[s["zone"].eq("") | s["province"].eq("")]):
        rep.fail(f"By Shop Ph2 shop(s) with no Zone or PBH Province, so on no "
                 f"Summary row: {nop['code'].tolist()}")
    if both := sorted(set(shop1["code"].map(fold)) & set(shop2["code"].map(fold))):
        rep.fail(f"shop(s) in BOTH phases, which the PH1+PH2 Summary would count "
                 f"twice: {both}")
    zone_t = p2.target.assign(k=p2.target["code"].map(fold)).drop_duplicates("k")
    zone_t = dict(zip(zone_t["k"], zone_t["zone"]))
    if diff := [(r.code, r.zone, zone_t[fold(r.code)]) for r in s.itertuples()
                if zone_t.get(fold(r.code)) and fold(zone_t[fold(r.code)]) != fold(r.zone)]:
        rep.warn(f"By Shop Ph2 Zone differs from the PH2 roster Region for "
                 f"{len(diff)} shop(s) (By Shop Ph2 published): {diff[:3]}")

    n_typed = int((~shop2["baseline_missing"]).sum())
    rep.warn(f"Phase 2 GA baselines for {n_typed} shop(s) are typed values in By "
             f"Shop Ph2!I:K. The months they average are not in the workbook, so "
             f"they are published as given, unverified.")
    rep.notes["ph2_baseline"] = "typed in By Shop Ph2!I:K; source period unknown"

    cur = ph2_current(src)
    total = cur["ga_mass"].sum() + cur["ga_migrant"].sum() + cur["ga_tourist"].sum()
    off = cur[~cur["code_key"].isin(set(keys))]
    if len(off) and total:
        ga_off = (off["ga_mass"] + off["ga_migrant"] + off["ga_tourist"]).sum()
        share = ga_off / total
        (rep.fail if share > C.OFF_ROSTER_GA_FAIL_SHARE else rep.warn)(
            f"{share:.1%} of Phase 2 {label} GA ({ga_off:,.0f}) is on {len(off)} "
            f"Data Total PH2 shop(s) not on By Shop Ph2: {off['code'].tolist()[:6]}")
    if len(nocur := shop2[~shop2["has_current_row"]]):
        rep.warn(f"{len(nocur)} By Shop Ph2 shop(s) have no {label} row in Data "
                 f"Total PH2 (shown as zero): {nocur['code'].tolist()[:8]}")

    return reconcile(shop2, s, "code", "shop_ph2", src, rep)


def _check_province_rows(p: pd.DataFrame, sheet_name: str, rep: Report) -> None:
    if len(m := p[~p["on_summary"] & ((p["n_shops"] > 0) | (p["ga"] > 0)
                                       | (p["ap_nontourist"] > 0))]):
        rep.fail(f"province(s) with shops or actuals have no {sheet_name} row: "
                 f"{m['province'].tolist()}")
    if len(w := p[p["on_summary"] & (p["n_shops"] > 0)
                  & (p["region"].map(fold) != p["region_sheet"].map(fold))]):
        rep.fail(f"{sheet_name} region disagrees with the shops' Zone for: "
                 + ", ".join(f"{r.province} ({sheet_name} {r.region_sheet}, "
                             f"shops {r.region})" for r in w.itertuples()))


def check_ph2_grids(src: Source, frames: dict, rep: Report) -> list[pd.DataFrame]:
    p2 = src.ph2
    n_sum, n_all = p2.sheet_names["summary"], repr(p2.sheet_names["summary_all"])
    _check_province_rows(frames["province_ph2"], n_sum, rep)
    _check_province_rows(frames["province_all"], n_all, rep)
    recs = [reconcile(frames["province_ph2"], p2.province_sheet, "province",
                      "province_ph2", src, rep),
            reconcile(frames["region_ph2"], p2.region_sheet, "region",
                      "region_ph2", src, rep),
            # A stale Phase 1 Pivot Base flows into the roll-up unchanged.
            reconcile(frames["province_all"], p2.all_province_sheet, "province",
                      "province_all", src, rep,
                      _carried_excuser(frames["province_all"], "province_key",
                                       _stale_gaps(rep, None))),
            reconcile(frames["region_all"], p2.all_region_sheet, "region",
                      "region_all", src, rep,
                      _carried_excuser(frames["region_all"], "region",
                                       _stale_gaps(rep, p2.all_province_sheet)))]
    for scope in ("ph2", "all"):
        reg, prov = frames[f"region_{scope}"], frames[f"province_{scope}"]
        body, total = reg[reg["region"] != "Total"], reg[reg["region"] == "Total"].iloc[0]
        for f in ("n_shops", "ga", "base_ga", "ap_nontourist", "mou_target"):
            if not (_close(float(body[f].sum()), float(total[f]))
                    and _close(float(prov[f].sum()), float(total[f]))):
                rep.fail(f"{scope}: regions / provinces do not sum to Total on {f}")
    return recs


def run_all(src: Source, meta: dict, meta_path: Path, is_final: bool,
            shop: pd.DataFrame, province: pd.DataFrame, region: pd.DataFrame,
            ph2: dict | None = None) -> tuple[Report, pd.DataFrame]:
    rep = Report()
    check_sheet_inventory(src, rep)
    check_period(src, rep, is_final)
    if rep.failures:          # nothing below is meaningful with a bad period
        return rep, pd.DataFrame()
    check_data_total(src, rep)
    check_roster(src, rep)
    check_lookups(src, rep)
    check_coverage(src, rep)
    check_against_previous(meta, meta_path, rep)
    recs = [check_shop_grid(shop, src, rep),
            check_province_grid(province, src, rep),
            check_region_grid(region, src, rep)]
    check_rollups(shop, province, region, rep)
    if src.ph2 is not None:
        check_ph2_period(src, rep)
        check_ph2_feeds(src, rep)
        recs.append(check_ph2_shops(src, shop, ph2["shop_ph2"], rep))
        recs += check_ph2_grids(src, ph2, rep)
    return rep, pd.concat(recs, ignore_index=True)
