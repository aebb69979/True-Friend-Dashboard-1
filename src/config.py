"""Schema contract for the True Friend report workbook.

Everything the ingest binds to lives here, so "the source changed" is always a
diff against ONE file. Nothing downstream hardcodes a column letter or a sheet
name.

The workbook (`Report True Friend V3.1 <DD-MM-YYYY>[-Final].xlsx`) is a
hand-maintained Excel report. Its lineage, as far as this pipeline cares:

    Data Total   (pasted monthly rows, month x shop)  ─┐
    Target Truefriend Submit 10  (the shop roster)    ─┼─► By Shop   (per shop)
    Pivot Base   (Jan+Feb GA / 2, a pivot of Data Total)┘   Summary   (per province,
                                                                       per region)

We read Data Total and the Target roster as FEEDS (bound by header name),
recompute By Shop and Summary from them, and reconcile against the cached
values in those two sheets. Pivot Base is never read: its baseline is
recomputed from Data Total, which is how a stale pivot gets caught.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Snapshot naming --------------------------------------------------------
# The filename as the report owner actually saves it:
#     Report True Friend V3.1 11-09-2026.xlsx
#     Report True Friend V3.1 31-08-2026-Final.xlsx
# ANCHORED, so a scratch copy ("... Aug 2026 Visibility 1.xlsx", "... (2).xlsx")
# is skipped and named rather than silently ranked among the snapshots. The
# date is a human label only; the PERIOD comes from the data (build._period_of).
SNAPSHOT_NAME_RE = re.compile(
    r"^Report True Friend V(?P<version>\d+(?:\.\d+)*) "
    r"(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})(?P<final>-Final)?\.xlsx$"
)
SNAPSHOT_EXAMPLE = "Report True Friend V3.1 DD-MM-YYYY[-Final].xlsx"

# --- Sheets -----------------------------------------------------------------

SHEET_DATA = "Data Total"
SHEET_TARGET = "Target Truefriend Submit 10"
# Oct'26 split the program into phases: the grids we read became "Summary Ph1"
# / "By Shop Ph1" (same layout), and "Summary " (trailing space) became a
# PH1+PH2 roll-up with a different layout. Candidates are tried in order, first
# present wins -- so a new workbook binds Ph1 and never the roll-up, and an old
# one still binds "Summary" / "By Shop".
SHEET_SUMMARY = ("Summary Ph1", "Summary")
SHEET_SHOP = ("By Shop Ph1", "By Shop")

# Phase 2 (from Oct'26). All five are read together or not at all: a snapshot
# with none of them is Phase 1 only; one with some but not all is refused.
# SHEET_SUMMARY_ALL is the PH1+PH2 roll-up -- the same trimmed name as the
# old Phase 1 "Summary", so it is only ever bound when Phase 2 is present,
# and its layout's "PH1 & PH2" banner guards against binding the wrong one.
SHEET_DATA_PH2 = "Data Total PH2"
SHEET_TARGET_PH2 = "Target Truefriend PH2"
SHEET_SHOP_PH2 = "By Shop Ph2"
SHEET_SUMMARY_PH2 = "Summary Ph2"
SHEET_SUMMARY_ALL = "Summary"
PH2_SHEETS = (SHEET_DATA_PH2, SHEET_TARGET_PH2, SHEET_SHOP_PH2,
              SHEET_SUMMARY_PH2)

# Names that must NEVER be bound as a source, even if a sheet above is renamed
# onto them. Matched on the trimmed name.
FORBIDDEN_SOURCE_SHEETS = {
    "Target Truefriend Submit 10 (2)",
    "Data Current",
    "Data Last",
    "Sheet5",
}

# Read only to detect churn -- validate.check_sheet_inventory warns when a sheet
# appears or disappears. Never fatal.
EXPECTED_SHEETS = {
    "Data Current", "Data Last", "Summary", "Summary Ph1", "Summary Ph2",
    "By Shop Ph1", "By Shop Ph2", "Data Total", "Data Total PH2", "Code",
    "Target Truefriend Submit 10", "Target Truefriend PH2", "ตกแต่งแล้ว",
    "INcentive", "Pivot Base", "Target Truefriend Submit 10 (2)", "Sheet5",
}

# Sheets deliberately never ingested, and WHY -- so nobody wires one back in.
IGNORED_SHEETS = {
    "Data Current": "hidden staging for a monthly paste; still holds Mar/Apr'26 "
                    "and XLOOKUPs into an external copy of an older report",
    "Data Last": "hidden staging, Jan-Mar'26",
    "Sheet5": "hidden scratch; XLOOKUPs Apr AP1D200 from an external workbook",
    "Target Truefriend Submit 10 (2)": "an older 76-shop roster; no formula "
                                       "references it",
    "Pivot Base": "Jan+Feb baseline pivot. Recomputed from Data Total instead, "
                  "so a pivot left unrefreshed after a roster change is caught",
    "Code": "True Friend Code / Outlet Code coverage checks for the owner",
    "ตกแต่งแล้ว": "pivots of Data Total filtered to decorated shops; the "
                  "Visibility page rebuilds these from the monthly table",
    "INcentive": "a chart shell with no cells",
}

# --- Feed: Data Total PH2 (header row 1, bound by name) ----------------------
# Phase 2's monthly paste. Its own header names, and one current month only:
# By Shop Ph2's SUMIFS have NO month filter, so a second month pasted below
# would be summed into this one (validate fails on that).
DATA_PH2_COLS = {
    "Mth": "month_label",
    "code_D": "retailer_code",
    "code_T": "s2_code",
    "join_code": "code",
    "rtr_name": "shop_name",
    "Region": "region",
    "Province": "province",
    "GA Mass": "ga_mass", "GA Migrant": "ga_migrant", "GA Tourist": "ga_tourist",
    "GA": "ga_total_sheet",
    # Phase 2's MOU measure: AP1D250, Mass + Migrant. No segment split.
    "AP1D250 M&M": "ap250_nontourist",
}
DATA_PH2_NUMERIC = ("ga_mass", "ga_migrant", "ga_tourist", "ga_total_sheet",
                    "ap250_nontourist")
DATA_PH2_TEXT = ("month_label", "retailer_code", "s2_code", "code",
                 "shop_name", "region", "province")

# --- Feed: Target Truefriend PH2 (header row 1, bound by name) --------------
TARGET_PH2_HEADER_ROW = 1
TARGET_PH2_COLS = {
    "No": "no",
    "Region": "zone",
    "Combine Code": "code",
    "ชื่อร้าน": "shop_name",
    "Commission Recomment Target 1M": "mou_target",
    "MOU": "mou_flag",
}
# By Shop Ph2!Z reads its MOU baseline from this column. It is headed
# "AVG AP1D200" but the report owner understands it as AP1D250; either header
# binds, so the expected rename does not break the build.
TARGET_PH2_BASELINE_HEADERS = ("AVG AP1D250", "AVG AP1D200")
TARGET_PH2_NUMERIC = ("mou_baseline", "mou_target")

# --- Months -----------------------------------------------------------------
# The workbook labels months as text, "Aug'26". Parsed to a sortable key.
MONTH_LABEL_RE = re.compile(r"^(?P<mon>[A-Za-z]{3})'(?P<yy>\d{2})$")
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Oct", "Nov", "Dec"]

# The GA baseline is the mean of these two months (Pivot Base shows Jan'26 and
# Feb'26 only, and By Shop / Summary take its "/2" columns).
BASELINE_MONTHS = ("Jan'26", "Feb'26")
BASELINE_DIVISOR = 2
# Pivot Base's page filter keeps only these Phase values ("Yes,CF 6Mar26",
# "Yes,CF Apr 26"); rows whose Phase lookup is #N/A -- shops not on the current
# roster -- drop out of the baseline. Matched as a prefix.
BASELINE_PHASE_PREFIX = "Yes"

# --- Feed: Data Total (header row 1, bound by name) --------------------------

SEGMENTS = ("mass", "migrant", "tourist")

DATA_COLS = {
    "Mth": "month_label",
    "Retailer Code_D": "retailer_code",
    "S2/L2 Code_T": "s2_code",
    "Combine Code": "code",
    "RTR Name": "shop_name",
    "Region": "region",
    "PBH Province": "province",
    "GA Mass": "ga_mass", "GA Migrant": "ga_migrant", "GA Tourist": "ga_tourist",
    "GA": "ga_total_sheet",
    "AP1D Mass": "ap1d_mass", "AP1D Migrant": "ap1d_migrant",
    "AP1D Tourist": "ap1d_tourist", "AP1D": "ap1d_total_sheet",
    "AP1D200 Mass": "ap200_mass", "AP1D200 Migrant": "ap200_migrant",
    "AP1D200 Tourist": "ap200_tourist", "AP1D200": "ap200_total_sheet",
    "M1 Mass": "m1_mass", "M1 Migrant": "m1_migrant", "M1 Tourist": "m1_tourist",
    "M1": "m1_total_sheet",
    # Cached VLOOKUPs into the Target roster -- compared against our own join.
    "Phase": "phase_sheet",
    "Visibility": "visibility_sheet",
}
# Values are numbers ONLY if Excel stores them as numbers. A number typed as
# text ("101") is invisible to SUMIFS and to a pivot's Sum, so it counts as 0
# here too -- and validate warns with the cell address so it gets fixed
# upstream. Coercing it would publish a figure the report does not.
DATA_NUMERIC = tuple(v for k, v in DATA_COLS.items()
                     if v.startswith(("ga_", "ap1d_", "ap200_", "m1_")))
DATA_TEXT = ("month_label", "retailer_code", "s2_code", "code", "shop_name",
             "region", "province")

# --- Feed: Target roster (banner rows 1-3, header row 4, bound by name) ------

TARGET_HEADER_ROW = 4
TARGET_COLS = {
    "No": "no",
    "Zone": "zone",
    "Retailer Code_D": "retailer_code",
    "S2/L2 Code_T": "s2_code",
    "Combine Code": "code",
    "Retailer Name": "shop_name",
    "Group": "group",
    "Cluster": "cluster",
    "RS Name": "rs_name",
    "PBH Name": "pbh_name",
    "Baseline": "mou_baseline",       # AP1D200 non-tourist baseline (Summary U)
    "Target Month": "mou_target",     # the monthly MOU target (Summary V)
    "final Y/N": "phase",             # what Data Total's "Phase" looks up
    "MOU": "mou_flag",
    "Visibility": "visibility",
    "Visibility Size": "visibility_size",
    "Province": "province",           # what Summary's COUNTIFS/SUMIFS key on
}
# Columns that are useful but not load-bearing for any published number. A
# missing one warns instead of failing -- e.g. "Target Jul-Sep.26" is renamed
# every quarter.
TARGET_OPTIONAL_COLS = {
    "Route": "route",
    "Starter Kit": "starter_kit",
    "SIM Starter": "sim_starter",
    "True Friend Code": "true_friend_code",
    "Outlet Code": "outlet_code",
    "เดือนที่ตกแต่ง": "decorated_month",
    "Target Jul-Sep.26": "target_quarter",
}
TARGET_NUMERIC = ("mou_baseline", "mou_target", "target_quarter")

# The Visibility values the roster uses, and how the dashboard labels them.
VISIBILITY_LABELS = {
    "ตกแต่งแล้ว": "Decorated",
    "ตกแต่ง": "To decorate",
    "ตกแต่ง2": "To decorate (round 2)",
    "ไม่แต่ง": "Not decorating",
}

# Region order as Summary prints it. BMA rows exist but carry no shops today.
REGION_ORDER = ["BMA-West", "BMA-East", "E", "N", "NE-1", "NE-2", "CW", "S"]


# --- Hand-edited sheets: guarded positional layouts --------------------------


@dataclass
class GridLayout:
    """One generation of a hand-edited grid's geometry.

    `fields` maps our name -> (column_letter, guard_row, expected_label). Every
    positional read is guarded; a moved header rejects the layout rather than
    silently binding the wrong column. Labels repeat ("Mass" appears four
    times), which is why the guard is a (column, label) pair, never a lookup.

    `banners` are extra (column, row, label) guards on the band above.
    `control` are single cells read by position, e.g. the run-rate day counts.
    """

    name: str
    first_data_row: int
    max_data_row: int              # scan limit; a real row needs a key value
    key_col: str
    fields: dict[str, tuple[str, int, str]]
    banners: list[tuple[str, int, str]] = field(default_factory=list)
    control: dict[str, str] = field(default_factory=dict)
    # Rows whose key equals one of these are totals, not entities.
    total_keys: tuple[str, ...] = ()
    # Fields that are typed INPUTS on this sheet, not formulas: read as data
    # and never checked for formula deviations (every value differs).
    inputs: tuple[str, ...] = ()


def _metric_fields(row: int, first: str) -> dict[str, tuple[str, int, str]]:
    """The 29-column metric block By Shop and Summary share, from `first` on.

    Same arithmetic, same labels, different start column -- generated once so
    the two sheets cannot be described inconsistently.
    """
    labels = [
        ("base_mass", "Mass"), ("base_migrant", "migrant"),
        ("base_tourist", "tourist"), ("base_ga", "Baseline GA"),
        ("ga_mass", "Mass"), ("ga_migrant", "migrant"), ("ga_tourist", "tourist"),
        ("ga", "Actual GA"),
        ("rr_mass", "Mass RR"), ("rr_migrant", "migrant RR"),
        ("rr_tourist", "tourist RR"), ("rr_ga", "RR GA"),
        ("growth_mass", "Mass"), ("growth_migrant", "migrant"),
        ("growth_tourist", "tourist"), ("growth_ga", "Total Growth GA"),
        ("growth_pct", "% Growth"),
        ("mou_baseline", "AP1D200 Non Tourist"), ("mou_target", "MOU"),
        ("ap_mass", "Mass"), ("ap_migrant", "migrant"), ("ap_tourist", "tourist"),
        ("ap_nontourist", "AP1D200 Non Tourist"),
        ("aprr_mass", "Mass RR"), ("aprr_migrant", "migrant RR"),
        ("aprr_tourist", "tourist RR"),
        ("aprr_nontourist", "AP1D200 Non Tourist GA RR"),
        ("ach_mou", "RR Ach MOU"), ("growth_mou", "Growth form MOU Baseline"),
    ]
    start = _col_idx(first)
    return {name: (_idx_col(start + i), row, label)
            for i, (name, label) in enumerate(labels)}


def _col_idx(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _idx_col(idx: int) -> str:
    s, n = "", idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


# By Shop, V3.1: header row 4, one row per roster shop from row 5. Rows are
# re-sorted between snapshots, so they are matched on Combine Code, never on
# row number.
SHOP_LAYOUT_V31 = GridLayout(
    name="v3.1",
    first_data_row=5,
    max_data_row=400,
    key_col="F",
    fields={
        "zone": ("B", 4, "Zone"),
        "province": ("C", 4, "PBH Province"),
        "code": ("F", 4, "Combine Code"),
        "shop_name": ("G", 4, "Retailer Name"),
        "phase": ("H", 4, "Phase"),
        **_metric_fields(4, "I"),
        "remark": ("AL", 4, "Remark"),
    },
    banners=[("I", 3, "Baseline GA"), ("U", 3, "Growth"), ("Z", 3, "Baseline"),
             ("AA", 3, "Target"), ("AB", 3, "Actual AP1D200")],
    # By Shop reads these from Summary; checked equal.
    control={"days_elapsed": "A1", "days_in_month": "A2", "month_label": "M2"},
)

# Summary, V3.1, province grid: header row 18, rows 19-62.
SUMMARY_PROVINCE_LAYOUT_V31 = GridLayout(
    name="v3.1",
    first_data_row=19,
    max_data_row=62,
    key_col="B",
    fields={
        "region": ("A", 18, "Region"),
        "province": ("B", 18, "Province"),
        "n_shops": ("C", 18, "#RTR True Friend"),
        **_metric_fields(18, "D"),
    },
    banners=[("D", 17, "Baseline GA"), ("P", 17, "Growth RR"),
             ("U", 17, "Baseline"), ("V", 17, "Target"),
             ("W", 17, "Actual AP1D200")],
    # A1 / A2 drive every run-rate: (actual / days_elapsed) * days_in_month.
    # H16 is the reporting month every SUMIFS matches on.
    control={"days_elapsed": "A1", "days_in_month": "A2", "month_label": "H16"},
)

# Summary, V3.1, region roll-up: header row 4, rows 5-13 (13 = Total). The
# "% Growth" header reads differently up here.
_region_fields = _metric_fields(4, "D")
_region_fields["growth_pct"] = (_region_fields["growth_pct"][0], 4,
                                "% Growth Baseline GA")
SUMMARY_REGION_LAYOUT_V31 = GridLayout(
    name="v3.1",
    first_data_row=5,
    max_data_row=13,
    key_col="B",
    fields={
        "region": ("B", 4, "Region"),
        "n_shops": ("C", 4, "#RTR True Friend"),
        **_region_fields,
    },
    banners=[("W", 3, "Actual AP1D200")],
    total_keys=("Total",),
)

# The Phase 2 block: the GA part of the 29 columns, then AP1D250 as a single
# non-tourist total -- no Mass / migrant / tourist split. The AP headers differ
# between sheets, so they are passed in.
def _short(row: int, first: str, *, mou_label: str, ap_label: str,
           rr_label: str, pct_label: str = "% Growth"
           ) -> dict[str, tuple[str, int, str]]:
    labels = [
        ("base_mass", "Mass"), ("base_migrant", "migrant"),
        ("base_tourist", "tourist"), ("base_ga", "Baseline GA"),
        ("ga_mass", "Mass"), ("ga_migrant", "migrant"), ("ga_tourist", "tourist"),
        ("ga", "Actual GA"),
        ("rr_mass", "Mass RR"), ("rr_migrant", "migrant RR"),
        ("rr_tourist", "tourist RR"), ("rr_ga", "RR GA"),
        ("growth_mass", "Mass"), ("growth_migrant", "migrant"),
        ("growth_tourist", "tourist"), ("growth_ga", "Total Growth GA"),
        ("growth_pct", pct_label),
        ("mou_baseline", mou_label), ("mou_target", "MOU"),
        ("ap_nontourist", ap_label), ("aprr_nontourist", rr_label),
        ("ach_mou", "RR Ach MOU"), ("growth_mou", "Growth form MOU Baseline"),
    ]
    start = _col_idx(first)
    return {name: (_idx_col(start + i), row, label)
            for i, (name, label) in enumerate(labels)}


# By Shop Ph2: header row 4, rows from 5. Zone, province and the Jan-Feb GA
# baseline (I:K) are TYPED here and exist nowhere else in the workbook.
SHOP_PH2_LAYOUT_V1 = GridLayout(
    name="ph2.v1",
    first_data_row=5,
    max_data_row=400,
    key_col="F",
    fields={
        "zone": ("B", 4, "Zone"),
        "province": ("C", 4, "PBH Province"),
        "code": ("F", 4, "Combine Code"),
        "shop_name": ("G", 4, "Retailer Name"),
        "phase": ("H", 4, "Phase"),
        **_short(4, "I", mou_label="AP1D250 Non Tourist",
                 ap_label="AP1D250 Non Tourist",
                 rr_label="AP1D250 Non Tourist GA RR"),
        "remark": ("AF", 4, "Remark"),
    },
    banners=[("I", 3, "Baseline GA"), ("U", 3, "Growth"), ("Z", 3, "Baseline"),
             ("AA", 3, "Target")],
    control={"days_elapsed": "A1", "days_in_month": "A2", "month_label": "M2"},
    inputs=("base_mass", "base_migrant", "base_tourist"),
)

# Summary Ph2 and the PH1+PH2 "Summary": province grid header row 18, rows
# 19-62; region block header row 4, rows 5-13. The B17 / B3 banner names the
# phase, so neither can bind the other (or Summary Ph1).
SUMMARY_PH2_PROVINCE_LAYOUT_V1 = GridLayout(
    name="ph2.v1", first_data_row=19, max_data_row=62, key_col="B",
    fields={
        "region": ("A", 18, "Region"),
        "province": ("B", 18, "Province"),
        "n_shops": ("C", 18, "#RTR True Friend"),
        # U18 says AP1D200, a leftover label; the cells sum By Shop Ph2!Z.
        **_short(18, "D", mou_label="AP1D200 Non Tourist",
                 ap_label="AP1D250 Non Tourist",
                 rr_label="AP1D250 Non Tourist GA RR"),
    },
    banners=[("B", 17, "PH2"), ("D", 17, "Baseline GA"), ("P", 17, "Growth RR"),
             ("U", 17, "Baseline"), ("V", 17, "Target")],
    control={"days_elapsed": "A1", "days_in_month": "A2", "month_label": "H16"},
)
SUMMARY_PH2_REGION_LAYOUT_V1 = GridLayout(
    name="ph2.v1", first_data_row=5, max_data_row=13, key_col="B",
    fields={
        "region": ("B", 4, "Region"),
        "n_shops": ("C", 4, "#RTR True Friend"),
        **_short(4, "D", mou_label="AP1D250 Non Tourist",
                 ap_label="AP1D250 Non Tourist",
                 rr_label="AP1D250 Non Tourist GA RR",
                 pct_label="% Growth Baseline GA"),
    },
    banners=[("B", 3, "PH2")],
    total_keys=("Total",),
)
SUMMARY_ALL_PROVINCE_LAYOUT_V1 = GridLayout(
    name="all.v1", first_data_row=19, max_data_row=62, key_col="B",
    fields={
        "region": ("A", 18, "Region"),
        "province": ("B", 18, "Province"),
        "n_shops": ("C", 18, "#RTR True Friend"),
        **_short(18, "D", mou_label="AP1D200 Non Tourist",
                 ap_label="AP1D200 Non Tourist",
                 rr_label="AP1D200/250 Non Tourist GA RR"),
    },
    banners=[("B", 17, "PH1 & PH2"), ("D", 17, "Baseline GA"),
             ("P", 17, "Growth RR"), ("U", 17, "Baseline"), ("V", 17, "Target")],
    control={"days_elapsed": "A1", "days_in_month": "A2", "month_label": "H16"},
)
SUMMARY_ALL_REGION_LAYOUT_V1 = GridLayout(
    name="all.v1", first_data_row=5, max_data_row=13, key_col="B",
    fields={
        "region": ("B", 4, "Region"),
        "n_shops": ("C", 4, "#RTR True Friend"),
        **_short(4, "D", mou_label="AP1D200/250 Non Tourist",
                 ap_label="AP1D200 Non Tourist",
                 rr_label="AP1D200/250 Non Tourist GA RR",
                 pct_label="% Growth Baseline GA"),
    },
    banners=[("B", 3, "PH1 & PH2")],
    total_keys=("Total",),
)

# Tried in order, newest first. When the sheet is restructured, ADD a layout at
# the front -- never edit one in place, or old snapshots stop rebuilding.
SHOP_LAYOUTS = (SHOP_LAYOUT_V31,)
SUMMARY_PROVINCE_LAYOUTS = (SUMMARY_PROVINCE_LAYOUT_V31,)
SUMMARY_REGION_LAYOUTS = (SUMMARY_REGION_LAYOUT_V31,)
SHOP_PH2_LAYOUTS = (SHOP_PH2_LAYOUT_V1,)
SUMMARY_PH2_PROVINCE_LAYOUTS = (SUMMARY_PH2_PROVINCE_LAYOUT_V1,)
SUMMARY_PH2_REGION_LAYOUTS = (SUMMARY_PH2_REGION_LAYOUT_V1,)
SUMMARY_ALL_PROVINCE_LAYOUTS = (SUMMARY_ALL_PROVINCE_LAYOUT_V1,)
SUMMARY_ALL_REGION_LAYOUTS = (SUMMARY_ALL_REGION_LAYOUT_V1,)

# --- Reconciliation ---------------------------------------------------------

# Every metric field is recomputed and compared. Counts and run-rates are
# floats out of Excel's own arithmetic, so the tolerance is relative.
RECONCILE_FIELDS = tuple(_metric_fields(0, "A"))
RECONCILE_REL_TOL = 1e-9
RECONCILE_ABS_TOL = 1e-6

# Within-row dependencies, so a cell whose FORMULA deviates from its column
# (a mistyped reference) excuses the cells computed from it -- and nothing
# else. A deviant cell never excuses a different row.
FIELD_DEPENDENTS = {
    "base_mass": ["base_ga", "growth_mass", "growth_ga", "growth_pct"],
    "base_migrant": ["base_ga", "growth_migrant", "growth_ga", "growth_pct"],
    "base_tourist": ["base_ga", "growth_tourist", "growth_ga", "growth_pct"],
    "ga_mass": ["ga", "rr_mass", "rr_ga", "growth_mass", "growth_ga",
                "growth_pct"],
    "ga_migrant": ["ga", "rr_migrant", "rr_ga", "growth_migrant", "growth_ga",
                   "growth_pct"],
    "ga_tourist": ["ga", "rr_tourist", "rr_ga", "growth_tourist", "growth_ga",
                   "growth_pct"],
    "rr_mass": ["rr_ga", "growth_mass", "growth_ga", "growth_pct"],
    "rr_migrant": ["rr_ga", "growth_migrant", "growth_ga", "growth_pct"],
    "rr_tourist": ["rr_ga", "growth_tourist", "growth_ga", "growth_pct"],
    "growth_mass": ["growth_ga", "growth_pct"],
    "growth_migrant": ["growth_ga", "growth_pct"],
    "growth_tourist": ["growth_ga", "growth_pct"],
    "growth_ga": ["growth_pct"],
    "mou_baseline": ["growth_mou"],
    "mou_target": ["ach_mou"],
    "ap_mass": ["ap_nontourist", "aprr_mass", "aprr_nontourist", "ach_mou",
                "growth_mou"],
    "ap_migrant": ["ap_nontourist", "aprr_migrant", "aprr_nontourist",
                   "ach_mou", "growth_mou"],
    "ap_tourist": ["aprr_tourist"],
    "aprr_mass": ["aprr_nontourist", "ach_mou", "growth_mou"],
    "aprr_migrant": ["aprr_nontourist", "ach_mou", "growth_mou"],
    "aprr_nontourist": ["ach_mou", "growth_mou"],
}

# Phase 2 and the PH1+PH2 roll-up have no AP segments: the non-tourist total
# is the input, and the run-rate is computed from it.
FIELD_DEPENDENTS_SHORT = {
    k: v for k, v in FIELD_DEPENDENTS.items()
    if not k.startswith(("ap_", "aprr_"))} | {
    "ap_nontourist": ["aprr_nontourist", "ach_mou", "growth_mou"],
    "aprr_nontourist": ["ach_mou", "growth_mou"],
}
SHORT_GRIDS = ("shop_ph2", "province_ph2", "region_ph2", "province_all",
               "region_all")

# The MOU measure each scope reports, for labels.
SCOPES = ("all", "ph1", "ph2")
SCOPE_AP_LABEL = {"ph1": "AP1D200", "ph2": "AP1D250", "all": "AP1D200/250"}

# A baseline difference is explained as "Pivot Base not refreshed" only if it
# equals the baseline of at most this many off-roster shops in that province.
STALE_BASELINE_MAX_SHOPS = 3

# How much current-month GA may sit on shops that are not on the roster before
# the build refuses (they count in Summary's province SUMIFS but in no By Shop
# row).
OFF_ROSTER_GA_FAIL_SHARE = 0.05
