# PyPSA-India documentation

PyPSA-India is a Python 3.13, Snakemake and PyPSA workflow that turns a
configuration-shaped Excel workbook into networks, solves them, and writes
auditable tables and figures. The shipped Kerala workbook is the reference
input; the workflow also supports generated templates and national or regional
boundaries.

## Start here

- [Overview](getting-started/overview.md) — workflow stages, model modes and important model assumptions.
- [Installation](getting-started/installation.md) — Python environment and pinned dependencies.
- [New model](getting-started/new-model.md) — generate and fill a workbook, create a scenario, and run it.
- [Configuration editing](getting-started/input-data/configuration.md) — YAML sections, temporal settings, solvers and policies.
- [PyPSA-India user guide](guide.html#tutorial) — the beginner tutorial plus detailed Excel/YAML instructions, policy examples, command helper, results interpretation and troubleshooting. It works offline after download.
- [Beginner demo download](guide.html#downloads) — a starter workbook and scenario YAML for following the tutorial.
- [Hosting the user guide](HOSTING.md) — build and publish the static documentation site.

## Workflow at a glance

The workflow validates inputs, builds a network for each configured year (or a
single multi-period network for perfect foresight), solves it, and writes
summaries and figures under `results/<scenario>/`.
