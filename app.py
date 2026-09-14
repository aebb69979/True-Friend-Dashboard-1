"""True Friend dashboard: serves data/processed/*.parquet.

Reads ONLY parquet -- never the source .xlsx, and never imports src/. Those
parquet files are produced by `python -m src.build`, which refuses to write
unless every By Shop and Summary cell reconciles against the workbook's own
cached values. The arithmetic here is limited to summing published columns
and dividing the sums, the same way Summary's region rows do.
"""

from __future__ import annotations

import base64
import hmac
import io
import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from PIL import Image

ROOT = Path(__file__).parent
DATA = ROOT / "data" / "processed"
ASSETS = ROOT / "assets"
LOGO = ASSETS / "true_logo_transparent.png"
LOGIN_BG = ASSETS / "login_background_true_friend_1.png"

st.set_page_config(page_title="True Friend Dashboard", page_icon=Image.open(LOGO),
                   layout="wide")

# --- theme ------------------------------------------------------------------
# Chart tokens from the validated reference palette. Brand red is for chrome
# (logo, primary buttons) only: in a chart, red would read as "critical".
LIGHT = {"surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e",
         "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
         "series": ["#2a78d6", "#eb6834", "#1baf7a"],
         "pos": "#2a78d6", "neg": "#e34948", "neutral": "#c3c2b7"}
DARK = {"surface": "#1a1a19", "primary": "#ffffff", "secondary": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
        "series": ["#3987e5", "#d95926", "#199e70"],
        "pos": "#3987e5", "neg": "#e66767", "neutral": "#383835"}


def theme() -> dict:
    return DARK if st.get_option("theme.base") == "dark" else LIGHT


T = theme()
SEGMENTS = [("mass", "Mass"), ("migrant", "Migrant"), ("tourist", "Tourist")]
# Colour follows the entity, never its rank or the current filter.
VIS_ORDER = ["Decorated", "To decorate", "To decorate (round 2)"]


def style(chart):
    return (chart
            .configure_view(strokeWidth=0)
            .configure_axis(grid=True, gridColor=T["grid"], domainColor=T["axis"],
                            tickColor=T["axis"], labelColor=T["secondary"],
                            titleColor=T["secondary"], labelFontSize=11,
                            titleFontSize=11)
            .configure_legend(labelColor=T["secondary"], titleColor=T["secondary"],
                              orient="top")
            .configure_title(color=T["primary"], fontSize=13, anchor="start"))


def ratio(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


# --- access gate ------------------------------------------------------------
# One shared password, checked BEFORE any parquet is read. Streamlit renders
# server-side, so stopping here means no figure reaches the browser. A missing
# secret stops the app -- do not "fix" that with a .get() default.


@st.cache_resource
def _login_background_css() -> str:
    # The source PNG is 1.3 MB; re-encoded once per server, not per visitor.
    im = Image.open(LOGIN_BG).convert("RGB")
    im.thumbnail((1600, 900))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"""<style>
    [data-testid="stApp"] {{
        background: url("data:image/jpeg;base64,{b64}") center / cover fixed;
    }}
    [data-testid="stHeader"] {{ background: transparent; }}
    .st-key-login_card {{
        background: #ffffff; color: #0b0b0b; border-radius: 16px;
        padding: 2rem 2rem 1.5rem; margin-top: 12vh;
        box-shadow: 0 12px 40px rgba(0,0,0,.25);
    }}
    .st-key-login_card p, .st-key-login_card label {{ color: #52514e; }}
    .st-key-login_card h1 {{ color: #0b0b0b; }}
    .st-key-login_card button[kind="primaryFormSubmit"] p {{ color: #ffffff; }}
    </style>"""


def check_password() -> bool:
    if st.session_state.get("auth_ok"):
        return True
    if "app_password" not in st.secrets:
        st.error("No `app_password` in Streamlit secrets, so the app cannot let "
                 "anyone in.\n\nLocal: copy `.streamlit/secrets.toml.example` to "
                 "`.streamlit/secrets.toml` and set a value.\n\n"
                 "Deployed: App settings → Secrets.")
        return False

    st.html(_login_background_css())
    valid = submitted = False
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid, st.container(key="login_card"):
        st.image(str(LOGO), width=140)
        st.title("True Friend Dashboard")
        st.caption("Retail partner performance · enter the password to continue.")
        with st.form("login", border=False):
            st.text_input("Password", type="password", key="_pw")
            submitted = st.form_submit_button("Sign in", type="primary",
                                              width="stretch")
        if submitted:
            # compare_digest, not ==: == short-circuits and leaks by timing.
            valid = hmac.compare_digest(st.session_state["_pw"],
                                        st.secrets["app_password"])
            if not valid:
                st.error("Incorrect password.")
    if submitted and valid:
        st.session_state["auth_ok"] = True
        del st.session_state["_pw"]
        st.rerun()
    return False


if not check_password():
    st.stop()


# --- data -------------------------------------------------------------------


def data_version() -> str:
    """Cache key for load(): without it, a rebuild keeps serving old numbers."""
    return "|".join(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}"
                    for p in sorted(DATA.glob("*")))


