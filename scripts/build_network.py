"""Build an unsolved PyPSA network for one planning year from a workbook."""

# Snakemake inserts a preamble, so this script must not use a future import.
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import pypsa

sys.path.insert(0, str(Path(__file__).parent))

import regions as G
import registry as R
from _helpers import (
    annuitised_capital_cost,
    config_carriers,
    config_years,
    configure_logging,
    snapshot_capital_weighting,
    workbook_path,
)
from errors import InputValidationError
from validate_inputs import declare_active, discover_loads, read_workbook

FILL = R.FILL_ME


# Snapshots


def financial_year_range(year: int, fiscal: bool = True) -> pd.DatetimeIndex:
    """The hourly index of one modelled year.

    Uses the Indian financial year (April–March) by default; ``fiscal=False``
    selects a calendar year.
    """
    if fiscal:
        start, end = f"{year - 1}-04-01", f"{year}-04-01"
    else:
        start, end = f"{year}-01-01", f"{year + 1}-01-01"
    return pd.date_range(start=start, end=end, freq="h", inclusive="left")


def build_snapshots(
    config: Mapping, year: int, book: Mapping[str, pd.DataFrame]
) -> tuple[pd.DatetimeIndex, np.ndarray, float]:
    """Return (snapshots, positional row selector, capital weighting)."""
    scenario = config.get("scenario", {})
    snap = config.get("snapshots", {})
    fiscal = bool(scenario.get("fiscal_year", True))
    select = str(snap.get("select", "all"))
    resolution = snap.get("resolution", 1)
    if type(resolution) is not int or resolution <= 0 or 24 % resolution:
        raise InputValidationError("snapshots.resolution must be a positive integer divisor of 24")

    full = financial_year_range(year, fiscal)
    positions = np.arange(len(full))

    if select == "all":
        keep = positions[::resolution]
    elif select == "custom_days":
        days = book.get("Custom days")
        if days is None or days.empty:
            raise InputValidationError(
                "snapshots.select is 'custom_days' but the 'Custom days' sheet is "
                "missing or empty."
            )
        wanted = {
            (int(m), int(d))
            for m, d in zip(days["Month"], days["Day"])
            if pd.notna(m) and pd.notna(d)
        }
        mask = np.array([(ts.month, ts.day) in wanted for ts in full])
        keep = positions[mask][::resolution]
    elif select == "peak_weeks":
        loads = book.get("Loads")
        names = ([R.demand_sheet_name(name) for name in declare_active(loads)]
                 if loads is not None and "name" in loads else ["Demand"])
        if not names:
            raise InputValidationError("Peak-week selection needs at least one active load.")
        series = pd.Series(0.0, index=full)
        for name in names:
            if name not in book:
                raise InputValidationError(f"Peak-week selection requires {name!r}.")
            series += _demand_series(book[name], year, positions, full, name, fiscal)
        iso = full.isocalendar()
        chosen: list[np.ndarray] = []
        for _, group in series.groupby([series.index.year, series.index.month]):
            group_iso = group.index.isocalendar()
            iso_year, week = group.groupby([group_iso.year, group_iso.week]).sum().idxmax()
            chosen.append(
                np.flatnonzero(((iso.year == iso_year) & (iso.week == week)).to_numpy())
            )
        keep = np.unique(np.concatenate(chosen))[::resolution]
    else:
        raise InputValidationError(
            f"Unknown snapshots.select {select!r}. Valid: all, custom_days, peak_weeks"
        )

    snapshots = full[keep]
    if snapshots.empty:
        raise InputValidationError("Snapshot selection produced no hours; check Custom days and the model year.")
    # Scale capital costs to the selected snapshots and their resolution.
    weighting = snapshot_capital_weighting(len(snapshots), resolution)
    return snapshots, keep, weighting


def _year_column(frame: pd.DataFrame, year: int) -> Any:
    for candidate in (year, str(year)):
        if candidate in frame.columns:
            return candidate
    raise InputValidationError(
        f"No column for year {year} (have {[c for c in frame.columns if str(c).isdigit()]})"
    )


# Cost lookup


