"""India's state-to-region mapping, bus generation, and scope metadata.

`REGIONS` supplies the reference map used in generated workbooks. `stamp_scope`
adds state and region identifiers to component rows so geographic constraints
can select them at any configured bus resolution.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import pandas as pd

from errors import InputValidationError

#: The five regional grids operated by Grid-India (formerly POSOCO).
REGION_NAMES: Mapping[str, str] = {
    "NR": "Northern Region",
    "WR": "Western Region",
    "SR": "Southern Region",
    "ER": "Eastern Region",
    "NER": "North Eastern Region",
}

#: state_id -> (state_name, region_id). Codes are ISO 3166-2:IN subdivisions.
STATES: Mapping[str, tuple[str, str]] = {
    # Northern
    "CH": ("Chandigarh", "NR"),
    "DL": ("Delhi", "NR"),
    "HR": ("Haryana", "NR"),
    "HP": ("Himachal Pradesh", "NR"),
    "JK": ("Jammu and Kashmir", "NR"),
    "LA": ("Ladakh", "NR"),
    "PB": ("Punjab", "NR"),
    "RJ": ("Rajasthan", "NR"),
    "UP": ("Uttar Pradesh", "NR"),
    "UK": ("Uttarakhand", "NR"),
    # Western
    "CT": ("Chhattisgarh", "WR"),
    "DH": ("Dadra and Nagar Haveli and Daman and Diu", "WR"),
    "GA": ("Goa", "WR"),
    "GJ": ("Gujarat", "WR"),
    "MP": ("Madhya Pradesh", "WR"),
    "MH": ("Maharashtra", "WR"),
    # Southern
    "AP": ("Andhra Pradesh", "SR"),
    "KA": ("Karnataka", "SR"),
    "KL": ("Kerala", "SR"),
    "PY": ("Puducherry", "SR"),
    "TN": ("Tamil Nadu", "SR"),
    "TG": ("Telangana", "SR"),
    # Eastern
    "BR": ("Bihar", "ER"),
    "JH": ("Jharkhand", "ER"),
    "OR": ("Odisha", "ER"),
    "SK": ("Sikkim", "ER"),
    "WB": ("West Bengal", "ER"),
    # North Eastern
    "AR": ("Arunachal Pradesh", "NER"),
    "AS": ("Assam", "NER"),
    "MN": ("Manipur", "NER"),
    "ML": ("Meghalaya", "NER"),
    "MZ": ("Mizoram", "NER"),
    "NL": ("Nagaland", "NER"),
    "TR": ("Tripura", "NER"),
}

#: Island systems are excluded because they have no synchronous mainland-grid
#: connection. Include them only when modelling their isolated systems.
ISLANDED_REGIONS: tuple[str, ...] = ()

#: The bus standing in for everything outside the study boundary.
EXTERNAL_BUS = "EXT"
EXTERNAL_STATE = "EXT"
EXTERNAL_REGION = "EXT"


def regions_table() -> pd.DataFrame:
    """The seeded `Regions` sheet."""
    return pd.DataFrame(
        [
            {"state_id": code, "state_name": name, "region_id": region}
            for code, (name, region) in STATES.items()
        ]
    ).sort_values(["region_id", "state_id"], ignore_index=True)


def states_in(level: str, identifier: str) -> list[str]:
    """Every state inside a boundary."""
    level = level.lower()
    if level == "country":
        return sorted(STATES)
    if level == "region":
        if identifier not in REGION_NAMES:
            raise InputValidationError(
                f"Unknown region {identifier!r}. "
                f"Valid: {', '.join(sorted(REGION_NAMES))}"
            )
        return sorted(code for code, (_, r) in STATES.items() if r == identifier)
    if level == "state":
        if identifier not in STATES:
            raise InputValidationError(
                f"Unknown state {identifier!r}. Valid: {', '.join(sorted(STATES))}"
            )
        return [identifier]
    raise InputValidationError(
        f"Unknown boundary level {level!r}. Valid: country, region, state"
    )


def default_resolution(level: str) -> str:
    """Return the default bus resolution for a boundary.

    Country boundaries use regions; regional and state boundaries use states.
    `spatial.resolution` can override this default.
    """
    return {"country": "region", "region": "state", "state": "state"}[level.lower()]


def generate_buses(spatial: Mapping) -> pd.DataFrame:
    """Build the `Buses` sheet from the `spatial:` configuration block.

    Electricity buses follow `resolution`. Storage and store buses terminate
    generated storage links and can be shared or created for each bus.
    """
    boundary = spatial.get("boundary", {})
    level = str(boundary.get("level", "state"))
    identifier = str(boundary.get("id", ""))
    resolution = str(spatial.get("resolution") or default_resolution(level))
    external = str(spatial.get("external", "aggregate"))
    role_buses = str(spatial.get("role_buses", "shared"))

    inside = states_in(level, identifier)
    if not bool(spatial.get("include_islanded", False)):
        inside = [s for s in inside if STATES[s][1] not in ISLANDED_REGIONS]
        if not inside:
            raise InputValidationError(
                f"Every state in {identifier!r} is islanded "
                f"({', '.join(ISLANDED_REGIONS)}). Set spatial.include_islanded: "
                "true to model it anyway."
            )

    rows: list[dict] = []
    if resolution == "state":
        for state in inside:
            rows.append(
                {
                    "name": state,
                    "role": "electricity",
                    "state_id": state,
                    "region_id": STATES[state][1],
                }
            )
    elif resolution == "region":
        for region in sorted({STATES[s][1] for s in inside if STATES[s][1]}):
            rows.append(
                {"name": region, "role": "electricity", "state_id": "", "region_id": region}
            )
    else:
        raise InputValidationError(
            f"Unknown spatial resolution {resolution!r}. Valid: state, region"
        )

    if external == "aggregate":
        rows.append(
            {
                "name": EXTERNAL_BUS,
                "role": "electricity",
                "state_id": EXTERNAL_STATE,
                "region_id": EXTERNAL_REGION,
            }
        )
    elif external != "none":
        raise InputValidationError(
            f"Unknown spatial.external {external!r}. Valid: aggregate, none"
        )

    electricity = [row["name"] for row in rows]
    if role_buses == "shared":
        rows.append({"name": "Storage", "role": "storage", "state_id": "", "region_id": ""})
        rows.append({"name": "Store", "role": "store", "state_id": "", "region_id": ""})
    elif role_buses == "per_bus":
        for bus in electricity:
            rows.append(
                {"name": f"{bus}_Storage", "role": "storage",
                 "state_id": "", "region_id": ""}
            )
            rows.append(
                {"name": f"{bus}_Store", "role": "store", "state_id": "", "region_id": ""}
            )
    else:
        raise InputValidationError(
            f"Unknown spatial.role_buses {role_buses!r}. Valid: shared, per_bus"
        )

    frame = pd.DataFrame(rows)
    frame["v_nom"] = pd.NA
    return frame


def stamp_scope(
    table: pd.DataFrame,
    buses: pd.DataFrame,
    *,
    bus_column: str = "bus",
) -> pd.DataFrame:
    """Fill `state_id` / `region_id` on a component table from its bus.

    Required by every scoped constraint: `scope_mask` reads these columns off
    the component table, so a component missing them cannot be selected by a
    `GeographicScope` and silently drops out of the constraint.

    Existing non-blank values win — an explicit override in the workbook is
    respected rather than overwritten from topology.
    """
    if bus_column not in table:
        return table

    lookup = buses.set_index("name")
    out = table.copy()
    for column in ("state_id", "region_id"):
        mapped = out[bus_column].map(lookup[column]) if column in lookup else pd.Series(
            pd.NA, index=out.index
        )
        if column in out:
            existing = out[column].astype("object").where(
                out[column].notna() & out[column].astype(str).str.strip().ne(""), other=pd.NA
            )
            out[column] = existing.fillna(mapped)
        else:
            out[column] = mapped
        out[column] = out[column].fillna("")
    return out


def validate_scope_columns(table: pd.DataFrame, label: str) -> list[str]:
    """Report components whose scope columns are blank. Empty list == fine."""
    problems: list[str] = []
    for column in ("state_id", "region_id"):
        if column not in table:
            problems.append(f"{label}: missing column {column!r}")
            continue
        blank = table.index[
            table[column].isna() | table[column].astype(str).str.strip().eq("")
        ]
        if len(blank):
            sample = ", ".join(str(x) for x in list(blank)[:5])
            problems.append(
                f"{label}: {len(blank)} row(s) have a blank {column} "
                f"(e.g. {sample}) — scoped constraints cannot select them"
            )
    return problems


__all__ = [
    "EXTERNAL_BUS",
    "ISLANDED_REGIONS",
    "REGION_NAMES",
    "STATES",
    "default_resolution",
    "generate_buses",
    "regions_table",
    "stamp_scope",
    "states_in",
    "validate_scope_columns",
]
