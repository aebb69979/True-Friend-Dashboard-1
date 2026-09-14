"""True Friend dashboard: serves data/processed/*.parquet.

Reads ONLY parquet -- never the source .xlsx, and never imports src/. Those
parquet files are produced by `python -m src.build`, which refuses to write
unless every By Shop and Summary cell reconciles against the workbook's own
cached values. The arithmetic here is limited to summing published columns
and dividing the sums, the same way Summary's region rows do.

Bilingual: every visible string goes through L(en, th). Thai is the default.
Data values (shop names, codes, regions) are shown as the source has them.
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
    container.selectbox(L("Language", "ภาษา"), LANGS, index=LANGS.index(LANG),
                        key=key, on_change=_apply, label_visibility="collapsed")


TH_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.",
             "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
             "Nov", "Dec"]


def month_short(key: str) -> str:
    """'2026-09' -> "Sep'26" / "ก.ย. 69" (Thai uses the Buddhist year)."""
    y, m = int(key[:4]), int(key[5:7])
    return L(f"{EN_MONTHS[m - 1]}'{y % 100:02d}", f"{TH_MONTHS[m - 1]} {(y + 543) % 100:02d}")


def month_long(key: str) -> str:
    """'2026-09' -> 'Sep 2026' / 'ก.ย. 2569'."""
    y, m = int(key[:4]), int(key[5:7])
    return L(f"{EN_MONTHS[m - 1]} {y}", f"{TH_MONTHS[m - 1]} {y + 543}")


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
SEGMENTS = [("mass", L("Mass", "กลุ่มทั่วไป (Mass)")),
            ("migrant", L("Migrant", "แรงงานข้ามชาติ (Migrant)")),
            ("tourist", L("Tourist", "นักท่องเที่ยว (Tourist)"))]
# Colour follows the entity, never its rank or the current filter. The build
# publishes English visibility keys; these are only how they are printed.
VIS_ORDER = ["Decorated", "To decorate", "To decorate (round 2)"]
VIS_LABEL = {"Decorated": L("Decorated", "ตกแต่งแล้ว"),
             "To decorate": L("To decorate", "รอตกแต่ง"),
             "To decorate (round 2)": L("To decorate (round 2)", "รอตกแต่ง (รอบ 2)"),
             "Not decorating": L("Not decorating", "ไม่ตกแต่ง"),
             "Not set": L("Not set", "ไม่ระบุ")}


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
                st.text_input(L("Password", "รหัสผ่าน"), type="password", key="_pw")
                submitted = st.form_submit_button(L("Sign in", "เข้าสู่ระบบ"),
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
sel = h[2].selectbox(L("Reporting period", "งวดรายงาน"), list(reversed(periods)),
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
    L("Region", "ภูมิภาค"), regions_with_shops, default=[], key="regions",
    placeholder=L(f"All regions ({len(regions_with_shops)})",
                  f"ทุกภูมิภาค ({len(regions_with_shops)})")) or regions_with_shops

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
        L("GA growth", "การเติบโต GA"), format="percent",
        help=L("GA run-rate vs the Jan–Feb'26 baseline. Empty where there was no "
               "GA in Jan–Feb (no baseline).",
               "GA RR เทียบฐาน ม.ค.–ก.พ. 69 · ว่างไว้หากไม่มียอด GA ใน ม.ค.–ก.พ. "
               "(ไม่มีฐาน)"))


def ach_col():
    return st.column_config.ProgressColumn(
        L("RR vs MOU", "RR เทียบ MOU"), format="percent", min_value=0,
        max_value=1.5, color=T["series"][0],
        help=L("AP1D200 non-tourist run-rate ÷ monthly MOU target. Bar tops out "
               "at 150%.",
               "AP1D200 RR ไม่รวมนักท่องเที่ยว ÷ เป้า MOU รายเดือน · แถบเต็มที่ 150%"))


def common_columns() -> dict:
    return {
        "region": L("Region", "ภูมิภาค"), "zone": L("Region", "ภูมิภาค"),
        "province": L("Province", "จังหวัด"), "province_label": L("Province", "จังหวัด"),
        "n_shops": num(L("Shops", "จำนวนร้าน"), "%d"),
        "base_ga": num(L("Baseline GA", "ฐาน GA")),
        "ga": num(L("GA MTD", "GA สะสม")),
        "rr_ga": num(L("GA run-rate", "GA RR")),
        "growth_pct": growth_col(),
        "mou_baseline": num(L("AP1D200 baseline", "ฐาน AP1D200")),
        "mou_target": num(L("MOU target", "เป้า MOU")),
        "ap_nontourist": num(L("AP1D200 MTD", "AP1D200 สะสม")),
        "aprr_nontourist": num(L("AP1D200 run-rate", "AP1D200 RR")),
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
tabs = st.tabs([L("Overview", "ภาพรวม"), L("Province", "จังหวัด"),
                L("Shop", "ร้านค้า"), L("Trend", "แนวโน้ม"),
                L("Visibility", "การตกแต่งร้าน")])


def render_overview() -> None:
    k = rollup(prov_v)
    c = st.columns(4)
    c[0].metric(L("True Friend shops", "ร้าน True Friend"), f"{k['n_shops']:,.0f}",
                help=L("Shops on the roster (Target Truefriend Submit 10).",
                       "ร้านค้าในรายชื่อ (Target Truefriend Submit 10)"))
    gap, mou = k["aprr_nontourist"] - k["mou_target"], k["mou_target"]
    c[1].metric(L("AP1D200 non-tourist · RR vs MOU", "AP1D200 ไม่รวมนักท่องเที่ยว · RR เทียบ MOU"),
                f"{k['ach_mou']:.0%}",
                delta=L(f"{gap:+,.0f} vs target {mou:,.0f}", f"{gap:+,.0f} เทียบเป้า {mou:,.0f}"),
                help=L(f"Run-rate {k['aprr_nontourist']:,.1f} ÷ MOU target {mou:,.0f}. "
                       f"Actual month-to-date {k['ap_nontourist']:,.0f}.",
                       f"RR {k['aprr_nontourist']:,.1f} ÷ เป้า MOU {mou:,.0f} · "
                       f"ยอดจริงสะสม {k['ap_nontourist']:,.0f}"))
    c[2].metric(L("GA run-rate", "GA RR"), f"{k['rr_ga']:,.0f}",
                delta=(L(f"{k['growth_pct']:+.1%} vs Jan–Feb baseline",
                         f"{k['growth_pct']:+.1%} เทียบฐาน ม.ค.–ก.พ.") if k["base_ga"]
                       else L("no Jan–Feb baseline", "ไม่มีฐาน ม.ค.–ก.พ.")),
                help=L(f"Baseline GA {k['base_ga']:,.1f} = (Jan'26 + Feb'26) ÷ 2.",
                       f"ฐาน GA {k['base_ga']:,.1f} = (ม.ค. 69 + ก.พ. 69) ÷ 2"))
    c[3].metric(L("GA month-to-date", "GA สะสมเดือนนี้"), f"{k['ga']:,.0f}",
                help=L(f"Actual gross adds through day {de} of {dm}.",
                       f"ยอด GA จริงถึงวันที่ {de} จาก {dm}"))

    reg = no_baseline_blank(region_rows[region_rows["region"].isin(sel_regions)])
    cols = st.columns(2, gap="large")
    with cols[0]:
        st.altair_chart(hbar(reg, "region", "ach_mou",
                             L("AP1D200 non-tourist run-rate vs MOU, by region",
                               "AP1D200 ไม่รวมนักท่องเที่ยว: RR เทียบ MOU รายภูมิภาค"),
                             target=1.0,
                             tooltip=[tip("region:N", L("Region", "ภูมิภาค")),
                                      tip("ach_mou:Q", L("RR vs MOU", "RR เทียบ MOU"), ".1%"),
                                      tip("aprr_nontourist:Q", L("Run-rate", "RR"), ",.1f"),
                                      tip("mou_target:Q", L("MOU target", "เป้า MOU"), ",.0f")]),
                        width="stretch")
        st.caption(L("Vertical rule = 100% of MOU.", "เส้นแนวตั้ง = 100% ของ MOU"))
    with cols[1]:
        st.altair_chart(hbar(reg, "region", "growth_pct",
                             L("GA run-rate growth vs Jan–Feb baseline, by region",
                               "การเติบโตของ GA RR เทียบฐาน ม.ค.–ก.พ. รายภูมิภาค"),
                             diverging=True, fmt="+.0%",
                             tooltip=[tip("region:N", L("Region", "ภูมิภาค")),
                                      tip("growth_pct:Q", L("Growth", "การเติบโต"), "+.1%"),
                                      tip("rr_ga:Q", L("GA run-rate", "GA RR"), ",.1f"),
                                      tip("base_ga:Q", L("Baseline GA", "ฐาน GA"), ",.1f")]),
                        width="stretch")

    st.markdown(f"**{L('By region', 'แยกตามภูมิภาค')}**")
    total = rollup(reg)
    total["region"] = L("Total", "รวม")
    table = pd.concat([reg, total.to_frame().T], ignore_index=True)
    st.dataframe(
        for_table(table)[["region", "n_shops", "base_ga", "ga", "rr_ga", "growth_pct",
                          "mou_baseline", "mou_target", "ap_nontourist",
                          "aprr_nontourist", "ach_mou"]],
        hide_index=True, width="stretch", column_config=common_columns())
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
    with cols[0]:
        st.altair_chart(hbar(with_target, "province_label", "ach_mou",
                             L("AP1D200 non-tourist run-rate vs MOU",
                               "AP1D200 ไม่รวมนักท่องเที่ยว: RR เทียบ MOU"),
                             target=1.0, row_px=24,
                             tooltip=[tip("province_label:N", L("Province", "จังหวัด")),
                                      tip("region:N", L("Region", "ภูมิภาค")),
                                      tip("ach_mou:Q", L("RR vs MOU", "RR เทียบ MOU"), ".1%"),
                                      tip("aprr_nontourist:Q", L("Run-rate", "RR"), ",.1f"),
                                      tip("mou_target:Q", L("MOU target", "เป้า MOU"), ",.0f")]),
                        width="stretch")
    with cols[1]:
        st.altair_chart(hbar(p, "province_label", "growth_pct",
                             L("GA run-rate growth vs baseline", "การเติบโตของ GA RR เทียบฐาน"),
                             diverging=True, fmt="+.0%", row_px=24,
                             tooltip=[tip("province_label:N", L("Province", "จังหวัด")),
                                      tip("growth_pct:Q", L("Growth", "การเติบโต"), "+.1%"),
                                      tip("rr_ga:Q", L("GA run-rate", "GA RR"), ",.1f"),
                                      tip("base_ga:Q", L("Baseline GA", "ฐาน GA"), ",.1f")]),
                        width="stretch")
    st.markdown(f"**{L('By province', 'แยกตามจังหวัด')}**")
    st.dataframe(
        for_table(p).sort_values(["region", "ach_mou"], ascending=[True, False])[
            ["region", "province_label", "n_shops", "base_ga", "ga", "rr_ga",
             "growth_pct", "mou_target", "ap_nontourist", "aprr_nontourist",
             "ach_mou", "growth_mou", "rank_ach_mou"]],
        hide_index=True, width="stretch",
        column_config=common_columns() | {
            "growth_mou": num(L("RR − AP1D200 baseline", "RR − ฐาน AP1D200")),
            "rank_ach_mou": num(L("Rank", "อันดับ"), "%d",
                                help=L("By RR vs MOU, across all provinces with a target.",
                                       "เรียงตาม RR เทียบ MOU ทุกจังหวัดที่มีเป้า"))})


FIELD_NAMES = {"growth_migrant": L("migrant growth", "การเติบโตกลุ่ม Migrant"),
               "growth_mass": L("mass growth", "การเติบโตกลุ่ม Mass"),
               "growth_tourist": L("tourist growth", "การเติบโตกลุ่ม Tourist"),
               "ap_tourist": L("AP1D200 tourist", "AP1D200 นักท่องเที่ยว")}


def render_shop() -> None:
    f = st.columns([2, 2, 2])
    provs = sorted(shop_v["province"].unique())
    sp = f[0].multiselect(L("Province", "จังหวัด"), provs, default=[], key="s_prov",
                          placeholder=L(f"All ({len(provs)})", f"ทั้งหมด ({len(provs)})")) or provs
    vis = [v for v in VIS_ORDER if v in set(shop_v["visibility_label"])] + sorted(
        set(shop_v["visibility_label"]) - set(VIS_ORDER))
    sv = f[1].multiselect(L("Visibility", "การตกแต่งร้าน"), vis, default=[], key="s_vis",
                          format_func=vis_label,
                          placeholder=L(f"All ({len(vis)})", f"ทั้งหมด ({len(vis)})")) or vis
    q = f[2].text_input(L("Search shop name or code", "ค้นหาชื่อร้านหรือรหัส"),
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
    c[0].metric(L("Shops shown", "ร้านที่แสดง"), f"{len(v):,}")
    c[1].metric(L("RR vs MOU", "RR เทียบ MOU"), f"{k['ach_mou']:.0%}",
                help=f"{k['aprr_nontourist']:,.1f} ÷ {k['mou_target']:,.0f}")
    c[2].metric(L("GA growth vs baseline", "การเติบโต GA เทียบฐาน"),
                f"{k['growth_pct']:+.1%}" if k["base_ga"] else "—",
                help=None if k["base_ga"] else L("No GA in Jan–Feb'26, so no baseline.",
                                                  "ไม่มียอด GA ใน ม.ค.–ก.พ. 69 จึงไม่มีฐาน"))

    v = no_baseline_blank(v)
    v["visibility_display"] = v["visibility_label"].map(vis_label)
    below = int((v["ach_mou"] < 1).sum())
    st.caption(L(f"{below} of {len(v)} shops are running below their MOU target.",
                 f"{below} จาก {len(v)} ร้าน ยังต่ำกว่าเป้า MOU"))
    st.dataframe(
        for_table(v).sort_values("ach_mou", ascending=False)[
            ["zone", "province", "shop_name", "code", "visibility_display",
             "base_ga", "ga", "rr_ga", "growth_pct", "mou_baseline", "mou_target",
             "ap_nontourist", "aprr_nontourist", "ach_mou", "growth_mou",
             "true_friend_code"]],
        hide_index=True, width="stretch", height=min(38 + 35 * len(v), 760),
        column_config=common_columns() | {
            "shop_name": st.column_config.TextColumn(L("Shop", "ร้านค้า"), width="medium"),
            "code": L("Combine code", "รหัสร้าน (Combine)"),
            "visibility_display": L("Visibility", "การตกแต่งร้าน"),
            "growth_mou": num(L("RR − baseline", "RR − ฐาน")),
            "true_friend_code": L("True Friend code", "รหัส True Friend")})
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


MEASURES = {"GA": ("ga", L("Gross adds", "ยอด GA")),
            "AP1D200": ("ap200", L("AP1D200 activations", "AP1D200")),
            "M1": ("m1", L("M1 revenue (THB)", "รายได้ M1 (บาท)"))}
ALL_SHOPS = "__all__"


def render_trend() -> None:
    f = st.columns([1.4, 1.3, 2.4])
    measure = f[0].segmented_control(
        L("Measure", "ตัวชี้วัด"), list(MEASURES), default="GA", key="t_measure",
        format_func=lambda k_: L("M1 revenue", "รายได้ M1") if k_ == "M1" else k_) or "GA"
    roster_only = f[1].toggle(L("Current roster only", "เฉพาะร้านในรายชื่อปัจจุบัน"),
                              value=True, key="t_roster",
                              help=L("Off includes shops that were in the programme "
                                     "earlier in the year and have since left.",
                                     "ปิดเพื่อรวมร้านที่เคยอยู่ในโครงการช่วงต้นปีแต่ออกไปแล้ว"))
    m = monthly[monthly["region"].isin(sel_regions) | monthly["zone"].isin(sel_regions)]
    if roster_only:
        m = m[m["on_roster"]]
    shops = dict(sorted(zip(m["code"], m["shop_name"]), key=lambda kv: kv[1]))
    pick = f[2].selectbox(L("Shop", "ร้านค้า"), [ALL_SHOPS] + list(shops), key="t_shop",
                          format_func=lambda k_: L("All shops", "ทุกร้าน") if k_ == ALL_SHOPS
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
    long["month_axis"] = [f"{lbl} {L('(MTD)', '(สะสม)')}" if mo == current and not pm.get("is_final")
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
                     tooltip=[tip("month_label:N", L("Month", "เดือน")),
                              tip("segment:N", L("Segment", "กลุ่มลูกค้า")),
                              tip("value:Q", title, ",.0f")])
             .properties(title=L(f"{title} by month, stacked by segment",
                                 f"{title} รายเดือน แยกตามกลุ่มลูกค้า")))
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
    with st.expander(L("Table view", "มุมมองตาราง")):
        wide = long.pivot_table(index="segment", columns="month_axis", values="value",
                                aggfunc="sum").reindex(columns=order)
        wide.loc[L("Total", "รวม")] = wide.sum()
        wide.index.name = L("Segment", "กลุ่มลูกค้า")
        st.dataframe(wide, width="stretch",
                     column_config={c_: num(c_) for c_ in wide.columns})


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
        tooltip=[tip("month_label:N", L("Month", "เดือน")),
                 tip("group:N", L("Visibility", "การตกแต่งร้าน")),
                 tip("per_shop:Q", L("Per shop", "ต่อร้าน"), ",.1f"),
                 tip("shops:Q", L("Shops", "จำนวนร้าน"))])
    lines = base.mark_line(strokeWidth=2)
    points = base.mark_point(filled=True, size=64, stroke=T["surface"], strokeWidth=2)
    st.altair_chart(style(alt.layer(lines, points).properties(
        height=340, title=L("AP1D200 non-tourist per shop, by visibility status "
                            "(complete months, current roster)",
                            "AP1D200 ไม่รวมนักท่องเที่ยว ต่อร้าน แยกตามสถานะการตกแต่ง "
                            "(เดือนที่ครบแล้ว · ร้านในรายชื่อปัจจุบัน)"))), width="stretch")
    st.caption(L("Groups are today's visibility status applied to every month, so a "
                 "shop decorated in August counts as “Decorated” in January too. The "
                 "table below separates before and after for each decorated shop.",
                 "กลุ่มใช้สถานะการตกแต่งปัจจุบันกับทุกเดือน ร้านที่ตกแต่งในเดือน ส.ค. "
                 "จึงถูกนับเป็น “ตกแต่งแล้ว” ตั้งแต่ ม.ค. ด้วย ตารางด้านล่างแยกก่อนและ"
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
                     "decorated": month_long(when) if when else L("not recorded", "ไม่มีบันทึก"),
                     "before": before.mean() if len(before) else None,
                     "after": after.mean() if len(after) else None,
                     "n_after": len(after)})
    t = pd.DataFrame(rows)
    t["change"] = (t["after"] - t["before"]) / t["before"]
    st.markdown("**" + L("Decorated shops · monthly AP1D200 non-tourist, before vs since decoration",
                         "ร้านที่ตกแต่งแล้ว · AP1D200 ไม่รวมนักท่องเที่ยวรายเดือน ก่อนเทียบหลังตกแต่ง")
                + "**")
    st.dataframe(for_table(t).sort_values("change", ascending=False, na_position="last"),
                 hide_index=True, width="stretch",
                 column_config={"shop_name": L("Shop", "ร้านค้า"),
                                "province": L("Province", "จังหวัด"),
                                "size": L("Size", "ขนาด"),
                                "decorated": L("Decorated", "ตกแต่งเมื่อ"),
                                "before": num(L("Avg / month before", "เฉลี่ย/เดือน ก่อน")),
                                "after": num(L("Avg / month since", "เฉลี่ย/เดือน หลัง")),
                                "n_after": num(L("Months since", "จำนวนเดือนหลัง"), "%d"),
                                "change": num(L("Change", "เปลี่ยนแปลง"), "percent")})
    st.caption(L("“Since” counts the decoration month itself and complete months after "
                 "it. Shops decorated in the report month have no complete month yet.",
                 "“หลัง” นับรวมเดือนที่ตกแต่งและเดือนที่ครบหลังจากนั้น ร้านที่ตกแต่งใน"
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
