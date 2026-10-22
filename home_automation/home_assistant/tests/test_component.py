"""Run against real HA in an isolated environment, never the MCP environment."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib
import inspect
import json
from pathlib import Path
import runpy
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed


REPOSITORY = Path(__file__).resolve().parents[3]
DOMAIN = "household_electricity_pricing"
COMPONENT = "custom_components." + DOMAIN
_bundle_directory = None


def setUpModule():
    global _bundle_directory
    _bundle_directory = tempfile.TemporaryDirectory()
    root = Path(_bundle_directory.name)
    build = runpy.run_path(
        str(REPOSITORY / "home_automation/home_assistant/bundle.py"),
        run_name="bundle_builder",
    )["build_bundle"]
    artifact = root / "integration.zip"
    build(artifact)
    with zipfile.ZipFile(artifact) as archive:
        archive.extractall(root)
    sys.path.insert(0, str(root))


def tearDownModule():
    sys.path.remove(_bundle_directory.name)
    _bundle_directory.cleanup()


def entry(
    domain: str, entry_id: str, data: dict,
    state: ConfigEntryState = ConfigEntryState.LOADED,
) -> ConfigEntry:
    return ConfigEntry(
        version=1, minor_version=1, domain=domain, entry_id=entry_id,
        title=domain, data=data, options={}, source="user",
        unique_id=domain, discovery_keys=MappingProxyType({}),
        subentries_data=[], state=state,
    )


class HAComponentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.integration = importlib.import_module(COMPONENT)
        self.coordinator_module = importlib.import_module(COMPONENT + ".coordinator")
        self.sensor_module = importlib.import_module(COMPONENT + ".sensor")
        self.flow_module = importlib.import_module(COMPONENT + ".config_flow")
        self.llm_module = importlib.import_module(COMPONENT + ".price_api")
        self.pricing = importlib.import_module(COMPONENT + "._vendor.home_automation.home_assistant.pricing")
        self.profile_module = importlib.import_module(COMPONENT + "._vendor.home_automation.electricity_pricing.engine")
        self.calculation_module = importlib.import_module(COMPONENT + ".calculation_api")
        self.config_directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.config_directory.name)
        profile_file = Path(self.config_directory.name) / "private" / "profile.json"
        profile_file.parent.mkdir()
        self.profile_value = json.loads((REPOSITORY / "examples/pricing-profile.example.json").read_text())
        profile_file.write_text(json.dumps(self.profile_value))
        self.nordpool = entry("nordpool", "market", {"currency": "EUR", "areas": ["EE"]})
        self.pricing_entry = entry(DOMAIN, "pricing", {
            "nordpool_config_entry": "market", "profile_path": "private/profile.json",
        })
        self.entries = {"market": self.nordpool, "pricing": self.pricing_entry}
        self.forward = AsyncMock()
        self.hass.config_entries = SimpleNamespace(
            async_get_entry=self.entries.get,
            async_forward_entry_setups=self.forward,
            async_unload_platforms=AsyncMock(return_value=True),
        )
        self.now = datetime(2026, 8, 3, 7, 7, tzinfo=timezone.utc)
        self.clock = patch("homeassistant.util.dt.utcnow", side_effect=lambda: self.now)
        self.clock.start()
        self.calls = []
        self.failures = set()
        self.empty = set()

        async def market_data(call):
            self.assertEqual(call.data["areas"], ["EE"])
            self.assertEqual(call.data["currency"], "EUR")
            self.assertEqual(call.data["config_entry"], "market")
            day = date.fromisoformat(call.data["date"])
            self.calls.append(day)
            if day in self.failures:
                raise HomeAssistantError("Synthetic provider failure")
            if day in self.empty:
                return {"EE": []}
            start, end = self.pricing.market_day_bounds(day)
            result = []
            while start < end:
                finish = start + timedelta(minutes=15)
                result.append({"start": start.isoformat(), "end": finish.isoformat(), "price": "100"})
                start = finish
            return {"EE": result}

        self.hass.services.async_register(
            "nordpool", "get_prices_for_date", market_data,
            supports_response=SupportsResponse.ONLY,
        )
        self.coordinator = self.coordinator_module.HouseholdPriceCoordinator(self.hass, self.pricing_entry)
        self.pricing_entry.runtime_data = self.coordinator

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()
        await self.hass.async_stop()
        self.clock.stop()
        self.config_directory.cleanup()

    async def refresh(self):
        data = await self.coordinator._async_update_data()
        self.coordinator.async_set_updated_data(data)
        return data

    async def test_real_runtime_imports_and_quarter_costs(self):
        data = await self.refresh()
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(data.today.complete)
        self.assertTrue(data.tomorrow.complete)
        sensors = [
            self.sensor_module.HouseholdPriceSensor(self.coordinator, "pricing", description)
            for description in self.sensor_module.DESCRIPTIONS
        ]
        self.assertEqual(len(sensors), 3)
        for sensor in sensors:
            self.assertTrue(sensor.available)
            self.assertIsInstance(sensor.native_value, Decimal)
            self.assertEqual(sensor.native_unit_of_measurement, "EUR/kWh")
            self.assertEqual(sensor.extra_state_attributes["fixed_fees"], "excluded")
            self.assertEqual(sensor.extra_state_attributes["profile_version"], self.profile_value["version"])
            self.assertEqual(
                sensor.extra_state_attributes["exact_price_text"],
                str(sensor.native_value) + " EUR/kWh",
            )

    async def test_complete_market_days_are_cached_but_tariffs_reload(self):
        before = await self.refresh()
        self.now += timedelta(minutes=15)
        value = dict(self.profile_value)
        value["version"] = "synthetic-updated"
        path = Path(self.config_directory.name) / "private/profile.json"
        path.write_text(json.dumps(value))
        after = await self.refresh()
        self.assertEqual(len(self.calls), 3)
        self.assertNotEqual(before.valid_from, after.valid_from)
        self.assertEqual(after.profile_version, "synthetic-updated")
        self.assertNotEqual(before.profile_sha256, after.profile_sha256)

    async def test_future_market_failure_does_not_hide_valid_current_rates(self):
        self.failures.add(date(2026, 8, 4))
        data = await self.refresh()
        self.assertTrue(data.today.complete)
        self.assertFalse(data.tomorrow.complete)
        self.assertTrue(self.coordinator.market_errors)
        self.assertIn("retrieval failed", next(iter(self.coordinator.market_errors.values())))

    async def test_current_market_failure_does_not_return_zero_or_stale_success(self):
        self.failures.add(date(2026, 8, 3))
        with self.assertRaisesRegex(UpdateFailed, "current quarter"):
            await self.coordinator._async_update_data()

    async def test_unpublished_future_retries_are_bounded(self):
        self.empty.add(date(2026, 8, 4))
        before = await self.refresh()
        self.assertFalse(before.tomorrow.complete)
        self.now += timedelta(minutes=15)
        await self.refresh()
        self.assertEqual(len(self.calls), 3)
        self.now += timedelta(hours=1)
        await self.refresh()
        self.assertEqual(len(self.calls), 4)

    async def test_sensors_and_service_do_not_expose_an_expired_quarter(self):
        data = await self.refresh()
        sensor = self.sensor_module.HouseholdPriceSensor(
            self.coordinator, "pricing", self.sensor_module.DESCRIPTIONS[1]
        )
        self.now = data.valid_until
        self.assertFalse(sensor.available)
        self.assertIsNone(sensor.native_value)
        self.assertIsNone(sensor.extra_state_attributes["exact_eur_kwh"])
        with patch.object(self.coordinator, "async_request_refresh", new=AsyncMock()):
            with self.assertRaisesRegex(ServiceValidationError, "unavailable"):
                await self.coordinator.async_price_response("today")

    async def test_quarter_callback_expires_before_waiting_for_a_refresh(self):
        data = await self.refresh()
        observed = []
        remove = self.coordinator.async_add_listener(
            lambda: observed.append(
                (
                    self.coordinator.data.contains(self.now),
                    self.coordinator.in_quarter_refresh_transition(
                        self.now
                    ),
                )
            )
        )
        self.now = data.valid_until
        self.assertTrue(
            self.coordinator.in_quarter_refresh_transition(self.now)
        )
        with patch.object(self.coordinator, "async_request_refresh", new=AsyncMock()):
            await self.coordinator.async_quarter_changed(self.now)
        self.assertEqual(observed, [(False, True)])
        self.assertTrue(
            self.coordinator.in_quarter_refresh_transition(self.now)
        )
        self.assertFalse(
            self.coordinator.in_quarter_refresh_transition(
                self.now + timedelta(seconds=61)
            )
        )
        remove()

    async def test_invalid_source_currency_or_missing_config_is_explicit(self):
        self.entries.pop("market")
        with self.assertRaisesRegex(UpdateFailed, "not loaded"):
            await self.coordinator._async_update_data()
        self.entries["market"] = entry("nordpool", "market", {"currency": "SEK", "areas": ["EE"]})
        with self.assertRaisesRegex(UpdateFailed, "EUR"):
            await self.coordinator._async_update_data()

    async def test_invalid_profile_does_not_leave_previous_success_available(self):
        await self.refresh()
        (Path(self.config_directory.name) / "private/profile.json").write_text("{}")
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()

    async def test_native_service_returns_typed_data_and_rejects_invalid_period(self):
        await self.refresh()
        await self.integration.async_setup(self.hass, {})
        result = await self.hass.services.async_call(
            DOMAIN, "get_prices", {"config_entry": "pricing", "period": "tomorrow"},
            blocking=True, return_response=True,
        )
        self.assertEqual(result["unit"], "EUR/kWh")
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["prices"]), 96)
        with self.assertRaises(ServiceValidationError):
            await self.coordinator.async_price_response("__dict__")

    async def test_config_flow_validates_existing_nordpool_and_confined_profile(self):
        flow = self.flow_module.HouseholdPriceConfigFlow()
        flow.hass = self.hass
        await flow._validate(self.pricing_entry.data)
        with self.assertRaises(self.profile_module.PricingError):
            await flow._validate({"nordpool_config_entry": "wrong", "profile_path": "private/profile.json"})
        with self.assertRaises(self.profile_module.PricingError):
            await flow._validate({"nordpool_config_entry": "market", "profile_path": "../outside.json"})

    async def test_read_only_llm_api_has_no_device_control_surface(self):
        await self.refresh()
        api = self.llm_module.HouseholdPriceAPI(self.hass, self.coordinator)
        instance = await api.async_get_api_instance(None)
        self.assertEqual(api.id, DOMAIN)
        self.assertEqual(api.name, "Household electricity prices (read only)")
        self.assertEqual(len(instance.tools), 4)
        self.assertEqual(
            {tool.name for tool in instance.tools},
            {
                "GetHouseholdElectricityPrices", "QuoteHouseholdElectricityInterval",
                "CalculateHouseholdElectricityPeriod", "EstimateHouseholdElectricityScenario",
            },
        )
        for item in instance.tools:
            self.assertTrue(item.annotations.read_only)
            self.assertFalse(item.annotations.destructive)
        tool = instance.tools[0]
        self.assertEqual(tool.name, "GetHouseholdElectricityPrices")
        self.assertTrue(tool.annotations.read_only)
        self.assertFalse(tool.annotations.destructive)
        self.assertEqual(tool.integration, DOMAIN)
        result = await tool.async_call(
            self.hass, SimpleNamespace(tool_args={"period": "today"}), None
        )
        self.assertFalse(result.error)
        self.assertEqual(result.data["fixed_fees"], "excluded")
        self.assertTrue(result.data["complete"])
        default_result = await tool.async_call(
            self.hass, SimpleNamespace(tool_args={}), None
        )
        self.assertEqual(default_result.data["delivery_date"], "2026-08-03")

    async def test_native_interval_action_returns_exact_engine_response(self):
        await self.integration.async_setup(self.hass, {})
        self.coordinator.calculator.price_provider = Mock(return_value={self.now.replace(minute=0): Decimal("100")})
        result = await self.hass.services.async_call(
            DOMAIN, "quote_electricity_interval",
            {
                "config_entry": "pricing", "start": "2026-08-03T10:00:00+03:00",
                "end": "2026-08-03T10:15:00+03:00", "consumption_kwh": "2", "export_kwh": "0.5",
            },
            blocking=True, return_response=True,
        )
        self.assertEqual(result["billable_import_kwh"], "1.5")
        self.assertEqual(result["amounts"]["including_vat_eur"], "0.3519")
        self.assertEqual(result["fixed_fees"], "excluded")
        self.assertEqual(result["price_basis"], "published")

    async def test_native_actions_reject_forecast_bill_and_unknown_action(self):
        await self.integration.async_setup(self.hass, {})
        with self.assertRaisesRegex(ServiceValidationError, "published prices only"):
            await self.hass.services.async_call(
                DOMAIN, "calculate_electricity_period",
                {"config_entry": "pricing", "period": "previous_month", "price_basis": "estimate"},
                blocking=True, return_response=True,
            )
        with self.assertRaisesRegex(ServiceValidationError, "Unknown"):
            await self.coordinator.async_calculate_response("homeassistant.turn_on", {})

    async def test_native_future_load_estimate_needs_supplied_prices(self):
        await self.integration.async_setup(self.hass, {})
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        prices = [
            {
                "start": (start + index * timedelta(minutes=15)).isoformat(),
                "end": (start + (index + 1) * timedelta(minutes=15)).isoformat(),
                "price_eur_mwh": "-30",
            }
            for index in range(4)
        ]
        result = await self.hass.services.async_call(
            DOMAIN, "estimate_electricity_scenario",
            {
                "config_entry": "pricing", "start": start.isoformat(),
                "duration_minutes": "60", "power_kw": "2",
                "price_basis": "estimate", "price_intervals": prices,
            },
            blocking=True, return_response=True,
        )
        self.assertEqual(result["calculation_kind"], "estimate")
        self.assertEqual(result["price_source"], "caller_supplied_estimated_prices")
        self.assertEqual(result["energy_kwh"], "2")
        self.assertEqual(result["fixed_fees"], "excluded")

    async def test_native_presets_fail_explicitly_for_unconfigured_history(self):
        await self.integration.async_setup(self.hass, {})
        with self.assertRaisesRegex(ServiceValidationError, "coverage"):
            await self.hass.services.async_call(
                DOMAIN, "calculate_electricity_period",
                {"config_entry": "pricing", "period": "last_12_months"},
                blocking=True, return_response=True,
            )
        with self.assertRaisesRegex(ServiceValidationError, "Elering"):
            await self.hass.services.async_call(
                DOMAIN, "calculate_electricity_period",
                {"config_entry": "pricing", "period": "month_to_date"},
                blocking=True, return_response=True,
            )

    async def test_native_month_to_date_returns_full_fees_and_actual_metering_cutoff(self):
        client = importlib.import_module(COMPONENT + "._vendor.home_automation.household_consumption.client")
        _, _, start, end = self.profile_module.period_bounds("2026-08-01", "2026-08-02")
        values = []
        current = start
        while current < end:
            values.append(client.Reading(current, Decimal("1"), Decimal("0")) if current < end - timedelta(days=1)
                          else client.Reading(current, None, None))
            current += timedelta(minutes=15)
        self.coordinator.calculator.client = Mock()
        self.coordinator.calculator.client.get_consumption.return_value = client.ConsumptionReport(
            client.DateWindow(date(2026, 8, 1), date(2026, 8, 2), start, end),
            "SYNTHETIC-POINT", "15_minutes", tuple(values), self.now,
        )
        self.coordinator.calculator.price_provider = Mock(
            return_value={item.start: Decimal("100") for item in values},
        )
        await self.integration.async_setup(self.hass, {})
        result = await self.hass.services.async_call(
            DOMAIN, "calculate_electricity_period",
            {"config_entry": "pricing", "period": "month_to_date"},
            blocking=True, return_response=True,
        )
        self.assertEqual(result["calculation_kind"], "provisional_bill")
        self.assertEqual(result["fixed_fees"], "full_current_month")
        self.assertEqual(result["interval_count"], 96)
        self.assertEqual(result["coverage"]["pending_completed_days"], 1)
        fees = {line["component_id"]: line["net_eur"] for line in result["lines"]}
        self.assertEqual(fees["supplier_monthly"], "2.35")
        self.assertEqual(fees["network_monthly"], "13.75")
        self.assertFalse(result["monthly_periods"][0]["complete_calendar_month"])

    async def test_config_flow_accepts_only_private_scoped_credentials_file(self):
        path = Path(self.config_directory.name) / "private/elering.json"
        path.write_text(json.dumps({"client_id": "synthetic-id", "client_secret": "not-a-real-secret"}))
        path.chmod(0o600)
        flow = self.flow_module.HouseholdPriceConfigFlow()
        flow.hass = self.hass
        data = {**self.pricing_entry.data, "elering_credentials_path": "private/elering.json"}
        await flow._validate(data)
        path.chmod(0o644)
        with self.assertRaisesRegex(self.coordinator_module.ConsumptionError, "group"):
            await flow._validate(data)

    async def test_llm_calculator_parameters_and_calls_use_the_same_action_schema(self):
        tool = self.calculation_module.HouseholdCalculationTool("quote_electricity_interval", self.coordinator)
        self.coordinator.calculator.price_provider = Mock(return_value={self.now.replace(minute=0): Decimal("100")})
        arguments = tool.parameters({
            "start": "2026-08-03T10:00:00+03:00", "end": "2026-08-03T10:15:00+03:00",
            "consumption_kwh": "1", "export_kwh": "0",
        })
        result = await tool.async_call(self.hass, SimpleNamespace(tool_args=arguments), None)
        self.assertFalse(result.error)
        self.assertEqual(result.data["calculation_kind"], "interval_quote")
        self.assertEqual(result.data["fixed_fees"], "excluded")

    async def test_entry_setup_registers_the_selected_api_and_cleanup(self):
        from homeassistant.helpers import llm

        callbacks = []
        old_coordinator = self.coordinator
        self.pricing_entry = entry(
            DOMAIN, "pricing", dict(self.pricing_entry.data),
            ConfigEntryState.SETUP_IN_PROGRESS,
        )
        self.entries["pricing"] = self.pricing_entry
        try:
            with patch.object(
                ConfigEntry, "async_on_unload",
                side_effect=lambda callback: callbacks.append(callback),
            ):
                self.assertTrue(
                    await self.integration.async_setup_entry(self.hass, self.pricing_entry)
                )
            self.coordinator = self.pricing_entry.runtime_data
            self.forward.assert_awaited_once_with(self.pricing_entry, ["sensor"])
            self.assertIn(DOMAIN, {api.id for api in llm.async_get_apis(self.hass)})
            self.assertTrue(
                await self.integration.async_unload_entry(self.hass, self.pricing_entry)
            )
        finally:
            for callback in reversed(callbacks):
                result = callback()
                if inspect.isawaitable(result):
                    await result
            await old_coordinator.async_shutdown()
        self.assertNotIn(DOMAIN, {api.id for api in llm.async_get_apis(self.hass)})

    async def test_energy_control_activity_distinguishes_commands_and_shadows(self):
        now = self.now.astimezone(timezone.utc)
        optimizer = SimpleNamespace(
            state={
                "readiness_pilot_started_at": (
                    now - timedelta(hours=3)
                ).isoformat(),
                "decision_history": [
                    {
                        "observed_at": (
                            now - timedelta(hours=2)
                        ).isoformat(),
                        "actual_target_c": 50,
                        "proposed_target_c": 55,
                        "state": "readiness_charge",
                        "reason": "Readiness charge.",
                        "current_price_eur_kwh": "0.25",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(hours=1)
                        ).isoformat(),
                        "actual_target_c": 50,
                        "proposed_target_c": 50,
                        "state": "readiness_pilot",
                        "reason": "Hold normal target.",
                        "write_sent": False,
                    },
                ],
            },
            async_add_listener=Mock(),
        )
        floor = SimpleNamespace(
            state={
                "decision_history": [
                    {
                        "observed_at": (
                            now - timedelta(minutes=30)
                        ).isoformat(),
                        "actual_offset_c": 0,
                        "proposed_offset_c": -4,
                        "state": "coast",
                        "reason": "Expensive block.",
                        "current_price_eur_kwh": "0.30",
                        "write_sent": False,
                    },
                    {
                        "observed_at": (
                            now - timedelta(minutes=20)
                        ).isoformat(),
                        "actual_offset_c": 0,
                        "proposed_offset_c": -4,
                        "state": "coast",
                        "reason": "Expensive block.",
                        "current_price_eur_kwh": "0.30",
                        "write_sent": False,
                    }
                ]
            },
            async_add_listener=Mock(),
        )
        extreme = SimpleNamespace(
            state={
                "decision_history": [
                    {
                        "observed_at": now.isoformat(),
                        "state": "clear",
                        "reason": "No block.",
                        "actions": [],
                        "write_sent": False,
                    }
                ]
            },
            async_add_listener=Mock(),
        )
        solar = SimpleNamespace(
            operation_history=[
                {
                    "at": (
                        now - timedelta(minutes=10)
                    ).isoformat(),
                    "operation": "acquire",
                    "phase": "verified",
                    "success": True,
                    "previous": {
                        "mode": "percent",
                        "configured": 100.0,
                    },
                    "confirmed": {
                        "mode": "percent",
                        "configured": 45.4,
                    },
                },
                {
                    "at": (
                        now - timedelta(minutes=5)
                    ).isoformat(),
                    "operation": "resume",
                    "phase": "reviewed",
                    "success": True,
                },
                {
                    "at": now.isoformat(),
                    "operation": "restore",
                    "phase": "post_dispatch_uncertain",
                    "success": False,
                    "error": "Synthetic uncertain restore.",
                },
            ],
            async_add_listener=Mock(),
        )
        sensor = self.sensor_module.EnergyControlActivitySensor(
            optimizer,
            floor,
            extreme,
            solar,
            "pricing",
        )

        attributes = sensor.extra_state_attributes
        self.assertEqual(attributes["verified_command_count"], 2)
        self.assertEqual(attributes["no_command_count"], 2)
        self.assertEqual(attributes["simulation_count"], 1)
        self.assertEqual(attributes["uncertain_command_count"], 1)
        self.assertEqual(attributes["review_required_count"], 1)
        self.assertEqual(
            [event["kind"] for event in attributes["events"]],
            [
                "no_command",
                "simulation",
                "no_command",
            ],
        )
        self.assertEqual(
            [event["kind"] for event in attributes["command_events"]],
            [
                "verified_command",
                "verified_command",
                "review_required",
            ],
        )
        resume_event = attributes["events"][-1]
        self.assertEqual(
            resume_event["change"],
            "Fresh-state reconciliation",
        )
        self.assertEqual(
            resume_event["result"],
            "No inverter command was sent.",
        )
        self.assertEqual(sensor.native_value, "review_required")
        self.assertEqual(
            attributes["command_events"][0][
                "decision_price_eur_kwh"
            ],
            0.25,
        )
        self.assertEqual(
            attributes["command_events"][0]["before_value"],
            50,
        )
        self.assertEqual(
            attributes["command_events"][0]["after_value"],
            55,
        )
        self.assertEqual(
            attributes["command_events"][0]["unit"],
            "°C",
        )

    async def test_energy_control_activity_preserves_audited_commands(self):
        now = self.now
        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "home_energy_control.jsonl"
            journal_path.write_text(
                json.dumps(
                    {
                        "event": "verified",
                        "observed_at": (
                            now - timedelta(hours=20)
                        ).isoformat(),
                        "operation": "heating_offset",
                        "previous": -2.0,
                        "requested": -6.0,
                        "confirmed": -6.0,
                        "request_id": "floor-command-inside-window",
                    }
                )
                + "\n"
            )
            simulations = [
                {
                    "observed_at": (
                        now - timedelta(minutes=30 - index)
                    ).isoformat(),
                    "state": "preheat" if index % 2 else "coast",
                    "write_sent": False,
                    "mode": "shadow",
                    "actual_offset_c": 0.0,
                    "proposed_offset_c": -4.0 if index % 2 else 4.0,
                    "reason": f"simulation {index}",
                    "current_price_eur_kwh": 0.1 + index / 1000,
                }
                for index in range(30)
            ]
            floor = SimpleNamespace(
                devices=SimpleNamespace(journal=journal_path),
                state={"decision_history": simulations},
            )
            activity = self.sensor_module.EnergyControlActivitySensor(
                optimizer=None,
                floor_shadow=floor,
                extreme_protection=None,
                solar_curtailment=None,
                entry_id="pricing",
            )
            audit_events, errors = activity._read_verified_audit_events(
                journal_path,
                now - timedelta(hours=24),
            )
            self.assertEqual(errors, [])
            activity._audit_events = audit_events

            attrs = activity.extra_state_attributes
            self.assertEqual(
                len(attrs["events"]),
                attrs["event_limit"],
            )
            self.assertTrue(
                any(
                    event.get("request_id") == "floor-command-inside-window"
                    for event in attrs["command_events"]
                )
            )
            self.assertFalse(
                any(
                    event.get("request_id") == "floor-command-inside-window"
                    for event in attrs["events"]
                )
            )
            self.assertEqual(attrs["verified_command_count"], 1)

    def test_energy_control_episodes_preserve_exact_duration_and_sources(self):
        now = self.now.astimezone(timezone.utc)
        first_start = now - timedelta(hours=4)
        first_end = first_start + timedelta(minutes=23, seconds=24)
        second_start = now - timedelta(hours=2)
        second_end = second_start + timedelta(minutes=22, seconds=24)
        optimizer = SimpleNamespace(
            mode="readiness_pilot",
            state={
                "readiness_pilot_started_at": (
                    now - timedelta(hours=8)
                ).isoformat(),
                "decision_history": [
                    {
                        "observed_at": first_start.isoformat(),
                        "actual_target_c": 50,
                        "proposed_target_c": 55,
                        "state": "readiness_charge",
                        "reason": "First verified readiness charge.",
                        "current_price_eur_kwh": "0.10",
                        "write_sent": True,
                    },
                    {
                        "observed_at": first_end.isoformat(),
                        "actual_target_c": 55,
                        "proposed_target_c": 50,
                        "state": "readiness_hold",
                        "reason": "First verified target restore.",
                        "current_price_eur_kwh": "0.12",
                        "write_sent": True,
                    },
                    {
                        "observed_at": second_start.isoformat(),
                        "actual_target_c": 50,
                        "proposed_target_c": 55,
                        "state": "readiness_charge",
                        "reason": "Second verified readiness charge.",
                        "current_price_eur_kwh": "0.09",
                        "write_sent": True,
                    },
                    {
                        "observed_at": second_end.isoformat(),
                        "actual_target_c": 55,
                        "proposed_target_c": 50,
                        "state": "readiness_hold",
                        "reason": "Second verified target restore.",
                        "current_price_eur_kwh": "0.11",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(minutes=30)
                        ).isoformat(),
                        "actual_target_c": 52,
                        "proposed_target_c": 52,
                        "state": "manual_override",
                        "reason": "Observed external target change.",
                        "current_price_eur_kwh": "0.15",
                        "write_sent": False,
                    },
                ],
            },
        )
        floor_start = now - timedelta(hours=3)
        floor_native = now - timedelta(hours=1, minutes=39)
        floor = SimpleNamespace(
            mode="active",
            state={
                "decision_history": [
                    {
                        "observed_at": floor_start.isoformat(),
                        "mode": "active",
                        "actual_offset_c": 0,
                        "proposed_offset_c": -4,
                        "state": "coast",
                        "reason": "Verified optimizer overlay.",
                        "current_price_eur_kwh": "0.30",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(minutes=10)
                        ).isoformat(),
                        "mode": "shadow",
                        "actual_offset_c": -2,
                        "proposed_offset_c": 4,
                        "state": "preheat",
                        "reason": "Counterfactual only.",
                        "current_price_eur_kwh": "0.08",
                        "write_sent": False,
                    },
                ],
                "native_schedule_transitions": [
                    {
                        "observed_at": floor_native.isoformat(),
                        "previous_offset_c": -4,
                        "native_offset_c": -2,
                        "classification": "accepted_schedule_transition",
                    }
                ],
            },
        )
        protection = SimpleNamespace(
            mode="armed",
            state={
                "decision_history": [
                    {
                        "observed_at": (
                            now - timedelta(minutes=20)
                        ).isoformat(),
                        "state": "hard_stop",
                        "reason": "Verified protection standby.",
                        "current_price_eur_kwh": "1.50",
                        "write_sent": True,
                        "actions": [
                            {
                                "load_id": "daikin_hot_water",
                                "operation": "standby",
                                "current_state": "on",
                                "proposed_state": "standby",
                            }
                        ],
                    }
                ]
            },
        )
        activity = self.sensor_module.EnergyControlActivitySensor(
            optimizer,
            floor,
            protection,
            None,
            "pricing",
        )

        attrs = activity.extra_state_attributes
        hot_water_charges = [
            episode
            for episode in attrs["action_episodes"]
            if episode["subsystem"] == "hot_water"
            and episode["after_value"] == 55
        ]
        self.assertEqual(
            [episode["duration_minutes"] for episode in hot_water_charges],
            [23.4, 22.4],
        )
        floor_episodes = [
            episode
            for episode in attrs["action_episodes"]
            if episode["subsystem"] == "floor_heating"
            and episode["unit"] == "°C"
        ]
        self.assertEqual(
            [episode["source"] for episode in floor_episodes],
            ["optimizer", "native_schedule"],
        )
        self.assertEqual(
            floor_episodes[0]["end"],
            floor_native.isoformat(),
        )
        self.assertEqual(
            {
                episode["source"]
                for episode in attrs["action_episodes"]
            },
            {
                "optimizer",
                "native_schedule",
                "manual_external",
            },
        )
        self.assertEqual(
            {
                episode["source"]
                for episode in attrs["status_episodes"]
            },
            {"protection"},
        )
        self.assertTrue(
            any(
                episode["operation"]
                == "daikin_hot_water:standby"
                for episode in attrs["status_episodes"]
            )
        )
        self.assertTrue(
            any(
                event["kind"] == "simulation"
                and event["system"] == "Floor heating"
                for event in attrs["events"]
            )
        )
        self.assertFalse(
            any(
                episode["operation"] == "preheat"
                and episode["after_value"] == 4
                for episode in attrs["action_episodes"]
            )
        )

    def test_energy_control_activity_retains_representative_full_day_actions(self):
        now = self.now.astimezone(timezone.utc)
        optimizer = SimpleNamespace(
            mode="safe",
            state={
                "decision_history": [
                    {
                        "observed_at": (
                            now - timedelta(hours=16)
                        ).isoformat(),
                        "actual_target_c": 50,
                        "proposed_target_c": 55,
                        "state": "readiness_charge",
                        "reason": "Morning readiness charge.",
                        "current_price_eur_kwh": "0.10",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(hours=15, minutes=15)
                        ).isoformat(),
                        "actual_target_c": 55,
                        "proposed_target_c": 50,
                        "state": "readiness",
                        "reason": "Morning readiness restore.",
                        "current_price_eur_kwh": "0.12",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(hours=5, minutes=15)
                        ).isoformat(),
                        "actual_target_c": 50,
                        "proposed_target_c": 45,
                        "state": "coast",
                        "reason": "Afternoon price coast.",
                        "current_price_eur_kwh": "0.34",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(hours=3, minutes=45)
                        ).isoformat(),
                        "actual_target_c": 45,
                        "proposed_target_c": 50,
                        "state": "recovery",
                        "reason": "Measured recovery.",
                        "current_price_eur_kwh": "0.24",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(hours=2)
                        ).isoformat(),
                        "actual_target_c": 50,
                        "proposed_target_c": 55,
                        "state": "readiness_final_topup",
                        "reason": "Evening readiness charge.",
                        "current_price_eur_kwh": "0.18",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(hours=1, minutes=15)
                        ).isoformat(),
                        "actual_target_c": 55,
                        "proposed_target_c": 50,
                        "state": "readiness",
                        "reason": "Evening readiness restore.",
                        "current_price_eur_kwh": "0.20",
                        "write_sent": True,
                    },
                ],
            },
        )
        floor = SimpleNamespace(
            mode="active",
            state={
                "decision_history": [
                    {
                        "observed_at": (
                            now - timedelta(hours=6)
                        ).isoformat(),
                        "actual_offset_c": -2,
                        "proposed_offset_c": -6,
                        "state": "coast",
                        "reason": "Floor peak shift.",
                        "current_price_eur_kwh": "0.28",
                        "write_sent": True,
                    },
                    {
                        "observed_at": (
                            now - timedelta(hours=4)
                        ).isoformat(),
                        "actual_offset_c": -6,
                        "proposed_offset_c": -2,
                        "state": "normal",
                        "reason": "Floor baseline restore.",
                        "current_price_eur_kwh": "0.16",
                        "write_sent": True,
                    },
                ],
                "native_schedule_transitions": [],
            },
        )
        activity = self.sensor_module.EnergyControlActivitySensor(
            optimizer,
            floor,
            None,
            None,
            "pricing",
        )
        activity._audit_events = [
            {
                "observed_at": (
                    now - timedelta(hours=30)
                ).isoformat(),
                "operation": "hot_water_enabled",
                "before_value": "off",
                "after_value": "on",
                "unit": None,
                "reason": "Persistent DHW circuit state.",
            },
            {
                "observed_at": (
                    now - timedelta(hours=30)
                ).isoformat(),
                "operation": "heating_enabled",
                "before_value": "off",
                "after_value": "on",
                "unit": None,
                "reason": "Persistent floor circuit state.",
            },
        ]

        attrs = activity.extra_state_attributes
        self.assertEqual(attrs["action_episode_count"], 8)
        self.assertEqual(attrs["action_episode_retained_count"], 8)
        self.assertEqual(attrs["action_episode_truncated_count"], 0)
        self.assertEqual(
            [episode["subsystem"] for episode in attrs["action_episodes"]],
            [
                "hot_water",
                "hot_water",
                "floor_heating",
                "hot_water",
                "floor_heating",
                "hot_water",
                "hot_water",
                "hot_water",
            ],
        )
        self.assertEqual(attrs["status_episode_count"], 2)
        self.assertTrue(
            all(
                episode["clipped_start"]
                for episode in attrs["status_episodes"]
            )
        )
        self.assertFalse(
            any(
                episode["operation"]
                in {"hot_water_enabled", "heating_enabled"}
                for episode in attrs["action_episodes"]
            )
        )

    def test_energy_control_episodes_clip_rolling_window_and_stay_bounded(self):
        self.now = datetime(
            2026,
            10,
            22,
            3,
            0,
            tzinfo=timezone.utc,
        )
        now = self.now
        records = [
            {
                "observed_at": (
                    now - timedelta(hours=30)
                ).isoformat(),
                "actual_target_c": 50,
                "proposed_target_c": 55,
                "state": "readiness_charge",
                "reason": "Episode crossing the rolling cutoff.",
                "current_price_eur_kwh": "0.12345",
                "write_sent": True,
            },
            {
                "observed_at": (
                    now - timedelta(hours=20)
                ).isoformat(),
                "actual_target_c": 55,
                "proposed_target_c": 50,
                "state": "readiness_hold",
                "reason": "Episode crossing the rolling cutoff.",
                "current_price_eur_kwh": "0.12345",
                "write_sent": True,
            },
        ]
        for index in range(40):
            observed_at = (
                now
                - timedelta(hours=19, minutes=30)
                + timedelta(minutes=30 * index)
            )
            before = 50 if index % 2 == 0 else 55
            after = 55 if index % 2 == 0 else 50
            records.append(
                {
                    "observed_at": observed_at.isoformat(),
                    "actual_target_c": before,
                    "proposed_target_c": after,
                    "state": "readiness_charge",
                    "reason": "Bounded rolling episode evidence.",
                    "current_price_eur_kwh": "0.12345",
                    "write_sent": True,
                }
            )
        optimizer = SimpleNamespace(
            mode="readiness_pilot",
            state={
                "readiness_pilot_started_at": (
                    now - timedelta(days=2)
                ).isoformat(),
                "decision_history": records,
            },
        )
        activity = self.sensor_module.EnergyControlActivitySensor(
            optimizer,
            None,
            None,
            None,
            "pricing",
        )
        activity._audit_events = [
            {
                "observed_at": (
                    now - timedelta(hours=30)
                ).isoformat(),
                "operation": "hot_water_enabled",
                "before_value": "off",
                "after_value": "on",
                "unit": None,
                "reason": "Persistent circuit evidence.",
            }
        ]

        attrs = activity.extra_state_attributes
        cutoff = now - timedelta(hours=24)
        self.assertLessEqual(
            len(attrs["action_episodes"]),
            attrs["action_episode_limit"],
        )
        self.assertTrue(attrs["action_episodes"][0]["clipped_start"])
        self.assertEqual(
            attrs["action_episodes"][0]["start"],
            cutoff.isoformat(),
        )
        self.assertGreater(attrs["action_episode_truncated_count"], 0)
        self.assertEqual(attrs["status_episode_count"], 1)
        self.assertEqual(attrs["status_episode_truncated_count"], 0)
        self.assertGreaterEqual(
            attrs["event_count"],
            attrs["event_retained_count"],
        )
        self.assertGreaterEqual(
            attrs["command_event_count"],
            attrs["command_event_retained_count"],
        )
        self.assertLess(len(json.dumps(attrs)), 16000)

    def test_solar_power_limit_sensors_use_equivalent_watts(self):
        controller = SimpleNamespace(
            attributes={
                "configured_limit_equivalent_w": 8000,
                "active_limit_equivalent_w": 6000,
                "requested_limit_equivalent_w": None,
                "qualified_proposed_limit_equivalent_w": 4000,
                "configured_limit_mode": "percent",
                "pmax_w": 8000,
            },
            async_add_listener=Mock(),
        )
        sensors = (
            self.sensor_module.SolarConfiguredPowerLimitSensor(
                controller,
                "pricing",
            ),
            self.sensor_module.SolarActivePowerLimitSensor(
                controller,
                "pricing",
            ),
            self.sensor_module.SolarControllerPowerRequestSensor(
                controller,
                "pricing",
            ),
            self.sensor_module.SolarQualifiedShadowProposalSensor(
                controller,
                "pricing",
            ),
        )
        self.assertEqual(
            [sensor.native_value for sensor in sensors],
            [8000.0, 6000.0, None, 4000.0],
        )
        self.assertEqual(
            [sensor.available for sensor in sensors],
            [True, True, False, True],
        )

    def test_solar_relevance_requires_daylight_and_fresh_output_gate(self):
        controller = SimpleNamespace(
            attributes={
                "sun_state": "below_horizon",
                "active_power_w": 6200,
                "active_power_fresh": True,
                "output_gate_w": 5000,
                "shadow_relevance_ready": False,
                "last_shadow_relevance_ready_at": (
                    "2026-10-22T09:41:00+00:00"
                ),
                "write_allowed": False,
                "grid_export_meter_available": False,
                "grid_export_meter_blocker": "Synthetic missing meter.",
            },
            async_add_listener=Mock(),
        )
        sensor = self.sensor_module.SolarRelevanceSensor(
            controller,
            "pricing",
        )
        self.assertEqual(sensor.native_value, "waiting_for_daylight")
        self.assertFalse(sensor.extra_state_attributes["ready"])
        self.assertEqual(
            sensor.extra_state_attributes[
                "last_shadow_relevance_ready_at"
            ],
            "2026-10-22T09:41:00+00:00",
        )

        controller.attributes["sun_state"] = "above_horizon"
        controller.attributes["active_power_w"] = 4200
        self.assertEqual(sensor.native_value, "waiting_for_output")

        controller.attributes["active_power_w"] = 6200
        controller.attributes["active_power_fresh"] = False
        self.assertEqual(sensor.native_value, "waiting_for_fresh_output")

        controller.attributes["active_power_fresh"] = True
        self.assertEqual(sensor.native_value, "qualifying")
        controller.attributes["shadow_relevance_ready"] = True
        self.assertEqual(sensor.native_value, "shadow_ready")
        self.assertFalse(sensor.extra_state_attributes["ready"])
        self.assertTrue(sensor.extra_state_attributes["shadow_ready"])
        self.assertEqual(
            sensor.extra_state_attributes["write_authority"],
            "shadow_only",
        )

    async def test_owner_activation_and_floor_controls_delegate_explicitly(self):
        select_module = importlib.import_module(COMPONENT + ".select")
        button_module = importlib.import_module(COMPONENT + ".button")
        floor = SimpleNamespace(
            mode="shadow",
            state={
                "fault": None,
                "manual_override": None,
                "ownership": None,
            },
            async_set_mode=AsyncMock(),
            async_activate_owner_approved=AsyncMock(),
            async_resume_after_review=AsyncMock(),
            async_return_to_baseline=AsyncMock(),
        )
        optimizer = SimpleNamespace(
            state={"fault": None},
            async_activate_full_control=AsyncMock(),
        )

        mode_select = select_module.FloorHeatingModeSelect(
            floor,
            "pricing",
        )
        self.assertEqual(
            mode_select.options,
            [
                "Paused",
                "Shadow (no writes)",
                "Active price shift",
            ],
        )
        await mode_select.async_select_option("Active price shift")
        floor.async_set_mode.assert_awaited_once_with("active")

        activate_floor = button_module.FloorHeatingActivateButton(
            floor,
            "pricing",
        )
        self.assertTrue(activate_floor.available)
        await activate_floor.async_press()
        floor.async_activate_owner_approved.assert_awaited_once()

        resume_floor = button_module.FloorHeatingResumeButton(
            floor,
            "pricing",
        )
        floor.state["manual_override"] = "External change."
        self.assertTrue(resume_floor.available)
        await resume_floor.async_press()
        floor.async_resume_after_review.assert_awaited_once()

        baseline_floor = button_module.FloorHeatingBaselineButton(
            floor,
            "pricing",
        )
        floor.state["ownership"] = {"target_offset": 4}
        self.assertTrue(baseline_floor.available)
        await baseline_floor.async_press()
        floor.async_return_to_baseline.assert_awaited_once()

        activate_dhw = button_module.DhwOptimizerFullControlButton(
            optimizer,
            "pricing",
        )
        self.assertTrue(activate_dhw.available)
        await activate_dhw.async_press()
        optimizer.async_activate_full_control.assert_awaited_once()

    def test_energy_control_authority_sensors_distinguish_runtime_authority(self):
        listener = Mock()
        activity = self.sensor_module.EnergyControlActivitySensor(
            SimpleNamespace(
                mode="safe_target_shift",
                decision=SimpleNamespace(state="coast", reason="Price shift."),
                state={"fault": None, "decision_history": []},
                async_add_listener=listener,
            ),
            SimpleNamespace(
                mode="active",
                decision=SimpleNamespace(
                    state="schedule_learning_hold",
                    reason="Native schedule changed.",
                ),
                state={"fault": None, "decision_history": []},
                async_add_listener=listener,
            ),
            SimpleNamespace(
                mode="shadow",
                decision=SimpleNamespace(state="clear", reason="No block."),
                state={"fault": None, "decision_history": []},
                async_add_listener=listener,
            ),
            SimpleNamespace(
                mode="armed",
                decision=SimpleNamespace(
                    state="persistence_fault",
                    reason="Review required.",
                ),
                state={"fault": "Synthetic fault."},
                operation_history=[],
                async_add_listener=listener,
            ),
            "pricing",
        )
        values = {
            key: self.sensor_module.EnergyControlAuthoritySensor(
                activity,
                key,
                name,
                "mdi:shield-check",
                "pricing",
            ).native_value
            for key, name in (
                ("hot_water", "Hot water authority"),
                ("floor_heating", "Floor heating authority"),
                ("extreme_protection", "Extreme protection authority"),
                ("solar", "Solar authority"),
            )
        }
        self.assertEqual(
            values,
            {
                "hot_water": "live",
                "floor_heating": "learning",
                "extreme_protection": "shadow",
                "solar": "fault",
            },
        )
        self.assertEqual(
            activity._controller_authority(
                SimpleNamespace(
                    mode="armed",
                    decision=SimpleNamespace(state="unavailable"),
                    state={"fault": None},
                ),
                live_modes=("armed",),
                shadow_modes=("shadow",),
            ),
            "unavailable",
        )

    async def test_solar_only_entry_loads_sensor_select_and_button_platforms(self):
        callbacks = []
        old_coordinator = self.coordinator
        self.pricing_entry = entry(
            DOMAIN,
            "pricing",
            {
                **self.pricing_entry.data,
                "solar_config_entry": "solar",
            },
            ConfigEntryState.SETUP_IN_PROGRESS,
        )
        self.entries["pricing"] = self.pricing_entry
        self.entries["solar"] = entry(
            "huawei_solar",
            "solar",
            {"enable_parameter_configuration": True},
        )
        devices = SimpleNamespace(
            solar_entry="solar",
            daikin_entry=None,
            solar_write_available=Mock(return_value=False),
        )
        solar = SimpleNamespace(
            async_start=AsyncMock(),
            async_stop=AsyncMock(),
            async_add_listener=Mock(),
            attributes={},
            operation_history=[],
        )
        try:
            with (
                patch.object(
                    self.integration,
                    "EnergyDevices",
                    return_value=devices,
                ),
                patch.object(
                    self.integration,
                    "SolarCurtailment",
                    return_value=solar,
                ),
                patch.object(
                    ConfigEntry,
                    "async_on_unload",
                    side_effect=lambda callback: callbacks.append(callback),
                ),
            ):
                self.assertTrue(
                    await self.integration.async_setup_entry(
                        self.hass, self.pricing_entry
                    )
                )
            self.coordinator = self.pricing_entry.runtime_data
            self.forward.assert_awaited_once_with(
                self.pricing_entry,
                ["sensor", "select", "button"],
            )
            runtime = self.hass.data[DOMAIN]["pricing"]
            self.assertIsNone(runtime["optimizer"])
            self.assertIs(runtime["solar_curtailment"], solar)
            solar.async_start.assert_awaited_once()
            select_module = importlib.import_module(COMPONENT + ".select")
            button_module = importlib.import_module(COMPONENT + ".button")
            sensor_module = importlib.import_module(COMPONENT + ".sensor")
            select_entities = []
            button_entities = []
            sensor_entities = []
            await select_module.async_setup_entry(
                self.hass,
                self.pricing_entry,
                select_entities.extend,
            )
            await button_module.async_setup_entry(
                self.hass,
                self.pricing_entry,
                button_entities.extend,
            )
            await sensor_module.async_setup_entry(
                self.hass,
                self.pricing_entry,
                sensor_entities.extend,
            )
            self.assertEqual(
                [
                    entity.__class__.__name__
                    for entity in select_entities
                ],
                ["SolarCurtailmentModeSelect"],
            )
            self.assertEqual(
                [
                    entity.__class__.__name__
                    for entity in button_entities
                ],
                [
                    "SolarCurtailmentCommissionButton",
                    "SolarCurtailmentResumeButton",
                ],
            )
            self.assertIn(
                "SolarCurtailmentSensor",
                {
                    entity.__class__.__name__
                    for entity in sensor_entities
                },
            )
            self.assertIn(
                "SolarCurtailmentOpportunitySensor",
                {
                    entity.__class__.__name__
                    for entity in sensor_entities
                },
            )
            self.assertIn(
                "SolarRelevanceSensor",
                {
                    entity.__class__.__name__
                    for entity in sensor_entities
                },
            )
            self.assertEqual(
                sum(
                    entity.__class__.__name__.endswith(
                        "PowerLimitSensor"
                    )
                    or entity.__class__.__name__
                    == "SolarControllerPowerRequestSensor"
                    for entity in sensor_entities
                ),
                3,
            )
            self.assertTrue(
                await self.integration.async_unload_entry(
                    self.hass, self.pricing_entry
                )
            )
            solar.async_stop.assert_awaited_once()
        finally:
            for callback in reversed(callbacks):
                result = callback()
                if inspect.isawaitable(result):
                    await result
            await old_coordinator.async_shutdown()

    async def test_refused_platform_unload_keeps_runtime_running(self):
        stop = AsyncMock()
        runtime = {
            "platforms": ["sensor", "select", "button"],
            "_cleanup_lock": asyncio.Lock(),
            "_cleanup_callbacks": [],
            "_started_runtimes": [("solar curtailment", stop)],
            "_callbacks_cleaned": False,
            "_runtimes_stopped": False,
            "_data_removed": False,
        }
        self.hass.data.setdefault(DOMAIN, {})[
            self.pricing_entry.entry_id
        ] = runtime
        self.hass.config_entries.async_unload_platforms.return_value = False
        try:
            self.assertFalse(
                await self.integration.async_unload_entry(
                    self.hass,
                    self.pricing_entry,
                )
            )
            stop.assert_not_awaited()
            self.assertIs(
                self.hass.data[DOMAIN][self.pricing_entry.entry_id],
                runtime,
            )
            self.assertFalse(runtime["_runtimes_stopped"])
        finally:
            self.hass.config_entries.async_unload_platforms.return_value = True
            await self.integration._async_cleanup_runtime(
                self.hass,
                self.pricing_entry,
                runtime,
            )

    async def test_partial_platform_forward_failure_stops_started_runtimes(self):
        callbacks = []
        created_coordinator = None
        self.pricing_entry = entry(
            DOMAIN,
            "pricing",
            {
                **self.pricing_entry.data,
                "solar_config_entry": "solar",
            },
            ConfigEntryState.SETUP_IN_PROGRESS,
        )
        self.entries["pricing"] = self.pricing_entry
        self.entries["solar"] = entry(
            "huawei_solar",
            "solar",
            {"enable_parameter_configuration": True},
        )
        forecast = SimpleNamespace(
            async_load=AsyncMock(),
            async_shutdown=AsyncMock(),
        )
        devices = SimpleNamespace(
            solar_entry="solar",
            daikin_entry=None,
            solar_write_available=Mock(return_value=False),
        )
        solar = SimpleNamespace(
            async_start=AsyncMock(),
            async_stop=AsyncMock(),
        )
        self.forward.side_effect = HomeAssistantError(
            "Synthetic platform forwarding failure"
        )
        try:
            with (
                patch.object(
                    self.integration,
                    "HouseholdPriceForecastCoordinator",
                    return_value=forecast,
                ),
                patch.object(
                    self.integration,
                    "EnergyDevices",
                    return_value=devices,
                ),
                patch.object(
                    self.integration,
                    "SolarCurtailment",
                    return_value=solar,
                ),
                patch.object(
                    ConfigEntry,
                    "async_on_unload",
                    side_effect=lambda callback: callbacks.append(callback),
                ),
            ):
                with self.assertRaisesRegex(
                    HomeAssistantError, "forwarding failure"
                ):
                    await self.integration.async_setup_entry(
                        self.hass, self.pricing_entry
                    )
            created_coordinator = self.pricing_entry.runtime_data
            forecast.async_load.assert_awaited_once()
            forecast.async_shutdown.assert_awaited_once()
            solar.async_start.assert_awaited_once()
            solar.async_stop.assert_awaited_once()
            self.hass.config_entries.async_unload_platforms.assert_awaited_once_with(
                self.pricing_entry,
                ["sensor", "select", "button"],
            )
            self.assertNotIn(
                "pricing", self.hass.data.get(DOMAIN, {})
            )
        finally:
            for callback in reversed(callbacks):
                result = callback()
                if inspect.isawaitable(result):
                    await result
            if (
                created_coordinator is not None
                and created_coordinator is not self.coordinator
            ):
                await created_coordinator.async_shutdown()

    async def test_extreme_protection_starts_before_heating_optimizers(self):
        callbacks = []
        old_coordinator = self.coordinator
        self.pricing_entry = entry(
            DOMAIN,
            "pricing",
            {
                **self.pricing_entry.data,
                "daikin_config_entry": "daikin",
            },
            ConfigEntryState.SETUP_IN_PROGRESS,
        )
        self.entries["pricing"] = self.pricing_entry
        self.entries["daikin"] = entry(
            "daikin_altherma",
            "daikin",
            {},
        )
        starts = []

        def component(name, **extra):
            async def start():
                starts.append(name)

            return SimpleNamespace(
                async_start=AsyncMock(side_effect=start),
                async_stop=AsyncMock(),
                **extra,
            )

        optimizer = component(
            "optimizer",
            set_extreme_protection=Mock(),
        )
        floor = component(
            "floor",
            set_extreme_protection=Mock(),
        )
        extreme = component("extreme")
        forecast = SimpleNamespace(
            async_load=AsyncMock(),
            async_shutdown=AsyncMock(),
        )
        devices = SimpleNamespace(
            solar_entry=None,
            daikin_entry="daikin",
        )
        try:
            with (
                patch.object(
                    self.integration,
                    "HouseholdPriceForecastCoordinator",
                    return_value=forecast,
                ),
                patch.object(
                    self.integration,
                    "EnergyDevices",
                    return_value=devices,
                ),
                patch.object(
                    self.integration,
                    "DhwOptimizer",
                    return_value=optimizer,
                ),
                patch.object(
                    self.integration,
                    "FloorHeatingShadow",
                    return_value=floor,
                ),
                patch.object(
                    self.integration,
                    "ExtremePriceProtection",
                    return_value=extreme,
                ),
                patch.object(
                    ConfigEntry,
                    "async_on_unload",
                    side_effect=lambda callback: callbacks.append(callback),
                ),
            ):
                self.assertTrue(
                    await self.integration.async_setup_entry(
                        self.hass,
                        self.pricing_entry,
                    )
                )
            self.coordinator = self.pricing_entry.runtime_data
            self.assertEqual(starts, ["extreme", "optimizer", "floor"])
            optimizer.set_extreme_protection.assert_called_once_with(extreme)
            floor.set_extreme_protection.assert_called_once_with(extreme)
            self.assertTrue(
                await self.integration.async_unload_entry(
                    self.hass,
                    self.pricing_entry,
                )
            )
        finally:
            for callback in reversed(callbacks):
                result = callback()
                if inspect.isawaitable(result):
                    await result
            await old_coordinator.async_shutdown()

    async def test_api_registration_failure_cleans_platforms_and_runtime(self):
        callbacks = []
        created_coordinator = None
        self.pricing_entry = entry(
            DOMAIN,
            "pricing",
            {
                **self.pricing_entry.data,
                "solar_config_entry": "solar",
            },
            ConfigEntryState.SETUP_IN_PROGRESS,
        )
        self.entries["pricing"] = self.pricing_entry
        self.entries["solar"] = entry(
            "huawei_solar",
            "solar",
            {"enable_parameter_configuration": True},
        )
        forecast = SimpleNamespace(
            async_load=AsyncMock(),
            async_shutdown=AsyncMock(),
        )
        devices = SimpleNamespace(
            solar_entry="solar",
            daikin_entry=None,
            solar_write_available=Mock(return_value=False),
        )
        solar = SimpleNamespace(
            async_start=AsyncMock(),
            async_stop=AsyncMock(),
        )
        unregister = Mock()
        try:
            with (
                patch.object(
                    self.integration,
                    "HouseholdPriceForecastCoordinator",
                    return_value=forecast,
                ),
                patch.object(
                    self.integration,
                    "EnergyDevices",
                    return_value=devices,
                ),
                patch.object(
                    self.integration,
                    "SolarCurtailment",
                    return_value=solar,
                ),
                patch.object(
                    self.integration.ha_llm,
                    "async_register_api",
                    side_effect=[
                        unregister,
                        HomeAssistantError(
                            "Synthetic API registration failure"
                        ),
                    ],
                ),
                patch.object(
                    ConfigEntry,
                    "async_on_unload",
                    side_effect=lambda callback: callbacks.append(callback),
                ),
            ):
                with self.assertRaisesRegex(
                    HomeAssistantError, "API registration failure"
                ):
                    await self.integration.async_setup_entry(
                        self.hass, self.pricing_entry
                    )
            created_coordinator = self.pricing_entry.runtime_data
            self.forward.assert_awaited_once_with(
                self.pricing_entry,
                ["sensor", "select", "button"],
            )
            self.hass.config_entries.async_unload_platforms.assert_awaited_once_with(
                self.pricing_entry,
                ["sensor", "select", "button"],
            )
            unregister.assert_called_once()
            forecast.async_shutdown.assert_awaited_once()
            solar.async_stop.assert_awaited_once()
            self.assertNotIn(
                "pricing", self.hass.data.get(DOMAIN, {})
            )
        finally:
            for callback in reversed(callbacks):
                result = callback()
                if inspect.isawaitable(result):
                    await result
            if (
                created_coordinator is not None
                and created_coordinator is not self.coordinator
            ):
                await created_coordinator.async_shutdown()


if __name__ == "__main__":
    unittest.main()
