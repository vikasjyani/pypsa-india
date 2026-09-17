# PyPSA-India beginner demo

This is a synthetic FY2030 teaching model, not an estimate of Kerala demand,
generation costs, or emissions. It has one Kerala (`KL`) electricity bus, a
constant 10 MW load, and one fixed 20 MW `TutorialSupply` generator with a
marginal cost of 3,000 INR/MWh. There are no candidate assets, storage, Links,
transfers, or policy constraints.

## Run it without changing project settings

From the project root, check whether the target name is unused **before**
extracting:

```powershell
Test-Path .\data\beginner_demo
```

If it returns `False`, extract `beginner_demo.zip` into the project's `data`
directory. The archive contains `beginner_demo/`, producing:

```
data/beginner_demo/scenario_config.yaml
data/beginner_demo/beginner_demo.xlsx
```

Then run the downloaded study without editing `base_config.yaml`:

```powershell
snakemake validate_inputs --config scenario=beginner_demo
Get-Content .\results\beginner_demo\validation_report.txt
snakemake run --config scenario=beginner_demo
```

If it returns `True`, extract the archive into a temporary folder outside
`data`. Rename the extracted `beginner_demo` folder to an unused, folder-safe
name (letters/digits followed by letters, digits, `_`, or `-`). Edit
`scenario_config.yaml` so `scenario.name` matches that folder name. The
workbook may keep the name `beginner_demo.xlsx`; `paths.workbook` only needs to
match the workbook filename. Move the renamed folder into `data`, then run with
`--config scenario=<new-name>`.

## Expected result

HiGHS should report `optimal`. The model selects 365 daily snapshots, each
weighted by 24 hours. Dispatch is 10 MW at every snapshot; annual generation
and load are each 87,600 MWh; and the objective is 262,800,000 INR:

```
10 MW × 8,760 h × 3,000 INR/MWh
```

Validation has zero errors. It may show two generic warnings about missing
`Storage` and `Store` role buses. They are expected because storage and its
links are disabled in this one-bus study.

## Reproduce the blank template manually

The download is already filled, so template generation is optional. The command
reads the project's root `generate_template.yaml`. First save a backup of that
file. Then replace it with the settings below, choosing an unused name in both
`scenario.name` and `template.output`, and run `snakemake generate_template`.
Restore your saved root settings afterward. The command creates the workbook
and companion scenario YAML; fill the workbook using the values documented in
the hosted tutorial.

```yaml
scenario:
  name: beginner_demo
  currency: INR
  fiscal_year: true
  years: [2030]
mode: myopic
family: dispatch
spatial:
  boundary: {level: state, id: KL}
  resolution: state
  external: none
  role_buses: shared
snapshots: {select: all, resolution: 24}
components:
  new_generators: false
  new_stores: false
  storage_units: false
  links: false
  transfer: false
loads: [KL]
carriers: [TutorialSupply]
costs: {wacc_row: WACC %, wacc_row_storage: WACC %, include_fom: false}
retirement: {enable: true}
unit_commitment: {enable: false}
solver: {name: highs, options: {presolve: "on"}}
constraints: []
template:
  output: data/beginner_demo/beginner_demo.xlsx
```
