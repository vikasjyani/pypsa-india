"""Write period-aware result tables from a solved PyPSA network.

The perfect-foresight workflow passes the same multi-investment-period network
to this script once per model year. ``period_network`` selects the requested
snapshots and removes assets which have not been built or have retired.
"""

# No ``from __future__`` import: Snakemake's ``script:`` directive prepends a
# preamble, so future imports are not legal here.
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pypsa

sys.path.insert(0, str(Path(__file__).parent))

from _helpers import configure_logging

SENTINEL = "all output tables are generated\n"
CAPACITY_COMPONENTS = ("Generator", "StorageUnit", "Link", "Line", "Transformer")
CURTAILMENT_CARRIERS = {"solar", "wind", "hydro ror"}


def _is_active(table: pd.DataFrame, year: int, retirement_enabled: bool = True) -> pd.Series:
    """Return PyPSA investment-period activity, treating build year 0 as existing."""
    build = pd.to_numeric(table.get("build_year", 0), errors="coerce")
    if not isinstance(build, pd.Series):
        build = pd.Series(float(build), index=table.index)
    build = build.fillna(0)
    life = pd.to_numeric(table.get("lifetime", np.inf), errors="coerce")
    if not isinstance(life, pd.Series):
        life = pd.Series(float(life), index=table.index)
    life = life.fillna(np.inf)
    commissioned = build.eq(0) | (build <= year)
    if not retirement_enabled:
        return commissioned
    return build.eq(0) | (commissioned & (build + life > year))


def period_network(n: pypsa.Network, year: int) -> pypsa.Network:
    """Copy only ``year`` from a multi-period network and screen inactive assets."""
    if isinstance(n.snapshots, pd.MultiIndex):
        periods = pd.Index(n.snapshots.get_level_values(0))
        available = sorted({int(period) for period in periods.unique()})
        if year not in available:
            raise ValueError(f"FY{year} is not in network investment periods {available}")
        snapshots = n.snapshots[periods.astype(int) == year]
        view = n.copy(snapshots=snapshots, investment_periods=[year])
    else:
        view = n.copy()

    # Removing inactive rows prevents stale imported time series from leaking
    # into summaries even if those values should be zero after optimization.
    retirement_enabled = bool(
        n.meta.get("pypsa_india", {}).get("retirement_enabled", True)
    )
    for component in ("Generator", "StorageUnit", "Store", "Link", "Line", "Transformer"):
        table = view.c[component].static
        if table.empty:
            continue
        inactive = table.index[~_is_active(table, year, retirement_enabled)]
        if len(inactive):
            view.remove(component, inactive)
    return view


def _carrier_color(n: pypsa.Network, carrier: pd.Series) -> pd.Series:
    if "color" not in n.carriers:
        return pd.Series("", index=carrier.index, dtype=object)
    return carrier.map(n.carriers["color"]).fillna("").astype(str)


def _stat_to_tidy(values: pd.Series | pd.DataFrame, value_name: str) -> pd.DataFrame:
    """Normalize PyPSA statistics Series/DataFrames to component/carrier/value."""
    if isinstance(values, pd.DataFrame):
        values = values.sum(axis=1, min_count=1)
    values = pd.to_numeric(values, errors="coerce").dropna()
    if values.empty:
        return pd.DataFrame(columns=["component", "carrier", value_name])
    out = values.rename(value_name).reset_index()
    if "component" not in out.columns:
        out.insert(0, "component", "")
    if "carrier" not in out.columns:
        out.insert(1, "carrier", "")
    return out[["component", "carrier", value_name]]


