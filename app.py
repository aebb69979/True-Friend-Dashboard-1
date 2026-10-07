"""True Friend dashboard: serves data/processed/*.parquet.

Reads ONLY parquet -- never the source .xlsx, and never imports src/. Those
parquet files are produced by `python -m src.build`, which refuses to write
unless every By Shop and Summary cell reconciles against the workbook's own
cached values. The arithmetic here is limited to summing published columns
and dividing the sums, the same way Summary's region rows do.

Bilingual, Thai by default. The rule: NAMES stay English in both languages --
tabs, column headers, metric and chart titles, control labels, legend and
tooltip fields, label values (Mass, Decorated, Total, Sep'26) -- so a Thai and
an English viewer point at the same words. Only explanation goes through
L(en, th): captions, notes, help text, comparison lines and messages.
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

# --- language ---------------------------------------------------------------
# Held in session_state["lang"], not in a widget's own key: the login screen
# and the dashboard each draw their own picker, and Streamlit discards a
# widget's state on any run where that widget is not drawn.
LANGS = ["TH", "EN"]
LANG = st.session_state.setdefault("lang", "TH")


def L(en: str, th: str) -> str:
    return th if LANG == "TH" else en


def lang_picker(container, key: str) -> None:
    def _apply():
        st.session_state["lang"] = st.session_state[key]
    container.selectbox("Language", LANGS, index=LANGS.index(LANG),
                        key=key, on_change=_apply, label_visibility="collapsed")


EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
             "Nov", "Dec"]


def month_short(key: str) -> str:
    """'2026-09' -> "Sep'26", as the workbook labels months."""
    y, m = int(key[:4]), int(key[5:7])
    return f"{EN_MONTHS[m - 1]}'{y % 100:02d}"


def month_long(key: str) -> str:
    """'2026-09' -> 'Sep 2026'."""
    y, m = int(key[:4]), int(key[5:7])
    return f"{EN_MONTHS[m - 1]} {y}"


# --- theme ------------------------------------------------------------------
# Chart tokens from the validated reference palette. Brand red is for chrome
# (logo, primary buttons) only: in a chart, red would read as "critical".
LIGHT = {"surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e",
         "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
         "series": ["#2a78d6", "#eb6834", "#1baf7a"],
         "pos": "#2a78d6", "neg": "#e34948", "neutral": "#c3c2b7",
         "page": "#eef1f5", "card": "#ffffff", "card_border": "#e2e5ea",
         "stripe": "#f4f4f1", "field": "#f0f2f6"}
DARK = {"surface": "#1a1a19", "primary": "#ffffff", "secondary": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
        "series": ["#3987e5", "#d95926", "#199e70"],
        "pos": "#3987e5", "neg": "#e66767", "neutral": "#383835",
        "page": "#0e0e0d", "card": "#1a1a19", "card_border": "#2c2c2a",
        "stripe": "#222221", "field": "#262730"}


def theme() -> dict:
    return DARK if st.get_option("theme.base") == "dark" else LIGHT


T = theme()
SEGMENTS = [("mass", "Mass"),
            ("migrant", "Migrant"),
            ("tourist", "Tourist")]
# Colour follows the entity, never its rank or the current filter. The build
# publishes English visibility keys; these are only how they are printed.
VIS_ORDER = ["Decorated", "To decorate", "To decorate (round 2)"]
VIS_LABEL = {"Decorated": "Decorated",
             "To decorate": "To decorate",
             "To decorate (round 2)": "To decorate (round 2)",
             "Not decorating": "Not decorating",
             "Not set": "Not set"}


def vis_label(key: str) -> str:
    return VIS_LABEL.get(key, key)


def style(chart):
    return (chart
            .configure_view(strokeWidth=0)
            .configure_axis(grid=True, gridColor=T["grid"], domainColor=T["axis"],
                            tickColor=T["axis"], labelColor=T["secondary"],
                            titleColor=T["secondary"], labelFontSize=11,
                            titleFontSize=11)
            .configure_legend(labelColor=T["secondary"], titleColor=T["secondary"],
                              orient="top", labelLimit=320)
            .configure_title(color=T["primary"], fontSize=13, anchor="start"))


def card(name: str, *, stretch: bool = False):
    """A white panel around one chart or table (styled by page_css).

    stretch=True for cards sharing a row, so their bottoms line up."""
    return st.container(key=f"card_{name}", height="stretch" if stretch else "content")


def zebra(frame: pd.DataFrame, bands: list[bool] | None = None):
    """Alternate row shading for st.dataframe (or per group, via bands).

    The grid draws on a canvas, so CSS cannot reach its rows; a Styler is the
    only hook. Text cells then print the Styler's display string (hence
    na_rep, or a missing value would read "nan"); number cells keep their
    column_config format, which the grid prefers over the Styler's.
    Every numeric column therefore needs a format in column_config.
    """
    bands = bands if bands is not None else [i % 2 == 1 for i in range(len(frame))]
    shade = [f"background-color: {T['stripe']}" if b else "" for b in bands]
    return frame.style.apply(lambda _: shade, axis=0).format(na_rep="")


def csv_bytes(frame: pd.DataFrame, column_config: dict) -> bytes:
    """The table as Excel will open it: on-screen headers, percents as 22.27
    under "GA growth (%)", blanks for missing values. utf-8-sig writes a BOM,
    without which Excel reads the file as ANSI and garbles every Thai name."""
    out = frame.reset_index() if frame.index.name else frame.copy()
    names = {}
    for c in out.columns:
        cfg = column_config.get(c, c)
        if isinstance(cfg, str):
            names[c] = cfg
            continue
        names[c] = cfg.get("label") or c
        if (cfg.get("type_config") or {}).get("format") == "percent":
            out[c] = (pd.to_numeric(out[c], errors="coerce") * 100).round(2)
            names[c] += " (%)"
        # Counts print as 122, not 122.0 (NaN in a column forced it to float).
        v = pd.to_numeric(out[c], errors="coerce")
        if v.notna().any() and v.notna().sum() == out[c].notna().sum() \
                and (v.dropna() % 1 == 0).all():
            out[c] = v.astype("Int64")
    return out.rename(columns=names).to_csv(index=False).encode("utf-8-sig")


def table(name: str, frame: pd.DataFrame, column_config: dict, *,
          group: str | None = None, **kw) -> None:
    """A striped st.dataframe with a CSV download of exactly what it shows.

    group: a region column to group rows by, in the header filter's order.
    Each region's name prints on its first row only and the shading alternates
    per region, not per row. The CSV keeps the name on every row, so it still
    filters and pivots in Excel.
    """
    shown, bands = frame, None
    if group:
        rank = {r: i for i, r in enumerate(regions_with_shops)}
        frame = frame.sort_values(group, key=lambda c: c.map(rank), kind="stable")
        starts = frame[group].ne(frame[group].shift())
        bands = (starts.cumsum() % 2 == 0).tolist()
        shown = frame.assign(**{group: frame[group].where(starts, "")})
    st.dataframe(zebra(shown, bands), width="stretch", column_config=column_config, **kw)
    st.download_button("Download CSV", csv_bytes(frame, column_config),
                       file_name=f"true_friend_{name}_{sel}.csv", mime="text/csv",
                       icon=":material/download:", key=f"dl_{name}", on_click="ignore")


def table_header(title: str, key: str, *, grouped: bool = False) -> bool:
    """Bold table title with a "Group by region" tick box on the right."""
    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"**{title}**")
        st.space("stretch")
        return st.checkbox("Group by region", value=grouped, key=key)


def page_css() -> str:
    return f"""<style>
    [data-testid="stApp"], [data-testid="stHeader"] {{ background: {T["page"]}; }}
    [class*="st-key-card_"], [data-testid="stMetric"] {{
        background: {T["card"]}; border: 1px solid {T["card_border"]};
        border-radius: 12px; box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
    }}
    [class*="st-key-card_"] {{ padding: 1.1rem 1.25rem 1rem; }}
    [data-testid="stMetric"] {{ padding: .9rem 1.25rem; }}
    /* Inputs on the grey page get a white fill; inside a card they keep the
       theme's default fill, which already contrasts with white. */
    [data-testid="stMain"] :is([data-testid="stSelectbox"], [data-testid="stMultiSelect"]) > div > div,
    [data-testid="stMain"] [data-testid="stTextInputRootElement"] {{ background: {T["card"]}; }}
    [class*="st-key-card_"] :is([data-testid="stSelectbox"], [data-testid="stMultiSelect"]) > div > div,
    [class*="st-key-card_"] [data-testid="stTextInputRootElement"] {{ background: {T["field"]}; }}
    </style>"""


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
    /* :has() ties the wallpaper to the login card itself. Streamlit can keep a
       CSS-only st.html block alive into the next run, so a plain stApp rule
       would linger behind the dashboard after sign-in; this one switches off
       the instant the card leaves the page. */
    [data-testid="stApp"]:has(.st-key-login_card) {{
        background: url("data:image/jpeg;base64,{b64}") center / cover fixed;
    }}
    .st-key-login_card {{
        background: #ffffff; color: #0b0b0b; border-radius: 16px;
        padding: 2rem 2rem 1.5rem; margin-top: 10vh;
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
        st.error(L("No `app_password` in Streamlit secrets, so the app cannot let "
                   "anyone in.\n\nLocal: copy `.streamlit/secrets.toml.example` to "
                   "`.streamlit/secrets.toml` and set a value.\n\n"
                   "Deployed: App settings → Secrets.",
                   "ไม่พบ `app_password` ใน Streamlit secrets จึงยังเข้าใช้งานไม่ได้\n\n"
                   "เครื่องตัวเอง: คัดลอก `.streamlit/secrets.toml.example` เป็น "
                   "`.streamlit/secrets.toml` แล้วตั้งรหัสผ่าน\n\n"
                   "บน Streamlit Cloud: App settings → Secrets"))
        return False

    # The login screen lives in one placeholder, emptied BEFORE st.rerun(): a
    # rerun keeps showing the previous run's elements until new ones replace
    # them, and the first authenticated run loads parquet before drawing
    # anything. Emptying removes the card at once, and the wallpaper rule is
    # keyed to the card (see _login_background_css).
    gate = st.empty()
    valid = submitted = False
    with gate.container():
        st.html(_login_background_css())
        _, lang_col = st.columns([6, 1])
        lang_picker(lang_col, "lang_login")
        _, mid, _ = st.columns([1, 1.1, 1])
        with mid, st.container(key="login_card"):
            st.title("True Friend Dashboard")
            st.caption(L("Retail partner performance · enter the password to continue.",
                         "ผลการดำเนินงานร้านค้าพันธมิตร · กรอกรหัสผ่านเพื่อเข้าใช้งาน"))
            with st.form("login", border=False):
                st.text_input("Password", type="password", key="_pw")
                submitted = st.form_submit_button("Sign in",
                                                  type="primary", width="stretch")
            if submitted:
                # compare_digest, not ==: == short-circuits and leaks by timing.
                valid = hmac.compare_digest(st.session_state["_pw"],
                                            st.secrets["app_password"])
                if not valid:
                    st.error(L("Incorrect password.", "รหัสผ่านไม่ถูกต้อง"))
    if submitted and valid:
        gate.empty()
        st.session_state["auth_ok"] = True
        del st.session_state["_pw"]
        st.rerun()
    return False


# The logo sits in the top-left header on every screen, login included.
st.logo(str(LOGO), size="large")

if not check_password():
    st.stop()
st.html(page_css())


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
    st.error(L("No processed data found. Run the build first:",
               "ไม่พบข้อมูลที่ประมวลผลแล้ว ให้รัน build ก่อน:") + "\n\n```\nmake build\n```")
    st.stop()

meta = d["meta"]
periods = list(meta.get("period_list") or [])
pmeta = meta.get("periods") or {}


def period_label(p: str) -> str:
    m = pmeta.get(p, {})
    if m.get("is_final"):
        return f"{month_long(p)} · {L('Final', 'ปิดยอดแล้ว')}"
    de_, dm_ = m.get("days_elapsed"), m.get("days_in_month")
    return f"{month_long(p)} · " + L(f"Working (day {de_} of {dm_})",
                                     f"ระหว่างเดือน (วันที่ {de_} จาก {dm_})")


# --- header -----------------------------------------------------------------
h = st.columns([3, 1.3, 1.6, 0.6], vertical_alignment="bottom")
h[0].title("True Friend Dashboard")
sel = h[2].selectbox("Reporting period", list(reversed(periods)),
                     index=0, format_func=period_label, key="period",
                     help=L("A closed month shows its -Final snapshot. The month in "
                            "progress shows the newest working copy.",
                            "เดือนที่ปิดยอดแล้วแสดงไฟล์ -Final ส่วนเดือนปัจจุบันแสดงไฟล์ล่าสุด"))
lang_picker(h[3], "lang_main")
pm = pmeta.get(sel, {})
de, dm = pm.get("days_elapsed"), pm.get("days_in_month")
h[0].caption(L(f"{month_short(sel)} · data through day {de} of {dm} · run-rate "
               f"= actual ÷ {de} × {dm} · source: {pm.get('source_file')}",
               f"{month_short(sel)} · ข้อมูลถึงวันที่ {de} จาก {dm} · RR "
               f"= ยอดจริง ÷ {de} × {dm} · ไฟล์ต้นทาง: {pm.get('source_file')}"))


def for_period(df: pd.DataFrame) -> pd.DataFrame:
    return (df[df["period"].eq(sel)]
            .drop(columns=["period", "is_final"]).reset_index(drop=True))


shop = for_period(d["shop"])
province = for_period(d["province"])
monthly = for_period(d["monthly"])
region_rows = for_period(d["region"])

regions_with_shops = [r for r in region_rows["region"]
                      if r != "Total" and region_rows.set_index("region").at[r, "n_shops"] > 0]
sel_regions = h[1].multiselect(
    "Region", regions_with_shops, default=[], key="regions",
    placeholder=f"All regions ({len(regions_with_shops)})") or regions_with_shops

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


# --- column vocabulary ------------------------------------------------------
# One label per published column, so every table names a figure the same way.


def num(label: str, fmt: str = "localized", **kw):
    return st.column_config.NumberColumn(label, format=fmt, **kw)


def growth_col():
    return st.column_config.NumberColumn(
        "GA growth", format="percent",
        help=L("GA run-rate vs the Jan–Feb'26 baseline. Empty where there was no "
               "GA in Jan–Feb (no baseline).",
               "GA run-rate เทียบฐาน ม.ค.–ก.พ. 69 · ว่างไว้หากไม่มียอด GA ใน ม.ค.–ก.พ. "
               "(ไม่มีฐาน)"))


def ach_col():
    return st.column_config.ProgressColumn(
        "RR vs MOU", format="percent", min_value=0,
        max_value=1.5, color=T["series"][0],
        help=L("AP1D200 non-tourist run-rate ÷ monthly MOU target. Bar tops out "
               "at 150%.",
               "AP1D200 run-rate (ไม่รวม Tourist) ÷ MOU target รายเดือน · แถบเต็มที่ 150%"))


def common_columns() -> dict:
    return {
        "region": "Region", "zone": "Region",
        "province": "Province", "province_label": "Province",
        "n_shops": num("Shops", "%d"),
        "base_ga": num("Baseline GA"),
        "ga": num("GA MTD"),
        "rr_ga": num("GA run-rate"),
        "growth_pct": growth_col(),
        "mou_baseline": num("AP1D200 baseline"),
        "mou_target": num("MOU target"),
        "ap_nontourist": num("AP1D200 MTD"),
        "aprr_nontourist": num("AP1D200 run-rate"),
        "ach_mou": ach_col(),
    }


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
        # Labels sit outside the bar end on whichever side the bar grows.
        text = alt.Text(f"{x}:Q", format=fmt)
        layers = [bars,
                  base.transform_filter(alt.datum[x] >= 0).mark_text(
                      align="left", dx=4, color=T["secondary"], fontSize=11).encode(text=text),
                  base.transform_filter(alt.datum[x] < 0).mark_text(
                      align="right", dx=-4, color=T["secondary"], fontSize=11).encode(text=text)]
    else:
        bars = base.mark_bar(cornerRadiusEnd=4, height={"band": 0.72},
                             color=T["series"][0])
        layers = [bars, base.mark_text(align="left", dx=4, color=T["secondary"],
                                       fontSize=11).encode(text=alt.Text(f"{x}:Q", format=fmt))]
    ref = target if target is not None else (0 if diverging else None)
    if ref is not None:
        layers.append(alt.Chart(pd.DataFrame({"v": [ref]})).mark_rule(
            color=T["axis"] if diverging else T["primary"], strokeWidth=1.5)
            .encode(x="v:Q"))
    return style(alt.layer(*layers).properties(title=title, height=height))


def tip(field: str, title: str, fmt: str | None = None):
    return alt.Tooltip(field, title=title, **({"format": fmt} if fmt else {}))


# --- tabs -------------------------------------------------------------------
tabs = st.tabs(["Overview", "Province",
                "Shop", "Trend",
                "Visibility"])


def render_overview() -> None:
    k = rollup(prov_v)
    c = st.columns(4)
    c[0].metric("True Friend shops", f"{k['n_shops']:,.0f}",
                help=L("Shops on the roster (Target Truefriend Submit 10).",
                       "ร้านค้าในรายชื่อ (Target Truefriend Submit 10)"), height="stretch")
    gap, mou = k["aprr_nontourist"] - k["mou_target"], k["mou_target"]
    c[1].metric("AP1D200 non-tourist · RR vs MOU",
                f"{k['ach_mou']:.0%}",
                delta=L(f"{gap:+,.0f} vs target {mou:,.0f}", f"{gap:+,.0f} เทียบเป้า {mou:,.0f}"),
                help=L(f"Run-rate {k['aprr_nontourist']:,.1f} ÷ MOU target {mou:,.0f}. "
                       f"Actual month-to-date {k['ap_nontourist']:,.0f}.",
                       f"run-rate {k['aprr_nontourist']:,.1f} ÷ MOU target {mou:,.0f} · "
                       f"ยอดจริงสะสม {k['ap_nontourist']:,.0f}"), height="stretch")
    c[2].metric("GA run-rate", f"{k['rr_ga']:,.0f}",
                delta=(L(f"{k['growth_pct']:+.1%} vs Jan–Feb baseline",
                         f"{k['growth_pct']:+.1%} เทียบฐาน ม.ค.–ก.พ.") if k["base_ga"]
                       else L("no Jan–Feb baseline", "ไม่มีฐาน ม.ค.–ก.พ.")),
                help=L(f"Baseline GA {k['base_ga']:,.1f} = (Jan'26 + Feb'26) ÷ 2.",
                       f"Baseline GA {k['base_ga']:,.1f} = (ม.ค. 69 + ก.พ. 69) ÷ 2"), height="stretch")
    c[3].metric("GA month-to-date", f"{k['ga']:,.0f}",
                help=L(f"Actual gross adds through day {de} of {dm}.",
                       f"ยอด GA จริงถึงวันที่ {de} จาก {dm}"), height="stretch")

    reg = no_baseline_blank(region_rows[region_rows["region"].isin(sel_regions)])
    cols = st.columns(2, gap="large")
    with cols[0], card("ov_mou", stretch=True):
        st.altair_chart(hbar(reg, "region", "ach_mou",
                             "AP1D200 non-tourist run-rate vs MOU, by region",
                             target=1.0,
                             tooltip=[tip("region:N", "Region"),
                                      tip("ach_mou:Q", "RR vs MOU", ".1%"),
                                      tip("aprr_nontourist:Q", "Run-rate", ",.1f"),
                                      tip("mou_target:Q", "MOU target", ",.0f")]),
                        width="stretch")
        st.caption(L("Vertical rule = 100% of MOU.", "เส้นแนวตั้ง = 100% ของ MOU"))
    with cols[1], card("ov_growth", stretch=True):
        st.altair_chart(hbar(reg, "region", "growth_pct",
                             "GA run-rate growth vs Jan–Feb baseline, by region",
                             diverging=True, fmt="+.0%",
                             tooltip=[tip("region:N", "Region"),
                                      tip("growth_pct:Q", "Growth", "+.1%"),
                                      tip("rr_ga:Q", "GA run-rate", ",.1f"),
                                      tip("base_ga:Q", "Baseline GA", ",.1f")]),
                        width="stretch")

    total = rollup(reg)
    total["region"] = "Total"
    by_region = pd.concat([reg, total.to_frame().T], ignore_index=True)
    with card("ov_table"):
        st.markdown("**By region**")
        table("region",
              for_table(by_region)[["region", "n_shops", "base_ga", "ga", "rr_ga",
                                    "growth_pct", "mou_baseline", "mou_target",
                                    "ap_nontourist", "aprr_nontourist", "ach_mou"]],
              common_columns(), hide_index=True)
        stale_note()


def stale_note() -> None:
    if stale := notes.get("stale_baseline"):
        n = sum(len(v["shops"]) for v in stale.values())
        provs = ", ".join(k_.title() for k_ in stale)
        st.caption(L(f"ⓘ Baselines here use the roster as it stands in this snapshot. "
                     f"The Excel Summary still counts {n} shop(s) that have left the "
                     f"roster, because its Pivot Base was not refreshed — so its "
                     f"baseline for {provs} is higher.",
                     f"ⓘ ฐานในหน้านี้คำนวณจากรายชื่อร้านปัจจุบัน แต่ Summary ใน Excel "
                     f"ยังนับร้านที่ออกจากโครงการแล้ว {n} ร้าน เพราะ Pivot Base ยังไม่ได้ "
                     f"Refresh ฐานของ {provs} ใน Excel จึงสูงกว่า"))


def render_province() -> None:
    p = no_baseline_blank(prov_v[(prov_v["n_shops"] > 0) | (prov_v["ga"] > 0)])
    p["province_label"] = p["province"].str.title()
    with_target = p[p["mou_target"] > 0]
    cols = st.columns([1, 1], gap="large")
    with cols[0], card("pr_mou", stretch=True):
        st.altair_chart(hbar(with_target, "province_label", "ach_mou",
                             "AP1D200 non-tourist run-rate vs MOU",
                             target=1.0, row_px=24,
                             tooltip=[tip("province_label:N", "Province"),
                                      tip("region:N", "Region"),
                                      tip("ach_mou:Q", "RR vs MOU", ".1%"),
                                      tip("aprr_nontourist:Q", "Run-rate", ",.1f"),
                                      tip("mou_target:Q", "MOU target", ",.0f")]),
                        width="stretch")
    with cols[1], card("pr_growth", stretch=True):
        st.altair_chart(hbar(p, "province_label", "growth_pct",
                             "GA run-rate growth vs baseline",
                             diverging=True, fmt="+.0%", row_px=24,
                             tooltip=[tip("province_label:N", "Province"),
                                      tip("growth_pct:Q", "Growth", "+.1%"),
                                      tip("rr_ga:Q", "GA run-rate", ",.1f"),
                                      tip("base_ga:Q", "Baseline GA", ",.1f")]),
                        width="stretch")
    with card("pr_table"):
        grouped = table_header("By province", "pr_group", grouped=True)
        table(
            "province",
            for_table(p).sort_values("ach_mou", ascending=False)[
                ["region", "province_label", "n_shops", "base_ga", "ga", "rr_ga",
                 "growth_pct", "mou_target", "ap_nontourist", "aprr_nontourist",
                 "ach_mou", "growth_mou", "rank_ach_mou"]],
            common_columns() | {
                "growth_mou": num("RR − AP1D200 baseline"),
                "rank_ach_mou": num("Rank", "%d",
                                    help=L("By RR vs MOU, across all provinces with a target.",
                                           "เรียงตาม RR vs MOU ทุกจังหวัดที่มี MOU target"))},
            group="region" if grouped else None, hide_index=True)


FIELD_NAMES = {"growth_migrant": "migrant growth",
               "growth_mass": "mass growth",
               "growth_tourist": "tourist growth",
               "ap_tourist": "AP1D200 tourist"}


def render_shop() -> None:
    f = st.columns([2, 2, 2])
    provs = sorted(shop_v["province"].unique())
    sp = f[0].multiselect("Province", provs, default=[], key="s_prov",
                          placeholder=f"All ({len(provs)})") or provs
    vis = [v for v in VIS_ORDER if v in set(shop_v["visibility_label"])] + sorted(
        set(shop_v["visibility_label"]) - set(VIS_ORDER))
    sv = f[1].multiselect("Visibility", vis, default=[], key="s_vis",
                          format_func=vis_label,
                          placeholder=f"All ({len(vis)})") or vis
    q = f[2].text_input("Search shop name or code",
                        key="s_q").strip().casefold()
    v = shop_v[shop_v["province"].isin(sp) & shop_v["visibility_label"].isin(sv)]
    if q:
        v = v[v["shop_name"].str.casefold().str.contains(q, regex=False)
              | v["code"].str.contains(q, regex=False)]
    if v.empty:
        st.info(L("Nothing matches the current filter.", "ไม่พบข้อมูลตามตัวกรอง"))
        return
    k = rollup(v.assign(n_shops=1))
    c = st.columns(3)
    c[0].metric("Shops shown", f"{len(v):,}", height="stretch")
    c[1].metric("RR vs MOU", f"{k['ach_mou']:.0%}",
                help=f"{k['aprr_nontourist']:,.1f} ÷ {k['mou_target']:,.0f}", height="stretch")
    c[2].metric("GA growth vs baseline",
                f"{k['growth_pct']:+.1%}" if k["base_ga"] else "—",
                help=None if k["base_ga"] else L("No GA in Jan–Feb'26, so no baseline.",
                                                  "ไม่มียอด GA ใน ม.ค.–ก.พ. 69 จึงไม่มีฐาน"), height="stretch")

    v = no_baseline_blank(v)
    v["visibility_display"] = v["visibility_label"].map(vis_label)
    with card("sh_table"):
        grouped = table_header("By shop", "sh_group")
        below = int((v["ach_mou"] < 1).sum())
        st.caption(L(f"{below} of {len(v)} shops are running below their MOU target.",
                     f"{below} จาก {len(v)} ร้าน ยังต่ำกว่าเป้า MOU"))
        table(
            "shop",
            for_table(v).sort_values("ach_mou", ascending=False)[
                ["zone", "province", "shop_name", "code", "visibility_display",
                 "base_ga", "ga", "rr_ga", "growth_pct", "mou_baseline", "mou_target",
                 "ap_nontourist", "aprr_nontourist", "ach_mou", "growth_mou",
                 "true_friend_code"]],
            common_columns() | {
                "shop_name": st.column_config.TextColumn("Shop", width="medium"),
                "code": "Combine code",
                "visibility_display": "Visibility",
                "growth_mou": num("RR − baseline"),
                "true_friend_code": "True Friend code"},
            group="zone" if grouped else None,
            hide_index=True, height=min(38 + 35 * len(v), 760))
        names = dict(zip(shop["code"], shop["shop_name"]))
        for c_ in (x for x in notes.get("recomputed_cells", []) if x["grid"] == "shop"):
            field = FIELD_NAMES.get(c_["field"], c_["field"])
            st.caption(L(f"ⓘ {names.get(c_['key'], c_['key'])}: {field} (and the totals "
                         f"built on it) differs from the Excel, whose cell By Shop!{c_['cell']} "
                         f"contains `{c_['sheet_formula']}` instead of `{c_['column_formula']}` "
                         f"like the rest of its column. Shown: {c_['ours']:,.1f}; "
                         f"Excel: {c_['sheet']:,.1f}.",
                         f"ⓘ {names.get(c_['key'], c_['key'])}: {field} (และยอดรวมที่คำนวณจากค่านี้) "
                         f"ต่างจาก Excel เพราะเซลล์ By Shop!{c_['cell']} ใน Excel เป็น "
                         f"`{c_['sheet_formula']}` แทนที่จะเป็น `{c_['column_formula']}` "
                         f"เหมือนแถวอื่นในคอลัมน์ · แดชบอร์ด: {c_['ours']:,.1f} · "
                         f"Excel: {c_['sheet']:,.1f}"))


MEASURES = {"GA": ("ga", "Gross adds"),
            "AP1D200": ("ap200", "AP1D200 activations"),
            "M1": ("m1", "M1 revenue (THB)")}
ALL_SHOPS = "__all__"


def render_trend() -> None:
    with card("tr_chart"):
        _trend()


def _trend() -> None:
    f = st.columns([1.4, 1.3, 2.4])
    measure = f[0].segmented_control(
        "Measure", list(MEASURES), default="GA", key="t_measure",
        format_func=lambda k_: "M1 revenue" if k_ == "M1" else k_) or "GA"
    roster_only = f[1].toggle("Current roster only",
                              value=True, key="t_roster",
                              help=L("Off includes shops that were in the programme "
                                     "earlier in the year and have since left.",
                                     "ปิดเพื่อรวมร้านที่เคยอยู่ในโครงการช่วงต้นปีแต่ออกไปแล้ว"))
    m = monthly[monthly["region"].isin(sel_regions) | monthly["zone"].isin(sel_regions)]
    if roster_only:
        m = m[m["on_roster"]]
    shops = dict(sorted(zip(m["code"], m["shop_name"]), key=lambda kv: kv[1]))
    pick = f[2].selectbox("Shop", [ALL_SHOPS] + list(shops), key="t_shop",
                          format_func=lambda k_: "All shops" if k_ == ALL_SHOPS
                          else f"{shops.get(k_, k_)} · {k_}")
    if pick != ALL_SHOPS:
        m = m[m["code"].eq(pick)]
    col, title = MEASURES[measure]
    long = (m.groupby("month")[[f"{col}_{s}" for s, _ in SEGMENTS]]
             .sum().reset_index()
             .melt(id_vars="month", var_name="segment", value_name="value"))
    long["segment"] = long["segment"].map({f"{col}_{s}": n for s, n in SEGMENTS})
    long["seg_order"] = long["segment"].map({n: i for i, (_, n) in enumerate(SEGMENTS)})
    current = pm.get("period")
    long["month_label"] = long["month"].map(month_short)
    long["month_axis"] = [f"{lbl} (MTD)" if mo == current and not pm.get("is_final")
                          else lbl for mo, lbl in zip(long["month"], long["month_label"])]
    order = long.drop_duplicates("month").sort_values("month")["month_axis"].tolist()
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
                     tooltip=[tip("month_label:N", "Month"),
                              tip("segment:N", "Segment"),
                              tip("value:Q", title, ",.0f")])
             .properties(title=f"{title} by month, stacked by segment"))
    totals = long.groupby("month_axis", as_index=False)["value"].sum()
    labels = (alt.Chart(totals).mark_text(dy=-6, color=T["secondary"], fontSize=11)
              .encode(x=alt.X("month_axis:N", sort=order), y="value:Q",
                      text=alt.Text("value:Q", format=",.0f")))
    st.altair_chart(style(alt.layer(chart, labels)), width="stretch")
    if not pm.get("is_final"):
        st.caption(L(f"{month_short(current)} is month-to-date through day {de} of {dm}; "
                     f"not comparable with complete months.",
                     f"{month_short(current)} เป็นยอดสะสมถึงวันที่ {de} จาก {dm} "
                     f"ยังเทียบกับเดือนที่ครบแล้วไม่ได้"))
    with st.expander("Table view"):
        wide = long.pivot_table(index="segment", columns="month_axis", values="value",
                                aggfunc="sum").reindex(columns=order)
        wide.loc["Total"] = wide.sum()
        wide.index.name = "Segment"
        table(f"trend_{measure.lower()}", wide,
              {"Segment": "Segment"} | {c_: num(c_) for c_ in wide.columns})


def render_visibility() -> None:
    """Does shop decoration (visibility) move AP1D200?

    Mirrors the workbook's ตกแต่งแล้ว pivots from the monthly table. Only
    complete months are compared: the report month is partial in every snapshot.
    """
    current = pm.get("period")
    m = monthly[monthly["on_roster"] & monthly["zone"].isin(sel_regions)
                & monthly["month"].lt(current)]
    if m.empty:
        st.info(L("No complete months for the current selection.",
                  "ไม่มีเดือนที่ครบแล้วสำหรับตัวกรองนี้"))
        return
    groups = [g for g in VIS_ORDER if g in set(m["visibility_label"])]
    avg = (m[m["visibility_label"].isin(groups)]
           .groupby(["month", "visibility_label"])
           .agg(total=("ap200_nontourist", "sum"), shops=("code", "nunique"))
           .reset_index())
    avg["per_shop"] = avg["total"] / avg["shops"]
    avg["month_label"] = avg["month"].map(month_short)
    avg["group"] = avg["visibility_label"].map(vis_label)
    order = avg.drop_duplicates("month").sort_values("month")["month_label"].tolist()
    scale = alt.Scale(domain=[vis_label(g) for g in VIS_ORDER], range=T["series"])
    base = alt.Chart(avg).encode(
        x=alt.X("month_label:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("per_shop:Q", title=None),
        color=alt.Color("group:N", title=None, scale=scale),
        tooltip=[tip("month_label:N", "Month"),
                 tip("group:N", "Visibility"),
                 tip("per_shop:Q", "Per shop", ",.1f"),
                 tip("shops:Q", "Shops")])
    lines = base.mark_line(strokeWidth=2)
    points = base.mark_point(filled=True, size=64, stroke=T["surface"], strokeWidth=2)
    with card("vi_chart"):
        st.altair_chart(style(alt.layer(lines, points).properties(
            height=340, title="AP1D200 non-tourist per shop, by visibility status "
                              "(complete months, current roster)")), width="stretch")
        st.caption(L("Groups are today's visibility status applied to every month, so a "
                     "shop decorated in August counts as “Decorated” in January too. The "
                     "table below separates before and after for each decorated shop.",
                     "กลุ่มใช้สถานะการตกแต่งปัจจุบันกับทุกเดือน ร้านที่ตกแต่งในเดือน ส.ค. "
                     "จึงถูกนับเป็น “Decorated” ตั้งแต่ ม.ค. ด้วย ตารางด้านล่างแยกก่อนและ"
                     "หลังตกแต่งของแต่ละร้าน"))

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
                     "decorated": month_long(when) if when else "not recorded",
                     "before": before.mean() if len(before) else None,
                     "after": after.mean() if len(after) else None,
                     "n_after": len(after)})
    t = pd.DataFrame(rows)
    t["change"] = (t["after"] - t["before"]) / t["before"]
    with card("vi_table"):
        st.markdown("**" + "Decorated shops · monthly AP1D200 non-tourist, before vs since decoration"
                    + "**")
        table("decorated",
              for_table(t).sort_values("change", ascending=False, na_position="last"),
              {"shop_name": "Shop",
               "province": "Province",
               "size": "Size",
               "decorated": "Decorated",
               "before": num("Avg / month before"),
               "after": num("Avg / month since"),
               "n_after": num("Months since", "%d"),
               "change": num("Change", "percent")},
              hide_index=True)
        st.caption(L("“Since” counts the decoration month itself and complete months after "
                     "it. Shops decorated in the report month have no complete month yet.",
                     "“since” นับรวมเดือนที่ตกแต่งและเดือนที่ครบหลังจากนั้น ร้านที่ตกแต่งใน"
                     "เดือนที่รายงานยังไม่มีเดือนที่ครบ"))


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
