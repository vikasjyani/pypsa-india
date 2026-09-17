"""Validate a filled workbook against the registry.

All checks accumulate before validation raises, so workbook issues can be
corrected together.
"""

# Snakemake adds a preamble before this script, so a future import would be invalid.
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import registry as R
import regions as G
from _helpers import config_carriers, config_years, configure_logging, workbook_path
from errors import InputValidationError

ERROR, WARNING = "ERROR", "WARNING"


@dataclass(frozen=True)
class Finding:
    level: str
    sheet: str
    message: str
    cells: tuple[str, ...] = ()

    def __str__(self) -> str:
        where = f" [{', '.join(self.cells[:6])}{'…' if len(self.cells) > 6 else ''}]" if self.cells else ""
        return f"{self.level:7} {self.sheet:20} {self.message}{where}"


class Report:
    """Accumulates findings; raises once, at the end."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def error(self, sheet: str, message: str, cells: Iterable[str] = ()) -> None:
        self.findings.append(Finding(ERROR, sheet, message, tuple(cells)))

    def warn(self, sheet: str, message: str, cells: Iterable[str] = ()) -> None:
        self.findings.append(Finding(WARNING, sheet, message, tuple(cells)))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == WARNING]

    def text(self) -> str:
        lines = [
            "pypsa-india input validation",
            "=" * 60,
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)",
            "",
        ]
        lines += [str(f) for f in self.findings] or ["(nothing to report)"]
        if not self.errors:
            lines += ["", "PASS — the workbook can be built."]
        else:
            lines += ["", "FAIL — fix the errors above and re-run."]
        return "\n".join(lines)

    def raise_if_failed(self) -> None:
        if self.errors:
            raise InputValidationError(
                f"{len(self.errors)} validation error(s):\n"
                + "\n".join(str(f) for f in self.errors)
            )


# --------------------------------------------------------------- utilities


def excel_cell(row_index: int, column: str) -> str:
    """A1-ish reference: header is row 1, so dataframe row 0 is row 2."""
    return f"{column}@row{row_index + 2}"


def resolve_columns(frame: pd.DataFrame, sheet: R.Sheet) -> pd.DataFrame:
    """Rename accepted aliases to the registry's own column names."""
    rename: dict[str, str] = {}
    present = {str(c) for c in frame.columns}
    for col in sheet.columns:
        if col.name in present:
            continue
        for alias in col.aliases:
            if alias in present:
                rename[alias] = col.name
                break
            if alias == "":
                unnamed = [c for c in frame.columns if str(c).startswith("Unnamed")]
                if unnamed:
                    rename[str(unnamed[0])] = col.name
                    break
    return frame.rename(columns=rename)


def looks_like_units_row(frame: pd.DataFrame, sheet: R.Sheet) -> bool:
    """Return whether row 0 is the generated template's units row.

    Generated workbooks place units below the header; manually prepared
    workbooks do not. Detection compares the first row with the units expected
    from the registry, including dynamic carrier and year columns.
    """
    if frame.empty:
        return False
    lookup = {col.name: col for col in sheet.columns}
    expected: dict[str, set[str]] = {}
    for column in frame.columns:
        col = lookup.get(str(column))
        if col is not None:
            expected[column] = {str(col.unit).strip().lower(), col.dtype}
        else:
            # Dynamic column: a per-carrier profile or per-year value.
            expected[column] = {"p.u.", "mw", "mwh", "value", "float", "int"}

    first = frame.iloc[0]
    matched = mismatched = 0
    for column, value in first.items():
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        token = str(value).strip().lower()
        if not token:
            continue
        if token in {t for t in expected.get(column, set()) if t}:
            matched += 1
        elif not pd.isna(pd.to_numeric(value, errors="coerce")):
            # A numeric value in row 0 is data.
            return False
        else:
            mismatched += 1
    return matched > 0 and matched >= mismatched


def drop_blank_key_rows(frame: pd.DataFrame, sheet: R.Sheet) -> pd.DataFrame:
    """Drop separator rows whose key columns are entirely blank.

    Hand-maintained workbooks grow blank spacer rows between blocks. They are
    not data, and treating them as rows produces a wall of "required column is
    blank" noise that buries real findings.
    """
    if not sheet.key or not set(sheet.key) <= set(frame.columns):
        return frame
    keys = frame[list(sheet.key)]
    blank = keys.isna() | keys.astype(str).apply(lambda c: c.str.strip().eq(""))
    return frame.loc[~blank.all(axis=1)].reset_index(drop=True)


