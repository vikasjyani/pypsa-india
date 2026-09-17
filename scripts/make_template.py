"""Create a blank scenario workbook from the active configuration.

The workbook contains the sheets selected by `components:`, their required
dependencies, a README sheet, units, column guidance, and dropdowns. It does
not overwrite an existing workbook unless `--merge` or `--force` is supplied.

Run standalone:
    python scripts/make_template.py [--scenario all_india] [--merge|--force]
"""

import argparse
# Snakemake adds a preamble before this script, so a future import would be invalid.
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import regions as G
import registry as R
from _helpers import (ROOT, config_carriers, config_loads, config_years,
                      configure_logging, load_config)
from errors import InputValidationError

try:  # openpyxl ships with pandas' excel extra; formatting degrades without it
    from openpyxl.comments import Comment
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    HAVE_OPENPYXL = True
except ImportError:  # pragma: no cover
    HAVE_OPENPYXL = False


HEADER_FILL = "FFEDF1F3"
UNIT_FONT_COLOR = "FF6B7A83"
FILL_ME_FILL = "FFF6DDE4"


# ------------------------------------------------------------------ content


def loads_frame(spatial: Mapping, loads: Sequence[str]) -> pd.DataFrame:
    """One row per load point, pre-filled.

    With no `loads:` in config the default is one load per electricity bus,
    named after it — so `Demand_NR` sits beside bus `NR`. Naming them in config
    overrides that; the bus then has to be filled in by hand.
    """
    buses = G.generate_buses(spatial)
    electricity = buses.loc[buses["role"].eq("electricity"), "name"].tolist()
    if loads:
        rows = [
            {"name": name, "bus": name if name in electricity else R.FILL_ME,
             "carrier": "AC", "active": True}
            for name in loads
        ]
    else:
        rows = [
            {"name": bus, "bus": bus, "carrier": "AC", "active": True}
            for bus in electricity
        ]
    return pd.DataFrame(rows, columns=["name", "bus", "carrier", "active"])


def sheet_frame(
    sheet: R.Sheet, years: Sequence[int], carriers: Sequence[str],
    spatial: Mapping, loads: Sequence[str] = ()
) -> pd.DataFrame:
    """One sheet's starting content: identity columns filled, values stubbed."""
    if sheet.name == "Regions":
        return G.regions_table()
    if sheet.name == "Buses":
        return G.generate_buses(spatial)
    if sheet.name == "Loads":
        return loads_frame(spatial, loads)

    columns = sheet.column_names(years=years, carriers=carriers)

    # Pre-fill identity columns for rows derived from the configuration.
    rows: list[dict[str, Any]] = []
    if sheet.name in ("P_max_pu", "P_min_pu", "Demand") or sheet.name.startswith(
            R.DEMAND_PREFIX):
        rows = []  # Time-series values are supplied as complete profiles.
    elif sheet.carrier_columns is False and "carrier" in [c.name for c in sheet.columns]:
        for carrier in carriers:
            row = {column: R.FILL_ME for column in columns}
            row["carrier"] = carrier
            if "TECHNOLOGY" in row:
                row["TECHNOLOGY"] = carrier
            if "Type" in row:
                row["Type"] = "Generator"
            rows.append(row)

    frame = pd.DataFrame(rows, columns=columns)
    for col in sheet.columns:
        if col.default is not None and col.name in frame:
            frame[col.name] = col.default
    return frame


def units_row(sheet: R.Sheet, columns: Sequence[str], years: Sequence[int]) -> list[str]:
    lookup = {col.name: col for col in sheet.columns}
    out: list[str] = []
    for column in columns:
        col = lookup.get(column)
        if col is not None:
            out.append(col.unit or col.dtype)
        elif str(column).isdigit():
            out.append("MW" if "p_min" in sheet.name or "p_max" in sheet.name else "value")
        else:
            out.append("p.u.")
    return out


