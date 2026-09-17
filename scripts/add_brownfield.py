"""Carry solved capacity forward into the next planning year.

Past vintages become fixed capacity, except market assets in expansion models.
Same-name Links may remain extendable with installed capacity as their lower
bound. The carried nominal capacity is ``max(installed, optimised)``.
"""

# Snakemake inserts a preamble, so this script must not use a future import.
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pypsa

sys.path.insert(0, str(Path(__file__).parent))

from _helpers import configure_logging
from errors import InputValidationError

#: (component, installed column, optimised column)
RATCHET = (
    ("Generator", "p_nom", "p_nom_opt"),
    ("StorageUnit", "p_nom", "p_nom_opt"),
    ("Store", "e_nom", "e_nom_opt"),
    ("Link", "p_nom", "p_nom_opt"),
)

#: Carriers that remain extendable when capacity is carried forward.
ALWAYS_EXTENDABLE = {"market"}


def _model_year(n: pypsa.Network) -> int | None:
    label = n.meta.get("pypsa_india", {}).get("model_year")
    return int(str(label).removeprefix("FY")) if label is not None else None


def _seasonal_profile(frame: pd.DataFrame, snapshots: pd.Index) -> pd.DataFrame:
    """Map the same month/day/hour across horizons, including leap February.

    Model years have different dates, so profiles are matched by seasonal keys
    instead of exact timestamps.
    February 29 uses February 28 when the source year is not a leap year.
    """
    def keys(index):
        return pd.MultiIndex.from_arrays(
            [index.month, index.day, index.hour, index.minute],
            names=["month", "day", "hour", "minute"],
        )
    source = frame.copy()
    source.index = keys(pd.DatetimeIndex(frame.index))
    source = source.groupby(level=[0, 1, 2, 3]).first()
    target = pd.DatetimeIndex(snapshots)
    wanted = keys(target)
    result = source.reindex(wanted)
    leap = (target.month == 2) & (target.day == 29)
    if leap.any():
        fallback = keys(target[leap] - pd.Timedelta(days=1))
        result.iloc[np.flatnonzero(leap)] = result.iloc[np.flatnonzero(leap)].fillna(
            pd.DataFrame(source.reindex(fallback).to_numpy(),
                         index=result.index[leap], columns=result.columns)
        )
    result.index = snapshots
    if result.isna().any().any():
        raise ValueError("Cannot align a carried availability profile to the new snapshot selection")
    return result


def _import_past_vintages(
    previous: pypsa.Network, current: pypsa.Network, component: str,
    nom: str, opt: str, logger,
) -> int:
    """Re-add assets that exist only in the previous year.

    Earlier vintages may have no row in the current candidate network. Preserve
    them as fixed capacity until the end of their lifetime.
    """
    old = previous.c[component].static
    new = current.c[component].static
    missing = old.index.difference(new.index)
    if not len(missing):
        return 0

    rows = old.loc[missing].copy()
    year = _model_year(current)
    if year is not None and current.meta.get("pypsa_india", {}).get("retirement_enabled", True):
        dated = rows.build_year.gt(0)
        rows = rows.loc[~dated | ((rows.build_year <= year) &
                                (rows.build_year + rows.lifetime > year))]
    # Corridors represent the fleet limit in each year, not a new vintage.
    if "is_transfer_corridor" in rows:
        rows = rows.loc[~rows.is_transfer_corridor.fillna(False).astype(bool)]
    if rows.empty:
        return 0
    missing = rows.index
    rows[nom] = np.maximum(
        pd.to_numeric(rows[nom], errors="coerce").fillna(0.0),
        pd.to_numeric(rows.get(opt, rows[nom]), errors="coerce").fillna(0.0),
    )

    # Use the operating year's candidate profile and fuel price. Peak-week
    # selections can differ between model years.
    analogs = {}
    if year is not None:
        for name, row in rows.iterrows():
            vintage = str(int(row.get("build_year", 0)))
            candidate = str(name)[:-len(vintage)] + str(year) if str(name).endswith(" " + vintage) else ""
            if candidate in new.index and all(
                str(new.at[candidate, attr]) == str(row[attr])
                for attr in ("carrier", "bus") if attr in new and attr in row
            ):
                analogs[name] = candidate
                if component == "Generator":
                    rows.at[name, "marginal_cost"] = new.at[candidate, "marginal_cost"]

    extendable = f"{nom}_extendable"
    if extendable in rows:
        carrier = rows["carrier"].astype(str).str.lower()
        rows[extendable] = carrier.isin(ALWAYS_EXTENDABLE) & (
            current.meta.get("pypsa_india", {}).get("family", "expansion") != "dispatch"
        )
    for bound in (f"{nom}_min", f"{nom}_max"):
        if bound in rows:
            rows[bound] = np.nan

    attrs = {
        column: rows[column].tolist()
        for column in rows.columns
        if column not in ("name", opt) and not column.endswith("_opt")
    }
    current.add(component, rows.index.tolist(), **attrs)

    # Restore time-varying inputs instead of PyPSA defaults such as p_max_pu=1.0.
    for attribute, frame in previous.c[component].dynamic.items():
        if frame.empty:
            continue
        columns = [c for c in frame.columns if c in missing]
        if not columns:
            continue
        defaults = previous.c[component].defaults
        if attribute in defaults.index and str(defaults.at[attribute, "status"]) == "Output":
            continue
        target = current.c[component].dynamic[attribute]
        for column in columns:
            analog = analogs.get(column)
            if analog is not None and analog in target:
                target[column] = target[analog].astype(float)
            elif analog is not None and attribute in new:
                target[column] = float(new.at[analog, attribute])
            else:
                target[column] = _seasonal_profile(frame[[column]], current.snapshots)[column].astype(float)

    logger.info(
        "%s: re-added %d past vintage(s) as fixed capacity", component, len(missing)
    )
    return len(missing)