def _coerce(series: pd.Series, dtype: str) -> tuple[pd.Series, pd.Series]:
    """Return (coerced, mask_of_failures)."""
    if dtype in ("float", "int"):
        out = pd.to_numeric(series, errors="coerce")
        bad = out.isna() & series.notna() & series.astype(str).str.strip().ne("")
        return out, bad
    if dtype == "bool":
        mapping = {
            "true": True, "1": True, "yes": True, "y": True, "t": True,
            "false": False, "0": False, "no": False, "n": False, "f": False,
        }
        as_text = series.astype(str).str.strip().str.lower()
        out = as_text.map(mapping)
        bad = out.isna() & series.notna() & as_text.ne("") & as_text.ne("nan")
        return out, bad
    if dtype == "date":
        out = pd.to_datetime(series, errors="coerce")
        bad = out.isna() & series.notna()
        return out, bad
    return series.astype("object"), pd.Series(False, index=series.index)


# ------------------------------------------------------------------ checks


def check_sheet_set(
    book: Mapping[str, pd.DataFrame], expected: Sequence[R.Sheet], report: Report
) -> None:
    """Structural: the workbook holds exactly the config-implied sheets."""
    present = set(book)
    wanted = {sheet.name for sheet in expected}

    for sheet in expected:
        if sheet.name in present:
            continue
        if sheet.seeded:
            report.warn(
                sheet.name,
                "seeded reference sheet absent; the built-in table is used instead",
            )
        else:
            report.error(
                sheet.name,
                "sheet is required by the config but absent from the workbook",
            )

    for name in sorted(present - wanted):
        if name == "README":
            continue  # our own cover sheet, written by make_template
        if name in R.DROPPED:
            report.warn(name, f"unused sheet — {R.DROPPED[name]}")
        elif name in R.BY_NAME:
            # This sheet is known but is not selected by the scenario.
            report.warn(
                name,
                "sheet is known but not selected by this scenario; it will be "
                "ignored. Enable it under components: (or snapshots.select) to use it",
            )
        else:
            report.warn(name, "sheet is not in the registry; it will be ignored")


def check_columns(frame: pd.DataFrame, sheet: R.Sheet, years, carriers, report) -> None:
    """Structural: required columns exist; year/carrier axes match config."""
    present = {str(c) for c in frame.columns}

    for col in sheet.columns:
        if col.required and col.name not in present:
            report.error(sheet.name, f"missing required column {col.name!r}")

    # Warn when column spellings differ only by case.
    lowered: dict[str, list[str]] = {}
    for column in present:
        lowered.setdefault(column.lower(), []).append(column)
    for _, spellings in lowered.items():
        if len(spellings) > 1:
            report.warn(
                sheet.name,
                f"columns differ only by case: {sorted(spellings)} — pick one",
            )

    if sheet.years == R.REQUIRED:
        found = {int(c) for c in present if str(c).isdigit()}
        missing = sorted(set(years) - found)
        if missing:
            report.error(
                sheet.name,
                f"year columns {missing} are modelled but absent "
                f"(sheet has {sorted(found) or 'none'})",
            )
        extra = sorted(found - set(years))
        if extra:
            report.warn(sheet.name, f"year columns {extra} are not modelled; ignored")

    if sheet.carrier_columns:
        missing = [c for c in carriers if c not in present]
        if missing:
            report.warn(
                sheet.name,
                f"no column for carrier(s) {missing}; "
                f"defaults will be used ({'1.0' if 'max' in sheet.name else '0.0'})",
            )


def check_placeholders(frame: pd.DataFrame, sheet: R.Sheet, report: Report) -> None:
    """Structural: no template placeholder survived into a filled workbook."""
    cells: list[str] = []
    for column in frame.columns:
        series = frame[column].astype(str).str.strip()
        for position in np.flatnonzero(series.eq(R.FILL_ME).to_numpy()):
            cells.append(excel_cell(int(position), str(column)))
    if cells:
        report.error(
            sheet.name,
            f"{len(cells)} cell(s) still contain {R.FILL_ME!r}",
            cells,
        )


