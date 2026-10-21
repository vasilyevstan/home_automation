from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from home_automation.electricity_market.prices import MARKET_INTERVAL, TALLINN, UTC
from home_automation.electricity_pricing.engine import PricingError, parse_profile
from home_automation.home_assistant.bundle import CORE_FILES, build_bundle
from home_automation.home_assistant.pricing import (
    build_snapshot,
    complete_market_day,
    market_dates,
    market_day_bounds,
    parse_nordpool_prices,
    price_window,
    profile_path,
)
from pricing_test_support import component, example, readings


def tariff():
    value = example()
    energy = component("energy", "spot", "0.01")
    network = component("network", "banded", section="network")
    network["rules"][0].pop("rate")
    network["rules"][0]["band_rates"] = {"day": "0.02", "night": "0.015"}
    exported = component("export", "spot", "-0.03", "export", "0")
    exported["direction"] = "export"
    exported["rules"][0]["tax_treatment"] = "outside_vat"
    balancing = component("export_fee", "unit", "0.005", "export")
    balancing["direction"] = "export"
    monthly = component("monthly", "monthly", "100")
    value["components"] = [energy, network, exported, balancing, monthly]
    return value


def response(rows, price="100"):
    return {"EE": [
        {"start": row.start.isoformat(), "end": row.end.isoformat(), "price": price}
        for row in rows
    ]}


