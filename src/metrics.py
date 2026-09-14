"""Recompute By Shop and Summary from Data Total and the Target roster.

A from-scratch reimplementation of the workbook's formulas, NOT a scrape of
its cached values; validate.py asserts the two agree. Excel semantics are
mirrored deliberately, oddities included:

  * SUMIFS / COUNTIFS / VLOOKUP match text case-insensitively, so province and
    code keys are casefolded -- Summary says "UDON THANI, NONG BUA LAM PHU"
    while Data Total says "Udon Thani, Nong Bua Lam Phu".
  * A number stored as text counts as 0 (ingest already did this).
  * IFERROR(x/y, 0) -> 0 on a zero denominator.
  * Run-rate = (actual / days_elapsed) * days_in_month, the two day counts
    taken from Summary!A1:A2 as the owner typed them.
  * "Non Tourist" AP1D200 = Mass + Migrant. Tourist is reported but excluded
    from MOU achievement.
  * GA totals are the SUM of the three segments. Data Total's own "GA" column
    is not used by any report formula (and does not always equal the sum).

Two places deliberately DO NOT mirror the sheet, each warned about by the
build rather than hidden:

  * The GA baseline is recomputed from Data Total using the pivot's own rule,
    so a Pivot Base left unrefreshed after a roster change shows up as a
    difference instead of being copied.
  * A cell whose formula deviates from its column (e.g. By Shop V33 = V32-U32
    instead of R33-J33) is computed with the column's formula.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from .ingest import Source, month_key

SEG = C.SEGMENTS


def fold(s) -> str:
    """Excel's text-match key: case-insensitive, otherwise exact."""
    return str(s).casefold() if s is not None else ""


