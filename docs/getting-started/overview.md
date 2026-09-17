# Overview

## What the workflow does

The project reads two YAML layers and one workbook:

```text
base_config.yaml
        + data/<scenario>/scenario_config.yaml
        + data/<scenario>/<workbook>.xlsx
        -> validate_inputs
        -> build_network (one fiscal year) or build_perfect_foresight
        -> solve
        -> summaries and plots
```

The scenario file describes years, geography, components, time sampling and
policies. The workbook contains the fleet, demand, cost tables, profiles and
capacity bounds. With one workbook in a scenario directory, the workflow finds
it automatically. If more than one exists, set `paths.workbook` in the
scenario file.

## Modes and capacity families

`mode: myopic` builds and solves one fiscal year at a time. Solved capacity is
carried into the next year as brownfield capacity. `mode: perfect_foresight`
builds one multi-period network and applies each investment period's policy
values to its period.

`family: expansion` allows extendable candidate vintages to build. Pipeline
values are incremental per vintage. `family: dispatch` freezes nominal
capacity at the greater of the workbook nominal and minimum values, so it
answers whether the supplied fleet can serve the demand. For fixed topology,
explicit `Links` rows need positive capacity values.

Storage has two PyPSA representations. `New_Storage.Type: Store` means energy
capacity in MWh; `Type: Storage` means a `StorageUnit` with power capacity in
MW. `components.new_stores` and `components.storage_units` are aliases that
select `New_Storage`; they do not filter its rows.

## Time and geography

Years are modelled as Indian financial years (April through March) by default;
set `scenario.fiscal_year: false` for calendar years. `snapshots.select` can be
`all`, `custom_days`, or `peak_weeks`. `snapshots.resolution` must be a positive
divisor of 24. Generator energy and emissions are annualized to 8,760 hours;
with `peak_weeks` they remain representative estimates, not full-hourly-year
results. A six-hour run is a sampled run and should be interpreted accordingly.

`peak_weeks` groups each calendar month by ISO year and week, selects the week
with the largest sum of coincident demand across all active loads, keeps that
whole week clipped to the financial year, and then takes every
`snapshots.resolution`-th hour. It is not the week containing the single
largest hourly peak, and thinning to three-hour or six-hour samples does not
guarantee that the annual maximum is included.

`spatial.boundary` selects a country, region or state. `spatial.resolution`
selects regional or state buses independently of the boundary. `external:
aggregate` adds an `EXT` bus; `none` makes a closed boundary. The builder stamps
state and regional identifiers on components so scoped policies remain
expressible after bus aggregation.

For a `peak_weeks` run, annual generator energy and emissions are normalized
with the annualized generator weights; they are representative estimates, not
equivalent full-hourly-year results. The shipped all-India workbook has a
known ragged tail in WR FY2040 (8,783 values). The current convention retains
the first 8,760 values, uses the non-leap financial-year interpretation,
copies February 28 for February 29 when needed, and drops the trailing 23
values with a warning. Inspect the original input before publishing policy
results if that tail may represent a missing day rather than padding.

## Outputs

Results are under `results/<scenario>/`:

- `validation_report.txt` records input errors and warnings;
- `prenetwork/` and `postnetwork/` contain NetCDF networks;
- `csvs/<year>/` contains summaries and policy audits;
- `csvs/all_years/combined_summary.txt` combines periods;
- When plotting is enabled, `figures/plots.json` indexes PNG/SVG figures and
  their source CSVs.

The workflow publishes a solved network only after configured policy audits
pass. Review `validation_report.txt` and the policy audit tables before using
results in downstream analysis.
