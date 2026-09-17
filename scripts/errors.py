"""Exception types for pypsa-india.

Split by *when* the failure happens, so a traceback says which stage broke:

- validation — before the Linopy model exists (bad config, bad input data, an
  unsupported combination). Recoverable by editing the workbook or the YAML.
- build      — while translating a spec into Linopy variables/constraints.
  Indicates a modelling or wiring bug, not user input.
- audit      — after the solve, while re-deriving a constraint from results.
"""

from __future__ import annotations


class PyPSAIndiaError(Exception):
    """Base class for every pypsa-india error."""


class ConstraintValidationError(PyPSAIndiaError):
    """Input data, configuration, or snapshots cannot support the constraint."""


class ConstraintInapplicable(ConstraintValidationError):
    """The constraint names real things, but none of them exist this horizon.

    A carrier that has retired, a technology absent from one region, a corridor
    not yet built: the policy is well formed and simply has nothing to act on,
    which is a modelling outcome rather than an error. Distinguished from a typo
    by checking the carrier and scope against the network's own vocabulary
    first, so `carrier: Sollar` still fails loudly instead of silently doing
    nothing. Subclasses ConstraintValidationError so existing handlers keep
    working; `ConstraintSet` catches it first and records a skipped audit row.
    """


class ConstraintBuildError(PyPSAIndiaError):
    """The Linopy model could not be extended with the constraint."""


class ConstraintAuditError(PyPSAIndiaError):
    """A solved network could not be audited against the constraint."""


class InputValidationError(PyPSAIndiaError):
    """The input workbook does not match the registry."""


__all__ = [
    "ConstraintAuditError",
    "ConstraintBuildError",
    "ConstraintInapplicable",
    "ConstraintValidationError",
    "InputValidationError",
    "PyPSAIndiaError",
]
