"""Aggregate directional import/export limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd
import pypsa

from constraints.base import ConstraintAudit, audit_passes, spec_to_dict
from constraints.helpers import (
    component_dim,
    model_variable,
    safe_name,
    validate_nonnegative_finite,
    validate_snapshots,
)
from errors import ConstraintInapplicable, ConstraintValidationError


@dataclass(frozen=True)
class ImportExportLimit:
    name: str
    from_bus: str
    to_bus: str
    max_export_mw: float | None = None
    max_import_mw: float | None = None

    kind: ClassVar[str] = "import_export_limit"

    def to_dict(self):
        return spec_to_dict(self)

    def _connections(self, n: pypsa.Network) -> tuple[list[str], list[str], list[str], list[str]]:
        forward_links = n.links.index[
            n.links.bus0.eq(self.from_bus) & n.links.bus1.eq(self.to_bus)
        ].astype(str).tolist()
        reverse_links = n.links.index[
            n.links.bus0.eq(self.to_bus) & n.links.bus1.eq(self.from_bus)
        ].astype(str).tolist()
        forward_lines = n.lines.index[
            n.lines.bus0.eq(self.from_bus) & n.lines.bus1.eq(self.to_bus)
        ].astype(str).tolist()
        reverse_lines = n.lines.index[
            n.lines.bus0.eq(self.to_bus) & n.lines.bus1.eq(self.from_bus)
        ].astype(str).tolist()
        return forward_links, reverse_links, forward_lines, reverse_lines

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        validate_snapshots(snapshots)
        if self.from_bus == self.to_bus:
            raise ConstraintValidationError("Import/export endpoints must differ.")
        if self.from_bus not in n.buses.index or self.to_bus not in n.buses.index:
            raise ConstraintValidationError("Import/export endpoints must be existing buses.")
        if self.max_export_mw is None and self.max_import_mw is None:
            raise ConstraintValidationError("At least one import/export limit is required.")
        if self.max_export_mw is not None:
            validate_nonnegative_finite(self.max_export_mw, "max_export_mw")
        if self.max_import_mw is not None:
            validate_nonnegative_finite(self.max_import_mw, "max_import_mw")
        if not any(self._connections(n)):
            raise ConstraintInapplicable(
                f"no Link or Line connects {self.from_bus!r} and {self.to_bus!r} "
                "this horizon"
            )

    def _net_expression(self, n: pypsa.Network, snapshots: pd.Index):
        forward_links, reverse_links, forward_lines, reverse_lines = self._connections(n)
        net = None
        if forward_links or reverse_links:
            link = model_variable(n, "Link-p")
            dim = component_dim(link)
            if forward_links:
                value = link.sel({dim: forward_links, "snapshot": list(snapshots)}).sum(dim)
                net = value if net is None else net + value
            if reverse_links:
                value = link.sel({dim: reverse_links, "snapshot": list(snapshots)}).sum(dim)
                net = -value if net is None else net - value
        if forward_lines or reverse_lines:
            line = model_variable(n, "Line-s")
            dim = component_dim(line)
            if forward_lines:
                value = line.sel({dim: forward_lines, "snapshot": list(snapshots)}).sum(dim)
                net = value if net is None else net + value
            if reverse_lines:
                value = line.sel({dim: reverse_lines, "snapshot": list(snapshots)}).sum(dim)
                net = -value if net is None else net - value
        if net is None:  # validate() prevents this path
            raise ConstraintValidationError("No modeled connection is available.")
        return net

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        net = self._net_expression(n, snapshots)
        tag = safe_name(self.name)
        if self.max_export_mw is not None:
            n.model.add_constraints(
                net <= float(self.max_export_mw), name=f"india_transfer_export_{tag}"
            )
        if self.max_import_mw is not None:
            n.model.add_constraints(
                -net <= float(self.max_import_mw), name=f"india_transfer_import_{tag}"
            )

    def _net_result(self, n: pypsa.Network, snapshots: pd.Index) -> pd.Series:
        forward_links, reverse_links, forward_lines, reverse_lines = self._connections(n)
        sns = pd.Index(snapshots)
        net = pd.Series(0.0, index=sns)
        if forward_links:
            values = n.links_t.p0.loc[sns, forward_links]
            if not np.isfinite(values.to_numpy()).all():
                raise ConstraintValidationError("Solved Link.p0 contains non-finite values.")
            net += values.sum(axis=1)
        if reverse_links:
            values = n.links_t.p0.loc[sns, reverse_links]
            if not np.isfinite(values.to_numpy()).all():
                raise ConstraintValidationError("Solved Link.p0 contains non-finite values.")
            net -= values.sum(axis=1)
        if forward_lines:
            values = n.lines_t.p0.loc[sns, forward_lines]
            if not np.isfinite(values.to_numpy()).all():
                raise ConstraintValidationError("Solved Line.p0 contains non-finite values.")
            net += values.sum(axis=1)
        if reverse_lines:
            values = n.lines_t.p0.loc[sns, reverse_lines]
            if not np.isfinite(values.to_numpy()).all():
                raise ConstraintValidationError("Solved Line.p0 contains non-finite values.")
            net -= values.sum(axis=1)
        if not np.isfinite(net).all():
            raise ConstraintValidationError("Solved corridor flow contains non-finite values.")
        return net

    def audit(self, n: pypsa.Network, snapshots: pd.Index):
        if not any(self._connections(n)):
            raise ConstraintInapplicable(
                f"no Link or Line connects {self.from_bus!r} and {self.to_bus!r} "
                "this horizon"
            )
        net = self._net_result(n, snapshots)
        records: list[ConstraintAudit] = []
        if self.max_export_mw is not None:
            actual = float(net.max())
            limit = float(self.max_export_mw)
            records.append(
                ConstraintAudit(
                    f"{self.name}:export", self.kind, f"{self.from_bus}->{self.to_bus}",
                    actual, "<=", limit, "MW_p0", audit_passes(actual, "<=", limit),
                    "net sending-end flow across all matching Links and Lines",
                )
            )
        if self.max_import_mw is not None:
            actual = float((-net).max())
            limit = float(self.max_import_mw)
            records.append(
                ConstraintAudit(
                    f"{self.name}:import", self.kind, f"{self.to_bus}->{self.from_bus}",
                    actual, "<=", limit, "MW_p0", audit_passes(actual, "<=", limit),
                    "net sending-end flow across all matching Links and Lines",
                )
            )
        return records


__all__ = ["ImportExportLimit"]
