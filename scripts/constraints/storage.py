"""Per-store discharge-throughput limit for each calendar window."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

import numpy as np
import pandas as pd
import pypsa
import xarray as xr

from constraints.base import ConstraintAudit, audit_passes, spec_to_dict
from constraints.helpers import (
    GeographicScope,
    coeff_like,
    component_dim,
    model_variable,
    safe_name,
    require_members,
    selected_names,
    snapshot_weights,
    validate_nonnegative_finite,
    validate_snapshots,
)
from errors import ConstraintValidationError

CycleWindow = Literal["daily", "weekly", "monthly", "annual"]

#: Calendar period alias per supported non-annual window.
_WINDOW_FREQ = {"daily": "D", "weekly": "W", "monthly": "M"}

#: Store snapshot weights represent elapsed hours for throughput accounting.
_WEIGHT = "stores"


@dataclass(frozen=True)
class StoreCyclingLimit:
    """Limit each store's discharge throughput to cycles times energy capacity.

    In expansion runs energy capacity may be a decision variable; with fixed
    capacity the same relation directly limits discharge throughput.
    """

    name: str
    max_cycles: float
    window: CycleWindow = "daily"
    carrier: str | None = None
    scope: GeographicScope = GeographicScope()
    #: Largest accepted snapshot interval in hours.
    max_snapshot_hours: float = 1.0

    kind: ClassVar[str] = "store_cycling"

    def to_dict(self):
        return spec_to_dict(self)

    def _names(self, n: pypsa.Network) -> list[str]:
        carriers = (self.carrier,) if self.carrier else None
        return require_members(
            n, n.stores, self.scope, carriers=carriers, what="Store rows",
        )

    def _capacity(self, n: pypsa.Network, names: list[str], column: str = "e_nom") -> pd.Series:
        capacity = pd.to_numeric(n.stores.loc[names, column], errors="coerce")
        if capacity.isna().any() or not np.isfinite(capacity).all() or capacity.lt(0).any():
            raise ConstraintValidationError(f"Store.{column} must be finite and non-negative.")
        return capacity

    def _split(self, n: pypsa.Network, names: list[str]) -> tuple[list[str], list[str]]:
        flag = n.stores.loc[names, "e_nom_extendable"].astype(bool)
        return flag.index[~flag].astype(str).tolist(), flag.index[flag].astype(str).tolist()

    def _windows(self, sns: pd.DatetimeIndex):
        """(label, snapshots) per calendar window, in order."""
        if self.window == "annual":
            yield "annual", sns
            return
        labels = sns.to_period(_WINDOW_FREQ[self.window])
        for label in labels.unique():
            yield str(label), sns[labels == label]

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        sns = validate_snapshots(snapshots)
        validate_nonnegative_finite(self.max_cycles, "max_cycles")
        if self.window not in ("daily", "weekly", "monthly", "annual"):
            raise ConstraintValidationError(
                f"window must be daily, weekly, monthly or annual; got {self.window!r}."
            )
        step = float(snapshot_weights(n, sns, _WEIGHT).max())
        if step > float(self.max_snapshot_hours) + 1e-9:
            raise ConstraintValidationError(
                f"Store cycling {self.name!r} needs near-hourly snapshots: this model "
                f"samples {step:g} h and the limit accepts {float(self.max_snapshot_hours):g} h. "
                "Coarser sampling averages away the intra-window charge/discharge swings "
                "that make up a cycle, so the count understates. Lower snapshots.resolution, "
                "or raise max_snapshot_hours to accept an understated count."
            )
        names = self._names(n)
        fixed, _ = self._split(n, names)
        capacity = self._capacity(n, names)
        vacuous = capacity.loc[fixed].index[capacity.loc[fixed].le(0)].tolist()
        if vacuous:
            raise ConstraintValidationError(
                f"Fixed Store rows {vacuous[:5]} have no energy capacity, so their cycling "
                "limit is vacuous. Give them e_nom, or exclude them from the scope."
            )

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        sns = validate_snapshots(snapshots)
        names = self._names(n)
        fixed, extendable = self._split(n, names)
        cycles = float(self.max_cycles)

        store_p = model_variable(n, "Store-p")
        dim = component_dim(store_p)
        selected = store_p.sel({dim: names, "snapshot": list(sns)})

        # Store-p is signed; cycling counts only positive (discharging) flow.
        discharge = n.model.add_variables(
            lower=0, coords=selected.coords, name=f"India-Store-discharge-{safe_name(self.name)}"
        )
        n.model.add_constraints(
            discharge - selected >= 0, name=f"india_store_discharge_aux_{safe_name(self.name)}"
        )

        e_nom = model_variable(n, "Store-e_nom") if extendable else None
        capacity = self._capacity(n, names)

        for label, window in self._windows(sns):
            weights = snapshot_weights(n, window, _WEIGHT)
            tag = f"{safe_name(self.name)}_{safe_name(label)}"
            for group, is_extendable in ((fixed, False), (extendable, True)):
                if not group:
                    continue
                picked = discharge.sel({dim: group, "snapshot": list(window)})
                # Keep the store dimension so this call emits one row per store.
                throughput = (
                    coeff_like(picked, snapshot_values=weights.to_numpy()) * picked
                ).sum("snapshot")
                if is_extendable:
                    built = e_nom.sel({component_dim(e_nom): group})
                    constraint = throughput - cycles * built <= 0
                else:
                    # Label-index the right-hand side to preserve store alignment.
                    rhs = xr.DataArray(
                        cycles * capacity.loc[group].to_numpy(),
                        dims=[dim], coords={dim: group},
                    )
                    constraint = throughput <= rhs
                suffix = "ext" if is_extendable else "fix"
                n.model.add_constraints(
                    constraint, name=f"india_store_cycles_{tag}_{suffix}"
                )

    def audit(self, n: pypsa.Network, snapshots: pd.Index):
        sns = validate_snapshots(snapshots)
        names = self._names(n)
        column = "e_nom_opt" if "e_nom_opt" in n.stores else "e_nom"
        capacity = self._capacity(n, names, column)
        discharge = n.stores_t.p.loc[sns, names]
        if not np.isfinite(discharge.to_numpy()).all():
            raise ConstraintValidationError(
                "Solved Store.p contains missing/non-finite values."
            )
        discharge = discharge.clip(lower=0)
        cycles = float(self.max_cycles)

        records: list[ConstraintAudit] = []
        for label, window in self._windows(sns):
            weights = snapshot_weights(n, window, _WEIGHT)
            energy = discharge.loc[window].mul(weights, axis=0).sum()
            for store in names:
                limit = cycles * float(capacity.at[store])
                actual = float(energy.at[store])
                records.append(
                    ConstraintAudit(
                        f"{self.name}:{store}:{label}", self.kind, self.scope.label,
                        actual, "<=", limit, "MWh_discharge", audit_passes(actual, "<=", limit),
                        f"store={store}; window={self.window}:{label}; "
                        f"energy_capacity_mwh={float(capacity.at[store]):.6f}; "
                        f"capacity_column={column}",
                    )
                )
        return records


__all__ = ["CycleWindow", "StoreCyclingLimit"]