def safe_div(num, den):
    """IFERROR(num/den, 0)."""
    num, den = np.asarray(num, float), np.asarray(den, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(den != 0, num / np.where(den != 0, den, 1), 0.0)


def report_month(src: Source) -> tuple[str, str]:
    label = str(src.control.get("month_label") or "").strip()
    return label, month_key(label)


def joined_data(src: Source) -> pd.DataFrame:
    """Data Total with the roster attributes each row looks up.

    Phase and Visibility are VLOOKUP(code, roster) in the sheet: FIRST match,
    #N/A when the shop is not on the roster. Mirrored with a left join on the
    first roster row per code.
    """
    d = src.data.copy()
    d["code_key"] = d["code"].map(fold)
    d["province_key"] = d["province"].map(fold)
    first = (src.target.assign(code_key=src.target["code"].map(fold))
             .drop_duplicates("code_key", keep="first").set_index("code_key"))
    for col in ("phase", "visibility", "visibility_size", "zone",
                "decorated_month", "mou_target", "mou_baseline"):
        d[col] = d["code_key"].map(first[col]) if col in first else None
    d["on_roster"] = d["code_key"].isin(first.index)
    for m in ("ga", "ap1d", "ap200", "m1"):
        d[m] = sum(d[f"{m}_{s}"] for s in SEG)
    d["ap200_nontourist"] = d["ap200_mass"] + d["ap200_migrant"]   # MOU AP1D200
    d["m1_nontourist"] = d["m1_mass"] + d["m1_migrant"]            # MOU M1
    return d


def baseline_rows(d: pd.DataFrame) -> pd.DataFrame:
    """The Data Total rows Pivot Base sums: Jan'26 + Feb'26, Phase = Yes*."""
    months = {fold(m) for m in C.BASELINE_MONTHS}
    phase = d["phase"].fillna("").astype(str)
    return d[d["month_label"].map(fold).isin(months)
             & phase.str.startswith(C.BASELINE_PHASE_PREFIX)]


def _metrics(frame: pd.DataFrame, days_elapsed: float, days_in_month: float
             ) -> pd.DataFrame:
    """The 29-column block, given base_*, ga_*, ap_*, mou_* already filled.

    One implementation for shops and provinces, as the sheet uses one set of
    formulas for both.
    """
    f = frame
    f["base_ga"] = f["base_mass"] + f["base_migrant"] + f["base_tourist"]
    f["ga"] = f["ga_mass"] + f["ga_migrant"] + f["ga_tourist"]
    for s in SEG:
        f[f"rr_{s}"] = f[f"ga_{s}"] / days_elapsed * days_in_month
        f[f"growth_{s}"] = f[f"rr_{s}"] - f[f"base_{s}"]
        f[f"aprr_{s}"] = f[f"ap_{s}"] / days_elapsed * days_in_month
    f["rr_ga"] = f["rr_mass"] + f["rr_migrant"] + f["rr_tourist"]
    f["growth_ga"] = f["growth_mass"] + f["growth_migrant"] + f["growth_tourist"]
    f["growth_pct"] = safe_div(f["growth_ga"], f["base_ga"])
    f["ap_nontourist"] = f["ap_mass"] + f["ap_migrant"]
    f["aprr_nontourist"] = f["aprr_mass"] + f["aprr_migrant"]
    f["ach_mou"] = safe_div(f["aprr_nontourist"], f["mou_target"])
    f["growth_mou"] = f["aprr_nontourist"] - f["mou_baseline"]
    return f


def _sums(rows: pd.DataFrame, key: str, index: pd.Index) -> pd.DataFrame:
    """SUMIFS of every segment column, aligned to `index`, 0 where absent."""
    cols = [f"ga_{s}" for s in SEG] + [f"ap200_{s}" for s in SEG]
    agg = rows.groupby(key)[cols].sum()
    return agg.reindex(index).fillna(0.0)


def build_shop(src: Source) -> pd.DataFrame:
    """One row per roster shop: the By Shop grid.

    The roster is every Target row with a Combine Code AND a Zone. A row with
    a code but no zone/province (a stub someone started typing) is on no
    By Shop or Summary row; validate warns about it.
    """
    label, _ = report_month(src)
    de = float(src.control["days_elapsed"])
    dm = float(src.control["days_in_month"])
    d = joined_data(src)

    t = src.target.copy()
    t["code_key"] = t["code"].map(fold)
    roster = t[t["zone"].ne("")].drop_duplicates("code_key", keep="first")
    out = roster[["code", "code_key", "zone", "province", "shop_name", "phase",
                  "visibility", "visibility_size", "decorated_month", "group",
                  "cluster", "rs_name", "pbh_name", "mou_flag",
                  "true_friend_code", "outlet_code", "route",
                  "target_quarter"]].reset_index(drop=True)
    idx = pd.Index(out["code_key"])

    base = baseline_rows(d)
    b = (base.groupby("code_key")[[f"ga_{s}" for s in SEG]].sum()
         / C.BASELINE_DIVISOR).reindex(idx)
    out["baseline_missing"] = b.isna().all(axis=1).to_numpy()
    for s in SEG:
        out[f"base_{s}"] = b[f"ga_{s}"].fillna(0.0).to_numpy()

    cur = d[d["month_label"].map(fold) == fold(label)]
    a = _sums(cur, "code_key", idx)
    for s in SEG:
        out[f"ga_{s}"] = a[f"ga_{s}"].to_numpy()
        out[f"ap_{s}"] = a[f"ap200_{s}"].to_numpy()

    # SUMIFS over the roster by code: a duplicated code sums, as the sheet does.
    tsum = t.groupby("code_key")[["mou_baseline", "mou_target"]].sum().reindex(idx)
    out["mou_baseline"] = tsum["mou_baseline"].fillna(0.0).to_numpy()
    out["mou_target"] = tsum["mou_target"].fillna(0.0).to_numpy()
    out["has_current_row"] = idx.isin(cur["code_key"])

    out = _metrics(out, de, dm)
    out["visibility_label"] = out["visibility"].map(C.VISIBILITY_LABELS).fillna(
        out["visibility"].replace("", "Not set"))
    return out.drop(columns="code_key")


def build_province(src: Source) -> pd.DataFrame:
    """One row per province: Summary's province grid.

    Keys differ by column, exactly as in the sheet: shop counts and MOU come
    from the roster's Province; baseline and actuals from Data Total's
    PBH Province.
    """
    label, _ = report_month(src)
    de = float(src.control["days_elapsed"])
    dm = float(src.control["days_in_month"])
    d = joined_data(src)
    t = src.target.assign(province_key=src.target["province"].map(fold))
    t = t[t["province"].ne("")]
    cur = d[d["month_label"].map(fold) == fold(label)]
    base = baseline_rows(d)

    keys = pd.Index(sorted((set(t["province_key"]) | set(cur["province_key"])
                            | set(base["province_key"])) - {""}))
    out = pd.DataFrame({"province_key": keys})

    # Label and region: the Summary row's own when it has one (that is the
    # label people read), else the roster's.
    sheet = src.province_sheet.assign(
        province_key=src.province_sheet["province"].map(fold))
    sheet = sheet.drop_duplicates("province_key").set_index("province_key")
    roster_label = t.groupby("province_key")["province"].first()
    roster_zone = t[t["zone"].ne("")].groupby("province_key")["zone"].agg(
        lambda z: z.mode().iat[0])
    data_label = d.groupby("province_key")["province"].first()
    data_region = d.groupby("province_key")["region"].agg(lambda z: z.mode().iat[0])
    out["province"] = [sheet["province"].get(k) or roster_label.get(k)
                       or data_label.get(k) for k in keys]
    out["region"] = [roster_zone.get(k) or data_region.get(k) or "" for k in keys]
    out["region_sheet"] = [sheet["region"].get(k) for k in keys]
    out["on_summary"] = keys.isin(sheet.index)

    out["n_shops"] = t.groupby("province_key").size().reindex(keys).fillna(0).to_numpy()
    b = (base.groupby("province_key")[[f"ga_{s}" for s in SEG]].sum()
         / C.BASELINE_DIVISOR).reindex(keys).fillna(0.0)
    a = _sums(cur, "province_key", keys)
    ts = t.groupby("province_key")[["mou_baseline", "mou_target"]].sum().reindex(keys).fillna(0.0)
    for s in SEG:
        out[f"base_{s}"] = b[f"ga_{s}"].to_numpy()
        out[f"ga_{s}"] = a[f"ga_{s}"].to_numpy()
        out[f"ap_{s}"] = a[f"ap200_{s}"].to_numpy()
    out["mou_baseline"] = ts["mou_baseline"].to_numpy()
    out["mou_target"] = ts["mou_target"].to_numpy()
    out = _metrics(out, de, dm)

    # Summary's own RANK only covers rows 32:62. Published instead: a rank over
    # every province that has an MOU target, by RR achievement.
    has = out["mou_target"] > 0
    out["rank_ach_mou"] = pd.NA
    out.loc[has, "rank_ach_mou"] = out.loc[has, "ach_mou"].rank(
        method="min", ascending=False).astype(int)
    return out


def build_region(province: pd.DataFrame) -> pd.DataFrame:
    """Summary rows 5-13: SUMIFS of the province grid by region, then Total.

    Ratios are recomputed from the summed parts, never summed themselves.
    """
    sum_cols = (["n_shops"]
                + [f"{p}_{s}" for p in ("base", "ga", "rr", "growth", "ap", "aprr")
                   for s in SEG]
                + ["base_ga", "ga", "rr_ga", "growth_ga", "mou_baseline",
                   "mou_target", "ap_nontourist", "aprr_nontourist", "growth_mou"])
    g = province.groupby("region")[sum_cols].sum()
    regions = list(C.REGION_ORDER) + sorted(set(g.index) - set(C.REGION_ORDER))
    out = g.reindex(regions).fillna(0.0).reset_index().rename(columns={"index": "region"})
    total = out[sum_cols].sum().to_frame().T
    total.insert(0, "region", "Total")
    out = pd.concat([out, total], ignore_index=True)
    out["growth_pct"] = safe_div(out["growth_ga"], out["base_ga"])
    out["ach_mou"] = safe_div(out["aprr_nontourist"], out["mou_target"])
    return out


def build_monthly(src: Source) -> pd.DataFrame:
    """Data Total, one row per month x shop, with roster attributes.

    The history behind the trend and visibility pages. Every month the snapshot
    holds, including shops that have since left the roster (`on_roster`).
    """
    d = joined_data(src)
    keep = (["month", "month_label", "code", "retailer_code", "s2_code",
             "shop_name", "region", "province", "zone", "on_roster", "phase",
             "visibility", "visibility_size", "decorated_month", "xl_row"]
            + [f"{m}_{s}" for m in ("ga", "ap1d", "ap200", "m1") for s in SEG]
            + ["ga", "ap1d", "ap200", "m1", "ap200_nontourist", "m1_nontourist",
               "ga_total_sheet"])
    out = d[keep].copy()
    out["visibility_label"] = out["visibility"].map(C.VISIBILITY_LABELS).fillna(
        out["visibility"].fillna("").replace("", "Not set"))
    out["decorated_month"] = pd.to_datetime(out["decorated_month"])
    return out.sort_values(["month", "region", "province", "code"]).reset_index(drop=True)