def check_types_and_ranges(frame: pd.DataFrame, sheet: R.Sheet, report: Report) -> None:
    """Type coercion and declared bounds, per registry column."""
    for col in sheet.columns:
        if col.name not in frame.columns:
            continue
        series = frame[col.name]
        coerced, bad = _coerce(series, col.dtype)
        if bad.any():
            cells = [excel_cell(int(p), col.name) for p in np.flatnonzero(bad.to_numpy())]
            report.error(
                sheet.name,
                f"{col.name!r} has {int(bad.sum())} value(s) that are not {col.dtype}",
                cells,
            )

        if col.required:
            blank = series.isna() | series.astype(str).str.strip().eq("")
            if blank.any():
                cells = [excel_cell(int(p), col.name) for p in np.flatnonzero(blank.to_numpy())]
                report.error(
                    sheet.name,
                    f"{col.name!r} is required but blank in {int(blank.sum())} row(s)",
                    cells,
                )

        if col.dtype in ("float", "int"):
            numeric = pd.to_numeric(series, errors="coerce")
            if col.ge is not None:
                below = numeric < col.ge
                if below.any():
                    cells = [excel_cell(int(p), col.name) for p in np.flatnonzero(below.to_numpy())]
                    report.error(
                        sheet.name,
                        f"{col.name!r} must be >= {col.ge} "
                        f"({int(below.sum())} row(s) below){_unit(col)}",
                        cells,
                    )
            if col.le is not None:
                above = numeric > col.le
                if above.any():
                    cells = [excel_cell(int(p), col.name) for p in np.flatnonzero(above.to_numpy())]
                    level = report.warn if col.name == "lifetime" else report.error
                    level(
                        sheet.name,
                        f"{col.name!r} must be <= {col.le} "
                        f"({int(above.sum())} row(s) above){_unit(col)}"
                        + (
                            " — this may be a calendar year entered as a duration"
                            if col.name == "lifetime"
                            else ""
                        ),
                        cells,
                    )

        if col.choices:
            values = series.dropna().astype(str).str.strip()
            lowered = {c.lower(): c for c in col.choices}
            unknown = values[~values.str.lower().isin(lowered) & values.ne("")]
            if len(unknown):
                report.error(
                    sheet.name,
                    f"{col.name!r} has value(s) outside {list(col.choices)}: "
                    f"{sorted(set(unknown))[:6]}",
                )
            miscased = sorted(
                {v for v in values if v.lower() in lowered and v != lowered[v.lower()]}
            )
            if miscased:
                report.warn(
                    sheet.name,
                    f"{col.name!r} casing differs from the canonical spelling: "
                    f"{miscased}; values are normalized on load",
                )


def _unit(col: R.Col) -> str:
    return f" [{col.unit}]" if col.unit else ""


def check_keys(frame: pd.DataFrame, sheet: R.Sheet, report: Report) -> None:
    """Referential: keys are unique. Excel makes duplicates trivially easy."""
    if not sheet.key or not set(sheet.key) <= set(frame.columns):
        return
    duplicated = frame.duplicated(subset=list(sheet.key), keep=False)
    if duplicated.any():
        sample = (
            frame.loc[duplicated, list(sheet.key)]
            .astype(str)
            .agg(" / ".join, axis=1)
            .unique()[:5]
        )
        # Lookup sheets use the first matching row; asset rows require unique keys.
        emit = report.error if sheet.shape == "asset_list" else report.warn
        emit(
            sheet.name,
            f"{int(duplicated.sum())} row(s) share a key {list(sheet.key)}: "
            f"{list(sample)}" + (" — asset identifiers must be unique" if sheet.shape == "asset_list"
                                else " — the first match wins, the rest are ignored"),
        )


