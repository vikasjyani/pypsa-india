"""Generate a workbook from the project's single template settings file."""

from pathlib import Path
import re
import sys
import yaml

TEMPLATE_ROOT = PROJECT_ROOT
TEMPLATE_CONFIG = TEMPLATE_ROOT / "generate_template.yaml"


rule generate_template:
    input:
        settings=str(TEMPLATE_CONFIG)
    # Deliberately no output directive: this explicit command must reach the
    # writer's overwrite check on every invocation, and Snakemake must never
    # delete an existing, user-filled workbook when a generation job fails.
    run:
        sys.path.insert(0, str(TEMPLATE_ROOT / "scripts"))
        from make_template import write_template
        from make_scenario_config import scenario_settings, write_scenario_config
        from errors import InputValidationError

        config = yaml.safe_load(Path(input.settings).read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise InputValidationError("generate_template.yaml must contain a YAML mapping.")
        scenario = config.get("scenario", {}) or {}
        name = scenario.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise InputValidationError(
                "scenario.name must contain letters, digits, underscores or hyphens, "
                "and begin with a letter or digit."
            )
        years = scenario.get("years")
        if (not isinstance(years, list) or not years
                or any(type(year) is not int for year in years)
                or years != sorted(set(years))):
            raise InputValidationError("scenario.years must be a non-empty, ascending list of unique integers.")
        carriers = config.get("carriers")
        if (not isinstance(carriers, list) or not carriers
                or any(not isinstance(carrier, str) or not carrier.strip() for carrier in carriers)):
            raise InputValidationError("carriers must be a non-empty list of carrier names.")

        settings = config.get("template", {}) or {}
        merge, force = settings.get("merge", False), settings.get("force", False)
        if type(merge) is not bool or type(force) is not bool or (merge and force):
            raise InputValidationError("template.merge and template.force must be booleans; enable only one.")
        output = Path(settings.get("output") or f"data/{name}/{name}_template.xlsx")
        if not output.is_absolute():
            output = TEMPLATE_ROOT / output
        if output.suffix.lower() != ".xlsx":
            raise InputValidationError("template.output must end in .xlsx.")
        scenario_dir = (TEMPLATE_ROOT / "data" / name).resolve()
        if output.resolve().parent != scenario_dir:
            raise InputValidationError(
                "template.output must be inside data/<scenario.name>/ so the workbook "
                "and scenario_config.yaml remain a portable pair."
            )
        scenario_config = scenario_dir / "scenario_config.yaml"
        existing = [path for path in (output, scenario_config) if path.exists()]
        if existing and not (merge or force):
            raise InputValidationError(
                f"Generation pair already exists ({', '.join(str(path) for path in existing)}). "
                "Existing files have been preserved. "
                "Choose a new scenario.name/template.output, or enable template.merge "
                "or template.force in generate_template.yaml."
            )
        # Check both destinations before writing either.  This avoids replacing a
        # workbook when the companion YAML cannot be safely produced.
        effective = scenario_settings(
            config, scenario_config, output, TEMPLATE_ROOT, merge=merge,
        )
        write_scenario_config(
            config, scenario_config, workbook=output, root=TEMPLATE_ROOT,
            merge=merge, force=force, check_only=True, settings=effective,
        )
        sheets = write_template(effective, output, merge=merge, force=force)
        write_scenario_config(
            config, scenario_config, workbook=output, root=TEMPLATE_ROOT,
            merge=merge, force=force, settings=effective,
        )
        print(
            f"Generated {output} and {scenario_config}\n"
            f"Workbook contains {len(sheets) + 1} sheets including README."
        )
