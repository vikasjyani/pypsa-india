# Editing scenario configuration

`base_config.yaml` holds project-wide logging and solver defaults plus
`run.scenario`. `data/<scenario>/scenario_config.yaml` holds the settings that
define one model. Scenario values are merged over the base values.

## Main sections

```yaml
scenario:
  years: [2030, 2035, 2040]
  fiscal_year: true
spatial:
  boundary: {level: country, id: IN}
  resolution: region
snapshots:
  select: all
  resolution: 3
components:
  new_generators: true
  new_stores: true
  links: true
mode: myopic
family: expansion
```

Use `paths.workbook` only when a scenario directory contains more than one
`.xlsx`; otherwise the single workbook is discovered automatically. `costs`
selects named `wacc` rows and controls FOM inclusion. `storage.max_hours` and
`storage.standing_loss` set storage defaults. `retirement.enable` controls
whether expired assets are removed between myopic horizons.

Shared storage role buses join regional grid buses and can pass flow through
the shared bus, bypassing a direct corridor policy. Set
`spatial.role_buses: per_bus` and fill regional storage/Link rows when local
storage locality is required; changing the setting alone does not rewire an
already-filled workbook. An explicit Link with negative `p_min_pu` and
efficiency below one uses `p1 = -efficiency * p0`, which makes reverse flow an
apparent gain. Use two non-negative directional Links or `Transfer_Capacity`
for physical two-direction losses.

The `solver` block passes `options` to PyPSA's optimizer. A matching nested
solver block, such as `solver.highs`, overrides those options. `unit_commitment`
is not implemented as a scenario-level switch; leave it disabled. Generator
and Link row fields still have schema defaults, but the builder does not turn
committable operation on.

## Perfect foresight

```yaml
mode: perfect_foresight
investment_periods:
  years: {2030: 5, 2035: 5, 2040: 1}
  discount_rate: 0.02
```

Supplied duration keys must be model years. Missing keys receive the gap to the
next year, and the last period receives one year. Policy maps, by contrast,
must explicitly cover every model year.

## Constraint examples

```yaml
constraints:
  - class: CO2EmissionLimit
    name: co2_cap
    limit_tco2: {2030: 12000000, 2035: 10000000, 2040: 8000000}
    basis: electrical_output
  - class: RPOConstraint
    name: renewable_share
    target_share: 0.43
    eligible_carriers: [Solar, Wind, Hydro]
```

`basis` is required for both emissions and fuel limits. Use the policy class
names and fields shown in the generated scenario configuration as the source
of truth for a new scenario.
