"""PyPSA 1.x/Linopy compatibility and scoped weighted-accounting helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
import pypsa
import xarray as xr

from errors import (
    ConstraintAuditError,
    ConstraintBuildError,
    ConstraintInapplicable,
    ConstraintValidationError,
)


@dataclass(frozen=True)
class GeographicScope:
    bus: str | None = None
    state_id: str | None = None
    region_id: str | None = None

    @property
    def label(self) -> str:
        values = [
            f"bus={self.bus}" if self.bus else "",
            f"state={self.state_id}" if self.state_id else "",
            f"region={self.region_id}" if self.region_id else "",
        ]
        return ",".join(value for value in values if value) or "system"


def validate_snapshots(snapshots: pd.Index) -> pd.DatetimeIndex:
    if isinstance(snapshots, pd.MultiIndex):
        raise ConstraintValidationError(
            "Pass multi-investment-period networks through ConstraintSet, which "
            "resolves policies and active assets separately for each period."
        )
    try:
        sns = pd.DatetimeIndex(snapshots)
    except Exception as exc:
        raise ConstraintValidationError("Constraint snapshots must be datetime-like.") from exc
    if sns.empty or sns.has_duplicates:
        raise ConstraintValidationError("Constraint snapshots must be non-empty and unique.")
    return sns


def safe_name(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "system"


def component_dim(var) -> str:
    """Return the PyPSA component dimension (`name` in PyPSA 1.x)."""
    dims = [str(dim) for dim in getattr(var, "dims", ())]
    if "name" in dims:
        return "name"
    candidates = [dim for dim in dims if dim != "snapshot"]
    if len(candidates) != 1:
        raise ConstraintBuildError(f"Cannot resolve component dimension from {dims}.")
    return candidates[0]


def coeff_like(
    var,
    *,
    snapshot_values=None,
    component_values=None,
    component_dim_name: str | None = None,
) -> xr.DataArray:
    """Materialize coefficients with exactly the selected variable's dimensions."""
    dims = list(getattr(var, "dims", ()))
    shape = [int(var.sizes[dim]) for dim in dims]
    data = np.ones(shape, dtype=float)
    if snapshot_values is not None and "snapshot" in dims:
        values = np.asarray(snapshot_values, dtype=float)
        if len(values) != int(var.sizes["snapshot"]):
            raise ConstraintBuildError("Snapshot coefficient length does not match the variable.")
        reshape = [1] * len(dims)
        reshape[dims.index("snapshot")] = len(values)
        data *= values.reshape(reshape)
    dim = component_dim_name or component_dim(var)
    if component_values is not None and dim in dims:
        values = np.asarray(component_values, dtype=float)
        if len(values) != int(var.sizes[dim]):
            raise ConstraintBuildError("Component coefficient length does not match the variable.")
        reshape = [1] * len(dims)
        reshape[dims.index(dim)] = len(values)
        data *= values.reshape(reshape)
    coords = {dim: var.coords[dim] for dim in dims if dim in getattr(var, "coords", {})}
    return xr.DataArray(data, dims=dims, coords=coords)


def model_variable(n: pypsa.Network, key: str):
    if getattr(n, "_model", None) is None:
        raise ConstraintBuildError("No Linopy model exists on the network.")
    try:
        return n.model.variables[key]
    except KeyError as exc:
        raise ConstraintBuildError(f"Required model variable {key!r} is absent.") from exc


def snapshot_weights(
    n: pypsa.Network, snapshots: pd.Index, column: str = "generators"
) -> pd.Series:
    sns = validate_snapshots(snapshots)
    if column not in n.snapshot_weightings:
        raise ConstraintValidationError(f"snapshot_weightings lacks {column!r}.")
    weights = pd.to_numeric(
        n.snapshot_weightings[column].reindex(sns), errors="coerce"
    )
    if weights.isna().any() or not np.isfinite(weights).all() or weights.lt(0).any():
        raise ConstraintValidationError("Snapshot weights must be finite and non-negative.")
    return weights