@st.cache_data
def load(version: str):
    if not (DATA / "meta.json").exists():
        return None
    return {name: pd.read_parquet(DATA / f"{name}.parquet")
            for name in ("shop", "province", "region", "monthly")} | {
        "meta": json.loads((DATA / "meta.json").read_text())}


d = load(data_version())
if d is None:
    st.error("No processed data found. Run the build first:\n\n```\nmake build\n```")
    st.stop()

meta = d["meta"]
periods = list(meta.get("period_list") or [])
pmeta = meta.get("periods") or {}


def period_label(p: str) -> str:
    m = pmeta.get(p, {})
    stamp = pd.Timestamp(p + "-01").strftime("%b %Y")
    if m.get("is_final"):
        return f"{stamp} · Final"
    return f"{stamp} · Working (day {m.get('days_elapsed')} of {m.get('days_in_month')})"


# --- header -----------------------------------------------------------------
st.logo(str(LOGO), size="large")
h = st.columns([3, 1.4, 1.6], vertical_alignment="bottom")
h[0].title("True Friend Dashboard")
sel = h[2].selectbox("Reporting period", list(reversed(periods)), index=0,
                     format_func=period_label, key="period",
                     help="A closed month shows its -Final snapshot. The month in "
                          "progress shows the newest working copy.")
pm = pmeta.get(sel, {})
de, dm = pm.get("days_elapsed"), pm.get("days_in_month")
h[0].caption(f"{pm.get('month_label')} · data through day {de} of {dm} · run-rate "
             f"= actual ÷ {de} × {dm} · source: {pm.get('source_file')}")


def for_period(df: pd.DataFrame) -> pd.DataFrame:
    return (df[df["period"].eq(sel)]
            .drop(columns=["period", "is_final"]).reset_index(drop=True))


shop = for_period(d["shop"])
province = for_period(d["province"])
monthly = for_period(d["monthly"])
region_rows = for_period(d["region"])

regions_with_shops = [r for r in region_rows["region"]
                      if r != "Total" and region_rows.set_index("region").at[r, "n_shops"] > 0]
sel_regions = h[1].multiselect("Region", regions_with_shops, default=[],
                               placeholder=f"All regions ({len(regions_with_shops)})",
                               key="regions") or regions_with_shops

prov_v = province[province["region"].isin(sel_regions)]
shop_v = shop[shop["zone"].isin(sel_regions)].copy()
# The roster types provinces in mixed case ("CHON BURI 2", "Chon Buri 2"); Excel
# matches them case-insensitively, so one display label per province here too.
shop_v["province"] = shop_v["province"].str.title()

# Cells the build recomputed rather than copied (a stale pivot, a broken
# formula). Viewers comparing against the Excel deserve to know which.
notes = (pm.get("validation") or {}).get("notes") or {}


