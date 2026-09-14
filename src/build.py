"""Orchestrator: xlsx -> recompute -> validate -> parquet.

    python -m src.build                     # every published period
    python -m src.build path/to/one.xlsx    # exactly one snapshot

Exits non-zero if any check fails, and writes NOTHING in that case.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config as C
from . import validate as V
from .ingest import SchemaError, read_workbook
from .metrics import (build_monthly, build_province, build_region, build_shop,
                      report_month)

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "data" / "processed"
META_PATH = OUT_DIR / "meta.json"
TABLES = ("shop", "province", "region", "monthly")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_raw() -> tuple[list[tuple[str, bool, Path]], list[str]]:
    """Every convention-named snapshot as (iso_date, is_final, path)."""
    dated, skipped = [], []
    for p in sorted(RAW_DIR.glob("*.xlsx")):
        if p.name.startswith("~$"):
            continue
        if m := C.SNAPSHOT_NAME_RE.match(p.name):
            iso = f"{m['year']}-{m['month']}-{m['day']}"
            dated.append((iso, bool(m["final"]), p))
        else:
            skipped.append(p.name)
    # Sorted on the ISO date, NOT the filename: "11-09-2026" sorts before
    # "31-08-2026" as text, which would make August the newest working copy.
    dated.sort(key=lambda t: (t[0], t[1]))
    return dated, skipped


def select_snapshots() -> tuple[list[tuple[Path, bool]], list[str]]:
    """One period per month: every -Final, plus the newest working copy."""
    dated, skipped = scan_raw()
    if not dated:
        raise SystemExit(
            f"No workbook matching {C.SNAPSHOT_EXAMPLE} in {RAW_DIR}/."
            + (f"\nIgnored (name does not match): {skipped}" if skipped else ""))
    finals = [(d, p, True) for d, f, p in dated if f]
    working = [(d, p, False) for d, f, p in dated if not f]
    chosen = finals + (working[-1:] if working else [])
    chosen.sort(key=lambda t: t[0])
    return [(p, f) for _d, p, f in chosen], skipped


def build_one(src_path: Path, is_final: bool) -> dict | None:
    print(f"\n{'=' * 78}\nsource   {src_path.name}"
          f"{'   [FINAL]' if is_final else '   [working copy]'}")
    try:
        src = read_workbook(src_path)
    except SchemaError as exc:
        print(f"\nSCHEMA ERROR\n{exc}\n", file=sys.stderr)
        return None

    label, period = report_month(src)
    de, dm = src.control.get("days_elapsed"), src.control.get("days_in_month")
    print(f"report   {label} -> period {period} | data through day {de} of {dm}")
    print(f"feeds    Data Total {len(src.data):,} rows | roster "
          f"{len(src.target)} rows | layouts {src.layouts}")

    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": src_path.name,
        "source_sha256": _sha256(src_path),
        "report_version": (name_m["version"] if (name_m := C.SNAPSHOT_NAME_RE.match(
            src_path.name)) else None),
        "period": period,
        "month_label": label,
        "days_elapsed": de,
        "days_in_month": dm,
        "layouts": src.layouts,
        "sheets": src.sheet_names,
        "months_in_data": sorted(src.data["month"].dropna().unique().tolist()),
        "baseline": {"months": list(C.BASELINE_MONTHS),
                     "divisor": C.BASELINE_DIVISOR,
                     "phase_prefix": C.BASELINE_PHASE_PREFIX},
    }

    try:
        shop = build_shop(src)
        province = build_province(src)
        region = build_region(province)
        monthly = build_monthly(src)
    except (KeyError, ValueError, TypeError) as exc:
        # Arithmetic on a malformed period (e.g. day counts that are text)
        # is reported by the period check below; do not publish either way.
        import traceback
        traceback.print_exc()
        print(f"\ncould not compute metrics: {exc!r}", file=sys.stderr)
        shop = province = region = monthly = None

    meta.update({"n_shops": int(len(shop)) if shop is not None else None,
                 "n_provinces": int((province["n_shops"] > 0).sum())
                 if province is not None else None})

    print("\nvalidation")
    if shop is None:
        rep = V.Report()
        V.check_period(src, rep, is_final)
        rep.fail("metrics could not be computed")
        recon = pd.DataFrame()
    else:
        rep, recon = V.run_all(src, meta, META_PATH, is_final, shop, province,
                               region)
    print(rep.render())

    if not recon.empty:
        print("\nreconciliation vs the sheet's own cached values "
              "(fields with any difference)")
        show = recon[(recon["max_abs_diff"] > 0) | (recon["n_mismatch"] > 0)
                     | (recon["n_excused"] > 0)]
        cells = {g: len(src.shop_sheet if g == "shop" else src.province_sheet
                        if g == "province" else src.region_sheet)
                 for g in recon["grid"].unique()}
        print("  rows compared: " + ", ".join(f"{g} {n}" for g, n in cells.items())
              + f" | fields each: {recon.groupby('grid').size().max()}")
        if show.empty:
            print("  every field matches exactly")
        else:
            with pd.option_context("display.width", 110):
                print(show.to_string(index=False,
                                     float_format=lambda v: f"{v:,.6g}"))

    return {"src": src_path, "period": period, "is_final": is_final,
            "shop": shop, "province": province, "region": region,
            "monthly": monthly, "meta": meta, "rep": rep}


def _tag(df: pd.DataFrame, period: str, is_final: bool) -> pd.DataFrame:
    out = df.copy()
    out.insert(0, "period", period)
    out.insert(1, "is_final", is_final)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", nargs="?",
                    help="path to one .xlsx (default: every published period)")
    ap.add_argument("--force", action="store_true",
                    help="write outputs even if validation fails "
                         "(to INSPECT a broken snapshot, never to ship one)")
    args = ap.parse_args(argv)

    if args.source:
        one = Path(args.source)
        m = C.SNAPSHOT_NAME_RE.match(one.name)
        selected, skipped = [(one, bool(m and m["final"]))], []
    else:
        selected, skipped = select_snapshots()
    if skipped:
        print(f"note     ignoring {len(skipped)} off-convention file(s) in "
              f"data/raw: {skipped}")
    print(f"publishing {len(selected)} snapshot(s): "
          + ", ".join(f"{p.name}{' [FINAL]' if f else ''}" for p, f in selected))

    built = []
    for path, is_final in selected:
        b = build_one(path, is_final)
        if b is None:
            return 2
        built.append(b)

    by_period: dict[str, dict] = {}
    for b in built:
        prev = by_period.get(b["period"])
        if prev is None or (b["is_final"] and not prev["is_final"]):
            if prev is not None:
                print(f"\nnote     {prev['src'].name} also covers {b['period']}; "
                      f"{b['src'].name} is finalised and supersedes it.")
            by_period[b["period"]] = b
        elif prev["is_final"] and not b["is_final"]:
            print(f"\nnote     {b['src'].name} covers {b['period']}, already "
                  f"closed by {prev['src'].name}. Not published.")
        elif prev["is_final"] and b["is_final"]:
            print(f"\nFAIL     two -Final snapshots cover {b['period']}: "
                  f"{prev['src'].name} and {b['src'].name}.", file=sys.stderr)
            return 1
    published = [by_period[k] for k in sorted(by_period)]

    if (failures := [b for b in published if not b["rep"].ok]) and not args.force:
        for b in failures:
            print(f"\n{b['period']}: {len(b['rep'].failures)} failure(s)",
                  file=sys.stderr)
        print("\nBUILD FAILED - nothing written.", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in TABLES:
        frames = [_tag(b[name], b["period"], b["is_final"]) for b in published
                  if b[name] is not None]
        pd.concat(frames, ignore_index=True).to_parquet(
            OUT_DIR / f"{name}.parquet", index=False)

    meta = dict(published[-1]["meta"])
    meta["periods"] = {
        b["period"]: {**b["meta"], "is_final": b["is_final"],
                      "validation": {"failures": b["rep"].failures,
                                     "warnings": b["rep"].warnings,
                                     "notes": b["rep"].notes}}
        for b in published
    }
    meta["period_list"] = [b["period"] for b in published]
    meta["current_period"] = published[-1]["period"]
    META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False,
                                    default=str))

    print(f"\nwrote -> {OUT_DIR}")
    for p in sorted(OUT_DIR.glob("*")):
        print(f"  {p.name:<22} {p.stat().st_size / 1024:>8,.0f} KB")
    print("\nperiods published:")
    for b in published:
        print(f"  {b['period']}  {'FINAL  ' if b['is_final'] else 'working'}"
              f"  {b['src'].name}")
    if any(not b["rep"].ok for b in published):
        print("\n(--force used: outputs written despite failures)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