class Costs:
    """Year-resolved cost lookups, keyed the way each sheet actually is."""

    def __init__(self, book: Mapping[str, pd.DataFrame], config: Mapping, year: int):
        self.book = book
        self.config = config
        self.year = year
        self.costs_config = config.get("costs", {}) or {}
        self._wacc = self._wacc_table()

    def _wacc_table(self) -> pd.Series:
        frame = self.book.get("wacc")
        if frame is None or "name" not in frame:
            return pd.Series(dtype=float)
        column = _year_column(frame, self.year)
        return pd.Series(
            pd.to_numeric(frame[column], errors="coerce").to_numpy(),
            index=frame["name"].astype(str).str.strip(),
        )

    def wacc(self, storage: bool = False) -> float:
        """Return the configured WACC row for generation or storage."""
        key = self.costs_config.get(
            "wacc_row_storage" if storage else "wacc_row", "WACC %"
        )
        if key not in self._wacc.index:
            raise InputValidationError(
                f"costs.{'wacc_row_storage' if storage else 'wacc_row'} = {key!r} "
                f"is not a row of the wacc sheet (have {list(self._wacc.index)})"
            )
        value = float(self._wacc.loc[key])
        if not np.isfinite(value) or value < 0:
            raise InputValidationError(f"wacc row {key!r} is not a finite rate")
        return value

    def _lookup(
        self, sheet: str, value_column: str | None, **filters: Any
    ) -> float | None:
        frame = self.book.get(sheet)
        if frame is None or frame.empty:
            return None
        mask = pd.Series(True, index=frame.index)
        for column, wanted in filters.items():
            if column not in frame or wanted is None:
                continue
            mask &= frame[column].astype(str).str.strip().str.lower().eq(
                str(wanted).strip().lower()
            )
        rows = frame.loc[mask]
        if rows.empty:
            return None
        column = value_column or _year_column(frame, self.year)
        if column not in rows:
            return None
        # Use the first matching row.
        value = pd.to_numeric(rows[column], errors="coerce").iloc[0]
        return None if pd.isna(value) else float(value)

    def capex(self, *, carrier=None, bus=None, technology=None) -> float:
        value = self._lookup(
            "Capital_cost", None, carrier=carrier, bus=bus, TECHNOLOGY=technology
        )
        return 0.0 if value is None else value

    def lifetime(self, *, carrier=None, technology=None) -> float | None:
        return self._lookup(
            "Lifetime", "lifetime", carrier=carrier, TECHNOLOGY=technology
        )

    def fom(self, *, carrier=None, technology=None) -> float:
        if not self.costs_config.get("include_fom", True):
            return 0.0
        frame = self.book.get("FOM")
        if frame is None:
            return 0.0
        # Prefer a year-specific FOM column when available.
        column = None
        if any(str(c) == str(self.year) for c in frame.columns):
            column = _year_column(frame, self.year)
        value = self._lookup("FOM", column or "FOM", carrier=carrier, TECHNOLOGY=technology)
        return 0.0 if value is None else value

    def fuel_cost(self, *, carrier=None, bus=None, technology=None) -> float:
        value = self._lookup(
            "Fuel_cost", None, carrier=carrier, Bus=bus, TECHNOLOGY=technology
        )
        return 0.0 if value is None else value

    def startup_cost(self, *, carrier=None) -> float:
        value = self._lookup("Startupcost", None, carrier=carrier)
        return 0.0 if value is None else value

    def annuitised(
        self, *, carrier=None, bus=None, technology=None,
        storage: bool = False, weighting: float = 1.0,
        lifetime_fallback: float = 25.0,
    ) -> tuple[float, float]:
        """(capital_cost, lifetime) for one technology at one bus."""
        capex = self.capex(carrier=carrier, bus=bus, technology=technology)
        life = self.lifetime(carrier=carrier, technology=technology)
        if life is None or not np.isfinite(life) or life <= 0:
            life = lifetime_fallback
        if capex <= 0:
            return float(self.fom(carrier=carrier, technology=technology) / weighting), float(life)
        cost = annuitised_capital_cost(
            capex,
            self.wacc(storage=storage),
            life,
            self.fom(carrier=carrier, technology=technology),
            weighting,
        )
        return float(cost), float(life)


# Time series


def profile(
    book: Mapping[str, pd.DataFrame],
    sheet: str,
    carrier: str,
    keep: np.ndarray,
    snapshots: pd.DatetimeIndex,
    default: float,
    target: pd.DatetimeIndex,
    bus: str | None = None,
    external_suffix: str = "_Outside",
    fiscal: bool = True,
) -> Any:
    """Per-unit availability for one carrier, on the target year's calendar.

    Aligns leap and non-leap source years by month, day, and hour before
    selecting snapshots. Missing values use ``default``.
    """
    frame = book.get(sheet)
    if frame is None:
        return default

    column = None
    if bus is not None and external_suffix:
        candidate = f"{carrier}{external_suffix}"
        if candidate in frame.columns:
            column = candidate
    if column is None and carrier in frame.columns:
        column = carrier
    if column is None:
        return default

    values = pd.to_numeric(frame[column], errors="coerce").to_numpy()
    picked = _align_year_column(values, target, f"{sheet}!{column}", fiscal)[keep]
    if np.isnan(picked).any():
        picked = np.where(np.isnan(picked), default, picked)
    return pd.Series(picked, index=snapshots)


# Network builder


