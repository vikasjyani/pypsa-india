# Installation

## Requirements

Use Python **3.13**. Install the pinned stack from
[`requirements.txt`](../../requirements.txt): PyPSA 1.2.4, Snakemake 9.23.1,
HiGHS 1.14.0, Linopy 0.8.0, pandas 2.2.3, NumPy 2.2.1, openpyxl 3.1.5,
PyYAML 6.0.3, matplotlib 3.10.0 and netCDF4 1.7.2.

From the `pypsa-india/` directory in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, run the commands from an activated terminal or
invoke `.venv\Scripts\python.exe` directly. The workflow's default solver is
HiGHS. Gurobi, CPLEX or GLPK can be selected only when separately installed
and licensed on the machine.

## Check the workflow setup

```powershell
snakemake -n --config scenario=er
```

The dry run should show validation, annual builds/solves, brownfield
carry-forward, summaries, combination and plotting without executing the
solver.
