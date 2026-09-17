"""Render traceable capacity, generation, emissions and curtailment figures."""

# Set the non-interactive backend before importing pyplot. This is required on
# headless Snakemake workers and keeps the script independent of desktop state.
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import is_color_like
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from _helpers import configure_logging

MANAGED_PLOT_IDS = {
    "capacity_power", "capacity_storage_power", "capacity_links",
    "capacity_energy", "generation", "emissions", "curtailment",
}


def _read(folder: Path, name: str) -> pd.DataFrame:
    path = folder / name
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _bool_values(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin({"true", "1", "yes"})


def _carrier_order(frame: pd.DataFrame, configured: list[str]) -> list[str]:
    seen = [str(value) for value in frame["carrier"].dropna().unique()]
    return [value for value in configured if value in seen] + sorted(set(seen) - set(configured))


def _colors(frame: pd.DataFrame, carriers: list[str]) -> dict[str, str]:
    fallback = list(plt.get_cmap("tab20").colors)
    result: dict[str, str] = {}
    for index, carrier in enumerate(carriers):
        supplied = frame.loc[frame["carrier"].astype(str).eq(carrier), "color"] if "color" in frame else pd.Series(dtype=str)
        value = next((str(v) for v in supplied if pd.notna(v) and is_color_like(str(v))), None)
        result[carrier] = value or fallback[index % len(fallback)]
    return result


def _source_frame(
    frame: pd.DataFrame,
    value_column: str,
    scale: float,
    unit: str,
    configured_carriers: list[str],
) -> pd.DataFrame:
    required = {"year", "carrier", value_column}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Plot source is missing columns {sorted(missing)}")
    out = frame[[c for c in ("year", "carrier", value_column, "color") if c in frame]].copy()
    out["year"] = pd.to_numeric(out["year"], errors="raise").astype(int)
    out["carrier"] = out["carrier"].astype(str)
    out["value"] = pd.to_numeric(out.pop(value_column), errors="coerce").fillna(0.0) / scale
    out = out.groupby(["year", "carrier"], as_index=False).agg(
        value=("value", "sum"),
        **({"color": ("color", "first")} if "color" in out else {}),
    )
    out["unit"] = unit
    order = _carrier_order(out, configured_carriers)
    rank = {carrier: i for i, carrier in enumerate(order)}
    out["_rank"] = out["carrier"].map(rank).fillna(len(rank))
    return out.sort_values(["year", "_rank", "carrier"]).drop(columns="_rank").reset_index(drop=True)


def _stacked_bar(
    source: pd.DataFrame,
    title: str,
    ylabel: str,
    png: Path,
    svg: Path,
    configured_carriers: list[str],
) -> None:
    carriers = _carrier_order(source, configured_carriers)
    palette = _colors(source, carriers)
    pivot = source.pivot(index="year", columns="carrier", values="value").fillna(0.0)
    pivot = pivot.reindex(columns=carriers)
    width = max(8.0, 1.1 * len(pivot.index) + 4.0)
    fig, ax = plt.subplots(figsize=(width, 5.5), constrained_layout=True)
    nonzero = source["value"].abs().max() > 1e-12
    if nonzero:
        pivot.plot(kind="bar", stacked=True, ax=ax, color=[palette[c] for c in carriers], width=0.75)
    else:
        ax.set_xlim(-0.5, len(pivot.index) - 0.5)
        ax.set_ylim(0, 1)
        ax.set_xticks(range(len(pivot.index)), [str(year) for year in pivot.index])
        ax.text(
            0.5, 0.5, "No non-zero values in reported periods",
            transform=ax.transAxes, ha="center", va="center", color="#555555",
        )
    ax.set_title(title, loc="left", fontsize=14, fontweight="bold")
    ax.set_xlabel("Model year")
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="x", rotation=0)
    ax.grid(axis="y", alpha=0.25, linewidth=0.8)
    ax.set_axisbelow(True)
    if nonzero:
        ax.legend(title="Carrier", bbox_to_anchor=(1.02, 1), loc="upper left", frameon=False)
    fig.savefig(png, dpi=180, bbox_inches="tight")
    fig.savefig(svg, bbox_inches="tight")
    plt.close(fig)


