"""Combine managed result tables across the configured model years."""

# NB: no `from __future__ import annotations` — Snakemake's script:
# directive prepends a preamble, which makes a __future__ import a
# SyntaxError. Python 3.10+ handles the annotation syntax natively.
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from _helpers import config_years, configure_logging

SENTINEL = "all years combined\n"
MANAGED_TABLES = {
    "capacity.csv", "statistics.csv", "statistics_by_carrier.csv",
    "generation.csv", "emissions.csv", "curtailment.csv", "costs.csv",
    "objective.csv", "constraint_audit.csv", "constraint_config.csv",
}


def combine(csv_root: Path, years: list[int], logger) -> list[str]:
    """Write one combined CSV per managed table, adding missing year labels.

    Existing year columns are retained. Managed combined files with no current
    source table are removed so reruns cannot retain obsolete results.
    """
    tables: dict[str, list[pd.DataFrame]] = {}
    for year in years:
        folder = csv_root / str(year)
        if not folder.is_dir():
            raise FileNotFoundError(f"Missing required summary directory for FY{year}: {folder}")
        for path in sorted(folder.glob("*.csv")):
            if path.name not in MANAGED_TABLES:
                continue
            frame = pd.read_csv(path)
            if "year" not in frame.columns:
                frame.insert(0, "year", year)
            tables.setdefault(path.name, []).append(frame)

    out_dir = csv_root / "all_years"
    out_dir.mkdir(parents=True, exist_ok=True)
    expected = set(tables)
    for stale in out_dir.glob("*.csv"):
        if stale.name in MANAGED_TABLES and stale.name not in expected:
            stale.unlink()
    written: list[str] = []
    for name, frames in sorted(tables.items()):
        combined = pd.concat(frames, ignore_index=True, sort=False)
        combined.to_csv(out_dir / name, index=False)
        written.append(f"{name} ({len(combined)} rows)")
    return written


def main(snakemake: Any) -> None:
    logger = configure_logging(snakemake)
    years = config_years(snakemake.config)
    out = Path(snakemake.output.combined)
    written = combine(out.parent.parent, years, logger)
    logger.info("combined %d table(s): %s", len(written), ", ".join(written))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(SENTINEL, encoding="utf-8")


snakemake: Any = globals().get("snakemake")
if __name__ == "__main__":
    if snakemake is None:
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("combine_summaries")
    main(snakemake)
