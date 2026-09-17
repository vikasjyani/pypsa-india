# Create and run a new model

This guide creates a scenario from the project template. The template command
does not require an existing scenario workbook.

## 1. Configure the template

Edit [`generate_template.yaml`](../../generate_template.yaml) in the
`pypsa-india/` project root. Keep these active:

```yaml
scenario:
  name: my_model
  years: [2030, 2035, 2040]
spatial:
  boundary: {level: country, id: IN}
carriers: [Coal, Hydro, Solar, Wind, Market]
```

The scenario name becomes the folder name. Country boundaries support `IN`;
regional identifiers are `NR`, `WR`, `SR`, `ER`, `NER`; state identifiers are
the two-letter codes in the seeded `Regions` sheet, such as `KL` or `MH`.

Optional `loads:` names create one `Demand_<name>` sheet per load. If omitted,
the generator creates one load per generated electricity bus, including `EXT`
when `external: aggregate` is selected. Load names must fit Excel's 31-character
sheet limit after the `Demand_` prefix.

`carriers` controls the carrier columns and choices emitted into the workbook;
it does not create technology assets by itself. Fill existing technologies in
`Generators` and candidate technologies in `New_Generators`, with their bus,
carrier, capacity and cost fields. The full scenario-layering and solver
configuration reference is [configuration.md](input-data/configuration.md).

## 2. Generate the workbook and scenario config

Run from `pypsa-india/`:

```powershell
snakemake generate_template
```

The workflow writes the workbook and its matching
`data/my_model/scenario_config.yaml`. The generated workbook has a README,
units rows, comments, dropdowns and highlighted `Please fill here` cells. Fill
every placeholder before running the model. Keep the units row directly under
each header.

If the output already exists, generation stops to protect filled data. Set one
of these in `generate_template.yaml` when deliberately regenerating:

```yaml
template:
  merge: true       # carry rows through columns/sheets that still exist
  # force: true     # replace and keep an .xlsx.bak backup
```

Use only one of `merge` and `force`. A custom `template.output` is relative to
the project root unless it is absolute.

## 3. Fill the workbook

The default sheets are `Regions`, `Buses`, `Loads`, demand sheets,
`Generators`, `P_max_pu`, `P_min_pu`, `Capital_cost`, `wacc`, `Lifetime`, `FOM`
and `CO2`. Selecting components adds:

| Selection | Sheets and meaning |
|---|---|
| `new_generators` | `New_Generators` plus pipeline bounds, fuel and startup costs |
| `new_stores` / `storage_units` | `New_Storage` plus `Pipeline_storage` |
| `links` | Explicit Link topology |
| `transfer` | Year-keyed transfer corridors that become directional Link pairs |
| `snapshots.select: custom_days` | `Custom days` representative dates |

For each active load, provide matching rows in `Demand_<name>` in the complete
chronological order of the modelled hourly fiscal or calendar year. In legacy
workbooks without `Loads`, the single `Demand` sheet remains supported. Demand
and availability profiles are positional; their row order must match the
generated fiscal-year or calendar-year index.

Existing `Generators` rows need a bus, carrier, nominal MW, marginal cost,
build year and lifetime. New generator rows are instantiated once per modelled
year. `Pipeline_p_min` and `Pipeline_p_max` are **incremental per-vintage**
bounds, not cumulative fleet totals. `Fuel_cost` supplies the variable cost of
new generators; existing generators use their own `Generators.marginal_cost`.
Use `inf` in a pipeline maximum when a bound is intentionally unbounded.

`Links` supports `p_nom`, `p_nom_min` and `p_nom_max` in MW. In dispatch mode,
positive fixed capacities are required wherever links keep the network
connected. `Transfer_Capacity` values are annual available corridor capacities;
they are converted to two directional links and do not accumulate as
brownfield investments.

For storage, `New_Storage.Type: Store` feeds `Store` and uses MWh in
`Pipeline_storage`; `Type: Storage` feeds `StorageUnit` and uses MW. The two
component flags are aliases and do not select rows by type.

Shared storage role buses join the regional grid and can pass flow through the
shared bus, bypassing a direct corridor policy. Use
`spatial.role_buses: per_bus` and fill regional storage and link rows when
locality matters; changing the setting alone on a filled workbook does not
rewire its rows. For explicit Links, a negative `p_min_pu` with efficiency
below one uses `p1 = -efficiency * p0`, so reverse flow has apparent energy
gain. Use two non-negative directional Links with chosen capacities/costs, or
`Transfer_Capacity`, for physical two-direction losses.

## 4. Edit the scenario YAML

The generated scenario config is where execution settings belong. Set `mode`,
`family`, `snapshots`, `components`, solver overrides, and any constraints.
`investment_periods.years` supplies whole-year period durations for perfect
foresight; omitted durations default to the gap to the next model year and the
final period defaults to one year. `investment_periods.discount_rate` is a
non-negative annual social discount rate.

The eight supported policy classes are:

- `CO2EmissionLimit` (`name`, `limit_tco2`, **required** `basis`);
- `FuelSupplyLimit` (`name`, `carrier`, `limit_mwh`, **required** `basis`);
- `RPOConstraint` (renewable generation share of served load);
- `MonthlyCufsProfile` (monthly capacity-factor floor/ceiling);
- `ImportExportLimit` (net corridor export/import limits);
- `TechnologyPhaseOut` (whole-fleet capacity trajectory);
- `ReserveMargin` (firm capacity at peak demand);
- `StoreCyclingLimit` (per-Store discharge cycles).

Policy fields that accept numeric values can use scalars or year-keyed maps. A
year map must cover every configured model year; values are not interpolated.
`scope` may be
omitted for the whole model or use `{bus: ...}`, `{state_id: ...}` or
`{region_id: ...}`. Ensure the scoped buses/components exist in the workbook.
`StoreCyclingLimit` requires near-hourly sampling by default; raise
`max_snapshot_hours` only when accepting an understated cycle count.

With `snapshots.select: peak_weeks`, each calendar month is grouped by ISO
year/week and the week with the largest sum of coincident demand across all
active loads is selected. The whole chosen week is retained (clipped to the
financial year) before `snapshots.resolution` subsampling. This does not mean
the week contains the maximum hourly peak, especially after three-hour or
six-hour thinning.

## 5. Validate and run

```powershell
snakemake run --config scenario=my_model
```

To make this the default run, set the generated scenario name in
`base_config.yaml`:

```yaml
run:
  scenario: my_model
```

Then the exact default command is:

```powershell
snakemake run
```

The alias `run` uses the scenario in `base_config.yaml` when no override is
given. The explicit form is useful for one-off runs:

```powershell
snakemake run --config scenario=er
snakemake run --config scenario=all_india
```

Inspect `results/my_model/validation_report.txt` before diagnosing a solve.
Then review each year's `constraint_audit.csv`, summary tables and
`figures/plots.json`. For sampled scenarios, interpret annual values as
representative estimates rather than full-hourly-year results.
