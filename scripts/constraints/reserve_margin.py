"""Deterministic planning reserve margin for dispatch and expansion studies.

The constraint requires credited generator and storage capacity to cover the
highest-demand snapshot plus the configured margin. Generator credits are either
provided explicitly or derived from availability in the selected peak hours.
"""

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
    input_matrix,
    load_series,
    model_variable,
    safe_name,
    selected_names,
    validate_snapshots,
)
from errors import ConstraintValidationError

#: Generators that exist to price shortage, not to serve it.
_NON_FIRM_CARRIERS = {"unserved_energy"}


@dataclass(frozen=True)
class ReserveMargin:
    name: str
    margin: float
    scope: GeographicScope = GeographicScope()
    #: Capacity credit per carrier. Missing credits are derived from mean
    #: availability over the selected highest-demand hours.
    capacity_credit_by_carrier: dict[str, float] = field(default_factory=dict)
    #: Number of highest-demand hours used for derived credits.
    peak_hours: int = 1
    #: Duration of the peak event used to convert storage energy to MW credit.
    storage_peak_duration_h: float | None = None

    kind: ClassVar[str] = "reserve_margin"

    def to_dict(self):
        return spec_to_dict(self)

    # ------------------------------------------------------------- requirement

    def _peak(self, n: pypsa.Network, snapshots: pd.Index):
        """(required MW, peak snapshot, the `peak_hours` highest-demand hours)."""
        sns = validate_snapshots(snapshots)
        demand = load_series(n, self.scope, sns)
        order = demand.sort_values(ascending=False, kind="stable").index
        top = order[: max(1, int(self.peak_hours))]
        peak_snapshot = order[0]
        required = float(demand.loc[peak_snapshot]) * (1.0 + float(self.margin))
        return required, peak_snapshot, top

    # ------------------------------------------------------- generator credit

    def _generator_credit(self, n: pypsa.Network, snapshots: pd.Index, top: pd.Index):
        """(names, per-generator capacity credit)."""
        sns = validate_snapshots(snapshots)
        names = [
            name for name in selected_names(n.generators, self.scope)
            if str(n.generators.at[name, "carrier"]) not in _NON_FIRM_CARRIERS
        ]
        if not names:
            raise ConstraintValidationError(
                f"No firm-capacity generators match {self.scope.label}."
            )
        availability = input_matrix(n, "Generator", "p_max_pu", names, sns)
        if availability.lt(0).any().any() or availability.gt(1).any().any():
            raise ConstraintValidationError(
                "Reserve-margin Generator.p_max_pu must lie in [0, 1]; values outside "
                "that range do not define a nameplate capacity credit."
            )
        derived = availability.loc[top].mean()
        credit = pd.Series(
            [
                float(self.capacity_credit_by_carrier[str(n.generators.at[name, "carrier"])])
                if str(n.generators.at[name, "carrier"]) in self.capacity_credit_by_carrier
                else float(derived.at[name])
                for name in names
            ],
            index=names, dtype=float,
        )
        capacity = pd.to_numeric(n.generators.loc[names, "p_nom"], errors="coerce")
        if capacity.isna().any() or not np.isfinite(capacity).all() or capacity.lt(0).any():
            raise ConstraintValidationError(
                "Reserve-margin Generator.p_nom must be finite and non-negative."
            )
        return names, credit

    @staticmethod
    def _split(table: pd.DataFrame, names: list[str], flag: str):
        marker = table.loc[names, flag].astype(bool)
        return (marker.index[~marker].astype(str).tolist(),
                marker.index[marker].astype(str).tolist())

    # --------------------------------------------------------- storage credit

    def _storage_channels(self, n: pypsa.Network) -> list[dict]:
        """One channel per bus carrying storage, with its energy and power members.

        Storage faces two limits and must respect both: enough *energy* to last
        the peak event, and enough *power* to deliver it. In this workflow
        storage sits on shared `Store` / `Storage` role buses and reaches load
        through links, so the link rating is a real ceiling on delivery -- crediting
        energy the network cannot deliver would overstate firm capacity.

        `max_hours` is static, so a StorageUnit's energy limit collapses to a
        coefficient: a 6 h unit against a 4 h event is credited 1.0, a 2 h unit 0.5.
        A Store holds MWh directly, so its limit is `e_nom / duration`.
        """
        if self.storage_peak_duration_h is None:
            return []
        duration = float(self.storage_peak_duration_h)
        channels: dict[str, dict] = {}

        units = selected_names(n.storage_units, self.scope) if len(n.storage_units) else []
        for name in units:
            bus = str(n.storage_units.at[name, "bus"])
            hours = float(pd.to_numeric(n.storage_units.at[name, "max_hours"], errors="coerce"))
            if not np.isfinite(hours) or hours <= 0:
                raise ConstraintValidationError(
                    f"StorageUnit {name!r} needs a positive max_hours to be credited."
                )
            channels.setdefault(bus, {"bus": bus, "units": [], "stores": []})
            channels[bus]["units"].append((name, min(1.0, hours / duration)))

        for name in (selected_names(n.stores, self.scope) if len(n.stores) else []):
            bus = str(n.stores.at[name, "bus"])
            channels.setdefault(bus, {"bus": bus, "units": [], "stores": []})
            channels[bus]["stores"].append((name, 1.0 / duration))

        for bus, channel in channels.items():
            # Links in either direction can deliver; p_nom bounds the sending-end
            # flow and link efficiency is handled by the network equations.
            channel["links"] = n.links.index[
                n.links.bus0.astype(str).eq(bus) | n.links.bus1.astype(str).eq(bus)
            ].astype(str).tolist()
        return list(channels.values())

    # ----------------------------------------------------------------- checks

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        margin = float(self.margin)
        if not np.isfinite(margin) or margin < 0:
            raise ConstraintValidationError("Reserve margin must be a non-negative ratio.")
        if int(self.peak_hours) < 1:
            raise ConstraintValidationError("peak_hours must be at least 1.")
        if self.storage_peak_duration_h is not None:
            duration = float(self.storage_peak_duration_h)
            if not np.isfinite(duration) or duration <= 0:
                raise ConstraintValidationError(
                    "storage_peak_duration_h must be a positive number of hours."
                )
        for carrier, credit in self.capacity_credit_by_carrier.items():
            value = float(credit)
            if not np.isfinite(value) or value < 0 or value > 1:
                raise ConstraintValidationError(
                    f"Capacity credit for {carrier!r} must lie in [0, 1]."
                )
        required, peak, top = self._peak(n, snapshots)
        names, credit = self._generator_credit(n, snapshots, top)
        _, extendable = self._split(n.generators, names, "p_nom_extendable")
        fixed_available = self._fixed_available(n, names, credit)
        storage = self._storage_channels(n)
        if not extendable and not storage and fixed_available + 1e-6 < required:
            raise ConstraintValidationError(
                f"Reserve margin {self.name!r} is infeasible at {peak}: fixed available "
                f"capacity={fixed_available:.3f} MW, required={required:.3f} MW and "
                "nothing extendable matches the scope."
            )

    def _fixed_available(self, n, names, credit) -> float:
        fixed, _ = self._split(n.generators, names, "p_nom_extendable")
        capacity = pd.to_numeric(n.generators.loc[fixed, "p_nom"], errors="coerce")
        return float((capacity * credit.loc[fixed]).sum())

    # ------------------------------------------------------------------- model

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        sns = validate_snapshots(snapshots)
        required, _, top = self._peak(n, sns)
        names, credit = self._generator_credit(n, sns, top)
        fixed, extendable = self._split(n.generators, names, "p_nom_extendable")
        fixed_available = self._fixed_available(n, names, credit)

        terms = []
        if extendable:
            variable = model_variable(n, "Generator-p_nom")
            selected = variable.sel({component_dim(variable): extendable})
            terms.append(
                (coeff_like(selected, component_values=credit.loc[extendable].to_numpy())
                 * selected).sum()
            )

        tag = safe_name(self.name)
        for channel in self._storage_channels(n):
            reserve, constant = self._storage_reserve(n, channel, tag)
            if reserve is None:
                fixed_available += constant
                continue
            terms.append(reserve)

        if not terms:
            return  # validation has already checked the fixed-only case
        n.model.add_constraints(
            sum(terms) >= required - fixed_available,
            name=f"india_reserve_margin_{tag}",
        )

    def _storage_reserve(self, n: pypsa.Network, channel: dict, tag: str):
        """A non-negative credit for one storage bus, bounded by energy and power.

        Returns (variable, 0.0) when anything on the channel is a decision
        variable, else (None, the constant MW it contributes).
        """
        bus = channel["bus"]
        energy_var, energy_fixed = self._channel_energy(n, channel)
        power_var, power_fixed = self._channel_power(n, channel)

        if energy_var is None and power_var is None:
            return None, min(energy_fixed, power_fixed)

        label = f"{tag}_{safe_name(bus)}"
        reserve = n.model.add_variables(lower=0, name=f"India-reserve-{label}")
        self._bound(n, reserve, energy_var, energy_fixed, f"india_reserve_energy_{label}")
        self._bound(n, reserve, power_var, power_fixed, f"india_reserve_power_{label}")
        return reserve, 0.0

    @staticmethod
    def _bound(n: pypsa.Network, reserve, variable, constant: float, name: str) -> None:
        """`reserve <= variable + constant`, with the variable moved to the left."""
        if variable is None:
            if np.isfinite(constant):
                n.model.add_constraints(reserve <= constant, name=name)
            return  # an infinite ceiling is not a constraint
        n.model.add_constraints(reserve - variable <= constant, name=name)

    def _channel_energy(self, n, channel):
        """Sustainable MW from the energy on this bus: (variable part, fixed part)."""
        expression, constant = None, 0.0
        for component, members, attribute, flag in (
            ("StorageUnit", channel["units"], "p_nom", "p_nom_extendable"),
            ("Store", channel["stores"], "e_nom", "e_nom_extendable"),
        ):
            if not members:
                continue
            table = n.storage_units if component == "StorageUnit" else n.stores
            names = [name for name, _ in members]
            coefficients = pd.Series(dict(members), dtype=float)
            marker = table.loc[names, flag].astype(bool)
            fixed = marker.index[~marker].astype(str).tolist()
            extendable = marker.index[marker].astype(str).tolist()
            if fixed:
                values = pd.to_numeric(table.loc[fixed, attribute], errors="coerce")
                constant += float((values * coefficients.loc[fixed]).sum())
            if extendable:
                variable = model_variable(n, f"{component}-{attribute}")
                selected = variable.sel({component_dim(variable): extendable})
                term = (coeff_like(
                    selected, component_values=coefficients.loc[extendable].to_numpy()
                ) * selected).sum()
                expression = term if expression is None else expression + term
        return expression, constant

    def _channel_power(self, n, channel):
        """Delivery ceiling from the links on this bus: (variable part, fixed part)."""
        links = channel["links"]
        if not links:
            return None, float("inf")  # storage is directly on the load bus
        marker = n.links.loc[links, "p_nom_extendable"].astype(bool)
        fixed = marker.index[~marker].astype(str).tolist()
        extendable = marker.index[marker].astype(str).tolist()
        constant = float(pd.to_numeric(n.links.loc[fixed, "p_nom"], errors="coerce").sum())
        expression = None
        if extendable:
            variable = model_variable(n, "Link-p_nom")
            selected = variable.sel({component_dim(variable): extendable})
            expression = selected.sum()
        return expression, constant

    # ------------------------------------------------------------------ audit

    def audit(self, n: pypsa.Network, snapshots: pd.Index):
        sns = validate_snapshots(snapshots)
        required, peak, top = self._peak(n, sns)
        names, credit = self._generator_credit(n, sns, top)
        capacity = self._solved(n.generators, names, "p_nom")
        available = float((capacity * credit).sum())

        storage_mw = 0.0
        for channel in self._storage_channels(n):
            energy = 0.0
            for component, members, attribute in (
                ("StorageUnit", channel["units"], "p_nom"),
                ("Store", channel["stores"], "e_nom"),
            ):
                if not members:
                    continue
                table = n.storage_units if component == "StorageUnit" else n.stores
                solved = self._solved(table, [name for name, _ in members], attribute)
                energy += float((solved * pd.Series(dict(members), dtype=float)).sum())
            power = (
                float(self._solved(n.links, channel["links"], "p_nom").sum())
                if channel["links"] else float("inf")
            )
            storage_mw += min(energy, power)
        available += storage_mw

        basis = (
            f"coincident_peak={peak}; peak_hours={int(self.peak_hours)}; "
            f"storage_credited_mw={storage_mw:.3f}"
            + ("" if self.storage_peak_duration_h is not None
               else "; storage NOT credited (storage_peak_duration_h unset)")
            + "; no transmission credit — run system-wide to avoid double counting"
        )
        return [
            ConstraintAudit(
                self.name, self.kind, self.scope.label, available, ">=", required, "MW",
                audit_passes(available, ">=", required), basis,
            )
        ]

    @staticmethod
    def _solved(table: pd.DataFrame, names: list[str], attribute: str) -> pd.Series:
        """Use input capacity for fixed rows and solved capacity for extendable rows.

        PyPSA may initialize fixed assets' optimal-capacity fields to zero before
        a solve. Their input capacities remain the applicable values.
        """
        if not names:
            return pd.Series(dtype=float)
        values = pd.to_numeric(table.loc[names, attribute], errors="coerce")
        optimised, flag = f"{attribute}_opt", f"{attribute}_extendable"
        if optimised in table and flag in table:
            solved = pd.to_numeric(table.loc[names, optimised], errors="coerce")
            extendable = table.loc[names, flag].astype(bool)
            values = values.where(~extendable, solved.where(solved.notna(), values))
        if values.isna().any() or not np.isfinite(values).all():
            raise ConstraintValidationError(
                f"Solved {attribute} is missing or non-finite for the reserve margin."
            )
        return values


__all__ = ["ReserveMargin"]