def build(
    config: Mapping, book: Mapping[str, pd.DataFrame], year: int, logger=None
) -> pypsa.Network:
    scenario = config.get("scenario", {})
    fiscal = bool(scenario.get("fiscal_year", True))
    spatial = config.get("spatial", {}) or {}
    external_bus = _external_bus_name(book)

    snapshots, keep, weighting = build_snapshots(config, year, book)
    target = financial_year_range(year, fiscal)
    resolution = int(config.get("snapshots", {}).get("resolution", 1) or 1)

    n = pypsa.Network()
    n.name = f"{scenario.get('name', 'scenario')}_FY{year}"
    n.set_snapshots(snapshots)

    # Objective and storage weights measure sampled hours. Generator weights
    # annualize energy to 8,760 hours; capital costs use the sample weighting.
    for column in ("objective", "generators", "stores"):
        n.snapshot_weightings[column] = float(resolution)
    n.snapshot_weightings["generators"] *= weighting

    n.meta = {
        "pypsa_india": {
            "model_year": f"FY{year}",
            "scenario": scenario.get("name", ""),
            "mode": config.get("mode", "myopic"),
            "family": config.get("family", "expansion"),
            "fiscal_year": fiscal,
            "retirement_enabled": bool(config.get("retirement", {}).get("enable", True)),
            "capital_weighting": weighting,
            "annual_energy_hours": 8760,
            "snapshot_selection": config.get("snapshots", {}).get("select", "all"),
            "co2_emission_basis": "electrical_output",
        }
    }

    buses = _buses(book, spatial)
    costs = Costs(book, config, year)

    _add_buses(n, buses)
    _add_carriers(n, book)
    _add_load(n, book, config, year, keep, snapshots, buses)
    _add_base_generators(n, book, buses, keep, snapshots, target, external_bus, fiscal)
    _add_new_generators(n, book, costs, buses, keep, snapshots, target, year, weighting,
                        external_bus, fiscal)
    _add_links(n, book, costs, year, weighting)
    _add_transfer_links(n, book, buses, year, logger)
    _add_storage(n, book, costs, buses, year, weighting, config)
    _add_storage_links(n, book, costs, buses, year, weighting, logger)
    if "is_transfer_corridor" in n.links:
        n.links["is_transfer_corridor"] = n.links["is_transfer_corridor"].fillna(False).astype(bool)

    _add_missing_carriers(n, logger)
    _stamp_scope(n, buses, book, year)
    if config.get("family", "expansion") == "dispatch":
        for component, nominal in (("Generator", "p_nom"), ("Link", "p_nom"),
                                   ("Store", "e_nom"), ("StorageUnit", "p_nom")):
            table = n.c[component].static
            if not table.empty:
                table[nominal] = np.maximum(table[nominal], table[f"{nominal}_min"])
                table[f"{nominal}_extendable"] = False
    _retire(n, year, config)
    return n


def _external_bus_name(book: Mapping[str, pd.DataFrame]) -> str:
    """Whichever bus stands for 'outside the boundary' in this workbook."""
    buses = book.get("Buses")
    if buses is None or "name" not in buses:
        return G.EXTERNAL_BUS
    names = [str(x) for x in buses["name"]]
    for name in names:
        if "outside" in name.lower() or name == G.EXTERNAL_BUS:
            return name
    return G.EXTERNAL_BUS


def _buses(book: Mapping[str, pd.DataFrame], spatial: Mapping) -> pd.DataFrame:
    """The Buses sheet, with role/scope columns filled in."""
    frame = book.get("Buses")
    if frame is None or frame.empty:
        return G.generate_buses(spatial)
    out = frame.copy()
    out["name"] = out["name"].astype(str).str.strip()
    if "role" not in out or out["role"].isna().all():
        role = pd.Series("electricity", index=out.index)
        role[out["name"].str.lower().eq("storage")] = "storage"
        role[out["name"].str.lower().eq("store")] = "store"
        out["role"] = role
    boundary = (spatial.get("boundary") or {})
    state = str(boundary.get("id", "")) or ""
    region = G.STATES.get(state, ("", ""))[1]
    for column, inside, outside in (
        ("state_id", state, G.EXTERNAL_STATE),
        ("region_id", region, G.EXTERNAL_REGION),
    ):
        if column not in out or out[column].isna().all():
            values = []
            for name, role in zip(out["name"], out["role"]):
                if role != "electricity":
                    values.append("")
                elif "outside" in name.lower() or name == G.EXTERNAL_BUS:
                    values.append(outside)
                else:
                    values.append(inside)
            out[column] = values
    return out


def _add_buses(n: pypsa.Network, buses: pd.DataFrame) -> None:
    n.add("Bus", buses["name"].tolist())
    indexed = buses.set_index("name")
    for column in ("role", "state_id", "region_id", "v_nom"):
        if column in indexed:
            n.buses[column] = indexed[column].reindex(n.buses.index)


