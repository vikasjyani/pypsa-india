"""Build a joint investment model with annual costs and separate fiscal periods.

Each new project is a vintage. Overnight investment costs and lifetimes are
fixed at commissioning; fuel prices and availability follow the operating year.
Annual representative snapshots carry 8760 hours of objective/energy weights;
storage elapsed-hour weights retain the configured sampling resolution.
"""
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import pypsa

sys.path.insert(0, str(Path(__file__).parent))
import build_network as single
import registry as R
from _helpers import config_years, configure_logging
from errors import InputValidationError
from validate_inputs import discover_loads, read_workbook


def investment_weights(config, years):
    """Years represented and sum of annual social discount factors per period.

    The final period defaults to one year; users may explicitly supply
    investment_periods.years and investment_periods.discount_rate.
    """
    settings = config.get('investment_periods', {}) or {}
    rate = float(settings.get('discount_rate', 0.0))
    if not np.isfinite(rate) or rate < 0:
        raise InputValidationError('investment_periods.discount_rate must be finite and non-negative')
    explicit = settings.get('years', {}) or {}
    records = []
    for i, year in enumerate(years):
        default = years[i + 1] - year if i + 1 < len(years) else 1
        duration = float(explicit.get(year, explicit.get(str(year), default)))
        if not np.isfinite(duration) or duration <= 0 or not duration.is_integer():
            raise InputValidationError('investment_periods.years must contain positive whole-year durations')
        objective = sum((1 + rate) ** -(year - years[0] + offset) for offset in range(int(duration)))
        records.append((duration, objective))
    return pd.DataFrame(records, index=pd.Index(years, name='period'), columns=['years', 'objective'])