def rollup(frame: pd.DataFrame) -> pd.Series:
    """Sum published parts, then divide -- exactly as Summary's region rows."""
    s = frame[["n_shops", "base_ga", "ga", "rr_ga", "growth_ga", "mou_target",
               "mou_baseline", "ap_nontourist", "aprr_nontourist"]].sum()
    s["growth_pct"] = ratio(s["growth_ga"], s["base_ga"])
    s["ach_mou"] = ratio(s["aprr_nontourist"], s["mou_target"])
    return s


INT = st.column_config.NumberColumn(format="localized")
ONE = st.column_config.NumberColumn(format="localized")


def pct_col(label: str, **kw):
    if label == "GA growth":
        kw.setdefault("help", "GA run-rate vs the Jan–Feb'26 baseline. Empty "
                              "where there was no GA in Jan–Feb (no baseline).")
    return st.column_config.NumberColumn(label, format="percent", **kw)


def ach_col(label="RR vs MOU"):
    return st.column_config.ProgressColumn(label, format="percent", min_value=0,
                                           max_value=1.5, color=T["series"][0],
                                           help="AP1D200 non-tourist run-rate ÷ "
                                                "monthly MOU target. Bar tops out "
                                                "at 150%.")


def hbar(frame: pd.DataFrame, y: str, x: str, title: str, *, fmt: str = ".0%",
         target: float | None = None, diverging: bool = False,
         tooltip: list | None = None, row_px: int = 26):
    """Horizontal bars, sorted, with an optional reference rule.

    One series -> one colour. Diverging -> two poles for above/below zero.
    """
    frame = frame.dropna(subset=[x])      # undefined values get no bar
    order = frame.sort_values(x, ascending=False)[y].tolist()
    height = max(200, row_px * len(frame) + 40)
    # Pad the value axis so a label past the longest bar is never clipped.
    lo = min(0.0, float(frame[x].min()) if len(frame) else 0.0, target or 0.0)
    hi = max(0.0, float(frame[x].max()) if len(frame) else 0.0, target or 0.0)
    pad = (hi - lo) * 0.14 or 0.1
    base = alt.Chart(frame).encode(
        y=alt.Y(f"{y}:N", sort=order, title=None,
                axis=alt.Axis(labelLimit=260, ticks=False, labelOverlap=False,
                              labelFontSize=11)),
        x=alt.X(f"{x}:Q", title=None,
                scale=alt.Scale(domain=[lo - (pad if lo < 0 else 0), hi + pad],
                                nice=False),
                axis=alt.Axis(format=fmt, tickCount=5)),
        tooltip=tooltip or [alt.Tooltip(f"{y}:N"), alt.Tooltip(f"{x}:Q", format=fmt)])
    if diverging:
        color = alt.condition(alt.datum[x] >= 0, alt.value(T["pos"]),
                              alt.value(T["neg"]))
        bars = base.mark_bar(cornerRadiusEnd=4, height={"band": 0.72}).encode(color=color)
    else:
        bars = base.mark_bar(cornerRadiusEnd=4, height={"band": 0.72},
                             color=T["series"][0])
    labels = base.mark_text(align="left", dx=4, color=T["secondary"],
                            fontSize=11).encode(text=alt.Text(f"{x}:Q", format=fmt))
    layers = [bars, labels]
    if diverging:
        # Labels sit outside the bar end on whichever side the bar grows.
        text = alt.Text(f"{x}:Q", format=fmt)
        layers = [bars,
                  base.transform_filter(alt.datum[x] >= 0).mark_text(
                      align="left", dx=4, color=T["secondary"], fontSize=11).encode(text=text),
                  base.transform_filter(alt.datum[x] < 0).mark_text(
                      align="right", dx=-4, color=T["secondary"], fontSize=11).encode(text=text)]
    ref = target if target is not None else (0 if diverging else None)
    if ref is not None:
        layers.append(alt.Chart(pd.DataFrame({"v": [ref]})).mark_rule(
            color=T["axis"] if diverging else T["primary"], strokeWidth=1.5)
            .encode(x="v:Q"))
    return style(alt.layer(*layers).properties(title=title, height=height))