def _add_carriers(n: pypsa.Network, book: Mapping[str, pd.DataFrame]) -> None:
    co2 = book.get("CO2")
    if co2 is not None and not co2.empty:
        frame = co2.drop_duplicates(subset=["carrier"], keep="first")
        n.add(
            "Carrier",
            frame["carrier"].astype(str).str.strip().tolist(),
            co2_emissions=pd.to_numeric(frame["tonnes/MWh"], errors="coerce")
            .fillna(0)
            .tolist(),
            color=frame["color"].astype(str).fillna("").tolist()
            if "color" in frame
            else "",
        )


def _add_missing_carriers(n: pypsa.Network, logger=None) -> list[str]:
    """Any carrier a component uses but the CO2 sheet omits, at zero emissions.

    Without this the emissions constraints fail with "Emission/fuel factors must
    be finite and non-negative" — `carrier.map(n.carriers.co2_emissions)` yields
    NaN for an unlisted carrier. Missing factors default to zero; validation
    warns because this is an assumption, not evidence of zero emissions.
    """
    used: set[str] = set()
    for component in ("Generator", "Store", "StorageUnit", "Link"):
        table = n.c[component].static
        if not table.empty and "carrier" in table:
            used |= {str(c).strip() for c in table["carrier"] if str(c).strip()}
    missing = sorted(used - set(n.carriers.index.astype(str)))
    if missing:
        n.add("Carrier", missing, co2_emissions=0.0)
        if logger is not None:
            logger.warning(
                "carriers absent from the CO2 sheet, added at co2_emissions=0: %s",
                ", ".join(missing),
            )
    return missing


def _demand_series(frame, year, keep, snapshots, label, fiscal=True) -> pd.Series:
    """One year's demand, mapped onto the target year's calendar.

    See `_align_year_column`: the length that decides leap-vs-non-leap is the
    year column's, not the sheet's.
    """
    column = _year_column(frame, year)
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy()
    target = financial_year_range(year, fiscal)
    picked = _align_year_column(values, target, f"{label}!{column}", fiscal)[keep]
    if not np.isfinite(picked).all():
        raise InputValidationError(f"{label}!{column} requires finite demand at every selected hour.")
    return pd.Series(picked, index=snapshots).astype(float)


def _matching_year_range(length: int, fiscal: bool) -> pd.DatetimeIndex | None:
    """A fiscal or calendar year of `length` hours, for seasonal mapping."""
    for probe in (2025, 2024):  # Non-leap financial year, then leap year.
        candidate = financial_year_range(probe, fiscal)
        if len(candidate) == length:
            return candidate
    return None


def _trim_trailing_blanks(values: np.ndarray) -> np.ndarray:
    """Drop trailing NaN. A sheet is padded to its longest column, so the rows
    past a column's last value are padding, not data."""
    filled = np.flatnonzero(~np.isnan(values))
    return values[: filled[-1] + 1] if len(filled) else values[:0]


def _align_year_column(
    values: np.ndarray, target: pd.DatetimeIndex, label: str, fiscal: bool = True
) -> np.ndarray:
    """Map one positional sheet column onto `target`'s hourly calendar.

    Financial years differ in length: FY2040 (Apr 2039 - Mar 2040) has a leap
    February and needs 8,784 hours where a non-leap year needs 8,760.

    Use the column's length, not the sheet's length. Sheets are padded to their
    longest column, so shorter non-leap columns can have trailing blanks beside
    leap-year columns.

    Equal-length years use positions. Different-length years map by month,
    day and hour in both directions. February 29 copies February 28 only when
    the source is a non-leap year. Extra trailing rows beyond a complete source
    year are ignored with a warning; internal missing values are not filled.
    """
    real = _trim_trailing_blanks(values)
    if len(real) == len(target):
        return real

    source = _matching_year_range(len(real), fiscal)
    if source is None:
        # Use the longest complete financial year and discard incomplete tails.
        for probe in (2024, 2025):  # Try the leap year first.
            candidate = financial_year_range(probe, fiscal)
            if len(candidate) <= len(real):
                logging.getLogger(__name__).warning(
                    "%s has %d values; keeping the first %d (one whole financial "
                    "year) and dropping %d trailing row(s)",
                    label, len(real), len(candidate), len(real) - len(candidate),
                )
                real, source = real[: len(candidate)], candidate
                break
    if source is None:
        raise InputValidationError(
            f"{label} has {len(real)} values, fewer than the 8760 hours of one "
            f"year — it cannot be mapped onto the target calendar of "
            f"{len(target)} hours."
        )
    lookup = pd.Series(
        real, index=pd.MultiIndex.from_arrays([source.month, source.day, source.hour])
    )
    lookup = lookup[~lookup.index.duplicated()]
    picked = lookup.reindex(
        pd.MultiIndex.from_arrays([target.month, target.day, target.hour])
    ).to_numpy()
    leap = np.flatnonzero(np.isnan(picked) & (target.month == 2) & (target.day == 29))
    if len(leap):
        back = target[leap] - pd.Timedelta(days=1)
        picked[leap] = lookup.reindex(
            pd.MultiIndex.from_arrays([back.month, back.day, back.hour])
        ).to_numpy()
    return picked


