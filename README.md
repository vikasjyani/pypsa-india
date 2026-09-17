# PyPSA-India

**New to the project? Start with the [PyPSA-India user guide](docs/guide.html#tutorial).**
The guide works offline and walks through setup, a first model, Excel inputs,
scenario YAML, technologies, policies, execution, results and troubleshooting.
It includes topic search, copyable examples and a scenario command helper.
The guide also links to a [downloadable beginner demo](docs/guide.html#downloads).

PyPSA-India is a Snakemake workflow that builds and solves a PyPSA power
network from Excel workbooks. Six scenarios ship with the repository, each a
workbook plus a `scenario_config.yaml` in its own folder under `data/`:

| scenario | boundary | buses |
|---|---|---|
| `all_india` | country | five regional-grid buses (NR WR SR ER NER) |
| `nr` `wr` `sr` `er` `ner` | one regional grid each | one bus per state |

`all_india` is the default in `base_config.yaml`. Build your own with
`snakemake generate_template` — see
[docs/getting-started/new-model.md](docs/getting-started/new-model.md).

## Setup

Use Python 3.13. The project dependencies are pinned in
[`requirements.txt`](requirements.txt), including PyPSA, Snakemake, HiGHS,
Linopy, pandas, NumPy, openpyxl, PyYAML, matplotlib and netCDF4.

From this directory:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The workflow reads `base_config.yaml` and
`data/<scenario>/scenario_config.yaml`, then discovers the single `.xlsx`
beside the scenario config (or uses its explicit `paths.workbook`).

## Generate an input template

From the **project root** (`pypsa-india/`, this README's folder), edit
[`generate_template.yaml`](generate_template.yaml), then run:

```powershell
snakemake generate_template
```

The required settings are active, and the file includes commented examples for
optional settings, sheet selections and policy classes. Review the active
component choices before generating; no existing scenario or filled workbook
is needed, and this command does not run the build/solve workflow.

The default outputs are
`data/<scenario.name>/<scenario.name>_template.xlsx` and
`data/<scenario.name>/scenario_config.yaml`.
It includes a README, units rows, input hints, and one demand sheet per load.
Set `template.output` to choose another path, relative to the project root.
The workflow profile supplies one core so no `-c1` argument is needed
(ordinary solve commands can still override this with `-c4`, for example).

Existing workbooks are preserved: a repeated command stops with an explanation.
Use a new name/path, or enable **one** of `template.merge` (retain existing
rows in retained columns and add new columns/sheets) and `template.force`
(replace after saving an `.xlsx.bak` backup).

For a new runnable scenario, follow this sequence from `pypsa-india/`:

1. Edit the root [`generate_template.yaml`](generate_template.yaml).
2. Run `snakemake generate_template`.
3. Fill the generated Excel workbook and its companion
   `data/<name>/scenario_config.yaml`.
4. Set the generated name under `run.scenario` in `base_config.yaml`:

   ```yaml
   run:
     scenario: my_model
   ```

5. Run `snakemake run`.

For a one-off run, leave `base_config.yaml` unchanged and use the optional CLI
override `snakemake run --config scenario=<name>`. Solver settings come
from `base_config.yaml` unless overridden in the scenario config. The existing
`make_template` rule remains available for regenerating a workbook for an
existing scenario.

## Run the scenarios

Run from `pypsa-india/`:

```powershell
snakemake run --config scenario=er
snakemake run --config scenario=all_india
```

`er` is the smallest shipped scenario and the quickest way to see the whole
workflow end to end. Results land under `results/er/`: per-year NetCDF files,
CSV tables, combined tables, the validation report, per-rule logs, and the
plotting manifest described below.

Coarser sampling — `snapshots.resolution` above 1, or `select: peak_weeks` —
gives representative estimates and does not replace a full hourly run.

For `snapshots.select: peak_weeks`, the builder groups each calendar month by
ISO year and week, chooses the week with the largest sum of coincident demand
across all active loads, keeps the whole chosen week clipped to the financial
year, and then subsamples it by `snapshots.resolution`. This is not a search for
the week containing the maximum hourly peak; three-hour or six-hour thinning
also does not guarantee that the annual maximum is selected.

For the all-India workbook, WR FY2040 has a known 8,783-value ragged tail. The
current convention retains the first 8,760 values, treats the financial year as
non-leap, copies February 28 for February 29 when needed, and drops the final
23 values with a warning. Inspect the source input before publishing policy
results if the tail may represent a missing day rather than padding.

Set `mode: perfect_foresight` in a scenario config to solve every horizon
jointly instead of one year at a time. That builds a single multi-period
network at `results/<scenario>/prenetwork/network_all.nc` and solves it to
`results/<scenario>/postnetwork/network_all.nc`.

Perfect foresight also needs an explicit investment-period block:

```yaml
investment_periods:
  years:
    2025: 5
    2030: 5
    2035: 5
  discount_rate: 0.0
```

`investment_periods.years` gives each period's duration in whole years. If a
period duration is omitted, it defaults to the gap to the next model year;
the final period defaults to **1 year**.
`investment_periods.discount_rate` is the non-negative annual social discount
rate used to form each period's objective weight. The scenario's current
value is `0.0`; changing it changes the objective weighting.

## Dispatch and expansion

The top-level `family` setting selects the capacity behavior:

- `dispatch` solves a fixed-capacity fleet. Each component's nominal capacity is frozen at
  `max(p_nom, p_nom_min)` (or `max(e_nom, e_nom_min)` for Stores). Explicit `Links` rows support
  `p_nom`, `p_nom_min`, and `p_nom_max` in MW; provide positive fixed link
  capacities where they are needed to keep the topology connected.
- `expansion` allows extendable new vintages. It is the default in the shipped
  scenarios.

Policy constraints are applied in either family, while the available capacity
and topology determine whether a particular policy is feasible.

`components.new_stores` and `components.storage_units` are aliases that both
select the single `New_Storage` sheet. They do not filter its rows. The
`New_Storage.Type` value selects the PyPSA component: `Store` uses energy
capacity in MWh, while `Storage` uses a `StorageUnit` power capacity in MW.

### Topology assumptions

Shared `Store`/`StorageUnit` role buses join the regional grid buses. They can
therefore pass flow through the shared bus and bypass a direct corridor policy.
For regional locality, use `spatial.role_buses: per_bus` and fill the regional
storage and link rows; changing that setting on an already-filled workbook
does not rewire its component rows.

For an explicit `Links` row with `p_min_pu < 0` and `efficiency < 1`, the model
uses `p1 = -efficiency * p0`; reverse flow consequently appears to gain energy.
Represent physical two-direction losses with two non-negative directional
Links, with the intended capacities and costs, or use `Transfer_Capacity` for
fixed corridors. Treat results under these topology assumptions as model
outputs, not physical policy certification.

`mode: myopic` solves one fiscal year at a time and carries solved brownfield
capacity forward. `mode: perfect_foresight` builds all configured periods into
one multi-period network and uses the `investment_periods` settings above.

Scenario files live in `data/<scenario>/scenario_config.yaml`. The base file
contains the default scenario, logging, and solver settings; the scenario file contains years,
spatial scope, components, constraints, and mode.

## Policy constraints

Eight typed classes, one per lever. A generated scenario config includes a
commented catalogue under `# ─── Available constraints ───`. You can regenerate
that catalogue with:

```powershell
python scripts/make_scenario_config.py --all
```

| Class | Caps |
|---|---|
| `RPOConstraint` | eligible generation as a share of served load |
| `CO2EmissionLimit` | tCO2 from generation |
| `FuelSupplyLimit` | a carrier's annual energy in MWh — coal, nuclear, gas, water |
| `MonthlyCufsProfile` | per-month utilisation ceiling/floor for one carrier |
| `TechnologyPhaseOut` | a carrier's total capacity, on a per-year trajectory |
| `ReserveMargin` | firm capacity against peak load |
| `ImportExportLimit` | net flow on a corridor |
| `StoreCyclingLimit` | discharge cycles per window, per Store — hourly snapshots only |

Policy fields that accept numeric values may use a year-keyed map
(`{2030: 0.43, 2035: 0.52}`) and are resolved per horizon. Every modelled year
must appear; values are not interpolated.

Per-*vintage* capacity bounds are not constraints here — the `Pipeline_p_min` and
`Pipeline_p_max` workbook sheets already write `Generator.p_nom_min` and
`p_nom_max`, which PyPSA enforces natively. `TechnologyPhaseOut` covers the one
case those sheets cannot: a ceiling on a carrier's whole fleet.

## Outputs and plotting manifests

Everything a run produces lives under one folder, so a scenario's results can
be copied, archived or deleted as a unit:

```text
results/<scenario>/
  logs/                 one log per rule, e.g. solve_network_2030.log
  validation_report.txt
  prenetwork/  brownfield/  postnetwork/     NetCDF networks
  csvs/<year>/ csvs/all_years/               summary tables
  figures/                                   PNG/SVG plus plots.json
```

The whole tree is git-ignored — it is reproducible from the workbook and the
scenario config.

The summary rules write CSVs under `results/<scenario>/csvs/<year>/` and
combine them under `results/<scenario>/csvs/all_years/`. Typical tables are
`statistics.csv`, `statistics_by_carrier.csv`, `capacity.csv`, `emissions.csv`,
`curtailment.csv`, `costs.csv`, and `constraint_audit.csv`.

`constraint_audit.csv` carries an `applicable` column. A policy whose scope
matches nothing in a given year — a carrier that has retired, a technology
absent from one region — is skipped and recorded with `applicable=False`
rather than aborting the run. A misspelled carrier or bus still fails loudly.

When plotting is enabled, the workflow declares a JSON plotting manifest at:

```text
results/<scenario>/figures/plots.json
```

When plotting is enabled, the manifest indexes generated PNG and SVG figures
and their source CSVs. The workflow can render capacity, generation, emissions
and curtailment figures. Storage energy capacity (MWh) is kept separate from
power capacity (MW).

The `capacity_energy` figure is titled **Installed Store energy capacity** and
does not infer energy capacity from `StorageUnit` power. The `capacity_links`
figure is titled **Installed Link and branch capacity** and includes storage
connector Links as well as regional corridors; it is not a corridor-only chart.

`constraint_audit.csv` preserves raw `lhs`, `rhs` and `margin` values and adds
the row-native numerical tolerance
`1e-6 + 1e-12 * max(abs(lhs), abs(rhs))`. This scale-aware check absorbs solver
roundoff at national magnitudes without changing policy equations. The
configured project default is HiGHS.

Generator energy weights are annualized to 8760 hours so annual energy,
emissions and policy accounting share one basis. Myopic objective and storage
weights retain sampled elapsed hours while capital costs are scaled to the
sample; perfect-foresight objective weights are annualized per operating period
and then include investment-period duration and discounting. The multi-period
objective is reported once as a whole-horizon value, not as a cost for every
year.