class Tables:
    """Build result DataFrames; each nonempty ``table_*`` result becomes a CSV."""

    def __init__(
        self,
        n: pypsa.Network,
        year: int,
        *,
        whole_objective: float | None = None,
        whole_periods: list[int] | None = None,
    ) -> None:
        self.n = n
        self.year = int(year)
        self.weights = n.snapshot_weightings["generators"].reindex(n.snapshots).fillna(1.0)
        self.whole_objective = whole_objective
        self.whole_periods = whole_periods or []

    def table_statistics(self) -> pd.DataFrame:
        return self.n.statistics(nice_names=False, round=None).reset_index()

    def table_statistics_by_carrier(self) -> pd.DataFrame:
        return self.n.statistics(groupby=["carrier"], nice_names=False, round=None).reset_index()

    def table_capacity(self) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for components, unit in ((CAPACITY_COMPONENTS, "MW"), (("Store",), "MWh")):
            present = [c for c in components if not self.n.c[c].static.empty]
            if not present:
                continue
            optimal = _stat_to_tidy(
                self.n.statistics.optimal_capacity(
                    components=present, groupby=["carrier"], nice_names=False,
                    drop_zero=False, round=None,
                ), "optimal",
            )
            installed = _stat_to_tidy(
                self.n.statistics.installed_capacity(
                    components=present, groupby=["carrier"], nice_names=False,
                    drop_zero=False, round=None,
                ), "installed",
            )
            frame = optimal.merge(installed, on=["component", "carrier"], how="outer").fillna(0)
            frame["built"] = frame["optimal"] - frame["installed"]
            frame["unit"] = unit
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out["color"] = _carrier_color(self.n, out["carrier"])
        return out[["component", "carrier", "installed", "built", "optimal", "unit", "color"]]

    def table_generation(self) -> pd.DataFrame:
        values = self.n.statistics.supply(
            components=["Generator"], groupby=["carrier"], at_port="bus0",
            nice_names=False, drop_zero=False, round=None,
        )
        out = _stat_to_tidy(values, "generation_MWh")
        if out.empty:
            return out
        out["unit"] = "MWh"
        out["color"] = _carrier_color(self.n, out["carrier"])
        return out

    def table_emissions(self) -> pd.DataFrame:
        """Account on the declared factor basis, including dynamic efficiency."""
        n = self.n
        columns = [
            "carrier", "basis", "electrical_output_MWh", "fuel_input_MWh",
            "emission_factor_tCO2_per_MWh_basis", "tCO2", "color",
        ]
        if n.generators.empty or "co2_emissions" not in n.carriers:
            return pd.DataFrame(columns=columns)
        basis = str(n.meta.get("pypsa_india", {}).get("co2_emission_basis", ""))
        if basis not in {"electrical_output", "fuel_input"}:
            raise ValueError(
                "n.meta['pypsa_india']['co2_emission_basis'] must be "
                "'electrical_output' or 'fuel_input' for emissions reporting"
            )
        names = n.generators.index
        dispatch = n.generators_t.p.reindex(index=n.snapshots, columns=names).fillna(0.0)
        efficiency = n.get_switchable_as_dense(
            "Generator", "efficiency", snapshots=n.snapshots, inds=names
        ).replace(0.0, np.nan)
        if not np.isfinite(efficiency.to_numpy(dtype=float)).all() or efficiency.le(0).any().any():
            raise ValueError("Emissions reporting requires finite positive generator efficiency")
        electrical = dispatch.mul(self.weights, axis=0).sum()
        fuel = dispatch.div(efficiency).mul(self.weights, axis=0).sum()
        factors = pd.to_numeric(
            n.generators["carrier"].map(n.carriers["co2_emissions"]), errors="coerce"
        )
        if factors.isna().any() or not np.isfinite(factors).all() or factors.lt(0).any():
            raise ValueError("Carrier CO2 emission factors must be finite and non-negative")
        basis_energy = electrical if basis == "electrical_output" else fuel
        assets = pd.DataFrame({
            "carrier": n.generators["carrier"],
            "electrical_output_MWh": electrical,
            "fuel_input_MWh": fuel,
            "factor_energy": basis_energy,
            "factor_times_energy": basis_energy * factors,
        })
        grouped = assets.groupby("carrier", dropna=False).agg(
            electrical_output_MWh=("electrical_output_MWh", "sum"),
            fuel_input_MWh=("fuel_input_MWh", "sum"),
            factor_energy=("factor_energy", "sum"),
            tCO2=("factor_times_energy", "sum"),
        )
        grouped["emission_factor_tCO2_per_MWh_basis"] = np.divide(
            grouped["tCO2"], grouped["factor_energy"],
            out=np.zeros(len(grouped), dtype=float), where=grouped["factor_energy"].ne(0),
        )
        grouped["basis"] = basis
        out = grouped.reset_index()
        out["color"] = _carrier_color(n, out["carrier"])
        return out[columns].sort_values("tCO2", ascending=False, ignore_index=True)

    def table_curtailment(self) -> pd.DataFrame:
        n = self.n
        if n.generators.empty:
            return pd.DataFrame()
        dispatch = n.generators_t.p.reindex(index=n.snapshots, columns=n.generators.index).fillna(0.0)
        weighted_hours = float(self.weights.sum())
        rows: list[dict] = []
        for carrier, members in n.generators.groupby("carrier").groups.items():
            names = list(members)
            capacity = pd.to_numeric(
                n.generators.loc[names, "p_nom_opt"] if "p_nom_opt" in n.generators
                else n.generators.loc[names, "p_nom"], errors="coerce",
            ).fillna(0.0)
            generated = float(dispatch[names].mul(self.weights, axis=0).sum().sum())
            per_unit = n.get_switchable_as_dense(
                "Generator", "p_max_pu", snapshots=n.snapshots, inds=pd.Index(names)
            )
            availability = float(per_unit.mul(capacity, axis=1).mul(self.weights, axis=0).sum().sum())
            gap = max(availability - generated, 0.0)
            tolerance = 1e-6 + 1e-9 * max(availability, generated)
            curtailed = 0.0 if gap <= tolerance else gap
            installed = float(capacity.sum())
            rows.append({
                "carrier": carrier,
                "n_generators": len(names),
                "capacity_MW": installed,
                "available_MWh": availability,
                "generated_MWh": generated,
                "curtailed_MWh": curtailed,
                "curtailment_pct": 100.0 * curtailed / availability if availability > 0 else np.nan,
                "CUF": generated / (installed * weighted_hours) if installed > 0 and weighted_hours > 0 else np.nan,
                # Only variable-resource headroom has the physical meaning of
                # curtailment. Dispatchable plants may also have p_max_pu
                # profiles, but unused thermal headroom is not curtailment.
                "interpretable": str(carrier).casefold() in CURTAILMENT_CARRIERS,
            })
        out = pd.DataFrame(rows).sort_values("carrier", ignore_index=True)
        out["color"] = _carrier_color(n, out["carrier"])
        return out

    def table_costs(self) -> pd.DataFrame:
        """Costs from PyPSA statistics, retaining investment-period weighting."""
        frames = []
        suffix = "present_value_contribution" if self.n.has_investment_periods else "single_horizon"
        for metric, accessor in (
            (f"capital_cost_{suffix}", self.n.statistics.capex),
            (f"operating_cost_{suffix}", self.n.statistics.opex),
            (f"system_cost_{suffix}", self.n.statistics.system_cost),
        ):
            values = accessor(groupby=["carrier"], nice_names=False, drop_zero=False, round=None)
            frame = _stat_to_tidy(values, "value")
            if frame.empty:
                continue
            frame.insert(0, "metric", metric)
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out["unit"] = "currency"
        return out[["metric", "component", "carrier", "value", "unit"]]

    def table_objective(self) -> pd.DataFrame:
        """Emit the optimization objective once, labelled with its true scope."""
        if self.whole_objective is None or not np.isfinite(self.whole_objective):
            return pd.DataFrame()
        if self.whole_periods:
            if self.year != min(self.whole_periods):
                return pd.DataFrame()
            scope = "whole_horizon"
            periods = ",".join(str(p) for p in self.whole_periods)
        else:
            scope = "single_horizon"
            periods = str(self.year)
        return pd.DataFrame([{
            "scope": scope, "periods": periods,
            "objective": self.whole_objective, "unit": "currency",
        }])