def _add_load(n, book, config, year, keep, snapshots, buses) -> None:
    """Add one Load per active Loads-sheet row or a single default load.

    ``Loads`` names each load point and ``Demand_<name>`` supplies its hourly
    profile. Workbooks without a Loads sheet use one load on the hub bus.
    """
    fiscal = bool(config.get("scenario", {}).get("fiscal_year", True))
    loads = book.get("Loads")
    if loads is not None and "name" in loads and len(loads):
        added = 0
        for _, row in loads.iterrows():
            name = str(row["name"]).strip()
            if not name:
                continue
            if not _boolean(row.get("active"), True):
                continue
            sheet = R.demand_sheet_name(name)
            frame = book.get(sheet)
            if frame is None:
                raise InputValidationError(
                    f"Load {name!r} is active but sheet {sheet!r} is missing."
                )
            bus = str(row["bus"]).strip()
            if bus not in set(buses["name"].astype(str)):
                raise InputValidationError(
                    f"Load {name!r} sits on unknown bus {bus!r}."
                )
            n.add(
                "Load", name, bus=bus,
                carrier=str(row.get("carrier", "") or "AC"),
                p_set=_demand_series(frame, year, keep, snapshots, sheet, fiscal),
            )
            added += 1
        if not added:
            raise InputValidationError(
                "The Loads sheet has no active rows, so the network has no demand."
            )
        return

    demand = book.get("Demand")
    if demand is None:
        raise InputValidationError(
            "No Loads sheet and no Demand sheet — the network would have no demand."
        )
    load_bus = str(config.get("spatial", {}).get("load_bus", "") or "")
    if not load_bus:
        electricity = buses.loc[buses["role"].eq("electricity"), "name"].tolist()
        preferred = [b for b in electricity if b.lower() in ("main_bus", "main bus")]
        load_bus = preferred[0] if preferred else electricity[0]
    n.add("Load", "load", bus=load_bus,
          p_set=_demand_series(demand, year, keep, snapshots, "Demand", fiscal))


def _add_base_generators(n, book, buses, keep, snapshots, target, external_bus, fiscal=True) -> None:
    """Add the existing fleet, batched by (carrier, bus).

    Called once per generator this is O(n) PyPSA `add` calls, each rebuilding a
    full time-series frame: 3,772 CEA units took minutes. Every unit sharing a
    (carrier, bus) also shares its availability profile, so one grouped call per
    combination does the same work in ~50 calls instead of ~3,800.
    """
    frame = book.get("Generators")
    if frame is None or frame.empty:
        return

    numeric = {
        "p_nom": 0.0, "marginal_cost": 0.0, "capital_cost": 0.0,
        "start_up_cost": 0.0, "shut_down_cost": 0.0,
        "ramp_limit_up": 1.0, "ramp_limit_down": 1.0,
    }
    integer = {"build_year": 2000, "min_up_time": 0, "min_down_time": 0}

    table = frame.copy()
    table["carrier"] = table["carrier"].astype(str).str.strip()
    table["bus"] = table["bus"].astype(str).str.strip()
    table["name"] = table["name"].astype(str)
    for column, default in numeric.items():
        table[column] = pd.to_numeric(table.get(column, pd.Series(default, index=table.index)), errors="coerce").fillna(default)
    for column, default in integer.items():
        table[column] = (
            pd.to_numeric(table.get(column, pd.Series(default, index=table.index)), errors="coerce").fillna(default).astype(int)
        )
    # Invalid lifetimes use the configured fallback duration.
    lifetime = pd.to_numeric(table.get("lifetime"), errors="coerce")
    table["lifetime"] = lifetime.where(
        lifetime.notna() & (lifetime > 0) & (lifetime <= 200), 25.0
    )

    for (carrier, bus), group in table.groupby(["carrier", "bus"], sort=False):
        is_external = bus == external_bus
        p_min = profile(book, "P_min_pu", carrier, keep, snapshots, 0.0, target,
                        bus=bus if is_external else None, fiscal=fiscal)
        p_max = profile(book, "P_max_pu", carrier, keep, snapshots, 1.0, target,
                        bus=bus if is_external else None, fiscal=fiscal)
        names = group["name"].tolist()
        # Broadcast each per-snapshot profile to the group's component columns.
        if isinstance(p_min, pd.Series):
            p_min = pd.DataFrame({name: p_min for name in names})
        if isinstance(p_max, pd.Series):
            p_max = pd.DataFrame({name: p_max for name in names})
        n.add(
            "Generator",
            names,
            bus=bus,
            carrier=carrier,
            p_nom=group["p_nom"].tolist(),
            # Existing assets are fixed except the market slack carrier.
            p_nom_extendable=bool(carrier.lower() == "market"),
            p_min_pu=p_min,
            p_max_pu=p_max,
            marginal_cost=group["marginal_cost"].tolist(),
            build_year=group["build_year"].tolist(),
            lifetime=group["lifetime"].tolist(),
            capital_cost=group["capital_cost"].tolist(),
            committable=False,
            start_up_cost=group["start_up_cost"].tolist(),
            shut_down_cost=group["shut_down_cost"].tolist(),
            min_up_time=group["min_up_time"].tolist(),
            min_down_time=group["min_down_time"].tolist(),
            ramp_limit_up=group["ramp_limit_up"].tolist(),
            ramp_limit_down=group["ramp_limit_down"].tolist(),
        )


