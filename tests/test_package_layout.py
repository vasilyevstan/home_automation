import importlib
import importlib.metadata
from pathlib import Path
import subprocess
import sys
import unittest


MODULES = {
    "nordpool_ee": "electricity_market.prices",
    "mcp_server": "electricity_market.server",
    "wattcast_forecast": "electricity_market.forecast",
    "elering_consumption": "household_consumption.client",
    "elering_consumption_mcp": "household_consumption.server",
    "electricity_pricing": "electricity_pricing.engine",
    "electricity_pricing_mcp": "electricity_pricing.server",
    "sensibo": "sensibo.client",
    "sensibo_mcp": "sensibo.server",
    "tapo_plugs": "tapo.client",
    "tapo_mcp": "tapo.server",
    "starlink": "starlink.client",
    "starlink_mcp": "starlink.server",
    "sonos": "sonos.client",
    "sonos_mcp": "sonos.server",
    "govee": "govee.client",
    "govee_mcp": "govee.server",
    "terrace_heater": "terrace_heater.client",
    "terrace_heater_mcp": "terrace_heater.server",
    "terrace_heater_setup": "terrace_heater.setup",
}

COMMANDS = {
    "nordpool-ee": "electricity_market.prices",
    "nordpool-ee-mcp": "electricity_market.server",
    "elering-kodala-consumption-mcp": "household_consumption.server",
    "electrisity-price-mcp": "electricity_pricing.server",
    "sensibo-mcp": "sensibo.server",
    "tp-tapo-mcp": "tapo.server",
    "starlink-mcp": "starlink.server",
    "sonos-mcp": "sonos.server",
    "govee-mcp": "govee.server",
    "terrace-heater-mcp": "terrace_heater.server",
    "terrace-heater-setup": "terrace_heater.setup",
}


class PackageLayoutTests(unittest.TestCase):
    def test_legacy_imports_share_implementation_and_state(self):
        for legacy, target in MODULES.items():
            with self.subTest(module=legacy):
                implementation = importlib.import_module("home_automation." + target)
                self.assertIs(importlib.import_module(legacy), implementation)

    def test_existing_console_commands_target_packaged_implementations(self):
        distribution = importlib.metadata.distribution("estonia-nordpool-prices")
        entries = {
            entry.name: entry
            for entry in distribution.entry_points
            if entry.group == "console_scripts"
        }
        self.assertEqual(set(entries), set(COMMANDS))
        for name, target in COMMANDS.items():
            with self.subTest(command=name):
                self.assertEqual(entries[name].value, "home_automation." + target + ":main")
                self.assertIs(
                    entries[name].load(),
                    importlib.import_module("home_automation." + target).main,
                )

    def test_each_mcp_has_its_own_implementation_and_documentation(self):
        for folder in sorted({target.split(".")[0] for target in MODULES.values()}):
            with self.subTest(folder=folder):
                package = importlib.import_module("home_automation." + folder)
                directory = Path(package.__file__).parent
                for name in ("server.py", "README.md", "AGENTS.md"):
                    self.assertTrue((directory / name).is_file(), str(directory / name))

    def test_package_import_does_not_load_any_mcp_or_device_sdk(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, home_automation; "
                "assert not {'mcp', 'kasa', 'soco', 'grpc', 'tinytuya', 'tuya_sharing'} "
                "& sys.modules.keys()",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_legacy_market_module_still_runs_as_a_cli(self):
        result = subprocess.run(
            [sys.executable, "-m", "nordpool_ee", "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
