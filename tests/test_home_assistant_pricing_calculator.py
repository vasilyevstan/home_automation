import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from home_automation.electricity_market.prices import MARKET_INTERVAL, TALLINN, UTC
from home_automation.electricity_pricing.engine import (
    PricingError, price_month_to_date, price_period, price_scenario,
)
from home_automation.home_assistant.calculator import (
    ElectricityCalculator, load_credentials_file, resolve_period,
)
from home_automation.household_consumption.client import ConsumptionError, ConsumptionReport, Reading
from pricing_test_support import component, prices_for, profile_with, readings, rule


NOW = datetime(2026, 8, 3, 7, 7, tzinfo=UTC)


def supplied(rows):
    return [
        {
            "start": row.start.isoformat(), "end": row.end.isoformat(),
            "consumption_kwh": str(row.consumption_kwh) if row.consumption_kwh is not None else None,
            "export_kwh": str(row.export_kwh) if row.export_kwh is not None else None,
        }
        for row in rows
    ]


def estimated_prices(rows, value="100"):
    return [
        {"start": row.start.isoformat(), "end": row.end.isoformat(), "price_eur_mwh": value}
        for row in rows
    ]


class MonthToDateEngineTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile_with(
            component(rate="0.01"),
            component("monthly", "monthly", "10.125"),
        )
        self.rows = readings("2026-08-01", "2026-08-02")

    def test_full_monthly_fees_and_invoice_rounding_are_explicitly_provisional(self):
        result = price_month_to_date(
            self.profile, self.rows, prices_for(self.rows), "2026-08-01", "2026-08-02",
            include_daily=True,
        )
        self.assertEqual(result.mode, "month_to_date")
        self.assertEqual(result.fixed_fees, "full_current_month")
        self.assertEqual(result.amounts.excluding_vat_eur, "12.05")
        self.assertEqual(result.amounts.vat_eur, "2.41")
        self.assertEqual(result.amounts.including_vat_eur, "14.46")
        self.assertFalse(result.monthly_periods[0].complete_calendar_month)
        fee = next(line for line in result.lines if line.component_id == "monthly")
        self.assertEqual(fee.unrounded_net_eur, "10.125")
        self.assertEqual(fee.net_eur, "10.13")
        daily = sum(Decimal(item.amounts.including_vat_eur) for item in result.daily_allocated_periods)
        self.assertNotEqual(daily, Decimal(result.amounts.including_vat_eur))
        self.assertTrue(any("Provisional" in warning for warning in result.warnings))

    def test_existing_report_and_complete_month_requirements_are_unchanged(self):
        result = price_period(self.profile, self.rows, prices_for(self.rows), "2026-08-01", "2026-08-02")
        self.assertEqual(result.fixed_fees, "calendar_day_allocated")
        self.assertLess(Decimal(result.amounts.including_vat_eur), Decimal("14.46"))
        with self.assertRaisesRegex(PricingError, "complete calendar months"):
            price_period(self.profile, self.rows, prices_for(self.rows), "2026-08-01", "2026-08-02", mode="monthly")

    def test_incomplete_or_cross_month_data_cannot_become_a_provisional_bill(self):
        for first, last, rows in (
            ("2026-08-01", "2026-08-02", self.rows[:-1]),
            ("2026-08-02", "2026-08-02", readings("2026-08-02")),
            ("2026-08-01", "2026-09-01", self.rows),
        ):
            with self.subTest(first=first, last=last), self.assertRaises(PricingError):
                price_month_to_date(self.profile, rows, prices_for(rows), first, last)

    def test_full_fee_checks_known_changes_even_after_the_metering_cutoff(self):
        fee = component("monthly", "monthly")
        fee["rules"] = [rule("10", end="2026-08-15"), rule("12", start="2026-08-15")]
        profile = profile_with(component(), fee)
        with self.assertRaisesRegex(PricingError, "changes inside"):
            price_month_to_date(profile, self.rows, prices_for(self.rows), "2026-08-01", "2026-08-02")


class NativeCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile_with(component(kind="spot", rate="0"), component("monthly", "monthly", "10"))
        self.profile_provider = Mock(return_value=self.profile)
        self.client = Mock()
        self.market = Mock(side_effect=lambda first, last: prices_for(readings(first.isoformat(), last.isoformat())))
        self.calculator = ElectricityCalculator(
            self.profile_provider, self.client, self.market, now_provider=lambda: NOW,
        )

    def report(self, rows):
        first, last, start, end = resolve_period(NOW, "month_to_date")
        from home_automation.household_consumption.client import DateWindow

        return ConsumptionReport(
            DateWindow(first, last, start, end), "SYNTHETIC-POINT", "15_minutes",
            tuple(Reading(row.start, row.consumption_kwh, row.export_kwh) for row in rows), NOW,
        )

    def test_presets_and_lengths_use_tallinn_calendar_dates(self):
        self.assertEqual(
            [item.isoformat() for item in resolve_period(NOW, "previous_month")[:2]],
            ["2026-07-01", "2026-07-31"],
        )
        self.assertEqual(
            [item.isoformat() for item in resolve_period(NOW, "last_12_months")[:2]],
            ["2025-08-01", "2026-07-31"],
        )
        self.assertEqual(
            [item.isoformat() for item in resolve_period(NOW, days=7)[:2]],
            ["2026-07-27", "2026-08-02"],
        )
        self.assertEqual(
            [item.isoformat() for item in resolve_period(NOW, start_date="2026-09-01", days=30)[:2]],
            ["2026-09-01", "2026-09-30"],
        )

    def test_length_and_preset_conflicts_are_errors(self):
        for arguments in (
            {"period": "previous_month", "days": 30}, {"days": True}, {"days": 0},
            {"days": 2.5}, {"days": 2, "end_date": "2026-08-02"},
            {"end_date": "2026-08-02"}, {"period": "unknown"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(PricingError):
                resolve_period(NOW, **arguments)
        with self.assertRaisesRegex(PricingError, "No completed"):
            resolve_period(datetime(2026, 8, 1, tzinfo=TALLINN), "month_to_date")

    def test_month_to_date_reports_real_latency_without_zero_filling(self):
        rows = readings("2026-08-01", "2026-08-02")
        rows = [row if index < 96 else replace(row, consumption_kwh=None, export_kwh=None)
                for index, row in enumerate(rows)]
        self.client.get_consumption.return_value = self.report(rows)
        result = self.calculator.period(period="month_to_date")
        self.assertEqual(result["calculation_kind"], "provisional_bill")
        self.assertEqual(result["fixed_fees"], "full_current_month")
        self.assertEqual(result["interval_count"], 96)
        self.assertEqual(result["coverage"]["pending_completed_days"], 1)
        self.assertFalse(result["coverage"]["current_day_included"])
        self.assertEqual(result["end"], "2026-08-02T00:00:00+03:00")
        self.assertEqual(result["price_basis"], "published")
        self.profile_provider.assert_called_once()

    def test_holes_inside_a_measured_day_remain_errors(self):
        rows = readings("2026-08-01", "2026-08-02")
        rows[20] = replace(rows[20], consumption_kwh=None)
        self.client.get_consumption.return_value = self.report(rows)
        with self.assertRaisesRegex(PricingError, "missing/null"):
            self.calculator.period(period="month_to_date")
        self.market.assert_not_called()

    def test_previous_month_uses_complete_month_invoice_arithmetic(self):
        rows = readings("2026-07-01", "2026-07-31")
        result = self.calculator.period(period="previous_month", intervals=supplied(rows))
        expected = price_period(self.profile, rows, prices_for(rows), "2026-07-01", "2026-07-31", mode="monthly")
        self.assertEqual(result["amounts"]["including_vat_eur"], expected.amounts.including_vat_eur)
        self.assertEqual(result["mode"], "monthly")
        self.assertEqual(result["fixed_fees"], "once_per_calendar_month")
        self.assertEqual(result["input_source"], "caller_supplied")
        self.client.get_consumption.assert_not_called()

    def test_missing_historical_tariffs_fail_before_household_or_price_requests(self):
        with self.assertRaisesRegex(PricingError, "coverage"):
            self.calculator.period(period="last_12_months")
        self.client.get_consumption.assert_not_called()
        self.market.assert_not_called()

    def test_dst_days_keep_actual_quarter_counts(self):
        for day, quarters in (("2026-03-29", 92), ("2026-10-25", 100)):
            rows = readings(day)
            result = self.calculator.period(start_date=day, intervals=supplied(rows))
            self.assertEqual(result["interval_count"], quarters)
            self.assertEqual(result["billable_import_kwh"], str(quarters))

    def test_future_prices_only_enter_explicit_supplied_estimates(self):
        rows = readings("2026-09-01")
        result = self.calculator.period(
            start_date="2026-09-01", intervals=supplied(rows),
            price_basis="estimate", price_intervals=estimated_prices(rows, "-30"),
        )
        self.assertEqual(result["calculation_kind"], "estimate")
        self.assertEqual(result["price_source"], "caller_supplied_estimated_prices")
        self.assertIsNone(result["prices_retrieved_at"])
        self.assertLess(Decimal(result["amounts"]["including_vat_eur"]), 0)
        self.assertTrue(any("cannot finalize" in warning for warning in result["warnings"]))
        self.market.assert_not_called()
        self.client.get_consumption.assert_not_called()

    def test_estimates_cannot_impersonate_bills_or_get_automatic_metering(self):
        rows = readings("2026-09-01")
        for arguments in (
            {"period": "previous_month", "price_basis": "estimate"},
            {"start_date": "2026-09-01", "price_basis": "estimate"},
            {"start_date": "2026-09-01", "intervals": supplied(rows), "price_intervals": estimated_prices(rows)},
            {"start_date": "2026-09-01", "price_basis": "unknown"},
            {"start_date": "2026-09-01"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(PricingError):
                self.calculator.period(**arguments)
        self.client.get_consumption.assert_not_called()
        self.market.assert_not_called()

    def test_estimated_price_coverage_and_units_are_checked(self):
        row = readings("2026-08-03")[0]
        valid = estimated_prices([row])
        for prices in ([], [*valid, *valid], [dict(valid[0], price_eur_mwh=True)], 1):
            with self.subTest(prices=prices), self.assertRaises(PricingError):
                self.calculator.quote(
                    row.start.isoformat(), row.end.isoformat(), "1", "0",
                    price_basis="estimate", price_intervals=prices,
                )
        result = self.calculator.quote(
            row.start.isoformat(), row.end.isoformat(), "1", "0",
            price_basis="estimate", price_intervals=valid,
        )
        self.assertEqual(result["amounts"]["including_vat_eur"], "0.12")
        self.assertEqual(result["fixed_fees"], "excluded")

    def test_scenarios_reuse_financial_engine_and_mark_nested_estimates(self):
        rows = readings("2026-08-03")[:4]
        before = [replace(row, consumption_kwh=Decimal("0")) for row in rows]
        result = self.calculator.scenario(
            mode="comparison", baseline_intervals=supplied(before), scenario_intervals=supplied(rows),
            price_basis="estimate", price_intervals=estimated_prices(rows),
        )
        expected = price_scenario(self.profile, rows, prices_for(rows), "raw", before)
        self.assertEqual(result["amounts"]["including_vat_eur"], expected.amounts.including_vat_eur)
        self.assertEqual(result["scenario"]["price_source"], "caller_supplied_estimated_prices")
        self.assertEqual(result["baseline"]["price_source"], "caller_supplied_estimated_prices")
        self.assertEqual(result["fixed_fees"], "excluded")
        load = self.calculator.scenario(start="2026-08-03T10:07:30+03:00", duration_minutes="60", power_kw="2")
        self.assertEqual(load["energy_kwh"], "2")
        self.assertEqual(len(load["energy_allocation"]), 5)
        self.assertEqual(load["start"], "2026-08-03T10:07:30+03:00")

    def test_actions_do_not_accept_unrestricted_operations_or_configuration(self):
        for action, arguments in (
            ("homeassistant.turn_on", {}), ("calculate_electricity_period", {"profile_path": "/tmp/profile.json"}),
            ("quote_electricity_interval", {"entity_id": "switch.example"}),
        ):
            with self.subTest(action=action), self.assertRaises(PricingError):
                self.calculator.invoke(action, arguments)


class EleringFileTests(unittest.TestCase):
    def test_private_file_loads_without_exposing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "credentials.json"
            path.write_text(json.dumps({"client_id": "synthetic-id", "client_secret": "not-a-real-secret"}))
            path.chmod(0o600)
            credentials = load_credentials_file(root, "credentials.json")
            self.assertEqual(credentials.client_id, "synthetic-id")
            self.assertNotIn("not-a-real-secret", repr(credentials))
            path.chmod(0o644)
            with self.assertRaisesRegex(ConsumptionError, "group"):
                load_credentials_file(root, "credentials.json")

    def test_missing_partial_duplicate_and_unconfined_files_are_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "credentials.json"
            with self.assertRaises(ConsumptionError):
                load_credentials_file(root, "credentials.json")
            for value in (
                "{}", '{"client_id":"synthetic-id"}',
                '{"client_id":"synthetic-id","client_secret":"x","client_secret":"y"}',
                '{"client_id":"synthetic-id","client_secret":false}', "{",
            ):
                path.write_text(value)
                path.chmod(0o600)
                with self.subTest(value=value), self.assertRaises(ConsumptionError):
                    load_credentials_file(root, "credentials.json")
            for name in ("../credentials.json", "/tmp/credentials.json", "credentials.yaml"):
                with self.subTest(name=name), self.assertRaises(PricingError):
                    load_credentials_file(root, name)


if __name__ == "__main__":
    unittest.main()
