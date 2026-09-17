"""Typed India policy constraints and persisted-configuration loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from constraints.base import (
    ConstraintAudit,
    ConstraintSet,
    ConstraintSpec,
    compose,
)
from constraints.capacity import TechnologyPhaseOut
from constraints.emissions import CO2EmissionLimit, FuelSupplyLimit
from constraints.helpers import GeographicScope
from constraints.monthly_cufs_profile import MonthlyCufsProfile
from constraints.reserve_margin import ReserveMargin
from constraints.rpo import RPOConstraint
from constraints.storage import StoreCyclingLimit
from constraints.transfer import ImportExportLimit
from errors import ConstraintValidationError


_CONSTRAINT_CLASSES = {
    cls.__name__: cls
    for cls in (
        CO2EmissionLimit,
        RPOConstraint,
        ReserveMargin,
        FuelSupplyLimit,
        MonthlyCufsProfile,
        ImportExportLimit,
        TechnologyPhaseOut,
        StoreCyclingLimit,
    )
}

#: Removed persisted class names and actionable replacement guidance.
_REMOVED = {
    "PipelineCapacityBounds": (
        "the Pipeline_p_min / Pipeline_p_max workbook sheets already set "
        "Generator.p_nom_min / p_nom_max per vintage"
    ),
    "ResourcePotentialLimit": (
        "the Pipeline_p_max workbook sheet sets Generator.p_nom_max per vintage; "
        "use TechnologyPhaseOut for a whole-fleet ceiling"
    ),
    "HydroSeasonalEnergyLimit": (
        "use MonthlyCufsProfile for a seasonal utilisation ceiling (a max_cf "
        "entry per month of the season), or FuelSupplyLimit for an absolute MWh "
        "ceiling — noting that FuelSupplyLimit is annual, so a seasonal absolute "
        "MWh cap is no longer expressible"
    ),
    "PeakShavingLimit": (
        "it could never fail — nothing in the solve alters Load.p_set. Shape the "
        "demand profile in the workbook instead"
    ),
    "StorageCyclingLimit": (
        "use StoreCyclingLimit, which is per-store and per-window rather than "
        "pooled across the whole fleet"
    ),
    "MonthlyEnergyBounds": "renamed MonthlyCufsProfile; the `basis` field is gone",
}


def constraint_from_dict(config: Mapping[str, Any]) -> ConstraintSpec:
    """Recreate a typed constraint from its persisted metadata representation."""
    values = dict(config)
    class_name = str(values.pop("class", ""))
    values.pop("kind", None)
    if class_name in _REMOVED:
        raise ConstraintValidationError(
            f"{class_name} was removed: {_REMOVED[class_name]}."
        )
    try:
        cls = _CONSTRAINT_CLASSES[class_name]
    except KeyError as exc:
        raise ConstraintValidationError(
            f"Unknown persisted India constraint class {class_name!r}. "
            f"Available: {', '.join(sorted(_CONSTRAINT_CLASSES))}."
        ) from exc
    if class_name == "RPOConstraint":
        values.setdefault("scope", {})
    scope = values.get("scope")
    if isinstance(scope, Mapping):
        values["scope"] = GeographicScope(**scope)
    if class_name == "RPOConstraint" and "eligible_carriers" in values:
        values["eligible_carriers"] = tuple(values["eligible_carriers"])
    if class_name == "MonthlyCufsProfile":
        for key in ("max_cf_by_month", "min_cf_by_month"):
            if key in values:
                values[key] = {int(month): bound for month, bound in values[key].items()}
    try:
        return cls(**values)
    except TypeError as exc:
        raise ConstraintValidationError(
            f"Invalid persisted configuration for {class_name}: {exc}"
        ) from exc


__all__ = [
    "CO2EmissionLimit",
    "ConstraintAudit",
    "ConstraintSet",
    "ConstraintSpec",
    "FuelSupplyLimit",
    "GeographicScope",
    "ImportExportLimit",
    "MonthlyCufsProfile",
    "RPOConstraint",
    "ReserveMargin",
    "StoreCyclingLimit",
    "TechnologyPhaseOut",
    "compose",
    "constraint_from_dict",
]