def check_references(book, expected, config, report: Report) -> None:
    """Referential integrity across sheets and against the config."""
    carriers = set(config_carriers(config))
    buses = book.get("Buses")
    bus_names = set(buses["name"].astype(str)) if buses is not None and "name" in buses else set()
    regions = book.get("Regions")
    states = set(regions["state_id"].astype(str)) if regions is not None else set(G.STATES)
    region_ids = (
        set(regions["region_id"].astype(str)) if regions is not None else set(G.REGION_NAMES)
    )
    states |= {G.EXTERNAL_STATE}
    region_ids |= {G.EXTERNAL_REGION}

    targets = {
        "carriers": carriers | set(R.PSEUDO_CARRIERS),
        "buses": bus_names | set(R.PSEUDO_BUSES),
        "states": states,
        "regions": region_ids,
    }

    for sheet in expected:
        frame = book.get(sheet.name)
        if frame is None:
            continue
        for col in sheet.columns:
            if not col.fk or col.name not in frame.columns:
                continue
            if "." in col.fk:
                target_sheet, target_col = col.fk.split(".", 1)
                other = book.get(target_sheet)
                if other is None or target_col not in other:
                    continue
                valid = set(other[target_col].astype(str))
                if col.fk == "Buses.name":
                    valid |= set(R.PSEUDO_BUSES)
            else:
                valid = targets.get(col.fk, set())
            if not valid:
                continue
            values = frame[col.name].dropna().astype(str).str.strip()
            unknown = sorted(set(values[~values.isin(valid) & values.ne("")]))
            if unknown:
                report.error(
                    sheet.name,
                    f"{col.name!r} references unknown {col.fk}: {unknown[:8]}",
                )

    # Generated storage links use these default role-bus names.
    for required in ("Store", "Storage"):
        if bus_names and required not in bus_names:
            report.warn(
                "Buses",
                f"no bus named {required!r} — the store/storage link pairs "
                "terminate there unless spatial.role_buses generates them",
            )

    # Each New_Generators row needs both pipeline bounds.
    new_gens = book.get("New_Generators")
    for pipeline in ("Pipeline_p_min", "Pipeline_p_max"):
        bounds = book.get(pipeline)
        if new_gens is None or bounds is None:
            continue
        key = ["TECHNOLOGY", "bus"]
        if not set(key) <= set(new_gens.columns) or not set(key) <= set(bounds.columns):
            continue
        have = set(map(tuple, bounds[key].astype(str).values))
        want = set(map(tuple, new_gens[key].astype(str).values))
        missing = sorted(want - have)
        if missing:
            report.error(
                pipeline,
                f"{len(missing)} New_Generators row(s) have no bound here: {missing[:5]}",
            )

    # A New_Generators row with no Capital_cost match is free to build:
    # `Costs.capex()` returns 0.0 when its lookup misses, `annuitised()` then
    # short-circuits to capital_cost=0.0, and the row is added extendable. Paired
    # with a Pipeline_p_max of `inf` that is unlimited free capacity, which the
    # optimiser will take. A typo in TECHNOLOGY is enough to trigger it.
    costs = book.get("Capital_cost")
    if new_gens is not None and costs is not None:
        key = ["TECHNOLOGY", "bus", "carrier"]
        if set(key) <= set(new_gens.columns) and set(key) <= set(costs.columns):
            def _key(frame):
                return set(
                    map(tuple, frame[key].astype(str).apply(
                        lambda column: column.str.strip().str.lower()
                    ).values)
                )
            missing = sorted(_key(new_gens) - _key(costs))
            if missing:
                report.error(
                    "Capital_cost",
                    f"{len(missing)} New_Generators row(s) have no capital cost, so "
                    f"they would be built for free: {missing[:5]}",
                )

    # A carrier without a CO2 row is treated as zero-emission.
    co2 = book.get("CO2")
    if co2 is not None and "carrier" in co2:
        listed = set(co2["carrier"].astype(str).str.strip())
        for sheet_name in ("Generators", "New_Generators"):
            frame = book.get(sheet_name)
            if frame is None or "carrier" not in frame:
                continue
            used = set(frame["carrier"].astype(str).str.strip()) - {""}
            missing = sorted(used - listed)
            if missing:
                report.warn(
                    "CO2",
                    f"{sheet_name} uses carrier(s) with no CO2 row: {missing} — "
                    "they will be treated as zero-emission, which understates "
                    "emissions for market/imported energy",
                )

    # Link cost and lifetime records use the `Links` carrier label.
    for sheet_name in ("Capital_cost", "Lifetime"):
        frame = book.get(sheet_name)
        if frame is None or "carrier" not in frame:
            continue
        if "Links" in book and "Links" not in set(frame["carrier"].astype(str)):
            report.warn(
                sheet_name,
                "no carrier='Links' row — link capital cost/lifetime is looked up "
                "by that literal; missing cost defaults to 0, missing lifetime to 25 years",
            )