def readme_frame(config: Mapping, sheets: Sequence[R.Sheet]) -> pd.DataFrame:
    scenario = config.get("scenario", {})
    meta = [
        ("Scenario", scenario.get("name", "")),
        ("Generated for years", ", ".join(str(y) for y in config_years(config))),
        ("Mode", config.get("mode", "")),
        ("Family", config.get("family", "")),
        ("Spatial boundary", str(config.get("spatial", {}).get("boundary", ""))),
        ("Currency", scenario.get("currency", "")),
        ("", ""),
        ("HOW TO FILL", f"Replace every {R.FILL_ME!r} cell. Row 2 of each sheet"),
        ("", "gives units and is ignored on load — do not delete it."),
        ("", ""),
        ("SHEETS", ""),
    ]
    rows = [{"Item": key, "Value": value} for key, value in meta]
    for sheet in sheets:
        rows.append(
            {
                "Item": sheet.name,
                "Value": f"[{'default' if sheet.default else 'optional'}] "
                f"{sheet.help or sheet.target}",
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------- formatting


def decorate(writer: Any, sheet: R.Sheet, frame: pd.DataFrame, units: Sequence[str],
             carriers: Sequence[str]) -> None:
    """Header styling, units row, comments, dropdowns, placeholder highlight."""
    if not HAVE_OPENPYXL:
        return
    worksheet = writer.sheets[sheet.name]
    lookup = {col.name: col for col in sheet.columns}
    n_cols = len(frame.columns)

    # Insert the units row directly below the header.
    worksheet.insert_rows(2)
    for index, unit in enumerate(units, start=1):
        cell = worksheet.cell(row=2, column=index, value=unit)
        cell.font = Font(italic=True, size=9, color=UNIT_FONT_COLOR)

    for index, column in enumerate(frame.columns, start=1):
        cell = worksheet.cell(row=1, column=index)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = Alignment(vertical="center", wrap_text=False)

        col = lookup.get(str(column))
        if col is not None:
            note = [
                f"{col.name} ({col.dtype})",
                "required" if col.required else "optional",
            ]
            if col.unit:
                note.append(f"unit: {col.unit}")
            if col.ge is not None or col.le is not None:
                note.append(f"range: {col.ge if col.ge is not None else '-inf'}"
                            f" .. {col.le if col.le is not None else 'inf'}")
            if col.fk:
                note.append(f"must exist in: {col.fk}")
            if col.help:
                note.append(col.help)
            cell.comment = Comment("\n".join(note), "pypsa-india")

        width = max(len(str(column)) + 2, 12)
        worksheet.column_dimensions[get_column_letter(index)].width = min(width, 32)

    worksheet.freeze_panes = "A3"

    # Add dropdowns for enumerated columns.
    for index, column in enumerate(frame.columns, start=1):
        col = lookup.get(str(column))
        choices = None
        if col is not None and col.choices:
            choices = col.choices
        elif col is not None and col.fk == "carriers" and carriers:
            choices = tuple(carriers)
        if not choices:
            continue
        joined = ",".join(str(c) for c in choices)
        if len(joined) > 250:  # Excel's inline-list limit
            continue
        letter = get_column_letter(index)
        validation = DataValidation(
            type="list", formula1=f'"{joined}"', allow_blank=True, showDropDown=False
        )
        worksheet.add_data_validation(validation)
        validation.add(f"{letter}3:{letter}1000")

    # Highlight unfilled template cells.
    if n_cols:
        span = f"A3:{get_column_letter(n_cols)}1000"
        worksheet.conditional_formatting.add(
            span,
            CellIsRule(
                operator="equal",
                formula=[f'"{R.FILL_ME}"'],
                fill=PatternFill("solid", bgColor=FILL_ME_FILL),
            ),
        )


# ------------------------------------------------------------------- merge


def merge_existing(new: pd.DataFrame, old: pd.DataFrame, sheet: R.Sheet) -> pd.DataFrame:
    """Keep existing rows and retain only columns in the current schema.

    Rows are carried as whole records. New columns receive the template
    placeholder when the existing sheet has the declared key columns.
    """
    if old.empty:
        return new
    if not sheet.key or not set(sheet.key) <= set(old.columns):
        return old.reindex(columns=new.columns) if len(old) else new

    keep = [c for c in new.columns if c in old.columns]
    carried = old[keep].copy()
    for column in new.columns:
        if column not in carried:
            carried[column] = R.FILL_ME
    return carried[new.columns]


# ------------------------------------------------------------------ writing


def write_template(
    config: Mapping, path: Path, *, merge: bool = False, force: bool = False,
    logger=None,
) -> list[str]:
    years = config_years(config)
    carriers = config_carriers(config)
    spatial = config.get("spatial", {}) or {}
    # Each declared load has a Demand_<name> sheet. Without declared loads,
    # create one load for each electricity bus.
    loads = config_loads(config)
    if not loads:
        buses = G.generate_buses(spatial)
        loads = buses.loc[buses["role"].eq("electricity"), "name"].astype(str).tolist()
    sheets = R.resolve_sheets(
        config.get("components", {}) or {},
        snapshots_select=str(config.get("snapshots", {}).get("select", "all")),
        loads=loads,
    )

    if path.exists() and not (merge or force):
        raise InputValidationError(
            f"{path} already exists. Re-running would destroy whatever is in it.\n"
            f"  --merge  keep every filled cell whose column still exists\n"
            f"  --force  overwrite (the old file is kept as {path.name}.bak)"
        )

    existing: dict[str, pd.DataFrame] = {}
    if path.exists():
        if force:
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_bytes(path.read_bytes())
            if logger:
                logger.info("backed up to %s", backup.name)
        if merge:
            book = pd.ExcelFile(path)
            existing = {
                name: pd.read_excel(book, name, skiprows=[1])
                for name in book.sheet_names
            }

    written: list[str] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        readme_frame(config, sheets).to_excel(writer, sheet_name="README", index=False)
        for sheet in sheets:
            frame = sheet_frame(sheet, years, carriers, spatial, loads)
            if merge and sheet.name in existing:
                frame = merge_existing(frame, existing[sheet.name], sheet)
            frame.to_excel(writer, sheet_name=sheet.name, index=False)
            decorate(
                writer, sheet, frame,
                units_row(sheet, list(frame.columns), years),
                carriers,
            )
            written.append(sheet.name)

        if merge:
            for name, frame in existing.items():
                if name not in written and name != "README":
                    frame.to_excel(writer, sheet_name=name, index=False)
                    if logger:
                        logger.warning(
                            "%s is no longer selected but was filled in — kept. "
                            "Remove it by hand if that is intended.", name,
                        )
    return written


def main(snakemake: Any) -> None:
    logger = configure_logging(snakemake)
    config = snakemake.config
    path = Path(snakemake.output.template)
    params = getattr(snakemake, "params", None)
    written = write_template(
        config, path,
        merge=bool(getattr(params, "merge", False)),
        force=bool(getattr(params, "force", False)),
        logger=logger,
    )
    logger.info("wrote %s with %d sheet(s): %s", path, len(written), ", ".join(written))


snakemake: Any = globals().get("snakemake")
if __name__ == "__main__":
    if snakemake is None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--scenario", default="all_india")
        parser.add_argument("--merge", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--out", default=None)
        args = parser.parse_args()

        from _helpers import mock_snakemake

        snakemake = mock_snakemake("make_template", scenario=args.scenario)
        snakemake.params.merge = args.merge
        snakemake.params.force = args.force
        if args.out:
            snakemake.output.template = args.out
    main(snakemake)
