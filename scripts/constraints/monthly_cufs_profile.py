"""Monthly capacity-factor ceilings and floors for a generator carrier."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pandas as pd
import pypsa

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
    validate_snapshots,
    validate_nonnegative_generator_dispatch,
    weighted_energy_from_result,
)
from errors import ConstraintValidationError

# Fallback names used only when the carrier table has no variability flag.
_VARIABLE_RE_NAMES = {"solar", "wind", "onwind", "offwind", "ror", "solar_utility",
                      "solar_rooftop", "wind_onshore", "wind_offshore", "run_of_river"}


def _is_variable_renewable(n: pypsa.Network, carrier: str) -> bool | None:
    """Return the declared variability of `carrier`, or None when it is unknown.

    Data first (`Carrier.is_variable_renewable`), name heuristic second, and None when
    neither can answer -- the caller must then fail closed rather than guess.
    """
    flag = n.carriers.get("is_variable_renewable")
    if flag is not None and carrier in flag.index:
        value = flag.at[carrier]
        if value is not None and str(value) != "" and not pd.isna(value):
            return bool(value)
    if str(carrier).lower() in _VARIABLE_RE_NAMES:
        return True
    return None


@dataclass(frozen=True)
class MonthlyCufsProfile:
    name: str
    carrier: str
    max_cf_by_month: dict[int, float] = field(default_factory=dict)
    min_cf_by_month: dict[int, float] = field(default_factory=dict)
    scope: GeographicScope = GeographicScope()
    allow_variable_renewable_min: bool = False

    kind: ClassVar[str] = "monthly_cufs_profile"

    def to_dict(self):
        return spec_to_dict(self)

    def _names(self, n: pypsa.Network) -> list[str]:
        return require_members(
            n, n.generators, self.scope, carriers=(self.carrier,),
            what=f"{self.carrier!r} generators",
        )

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        validate_snapshots(snapshots)
        if not self.max_cf_by_month and not self.min_cf_by_month:
            raise ConstraintValidationError("At least one monthly CUF bound is required.")
        for label, mapping in (
            ("max_cf_by_month", self.max_cf_by_month),
            ("min_cf_by_month", self.min_cf_by_month),
        ):
            for month, value in mapping.items():
                if not isinstance(month, int) or isinstance(month, bool) or month not in range(1, 13):
                    raise ConstraintValidationError(f"{label} has invalid calendar month {month}.")
                number = float(value)
                if not np.isfinite(number) or number < 0 or number > 1:
                    raise ConstraintValidationError(f"{label}[{month}] must lie in [0, 1].")
        for month in set(self.max_cf_by_month).intersection(self.min_cf_by_month):
            if float(self.min_cf_by_month[month]) > float(self.max_cf_by_month[month]):
                raise ConstraintValidationError(f"Monthly minimum exceeds maximum for month {month}.")
        if (
            any(float(value) > 0 for value in self.min_cf_by_month.values())
            and not self.allow_variable_renewable_min
        ):
            variable = _is_variable_renewable(n, self.carrier)
            if variable is not False:
                reason = (
                    f"{self.carrier!r} is declared variable renewable"
                    if variable
                    else (
                        f"the carrier table does not declare whether {self.carrier!r} is "
                        "variable renewable (add an is_variable_renewable column), so this "
                        "is refused conservatively"
                    )
                )
                raise ConstraintValidationError(
                    "A minimum monthly CUF for variable renewable generation forces dispatch "
                    f"and suppresses legitimate curtailment: {reason}. Set "
                    "allow_variable_renewable_min=True only deliberately."
                )
        names = self._names(n)
        validate_nonnegative_generator_dispatch(n, names, snapshots)
        fixed, extendable = self._split(n, names)
        capacity = pd.to_numeric(n.generators.loc[names, "p_nom"], errors="coerce")
        if capacity.isna().any() or not np.isfinite(capacity).all():
            raise ConstraintValidationError("Monthly CUF capacity must be finite.")
        if capacity.loc[fixed].sum() <= 0 and not extendable:
            raise ConstraintValidationError(
                "Monthly CUF denominator capacity must be positive: no fixed capacity "
                "and nothing extendable to build."
            )

    def _split(self, n: pypsa.Network, names: list[str]) -> tuple[list[str], list[str]]:
        """Partition members into fixed-capacity and extendable."""
        flag = n.generators.loc[names, "p_nom_extendable"].astype(bool)
        return flag.index[~flag].astype(str).tolist(), flag.index[flag].astype(str).tolist()

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        sns = validate_snapshots(snapshots)
        names = self._names(n)
        fixed, extendable = self._split(n, names)
        fixed_capacity = float(
            pd.to_numeric(n.generators.loc[fixed, "p_nom"], errors="coerce").sum()
        )

        var = model_variable(n, "Generator-p")
        dim = component_dim(var)
        weights = snapshot_weights(n, sns)

        # For extendable members, move the capacity term to the left so the
        # capacity-factor bound remains linear during model construction:
        #
        #   sum_t w_t p_t  -  CF (sum_t w_t) sum_i p_nom_i^ext  <=  CF (sum_t w_t) P_fixed
        #
        capacity_var = None
        if extendable:
            p_nom = model_variable(n, "Generator-p_nom")
            capacity_var = p_nom.sel({component_dim(p_nom): extendable}).sum()

        for month in sorted(set(self.max_cf_by_month) | set(self.min_cf_by_month)):
            month_sns = sns[sns.month == int(month)]
            if month_sns.empty:
                continue
            selected = var.sel({dim: names, "snapshot": list(month_sns)})
            month_weights = weights.reindex(month_sns)
            energy = (
                coeff_like(selected, snapshot_values=month_weights.to_numpy()) * selected
            ).sum()
            weight_sum = float(month_weights.sum())
            tag = f"{safe_name(self.name)}_{int(month):02d}"

            for bound, mapping, sense in (
                ("max", self.max_cf_by_month, "<="),
                ("min", self.min_cf_by_month, ">="),
            ):
                if month not in mapping:
                    continue
                factor = float(mapping[month]) * weight_sum
                rhs = factor * fixed_capacity
                lhs = energy if capacity_var is None else energy - factor * capacity_var
                constraint = lhs <= rhs if sense == "<=" else lhs >= rhs
                n.model.add_constraints(constraint, name=f"india_monthly_{bound}_{tag}")

    def audit(self, n: pypsa.Network, snapshots: pd.Index):
        sns = validate_snapshots(snapshots)
        names = self._names(n)
        # p_nom_opt after a solve, p_nom when the network is unsolved.
        column = "p_nom_opt" if "p_nom_opt" in n.generators else "p_nom"
        capacity = float(
            pd.to_numeric(n.generators.loc[names, column], errors="coerce").fillna(0).sum()
        )
        weights = snapshot_weights(n, sns)
        records: list[ConstraintAudit] = []
        for month in sorted(set(self.max_cf_by_month) | set(self.min_cf_by_month)):
            month_sns = sns[sns.month == int(month)]
            if month_sns.empty:
                continue
            energy = weighted_energy_from_result(n, "Generator", "p", names, month_sns)
            denominator = capacity * float(weights.reindex(month_sns).sum())
            detail = (
                f"carrier={self.carrier}; calendar_month={int(month)}; "
                f"capacity_column={column}"
            )
            if month in self.max_cf_by_month:
                rhs = float(self.max_cf_by_month[month]) * denominator
                records.append(
                    ConstraintAudit(
                        f"{self.name}:max:{int(month):02d}", self.kind, self.scope.label,
                        energy, "<=", rhs, "MWh_el", audit_passes(energy, "<=", rhs), detail,
                    )
                )
            if month in self.min_cf_by_month:
                rhs = float(self.min_cf_by_month[month]) * denominator
                records.append(
                    ConstraintAudit(
                        f"{self.name}:min:{int(month):02d}", self.kind, self.scope.label,
                        energy, ">=", rhs, "MWh_el", audit_passes(energy, ">=", rhs), detail,
                    )
                )
        return records


__all__ = ["MonthlyCufsProfile"]