def check_loads(book, path, report: Report) -> None:
    """Every active load needs its Demand sheet, and vice versa."""
    loads = book.get("Loads")
    if loads is None or "name" not in loads:
        return

    declared = {
        str(name).strip() for name in loads["name"].dropna()
        if str(name).strip()
    }
    active = set(declare_active(loads))

    present = {
        name[len(R.DEMAND_PREFIX):]
        for name in pd.ExcelFile(path).sheet_names
        if name.startswith(R.DEMAND_PREFIX)
    }

    for name in sorted(active - present):
        report.error(
            "Loads",
            f"load {name!r} is active but has no {R.demand_sheet_name(name)!r} "
            "sheet — add one, or set active FALSE",
        )
    for name in sorted(present - declared):
        report.warn(
            R.demand_sheet_name(name),
            f"no row named {name!r} in Loads, so this sheet is ignored",
        )
    for name in sorted(declared - active):
        report.warn("Loads", f"load {name!r} is inactive and will not be built")

    if declared and "Demand" in pd.ExcelFile(path).sheet_names:
        report.warn(
            "Demand",
            "both a Loads sheet and a single Demand sheet are present; "
            "Loads wins and Demand is ignored",
        )


def declare_active(loads: pd.DataFrame) -> list[str]:
    frame = loads[loads["name"].notna()]
    frame = frame[frame["name"].astype(str).str.strip().ne("")]
    if "active" in frame.columns:
        flag = frame["active"].astype(str).str.strip().str.lower()
        frame = frame[~flag.isin(("false", "0", "no", "n", "f"))]
    return [str(name).strip() for name in frame["name"]]


def check_temporal(book, expected, config, report: Report) -> None:
    """Check complete hourly columns, allowing trailing leap-year padding."""
    fiscal = bool(config.get("scenario", {}).get("fiscal_year", True))
    lengths: dict[str, int] = {}
    for sheet in expected:
        if sheet.shape != "timeseries":
            continue
        frame = book.get(sheet.name)
        if frame is None:
            continue
        lengths[sheet.name] = len(frame)
        value_columns = ([str(y) for y in config_years(config)] if sheet.years == R.REQUIRED
                         else [str(c) for c in frame if str(c) != "snapshot"])
        for column in value_columns:
            if column not in frame:
                continue  # the structural check reports absent year columns
            raw = frame[column]
            values = pd.to_numeric(raw, errors="coerce")
            filled = raw.notna() & raw.astype(str).str.strip().ne("")
            positions = np.flatnonzero(filled.to_numpy())
            if not len(positions):
                report.error(sheet.name, f"{column!r} has no hourly data")
                continue
            effective = values.iloc[:positions[-1] + 1]
            if len(effective) < 8760:
                report.error(sheet.name, f"{column!r} has {len(effective)} hourly values; at least one complete 8760-hour year is required")
            elif len(effective) not in (8760, 8784):
                retained = 8784 if len(effective) >= 8784 else 8760
                report.warn(sheet.name, f"{column!r} has {len(effective)} values; the ragged-tail fallback keeps the first {retained} and drops {len(effective) - retained} trailing rows before calendar mapping")
            if not np.isfinite(effective.to_numpy(dtype=float)).all():
                report.error(sheet.name, f"{column!r} has blank, non-numeric or infinite values inside its hourly profile")
            if sheet.carrier_columns and (effective.lt(0).any() or effective.gt(1).any()):
                report.error(sheet.name, f"{column!r} must be between 0 and 1")
            elif not sheet.carrier_columns and effective.lt(0).any():
                # PyPSA accepts negative loads as fixed injections. Constraints
                # that require non-negative demand reject them only in their
                # selected scope.
                report.warn(sheet.name, f"{column!r} has {int(effective.lt(0).sum())} negative demand values "
                     "(fixed net injections); RPO/ReserveMargin require non-negative loads. "
                     "Check the source or represent injections with Generators/Links.")

    if len(set(lengths.values())) > 1:
        report.warn(
            "timeseries",
            f"sheets have different row counts: {lengths}. Each hourly column is "
            "aligned separately by month/day/hour after trimming trailing blanks; "
            "February 29 uses February 28 only for a non-leap source profile.",
        )

    for name, length in lengths.items():
        if length not in (8760, 8784):
            report.warn(
                name,
                f"{length} rows — a full {'financial' if fiscal else 'calendar'} "
                "year is 8760 (8784 in a leap year)",
            )

    days = book.get("Custom days")
    if days is not None and {"Month", "Day"} <= set(days.columns):
        month = pd.to_numeric(days["Month"], errors="coerce")
        day = pd.to_numeric(days["Day"], errors="coerce")
        dates = pd.to_datetime(dict(year=[2000] * len(days), month=month, day=day), errors="coerce")
        bad = dates.isna() | month.mod(1).ne(0) | day.mod(1).ne(0)
        if bad.any():
            report.error("Custom days", f"{int(bad.sum())} invalid Month/Day pair(s)")


