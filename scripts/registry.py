"""The component registry — the single source of truth for the input workbook.

`make_template.py`, `validate_inputs.py` and `build_network.py` all read this
module, so the emitted template, the validation rules and the network builder
cannot drift apart.

Three things are deliberately *not* constants, because they are functions of the
scenario config rather than of the schema:

- year columns    — one per ``scenario.years``   (``Years.REQUIRED`` / ``OPTIONAL``)
- carrier columns — one per ``carriers``          (``Sheet.carrier_columns``)
- which sheets exist at all — ``Sheet.default`` plus the selection in
  ``components:``, closed transitively over ``Sheet.requires``.

A sheet whose ``years`` is ``OPTIONAL`` is read in whichever shape it arrives:
year columns if present, a single static value column otherwise. `FOM` is the
motivating case — it is year-invariant in the current workbook but escalates in
others.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal, Mapping, Sequence

# ---------------------------------------------------------------- vocabulary

#: How a sheet is laid out.
Shape = Literal["asset_list", "timeseries", "lookup", "wide_year"]

#: Whether a sheet carries one column per modelled year.
NO, REQUIRED, OPTIONAL = "no", "required", "optional"

#: The value placed in unfilled cells of a generated template.
FILL_ME = "Please fill here"

#: Component-role buses. `electricity` buses are geographic; the other two are
#: wiring artefacts that terminate the store/storage link pairs.
BUS_ROLES = ("electricity", "storage", "store")

#: Values accepted in the `Type` column, which selects the PyPSA component.
#: Validation normalizes case while warning about non-canonical spellings.
COMPONENT_TYPES = ("Generator", "Storage", "Store", "Links")

#: Non-generation carrier labels used for links and storage wiring. These are
#: exempt from the configured-carrier foreign key.
PSEUDO_CARRIERS = ("Links", "Storage", "Store", "AC", "DC")

#: Bus labels used in cost records that do not identify a `Buses` row.
PSEUDO_BUSES = ("Links",)


# ------------------------------------------------------------------- schema


@dataclass(frozen=True)
class Col:
    """One column of one sheet."""

    name: str
    dtype: Literal["str", "float", "int", "bool", "date"] = "float"
    required: bool = True
    unit: str = ""
    ge: float | None = None
    le: float | None = None
    #: Header spellings accepted from an existing workbook and renamed to
    #: `name` on load. The empty string matches a blank header cell.
    aliases: tuple[str, ...] = ()
    #: Referential integrity: "Sheet.column", or the pseudo-tables
    #: "carriers" / "buses" / "states" / "regions".
    fk: str | None = None
    choices: tuple[str, ...] | None = None
    #: Emitted into the template instead of FILL_ME, and used by the builder
    #: when the column is absent from a filled workbook.
    default: Any = None
    help: str = ""

    @property
    def optional(self) -> bool:
        return not self.required


@dataclass(frozen=True)
class Sheet:
    """One sheet of the input workbook."""

    name: str
    shape: Shape
    columns: tuple[Col, ...]
    #: Columns that together identify a row. Empty for positional timeseries.
    key: tuple[str, ...] = ()
    #: True → always emitted. False → only when named in `components:`.
    default: bool = False
    #: Other sheets this one implies. Closed transitively by `resolve_sheets`.
    requires: tuple[str, ...] = ()
    #: NO | REQUIRED | OPTIONAL — see module docstring.
    years: str = NO
    #: True → one extra column per configured carrier (P_max_pu, P_min_pu).
    carrier_columns: bool = False
    #: True → shipped populated as reference data, not stubbed for the user.
    seeded: bool = False
    #: The PyPSA component this sheet feeds, for documentation.
    target: str = ""
    help: str = ""

    def column(self, name: str) -> Col | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def column_names(
        self,
        *,
        years: Sequence[int] = (),
        carriers: Sequence[str] = (),
        wide: bool | None = None,
    ) -> list[str]:
        """The full ordered column list for a given scenario config.

        `wide` overrides the year decision for OPTIONAL sheets: True emits year
        columns, False emits the static value column. Defaults to False, which
        matches the current workbook's FOM.
        """
        names = [col.name for col in self.columns]
        if self.carrier_columns:
            names += list(carriers)
        if self.years == REQUIRED or (self.years == OPTIONAL and wide):
            names += [str(year) for year in years]
        return names


# ------------------------------------------------- shared column fragments

#: Component scope fields. The network builder fills blank values from the
#: `Buses` sheet so geographic constraints can select component rows.
def _scope_cols(required: bool = True) -> tuple[Col, ...]:
    return (
        Col("state_id", "str", required=required, fk="states",
            help="Two-letter state code. Set by the generator from `spatial`."),
        Col("region_id", "str", required=required, fk="regions",
            help="Regional grid code (NR/WR/SR/ER/NER)."),
    )


_TECH_KEY = (
    Col("TECHNOLOGY", "str", help="Technology label; joins the cost sheets."),
    Col("carrier", "str", fk="carriers"),
    Col("Type", "str", choices=COMPONENT_TYPES,
        help="PyPSA component class this row describes."),
)

_UNIT = Col("Unit", "str", required=False, help="Free text, documentation only.")


# ------------------------------------------------------------------ sheets

REGIONS = Sheet(
    name="Regions",
    shape="lookup",
    key=("state_id",),
    default=True,
    seeded=True,
    target="",
    help="India state → regional-grid map. Reference data: shipped filled.",
    columns=(
        Col("state_id", "str"),
        Col("state_name", "str"),
        Col("region_id", "str"),
    ),
)

BUSES = Sheet(
    name="Buses",
    shape="asset_list",
    key=("name",),
    default=True,
    target="Bus",
    help=(
        "Electricity buses are geographic and generated from `spatial`. "
        "The storage/store roles are wiring artefacts: with shared role buses "
        "there is exactly one of each for the whole model."
    ),
    columns=(
        Col("name", "str"),
        Col("role", "str", required=False, choices=BUS_ROLES, default="electricity"),
        *_scope_cols(required=False),  # role buses are not geographic
        Col("v_nom", "float", required=False, unit="kV", ge=0),
    ),
)

DEMAND = Sheet(
    name="Demand",
    shape="timeseries",
    default=True,
    years=REQUIRED,
    target="Load.p_set",
    help=(
        "One row per snapshot of the financial year, in order — the builder "
        "stamps the datetime index positionally. One column per modelled year."
    ),
    columns=(
        Col("sn", "int", required=False,
            help="Row counter. Documentation only — nothing reads it; order is what "
                 "matters. Optional because a sheet is as long as its longest year "
                 "column, so the padding rows beside a leap column have no counter "
                 "and are not data."),
    ),
)

#: Per-load demand sheets are named `Demand_<load name>`, so the sheet SET is a
#: function of the Loads sheet's contents rather than of the config alone.
DEMAND_PREFIX = "Demand_"


def demand_sheet_name(load: str) -> str:
    """`Demand_wr` for a load named `wr`."""
    return f"{DEMAND_PREFIX}{load}"


def demand_sheet(load: str) -> "Sheet":
    """A `Demand_<load>` sheet: the same shape as the single-load `Demand`."""
    return replace(
        DEMAND,
        name=demand_sheet_name(load),
        target=f"Load[{load}].p_set",
        help=(
            f"Hourly profile for the load named {load!r} in the Loads sheet. "
            "One row per snapshot in order; one column per modelled year."
        ),
    )


LOADS = Sheet(
    name="Loads",
    shape="asset_list",
    key=("name",),
    default=True,
    target="Load",
    help=(
        "One row per load point. Each active row needs a matching "
        "`Demand_<name>` sheet carrying its hourly profile."
    ),
    columns=(
        Col("name", "str", help="Unique load name. Also names its Demand_<name> sheet."),
        Col("bus", "str", fk="Buses.name", help="Bus/node the load is connected to."),
        Col("carrier", "str", required=False, default="AC",
            help="Carrier type, usually AC."),
        Col("active", "bool", required=False, default=True,
            help="Include in the simulation. FALSE leaves the row and its "
                 "Demand sheet in place but out of the network."),
    ),
)

GENERATORS = Sheet(
    name="Generators",
    shape="asset_list",
    key=("name",),
    default=True,
    target="Generator",
    help="Existing plant. One row per real unit.",
    columns=(
        Col("name", "str"),
        Col("bus", "str", fk="Buses.name"),
        *_scope_cols(required=False),  # derived from bus when blank
        Col("carrier", "str", fk="carriers"),
        Col("p_nom", "float", unit="MW", ge=0),
        Col("marginal_cost", "float", unit="Rs/MWh", ge=0,
            help="All-in variable cost. New generators instead read Fuel_cost."),
        Col("build_year", "int", ge=1900, le=2100),
        Col("lifetime", "float", unit="years", ge=0, le=200,
            help="Duration, not a year. >200 warns but is not rejected."),
        Col("capital_cost", "float", required=False, unit="Rs/MW", ge=0, default=0.0),
        Col("committable", "bool", required=False, default=False),
        Col("start_up_cost", "float", required=False, unit="Rs/MW", ge=0, default=0.0),
        Col("shut_down_cost", "float", required=False, unit="Rs/MW", ge=0, default=0.0),
        Col("min_up_time", "int", required=False, unit="h", ge=0, default=0),
        Col("min_down_time", "int", required=False, unit="h", ge=0, default=0),
        Col("ramp_limit_up", "float", required=False, unit="p.u./h", ge=0, le=1, default=1.0),
        Col("ramp_limit_down", "float", required=False, unit="p.u./h", ge=0, le=1, default=1.0),
        Col("p_nom_extendable", "bool", required=False, default=False,
            help="Ignored for existing plant. The builder derives this from the "
                 "carrier (True only for 'Market')."),
        Col("ownership", "str", required=False,
            help="Carried through as metadata. Use one ownership column spelling."),
        Col("Expiry date of Contract", "str", required=False,
            help="Metadata only; no PyPSA attribute reads it."),
    ),
)

P_MAX_PU = Sheet(
    name="P_max_pu",
    shape="timeseries",
    default=True,
    carrier_columns=True,
    target="Generator.p_max_pu",
    help="Availability per carrier, one row per snapshot. Missing carrier ⇒ 1.0.",
    columns=(Col("snapshot", "date", aliases=("base snaps",)),),
)

P_MIN_PU = Sheet(
    name="P_min_pu",
    shape="timeseries",
    default=True,
    carrier_columns=True,
    target="Generator.p_min_pu",
    help="Must-run floor per carrier, one row per snapshot. Missing carrier ⇒ 0.0.",
    columns=(Col("snapshot", "date", aliases=("base snaps",)),),
)

CAPITAL_COST = Sheet(
    name="Capital_cost",
    shape="wide_year",
    key=("TECHNOLOGY", "bus", "carrier", "Type"),
    default=True,
    years=REQUIRED,
    target="capital_cost (annuity input)",
    help="Overnight capex. Needs a carrier='Links' row for the inverter pair.",
    columns=(
        Col("TECHNOLOGY", "str"),
        Col("bus", "str", fk="Buses.name"),
        Col("carrier", "str", fk="carriers"),
        Col("Type", "str", choices=COMPONENT_TYPES),
        _UNIT,
    ),
)

WACC = Sheet(
    name="wacc",
    shape="wide_year",
    key=("name",),
    default=True,
    years=REQUIRED,
    target="annuity rate",
    help=(
        "Named rows, selected by `costs.wacc_row` and `costs.wacc_row_storage`."
    ),
    columns=(
        Col("name", "str", aliases=("", "Unnamed: 0"),
            help="Row label, e.g. default, storage, interest_rate."),
    ),
)

LIFETIME = Sheet(
    name="Lifetime",
    shape="lookup",
    key=("TECHNOLOGY", "carrier", "Type"),
    default=True,
    target="annuity nper, .lifetime",
    columns=(
        *_TECH_KEY,
        _UNIT,
        Col("lifetime", "float", unit="years", ge=0, le=200),
        Col("Operational_Life", "float", required=False, unit="years", ge=0, le=200),
    ),
)

FOM = Sheet(
    name="FOM",
    shape="lookup",
    key=("TECHNOLOGY", "carrier", "Type"),
    default=True,
    years=OPTIONAL,
    target="added to the annuitised capital cost",
    help=(
        "Fixed O&M. Year-invariant in the current workbook — a single FOM "
        "column. Supply year columns instead and the loader reads it wide, so "
        "escalating FOM needs no code change."
    ),
    columns=(
        *_TECH_KEY,
        _UNIT,
        Col("FOM", "float", required=False, unit="Rs/MW", ge=0,
            help="Static value. Omit this column when supplying year columns."),
    ),
)

CO2 = Sheet(
    name="CO2",
    shape="lookup",
    key=("carrier",),
    default=True,
    target="Carrier.co2_emissions, Carrier.color",
    columns=(
        Col("TECHNOLOGY", "str", required=False),
        Col("carrier", "str", fk="carriers"),
        Col("tonnes/MWh", "float", unit="t/MWh", ge=0),
        Col("color", "str", required=False, help="Hex colour; feeds n.carriers.color."),
    ),
)

NEW_GENERATORS = Sheet(
    name="New_Generators",
    shape="asset_list",
    key=("TECHNOLOGY", "bus"),
    requires=("Pipeline_p_min", "Pipeline_p_max", "Fuel_cost", "Startupcost"),
    target="Generator (one vintage per modelled year)",
    help=(
        "Candidate capacity. Instantiated once per modelled year as "
        "'{TECHNOLOGY} {bus} {year}', so bounds are per vintage and additive."
    ),
    columns=(
        Col("TECHNOLOGY", "str"),
        Col("bus", "str", fk="Buses.name"),
        Col("carrier", "str", fk="carriers"),
        Col("Type", "str", choices=COMPONENT_TYPES, default="Generator"),
        Col("p_nom_extendable", "bool", default=True),
        Col("p_min_pu", "float", required=False, unit="p.u.", ge=0, le=1, default=0.0),
        Col("min_up_time", "int", required=False, unit="h", ge=0, default=0),
        Col("min_down_time", "int", required=False, unit="h", ge=0, default=0),
        Col("ramp_limit_up", "float", required=False, unit="p.u./h", ge=0, le=1, default=1.0),
        Col("ramp_limit_down", "float", required=False, unit="p.u./h", ge=0, le=1, default=1.0),
    ),
)

PIPELINE_P_MIN = Sheet(
    name="Pipeline_p_min",
    shape="wide_year",
    key=("TECHNOLOGY", "bus"),
    years=REQUIRED,
    target="Generator.p_nom_min",
    help=(
        "Committed capacity the optimiser must build, per vintage. "
        "INCREMENTAL, not cumulative — one vintage per year."
    ),
    columns=(
        Col("TECHNOLOGY", "str"),
        Col("bus", "str", fk="Buses.name"),
        Col("carrier", "str", required=False, fk="carriers"),
        Col("Type", "str", required=False, choices=COMPONENT_TYPES),
    ),
)

PIPELINE_P_MAX = replace(
    PIPELINE_P_MIN,
    name="Pipeline_p_max",
    target="Generator.p_nom_max",
    help="Technical/resource ceiling per vintage. 'inf' is allowed.",
)

FUEL_COST = Sheet(
    name="Fuel_cost",
    shape="wide_year",
    key=("TECHNOLOGY", "Bus", "carrier", "Type"),
    years=REQUIRED,
    target="Generator.marginal_cost (new generators only)",
    help=(
        "Fuel-only variable cost for new build. Existing plant uses its own "
        "all-in Generators.marginal_cost — deliberately a different quantity."
    ),
    columns=(
        Col("TECHNOLOGY", "str", required=False),
        Col("Bus", "str", required=False, fk="Buses.name"),
        Col("carrier", "str", fk="carriers"),
        Col("Type", "str", choices=COMPONENT_TYPES),
        _UNIT,
    ),
)

STARTUP_COST = Sheet(
    name="Startupcost",
    shape="wide_year",
    key=("carrier", "Type"),
    years=REQUIRED,
    target="Generator.start_up_cost and .shut_down_cost",
    help="One value feeds both start-up and shut-down cost; there is no separate sheet.",
    columns=(
        Col("carrier", "str", fk="carriers"),
        Col("Type", "str", choices=COMPONENT_TYPES),
        _UNIT,
    ),
)

NEW_STORAGE = Sheet(
    name="New_Storage",
    shape="asset_list",
    key=("TECHNOLOGY", "bus"),
    requires=("Pipeline_storage",),
    target="Store when Type=='Store', else StorageUnit",
    help="`Type` is the component discriminator — the crux of the selection mechanism.",
    columns=(
        Col("TECHNOLOGY", "str"),
        Col("bus", "str", fk="Buses.name"),
        Col("carrier", "str", fk="carriers"),
        Col("Type", "str", choices=("Store", "Storage")),
    ),
)

PIPELINE_STORAGE = Sheet(
    name="Pipeline_storage",
    shape="wide_year",
    key=("TECHNOLOGY", "bus"),
    years=REQUIRED,
    target="Store.e_nom / StorageUnit.p_nom_min+p_nom_max",
    help=(
        "Committed storage per vintage. NOTE the unit depends on Type: MWh for "
        "Store rows, MW for Storage rows — make_template's units row cannot "
        "show that per row; it just reads 'value'."
    ),
    columns=(
        Col("TECHNOLOGY", "str"),
        Col("bus", "str", fk="Buses.name"),
        Col("carrier", "str", required=False, fk="carriers"),
        Col("Type", "str", choices=("Store", "Storage")),
    ),
)

LINKS = Sheet(
    name="Links",
    shape="asset_list",
    key=("name",),
    target="Link",
    help="Explicit links. Store charge/discharge pairs are generated, not listed here.",
    columns=(
        Col("name", "str"),
        Col("bus0", "str", fk="Buses.name"),
        Col("bus1", "str", fk="Buses.name"),
        Col("type", "str", required=False),
        Col("carrier", "str", required=False, fk="carriers"),
        Col("efficiency", "float", ge=0, le=1, default=1.0),
        Col("p_nom_extendable", "bool", default=True),
        Col("p_nom", "float", required=False, unit="MW", ge=0, default=0.0),
        Col("p_nom_min", "float", required=False, unit="MW", ge=0, default=0.0),
        Col("p_nom_max", "float", required=False, unit="MW", ge=0),
        Col("p_min_pu", "float", required=False, unit="p.u.", ge=-1, le=1, default=0.0),
        Col("p_max_pu", "float", required=False, unit="p.u.", ge=0, le=1, default=1.0),
        Col("capital_cost", "float", required=False, unit="Rs/MW", ge=0, default=0.0),
        Col("marginal_cost", "float", required=False, unit="Rs/MWh", ge=0, default=0.0),
    ),
)

TRANSFER_CAPACITY = Sheet(
    name="Transfer_Capacity",
    shape="wide_year",
    key=("from_bus", "to_bus"),
    years=REQUIRED,
    target="Link (inter-bus corridor)",
    help=(
        "Inter-bus transfer limits, one column per year (MW). Becomes a Link "
        "pair and feeds constraints.ImportExportLimit."
    ),
    columns=(
        Col("from_bus", "str", fk="Buses.name"),
        Col("to_bus", "str", fk="Buses.name"),
        Col("efficiency", "float", required=False, ge=0, le=1, default=1.0),
    ),
)

CUSTOM_DAYS = Sheet(
    name="Custom days",
    shape="lookup",
    key=("Month", "Day"),
    target="snapshot selection",
    help="Representative days. Only read when snapshots.select == 'custom_days'.",
    columns=(
        Col("Month", "int", ge=1, le=12),
        Col("Day", "int", ge=1, le=31),
    ),
)


#: Every sheet the workflow knows about, in template emission order.
SHEETS: tuple[Sheet, ...] = (
    REGIONS,
    BUSES,
    LOADS,
    DEMAND,
    GENERATORS,
    P_MAX_PU,
    P_MIN_PU,
    NEW_GENERATORS,
    PIPELINE_P_MIN,
    PIPELINE_P_MAX,
    NEW_STORAGE,
    PIPELINE_STORAGE,
    LINKS,
    TRANSFER_CAPACITY,
    CAPITAL_COST,
    FUEL_COST,
    STARTUP_COST,
    WACC,
    LIFETIME,
    FOM,
    CO2,
    CUSTOM_DAYS,
)

BY_NAME: Mapping[str, Sheet] = {sheet.name: sheet for sheet in SHEETS}

#: `components:` key → the sheets it turns on. Everything else is default.
COMPONENT_SHEETS: Mapping[str, tuple[str, ...]] = {
    "buses": ("Buses",),
    "generators": ("Generators",),
    "demand": ("Demand",),
    "loads": ("Loads",),
    "availability": ("P_max_pu", "P_min_pu"),
    "new_generators": ("New_Generators",),
    "new_stores": ("New_Storage",),
    "storage_units": ("New_Storage",),
    "links": ("Links",),
    "transfer": ("Transfer_Capacity",),
}

#: Recognized sheet names that the current workflow does not use.
DROPPED: Mapping[str, str] = {
    "base Generators": "not used by the current workflow",
    "Demand_final": "not used by the current workflow",
    "Demand": "use the active Demand or Demand_<name> sheets",
    "Settings": "use scenario configuration instead",
    "Monthly_Constraints": "use MonthlyCufsProfile in scenario constraints instead",
    "Pipe_Line_Generators_p_min": "accepted as the Pipeline_p_min alias",
    "Pipe_Line_Generators_p_max": "accepted as the Pipeline_p_max alias",
    "Pipe_Line_Storage_p_min": "accepted as the Pipeline_storage alias",
}


# ---------------------------------------------------------------- selection


def resolve_sheets(
    components: Mapping[str, bool],
    *,
    snapshots_select: str = "all",
    loads: Sequence[str] = (),
) -> list[Sheet]:
    """The sheets implied by a config, in emission order.

    Default sheets are always present. Selected components add theirs, and
    `requires` is closed transitively so a user who asks for `new_generators`
    never has to know it also needs the pipeline and cost sheets.
    """
    wanted: set[str] = {sheet.name for sheet in SHEETS if sheet.default}

    for component, enabled in components.items():
        if not enabled:
            continue
        if component not in COMPONENT_SHEETS:
            raise KeyError(
                f"Unknown component {component!r}. "
                f"Valid: {', '.join(sorted(COMPONENT_SHEETS))}"
            )
        wanted.update(COMPONENT_SHEETS[component])

    if snapshots_select == "custom_days":
        wanted.add("Custom days")

    # transitive closure over `requires`
    pending = list(wanted)
    while pending:
        sheet = BY_NAME[pending.pop()]
        for name in sheet.requires:
            if name not in wanted:
                wanted.add(name)
                pending.append(name)

    resolved = [sheet for sheet in SHEETS if sheet.name in wanted]

    # Each declared load receives a Demand_<name> sheet. Without loads, retain
    # the single Demand sheet.
    if loads:
        resolved = [s for s in resolved if s.name != "Demand"]
        index = next(
            (i for i, s in enumerate(resolved) if s.name == "Loads"), len(resolved) - 1
        )
        for offset, load in enumerate(loads, start=1):
            resolved.insert(index + offset, demand_sheet(load))
    else:
        # The single Demand sheet does not require a Loads sheet.
        resolved = [s for s in resolved if s.name != "Loads"]
    return resolved


#: Years a model could plausibly cover. Wide enough for any scenario, narrow
#: enough that it cannot collide with a month key (1-12) or a carrier name.
YEAR_RANGE = (1900, 2200)


def is_year_map(value: Any, years: Sequence[int] = ()) -> bool:
    """True when `value` is keyed by calendar year.

    The disambiguation rule for constraint config: a mapping whose keys all
    parse as integers inside `YEAR_RANGE` is a year-map and gets resolved to a
    scalar. Anything else — `max_cf_by_month` (keyed 1-12),
    `capacity_credit_by_carrier` (keyed by carrier string) — passes through.

    Deliberately independent of `years`: a map may carry horizons this scenario
    does not model, and the extras are simply unused. Only a *missing* modelled
    year is an error, and `_helpers.resolve_year_values` raises on that — a
    map judged "not a year map" because it covered too much would otherwise
    reach `float()` as a raw dict.
    """
    if not isinstance(value, Mapping) or not value:
        return False
    low, high = YEAR_RANGE
    try:
        keys = {int(key) for key in value}
    except (TypeError, ValueError):
        return False
    return all(low <= key <= high for key in keys)


__all__ = [
    "BUS_ROLES",
    "BY_NAME",
    "COMPONENT_SHEETS",
    "COMPONENT_TYPES",
    "DEMAND_PREFIX",
    "demand_sheet",
    "demand_sheet_name",
    "Col",
    "DROPPED",
    "FILL_ME",
    "NO",
    "OPTIONAL",
    "REQUIRED",
    "PSEUDO_BUSES",
    "PSEUDO_CARRIERS",
    "SHEETS",
    "Sheet",
    "YEAR_RANGE",
    "is_year_map",
    "resolve_sheets",
]
