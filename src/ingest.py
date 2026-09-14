"""Read the True Friend workbook into tidy frames.

Two binding strategies, and the choice is not stylistic:

  * FEEDS (Data Total, the Target roster) are bound by HEADER NAME. Column
    order is not part of the contract.
  * HAND-EDITED GRIDS (By Shop, Summary) are read POSITIONALLY, with every
    position guarded by an expected label -- their headers repeat ("Mass" four
    times) so no name lookup can resolve them.

The workbook is opened twice: once for cached VALUES, once for FORMULAS on the
two grids only. The formulas are how a cell that was typed differently from the
rest of its column (a mistyped reference, a deleted SUMIFS) is told apart from
a real disagreement -- see `formula_deviations`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from . import config as C


class SchemaError(RuntimeError):
    """The workbook no longer matches the contract in config.py."""


# --- cell helpers -----------------------------------------------------------


def col_to_idx(col: str) -> int:
    """'AB' -> 27 (0-based)."""
    return C._col_idx(col)


def idx_to_col(idx: int) -> str:
    return C._idx_col(idx)


def norm(v) -> str:
    """Collapse whitespace so ' Baseline GA' compares cleanly."""
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _grid(ws) -> list[list]:
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _cell(grid: list[list], col: str, row: int):
    """1-based row, spreadsheet column letter. Out of range -> None."""
    r, c = row - 1, col_to_idx(col)
    if r < 0 or r >= len(grid) or c >= len(grid[r]):
        return None
    return grid[r][c]


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def month_key(label: str) -> str | None:
    """"Aug'26" -> "2026-08". None if it is not a month label."""
    m = C.MONTH_LABEL_RE.match(norm(label))
    if not m or m.group("mon").title() not in C.MONTH_ABBR:
        return None
    return f"20{m.group('yy')}-{C.MONTH_ABBR.index(m.group('mon').title()) + 1:02d}"


@dataclass
class Source:
    """Everything the build needs, already extracted from the .xlsx."""

    path: Path
    sheet_names: list[str]
    data: pd.DataFrame            # Data Total, one row per month x shop
    target: pd.DataFrame          # the roster, every row with a Combine Code
    shop_sheet: pd.DataFrame      # By Shop cached values
    province_sheet: pd.DataFrame  # Summary province grid cached values
    region_sheet: pd.DataFrame    # Summary region block cached values
    control: dict                 # days_elapsed, days_in_month, month_label
    shop_control: dict            # the same three, as By Shop sees them
    layouts: dict[str, str]       # grid -> generation name
    # (grid, key, field) -> "the cell's formula" for cells whose formula
    # differs from their column's. Excused from reconciliation, warned about.
    deviations: dict[tuple[str, str, str], str] = field(default_factory=dict)
    # Numbers stored as text in Data Total: [(cell, header, text)].
    text_numbers: list[tuple[str, str, str]] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)


# --- sheet resolution -------------------------------------------------------


def _resolve(names: list[str], wanted: str, path: Path) -> str:
    """The actual sheet name for `wanted`, matched on the TRIMMED name.

    A stray trailing space from Excel's rename box is a typo, never a second
    sheet; two sheets that trim to the same name are a coin flip, so they raise.
    """
    if wanted.strip() in {n.strip() for n in C.FORBIDDEN_SOURCE_SHEETS}:
        raise SchemaError(f"{wanted!r} is on FORBIDDEN_SOURCE_SHEETS.")
    found = [n for n in names if n.strip() == wanted.strip()]
    if not found:
        raise SchemaError(f"{path.name}: required sheet {wanted!r} is missing. "
                          f"Sheets present: {names}")
    if len(found) > 1:
        raise SchemaError(f"{path.name}: sheet name collision -- {found!r} "
                          f"differ only by whitespace.")
    return found[0]


# --- feeds: bound by header name --------------------------------------------


def _header_index(header_row: list, label: str, wanted: dict[str, str],
                  optional: dict[str, str] | None = None
                  ) -> tuple[dict[str, int], list[str]]:
    positions: dict[str, list[int]] = {}
    for i, v in enumerate(header_row):
        if v is not None:
            positions.setdefault(norm(v), []).append(i)
    idx, missing_optional = {}, []
    for group, required in ((wanted, True), (optional or {}, False)):
        for src, out in group.items():
            hits = positions.get(norm(src), [])
            if not hits:
                if required:
                    raise SchemaError(
                        f"{label}: expected column {src!r} not found in the "
                        f"header row.\n  Present: {sorted(positions)}")
                missing_optional.append(src)
                continue
            if len(hits) > 1:
                # "Cost Value" and "Remark" appear twice on the roster. Binding
                # either by name would be a guess, so a duplicated WANTED header
                # is refused rather than resolved.
                raise SchemaError(
                    f"{label}: header {src!r} appears {len(hits)} times "
                    f"(columns {[idx_to_col(h) for h in hits]}); cannot bind "
                    f"it by name.")
            idx[out] = hits[0]
    return idx, missing_optional


def read_data_total(grid: list[list], label: str
                    ) -> tuple[pd.DataFrame, list[tuple[str, str, str]]]:
    if not grid:
        raise SchemaError(f"{label}: sheet is empty.")
    idx, _ = _header_index(grid[0], label, C.DATA_COLS)
    out_to_src = {v: k for k, v in C.DATA_COLS.items()}
    rows, text_numbers = [], []
    for r, raw in enumerate(grid[1:], start=2):
        if raw is None or all(v is None for v in raw):
            continue
        rec = {"xl_row": r}
        for out, i in idx.items():
            v = raw[i] if i < len(raw) else None
            if out in C.DATA_NUMERIC:
                if _is_number(v):
                    rec[out] = float(v)
                else:
                    # Blank and text both contribute 0 -- exactly what SUMIFS
                    # and a pivot Sum do. Text that LOOKS numeric is recorded,
                    # because it is a value the report silently drops.
                    if v is not None and norm(v) != "":
                        text_numbers.append((f"{idx_to_col(i)}{r}",
                                             out_to_src[out], norm(v)))
                    rec[out] = 0.0
            elif out in C.DATA_TEXT:
                rec[out] = norm(v)
            else:
                rec[out] = None if v is None else norm(v)
        if not rec["month_label"] and not rec["code"]:
            continue
        rows.append(rec)
    df = pd.DataFrame(rows)
    df["month"] = df["month_label"].map(month_key)
    return df, text_numbers


def read_target(grid: list[list], label: str) -> tuple[pd.DataFrame, list[str]]:
    header = grid[C.TARGET_HEADER_ROW - 1] if len(grid) >= C.TARGET_HEADER_ROW else []
    idx, missing = _header_index(header, label, C.TARGET_COLS,
                                 C.TARGET_OPTIONAL_COLS)
    rows = []
    for r, raw in enumerate(grid[C.TARGET_HEADER_ROW:], start=C.TARGET_HEADER_ROW + 1):
        rec = {"xl_row": r}
        for out, i in idx.items():
            v = raw[i] if i < len(raw) else None
            if out in C.TARGET_NUMERIC:
                rec[out] = float(v) if _is_number(v) else 0.0
            elif out == "decorated_month":
                rec[out] = pd.Timestamp(v) if hasattr(v, "year") else None
            else:
                rec[out] = norm(v)
        # A real roster row has a Combine Code; drag-filled dropdown rows below
        # the list do not. The one formula-built code (=C40&"-"&D40) reads "-"
        # when both parts are blank.
        if rec.get("code", "").strip("- ") == "":
            continue
        rows.append(rec)
    for out in C.TARGET_OPTIONAL_COLS.values():
        if out not in idx:
            for rec in rows:
                rec[out] = None
    return pd.DataFrame(rows), missing


# --- hand-edited grids: positional, every position guarded -------------------


def read_grid(grid: list[list], label: str, layouts) -> tuple[pd.DataFrame, dict, C.GridLayout]:
    """Read a hand-edited grid under the first layout whose guards all pass."""
    attempts = []
    for lay in layouts:
        bad = [(col, row, exp, norm(_cell(grid, col, row)))
               for col, row, exp in
               [(c, r, e) for c, r, e in lay.fields.values()] + lay.banners
               if norm(_cell(grid, col, row)) != exp]
        if bad:
            col, row, exp, got = bad[0]
            attempts.append(f"layout {lay.name}: {label}!{col}{row} expected "
                            f"{exp!r}, found {got!r}"
                            + (f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""))
            continue
        rows = []
        for r in range(lay.first_data_row, lay.max_data_row + 1):
            key = norm(_cell(grid, lay.key_col, r))
            if not key:
                continue
            rec = {"xl_row": r}
            for name, (col, _g, _e) in lay.fields.items():
                v = _cell(grid, col, r)
                if name in ("region", "province", "code", "shop_name", "zone",
                            "phase", "remark"):
                    rec[name] = norm(v)
                else:
                    # Keep the cached value as-is; reconciliation decides what
                    # a blank or an error string means for that field.
                    rec[name] = v
            rows.append(rec)
        if not rows:
            raise SchemaError(f"{label}: no data rows in "
                              f"{lay.first_data_row}:{lay.max_data_row}.")
        control = {k: _cell(grid, *_split(ref)) for k, ref in lay.control.items()}
        return pd.DataFrame(rows), control, lay
    raise SchemaError(
        f"{label}: no layout matched.\n  - " + "\n  - ".join(attempts) +
        "\nIf the sheet was genuinely restructured, ADD a new layout in "
        "config.py rather than editing the existing one.")


def _split(ref: str) -> tuple[str, int]:
    m = re.match(r"^([A-Z]+)(\d+)$", ref)
    return m.group(1), int(m.group(2))


def formula_deviations(fgrid: list[list], label: str, lay: C.GridLayout,
                       keys: dict[int, str]) -> dict[tuple[str, str, str], str]:
    """Cells whose formula differs from the rest of their column.

    Each formula is normalised by replacing its own row number with {r}, so
    `=R33-J33` and `=R5-J5` compare equal. The most common shape in a column is
    canonical; any other shape -- a different formula, a pasted constant, a
    blank where every neighbour has a SUMIFS -- is a deviation. Our
    recomputation implements the canonical formula, so these are exactly the
    cells where we should expect to disagree with the sheet.
    """
    out = {}
    for name, (col, _g, _e) in lay.fields.items():
        if name not in C.RECONCILE_FIELDS:
            continue
        shapes: dict[str, list[int]] = {}
        for r in keys:
            v = _cell(fgrid, col, r)
            if isinstance(v, str) and v.startswith("="):
                shape = re.sub(rf"(?<=[A-Z]){r}(?!\d)", "{r}", v)
            else:
                shape = f"<value {v!r}>"
            shapes.setdefault(shape, []).append(r)
        if len(shapes) < 2:
            continue
        canonical = max(shapes, key=lambda s: len(shapes[s]))
        for shape, rows in shapes.items():
            if shape == canonical:
                continue
            for r in rows:
                v = _cell(fgrid, col, r)
                out[(label, keys[r], name)] = (
                    f"{col}{r} is {v!r}" if not (isinstance(v, str) and v.startswith("="))
                    else f"{col}{r} is {v}") + f"; column is {canonical}"
    return out


# --- entry point ------------------------------------------------------------


def read_workbook(path: str | Path) -> Source:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such workbook: {path}")

    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        names = list(wb.sheetnames)
        s_data = _resolve(names, C.SHEET_DATA, path)
        s_target = _resolve(names, C.SHEET_TARGET, path)
        s_summary = _resolve(names, C.SHEET_SUMMARY, path)
        s_shop = _resolve(names, C.SHEET_SHOP, path)
        data, text_numbers = read_data_total(_grid(wb[s_data]), s_data)
        target, missing = read_target(_grid(wb[s_target]), s_target)
        g_summary, g_shop = _grid(wb[s_summary]), _grid(wb[s_shop])
    finally:
        wb.close()

    shop, shop_control, lay_shop = read_grid(g_shop, s_shop, C.SHOP_LAYOUTS)
    prov, control, lay_prov = read_grid(g_summary, f"{s_summary} (province)",
                                        C.SUMMARY_PROVINCE_LAYOUTS)
    region, _, lay_reg = read_grid(g_summary, f"{s_summary} (region)",
                                   C.SUMMARY_REGION_LAYOUTS)

    # Second pass, formulas only, and only for the two grids.
    wbf = load_workbook(path, data_only=False, read_only=True)
    try:
        f_shop, f_summary = _grid(wbf[s_shop]), _grid(wbf[s_summary])
    finally:
        wbf.close()
    deviations = {}
    deviations.update(formula_deviations(
        f_shop, "shop", lay_shop, dict(zip(shop["xl_row"], shop["code"]))))
    deviations.update(formula_deviations(
        f_summary, "province", lay_prov,
        dict(zip(prov["xl_row"], prov["province"]))))

    return Source(
        path=path, sheet_names=names, data=data, target=target,
        shop_sheet=shop, province_sheet=prov, region_sheet=region,
        control=control, shop_control=shop_control,
        layouts={"shop": lay_shop.name, "province": lay_prov.name,
                 "region": lay_reg.name},
        deviations=deviations, text_numbers=text_numbers,
        missing_optional=missing,
    )