# --- tabs -------------------------------------------------------------------
tabs = st.tabs(["Overview", "Province", "Shop", "Trend", "Visibility"])


def render_overview() -> None:
    k = rollup(prov_v)
    c = st.columns(4)
    c[0].metric("True Friend shops", f"{k['n_shops']:,.0f}",
                help="Shops on the roster (Target Truefriend Submit 10).")
    c[1].metric("AP1D200 non-tourist · RR vs MOU", f"{k['ach_mou']:.0%}",
                delta=f"{k['aprr_nontourist'] - k['mou_target']:+,.0f} vs target "
                      f"{k['mou_target']:,.0f}",
                help=f"Run-rate {k['aprr_nontourist']:,.1f} ÷ MOU target "
                     f"{k['mou_target']:,.0f}. Actual month-to-date "
                     f"{k['ap_nontourist']:,.0f}.")
    c[2].metric("GA run-rate", f"{k['rr_ga']:,.0f}",
                delta=(f"{k['growth_pct']:+.1%} vs Jan–Feb baseline" if k["base_ga"]
                       else "no Jan–Feb baseline"),
                help=f"Baseline GA {k['base_ga']:,.1f} = (Jan'26 + Feb'26) ÷ 2.")
    c[3].metric("GA month-to-date", f"{k['ga']:,.0f}",
                help=f"Actual gross adds through day {de} of {dm}.")

    reg = no_baseline_blank(region_rows[region_rows["region"].isin(sel_regions)])
    cols = st.columns(2, gap="large")
    with cols[0]:
        st.altair_chart(hbar(reg, "region", "ach_mou",
                             "AP1D200 non-tourist run-rate vs MOU, by region",
                             target=1.0,
                             tooltip=["region",
                                      alt.Tooltip("ach_mou:Q", format=".1%", title="RR vs MOU"),
                                      alt.Tooltip("aprr_nontourist:Q", format=",.1f", title="Run-rate"),
                                      alt.Tooltip("mou_target:Q", format=",.0f", title="MOU target")]),
                        width="stretch")
        st.caption("Vertical rule = 100% of MOU.")
    with cols[1]:
        st.altair_chart(hbar(reg, "region", "growth_pct",
                             "GA run-rate growth vs Jan–Feb baseline, by region",
                             diverging=True, fmt="+.0%",
                             tooltip=["region",
                                      alt.Tooltip("growth_pct:Q", format="+.1%", title="Growth"),
                                      alt.Tooltip("rr_ga:Q", format=",.1f", title="GA run-rate"),
                                      alt.Tooltip("base_ga:Q", format=",.1f", title="Baseline GA")]),
                        width="stretch")

    st.markdown("**By region**")
    total = rollup(reg)
    total["region"] = "Total"
    table = pd.concat([reg, total.to_frame().T], ignore_index=True)
    st.dataframe(
        for_table(table)[["region", "n_shops", "base_ga", "ga", "rr_ga", "growth_pct",
               "mou_baseline", "mou_target", "ap_nontourist", "aprr_nontourist",
               "ach_mou"]],
        hide_index=True, width="stretch",
        column_config={"region": "Region", "n_shops": st.column_config.NumberColumn("Shops", format="%d"),
                       "base_ga": st.column_config.NumberColumn("Baseline GA", format="localized"),
                       "ga": st.column_config.NumberColumn("GA MTD", format="localized"),
                       "rr_ga": st.column_config.NumberColumn("GA run-rate", format="localized"),
                       "growth_pct": pct_col("GA growth"),
                       "mou_baseline": st.column_config.NumberColumn("AP1D200 baseline", format="localized"),
                       "mou_target": st.column_config.NumberColumn("MOU target", format="localized"),
                       "ap_nontourist": st.column_config.NumberColumn("AP1D200 MTD", format="localized"),
                       "aprr_nontourist": st.column_config.NumberColumn("AP1D200 run-rate", format="localized"),
                       "ach_mou": ach_col()})
    if stale := notes.get("stale_baseline"):
        st.caption("ⓘ Baselines here use the roster as it stands in this snapshot. "
                   f"The Excel Summary still counts {sum(len(v['shops']) for v in stale.values())} "
                   "shop(s) that have left the roster, because its Pivot Base was not "
                   "refreshed — so its baseline for "
                   + ", ".join(k.title() for k in stale) + " is higher.")


