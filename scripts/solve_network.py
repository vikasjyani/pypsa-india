"""Solve a planning year or joint investment horizon with typed policies.

Configured constraints are added through ``ConstraintSet.extra_functionality``
and audited from the solved network in ``constraint_audit.csv``.
"""

# Snakemake inserts a preamble, so this script must not use a future import.
import sys
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import pypsa

sys.path.insert(0, str(Path(__file__).parent))

from _helpers import configure_logging, constraints_for_year
from constraints import ConstraintSet, constraint_from_dict
from errors import ConstraintValidationError

#: Constraint classes that are incompatible with expansion models.
REFUSE_EXTENDABLE = set()


def solver_settings(config: Mapping) -> tuple[str, dict]:
    """Return the configured solver name and options."""
    solver = config.get("solver", {}) or {}
    name = str(solver.get("name", "highs")).lower()
    options = dict(solver.get("options", {}) or {})
    options.update(dict(solver.get(name, {}) or {}))
    return name, options


def build_constraints(config: Mapping, year: int, logger) -> ConstraintSet:
    """Build the configured typed constraints for one year."""
    family = str(config.get("family", "expansion")).lower()
    entries = constraints_for_year(config, year)

    specs = []
    for entry in entries:
        class_name = str(entry.get("class", ""))
        if family == "expansion" and class_name in REFUSE_EXTENDABLE:
            raise ConstraintValidationError(
                f"{class_name} ({entry.get('name')!r}) raises on p_nom_extendable "
                f"but family is 'expansion'. Set family: dispatch, drop the "
                "constraint, or use a constraint class that supports expansion."
            )
        specs.append(constraint_from_dict(entry))

    if specs:
        logger.info(
            "constraints: %s", ", ".join(f"{s.name} ({s.kind})" for s in specs)
        )
    return ConstraintSet(specs)


def solve(n: pypsa.Network, config: Mapping, year: int, logger) -> ConstraintSet:
    name, options = solver_settings(config)
    multi_period = isinstance(n.snapshots, pd.MultiIndex)
    cset = (ConstraintSet(periods={int(period): build_constraints(config, int(period), logger)
                                 for period in n.investment_periods})
            if multi_period else build_constraints(config, int(year), logger))

    kwargs: dict[str, Any] = {"solver_name": name}
    if multi_period:
        kwargs['multi_investment_periods'] = True
    if options:
        kwargs["solver_options"] = options
    if len(cset):
        kwargs["extra_functionality"] = cset.extra_functionality

    logger.info("solving FY%s with %s (%d constraints)", year, name, len(cset))
    status, condition = n.optimize(**kwargs)

    if status != "ok" or condition != 'optimal':
        raise RuntimeError(
            f"FY{year} solve finished with status={status!r}, condition={condition!r}. "
            "An infeasible model usually means a binding constraint has no feasible "
            "capacity: check results/<scenario>/validation_report.txt for the "
            "peak-demand-vs-capacity warning."
        )
    logger.info("FY%s objective = %.6e", year, n.objective)
    return cset


def main(snakemake: Any) -> None:
    logger = configure_logging(snakemake)
    config = snakemake.config
    raw_year = getattr(snakemake.wildcards, 'year', getattr(snakemake.params, 'year', 'all'))
    year = 'all' if str(raw_year) == 'all' else int(raw_year)

    n = pypsa.Network(snakemake.input.network)
    cset = solve(n, config, year, logger)

    out = Path(snakemake.output.network)
    out.parent.mkdir(parents=True, exist_ok=True)
    target = out.parents[1] / "csvs" / str(year)
    target.mkdir(parents=True, exist_ok=True)

    if len(cset):
        audit = cset.audit(n)
        audit.insert(0, "year", audit['period'] if 'period' in audit else year)
        audit.to_csv(target / "constraint_audit.csv", index=False)
        cset.config_table().to_csv(target / "constraint_config.csv", index=False)

        if audit.empty:
            # Zero rows is not a pass. `failed` would be empty and the log would
            # read "all 0 constraint audit rows passed", publishing a network
            # against which nothing was actually checked.
            raise RuntimeError(
                f"{len(cset)} constraint(s) were applied but produced no audit rows. "
                "An audit that cannot report is not evidence; check that each spec's "
                "scope still matches components and snapshots in this horizon."
            )

        failed = audit.loc[~audit["passed"].astype(bool)]
        if len(failed):
            # A violated constraint after a successful solve means the audit and
            # the model disagree — a real defect, not a modelling outcome.
            logger.error(
                "%d constraint(s) FAILED their post-solve audit:\n%s",
                len(failed), failed.to_string(index=False),
            )
            raise RuntimeError(
                f"{len(failed)} constraint(s) failed the post-solve audit; see "
                f"{target / 'constraint_audit.csv'}"
            )

        # A constraint whose scope matches nothing this year is skipped and
        # recorded rather than aborting the run — a carrier can retire mid-horizon.
        # Skipping is legitimate; skipping *everything* means the year solved with
        # no policy in force, which nobody should discover from a results table.
        applicable = audit["applicable"].astype(bool) if "applicable" in audit else None
        if applicable is not None and not applicable.all():
            skipped = audit.loc[~applicable, ["name", "detail"]]
            logger.warning(
                "%d constraint row(s) did not apply in FY%s:\n%s",
                len(skipped), year, skipped.to_string(index=False),
            )
        if applicable is not None and not applicable.any():
            logger.warning(
                "NO constraint applied in FY%s — this horizon solved with no policy "
                "in force. Check the scopes under constraints:.", year,
            )
        logger.info("all %d constraint audit rows passed", len(audit))
    else:
        # Header-only tables clear prior policy rows and remain valid inputs to
        # pandas.read_csv and concatenation.
        period_columns = ['period'] if isinstance(n.snapshots, pd.MultiIndex) else []
        pd.DataFrame(columns=['year', *period_columns, 'name', 'kind', 'scope',
                              'lhs', 'sense', 'rhs', 'unit', 'passed', 'detail',
                              'margin', 'tolerance']).to_csv(
            target / 'constraint_audit.csv', index=False)
        pd.DataFrame(columns=[*period_columns, 'name', 'class', 'kind', 'scope']).to_csv(
            target / 'constraint_config.csv', index=False)
        logger.info('wrote empty policy audit/config tables for unconstrained solve')

    n.export_to_netcdf(out)
    logger.info("wrote %s", out)


snakemake: Any = globals().get("snakemake")
if __name__ == "__main__":
    if snakemake is None:
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("solve_network", year=2030)
    main(snakemake)
