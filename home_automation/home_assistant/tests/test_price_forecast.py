"""Seven-day household price forecast tests using real HA APIs."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import importlib
import unittest
from unittest.mock import AsyncMock, patch

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

    def add_archive_hours(
        self,
        coordinator,
        delivery_day: date,
        *,
        issued_at: datetime,
        horizon_days: int,
        hour_indexes: list[int],
        p50: Decimal,
        archive_origin: str | None = None,
    ):
        start, _ = self.pricing.market_bounds(delivery_day)
        origin = (
            archive_origin
            or self.module.ARCHIVE_ORIGIN_OBSERVED
        )
        for hour_index in hour_indexes:
            hour = start + timedelta(hours=hour_index)
            record = {
                "start": hour.astimezone(self.module.UTC).isoformat(),
                "issued_at": issued_at.astimezone(
                    self.module.UTC
                ).isoformat(),
                "model_trained_at": (
                    issued_at - timedelta(hours=6)
                ).astimezone(self.module.UTC).isoformat(),
                "horizon_days": horizon_days,
                "p10_spot_eur_mwh": format(p50 - Decimal("10"), "f"),
                "p50_spot_eur_mwh": format(p50, "f"),
                "p90_spot_eur_mwh": format(p50 + Decimal("10"), "f"),
                "archive_origin": origin,
            }
            coordinator.archive[
                coordinator._archive_key(record)
            ] = record

    def add_official_schedule(
        self,
        coordinator,
        delivery_day: date,
        *,
        spot: Decimal = Decimal("100"),
    ):
        profile = coordinator._profile()
        start, end = self.pricing.market_bounds(delivery_day)
        rows = []
        current = start
        while current < end:
            rows.append({
                "start": current.astimezone(
                    self.module.UTC
                ).isoformat(),
                "import_eur_kwh": self.pricing.forecast_import_eur_kwh(
                    profile,
                    spot,
                    current,
                ),
            })
            current += self.module.MARKET_INTERVAL
        schedule = coordinator._validated_official_schedule({
            "delivery_date": delivery_day.isoformat(),
            "expected_intervals": len(rows),
            "available_intervals": len(rows),
            "complete": True,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_sha256": profile.sha256,
            "intervals": rows,
        })
        coordinator.official_schedules[
            delivery_day.isoformat()
        ] = schedule
        return schedule

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
        self.assertIn(
            "distinct issuances observed by hourly polling",
            response["archive"]["sampling"].lower(),
        )
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

    async def test_archive_retains_vintages_and_evolution_orders_them(self):
        coordinator = await self.forecast_coordinator()
        older = self.report(
            issued_delta=timedelta(hours=2),
            p50=Decimal("100"),
        )
        newer = self.report(
            issued_delta=timedelta(minutes=20),
            p50=Decimal("80"),
        )
        self.assertTrue(coordinator._record_report(older, self.now))
        self.assertTrue(coordinator._record_report(newer, self.now))
        self.assertEqual(len(coordinator.archive), 2 * 7 * 24)

        latest = await coordinator.async_response("archive", 8, 1, 1)
        self.assertEqual(latest["stored_hours"], 24)
        self.assertEqual(len(latest["intervals"]), 24 * 4)
        first_latest = latest["intervals"][0]
        expected_latest = self.pricing.forecast_import_eur_kwh(
            coordinator._profile(),
            Decimal("80"),
            datetime.fromisoformat(first_latest["start"]),
        )
        self.assertEqual(
            first_latest["prediction"]["p50_import_eur_kwh"],
            expected_latest,
        )

        delivery_day = datetime.fromisoformat(
            latest["intervals"][0]["start"]
        ).date()
        evolution = await coordinator.async_response(
            "evolution",
            8,
            1,
            1,
            delivery_day,
        )
        self.assertEqual(evolution["schema_version"], 3)
        self.assertFalse(evolution["all_horizons"])
        self.assertEqual(evolution["horizons_included"], [1])
        self.assertEqual(evolution["retained_issuance_count"], 2)
        self.assertEqual(evolution["expected_intervals"], 24 * 4)
        self.assertEqual(evolution["available_intervals"], 24 * 4)
        self.assertTrue(evolution["complete_delivery_coverage"])
        self.assertEqual(evolution["matched_intervals"], 24 * 4)
        self.assertTrue(evolution["complete_matched_coverage"])
        self.assertEqual(evolution["comparison_intervals"], 24 * 4)
        self.assertEqual(
            Decimal(evolution["first_retained_mae_eur_kwh"]),
            0,
        )
        self.assertGreater(
            Decimal(evolution["latest_retained_mae_eur_kwh"]),
            0,
        )
        self.assertEqual(evolution["movement"], "worsened")
        first_point = evolution["intervals"][0]
        self.assertEqual(first_point["revision_count"], 2)
        self.assertLess(
            datetime.fromisoformat(first_point["first_retained"]["issued_at"]),
            datetime.fromisoformat(first_point["latest_retained"]["issued_at"]),
        )
        self.assertFalse(evolution["tracking_active_for_full_horizon"])
        await coordinator.async_shutdown()

    async def test_all_horizons_use_common_coverage_for_issuance_movement(self):
        coordinator = await self.forecast_coordinator()
        delivery_day = (
            self.now.astimezone(self.module.TALLINN).date()
            + timedelta(days=1)
        )
        delivery_start, _ = self.pricing.market_bounds(delivery_day)
        coordinator.archive = {}
        coordinator.vintage_tracking_started_at = (
            delivery_start - timedelta(days=4)
        ).astimezone(self.module.UTC).isoformat()
        self.add_archive_hours(
            coordinator,
            delivery_day,
            issued_at=delivery_start - timedelta(days=3),
            horizon_days=3,
            hour_indexes=[0, 1],
            p50=Decimal("130"),
        )
        self.add_archive_hours(
            coordinator,
            delivery_day,
            issued_at=delivery_start - timedelta(days=2),
            horizon_days=2,
            hour_indexes=[1],
            p50=Decimal("150"),
        )
        self.add_archive_hours(
            coordinator,
            delivery_day,
            issued_at=delivery_start - timedelta(days=2),
            horizon_days=2,
            hour_indexes=[2, 3],
            p50=Decimal("100"),
        )
        self.add_archive_hours(
            coordinator,
            delivery_day,
            issued_at=delivery_start - timedelta(days=1),
            horizon_days=1,
            hour_indexes=[1],
            p50=Decimal("110"),
        )
        self.add_archive_hours(
            coordinator,
            delivery_day,
            issued_at=delivery_start - timedelta(days=1),
            horizon_days=1,
            hour_indexes=[2],
            p50=Decimal("100"),
        )
        schedule = self.add_official_schedule(
            coordinator,
            delivery_day,
        )

        evolution = await coordinator.async_response(
            "evolution",
            8,
            1,
            1,
            delivery_day,
            True,
        )

        self.assertTrue(evolution["all_horizons"])
        self.assertIsNone(evolution["horizon_days"])
        self.assertEqual(evolution["horizons_included"], [1, 2, 3])
        self.assertEqual(evolution["retained_issuance_count"], 3)
        self.assertEqual(evolution["expected_intervals"], 96)
        self.assertEqual(evolution["available_intervals"], 16)
        self.assertTrue(evolution["partial_delivery_coverage"])
        self.assertEqual(evolution["official_intervals"], 96)
        self.assertTrue(evolution["complete_official_coverage"])
        self.assertEqual(evolution["matched_intervals"], 16)
        self.assertEqual(
            evolution["common_coverage_matched_intervals"],
            4,
        )

        metrics = evolution["issuance_metrics"]
        self.assertEqual(
            [item["horizon_days"] for item in metrics],
            [3, 2, 1],
        )
        self.assertEqual(
            [item["available_intervals"] for item in metrics],
            [8, 12, 8],
        )
        self.assertEqual(
            [item["matched_intervals"] for item in metrics],
            [8, 12, 8],
        )
        self.assertEqual(
            [
                item["common_coverage_matched_intervals"]
                for item in metrics
            ],
            [4, 4, 4],
        )
        self.assertEqual(
            [item["movement_vs_previous"] for item in metrics],
            ["unavailable", "worsened", "improved"],
        )
        self.assertFalse(metrics[-1]["complete_delivery_coverage"])
        self.assertTrue(metrics[-1]["partial_delivery_coverage"])
        profile = coordinator._profile()
        official = {
            datetime.fromisoformat(row["start"]): Decimal(
                row["import_eur_kwh"]
            )
            for row in schedule["intervals"]
        }
        common_quarters = [
            delivery_start + timedelta(hours=1)
            + index * self.module.MARKET_INTERVAL
            for index in range(4)
        ]
        for metric, spot in zip(
            metrics,
            (Decimal("130"), Decimal("150"), Decimal("110")),
            strict=True,
        ):
            errors = [
                Decimal(
                    self.pricing.forecast_import_eur_kwh(
                        profile,
                        spot,
                        quarter,
                    )
                ) - official[quarter]
                for quarter in common_quarters
            ]
            expected_mae = sum(
                (abs(error) for error in errors),
                Decimal(0),
            ) / len(errors)
            expected_bias = sum(errors, Decimal(0)) / len(errors)
            self.assertEqual(
                Decimal(metric["mae_eur_kwh"]),
                expected_mae,
            )
            self.assertEqual(
                Decimal(metric["bias_eur_kwh"]),
                expected_bias,
            )

        common_point = next(
            point
            for point in evolution["intervals"]
            if datetime.fromisoformat(point["start"]).astimezone(
                self.module.UTC
            ) == delivery_start + timedelta(hours=1)
        )
        self.assertEqual(
            [item["horizon_days"] for item in common_point["vintages"]],
            [3, 2, 1],
        )
        self.assertTrue(all(
            item["archive_origin"]
            == self.module.ARCHIVE_ORIGIN_OBSERVED
            for item in common_point["vintages"]
        ))
        await coordinator.async_shutdown()

    async def test_default_evolution_remains_single_horizon(self):
        coordinator = await self.forecast_coordinator()
        delivery_day = (
            self.now.astimezone(self.module.TALLINN).date()
            + timedelta(days=1)
        )
        delivery_start, _ = self.pricing.market_bounds(delivery_day)
        coordinator.archive = {}
        coordinator.vintage_tracking_started_at = (
            delivery_start - timedelta(days=4)
        ).astimezone(self.module.UTC).isoformat()
        for horizon, spot in (
            (3, Decimal("130")),
            (2, Decimal("120")),
            (1, Decimal("110")),
        ):
            self.add_archive_hours(
                coordinator,
                delivery_day,
                issued_at=delivery_start - timedelta(days=horizon),
                horizon_days=horizon,
                hour_indexes=[0],
                p50=spot,
            )
        self.add_official_schedule(coordinator, delivery_day)

        evolution = await coordinator.async_response(
            "evolution",
            8,
            1,
            1,
            delivery_day,
        )

        self.assertFalse(evolution["all_horizons"])
        self.assertEqual(evolution["horizon_days"], 1)
        self.assertEqual(evolution["horizons_included"], [1])
        self.assertEqual(evolution["retained_issuance_count"], 1)
        self.assertEqual(
            {
                vintage["horizon_days"]
                for point in evolution["intervals"]
                for vintage in point["vintages"]
            },
            {1},
        )
        with self.assertRaisesRegex(
            self.module.ServiceValidationError,
            "delivery date",
        ):
            await coordinator.async_response(
                "evolution",
                8,
                1,
                1,
                None,
                True,
            )
        await coordinator.async_shutdown()

    async def test_same_issuance_with_changed_values_is_rejected(self):
        coordinator = await self.forecast_coordinator()
        original = self.report(p50=Decimal("100"))
        conflicting = self.report(p50=Decimal("101"))
        self.assertTrue(coordinator._record_report(original, self.now))
        with self.assertRaisesRegex(
            self.module.ForecastError,
            "without a new issuance",
        ):
            coordinator._record_report(conflicting, self.now)
        await coordinator.async_shutdown()

    async def test_version_one_archive_migrates_as_partial_vintage_history(self):
        coordinator = await self.forecast_coordinator()
        report = self.report()
        slot = report.hours[0]
        legacy = {
            "start": slot.start.astimezone(self.module.UTC).isoformat(),
            "issued_at": report.issued_at.astimezone(
                self.module.UTC
            ).isoformat(),
            "model_trained_at": report.model_trained_at.astimezone(
                self.module.UTC
            ).isoformat(),
            "horizon_days": slot.horizon_days,
            "p10_spot_eur_mwh": format(slot.p10, "f"),
            "p50_spot_eur_mwh": format(slot.p50, "f"),
            "p90_spot_eur_mwh": format(slot.p90, "f"),
        }
        coordinator.store.async_load = AsyncMock(
            return_value={"records": [legacy]}
        )
        coordinator.store.async_save = AsyncMock()
        await coordinator.async_load()

        self.assertEqual(len(coordinator.archive), 1)
        migrated = next(iter(coordinator.archive.values()))
        self.assertEqual(
            migrated["archive_origin"],
            self.module.ARCHIVE_ORIGIN_MIGRATED,
        )
        coordinator.store.async_save.assert_awaited_once()
        saved = coordinator.store.async_save.await_args.args[0]
        self.assertEqual(
            saved["schema_version"],
            self.module.ARCHIVE_SCHEMA_VERSION,
        )
        self.assertIsNotNone(saved["vintage_tracking_started_at"])
        self.assertEqual(
            saved["records"][0]["archive_origin"],
            self.module.ARCHIVE_ORIGIN_MIGRATED,
        )
        self.assertEqual(saved["official_schedules"], [])
        delivery_day = slot.start.astimezone(
            self.module.TALLINN
        ).date()
        evolution = coordinator._evolution_response(
            coordinator._profile(),
            self.now,
            8,
            1,
            1,
            delivery_day,
            coordinator._published_prices(self.now),
        )
        self.assertTrue(evolution["contains_migrated_latest_only"])
        self.assertEqual(
            evolution["issuance_metrics"][0]["archive_origin"],
            self.module.ARCHIVE_ORIGIN_MIGRATED,
        )
        self.assertTrue(
            evolution["issuance_metrics"][0]["partial_history"]
        )
        self.assertEqual(
            evolution["intervals"][0]["first_retained"][
                "archive_origin"
            ],
            self.module.ARCHIVE_ORIGIN_MIGRATED,
        )
        self.assertNotIn("original", str(evolution).lower())
        await coordinator.async_shutdown()

    async def test_version_two_archive_migrates_without_losing_origins(self):
        coordinator = await self.forecast_coordinator()
        report = self.report()
        slot = report.hours[0]
        record = {
            "start": slot.start.astimezone(self.module.UTC).isoformat(),
            "issued_at": report.issued_at.astimezone(
                self.module.UTC
            ).isoformat(),
            "model_trained_at": report.model_trained_at.astimezone(
                self.module.UTC
            ).isoformat(),
            "horizon_days": slot.horizon_days,
            "p10_spot_eur_mwh": format(slot.p10, "f"),
            "p50_spot_eur_mwh": format(slot.p50, "f"),
            "p90_spot_eur_mwh": format(slot.p90, "f"),
            "archive_origin": self.module.ARCHIVE_ORIGIN_OBSERVED,
        }
        tracking_started = (
            self.now - timedelta(days=8)
        ).astimezone(self.module.UTC).isoformat()
        coordinator.store.async_load = AsyncMock(return_value={
            "schema_version": 2,
            "vintage_tracking_started_at": tracking_started,
            "records": [record],
        })
        coordinator.store.async_save = AsyncMock()

        await coordinator.async_load()

        saved = coordinator.store.async_save.await_args.args[0]
        self.assertEqual(
            saved["schema_version"],
            self.module.ARCHIVE_SCHEMA_VERSION,
        )
        self.assertEqual(
            saved["records"][0]["archive_origin"],
            self.module.ARCHIVE_ORIGIN_OBSERVED,
        )
        self.assertEqual(saved["official_schedules"], [])
        self.assertEqual(
            coordinator.vintage_tracking_started_at,
            tracking_started,
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
        summary = sensor.extra_state_attributes["evolution_summary"]
        for key in (
            "expected_intervals",
            "available_intervals",
            "complete_delivery_coverage",
            "partial_delivery_coverage",
            "official_intervals",
            "matched_intervals",
            "complete_matched_coverage",
            "partial_matched_coverage",
        ):
            self.assertIn(key, summary)
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

    async def test_evolution_handles_23_24_25_hour_delivery_days(self):
        coordinator = await self.forecast_coordinator()
        profile = coordinator._profile()
        cases = (
            (date(2026, 3, 29), 92),
            (date(2026, 8, 3), 96),
            (date(2026, 10, 25), 100),
        )
        for delivery_day, expected in cases:
            with self.subTest(delivery_day=delivery_day):
                coordinator.archive = {}
                coordinator.official_schedules = {}
                delivery_start, delivery_end = self.pricing.market_bounds(
                    delivery_day
                )
                hours = int(
                    (delivery_end - delivery_start)
                    / self.module.HOUR
                )
                coordinator.vintage_tracking_started_at = (
                    delivery_start - timedelta(days=2)
                ).isoformat()
                self.add_archive_hours(
                    coordinator,
                    delivery_day,
                    issued_at=delivery_start - timedelta(days=1),
                    horizon_days=1,
                    hour_indexes=list(range(hours)),
                    p50=Decimal("100"),
                )
                self.add_official_schedule(
                    coordinator,
                    delivery_day,
                )
                now = delivery_start + timedelta(hours=12)
                evolution = coordinator._evolution_response(
                    profile,
                    now,
                    1,
                    1,
                    0,
                    delivery_day,
                    None,
                    False,
                    coordinator.official_schedules,
                )
                self.assertEqual(
                    evolution["expected_intervals"],
                    expected,
                )
                self.assertEqual(
                    evolution["available_intervals"],
                    expected,
                )
                self.assertEqual(
                    evolution["official_intervals"],
                    expected,
                )
                self.assertEqual(
                    evolution["matched_intervals"],
                    expected,
                )
                self.assertTrue(
                    evolution["complete_delivery_coverage"]
                )
                self.assertTrue(
                    evolution["complete_official_coverage"]
                )
                self.assertTrue(
                    evolution["complete_matched_coverage"]
                )
        await coordinator.async_shutdown()

    async def test_official_schedule_persists_after_delivery_day_rollover(self):
        coordinator = await self.forecast_coordinator()
        coordinator.store.async_save = AsyncMock()
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
            await coordinator._async_update_data()
        payload = coordinator.store.async_save.await_args.args[0]
        delivery_day = (
            self.now.astimezone(self.module.TALLINN).date()
            + timedelta(days=1)
        )
        stored = {
            item["delivery_date"]: item
            for item in payload["official_schedules"]
        }
        self.assertEqual(
            payload["schema_version"],
            self.module.ARCHIVE_SCHEMA_VERSION,
        )
        self.assertTrue(stored[delivery_day.isoformat()]["complete"])
        self.assertEqual(
            stored[delivery_day.isoformat()]["available_intervals"],
            96,
        )
        self.assertEqual(
            stored[delivery_day.isoformat()]["profile_id"],
            self.coordinator.data.profile_id,
        )
        self.assertEqual(
            stored[delivery_day.isoformat()]["profile_version"],
            self.coordinator.data.profile_version,
        )
        self.assertEqual(
            stored[delivery_day.isoformat()]["profile_sha256"],
            self.coordinator.data.profile_sha256,
        )

        self.now += timedelta(days=2)
        reloaded = self.module.HouseholdPriceForecastCoordinator(
            self.hass,
            self.pricing_entry,
            self.coordinator,
        )
        reloaded.store.async_load = AsyncMock(return_value=payload)
        reloaded.store.async_save = AsyncMock()
        await reloaded.async_load()
        evolution = await reloaded.async_response(
            "evolution",
            3,
            1,
            0,
            delivery_day,
        )
        self.assertEqual(evolution["matched_intervals"], 96)
        self.assertTrue(evolution["complete_matched_coverage"])
        self.assertEqual(
            evolution["official_schedule"]["profile_sha256"],
            self.coordinator.data.profile_sha256,
        )
        self.assertTrue(
            evolution["official_schedule"][
                "profile_matches_forecast_profile"
            ]
        )
        await reloaded.async_shutdown()
        await coordinator.async_shutdown()

    async def test_provider_failure_is_explicit_update_failure(self):
        coordinator = await self.forecast_coordinator()
        coordinator.store.async_save = AsyncMock()
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
        coordinator.store.async_save.assert_awaited_once()
        saved = coordinator.store.async_save.await_args.args[0]
        self.assertEqual(len(saved["official_schedules"]), 2)
        self.assertTrue(
            all(
                schedule["complete"]
                for schedule in saved["official_schedules"]
            )
        )
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
        delivery_day = datetime.fromisoformat(
            future_archive["intervals"][0]["start"]
        ).date()
        evolution = await self.hass.services.async_call(
            base.DOMAIN,
            "get_price_forecast",
            {
                "config_entry": "pricing",
                "view": "evolution",
                "history_days": 8,
                "horizon_days": 1,
                "include_future_days": 1,
                "delivery_date": delivery_day.isoformat(),
            },
            blocking=True,
            return_response=True,
        )
        self.assertEqual(evolution["delivery_date"], delivery_day.isoformat())
        self.assertEqual(evolution["retained_issuance_count"], 1)
        self.assertFalse(evolution["all_horizons"])
        combined = await self.hass.services.async_call(
            base.DOMAIN,
            "get_price_forecast",
            {
                "config_entry": "pricing",
                "view": "evolution",
                "history_days": 8,
                "horizon_days": 1,
                "include_future_days": 1,
                "delivery_date": delivery_day.isoformat(),
                "all_horizons": True,
            },
            blocking=True,
            return_response=True,
        )
        self.assertTrue(combined["all_horizons"])
        self.assertEqual(combined["horizons_included"], [1])
        with self.assertRaisesRegex(
            self.module.ServiceValidationError,
            "evolution view",
        ):
            await self.hass.services.async_call(
                base.DOMAIN,
                "get_price_forecast",
                {
                    "config_entry": "pricing",
                    "view": "archive",
                    "all_horizons": True,
                },
                blocking=True,
                return_response=True,
            )
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
