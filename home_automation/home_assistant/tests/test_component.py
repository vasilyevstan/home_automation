"""Run against real HA in an isolated environment, never the MCP environment."""

from __future__ import annotations

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
from unittest.mock import AsyncMock, patch
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
            lambda: observed.append(self.coordinator.data.contains(self.now))
        )
        self.now = data.valid_until
        with patch.object(self.coordinator, "async_request_refresh", new=AsyncMock()):
            await self.coordinator.async_quarter_changed(self.now)
        self.assertEqual(observed, [False])
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
        self.assertEqual(len(instance.tools), 1)
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


if __name__ == "__main__":
    unittest.main()
