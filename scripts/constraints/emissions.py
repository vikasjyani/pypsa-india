"""CO2 and fuel-budget constraints with explicit energy bases."""

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
    model_variable,
    safe_name,
    require_members,
    selected_names,
    snapshot_weights,
    validate_nonnegative_finite,
    validate_nonnegative_generator_dispatch,
    validate_snapshots,
)
from errors import ConstraintValidationError

EnergyBasis = Literal["electrical_output", "fuel_input"]


def assert_basis_matches_data(n: pypsa.Network, basis: EnergyBasis) -> None:
    """Refuse a basis the network's own emission factors are not on.

    ``Carrier.co2_emissions`` carries a single number and nothing in it says which
    basis it is on. Choosing the wrong one is silent and large: a fuel-basis coal
    factor read as output-basis understates emissions by ``1/efficiency`` (~2.6x).
    The importer records the bundle's basis in ``n.meta``; if it is absent (a
    hand-built network) we cannot check and stay out of the way.
    """
    declared = n.meta.get("pypsa_india", {}).get("co2_emission_basis")
    if declared and declared != basis:
        raise ConstraintValidationError(
            f"Carrier.co2_emissions holds {declared!r}-basis factors (recorded at "
            f"import) but this constraint declares basis={basis!r}. Mixing the two "
            "misstates emissions by a factor of 1/efficiency. Use "
            f"basis={declared!r}, or rebuild the network from a bundle whose factors "
            f"are on the {basis!r} basis."
        )


def _dispatch_coefficients(
    n: pypsa.Network,
    names: list[str],
    *,
    carrier_factor: bool,
    basis: EnergyBasis,
) -> np.ndarray:
    if carrier_factor:
        if "co2_emissions" not in n.carriers:
            raise ConstraintValidationError("Carriers lack co2_emissions.")
        factors = n.generators.loc[names, "carrier"].map(n.carriers.co2_emissions)
    else:
        factors = pd.Series(1.0, index=names)
    factors = pd.to_numeric(factors, errors="coerce")
    if factors.isna().any() or not np.isfinite(factors).all() or factors.lt(0).any():
        raise ConstraintValidationError("Emission/fuel factors must be finite and non-negative.")
    if basis == "fuel_input":
        efficiency = pd.to_numeric(n.generators.loc[names, "efficiency"], errors="coerce")
        if efficiency.isna().any() or not np.isfinite(efficiency).all() or efficiency.le(0).any():
            raise ConstraintValidationError(
                "Fuel-input accounting requires finite positive static generator efficiency."
            )
        factors = factors / efficiency
    return factors.to_numpy(dtype=float)


def _weighted_dispatch_expression(
    n: pypsa.Network,
    snapshots: pd.Index,
    names: list[str],
    factors: np.ndarray,
):
    var = model_variable(n, "Generator-p")
    dim = component_dim(var)
    selected = var.sel({dim: names, "snapshot": list(snapshots)})
    weights = snapshot_weights(n, snapshots)
    coefficients = coeff_like(
        selected,
        snapshot_values=weights.to_numpy(),
        component_values=factors,
        component_dim_name=dim,
    )
    return (coefficients * selected).sum()


def _weighted_dispatch_value(
    n: pypsa.Network,
    snapshots: pd.Index,
    names: list[str],
    factors: np.ndarray,
) -> float:
    values = n.generators_t.p.loc[pd.Index(snapshots), names]
    if not np.isfinite(values.to_numpy()).all():
        raise ConstraintValidationError(
            "Solved Generator.p contains missing/non-finite values."
        )
    weights = snapshot_weights(n, snapshots)
    return float(
        values.mul(weights, axis=0).mul(pd.Series(factors, index=names), axis=1).sum().sum()
    )


@dataclass(frozen=True)
class CO2EmissionLimit:
    name: str
    limit_tco2: float
    basis: EnergyBasis
    scope: GeographicScope = GeographicScope()

    kind: ClassVar[str] = "co2_emission_limit"

    def to_dict(self):
        return spec_to_dict(self)

    def _names(self, n: pypsa.Network) -> list[str]:
        return require_members(n, n.generators, self.scope, what="generators")

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        validate_snapshots(snapshots)
        validate_nonnegative_finite(self.limit_tco2, "limit_tco2")
        if self.basis not in {"electrical_output", "fuel_input"}:
            raise ConstraintValidationError("CO2 basis must be electrical_output or fuel_input.")
        assert_basis_matches_data(n, self.basis)
        names = self._names(n)
        validate_nonnegative_generator_dispatch(n, names, snapshots)
        _dispatch_coefficients(n, names, carrier_factor=True, basis=self.basis)

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        names = self._names(n)
        factors = _dispatch_coefficients(n, names, carrier_factor=True, basis=self.basis)
        expr = _weighted_dispatch_expression(n, snapshots, names, factors)
        n.model.add_constraints(
            expr <= float(self.limit_tco2),
            name=f"india_co2_{safe_name(self.name)}",
        )

    def audit(self, n: pypsa.Network, snapshots: pd.Index):
        names = self._names(n)
        factors = _dispatch_coefficients(n, names, carrier_factor=True, basis=self.basis)
        actual = _weighted_dispatch_value(n, snapshots, names, factors)
        limit = float(self.limit_tco2)
        return [
            ConstraintAudit(
                self.name,
                self.kind,
                self.scope.label,
                actual,
                "<=",
                limit,
                "tCO2",
                audit_passes(actual, "<=", limit),
                f"basis={self.basis}",
            )
        ]


@dataclass(frozen=True)
class FuelSupplyLimit:
    name: str
    carrier: str
    limit_mwh: float
    basis: EnergyBasis
    scope: GeographicScope = GeographicScope()

    kind: ClassVar[str] = "fuel_supply_limit"

    def to_dict(self):
        return spec_to_dict(self)

    def _names(self, n: pypsa.Network) -> list[str]:
        return require_members(
            n, n.generators, self.scope, carriers=(self.carrier,),
            what=f"{self.carrier!r} generators",
        )

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        validate_snapshots(snapshots)
        validate_nonnegative_finite(self.limit_mwh, "limit_mwh")
        if self.basis not in {"electrical_output", "fuel_input"}:
            raise ConstraintValidationError("Fuel basis must be electrical_output or fuel_input.")
        names = self._names(n)
        validate_nonnegative_generator_dispatch(n, names, snapshots)
        _dispatch_coefficients(n, names, carrier_factor=False, basis=self.basis)

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        names = self._names(n)
        factors = _dispatch_coefficients(n, names, carrier_factor=False, basis=self.basis)
        expr = _weighted_dispatch_expression(n, snapshots, names, factors)
        n.model.add_constraints(
            expr <= float(self.limit_mwh),
            name=f"india_fuel_{safe_name(self.name)}",
        )

    def audit(self, n: pypsa.Network, snapshots: pd.Index):
        names = self._names(n)
        factors = _dispatch_coefficients(n, names, carrier_factor=False, basis=self.basis)
        actual = _weighted_dispatch_value(n, snapshots, names, factors)
        limit = float(self.limit_mwh)
        unit = "MWh_fuel" if self.basis == "fuel_input" else "MWh_el"
        return [
            ConstraintAudit(
                self.name,
                self.kind,
                self.scope.label,
                actual,
                "<=",
                limit,
                unit,
                audit_passes(actual, "<=", limit),
                f"carrier={self.carrier}; basis={self.basis}",
            )
        ]


__all__ = ["CO2EmissionLimit", "EnergyBasis", "FuelSupplyLimit"]
