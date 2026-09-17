"""Shared configuration, logging, paths and annualized cost calculations.

``mock_snakemake`` supplies rule inputs, outputs and settings for standalone
script execution using the same scenario and path conventions as the workflow.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]

#: Project-wide settings, at the project root.
BASE_CONFIG_NAME = "base_config.yaml"
#: Per-scenario settings, inside that scenario's folder under data/.
SCENARIO_CONFIG_NAME = "scenario_config.yaml"

sys.path.insert(0, str(Path(__file__).parent))

from errors import InputValidationError  # noqa: E402
from registry import is_year_map  # noqa: E402


# ------------------------------------------------------------------ logging


def configure_logging(snakemake: Any = None, level: str | None = None) -> logging.Logger:
    """Configure console logging and a file handler when a rule log is supplied."""
    config = getattr(snakemake, "config", {}) or {}
    logging_config = config.get("logging", {})
    resolved = level or logging_config.get("level", "INFO")
    fmt = logging_config.get("format", "%(levelname)s:%(name)s:%(message)s")

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    logfiles = getattr(snakemake, "log", None) or []
    if logfiles:
        path = Path(logfiles[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, mode="w", encoding="utf-8"))

    logging.basicConfig(
        level=resolved, format=fmt, handlers=handlers, force=True
    )
    return logging.getLogger(getattr(snakemake, "rule", "pypsa-india"))


# ------------------------------------------------------------------- config


def _deep_merge(base: Mapping, override: Mapping) -> dict:
    """Recursive dict merge; `override` wins on scalars and lists."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def scenario_dir(scenario: str, root: Path | None = None) -> Path:
    """The folder holding one scenario's workbook and its config."""
    return (root or ROOT) / "data" / scenario


def available_scenarios(root: Path | None = None) -> list[str]:
    data = (root or ROOT) / "data"
    if not data.is_dir():
        return []
    return sorted(
        p.name for p in data.iterdir()
        if p.is_dir() and (p / SCENARIO_CONFIG_NAME).exists()
    )


def load_config(scenario: str, root: Path | None = None) -> dict:
    """Merge the selected scenario YAML over the project's base configuration.

    Nested mappings merge recursively; scenario scalars and lists replace base
    values. The selected folder sets ``scenario.name`` and ``paths.scenario_dir``.
    """
    root = root or ROOT
    base_path = root / BASE_CONFIG_NAME
    folder = scenario_dir(scenario, root)
    scenario_path = folder / SCENARIO_CONFIG_NAME

    if not base_path.exists():
        raise FileNotFoundError(f"Missing base config: {base_path}")
    if not scenario_path.exists():
        found = available_scenarios(root)
        raise FileNotFoundError(
            f"Missing {scenario_path}. "
            f"A scenario is a folder under data/ holding {SCENARIO_CONFIG_NAME} "
            f"and its workbook. "
            f"Available: {', '.join(found) or '(none)'}"
        )

    base = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    scenario_config = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(base, scenario_config)
    merged.setdefault("scenario", {})["name"] = scenario
    merged.setdefault("paths", {})["scenario_dir"] = str(folder)
    return merged


def config_years(config: Mapping) -> list[int]:
    return [int(year) for year in config["scenario"]["years"]]


def config_loads(config: Mapping) -> list[str]:
    """Load names declared in config, if any.

    Empty means "derive them" — the template generator makes one load per
    electricity bus, and the validator/builder read whatever the workbook's
    Loads sheet actually contains.
    """
    return [str(name).strip() for name in config.get("loads", []) or []]


def config_carriers(config: Mapping) -> list[str]:
    return [str(carrier) for carrier in config.get("carriers", [])]


# ------------------------------------------------- per-year value resolution


def resolve_year_values(value: Any, year: int, years: Sequence[int]) -> Any:
    """Collapse year-keyed maps to the scalar for `year`, recursively.

    The disambiguation rule (see `registry.is_year_map`): a non-empty mapping
    whose keys are all plausible calendar years (1900 through 2200) is a
    year-map. `max_cf_by_month` (keyed 1-12) and
    `capacity_credit_by_carrier` (keyed by carrier) therefore pass through
    untouched, and a bare scalar means "constant across all years".

    Every year-map must cover all configured years. Values are not interpolated.
    """
    if is_year_map(value, years):
        keys = {int(key): key for key in value}
        missing = sorted(set(int(y) for y in years) - set(keys))
        if missing:
            raise InputValidationError(
                f"Year-keyed value covers {sorted(keys)} but the scenario models "
                f"{sorted(int(y) for y in years)}; missing {missing}. "
                "Supply every modelled year explicitly."
            )
        return resolve_year_values(value[keys[int(year)]], year, years)
    if isinstance(value, Mapping):
        return {k: resolve_year_values(v, year, years) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_year_values(v, year, years) for v in value]
    return value