def check_link_topology(book, report: Report) -> None:
    """Expose valid PyPSA Link settings with important physical consequences."""
    links = book.get("Links")
    if links is None or links.empty:
        return
    efficiency = pd.to_numeric(links.get("efficiency", pd.Series(1.0, index=links.index)), errors="coerce").fillna(1.0)
    minimum = pd.to_numeric(links.get("p_min_pu", pd.Series(0.0, index=links.index)), errors="coerce").fillna(0.0)
    asymmetric = minimum.lt(0) & efficiency.ne(1.0)
    if asymmetric.any():
        names = links.loc[asymmetric, "name"].astype(str).tolist() if "name" in links else links.index[asymmetric].tolist()
        report.warn(
            "Links",
            f"{len(names)} bidirectional links have efficiency != 1: {names[:6]}. "
            "PyPSA applies p1 = -efficiency * p0 in both directions; efficiency "
            "below 1 therefore creates an apparent energy gain in reverse flow. "
            "For losses in both directions, use two nonnegative directional Links "
            "with the intended capacities and costs (or Transfer_Capacity for fixed corridors).",
        )
    buses = book.get("Buses")
    if buses is None or not {"name", "role"} <= set(buses) or not {"bus0", "bus1"} <= set(links):
        return
    roles = buses["role"].astype(str).str.strip().str.lower()
    electricity = set(buses.loc[roles.eq("electricity"), "name"].astype(str))
    shared = {}
    for role_bus in buses.loc[roles.isin(["storage", "store"]), "name"].astype(str):
        neighbors = set(links.loc[links["bus0"].astype(str).eq(role_bus), "bus1"].astype(str))
        neighbors.update(links.loc[links["bus1"].astype(str).eq(role_bus), "bus0"].astype(str))
        regions = sorted(neighbors & electricity)
        if len(regions) > 1:
            shared[role_bus] = regions
    if shared:
        report.warn(
            "Links",
            f"Shared storage role buses connect multiple electricity buses: {shared}. "
            "These hubs permit inter-regional pass-through and can bypass policies "
            "on direct corridors. Use separate regional role buses and region-specific "
            "storage/links when storage must remain geographically local.",
        )


def check_physical(book, config, report: Report) -> None:
    """Check capacity contradictions and report consequential topology choices."""
    check_link_topology(book, report)
    for sheet_name, lo, hi in (
        ("Pipeline_p_min", "Pipeline_p_min", "Pipeline_p_max"),
    ):
        low, high = book.get(lo), book.get(hi)
        if low is None or high is None:
            continue
        key = ["TECHNOLOGY", "bus"]
        if not set(key) <= set(low.columns) or not set(key) <= set(high.columns):
            continue
        years = [str(y) for y in config_years(config)]
        merged = low.merge(high, on=key, suffixes=("_min", "_max"))
        for year in years:
            cmin, cmax = f"{year}_min", f"{year}_max"
            if cmin not in merged or cmax not in merged:
                continue
            a = pd.to_numeric(merged[cmin], errors="coerce")
            b = pd.to_numeric(merged[cmax], errors="coerce")
            bad = a > b
            if bad.any():
                rows = merged.loc[bad, key].astype(str).agg(" / ".join, axis=1).tolist()
                report.error(
                    "Pipeline_p_min",
                    f"p_nom_min > p_nom_max in {year} for {rows[:5]}",
                )

    # Repeated ceilings across several technologies can be template values
    # rather than technology-specific resource limits.
    ceilings = book.get("Pipeline_p_max")
    if ceilings is not None and "TECHNOLOGY" in ceilings:
        year_columns = [c for c in ceilings.columns if str(c).isdigit()]
        if year_columns:
            per_tech = ceilings.groupby("TECHNOLOGY")[year_columns].max()
            for values, group in per_tech.groupby(list(per_tech.columns)):
                if len(group) >= 3:
                    report.warn(
                        "Pipeline_p_max",
                        f"{len(group)} technologies share one ceiling "
                        f"{list(values)}: {sorted(group.index)} — this looks like "
                        "a shared template value rather than a resource limit. "
                        "Review resource-limited technologies before solving",
                    )

    demand = book.get("Demand")
    generators = book.get("Generators")
    if demand is None or generators is None or "p_nom" not in generators:
        return
    years = [y for y in config_years(config) if str(y) in map(str, demand.columns)]
    fixed = pd.to_numeric(generators["p_nom"], errors="coerce").sum()
    new_max = book.get("Pipeline_p_max")
    for year in years:
        column = year if year in demand.columns else str(year)
        peak = pd.to_numeric(demand[column], errors="coerce").max()
        headroom = fixed
        if new_max is not None and str(year) in map(str, new_max.columns):
            col = year if year in new_max.columns else str(year)
            extra = pd.to_numeric(
                new_max[col].replace("inf", np.inf), errors="coerce"
            ).sum()
            headroom = headroom + (0 if np.isnan(extra) else extra)
        if np.isfinite(headroom) and headroom < peak:
            report.warn(
                "Demand",
                f"FY{year}: peak demand {peak:,.0f} MW exceeds total available "
                f"capacity {headroom:,.0f} MW — the model will be infeasible "
                "unless an unserved-energy generator exists",
            )