def write_tables(
    n: pypsa.Network,
    year: int,
    out_dir: Path,
    logger,
    *,
    whole_objective: float | None = None,
    whole_periods: list[int] | None = None,
) -> list[str]:
    tables = Tables(n, year, whole_objective=whole_objective, whole_periods=whole_periods)
    methods = [
        name for name in dir(tables)
        if name.startswith("table_") and callable(getattr(tables, name))
    ]
    written: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for method in sorted(methods):
        frame = getattr(tables, method)()
        path = out_dir / f"{method.removeprefix('table_')}.csv"
        if write_table(frame, path, logger, method):
            written.append(path.name)
    return written


def write_table(frame: pd.DataFrame, path: Path, logger, label: str) -> bool:
    """Write a current table or remove its stale prior-run counterpart."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        logger.info("skipped %s (no rows)", label)
        if path.exists():
            path.unlink()
        return False
    frame.to_csv(path, index=False)
    return True


def write_period_constraint_tables(network_path: Path, year: int, out_dir: Path) -> list[str]:
    """Copy one period's audit/config rows from ``csvs/all`` and clear stale files."""
    source_dir = network_path.parents[1] / "csvs" / "all"
    if not source_dir.is_dir() or source_dir.resolve() == out_dir.resolve():
        return []
    written = []
    for name in ("constraint_audit.csv", "constraint_config.csv"):
        source = source_dir / name
        target = out_dir / name
        if not source.exists():
            if target.exists():
                target.unlink()
            continue
        frame = pd.read_csv(source)
        period_column = "period" if "period" in frame else "year" if "year" in frame else None
        if period_column is None:
            if target.exists():
                target.unlink()
            continue
        selected = frame.loc[pd.to_numeric(frame[period_column], errors="coerce").eq(year)]
        if selected.empty:
            if target.exists():
                target.unlink()
            continue
        selected.to_csv(target, index=False)
        written.append(name)
    return written


def main(snakemake: Any) -> None:
    logger = configure_logging(snakemake)
    year = int(snakemake.wildcards.year)
    network_path = Path(snakemake.input.network)
    whole = pypsa.Network(network_path)
    periods = sorted(int(p) for p in whole.investment_periods) if isinstance(whole.snapshots, pd.MultiIndex) else []
    objective = getattr(whole, "objective", None)
    objective = float(objective) if objective is not None else None
    n = period_network(whole, year)
    out = Path(snakemake.output.summary)
    written = write_tables(
        n, year, out.parent, logger,
        whole_objective=objective, whole_periods=periods,
    )
    written += write_period_constraint_tables(network_path, year, out.parent)
    logger.info("FY%s wrote %d table(s): %s", year, len(written), ", ".join(written))
    out.write_text(SENTINEL, encoding="utf-8")


snakemake: Any = globals().get("snakemake")
if __name__ == "__main__":
    if snakemake is None:
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("make_summary", year=2030)
    main(snakemake)