def _add_new_generators(
    n, book, costs: Costs, buses, keep, snapshots, target, year, weighting, external_bus,
    fiscal=True,
) -> None:
    frame = book.get("New_Generators")
    if frame is None or frame.empty:
        return
    p_min = book.get("Pipeline_p_min")
    p_max = book.get("Pipeline_p_max")

    for _, row in frame.iterrows():
        technology = str(row["TECHNOLOGY"]).strip()
        carrier = str(row["carrier"]).strip()
        bus = str(row["bus"]).strip()
        is_external = bus == external_bus

        capital_cost, lifetime = costs.annuitised(
            carrier=carrier, bus=bus, technology=technology, weighting=weighting
        )
        lower = _pipeline_value(p_min, technology, bus, year, 0.0)
        upper = _pipeline_value(p_max, technology, bus, year, np.inf)

        n.add(
            "Generator",
            f"{technology} {bus} {year}",
            bus=bus,
            carrier=carrier,
            p_nom=0.0,
            p_nom_min=lower,
            p_nom_max=upper,
            p_nom_extendable=_boolean(row.get("p_nom_extendable"), True),
            p_min_pu=profile(book, "P_min_pu", carrier, keep, snapshots, 0.0, target,
                             bus=bus if is_external else None, fiscal=fiscal),
            p_max_pu=profile(book, "P_max_pu", carrier, keep, snapshots, 1.0, target,
                             bus=bus if is_external else None, fiscal=fiscal),
            marginal_cost=costs.fuel_cost(carrier=carrier, bus=bus, technology=technology),
            build_year=int(year),
            lifetime=float(lifetime),
            capital_cost=capital_cost,
            committable=False,
            start_up_cost=costs.startup_cost(carrier=carrier),
            # Use the startup-cost input for shutdown costs when no separate input exists.
            shut_down_cost=costs.startup_cost(carrier=carrier),
            min_up_time=int(_number(row.get("min_up_time"), 0)),
            min_down_time=int(_number(row.get("min_down_time"), 0)),
            ramp_limit_up=_number(row.get("ramp_limit_up"), 1.0),
            ramp_limit_down=_number(row.get("ramp_limit_down"), 1.0),
        )


def _pipeline_value(frame, technology, bus, year, default) -> float:
    if frame is None or frame.empty:
        return default
    mask = frame["TECHNOLOGY"].astype(str).str.strip().str.lower().eq(technology.lower())
    if "bus" in frame:
        mask &= frame["bus"].astype(str).str.strip().str.lower().eq(bus.lower())
    rows = frame.loc[mask]
    if rows.empty:
        return default
    try:
        column = _year_column(frame, year)
    except InputValidationError:
        return default
    raw = rows[column].iloc[0]
    if isinstance(raw, str) and raw.strip().lower() in ("inf", "infinity"):
        return np.inf
    value = pd.to_numeric(raw, errors="coerce")
    return default if pd.isna(value) else float(value)


def _add_links(n, book, costs: Costs, year, weighting) -> None:
    frame = book.get("Links")
    if frame is None or frame.empty:
        return
    # Split one inverter cost across the charge and discharge links.
    link_capex, link_life = costs.annuitised(
        carrier="Links", technology="Links", weighting=weighting
    )
    for _, row in frame.iterrows():
        name = str(row["name"]).strip()
        sheet_capex = _number(row.get("capital_cost"), np.nan)
        capital_cost = (
            sheet_capex if np.isfinite(sheet_capex) and sheet_capex > 0
            else link_capex / 2.0
        )
        n.add(
            "Link",
            name,
            bus0=str(row["bus0"]).strip(),
            bus1=str(row["bus1"]).strip(),
            carrier=str(row.get("carrier", "") or ""),
            efficiency=_number(row.get("efficiency"), 1.0),
            p_nom=_number(row.get("p_nom"), 0.0),
            p_nom_min=_number(row.get("p_nom_min"), 0.0),
            p_nom_max=_number(row.get("p_nom_max"), np.inf),
            p_nom_extendable=_boolean(row.get("p_nom_extendable"), True),
            p_min_pu=_number(row.get("p_min_pu"), 0.0),
            p_max_pu=_number(row.get("p_max_pu"), 1.0),
            capital_cost=float(capital_cost),
            marginal_cost=_number(row.get("marginal_cost"), 0.0),
            lifetime=float(link_life),
        )