def constraints_for_year(config: Mapping, year: int) -> list[dict]:
    """The `constraints:` block with every year-map collapsed for `year`."""
    years = config_years(config)
    return [
        resolve_year_values(dict(entry), year, years)
        for entry in config.get("constraints", []) or []
    ]


# -------------------------------------------------------------------- costs


def crf(rate: float, lifetime: float) -> float:
    """Return the annual capital recovery factor for an upfront investment.

    ``rate`` is an annual fraction and ``lifetime`` is in years. At zero rate,
    the factor is ``1 / lifetime``.
    """
    rate = float(rate)
    lifetime = float(lifetime)
    if lifetime <= 0:
        raise ValueError(f"lifetime must be positive, got {lifetime}")
    if rate == 0:
        return 1.0 / lifetime
    factor = (1.0 + rate) ** lifetime
    return rate * factor / (factor - 1.0)


def annuitised_capital_cost(
    capex: float, rate: float, lifetime: float, fom: float = 0.0,
    capital_weighting: float = 1.0,
) -> float:
    """Return ``(capex * CRF + annual FOM) / capital_weighting``.

    Capex and FOM must use the same capacity unit. ``capital_weighting`` scales
    annual costs to the hours represented by the optimization objective.
    """
    return (float(capex) * crf(rate, lifetime) + float(fom)) / float(capital_weighting)


def snapshot_capital_weighting(n_snapshots: int, resolution: int = 1) -> float:
    """Return ``8760 / (n_snapshots * resolution)``; 1.0 for 8,760 hours."""
    represented = int(n_snapshots) * int(resolution)
    if represented <= 0:
        raise ValueError("snapshot count and resolution must be positive")
    return 8760.0 / represented


# -------------------------------------------------------------------- paths


@dataclass(frozen=True)
class Paths:
    """Scenario paths shared by the workflow and standalone rule execution."""

    scenario: str
    root: Path = ROOT

    @property
    def results(self) -> Path:
        return self.root / "results" / self.scenario

    def prenetwork(self, year: int | str) -> Path:
        return self.results / "prenetwork" / f"network_{year}.nc"

    def brownfield(self, year: int) -> Path:
        return self.results / "brownfield" / f"network_{year}.nc"

    def postnetwork(self, year: int | str) -> Path:
        return self.results / "postnetwork" / f"network_{year}.nc"

    def csvs(self, year: int | str) -> Path:
        return self.results / "csvs" / str(year)

    @property
    def validation_report(self) -> Path:
        return self.results / "validation_report.txt"

    @property
    def scenario_dir(self) -> Path:
        return self.root / "data" / self.scenario

    @property
    def template(self) -> Path:
        return self.scenario_dir / f"{self.scenario}_template.xlsx"

    def log(self, rule: str, **wildcards: Any) -> Path:
        suffix = "_".join(str(v) for v in wildcards.values())
        name = f"{rule}_{suffix}" if suffix else rule
        return self.results / "logs" / f"{name}.log"


def workbook_path(config: Mapping, root: Path | None = None) -> Path:
    """Resolve an explicit workbook path or discover one scenario-local XLSX.

    Relative explicit paths are checked against the scenario folder, project
    root and working directory, in that order. Discovery ignores Excel lock
    files and requires exactly one workbook.
    """
    root = root or ROOT
    folder = Path(config.get("paths", {}).get("scenario_dir")
                  or scenario_dir(config["scenario"]["name"], root))

    named = config.get("paths", {}).get("workbook")
    if named:
        candidate = Path(named)
        for base in (folder, root, Path.cwd()):
            resolved = candidate if candidate.is_absolute() else base / candidate
            if resolved.exists():
                return resolved
        raise FileNotFoundError(f"paths.workbook {named!r} not found (looked in {folder})")

    books = sorted(
        p for p in folder.glob("*.xlsx")
        if not p.name.startswith("~$") and not p.name.endswith(".bak")
    )
    if not books:
        raise FileNotFoundError(
            f"No .xlsx in {folder}. Put the filled workbook there, or run "
            f"`snakemake make_template` to generate one."
        )
    if len(books) > 1:
        raise InputValidationError(
            f"{folder} holds {len(books)} workbooks "
            f"({', '.join(b.name for b in books)}). Name the one to use with "
            f"paths.workbook in {SCENARIO_CONFIG_NAME}."
        )
    return books[0]


