"""Seven-day household price forecast tests using real HA APIs."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import test_component as base


setUpModule = base.setUpModule
tearDownModule = base.tearDownModule


class PriceForecastTests(base.HAComponentTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.module = importlib.import_module(base.COMPONENT + ".price_forecast")
        self.forecast_core = importlib.import_module(
            base.COMPONENT + "._vendor.home_automation.electricity_market.forecast"
        )
        self.sensor_module = importlib.import_module(base.COMPONENT + ".sensor")

    def report(
        self,
        *,
        issued_delta: timedelta = timedelta(minutes=20),
        p10: Decimal = Decimal("50"),
        p50: Decimal = Decimal("100"),
        p90: Decimal = Decimal("150"),
    ):
        first_day = self.now.astimezone(self.module.TALLINN).date() + timedelta(
            days=1
        )
        start, _ = self.pricing.market_bounds(first_day)
        _, end = self.pricing.market_bounds(first_day + timedelta(days=6))
        hours = []
        current = start
        while current < end:
            horizon = min(
                7,
                (
                    current.astimezone(self.module.TALLINN).date() - first_day
                ).days + 1,
            )
            hours.append(
                self.forecast_core.PredictedHour(
                    current,
                    p10,
                    p50,
                    p90,
                    horizon,
                )
            )
            current += timedelta(hours=1)
        return self.forecast_core.ForecastReport(
            issued_at=self.now - issued_delta,
            retrieved_at=self.now,
            model_trained_at=self.now - timedelta(days=1),
            hours=tuple(hours),
            attribution=(
                "Wattcast; prices from Elering; weather from Open-Meteo.com"
            ),
        )

    def accuracy(self):
        horizons = tuple(
            self.forecast_core.HorizonAccuracy(
                horizon,
                self.forecast_core.BacktestScore(
                    Decimal("35"),
                    Decimal("40"),
                    Decimal("45"),
                    1000,
                    self.now.date() - timedelta(days=180),
                ),
                self.forecast_core.LiveScore(
                    Decimal("30") + horizon,
                    Decimal("78"),
                    100,
                ),
            )
            for horizon in range(1, 8)
        )
        return self.forecast_core.AccuracyReport(
            retrieved_at=self.now,
            model_trained_at=self.now - timedelta(days=1),
            live_days=14,
            nominal_coverage_percent=Decimal("80"),
            band_method="quantile",
            horizons=horizons,
        )

    async def forecast_coordinator(self):
        await self.refresh()
        coordinator = self.module.HouseholdPriceForecastCoordinator(
            self.hass,
            self.pricing_entry,
            self.coordinator,
        )
        await coordinator.async_load()
        return coordinator

    async def test_current_forecast_uses_all_in_prices_and_published_overlay(self):
        coordinator = await self.forecast_coordinator()
        report = self.report()
        accuracy = self.accuracy()
        with (
            patch.object(self.module, "fetch_forecast", return_value=report),
            patch.object(self.module, "fetch_accuracy", return_value=accuracy),
        ):
            data = await coordinator._async_update_data()
        response = data.response
        self.assertEqual(response["interval_minutes"], 15)
        self.assertEqual(response["expected_intervals"], 7 * 24 * 4)
        self.assertEqual(response["published_intervals"], 24 * 4)
        self.assertEqual(response["forecast_intervals"], 6 * 24 * 4)
        self.assertEqual(response["unavailable_intervals"], 0)
        self.assertTrue(response["complete"])
        self.assertEqual(
            response["price_basis"],
            "household_all_in_import_including_applicable_vat",
        )
        first_forecast = next(
            point for point in response["intervals"]
            if point["basis"] == "forecast"
        )
        start = datetime.fromisoformat(first_forecast["start"])
        profile = coordinator._profile()
        expected = self.pricing.forecast_import_eur_kwh(
            profile,
            Decimal("100"),
            start,
        )
        prediction = first_forecast["prediction"]
        self.assertEqual(prediction["p50_import_eur_kwh"], expected)
        self.assertLess(
            Decimal(prediction["p10_import_eur_kwh"]),
            Decimal(prediction["p50_import_eur_kwh"]),
        )
        self.assertLess(
            Decimal(prediction["p50_import_eur_kwh"]),
            Decimal(prediction["p90_import_eur_kwh"]),
        )
        self.assertEqual(len(response["daily_summaries"]), 7)
        self.assertEqual(len(coordinator.archive), 7 * 24)
        await coordinator.async_shutdown()

    async def test_forecast_archive_deduplicates_and_serves_horizon_history(self):
        coordinator = await self.forecast_coordinator()
        report = self.report()
        with (
            patch.object(self.module, "fetch_forecast", return_value=report),
            patch.object(
                self.module,
                "fetch_accuracy",
                side_effect=self.forecast_core.ForecastError("metrics offline"),
            ),
        ):
            first = await coordinator._async_update_data()
            second = await coordinator._async_update_data()
        self.assertEqual(len(coordinator.archive), 7 * 24)
        self.assertEqual(
            first.response["accuracy"]["status"], "unavailable"
        )
        self.assertEqual(
            second.response["archive"]["record_count"], 7 * 24
        )
        future = await coordinator.async_response("archive", 8, 1, 1)
        self.assertEqual(future["include_future_days"], 1)
        self.assertEqual(future["stored_hours"], 24)
        self.assertEqual(len(future["intervals"]), 24 * 4)
        self.now = datetime.fromisoformat(first.response["window_end"]) + timedelta(
            hours=1
        )
        archive = await coordinator.async_response("archive", 8, 1)
        self.assertEqual(archive["stored_hours"], 24)
        self.assertEqual(len(archive["intervals"]), 24 * 4)
        self.assertTrue(
            all(
                point["horizon_days"] == 1
                for point in archive["intervals"]
            )
        )
        await coordinator.async_shutdown()

    async def test_sensor_summary_excludes_full_interval_payload(self):
        coordinator = await self.forecast_coordinator()
        with (
            patch.object(
                self.module,
                "fetch_forecast",
                return_value=self.report(),
            ),
            patch.object(
                self.module,
                "fetch_accuracy",
                return_value=self.accuracy(),
            ),
        ):
            data = await coordinator._async_update_data()
        coordinator.async_set_updated_data(data)
        sensor = self.sensor_module.PriceForecastSensor(coordinator, "pricing")
        self.assertIsNotNone(sensor.native_value)
        self.assertEqual(
            sensor.extra_state_attributes["forecast_intervals"],
            6 * 24 * 4,
        )
        self.assertNotIn("intervals", sensor.extra_state_attributes)
        await coordinator.async_shutdown()

    async def test_stale_forecast_remains_explicit(self):
        coordinator = await self.forecast_coordinator()
        with (
            patch.object(
                self.module,
                "fetch_forecast",
                return_value=self.report(issued_delta=timedelta(hours=4)),
            ),
            patch.object(
                self.module,
                "fetch_accuracy",
                return_value=self.accuracy(),
            ),
        ):
            data = await coordinator._async_update_data()
        self.assertTrue(data.response["stale"])
        self.assertTrue(
            any("stale" in warning.lower() for warning in data.response["warnings"])
        )
        await coordinator.async_shutdown()

    async def test_negative_forecast_prices_are_preserved(self):
        coordinator = await self.forecast_coordinator()
        with (
            patch.object(
                self.module,
                "fetch_forecast",
                return_value=self.report(
                    p10=Decimal("-300"),
                    p50=Decimal("-200"),
                    p90=Decimal("-100"),
                ),
            ),
            patch.object(
                self.module,
                "fetch_accuracy",
                return_value=self.accuracy(),
            ),
        ):
            data = await coordinator._async_update_data()
        prediction = next(
            point["prediction"]
            for point in data.response["intervals"]
            if point["basis"] == "forecast"
        )
        self.assertLess(Decimal(prediction["p10_import_eur_kwh"]), 0)
        self.assertLess(
            Decimal(prediction["p10_import_eur_kwh"]),
            Decimal(prediction["p50_import_eur_kwh"]),
        )
        self.assertLess(
            Decimal(prediction["p50_import_eur_kwh"]),
            Decimal(prediction["p90_import_eur_kwh"]),
        )
        await coordinator.async_shutdown()

    async def test_provider_published_hour_needs_canonical_ha_price(self):
        coordinator = await self.forecast_coordinator()
        report = self.report()
        report = self.forecast_core.ForecastReport(
            issued_at=report.issued_at,
            retrieved_at=report.retrieved_at,
            model_trained_at=report.model_trained_at,
            hours=(
                self.forecast_core.PublishedHour(
                    report.hours[0].start,
                    Decimal("100"),
                ),
                *report.hours[1:],
            ),
            attribution=report.attribution,
        )
        original = coordinator.price_coordinator.data
        coordinator.price_coordinator.data = None
        try:
            result = coordinator._build_current_response(
                coordinator._profile(),
                report,
                None,
                "metrics offline",
                self.now,
                {},
            )
        finally:
            coordinator.price_coordinator.data = original
        response = result.response
        self.assertEqual(response["unavailable_intervals"], 4)
        self.assertEqual(response["forecast_intervals"], 7 * 24 * 4 - 4)
        unavailable = response["intervals"][:4]
        self.assertTrue(
            all(point["basis"] == "unavailable" for point in unavailable)
        )
        self.assertTrue(
            all(
                point["unavailable_reason"]
                == "The canonical Nord Pool quarter is unavailable."
                for point in unavailable
            )
        )
        await coordinator.async_shutdown()

    async def test_dst_weeks_have_167_or_169_hours(self):
        coordinator = await self.forecast_coordinator()
        spring = datetime(2026, 3, 28, 12, tzinfo=self.module.UTC)
        autumn = datetime(2026, 10, 24, 12, tzinfo=self.module.UTC)
        spring_bounds = self.module._window_bounds(spring)
        autumn_bounds = self.module._window_bounds(autumn)
        normal_bounds = self.module._window_bounds(self.now)
        self.assertEqual(
            int((spring_bounds[1] - spring_bounds[0]).total_seconds() / 3600),
            167,
        )
        self.assertEqual(
            int((autumn_bounds[1] - autumn_bounds[0]).total_seconds() / 3600),
            169,
        )
        self.assertEqual(
            int((normal_bounds[1] - normal_bounds[0]).total_seconds() / 3600),
            168,
        )
        await coordinator.async_shutdown()

    async def test_provider_failure_is_explicit_update_failure(self):
        coordinator = await self.forecast_coordinator()
        with patch.object(
            self.module,
            "fetch_forecast",
            side_effect=self.forecast_core.ForecastError("provider offline"),
        ):
            with self.assertRaises(self.module.UpdateFailed):
                await coordinator._async_update_data()
            self.assertEqual(coordinator.refresh_error, "provider offline")
            with self.assertRaisesRegex(
                self.module.ServiceValidationError,
                "provider offline",
            ):
                await coordinator.async_response("current", 7, 1)
        await coordinator.async_shutdown()

    async def test_cached_response_discloses_failed_refresh(self):
        coordinator = await self.forecast_coordinator()
        with (
            patch.object(
                self.module,
                "fetch_forecast",
                return_value=self.report(),
            ),
            patch.object(
                self.module,
                "fetch_accuracy",
                return_value=self.accuracy(),
            ),
        ):
            data = await coordinator._async_update_data()
        coordinator.async_set_updated_data(data)
        with patch.object(
            self.module,
            "fetch_forecast",
            side_effect=self.forecast_core.ForecastError("provider offline"),
        ):
            with self.assertRaises(self.module.UpdateFailed):
                await coordinator._async_update_data()
        response = await coordinator.async_response("current", 7, 1)
        self.assertEqual(response["refresh_status"], "failed")
        self.assertEqual(response["refresh_error"], "provider offline")
        self.assertTrue(
            any(
                "retained issuance" in warning
                for warning in response["warnings"]
            )
        )
        sensor = self.sensor_module.PriceForecastSensor(
            coordinator,
            "pricing",
        )
        self.assertEqual(
            sensor.extra_state_attributes["refresh_status"],
            "failed",
        )
        await coordinator.async_shutdown()

    async def test_response_service_supports_current_and_archive_views(self):
        coordinator = await self.forecast_coordinator()
        with (
            patch.object(
                self.module,
                "fetch_forecast",
                return_value=self.report(),
            ),
            patch.object(
                self.module,
                "fetch_accuracy",
                return_value=self.accuracy(),
            ),
        ):
            data = await coordinator._async_update_data()
        coordinator.async_set_updated_data(data)
        self.hass.data.setdefault(base.DOMAIN, {})["pricing"] = {
            "forecast": coordinator
        }
        await self.integration.async_setup(self.hass, {})
        current = await self.hass.services.async_call(
            base.DOMAIN,
            "get_price_forecast",
            {
                "config_entry": "pricing",
                "view": "current",
            },
            blocking=True,
            return_response=True,
        )
        self.assertEqual(current["expected_intervals"], 7 * 24 * 4)
        future_archive = await self.hass.services.async_call(
            base.DOMAIN,
            "get_price_forecast",
            {
                "config_entry": "pricing",
                "view": "archive",
                "history_days": 8,
                "horizon_days": 1,
                "include_future_days": 1,
            },
            blocking=True,
            return_response=True,
        )
        self.assertEqual(future_archive["stored_hours"], 24)
        self.now = datetime.fromisoformat(current["window_end"]) + timedelta(
            hours=1
        )
        archive = await self.hass.services.async_call(
            base.DOMAIN,
            "get_price_forecast",
            {
                "config_entry": "pricing",
                "view": "archive",
                "history_days": 8,
                "horizon_days": 1,
            },
            blocking=True,
            return_response=True,
        )
        self.assertEqual(archive["stored_hours"], 24)
        await coordinator.async_shutdown()

    async def test_archive_retention_removes_expired_predictions(self):
        coordinator = await self.forecast_coordinator()
        report = self.report()
        self.assertTrue(coordinator._record_report(report, self.now))
        self.assertTrue(
            coordinator._record_report(
                report,
                self.now + timedelta(
                    days=self.module.ARCHIVE_RETENTION_DAYS + 8
                ),
            )
        )
        self.assertEqual(coordinator.archive, {})
        await coordinator.async_shutdown()


if __name__ == "__main__":
    unittest.main()
