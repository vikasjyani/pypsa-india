"""PyPSA-India: generate inputs, or build and solve a filled workbook."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(workflow.basedir).resolve()
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
include: "rules/generate_template.smk"

# A new template needs neither a scenario config nor a filled workbook.  The
# DAG settings are populated only after this file is parsed, so they cannot be
# used here.  Snakemake retains requested targets in argv at parse time.
TEMPLATE_ONLY = "generate_template" in sys.argv
if not TEMPLATE_ONLY:
    from _helpers import load_config, workbook_path
    # `--config scenario=NAME` wins; otherwise base_config.yaml's run.scenario.
    import yaml as _yaml
    _BASE = _yaml.safe_load((Path(workflow.basedir) / "base_config.yaml").read_text(encoding="utf-8")) or {}
    SCENARIO = config.get("scenario") or (_BASE.get("run", {}) or {}).get("scenario")
    if not SCENARIO:
        raise ValueError(
            "No scenario selected. Set run.scenario in base_config.yaml, "
            "or pass --config scenario=NAME"
        )
    CONFIG = load_config(SCENARIO, root=Path(workflow.basedir))
    config.update(CONFIG)
    YEARS = [int(y) for y in CONFIG["scenario"]["years"]]
    MODE = CONFIG.get("mode", "myopic")
    if MODE not in ("myopic", "perfect_foresight"):
        raise ValueError("mode must be myopic or perfect_foresight")
    if not YEARS or YEARS != sorted(set(YEARS)):
        raise ValueError("scenario.years must be nonempty, unique and increasing")
    BASE_YEAR = YEARS[0]
    WORKBOOK = str(workbook_path(CONFIG, root=Path(workflow.basedir)))
    RESULTS = f"results/{SCENARIO}"
    CONFIG_FILES = ["base_config.yaml", f"data/{SCENARIO}/scenario_config.yaml"]
    SOURCES = [str(p) for p in (Path(workflow.basedir) / "scripts").rglob("*.py")
               if not p.name.startswith("test_") and p.name not in
               ("make_summary.py", "combine_summaries.py", "plot_results.py", "make_template.py")]
    wildcard_constraints:
        year=r"\d{4}",
    rule all:
        input:
            f"{RESULTS}/csvs/all_years/combined_summary.txt",
            f"{RESULTS}/figures/plots.json",
        default_target: True
    # A memorable alias for the default build/solve target.
    rule run:
        input:
            f"{RESULTS}/csvs/all_years/combined_summary.txt",
            f"{RESULTS}/figures/plots.json",
    # Outside the default DAG: the user fills this workbook by hand.
    rule make_template:
        params:
            merge=config.get("merge", False),
            force=config.get("force", False),
            settings=CONFIG,
        output:
            template=f"data/{SCENARIO}/{SCENARIO}_template.xlsx",
        log:
            f"{RESULTS}/logs/make_template.log",
        script:
            "scripts/make_template.py"
    rule validate_inputs:
        input:
            workbook=WORKBOOK,
            configuration=CONFIG_FILES,
            sources=SOURCES,
        output:
            report=f"{RESULTS}/validation_report.txt",
        params:
            settings=CONFIG,
        log:
            f"{RESULTS}/logs/validate_inputs.log",
        script:
            "scripts/validate_inputs.py"
    if MODE == "perfect_foresight":
        rule build_perfect_foresight:
            input:
                workbook=WORKBOOK,
                report=f"{RESULTS}/validation_report.txt",
            output:
                network=f"{RESULTS}/prenetwork/network_all.nc",
            log:
                f"{RESULTS}/logs/build_perfect_foresight.log",
            resources:
                mem_mb=16000,
            script:
                "scripts/build_perfect_foresight.py"
        rule solve_perfect_foresight:
            input:
                network=f"{RESULTS}/prenetwork/network_all.nc",
            output:
                network=f"{RESULTS}/postnetwork/network_all.nc",
            params:
                year="all",
            log:
                f"{RESULTS}/logs/solve_perfect_foresight.log",
            threads: workflow.cores
            resources:
                mem_mb=24000,
            script:
                "scripts/solve_network.py"
    else:
        rule build_network:
            input:
                workbook=WORKBOOK,
                report=f"{RESULTS}/validation_report.txt",
            output:
                network=f"{RESULTS}/prenetwork/network_{{year}}.nc",
            log:
                f"{RESULTS}/logs/build_network_{{year}}.log",
            resources:
                mem_mb=8000,
            script:
                "scripts/build_network.py"
        def previous_year(wildcards):
            index = YEARS.index(int(wildcards.year))
            return f"{RESULTS}/postnetwork/network_{YEARS[index - 1]}.nc" if index else []
        rule add_brownfield:
            input:
                network=f"{RESULTS}/prenetwork/network_{{year}}.nc",
                previous=previous_year,
            output:
                network=f"{RESULTS}/brownfield/network_{{year}}.nc",
            log:
                f"{RESULTS}/logs/add_brownfield_{{year}}.log",
            resources:
                mem_mb=8000,
            script:
                "scripts/add_brownfield.py"
        rule solve_network:
            input:
                network=lambda w: f"{RESULTS}/{'prenetwork' if int(w.year) == BASE_YEAR else 'brownfield'}/network_{w.year}.nc",
            output:
                network=f"{RESULTS}/postnetwork/network_{{year}}.nc",
            log:
                f"{RESULTS}/logs/solve_network_{{year}}.log",
            threads: workflow.cores
            resources:
                mem_mb=16000,
            script:
                "scripts/solve_network.py"
    rule make_summary:
        input:
            network=lambda w: f"{RESULTS}/postnetwork/network_{'all' if MODE == 'perfect_foresight' else w.year}.nc",
        output:
            summary=f"{RESULTS}/csvs/{{year}}/summary.txt",
        log:
            f"{RESULTS}/logs/make_summary_{{year}}.log",
        script:
            "scripts/make_summary.py"
    rule combine_summaries:
        input:
            summaries=expand(f"{RESULTS}/csvs/{{year}}/summary.txt", year=YEARS),
        output:
            combined=f"{RESULTS}/csvs/all_years/combined_summary.txt",
        log:
            f"{RESULTS}/logs/combine_summaries.log",
        script:
            "scripts/combine_summaries.py"
    rule plot_results:
        input:
            combined=f"{RESULTS}/csvs/all_years/combined_summary.txt",
        output:
            manifest=f"{RESULTS}/figures/plots.json",
        log:
            f"{RESULTS}/logs/plot_results.log",
        script:
            "scripts/plot_results.py"