def build(config: Mapping, book: Mapping, years=None, logger=None):
    years = list(config_years(config) if years is None else years)
    years = [int(year) for year in years]
    if not years or years != sorted(set(years)):
        raise InputValidationError('Perfect foresight years must be non-empty, increasing and unique')
    annual = {year: single.build(config, book, year, logger) for year in years}
    n = pypsa.Network()
    n.name = f"{config.get('scenario', {}).get('name', 'scenario')}_perfect_foresight"
    snapshots = pd.MultiIndex.from_tuples(
        [(year, ts) for year, network in annual.items() for ts in network.snapshots],
        names=['period', 'timestep'],
    )
    n.set_snapshots(snapshots)
    n.investment_periods = years
    n.investment_period_weightings = investment_weights(config, years)
    n.meta = dict(annual[years[0]].meta)
    n.meta['pypsa_india'] = dict(n.meta['pypsa_india'], mode='perfect_foresight', model_years=years,
                               annual_energy_hours=8760, investment_discount_rate=float((config.get('investment_periods', {}) or {}).get('discount_rate', 0)))
    n.meta['pypsa_india'].pop('model_year', None)
    for year, source in annual.items():
        weights = source.snapshot_weightings.copy()
        annual_factor = float(source.meta['pypsa_india']['capital_weighting'])
        # Single-period generation energy is already annualized. Only its
        # sampled objective needs expansion here; storage retains elapsed hours.
        weights['objective'] *= annual_factor
        n.snapshot_weightings.loc[year, :] = weights.to_numpy()

    # target -> (origin period, annual component name). Undated extendable
    # conversion links receive investment vintages just like generation/storage.
    origins = {}
    for component in ('Bus', 'Carrier', 'Load', 'Generator', 'Link', 'Store', 'StorageUnit'):
        origins[component] = {}
        for year, source in annual.items():
            table = source.c[component].static
            for name, row in table.iterrows():
                target = str(name)
                values = row.to_dict()
                is_corridor = pd.notna(row.get('is_transfer_corridor')) and bool(row.get('is_transfer_corridor'))
                if component == 'Link' and is_corridor:
                    target = f'{name} {year}'
                if component == 'Link' and bool(row.get('p_nom_extendable', False)) and int(row.get('build_year', 0)) == 0:
                    target = f'{name} {year}'
                    values['build_year'] = year
                if target in origins[component]:
                    continue
                if 'capital_cost' in values:
                    # Builder annualises candidate assets then scales their
                    # costs to its sampled hours. Undo only this sample scale.
                    candidate = component in ('Store', 'StorageUnit') or (component == 'Generator' and str(name).endswith(f' {year}'))
                    if component == 'Link':
                        links = book.get('Links', pd.DataFrame())
                        explicit = links.loc[links['name'].astype(str).eq(str(name)), 'capital_cost'] if 'capital_cost' in links and 'name' in links else pd.Series(dtype=float)
                        candidate = explicit.empty or pd.to_numeric(explicit, errors='coerce').fillna(0).iloc[0] <= 0
                    if candidate:
                        values['capital_cost'] *= float(source.meta['pypsa_india']['capital_weighting'])
                if component == 'Link' and is_corridor:
                    # Transfer_Capacity is an available corridor total for the
                    # operating year, so successive fixed corridors never stack.
                    values['build_year'] = year
                    values['lifetime'] = years[years.index(year) + 1] - year if year != years[-1] else 1
                if int(values.get('build_year', 1)) == 0 and 'lifetime' in values:
                    values['lifetime'] = np.inf
                if 'lifetime' in values and not is_corridor and not bool(config.get('retirement', {}).get('enable', True)):
                    # PyPSA enforces lifetimes itself in investment mode, so
                    # merely skipping the single-period retirement pass is
                    # insufficient to honour retirement.enable=False.
                    values['lifetime'] = np.inf
                n.add(component, target, **values)
                origins[component][target] = (year, str(name))

    # Reuse the operating year's matching candidate to carry its availability
    # and fuel price onto every still-live vintage of that technology.
    for component in ('Load', 'Generator', 'Link', 'Store', 'StorageUnit'):
        target_table = n.c[component].static
        attributes = set().union(*(set(src.c[component].dynamic) for src in annual.values()))
        if component == 'Generator':
            attributes.add('marginal_cost')
        for attribute in attributes:
            frames = []
            populated = False
            for year, source in annual.items():
                source_table = source.c[component].static
                source_dynamic = source.c[component].dynamic.get(attribute, pd.DataFrame(index=source.snapshots))
                columns = {}
                for target, (origin_year, origin_name) in origins[component].items():
                    current = origin_name
                    dated = origin_name.endswith(f' {origin_year}')
                    if dated:
                        analog = origin_name[:-len(str(origin_year))] + str(year)
                        if analog in source_table.index:
                            current = analog
                    if current in source_dynamic:
                        columns[target] = source_dynamic[current]
                        populated = True
                    elif attribute in target_table:
                        # A vintage's fuel price follows the operating year;
                        # availability static defaults come from its own row.
                        columns[target] = source_table.at[current, attribute] if attribute == 'marginal_cost' and current in source_table.index else target_table.at[target, attribute]
                        populated |= attribute == 'marginal_cost'
                    else:
                        continue
                values = pd.DataFrame(columns, index=source.snapshots)
                values.index = pd.MultiIndex.from_product([[year], values.index], names=snapshots.names)
                frames.append(values)
            if populated:
                n.c[component].dynamic[attribute] = pd.concat(frames).reindex(snapshots)
    for component, attr in (('Store', 'e_cyclic_per_period'), ('StorageUnit', 'cyclic_state_of_charge_per_period')):
        if not n.c[component].static.empty:
            n.c[component].static[attr] = True
    if 'is_transfer_corridor' in n.links:
        # PyPSA's incremental row additions otherwise leave a mixture of
        # bool/float/NaN objects, which xarray cannot serialize to NetCDF.
        n.links['is_transfer_corridor'] = n.links['is_transfer_corridor'].fillna(False).astype(bool)
    return n


def main(snakemake: Any):
    logger = configure_logging(snakemake)
    config = snakemake.config
    workbook = Path(snakemake.input.workbook)
    sheets = R.resolve_sheets(config.get('components', {}) or {},
                             snapshots_select=str(config.get('snapshots', {}).get('select', 'all')),
                             loads=discover_loads(workbook))
    book = read_workbook(workbook, sheets)
    n = build(config, book, logger=logger)
    output = Path(snakemake.output.network)
    output.parent.mkdir(parents=True, exist_ok=True)
    n.export_to_netcdf(output)
    logger.info('wrote perfect-foresight network: %s; periods=%s', output, list(n.investment_periods))


snakemake = globals().get('snakemake')
if __name__ == '__main__':
    if snakemake is None:
        from _helpers import mock_snakemake
        snakemake = mock_snakemake('build_perfect_foresight', year='all')
    main(snakemake)