def generate_plots(
    combined_dir: Path,
    manifest_path: Path,
    config: dict | None = None,
) -> dict:
    """Create figures plus the exact, unit-normalized CSV behind every plot."""
    config = config or {}
    configured_carriers = [str(c) for c in config.get("carriers", [])]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = manifest_path.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    required_sources = ["capacity.csv", "generation.csv", "emissions.csv", "curtailment.csv"]
    missing_sources = [name for name in required_sources if not (combined_dir / name).exists()]
    if missing_sources:
        raise FileNotFoundError(
            f"Cannot plot incomplete combined summaries; missing {missing_sources} in {combined_dir}"
        )

    def add(
        plot_id: str,
        frame: pd.DataFrame,
        value_column: str,
        scale: float,
        unit: str,
        title: str,
        ylabel: str,
    ) -> None:
        if frame.empty:
            return
        source = _source_frame(frame, value_column, scale, unit, configured_carriers)
        if source.empty:
            return
        source_path = data_dir / f"{plot_id}.csv"
        png = manifest_path.parent / f"{plot_id}.png"
        svg = manifest_path.parent / f"{plot_id}.svg"
        source.to_csv(source_path, index=False)
        _stacked_bar(source, title, ylabel, png, svg, configured_carriers)
        entries.append({
            "id": plot_id,
            "title": title,
            "unit": unit,
            "source": str(source_path.relative_to(manifest_path.parent)).replace("\\", "/"),
            "png": png.name,
            "svg": svg.name,
        })

    capacity = _read(combined_dir, "capacity.csv")
    if not capacity.empty:
        power = capacity.loc[capacity["unit"].eq("MW") & capacity["component"].eq("Generator")].copy()
        add("capacity_power", power, "optimal", 1_000.0, "GW", "Installed generation capacity", "Generation capacity (GW)")
        storage_power = capacity.loc[capacity["unit"].eq("MW") & capacity["component"].eq("StorageUnit")].copy()
        add("capacity_storage_power", storage_power, "optimal", 1_000.0, "GW", "Installed storage power capacity", "Storage power capacity (GW)")
        links = capacity.loc[capacity["unit"].eq("MW") & capacity["component"].isin(["Link", "Line", "Transformer"])].copy()
        add("capacity_links", links, "optimal", 1_000.0, "GW", "Installed Link and branch capacity", "Link/branch capacity (GW)")
        energy = capacity.loc[capacity["unit"].eq("MWh")].copy()
        add("capacity_energy", energy, "optimal", 1_000.0, "GWh", "Installed Store energy capacity", "Store energy capacity (GWh)")

    generation = _read(combined_dir, "generation.csv")
    add("generation", generation, "generation_MWh", 1_000_000.0, "TWh", "Annual electricity generation", "Generation (TWh)")

    emissions = _read(combined_dir, "emissions.csv")
    add("emissions", emissions, "tCO2", 1_000_000.0, "MtCO2", "Annual CO2 emissions", "Emissions (MtCO2)")

    curtailment = _read(combined_dir, "curtailment.csv")
    if not curtailment.empty and "interpretable" in curtailment:
        curtailment = curtailment.loc[_bool_values(curtailment["interpretable"])].copy()
    add("curtailment", curtailment, "curtailed_MWh", 1_000.0, "GWh", "Annual renewable curtailment", "Curtailed energy (GWh)")

    # Curtailment is applicable only when the summary contains at least one
    # interpretable variable-resource row. A thermal-only scenario is complete
    # without that chart.
    primary = {"capacity_power", "generation", "emissions"}
    emitted = {entry["id"] for entry in entries}
    if missing := sorted(primary - emitted):
        raise ValueError(f"Required plot(s) have no source rows: {missing}")

    manifest = {
        "schema_version": 1,
        "scenario": str(config.get("scenario", {}).get("name", "")),
        "source_directory": str(combined_dir.resolve()),
        "plots": entries,
    }
    # Optional plots may disappear when a rerun has no applicable components or
    # renewable-curtailment rows. Remove only files managed by this script.
    emitted = {entry["id"] for entry in entries}
    for plot_id in MANAGED_PLOT_IDS - emitted:
        for path in (
            data_dir / f"{plot_id}.csv",
            manifest_path.parent / f"{plot_id}.png",
            manifest_path.parent / f"{plot_id}.svg",
        ):
            if path.exists():
                path.unlink()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(snakemake: Any) -> None:
    logger = configure_logging(snakemake)
    combined = Path(snakemake.input.combined)
    manifest_path = Path(snakemake.output.manifest)
    manifest = generate_plots(combined.parent, manifest_path, snakemake.config)
    logger.info("wrote %d plot(s) and manifest %s", len(manifest["plots"]), manifest_path)


snakemake: Any = globals().get("snakemake")
if __name__ == "__main__":
    if snakemake is None:
        raise SystemExit("plot_results.py is run by Snakemake")
    main(snakemake)