# ---------------------------------------------------------- mock_snakemake


def mock_snakemake(rule: str, scenario: str = "all_india", **wildcards: Any) -> SimpleNamespace:
    """Create the execution namespace for a supported rule and scenario.

    Loads the scenario configuration and workbook path, resolves year-dependent
    inputs and outputs, and creates output/log directories. The year defaults
    to the first configured year; ``year="all"`` selects a multi-period network.
    This prepares arguments only; it does not execute dependency jobs.
    """
    config = load_config(scenario)
    paths = Paths(scenario)
    raw_year = wildcards.get("year", config_years(config)[0])
    year = "all" if str(raw_year) == "all" else int(raw_year)
    years = config_years(config)
    multi = config.get("mode") == "perfect_foresight"
    network_year = "all" if multi else year
    previous = []
    if isinstance(year, int) and year in years and years.index(year):
        previous = str(paths.postnetwork(years[years.index(year) - 1]))
    workbook = str(workbook_path(config))

    table: Mapping[str, tuple[dict, dict]] = {
        "make_template": (
            {},
            {"template": str(paths.template)},
        ),
        "validate_inputs": (
            {"workbook": workbook},
            {"report": str(paths.validation_report)},
        ),
        "build_network": (
            {"workbook": workbook, "report": str(paths.validation_report)},
            {"network": str(paths.prenetwork(year))},
        ),
        "build_perfect_foresight": (
            {"workbook": workbook, "report": str(paths.validation_report)},
            {"network": str(paths.prenetwork("all"))},
        ),
        "add_brownfield": (
            {"network": str(paths.prenetwork(year)), "previous": previous},
            {"network": str(paths.brownfield(year))},
        ),
        "solve_network": (
            {"network": str(paths.brownfield(year) if previous and not multi else paths.prenetwork(network_year))},
            {"network": str(paths.postnetwork(network_year))},
        ),
        "make_summary": (
            {"network": str(paths.postnetwork(network_year))},
            {"summary": str(paths.csvs(year) / "summary.txt")},
        ),
        "combine_summaries": (
            {"summaries": [str(paths.csvs(y) / "summary.txt") for y in config_years(config)]},
            {"combined": str(paths.csvs("all_years") / "combined_summary.txt")},
        ),
        "plot_results": (
            {"combined": str(paths.csvs("all_years") / "combined_summary.txt")},
            {"manifest": str(paths.results / "figures" / "plots.json")},
        ),
    }
    if rule not in table:
        raise KeyError(f"Unknown rule {rule!r}. Known: {', '.join(sorted(table))}")

    inputs, outputs = table[rule]
    log = paths.log(rule, **wildcards)
    log.parent.mkdir(parents=True, exist_ok=True)
    for value in outputs.values():
        Path(value).parent.mkdir(parents=True, exist_ok=True)

    return SimpleNamespace(
        rule=rule,
        config=config,
        input=_Namespace(inputs),
        output=_Namespace(outputs),
        params=SimpleNamespace(scenario=scenario, year=year, paths=paths),
        wildcards=SimpleNamespace(**{"scenario": scenario, **wildcards, "year": year}),
        log=[str(log)],
        threads=1,
    )


class _Namespace(SimpleNamespace):
    """Attribute access plus `[0]` indexing, like Snakemake's IO objects."""

    def __init__(self, mapping: Mapping[str, Any]) -> None:
        super().__init__(**mapping)
        self._order = list(mapping)

    def __getitem__(self, index: int) -> Any:
        return getattr(self, self._order[index])

    def __iter__(self) -> Iterable[Any]:
        return iter(getattr(self, key) for key in self._order)

    def __fspath__(self) -> str:
        return str(self[0])


__all__ = [
    "BASE_CONFIG_NAME",
    "Paths",
    "ROOT",
    "SCENARIO_CONFIG_NAME",
    "available_scenarios",
    "scenario_dir",
    "annuitised_capital_cost",
    "config_carriers",
    "config_loads",
    "config_years",
    "configure_logging",
    "constraints_for_year",
    "crf",
    "load_config",
    "mock_snakemake",
    "resolve_year_values",
    "snapshot_capital_weighting",
    "workbook_path",
]
