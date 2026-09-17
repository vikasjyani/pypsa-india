"""Aggregate per-year capacity ceiling for all generators of one carrier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd
import pypsa

from constraints.base import ConstraintAudit, audit_passes, spec_to_dict
from constraints.helpers import (
    GeographicScope, component_dim, model_financial_year_start, model_variable,
    safe_name, selected_names, validate_nonnegative_finite, validate_snapshots,
)
from errors import ConstraintValidationError


def _capacity_parts(table: pd.DataFrame, names: list[str]) -> tuple[list[str], list[str], float]:
    """Return fixed/extendable members and checked fixed MW capacity."""
    if not names:
        return [], [], 0.0
    capacity = pd.to_numeric(table.loc[names, "p_nom"], errors="coerce")
    if capacity.isna().any() or not np.isfinite(capacity).all() or capacity.lt(0).any():
        raise ConstraintValidationError("Capacity p_nom must be finite and non-negative.")
    flag = table.loc[names, "p_nom_extendable"].astype(bool)
    fixed = flag.index[~flag].astype(str).tolist()
    extendable = flag.index[flag].astype(str).tolist()
    return fixed, extendable, float(capacity.loc[fixed].sum())


def _result_capacity(table: pd.DataFrame, names: list[str]) -> float:
    """Use solved capacity where present; input capacity is the unsolved fallback."""
    if not names:
        return 0.0
    capacity = pd.to_numeric(table.loc[names, "p_nom"], errors="coerce")
    if "p_nom_opt" in table:
        solved = pd.to_numeric(table.loc[names, "p_nom_opt"], errors="coerce")
        capacity = solved.where(solved.notna(), capacity)
    if capacity.isna().any() or not np.isfinite(capacity).all():
        raise ConstraintValidationError("Solved capacity is missing or non-finite.")
    return float(capacity.sum())


@dataclass(frozen=True)
class TechnologyPhaseOut:
    """Cap active fixed and extendable capacity for the selected carrier."""

    name: str
    carrier: str
    max_capacity_mw: float = 0.0
    scope: GeographicScope = GeographicScope()
    kind: ClassVar[str] = "technology_phase_out"

    def to_dict(self):
        return spec_to_dict(self)

    def _state(self, n: pypsa.Network, snapshots: pd.Index):
        names = selected_names(n.generators, self.scope, carriers=(self.carrier,))
        _, extendable, fixed = _capacity_parts(n.generators, names)
        return names, extendable, fixed, float(self.max_capacity_mw), model_financial_year_start(n, snapshots)

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        validate_snapshots(snapshots)
        if not str(self.carrier).strip():
            raise ConstraintValidationError("Phase-out carrier must be non-blank.")
        validate_nonnegative_finite(self.max_capacity_mw, "max_capacity_mw")
        _, _, fixed, limit, fy = self._state(n, snapshots)
        if fixed > limit + 1e-6:
            raise ConstraintValidationError(
                f"Technology phase-out {self.name!r} is infeasible in FY{fy}: fixed "
                f"{self.carrier!r} capacity {fixed:.3f} MW already exceeds the "
                f"{limit:.3f} MW allowed that year. A model cannot retire capacity it "
                f"was handed as fixed — soften max_capacity_mw for FY{fy}, or remove the "
                "plant from the network inputs so it is never built."
            )

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        _, extendable, fixed, limit, _ = self._state(n, snapshots)
        if not extendable:
            return
        variable = model_variable(n, "Generator-p_nom")
        n.model.add_constraints(
            variable.sel({component_dim(variable): extendable}).sum() <= limit - fixed,
            name=f"india_phase_out_{safe_name(self.name)}",
        )

    def audit(self, n: pypsa.Network, snapshots: pd.Index):
        names, _, _, limit, fy = self._state(n, snapshots)
        actual = _result_capacity(n.generators, names)
        return [ConstraintAudit(
            self.name, self.kind, self.scope.label, actual, "<=", limit, "MW_p_nom",
            audit_passes(actual, "<=", limit),
            f"carrier={self.carrier}; model_fy={fy}; policy_limit_mw={limit:.6f}",
        )]


__all__ = ["TechnologyPhaseOut"]