class HomeAssistantPriceTests(unittest.TestCase):
    def setUp(self):
        self.profile = parse_profile(tariff())
        self.rows = readings("2026-08-03", "2026-08-04")
        self.prices = parse_nordpool_prices([response(self.rows)])
        self.now = datetime(2026, 8, 3, 10, 7, tzinfo=TALLINN)

    def test_actual_import_and_export_costs_reuse_all_components(self):
        result = build_snapshot(self.profile, self.prices, self.now)
        self.assertEqual(Decimal(result.current.spot_eur_kwh), Decimal("0.1"))
        self.assertEqual(Decimal(result.current.import_eur_kwh), Decimal("0.156"))
        self.assertEqual(Decimal(result.current.export_earnings_eur_kwh), Decimal("0.064"))
        self.assertEqual(result.current.start, "2026-08-03T10:00:00+03:00")
        self.assertEqual(result.current.end, "2026-08-03T10:15:00+03:00")
        self.assertTrue(result.today.complete)
        self.assertTrue(result.tomorrow.complete)
        self.assertEqual(result.attributes()["fixed_fees"], "excluded")
        self.assertEqual(result.profile_sha256, self.profile.sha256)

    def test_negative_spot_and_export_earnings_remain_negative(self):
        prices = parse_nordpool_prices([response(self.rows, "-100")])
        result = build_snapshot(self.profile, prices, self.now)
        self.assertEqual(Decimal(result.current.spot_eur_kwh), Decimal("-0.1"))
        self.assertEqual(Decimal(result.current.import_eur_kwh), Decimal("-0.084"))
        self.assertEqual(Decimal(result.current.export_earnings_eur_kwh), Decimal("-0.136"))

    def test_positive_spot_can_still_mean_loss_making_export(self):
        prices = parse_nordpool_prices([response(self.rows, "10")])
        result = build_snapshot(self.profile, prices, self.now)
        self.assertEqual(Decimal(result.current.export_earnings_eur_kwh), Decimal("-0.026"))

    def test_tariff_clock_is_not_nord_pool_peak_clock(self):
        before = build_snapshot(
            self.profile, self.prices, datetime(2026, 8, 3, 6, 59, tzinfo=TALLINN)
        )
        after = build_snapshot(
            self.profile, self.prices, datetime(2026, 8, 3, 7, 0, tzinfo=TALLINN)
        )
        self.assertEqual(Decimal(before.current.import_eur_kwh), Decimal("0.150"))
        self.assertEqual(Decimal(after.current.import_eur_kwh), Decimal("0.156"))

    def test_unpublished_next_day_is_not_zero_or_an_entirely_failed_current_quote(self):
        prices = parse_nordpool_prices([response(readings("2026-08-03"))])
        result = build_snapshot(self.profile, prices, self.now)
        self.assertFalse(result.tomorrow.complete)
        self.assertEqual(result.tomorrow.prices, ())
        self.assertEqual(result.tomorrow.missing_market_intervals, 96)
        self.assertIn("incomplete", result.tomorrow.unavailable_reason)
        self.assertEqual(Decimal(result.current.import_eur_kwh), Decimal("0.156"))

    def test_partial_windows_report_the_actual_missing_quarters(self):
        prices = dict(self.prices)
        del prices[self.rows[0].start]
        result = build_snapshot(self.profile, prices, self.now)
        self.assertFalse(result.today.complete)
        self.assertEqual(result.today.missing_market_intervals, 1)
        self.assertEqual(len(result.today.prices), 95)

    def test_missing_current_quarter_is_an_error(self):
        prices = dict(self.prices)
        del prices[self.now.replace(minute=0).astimezone(UTC)]
        with self.assertRaisesRegex(PricingError, "current quarter"):
            build_snapshot(self.profile, prices, self.now)

    def test_snapshot_expires_at_its_actual_quarter_boundary(self):
        result = build_snapshot(self.profile, self.prices, self.now)
        self.assertTrue(result.contains(self.now))
        self.assertFalse(result.contains(result.valid_until))
        self.assertFalse(result.contains(result.valid_from - timedelta(microseconds=1)))
        with self.assertRaises(PricingError):
            result.contains(datetime(2026, 8, 3, 10))
        with self.assertRaises(PricingError):
            build_snapshot(self.profile, self.prices, datetime(2026, 8, 3, 10))

    def test_dst_windows_and_repeated_quarters_are_preserved(self):
        for day, count in (("2026-03-29", 92), ("2026-10-25", 100)):
            with self.subTest(day=day):
                rows = readings(day)
                prices = parse_nordpool_prices([response(rows)])
                window = price_window(self.profile, prices, date.fromisoformat(day))
                self.assertTrue(window.complete)
                self.assertEqual(window.expected_intervals, count)
                self.assertEqual(len(window.prices), count)
                self.assertEqual(len({point.start for point in window.prices}), count)

    def test_tallinn_days_include_the_previous_cet_market_day(self):
        self.assertEqual(
            market_dates(date(2026, 8, 3)),
            (date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 4)),
        )
        first, last = market_day_bounds(date(2026, 3, 29))
        self.assertEqual(int((last - first) / MARKET_INTERVAL), 92)
        first, last = market_day_bounds(date(2026, 10, 25))
        self.assertEqual(int((last - first) / MARKET_INTERVAL), 100)
        points = {
            first + index * MARKET_INTERVAL: Decimal(1)
            for index in range(100)
        }
        self.assertTrue(complete_market_day(points, date(2026, 10, 25)))
        points.pop(first)
        self.assertFalse(complete_market_day(points, date(2026, 10, 25)))

    def test_profile_expiry_does_not_extrapolate_into_tomorrow(self):
        rows = readings("2026-12-31", "2027-01-01")
        result = build_snapshot(
            self.profile, parse_nordpool_prices([response(rows)]),
            datetime(2026, 12, 31, 12, tzinfo=TALLINN),
        )
        self.assertFalse(result.tomorrow.complete)
        self.assertEqual(result.tomorrow.missing_market_intervals, 0)
        self.assertEqual(result.tomorrow.prices, ())
        self.assertIn("profile", result.tomorrow.unavailable_reason)
        with self.assertRaisesRegex(PricingError, "coverage"):
            build_snapshot(
                self.profile, parse_nordpool_prices([response(rows)]),
                datetime(2027, 1, 1, 12, tzinfo=TALLINN),
            )

    def test_invoice_derived_qualification_survives(self):
        value = tariff()
        value["sources"][0]["basis"] = "invoice_derived"
        profile = parse_profile(value)
        result = build_snapshot(profile, self.prices, self.now)
        self.assertTrue(result.current.invoice_derived)
        self.assertTrue(result.attributes()["invoice_derived_rules_used"])
        self.assertEqual(result.profile_version, profile.version)

    def test_price_window_is_json_serializable_and_has_explicit_units(self):
        data = build_snapshot(self.profile, self.prices, self.now).today.as_dict()
        json.dumps(data, allow_nan=False)
        self.assertEqual(data["unit"], "EUR/kWh")
        self.assertEqual(data["spot_vat_basis"], "excluding_vat")
        self.assertEqual(data["export_basis"], "net_earnings_after_export_charges")
        self.assertEqual(data["fixed_fees"], "excluded")

    def test_invalid_prices_are_not_replaced_with_zero(self):
        for value in (None, True, "unknown", "unavailable", "NaN", float("inf")):
            with self.subTest(value=value), self.assertRaises(PricingError):
                parse_nordpool_prices([response(self.rows[:1], value)])
        self.assertEqual(
            list(parse_nordpool_prices([response(self.rows[:1], 0)]).values()),
            [Decimal(0)],
        )

    def test_invalid_shape_duplicates_naive_times_and_hourly_inputs_fail(self):
        valid = response(self.rows[:1])
        invalid = [
            None, {}, {"FI": []}, {"EE": None},
            {"EE": [dict(valid["EE"][0], forecast=True)]},
            {"EE": [valid["EE"][0], valid["EE"][0]]},
            {"EE": [dict(valid["EE"][0], start="2026-08-03T00:00:00")]},
            {"EE": [dict(valid["EE"][0], end=(self.rows[0].start + timedelta(hours=1)).isoformat())]},
            {"EE": [dict(valid["EE"][0], start=(self.rows[0].start + timedelta(minutes=1)).isoformat())]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(PricingError):
                parse_nordpool_prices([value])
        with self.assertRaisesRegex(PricingError, "Duplicate"):
            parse_nordpool_prices([valid, valid])

    def test_profile_paths_are_private_config_relative_and_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(profile_path(root, "private/profile.json"), root / "private/profile.json")
            for value in ("", "/tmp/profile.json", "../profile.json", "private/token.txt"):
                with self.subTest(value=value), self.assertRaises(PricingError):
                    profile_path(root, value)
            (root / "external").symlink_to(root.parent, target_is_directory=True)
            with self.assertRaises(PricingError):
                profile_path(root, "external/profile.json")


class HomeAssistantBundleTests(unittest.TestCase):
    def test_native_action_forms_and_english_copy_stay_in_sync(self):
        component = (
            Path(__file__).resolve().parents[1]
            / "home_automation/home_assistant/custom_components/household_electricity_pricing"
        )
        strings = json.loads((component / "strings.json").read_text())
        self.assertEqual(strings, json.loads((component / "translations/en.json").read_text()))
        self.assertEqual(
            set(strings["services"]),
            {
                "get_prices",
                "get_price_forecast",
                "quote_electricity_interval",
                "calculate_electricity_period",
                "estimate_electricity_scenario",
            },
        )
        self.assertEqual(set(strings["config"]), {"step", "error", "abort"})
        self.assertEqual(set(strings["options"]), {"step", "error"})
        self.assertIn("dhw_preheat_target", strings["options"]["step"]["init"]["data"])
        self.assertIn(
            "extreme_price_climate_entities",
            strings["options"]["step"]["init"]["data"],
        )
        forms = (component / "services.yaml").read_text()
        for action in strings["services"]:
            self.assertIn(action + ":", forms)
        for step in ("user", "reconfigure"):
            self.assertIn("elering_credentials_path", strings["config"]["step"][step]["data"])

    def test_bundle_is_reproducible_and_contains_unchanged_core_not_sdk_or_private_data(self):
        package = Path(__file__).resolve().parents[1] / "home_automation"
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first.zip", Path(directory) / "second.zip"
            hashes = build_bundle(first)
            self.assertEqual(hashes, build_bundle(second))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with self.assertRaises(FileExistsError):
                build_bundle(first)
            prefix = "custom_components/household_electricity_pricing/"
            with zipfile.ZipFile(first) as archive:
                for name in CORE_FILES:
                    self.assertEqual(
                        archive.read(prefix + "_vendor/home_automation/" + name),
                        (package / name).read_bytes(),
                    )
                manifest = json.loads(archive.read(prefix + "manifest.json"))
                self.assertEqual(manifest["requirements"], [])
                self.assertEqual(manifest["dependencies"], ["nordpool"])
                self.assertEqual(
                    manifest["after_dependencies"], ["huawei_solar"]
                )
                self.assertEqual(manifest["domain"], "household_electricity_pricing")
                self.assertEqual(manifest["version"], "0.13.4")
                names = archive.namelist()
                self.assertTrue(all(name.startswith(prefix) for name in names))
                self.assertFalse(any(
                    name.endswith(("profile.json", ".env", "server.py", "pyproject.toml"))
                    for name in names
                ))
                self.assertIn(prefix + "LICENSE", names)
                self.assertIn(prefix + "price_api.py", names)
                self.assertIn(prefix + "dhw_optimizer.py", names)
                self.assertIn(prefix + "floor_heating_shadow.py", names)
                self.assertIn(prefix + "extreme_price_protection.py", names)
                self.assertIn(prefix + "solar_curtailment.py", names)
                self.assertIn(prefix + "price_forecast.py", names)
                self.assertIn(
                    prefix
                    + "_vendor/home_automation/electricity_market/forecast.py",
                    names,
                )
                self.assertIn(prefix + "select.py", names)
                self.assertIn(prefix + "button.py", names)
                self.assertNotIn(prefix + "llm.py", names)

    def test_generated_calculator_imports_and_runs_without_ha_or_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "component.zip"
            build_bundle(artifact)
            with zipfile.ZipFile(artifact) as archive:
                archive.extractall(root)
            vendor = root / "custom_components/household_electricity_pricing/_vendor"
            code = (
                "import sys,json,importlib.abc\n"
                "class NoFramework(importlib.abc.MetaPathFinder):\n"
                " def find_spec(self,fullname,path=None,target=None):\n"
                "  if fullname.split('.')[0] in ('mcp','homeassistant'):\n"
                "   raise AssertionError('Unexpected framework import: '+fullname)\n"
                "sys.meta_path.insert(0,NoFramework())\n"
                "sys.path.insert(0,sys.argv[1])\n"
                "from home_automation.electricity_pricing.engine import parse_profile\n"
                "from home_automation.home_assistant.pricing import build_snapshot,parse_nordpool_prices\n"
                "from home_automation.home_assistant.calculator import ElectricityCalculator\n"
                "from datetime import datetime,timedelta\n"
                "data=json.load(sys.stdin)\n"
                "result=build_snapshot(parse_profile(data['profile']),parse_nordpool_prices([data['prices']]),datetime.fromisoformat(data['now']))\n"
                "assert result.current.import_eur_kwh == data['expected_import']\n"
                "calculator=ElectricityCalculator(lambda:parse_profile(data['profile']),price_provider=lambda first,last:parse_nordpool_prices([data['prices']]))\n"
                "start=datetime.fromisoformat(data['now'])\n"
                "quote=calculator.quote(start.isoformat(),(start+timedelta(minutes=15)).isoformat(),'1','0',quantity_basis='billable')\n"
                "assert quote['amounts']['including_vat_eur'] == data['expected_import']\n"
                "assert 'mcp' not in sys.modules and 'homeassistant' not in sys.modules\n"
                "print('SDK-free calculator works')\n"
            )
            source_profile = parse_profile(tariff())
            source_prices = response(readings("2026-08-03", "2026-08-04"))
            now = datetime(2026, 8, 3, 10, tzinfo=TALLINN)
            expected = build_snapshot(source_profile, parse_nordpool_prices([source_prices]), now)
            result = subprocess.run(
                [sys.executable, "-I", "-c", code, str(vendor)],
                input=json.dumps({
                    "profile": tariff(), "prices": source_prices,
                    "now": now.isoformat(), "expected_import": expected.current.import_eur_kwh,
                }),
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SDK-free calculator works", result.stdout)


if __name__ == "__main__":
    unittest.main()