def carry_forward(previous: pypsa.Network, current: pypsa.Network, logger) -> pypsa.Network:
    for component, nom, opt in RATCHET:
        old = previous.c[component].static
        new = current.c[component].static
        if old.empty or opt not in old:
            continue

        _import_past_vintages(previous, current, component, nom, opt, logger)
        new = current.c[component].static

        built = pd.to_numeric(old[opt], errors="coerce").fillna(0.0)
        shared = new.index.intersection(old.index)
        if "is_transfer_corridor" in new:
            shared = shared[~new.loc[shared, "is_transfer_corridor"].fillna(False).astype(bool)]
        if not len(shared):
            continue

        installed = pd.to_numeric(new.loc[shared, nom], errors="coerce").fillna(0.0)
        ratcheted = np.maximum(installed, built.reindex(shared).fillna(0.0))
        new.loc[shared, nom] = ratcheted

        extendable = f"{nom}_extendable"
        if extendable in new:
            expansion = (
                current.meta.get("pypsa_india", {}).get("family", "expansion")
                != "dispatch"
            )
            if component == "Link":
                # Keep expansion for same-name Links while setting the carried
                # capacity as a non-shrinkable floor.
                keep = new.loc[shared, extendable].astype(bool) & expansion
                maximum = pd.to_numeric(
                    new.loc[shared, f"{nom}_max"], errors="coerce"
                )
                impossible = keep & maximum.lt(ratcheted - 1e-9)
                if impossible.any():
                    details = ", ".join(
                        f"{name}: installed={ratcheted.at[name]:g}, "
                        f"{nom}_max={maximum.at[name]:g}"
                        for name in impossible.index[impossible]
                    )
                    raise InputValidationError(
                        "Current Link capacity ceiling is below carried installed "
                        f"capacity ({details}). Raise the current {nom}_max or use "
                        "a distinct vintage name; installed capacity is never discarded."
                    )
                floor = pd.to_numeric(
                    new.loc[shared, f"{nom}_min"], errors="coerce"
                ).fillna(0.0)
                new.loc[shared[keep], f"{nom}_min"] = np.maximum(
                    floor.loc[keep], ratcheted.loc[keep]
                )
            else:
                carrier = new.loc[shared, "carrier"].astype(str).str.lower()
                keep = carrier.isin(ALWAYS_EXTENDABLE) & expansion
            new.loc[shared, extendable] = keep
        else:
            keep = pd.Series(False, index=shared)

        # Fixed assets cannot retain nominal-capacity bounds.
        for bound in (f"{nom}_min", f"{nom}_max"):
            if bound in new:
                new.loc[shared, bound] = new.loc[shared, bound].where(keep, other=np.nan)

        grown = int((ratcheted > installed).sum())
        logger.info(
            "%s: ratcheted %d shared asset(s), kept %d extendable, %d grew "
            "from the previous solve",
            component, len(shared), int(keep.sum()), grown,
        )
    return current


def main(snakemake: Any) -> None:
    logger = configure_logging(snakemake)
    current = pypsa.Network(snakemake.input.network)
    previous_path = getattr(snakemake.input, "previous", None)

    if previous_path:
        previous = pypsa.Network(str(previous_path))
        logger.info("carrying capacity forward from %s", Path(str(previous_path)).name)
        current = carry_forward(previous, current, logger)
    else:
        logger.info("base year — nothing to carry forward")

    out = Path(snakemake.output.network)
    out.parent.mkdir(parents=True, exist_ok=True)
    current.export_to_netcdf(out)
    logger.info("wrote %s", out)


snakemake: Any = globals().get("snakemake")
if __name__ == "__main__":
    if snakemake is None:
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("add_brownfield", year=2035)
    main(snakemake)