def _add_transfer_links(n, book, buses, year, logger=None) -> None:
    """Inter-bus corridors from the Transfer_Capacity sheet.

    One Link per direction per row. The year value is the available corridor
    capacity, not an incremental build. Separate nonnegative flows preserve
    physical losses in both directions.
    """
    frame = book.get("Transfer_Capacity")
    if frame is None or frame.empty:
        return
    known = set(buses["name"].astype(str))
    added = 0
    for _, row in frame.iterrows():
        source, target = str(row["from_bus"]).strip(), str(row["to_bus"]).strip()
        if source not in known or target not in known or source == target:
            raise InputValidationError(f"Invalid transfer endpoints: {source!r}, {target!r}")
        capacity = _pipeline_value(
            frame.assign(TECHNOLOGY=frame["from_bus"], bus=frame["to_bus"]),
            source, target, year, 0.0,
        )
        if not np.isfinite(capacity) or capacity < 0:
            raise InputValidationError(f"Invalid transfer capacity for {source}->{target}: {capacity}")
        efficiency = _number(row.get("efficiency"), 1.0)
        for a, b in ((source, target), (target, source)):
            n.add(
                "Link", f"{a}->{b}",
                bus0=a, bus1=b, carrier="AC",
                efficiency=efficiency,
                p_nom=float(capacity), p_nom_extendable=False,
                p_min_pu=0.0, p_max_pu=1.0,
                marginal_cost=0.0,
            )
            n.links.loc[f"{a}->{b}", "is_transfer_corridor"] = True
            added += 1
    if added and logger:
        logger.info("added %d transfer link(s)", added)


def _add_storage_links(
    n, book, costs: Costs, buses, year, weighting, logger=None
) -> None:
    """Charge/discharge link pairs for storage that has none.

    A Store or StorageUnit on a role bus needs a link pair to an electricity
    bus. Add the pair only when the Links sheet does not provide one.
    """
    if n.stores.empty and n.storage_units.empty:
        return
    electricity = buses.loc[buses["role"].eq("electricity"), "name"].tolist()
    if not electricity:
        return
    hub = next(
        (b for b in electricity if b.lower() in ("main_bus", "main bus")), electricity[0]
    )

    capital_cost, lifetime = costs.annuitised(
        carrier="Links", technology="Links", weighting=weighting
    )
    added = 0
    for component in ("Store", "StorageUnit"):
        table = n.c[component].static
        for name, row in table.iterrows():
            bus = str(row["bus"])
            if bus in electricity:
                continue  # already on an electricity bus
            local_hub = next((b for b in electricity if bus in (f"{b}_Store", f"{b}_Storage")), hub)
            existing = n.links[
                (n.links.bus0.eq(bus) & n.links.bus1.eq(local_hub))
                | (n.links.bus0.eq(local_hub) & n.links.bus1.eq(bus))
            ]
            if len(existing):
                continue  # the workbook already wired this bus up
            for direction, (a, b) in (
                ("charge", (local_hub, bus)), ("discharge", (bus, local_hub)),
            ):
                n.add(
                    "Link", f"{name} {direction}",
                    bus0=a, bus1=b, carrier="AC",
                    efficiency=1.0,
                    p_nom_extendable=True,
                    p_min_pu=0.0, p_max_pu=1.0,
                    # Split one inverter cost across the charge and discharge links.
                    capital_cost=capital_cost / 2.0,
                    marginal_cost=0.0,
                    build_year=int(year),
                    lifetime=float(lifetime),
                )
                added += 1
    if added and logger:
        logger.info("added %d storage link(s) for unwired storage buses", added)


def _add_storage(n, book, costs: Costs, buses, year, weighting, config) -> None:
    frame = book.get("New_Storage")
    if frame is None or frame.empty:
        return
    pipeline = book.get("Pipeline_storage")
    storage_config = config.get("storage", {}) or {}
    max_hours = float(storage_config.get("max_hours", 6))
    standing_loss = float(storage_config.get("standing_loss", 0.002))

    for _, row in frame.iterrows():
        technology = str(row["TECHNOLOGY"]).strip()
        carrier = str(row["carrier"]).strip()
        bus = str(row["bus"]).strip()
        # Treat Store and store as the same storage type.
        is_store = str(row["Type"]).strip().lower() == "store"

        capital_cost, lifetime = costs.annuitised(
            carrier=carrier, bus=bus, technology=technology,
            storage=True, weighting=weighting,
        )
        committed = _pipeline_value(pipeline, technology, bus, year, 0.0)

        if is_store:
            # Pipeline storage is an energy floor for Stores and a power floor
            # for StorageUnits.
            n.add(
                "Store",
                f"{technology} {bus} {year}",
                bus=bus,
                carrier=carrier,
                e_nom_extendable=True,
                e_nom_min=float(committed),
                e_cyclic=True,
                standing_loss=standing_loss,
                capital_cost=capital_cost,
                build_year=int(year),
                lifetime=float(lifetime),
            )
        else:
            n.add(
                "StorageUnit",
                f"{technology} {bus} {year}",
                bus=bus,
                carrier=carrier,
                p_nom_extendable=True,
                p_nom_min=float(committed),
                max_hours=max_hours,
                standing_loss=standing_loss,
                cyclic_state_of_charge=True,
                capital_cost=capital_cost,
                marginal_cost=0.0,
                build_year=int(year),
                lifetime=float(lifetime),
            )