def check_constraints(config, report: Report) -> None:
    """Config-level: constraint classes, names, and the family/mode gates."""
    from constraints import _CONSTRAINT_CLASSES, _REMOVED  # noqa: PLC0415

    entries = config.get("constraints", []) or []
    family = str(config.get("family", "expansion")).lower()
    mode = str(config.get("mode", "myopic")).lower()
    carriers = set(config_carriers(config))
    years = config_years(config)

    if family not in {"dispatch", "expansion"}:
        report.error("config", "family must be dispatch or expansion")
    if mode not in {"myopic", "perfect_foresight"}:
        report.error("config", "mode must be myopic or perfect_foresight")
    if not years or years != sorted(set(years)):
        report.error("config", "scenario.years must be nonempty, unique and increasing")
    resolution = config.get("snapshots", {}).get("resolution", 1)
    if not isinstance(resolution, int) or isinstance(resolution, bool) or resolution <= 0 or 24 % resolution:
        report.error("config", "snapshots.resolution must be a positive integer divisor of 24")
    if config.get("snapshots", {}).get("select", "all") not in {"all", "custom_days", "peak_weeks"}:
        report.error("config", "snapshots.select must be all, custom_days or peak_weeks")
    if bool((config.get("unit_commitment", {}) or {}).get("enable", False)):
        report.error("config", "unit_commitment.enable=true is not implemented")

    seen: set[str] = set()
    for entry in entries:
        name = str(entry.get("name", ""))
        class_name = str(entry.get("class", ""))

        if class_name in _REMOVED:
            report.error("constraints", f"{class_name} was removed: {_REMOVED[class_name]}.")
            continue
        if class_name not in _CONSTRAINT_CLASSES:
            report.error(
                "constraints",
                f"unknown class {class_name!r}; valid: "
                f"{', '.join(sorted(_CONSTRAINT_CLASSES))}",
            )
            continue
        if not name.strip():
            report.error("constraints", f"{class_name} has a blank name")
        if name in seen:
            report.error(
                "constraints",
                f"duplicate constraint name {name!r} — ConstraintSet requires unique names",
            )
        seen.add(name)

        eligible = entry.get("eligible_carriers") or ()
        unknown = sorted(set(map(str, eligible)) - carriers)
        if unknown:
            report.error(
                "constraints",
                f"{name!r} lists eligible_carriers not in config.carriers: {unknown}",
            )
        if "carrier" in entry and str(entry["carrier"]) not in carriers:
            report.error(
                "constraints",
                f"{name!r} references carrier {entry['carrier']!r}, not in config.carriers",
            )

        for key, value in entry.items():
            if isinstance(value, Mapping) and R.is_year_map(value, years):
                missing = sorted(set(years) - {int(k) for k in value})
                if missing:
                    report.error(
                        "constraints",
                        f"{name!r}.{key} is year-keyed but misses {missing}; "
                        "supply every modelled year (no interpolation)",
                    )

    from _helpers import constraints_for_year
    from constraints import constraint_from_dict
    from errors import ConstraintValidationError
    for year in years:
        try:
            for entry in constraints_for_year(config, year):
                constraint_from_dict(entry)
        except (ValueError, TypeError, KeyError, InputValidationError, ConstraintValidationError) as exc:
            report.error("constraints", f"FY{year}: {exc}")