def scope_mask(table: pd.DataFrame, scope: GeographicScope) -> pd.Series:
    mask = pd.Series(True, index=table.index)
    for column, requested in (
        ("bus", scope.bus),
        ("state_id", scope.state_id),
        ("region_id", scope.region_id),
    ):
        if requested is None:
            continue
        if column not in table:
            raise ConstraintValidationError(
                f"Scope {scope.label} requires missing component column {column!r}."
            )
        mask &= table[column].fillna("").astype(str).eq(str(requested))
    return mask


def selected_names(
    table: pd.DataFrame,
    scope: GeographicScope,
    *,
    carriers: tuple[str, ...] | None = None,
) -> list[str]:
    mask = scope_mask(table, scope)
    if carriers is not None:
        if "carrier" not in table:
            raise ConstraintValidationError("Carrier filtering requires a carrier column.")
        mask &= table["carrier"].isin(carriers)
    return table.index[mask].astype(str).tolist()



def require_members(
    n: pypsa.Network,
    table: pd.DataFrame,
    scope: GeographicScope,
    *,
    carriers: tuple[str, ...] | None = None,
    what: str = "components",
) -> list[str]:
    """Members of `table` in `scope`, distinguishing an empty set from a typo.

    A policy whose scope matches nothing is usually legitimate: coal retired
    before 2050, PSP absent from one region, a corridor not built until 2035.
    Aborting the run for those would make a multi-decade scenario unusable.

    But a policy that matches nothing because `carrier: Sollar` was misspelled
    must not silently do nothing -- that is how a cap quietly stops capping. So
    the carrier and scope are checked against the network's own vocabulary
    first: an unknown name raises, a known name matching no rows is reported as
    inapplicable for the caller to skip and record.
    """
    if carriers is not None:
        known = set(n.carriers.index.astype(str))
        unknown = sorted({str(c) for c in carriers} - known)
        if unknown and known:
            raise ConstraintValidationError(
                f"carrier(s) {unknown} are not in the network's carrier table "
                f"(have {sorted(known)[:12]}). Fix the spelling, or add the carrier."
            )
    if scope.bus is not None and str(scope.bus) not in set(n.buses.index.astype(str)):
        raise ConstraintValidationError(
            f"scope bus {scope.bus!r} is not a bus in this network "
            f"(have {sorted(n.buses.index.astype(str))[:12]})."
        )
    for field, value in (("state_id", scope.state_id), ("region_id", scope.region_id)):
        if value is None:
            continue
        seen = set()
        for frame in (n.buses, n.generators, n.stores, n.storage_units):
            if field in frame:
                seen |= set(frame[field].dropna().astype(str))
        if seen and str(value) not in seen:
            raise ConstraintValidationError(
                f"scope {field}={value!r} appears nowhere in this network "
                f"(have {sorted(seen)[:12]})."
            )

    names = selected_names(table, scope, carriers=carriers)
    if not names:
        carrier_text = f"carrier(s) {list(carriers)} " if carriers else ""
        raise ConstraintInapplicable(
            f"no {what} match {carrier_text}in {scope.label} this horizon"
        )
    return names


def input_matrix(
    n: pypsa.Network,
    component: str,
    attribute: str,
    names: list[str],
    snapshots: pd.Index,
) -> pd.DataFrame:
    sns = validate_snapshots(snapshots)
    static = n.c[component].static
    default = float(n.c[component].defaults.at[attribute, "default"])
    values = pd.DataFrame(default, index=sns, columns=names, dtype=float)
    if attribute in static:
        base = pd.to_numeric(static.loc[names, attribute], errors="coerce")
        for name in names:
            if pd.notna(base.at[name]):
                values[name] = float(base.at[name])
    dynamic = n.c[component].dynamic[attribute]
    present = [name for name in names if name in dynamic.columns]
    if present:
        override = dynamic.loc[:, present].reindex(sns).apply(pd.to_numeric, errors="coerce")
        values.loc[:, present] = override
    if values.isna().any().any() or not np.isfinite(values.to_numpy()).all():
        raise ConstraintValidationError(f"{component}.{attribute} contains missing/non-finite values.")
    return values


