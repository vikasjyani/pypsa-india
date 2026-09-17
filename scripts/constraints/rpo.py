"""India-ready renewable purchase/consumption share constraint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

import numpy as np
import pandas as pd
import pypsa

from constraints.base import ConstraintAudit, audit_passes, spec_to_dict
from constraints.helpers import (
    GeographicScope,
    coeff_like,
    component_dim,
    load_series,
    model_variable,
    safe_name,
    selected_names,
    snapshot_weights,
    validate_snapshots,
    validate_nonnegative_generator_dispatch,
    weighted_energy_from_result,
)
from errors import ConstraintValidationError


@dataclass(frozen=True)
class RPOConstraint:
    """Require eligible generation to meet a served-load share in one explicit scope."""

    name: str
    target_share: float
    eligible_carriers: tuple[str, ...]
    scope: GeographicScope
    category: str = "total_renewable"
    policy_vintage: str = ""
    accounting_basis: Literal["served_load"] = "served_load"

    kind: ClassVar[str] = "renewable_share_rpo"

    def to_dict(self):
        return spec_to_dict(self)

    def _eligible(self, n: pypsa.Network) -> list[str]:
        names = selected_names(
            n.generators, self.scope, carriers=tuple(self.eligible_carriers)
        )
        if not names:
            raise ConstraintValidationError(
                f"RPO {self.name!r} has no eligible generators in {self.scope.label}; "
                "an empty eligible set is an error, not a whole-builder early return."
            )
        return names

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        validate_snapshots(snapshots)
        target = float(self.target_share)
        if not np.isfinite(target) or target <= 0 or target > 1:
            raise ConstraintValidationError("RPO target_share must be finite and in (0, 1].")
        if not self.eligible_carriers or any(not str(x).strip() for x in self.eligible_carriers):
            raise ConstraintValidationError("RPO eligible_carriers must be explicit and non-empty.")
        if self.accounting_basis != "served_load":
            raise ConstraintValidationError("v0.1 supports only accounting_basis='served_load'.")
        eligible = self._eligible(n)
        validate_nonnegative_generator_dispatch(n, eligible, snapshots)
        demand = load_series(n, self.scope, snapshots)
        if (demand < 0).any() or not np.isfinite(demand).all():
            raise ConstraintValidationError("RPO served-load denominator must be finite/non-negative.")
        if float(demand.sum()) <= 0:
            raise ConstraintValidationError("RPO served-load denominator is zero.")

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        eligible = self._eligible(n)
        var = model_variable(n, "Generator-p")
        dim = component_dim(var)
        selected = var.sel({dim: eligible, "snapshot": list(snapshots)})
        weights = snapshot_weights(n, snapshots)
        eligible_energy = (
            coeff_like(selected, snapshot_values=weights.to_numpy()) * selected
        ).sum()
        denominator = float((load_series(n, self.scope, snapshots) * weights).sum())
        n.model.add_constraints(
            eligible_energy >= float(self.target_share) * denominator,
            name=f"india_rpo_{safe_name(self.name)}",
        )

    def audit(self, n: pypsa.Network, snapshots: pd.Index):
        eligible = self._eligible(n)
        weights = snapshot_weights(n, snapshots)
        eligible_mwh = weighted_energy_from_result(
            n, "Generator", "p", eligible, snapshots
        )
        served_mwh = float((load_series(n, self.scope, snapshots) * weights).sum())
        required = float(self.target_share) * served_mwh
        share = eligible_mwh / served_mwh if served_mwh else float("nan")
        return [
            ConstraintAudit(
                self.name,
                self.kind,
                self.scope.label,
                eligible_mwh,
                ">=",
                required,
                "MWh_el",
                audit_passes(eligible_mwh, ">=", required),
                f"category={self.category}; achieved_share={share:.8f}; "
                f"policy_vintage={self.policy_vintage or 'unspecified'}; "
                "local eligible generation only; REC/import procurement not inferred",
            )
        ]


__all__ = ["RPOConstraint"]