# ------------------------------------------------------------------ driver


def discover_loads(path: Path) -> list[str]:
    """Return active load names from the workbook's optional `Loads` sheet.

    These names determine the expected `Demand_<name>` sheets. Without a Loads
    sheet, validation uses the single-Demand layout.
    """
    excel = pd.ExcelFile(path)
    if "Loads" not in excel.sheet_names:
        return []
    frame = pd.read_excel(excel, "Loads")
    frame.columns = [str(c) for c in frame.columns]
    if "name" not in frame.columns:
        return []
    if looks_like_units_row(frame, R.LOADS):
        frame = frame.iloc[1:].reset_index(drop=True)
    frame = frame[frame["name"].notna()]
    frame = frame[frame["name"].astype(str).str.strip().ne("")]
    if "active" in frame.columns:
        flag = frame["active"].astype(str).str.strip().str.lower()
        frame = frame[~flag.isin(("false", "0", "no", "n", "f"))]
    return [str(name).strip() for name in frame["name"]]


def read_workbook(path: Path, expected: Sequence[R.Sheet]) -> dict[str, pd.DataFrame]:
    """Load every sheet, applying the registry's accepted aliases."""
    alias = {
        "Pipeline_p_min": "Pipe_Line_Generators_p_min",
        "Pipeline_p_max": "Pipe_Line_Generators_p_max",
        "Pipeline_storage": "Pipe_Line_Storage_p_min",
    }
    excel = pd.ExcelFile(path)
    book: dict[str, pd.DataFrame] = {}
    for sheet in expected:
        source = sheet.name if sheet.name in excel.sheet_names else alias.get(sheet.name)
        if source is None or source not in excel.sheet_names:
            continue
        frame = pd.read_excel(excel, source)
        frame.columns = [str(c) for c in frame.columns]
        frame = resolve_columns(frame, sheet)
        if looks_like_units_row(frame, sheet):
            frame = frame.iloc[1:].reset_index(drop=True)
        frame = drop_blank_key_rows(frame, sheet)
        book[sheet.name] = frame
    # Retain unexpected sheet names so check_sheet_set can report them.
    for name in excel.sheet_names:
        if name not in book and name not in alias.values():
            book.setdefault(name, pd.read_excel(excel, name, nrows=0))
    return book


def validate(config: Mapping, path: Path) -> Report:
    report = Report()
    years = config_years(config)
    carriers = config_carriers(config)
    expected = R.resolve_sheets(
        config.get("components", {}) or {},
        snapshots_select=str(config.get("snapshots", {}).get("select", "all")),
        loads=discover_loads(path),
    )

    book = read_workbook(path, expected)

    check_sheet_set(book, expected, report)
    for sheet in expected:
        frame = book.get(sheet.name)
        if frame is None:
            continue
        check_columns(frame, sheet, years, carriers, report)
        check_placeholders(frame, sheet, report)
        check_types_and_ranges(frame, sheet, report)
        check_keys(frame, sheet, report)

    check_loads(book, path, report)
    check_references(book, expected, config, report)
    check_temporal(book, expected, config, report)
    check_physical(book, config, report)
    check_constraints(config, report)
    return report


def main(snakemake: Any) -> None:
    logger = configure_logging(snakemake)
    config = snakemake.config
    path = Path(snakemake.input.workbook)

    logger.info("validating %s", path.name)
    report = validate(config, path)

    out = Path(snakemake.output.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.text(), encoding="utf-8")

    for finding in report.warnings:
        logger.warning("%s: %s", finding.sheet, finding.message)
    logger.info(
        "%d error(s), %d warning(s) -> %s",
        len(report.errors), len(report.warnings), out,
    )
    # Log the errors as well as writing them. Snakemake deletes a failed job's
    # outputs, so the report is gone by the time the user reads the failure —
    # and a freshly generated template fails with dozens of placeholder errors,
    # which is precisely when a new user needs to see the list.
    for finding in report.errors:
        logger.error("%s: %s%s", finding.sheet, finding.message,
                     f" [{', '.join(finding.cells[:6])}]" if finding.cells else "")
    report.raise_if_failed()


snakemake: Any = globals().get("snakemake")
if __name__ == "__main__":
    if snakemake is None:
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("validate_inputs")
    main(snakemake)