def load_series(
    n: pypsa.Network, scope: GeographicScope, snapshots: pd.Index
) -> pd.Series:
    # A valid scope with no load has no peak and no RPO denominator, so the
    # policy is inapplicable rather than broken. An invalid scope still raises.
    names = require_members(n, n.loads, scope, what="Loads")
    values = input_matrix(n, "Load", "p_set", names, snapshots)
    if values.lt(0).any().any():
        raise ConstraintValidationError(
            "India policy accounting requires non-negative Load.p_set; model exports "
            "with a dedicated Generator or Link instead of a negative Load."
        )
    return values.sum(axis=1)


def validate_nonnegative_generator_dispatch(
    n: pypsa.Network, names: list[str], snapshots: pd.Index
) -> None:
    """Reject signed Generator dispatch from energy/emissions accounting sets."""
    lower = input_matrix(n, "Generator", "p_min_pu", names, snapshots)
    if lower.lt(0).any().any():
        raise ConstraintValidationError(
            "Energy/emissions constraints require non-negative Generator dispatch. "
            "Signed conversion flows must use Links and a dedicated accounting rule."
        )


def weighted_energy_from_result(
    n: pypsa.Network,
    component: Literal["Generator", "StorageUnit", "Store"],
    attribute: str,
    names: list[str],
    snapshots: pd.Index,
    *,
    positive_only: bool = False,
    weight_column: str | None = None,
) -> float:
    """Weighted energy for `names` over `snapshots`, from the solved result.

    `weight_column` defaults by component and should almost never be passed.
    Under snapshot sampling `generators` is scaled so a year still totals 8760
    hours, while `stores` keeps the real elapsed interval. Storage energy on
    the annualised column would be inflated by the sampling factor.
    """
    if not names:
        return 0.0
    if weight_column is None:
        weight_column = "generators" if component == "Generator" else "stores"
    dynamic = n.c[component].dynamic[attribute]
    missing = set(names).difference(dynamic.columns)
    if missing:
        raise ConstraintAuditError(
            f"Solved {component}.{attribute} lacks columns: {sorted(missing)[:10]}"
        )
    values = dynamic.loc[pd.Index(snapshots), names]
    numeric = values.apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy()).all():
        raise ConstraintAuditError(
            f"Solved {component}.{attribute} contains missing/non-finite values."
        )
    values = numeric
    if positive_only:
        values = values.clip(lower=0)
    weights = snapshot_weights(n, snapshots, weight_column)
    return float(values.mul(weights, axis=0).sum().sum())


def model_financial_year_start(n: pypsa.Network, snapshots: pd.Index) -> int:
    label = n.meta.get("pypsa_india", {}).get("model_year")
    if isinstance(label, str) and label.startswith("FY") and len(label) >= 6:
        try:
            return int(label[2:6])
        except ValueError:
            pass
    first = validate_snapshots(snapshots)[0]
    return int(first.year if first.month >= 4 else first.year - 1)


def validate_nonnegative_finite(value: float, label: str) -> float:
    result = float(value)
    if not np.isfinite(result) or result < 0:
        raise ConstraintValidationError(f"{label} must be finite and non-negative.")
    return result


__all__ = [
    "GeographicScope",
    "coeff_like",
    "component_dim",
    "input_matrix",
    "load_series",
    "require_members",
    "model_financial_year_start",
    "model_variable",
    "safe_name",
    "scope_mask",
    "selected_names",
    "snapshot_weights",
    "validate_nonnegative_finite",
    "validate_nonnegative_generator_dispatch",
    "validate_snapshots",
    "weighted_energy_from_result",
]