def _stamp_scope(n: pypsa.Network, buses: pd.DataFrame, book=None, year=None) -> None:
    """Write state_id/region_id onto every component table.

    `constraints.helpers.scope_mask` filters on these columns; a component
    without them cannot be selected by a GeographicScope and silently drops out
    of every scoped constraint.
    """
    lookup = buses.set_index("name")
    for component, bus_column in (
        ("Generator", "bus"), ("Load", "bus"), ("Store", "bus"),
        ("StorageUnit", "bus"), ("Link", "bus1"),
    ):
        table = n.c[component].static
        if table.empty:
            continue
        for column in ("state_id", "region_id"):
            if column not in lookup:
                continue
            mapped = table[bus_column].map(lookup[column]).fillna("").astype(str)
            existing = table.get(column, pd.Series("", index=table.index)).fillna("").astype(str)
            table[column] = existing.where(existing.str.strip().ne(""), mapped)
    # Explicit asset geography must survive a coarse regional bus resolution.
    for sheet, component in (("Generators", "Generator"), ("New_Generators", "Generator"),
                             ("New_Storage", None), ("Links", "Link")):
        frame = (book or {}).get(sheet)
        if frame is None:
            continue
        for _, row in frame.iterrows():
            target = component
            if sheet == "New_Storage":
                target = "Store" if str(row.get("Type", "")).lower() == "store" else "StorageUnit"
            name = str(row["name"]) if sheet in ("Generators", "Links") else f"{str(row['TECHNOLOGY']).strip()} {str(row['bus']).strip()} {year}"
            table = n.c[target].static
            if name not in table.index:
                continue
            for column in ("state_id", "region_id", "TECHNOLOGY", "ownership"):
                if pd.notna(row.get(column)) and str(row[column]).strip():
                    table.loc[name, column] = str(row[column]).strip()


def _retire(n: pypsa.Network, year: int, config: Mapping) -> None:
    """Remove vintages that are not active in the model year."""
    if not bool(config.get("retirement", {}).get("enable", True)):
        return
    for component in ("Generator", "Store", "StorageUnit", "Link"):
        table = n.c[component].static
        if table.empty or "build_year" not in table:
            continue
        build_year = pd.to_numeric(table["build_year"], errors="coerce").fillna(0)
        lifetime = pd.to_numeric(
            table.get("lifetime", pd.Series(np.inf, index=table.index)), errors="coerce"
        ).fillna(np.inf)
        # PyPSA's build_year default of 0 means "always active", not "built in
        # year 0" — without this guard every asset lacking a build year is
        # retired immediately.
        dated = build_year > 0
        not_yet = dated & (build_year > year)
        expired = dated & ((build_year + lifetime) <= year)
        doomed = table.index[not_yet | expired]
        if len(doomed):
            n.remove(component, doomed.tolist())


def _number(value: Any, default: float) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return float(default) if pd.isna(number) else float(number)


def _boolean(value: Any, default: bool) -> bool:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "t"):
        return True
    if text in ("false", "0", "no", "n", "f"):
        return False
    return default


# ------------------------------------------------------------------ driver


def main(snakemake: Any) -> None:
    logger = configure_logging(snakemake)
    config = snakemake.config
    year = int(snakemake.wildcards.year)

    workbook = Path(snakemake.input.workbook)
    expected = R.resolve_sheets(
        config.get("components", {}) or {},
        snapshots_select=str(config.get("snapshots", {}).get("select", "all")),
        loads=discover_loads(workbook),
    )
    book = read_workbook(workbook, expected)

    logger.info("building FY%s", year)
    n = build(config, book, year, logger)
    logger.info(
        "FY%s: %d buses, %d generators, %d links, %d stores, %d storage units, "
        "%d snapshots",
        year, len(n.buses), len(n.generators), len(n.links),
        len(n.stores), len(n.storage_units), len(n.snapshots),
    )

    out = Path(snakemake.output.network)
    out.parent.mkdir(parents=True, exist_ok=True)
    n.export_to_netcdf(out)
    logger.info("wrote %s", out)


snakemake: Any = globals().get("snakemake")
if __name__ == "__main__":
    if snakemake is None:
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("build_network", year=2030)
    main(snakemake)
