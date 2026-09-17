"""Constraint composition, serialization metadata, and independent audit records."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol
from types import SimpleNamespace
from copy import deepcopy
import logging
import math

import pandas as pd
import pypsa
from linopy import Variable

from errors import (
    ConstraintAuditError,
    ConstraintInapplicable,
    ConstraintValidationError,
)


AUDIT_ATOL = 1e-6
AUDIT_RTOL = 1e-12


def audit_tolerance(lhs: float, rhs: float) -> float:
    """Numerical feasibility tolerance in the row's native unit.

    Recomputing weighted national-scale totals after a solve introduces small
    floating-point roundoff that a fixed epsilon cannot distinguish from a
    material violation. Keep a micro-unit absolute floor near zero and allow
    one part per trillion of the row magnitude. Non-finite values receive no
    usable tolerance and always fail below.
    """
    left, right = float(lhs), float(rhs)
    if not math.isfinite(left) or not math.isfinite(right):
        return float("nan")
    return AUDIT_ATOL + AUDIT_RTOL * max(abs(left), abs(right))


def audit_passes(lhs: float, sense: str, rhs: float) -> bool:
    """Compare a solved audit row without hiding a material violation."""
    left, right = float(lhs), float(rhs)
    tolerance = audit_tolerance(left, right)
    if not math.isfinite(tolerance):
        return False
    if sense == "<=":
        return left <= right + tolerance
    if sense == ">=":
        return left + tolerance >= right
    if sense == "==":
        return abs(left - right) <= tolerance
    raise ValueError(f"Unknown audit sense {sense!r}.")


@dataclass(frozen=True)
class ConstraintAudit:
    name: str
    kind: str
    scope: str
    lhs: float
    sense: str
    rhs: float
    unit: str
    passed: bool
    detail: str = ""
    #: False when the policy was well formed but had nothing to act on this
    #: horizon. Such a row is not a pass and not a failure; it is the record
    #: that the constraint was configured and deliberately did nothing.
    applicable: bool = True

    @property
    def margin(self) -> float:
        if self.sense == "<=":
            return self.rhs - self.lhs
        if self.sense == ">=":
            return self.lhs - self.rhs
        return -abs(self.lhs - self.rhs)

    def as_record(self) -> dict[str, object]:
        record = asdict(self)
        record["margin"] = self.margin
        record["tolerance"] = audit_tolerance(self.lhs, self.rhs)
        return record


class ConstraintSpec(Protocol):
    name: str
    kind: str

    def validate(self, n: pypsa.Network, snapshots: pd.Index) -> None: ...

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None: ...

    def audit(self, n: pypsa.Network, snapshots: pd.Index) -> Sequence[ConstraintAudit]: ...

    def to_dict(self) -> dict[str, Any]: ...


def spec_to_dict(spec: ConstraintSpec) -> dict[str, Any]:
    if not is_dataclass(spec):
        raise TypeError(f"Constraint specs must be dataclasses; got {type(spec).__name__}")
    value = asdict(spec)
    value["kind"] = spec.kind
    value["class"] = type(spec).__name__
    return value


class ConstraintSet:
    """An ordered, uniquely named collection used as a PyPSA callback."""

    def __init__(self, constraints: Iterable[ConstraintSpec] = (), *, periods=None) -> None:
        self.constraints = tuple(constraints)
        self.periods = periods
        names = [constraint.name for constraint in self.constraints]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ConstraintValidationError(
                f"Constraint names must be unique; duplicates: {duplicates}"
            )
        if any(not str(name).strip() for name in names):
            raise ConstraintValidationError("Constraint names must be non-blank.")

    def validate(self, n: pypsa.Network, snapshots: pd.Index | None = None) -> None:
        for _, view, specs, sns in self._horizons(n, snapshots):
            for constraint in specs:
                try:
                    constraint.validate(view, sns)
                except ConstraintInapplicable:
                    continue  # nothing to act on; audit() records why

    def _horizons(self, n, snapshots=None):
        sns = n.snapshots if snapshots is None else snapshots
        if isinstance(n.snapshots, pd.MultiIndex):
            sns = pd.MultiIndex.from_tuples(list(sns), names=n.snapshots.names)
            for period in sns.get_level_values(0).unique():
                view = period_network(n, period, sns[sns.get_level_values(0) == period])
                specs = self.periods[int(period)].constraints if self.periods is not None else self.constraints
                yield int(period), view, specs, view.snapshots
        else:
            yield None, n, self.constraints, pd.Index(sns)

    def add_to_model(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        if getattr(n, "_model", None) is None:
            raise ConstraintValidationError(
                "ConstraintSet.add_to_model() requires n.optimize.create_model() or PyPSA's "
                "extra_functionality callback."
            )
        self.validate(n, snapshots)
        for _, view, specs, sns in self._horizons(n, snapshots):
            for constraint in specs:
                try:
                    constraint.add_to_model(view, sns)
                except ConstraintInapplicable as exc:
                    logging.getLogger(__name__).info(
                        "%s not applied: %s", constraint.name, exc
                    )

    def extra_functionality(self, n: pypsa.Network, snapshots: pd.Index) -> None:
        self.add_to_model(n, snapshots)

    def audit(self, n: pypsa.Network, snapshots: pd.Index | None = None) -> pd.DataFrame:
        records: list[dict[str, object]] = []
        for period, view, specs, sns in self._horizons(n, snapshots):
            for constraint in specs:
                try:
                    for item in constraint.audit(view, sns):
                        record = item.as_record()
                        if period is not None:
                            record['period'] = period
                        records.append(record)
                except ConstraintInapplicable as exc:
                    # Still emit a row. A constraint that vanishes from the audit
                    # is indistinguishable from one that was never configured.
                    record = ConstraintAudit(
                        constraint.name, constraint.kind,
                        getattr(getattr(constraint, "scope", None), "label", "system"),
                        0.0, "<=", 0.0, "n/a", True, f"not applicable: {exc}",
                        applicable=False,
                    ).as_record()
                    if period is not None:
                        record['period'] = period
                    records.append(record)
                except Exception as exc:
                    if isinstance(exc, ConstraintAuditError):
                        raise
                    raise ConstraintAuditError(
                        f"Audit failed for {constraint.name!r} in period {period}: {exc}"
                    ) from exc
        columns = [
            "name",
            "kind",
            "scope",
            "lhs",
            "sense",
            "rhs",
            "unit",
            "passed",
            "applicable",
            "detail",
            "margin",
            "tolerance",
        ]
        if isinstance(n.snapshots, pd.MultiIndex):
            columns.insert(0, 'period')
        return pd.DataFrame.from_records(records, columns=columns)

    def config_table(self) -> pd.DataFrame:
        if self.periods is not None:
            return pd.concat([value.config_table().assign(period=period) for period, value in self.periods.items()], ignore_index=True)
        return pd.DataFrame.from_records([constraint.to_dict() for constraint in self.constraints])

    def __len__(self) -> int:
        if self.periods is not None:
            return sum(len(value) for value in self.periods.values())
        return len(self.constraints)


class _Dynamic(dict):
    __getattr__ = dict.__getitem__


class _PeriodVariables:
    def __init__(self, model, snapshots, components):
        self.model, self.snapshots, self.components = model, snapshots, components

    def __getitem__(self, key):
        var = self.model.variables[key]
        data = var.data
        if 'snapshot' in data.dims:
            data = data.sel(snapshot=self.snapshots).reset_index('snapshot', drop=True)
            data = data.assign_coords(snapshot=pd.DatetimeIndex(self.snapshots.get_level_values(1), name='snapshot'))
        component = key.split('-')[0]
        if component in self.components and 'name' in data.dims:
            names = data.indexes['name'].intersection(self.components[component].static.index)
            data = data.sel(name=names)
        return Variable(data, self.model, var.name)


class _PeriodModel:
    def __init__(self, model, period, snapshots, components):
        self._model, self._period = model, period
        self.variables = _PeriodVariables(model, snapshots, components)

    def add_constraints(self, *args, **kwargs):
        kwargs['name'] = f"{kwargs.get('name', 'india')}_period_{self._period}"
        return self._model.add_constraints(*args, **kwargs)

    def add_variables(self, *args, **kwargs):
        kwargs['name'] = f"{kwargs.get('name', 'india')}_period_{self._period}"
        return self._model.add_variables(*args, **kwargs)


def period_network(n, period, snapshots=None):
    """Read-only single-period view; LP labels still point at the shared model.

    Inactive vintages are excluded from both policy capacity denominators and
    dispatch accounting. Datetime coordinates preserve existing seasonal policy
    semantics while investment variables retain their global Linopy labels.
    """
    sns = n.snapshots[n.snapshots.get_level_values(0) == period] if snapshots is None else snapshots
    dates = pd.DatetimeIndex(sns.get_level_values(1), name='snapshot')
    components = {}
    view = SimpleNamespace(snapshots=dates, c=components, meta=deepcopy(n.meta))
    view.meta.setdefault('pypsa_india', {})['model_year'] = f'FY{int(period)}'
    view.snapshot_weightings = n.snapshot_weightings.loc[sns].set_axis(dates)
    for source in n.c.values():
        component = source.name
        static = source.static
        if 'build_year' in static and 'lifetime' in static:
            active = (static.build_year <= int(period)) & ((static.build_year + static.lifetime > int(period)) | static.build_year.eq(0))
            static = static.loc[active]
        dynamic = _Dynamic()
        for attr, frame in source.dynamic.items():
            dynamic[attr] = frame.reindex(sns).set_axis(dates).loc[:, frame.columns.intersection(static.index)]
        components[component] = SimpleNamespace(static=static, dynamic=dynamic, defaults=source.defaults)
        setattr(view, source.list_name, static)
        setattr(view, source.list_name + '_t', dynamic)
    model = getattr(n, '_model', None)
    view._model = model
    if model is not None:
        view.model = _PeriodModel(model, period, sns, components)
    return view


def compose(
    *callbacks: Callable[[pypsa.Network, pd.Index], None] | None,
) -> Callable[[pypsa.Network, pd.Index], None]:
    """Compose India and user callbacks without hiding execution order."""
    active = tuple(callback for callback in callbacks if callback is not None)

    def combined(n: pypsa.Network, snapshots: pd.Index) -> None:
        for callback in active:
            callback(n, snapshots)

    return combined


__all__ = [
    "ConstraintAudit",
    "ConstraintSet",
    "ConstraintSpec",
    "compose",
    "spec_to_dict",
    "audit_passes",
    "audit_tolerance",
]