ROUND_1 = ("base_ga", "rr_ga", "mou_baseline", "aprr_nontourist", "growth_mou",
           "before", "after")


def for_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Round for display only, so "localized" prints 1,407.3 not 1407.27273."""
    out = frame.copy()
    for c in ROUND_1:
        if c in out:
            out[c] = pd.to_numeric(out[c], errors="coerce").round(1)
    return out


def no_baseline_blank(frame: pd.DataFrame) -> pd.DataFrame:
    """Growth % with a zero baseline is undefined. The Excel's IFERROR prints
    0%, which reads as "flat"; the dashboard prints a blank instead."""
    out = frame.copy()
    out["growth_pct"] = out["growth_pct"].where(out["base_ga"] != 0)
    return out


def render_province() -> None:
    p = no_baseline_blank(prov_v[(prov_v["n_shops"] > 0) | (prov_v["ga"] > 0)])
    p["province_label"] = p["province"].str.title()
    with_target = p[p["mou_target"] > 0]
    cols = st.columns([1, 1], gap="large")
    with cols[0]:
        st.altair_chart(hbar(with_target, "province_label", "ach_mou",
                             "AP1D200 non-tourist run-rate vs MOU", target=1.0,
                             row_px=24,
                             tooltip=[alt.Tooltip("province_label:N", title="Province"),
                                      alt.Tooltip("region:N", title="Region"),
                                      alt.Tooltip("ach_mou:Q", format=".1%", title="RR vs MOU"),
                                      alt.Tooltip("aprr_nontourist:Q", format=",.1f", title="Run-rate"),
                                      alt.Tooltip("mou_target:Q", format=",.0f", title="MOU target")]),
                        width="stretch")
    with cols[1]:
        st.altair_chart(hbar(p, "province_label", "growth_pct",
                             "GA run-rate growth vs baseline", diverging=True,
                             fmt="+.0%", row_px=24,
                             tooltip=[alt.Tooltip("province_label:N", title="Province"),
                                      alt.Tooltip("growth_pct:Q", format="+.1%", title="Growth"),
                                      alt.Tooltip("rr_ga:Q", format=",.1f", title="GA run-rate"),
                                      alt.Tooltip("base_ga:Q", format=",.1f", title="Baseline GA")]),
                        width="stretch")
    st.markdown("**By province**")
    st.dataframe(
        for_table(p).sort_values(["region", "ach_mou"], ascending=[True, False])[
            ["region", "province_label", "n_shops", "base_ga", "ga", "rr_ga",
             "growth_pct", "mou_target", "ap_nontourist", "aprr_nontourist",
             "ach_mou", "growth_mou", "rank_ach_mou"]],
        hide_index=True, width="stretch",
        column_config={"region": "Region", "province_label": "Province",
                       "n_shops": st.column_config.NumberColumn("Shops", format="%d"),
                       "base_ga": st.column_config.NumberColumn("Baseline GA", format="localized"),
                       "ga": st.column_config.NumberColumn("GA MTD", format="localized"),
                       "rr_ga": st.column_config.NumberColumn("GA run-rate", format="localized"),
                       "growth_pct": pct_col("GA growth"),
                       "mou_target": st.column_config.NumberColumn("MOU target", format="localized"),
                       "ap_nontourist": st.column_config.NumberColumn("AP1D200 MTD", format="localized"),
                       "aprr_nontourist": st.column_config.NumberColumn("AP1D200 run-rate", format="localized"),
                       "ach_mou": ach_col(),
                       "growth_mou": st.column_config.NumberColumn("RR − AP1D200 baseline", format="localized"),
                       "rank_ach_mou": st.column_config.NumberColumn("Rank", format="%d",
                                                                    help="By RR vs MOU, across all provinces with a target.")})


def render_shop() -> None:
    f = st.columns([2, 2, 2])
    provs = sorted(shop_v["province"].unique())
    sp = f[0].multiselect("Province", provs, default=[], key="s_prov",
                          placeholder=f"All ({len(provs)})") or provs
    vis = [v for v in VIS_ORDER if v in set(shop_v["visibility_label"])] + sorted(
        set(shop_v["visibility_label"]) - set(VIS_ORDER))
    sv = f[1].multiselect("Visibility", vis, default=[], key="s_vis",
                          placeholder=f"All ({len(vis)})") or vis
    q = f[2].text_input("Search shop name or code", key="s_q").strip().casefold()
    v = shop_v[shop_v["province"].isin(sp) & shop_v["visibility_label"].isin(sv)]
    if q:
        v = v[v["shop_name"].str.casefold().str.contains(q, regex=False)
              | v["code"].str.contains(q, regex=False)]
    if v.empty:
        st.info("Nothing matches the current filter.")
        return
    k = rollup(v.assign(n_shops=1))
    c = st.columns(3)
    c[0].metric("Shops shown", f"{len(v):,}")
    c[1].metric("RR vs MOU", f"{k['ach_mou']:.0%}",
                help=f"{k['aprr_nontourist']:,.1f} ÷ {k['mou_target']:,.0f}")
    c[2].metric("GA growth vs baseline",
                f"{k['growth_pct']:+.1%}" if k["base_ga"] else "—",
                help=None if k["base_ga"] else "No GA in Jan–Feb'26, so no baseline.")

    v = no_baseline_blank(v)
    below = int((v["ach_mou"] < 1).sum())
    st.caption(f"{below} of {len(v)} shops are running below their MOU target.")
    st.dataframe(
        for_table(v).sort_values("ach_mou", ascending=False)[
            ["zone", "province", "shop_name", "code", "visibility_label",
             "base_ga", "ga", "rr_ga", "growth_pct", "mou_baseline", "mou_target",
             "ap_nontourist", "aprr_nontourist", "ach_mou", "growth_mou",
             "true_friend_code"]],
        hide_index=True, width="stretch", height=min(38 + 35 * len(v), 760),
        column_config={"zone": "Region", "province": "Province",
                       "shop_name": st.column_config.TextColumn("Shop", width="medium"),
                       "code": "Combine code", "visibility_label": "Visibility",
                       "base_ga": st.column_config.NumberColumn("Baseline GA", format="localized"),
                       "ga": st.column_config.NumberColumn("GA MTD", format="localized"),
                       "rr_ga": st.column_config.NumberColumn("GA run-rate", format="localized"),
                       "growth_pct": pct_col("GA growth"),
                       "mou_baseline": st.column_config.NumberColumn("AP1D200 baseline", format="localized"),
                       "mou_target": st.column_config.NumberColumn("MOU target", format="localized"),
                       "ap_nontourist": st.column_config.NumberColumn("AP1D200 MTD", format="localized"),
                       "aprr_nontourist": st.column_config.NumberColumn("AP1D200 run-rate", format="localized"),
                       "ach_mou": ach_col(),
                       "growth_mou": st.column_config.NumberColumn("RR − baseline", format="localized"),
                       "true_friend_code": "True Friend code"})
    names = dict(zip(shop["code"], shop["shop_name"]))
    for c in (x for x in notes.get("recomputed_cells", []) if x["grid"] == "shop"):
        st.caption(f"ⓘ {names.get(c['key'], c['key'])}: {c['field'].replace('_', ' ')} "
                   f"(and the totals built on it) differs from the Excel, whose cell "
                   f"By Shop!{c['cell']} contains `{c['sheet_formula']}` instead of "
                   f"`{c['column_formula']}` like the rest of its column. Shown: "
                   f"{c['ours']:,.1f}; Excel: {c['sheet']:,.1f}.")


MEASURES = {"GA": ("ga", "Gross adds"),
            "AP1D200": ("ap200", "AP1D200 activations"),
            "M1 revenue": ("m1", "M1 revenue (THB)")}


def render_trend() -> None:
    f = st.columns([1.3, 1.2, 2.5])
    measure = f[0].segmented_control("Measure", list(MEASURES), default="GA",
                                     key="t_measure") or "GA"
    roster_only = f[1].toggle("Current roster only", value=True, key="t_roster",
                              help="Off includes shops that were in the programme "
                                   "earlier in the year and have since left.")
    m = monthly[monthly["region"].isin(sel_regions) | monthly["zone"].isin(sel_regions)]
    if roster_only:
        m = m[m["on_roster"]]
    names = ["All shops"] + sorted((m["shop_name"] + " · " + m["code"]).unique())
    pick = f[2].selectbox("Shop", names, key="t_shop")
    if pick != "All shops":
        m = m[m["code"].eq(pick.rsplit(" · ", 1)[1])]
    col, title = MEASURES[measure]
    long = (m.groupby(["month", "month_label"])[[f"{col}_{s}" for s, _ in SEGMENTS]]
             .sum().reset_index()
             .melt(id_vars=["month", "month_label"], var_name="segment",
                   value_name="value"))
    seg_names = {f"{col}_{s}": n for s, n in SEGMENTS}
    long["segment"] = long["segment"].map(seg_names)
    long["seg_order"] = long["segment"].map({n: i for i, (_, n) in enumerate(SEGMENTS)})
    current = pm.get("period")
    long["month_axis"] = long.apply(
        lambda r: f"{r.month_label} (MTD)" if r.month == current and not pm.get("is_final")
        else r.month_label, axis=1)
    order = long.drop_duplicates("month").sort_values("month")["month_axis"].tolist()
    fmt = ",.0f"
    chart = (alt.Chart(long, height=340)
             .mark_bar(stroke=T["surface"], strokeWidth=2)
             .encode(x=alt.X("month_axis:N", sort=order, title=None,
                             axis=alt.Axis(labelAngle=0)),
                     y=alt.Y("sum(value):Q", title=None, stack="zero",
                             axis=alt.Axis(format=",.0f")),
                     color=alt.Color("segment:N", title=None,
                                     scale=alt.Scale(domain=[n for _, n in SEGMENTS],
                                                     range=T["series"])),
                     order=alt.Order("seg_order:Q"),
                     tooltip=[alt.Tooltip("month_label:N", title="Month"),
                              alt.Tooltip("segment:N", title="Segment"),
                              alt.Tooltip("value:Q", format=fmt, title=title)])
             .properties(title=f"{title} by month, stacked by segment"))
    totals = (long.groupby("month_axis", as_index=False)["value"].sum())
    labels = (alt.Chart(totals).mark_text(dy=-6, color=T["secondary"], fontSize=11)
              .encode(x=alt.X("month_axis:N", sort=order), y="value:Q",
                      text=alt.Text("value:Q", format=",.0f")))
    st.altair_chart(style(alt.layer(chart, labels)), width="stretch")
    if not pm.get("is_final"):
        st.caption(f"{pm.get('month_label')} is month-to-date through day {de} of {dm}; "
                   f"not comparable with complete months.")
    with st.expander("Table view"):
        wide = long.pivot_table(index="segment", columns="month_axis", values="value",
                                aggfunc="sum").reindex(columns=order)
        wide.loc["Total"] = wide.sum()
        st.dataframe(wide, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="localized")
                                    for c in wide.columns})


def render_visibility() -> None:
    """Does shop decoration (visibility) move AP1D200?

    Mirrors the workbook's ตกแต่งแล้ว pivots from the monthly table. Only
    complete months are compared: the report month is partial in every snapshot.
    """
    current = pm.get("period")
    m = monthly[monthly["on_roster"] & monthly["zone"].isin(sel_regions)
                & monthly["month"].lt(current)]
    if m.empty:
        st.info("No complete months for the current selection.")
        return
    groups = [g for g in VIS_ORDER if g in set(m["visibility_label"])]
    avg = (m[m["visibility_label"].isin(groups)]
           .groupby(["month", "month_label", "visibility_label"])
           .agg(total=("ap200_nontourist", "sum"), shops=("code", "nunique"))
           .reset_index())
    avg["per_shop"] = avg["total"] / avg["shops"]
    order = avg.drop_duplicates("month").sort_values("month")["month_label"].tolist()
    scale = alt.Scale(domain=VIS_ORDER, range=T["series"])
    base = alt.Chart(avg).encode(
        x=alt.X("month_label:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("per_shop:Q", title=None),
        color=alt.Color("visibility_label:N", title=None, scale=scale),
        tooltip=[alt.Tooltip("month_label:N", title="Month"),
                 alt.Tooltip("visibility_label:N", title="Visibility"),
                 alt.Tooltip("per_shop:Q", format=",.1f", title="Per shop"),
                 alt.Tooltip("shops:Q", title="Shops")])
    lines = base.mark_line(strokeWidth=2)
    points = base.mark_point(filled=True, size=64, stroke=T["surface"], strokeWidth=2)
    st.altair_chart(style(alt.layer(lines, points).properties(
        height=340, title="AP1D200 non-tourist per shop, by visibility status "
                          "(complete months, current roster)")), width="stretch")
    st.caption("Groups are today's visibility status applied to every month, so a "
               "shop decorated in August counts as “Decorated” in January too. The "
               "table below separates before and after for each decorated shop.")

    dec = shop_v[shop_v["visibility_label"].eq("Decorated")][
        ["code", "shop_name", "province", "visibility_size", "decorated_month"]].copy()
    if dec.empty:
        return
    mm = m.set_index("code")
    rows = []
    for r in dec.itertuples():
        hist = mm.loc[[r.code]] if r.code in mm.index else pd.DataFrame()
        when = pd.Timestamp(r.decorated_month).strftime("%Y-%m") if pd.notna(r.decorated_month) else None
        before = hist[hist["month"] < when]["ap200_nontourist"] if when else pd.Series(dtype=float)
        after = hist[hist["month"] >= when]["ap200_nontourist"] if when else pd.Series(dtype=float)
        rows.append({"shop_name": r.shop_name, "province": r.province,
                     "size": r.visibility_size,
                     "decorated": pd.Timestamp(r.decorated_month).strftime("%b %Y")
                     if when else "not recorded",
                     "before": before.mean() if len(before) else None,
                     "after": after.mean() if len(after) else None,
                     "n_after": len(after)})
    t = pd.DataFrame(rows)
    t["change"] = (t["after"] - t["before"]) / t["before"]
    st.markdown("**Decorated shops · monthly AP1D200 non-tourist, before vs since decoration**")
    st.dataframe(for_table(t).sort_values("change", ascending=False, na_position="last"),
                 hide_index=True, width="stretch",
                 column_config={"shop_name": "Shop", "province": "Province",
                                "size": "Size", "decorated": "Decorated",
                                "before": st.column_config.NumberColumn("Avg / month before", format="localized"),
                                "after": st.column_config.NumberColumn("Avg / month since", format="localized"),
                                "n_after": st.column_config.NumberColumn("Months since", format="%d"),
                                "change": pct_col("Change")})
    st.caption("“Since” counts the decoration month itself and complete months after "
               "it. Shops decorated in the report month have no complete month yet.")


with tabs[0]:
    render_overview()
with tabs[1]:
    render_province()
with tabs[2]:
    render_shop()
with tabs[3]:
    render_trend()
with tabs[4]:
    render_visibility()
