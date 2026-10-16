"""Build the HA integration with unchanged, SDK-free financial source modules."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence
import zipfile


DOMAIN = "household_electricity_pricing"
CORE_FILES = (
    "__init__.py",
    "electricity_market/__init__.py",
    "electricity_market/prices.py",
    "electricity_pricing/__init__.py",
    "electricity_pricing/data.py",
    "electricity_pricing/engine.py",
    "household_consumption/__init__.py",
    "household_consumption/client.py",
    "home_assistant/__init__.py",
    "home_assistant/calculator.py",
    "home_assistant/pricing.py",
)
COMPONENT_FILES = (
    "__init__.py",
    "manifest.json",
    "const.py",
    "config_flow.py",
    "coordinator.py",
    "controls.py",
    "dhw_optimizer.py",
    "floor_heating_shadow.py",
    "sensor.py",
    "select.py",
    "button.py",
    "price_api.py",
    "calculation_api.py",
    "services.yaml",
    "strings.json",
    "translations/en.json",
)


def build_bundle(destination: Path) -> dict[str, str]:
    package_root = Path(__file__).resolve().parents[1]
    repository = package_root.parent
    component_root = Path(__file__).parent / "custom_components" / DOMAIN
    prefix = "custom_components/" + DOMAIN + "/"
    entries: dict[str, bytes] = {}
    sources: dict[str, str] = {}

    def include(source: Path, target: str) -> None:
        if source.is_symlink() or not source.resolve().is_relative_to(repository):
            raise ValueError("Bundle sources must be regular files inside the source tree.")
        content = source.read_bytes()
        entries[prefix + target] = content
        sources[target] = hashlib.sha256(content).hexdigest()

    for name in COMPONENT_FILES:
        include(component_root / name, name)
    for name in CORE_FILES:
        include(package_root / name, "_vendor/home_automation/" + name)
    include(repository / "LICENSE", "LICENSE")
    entries[prefix + "_vendor/__init__.py"] = b'"""Bundled SDK-free source modules."""\n'
    entries[prefix + "source_manifest.json"] = (
        json.dumps({"schema_version": 1, "sha256": sources}, indent=2, sort_keys=True) + "\n"
    ).encode()
    with destination.open("xb") as handle:
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
    return sources


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New ZIP file; existing files are not overwritten.")
    args = parser.parse_args(argv)
    sources = build_bundle(args.output)
    print("Built {} from {} checked source files.".format(args.output, len(sources)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
