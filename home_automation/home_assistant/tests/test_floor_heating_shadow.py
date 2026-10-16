"""Floor-heating Shadow policy and runtime tests using real HA APIs."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_component as base


setUpModule = base.setUpModule
tearDownModule = base.tearDownModule


class FloorHeatingShadowTests(base.HAComponentTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.module = importlib.import_module(
            base.COMPONENT + ".floor_heating_shadow"
        )
        self.sensor_module = importlib.import_module(base.COMPONENT + ".sensor")
        self.policy = self.module.FloorHeatingPolicy()

    def intervals(self, start: datetime, hours: int = 48):
        values = ["0.20"] * (hours * 4)
        for index in range(48, 56):
            if index < len(values):
                values[index] = "0.10"
        for index in range(72, 80):
            if index < len(values):
                values[index] = "0.40"
        return tuple(
            self.module.PriceInterval(
                start + index * timedelta(minutes=15),
                start + (index + 1) * timedelta(minutes=15),
                Decimal(value),
            )
            for index, value in enumerate(values)
        )

    def observation(self, **changes):
        values = {
            "indoor_temperature": 20.5,
            "outdoor_temperature": 8.0,
            "leaving_water_temperature": 31.0,
            "actual_offset": 0.0,
            "enabled": True,
            "override_reason": None,
        }
        values.update(changes)
        return self.module.FloorHeatingObservation(**values)

    def test_policy_distinguishes_native_range_from_candidate_envelope(self):
        self.assertEqual(self.policy.minimum_offset, -2)
        self.assertEqual(self.policy.neutral_offset, 0)
        self.assertEqual(self.policy.preheat_offset, 1)
        self.assertEqual(self.module.FLOOR_NATIVE_MIN_OFFSET, -10)
        self.assertEqual(self.module.FLOOR_NATIVE_MAX_OFFSET, 10)

    def test_selects_cheap_before_peak_pair_and_simulates_bounded_offsets(self):
        start = datetime.fromisoformat("2026-10-17T00:00:00+00:00")
        prices = self.intervals(start)
        initial = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(),
            intervals=prices,
        )
        self.assertTrue(initial.data_complete)
        self.assertTrue(initial.price_gate)
        self.assertEqual(initial.plan.cheap_window.start, start + timedelta(hours=12))
        self.assertEqual(initial.plan.peak_window.start, start + timedelta(hours=18))
        self.assertEqual(initial.state, "hold")

        preheat = self.module.plan_floor_heating(
            now=start + timedelta(hours=12),
            policy=self.policy,
            observation=self.observation(actual_offset=-2),
            intervals=prices,
            planned=initial.plan,
        )
        self.assertEqual(preheat.state, "preheat")
        self.assertEqual(preheat.proposed_offset, 0)
        self.assertEqual(preheat.aggressive_candidate_offset, 1)

        coast = self.module.plan_floor_heating(
            now=start + timedelta(hours=18),
            policy=self.policy,
            observation=self.observation(actual_offset=0),
            intervals=prices,
            planned=initial.plan,
        )
        self.assertEqual(coast.state, "coast")
        self.assertEqual(coast.proposed_offset, -2)

    def test_comfort_and_dhw_priority_override_price_candidate(self):
        start = datetime.fromisoformat("2026-10-17T00:00:00+00:00")
        prices = self.intervals(start)
        initial = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(),
            intervals=prices,
        )
        comfort = self.module.plan_floor_heating(
            now=start + timedelta(hours=18),
            policy=self.policy,
            observation=self.observation(indoor_temperature=20),
            intervals=prices,
            planned=initial.plan,
        )
        self.assertEqual(comfort.state, "comfort_guard")
        self.assertEqual(comfort.proposed_offset, 0)

        deferred = self.module.plan_floor_heating(
            now=start + timedelta(hours=12),
            policy=self.policy,
            observation=self.observation(),
            intervals=prices,
            planned=initial.plan,
            dhw_state="preheat",
            dhw_proposed_target=55,
        )
        self.assertEqual(deferred.state, "deferred")
        self.assertTrue(deferred.compressor_conflict)
        self.assertEqual(deferred.proposed_offset, 0)

    def test_missing_prices_and_external_offsets_never_create_actions(self):
        start = datetime.fromisoformat("2026-10-17T00:00:00+00:00")
        incomplete = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(),
            intervals=self.intervals(start, hours=12),
        )
        self.assertEqual(incomplete.state, "unavailable")
        self.assertFalse(incomplete.data_complete)

        external = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(actual_offset=-5),
            intervals=self.intervals(start),
        )
        self.assertEqual(external.state, "yield")
        self.assertEqual(external.proposed_offset, -5)
        self.assertIn("Shadow envelope", external.reason)

    def test_room_temperature_source_converts_fahrenheit(self):
        self.hass.states.async_set(
            "sensor.office_temperature",
            "68",
            {"unit_of_measurement": "°F"},
        )
        value, updated, reason = self.module.read_room_temperature_source(
            self.hass,
            "sensor.office_temperature",
            maximum_age=None,
        )
        self.assertAlmostEqual(value, 20)
        self.assertIsNotNone(updated)
        self.assertIsNone(reason)

    def test_room_temperature_source_rejects_stale_measurement(self):
        self.hass.states.async_set(
            "sensor.office_temperature",
            "21",
            {"unit_of_measurement": "°C"},
        )
        state = self.hass.states.get("sensor.office_temperature")
        self.assertIsNotNone(state)
        value, updated, reason = self.module.read_room_temperature_source(
            self.hass,
            "sensor.office_temperature",
            now=state.last_updated + timedelta(minutes=31),
        )
        self.assertIsNone(value)
        self.assertEqual(updated, state.last_reported)
        self.assertIn("stale", reason)

    def _runtime_status(self):
        return {
            "heating": {
                "states": {
                    name: False
                    for name in importlib.import_module(
                        base.COMPONENT + ".controls"
                    ).FLAGS
                },
                "operations": {
                    "OperationMode": "heating",
                    "Power": "on",
                    "LeavingWaterTemperatureOffsetHeating": 0,
                },
                "sensors": {
                    "IndoorTemperature": 12,
                    "OutdoorTemperature": 8,
                    "LeavingWaterTemperatureCurrent": 31,
                },
            },
            "control_mode": "LWT control",
        }

    def _runtime_entry(self, room_entities):
        return SimpleNamespace(
            entry_id=self.pricing_entry.entry_id,
            options={"floor_indoor_temperature_entities": room_entities},
        )

    async def test_runtime_uses_coldest_room_and_exposes_numeric_sensor(self):
        await self.refresh()
        self.hass.states.async_set(
            "climate.living_room",
            "heat",
            {"current_temperature": 22.4, "temperature_unit": "°C"},
        )
        self.hass.states.async_set(
            "sensor.bedroom_temperature",
            "21.7",
            {"unit_of_measurement": "°C"},
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=self._runtime_status()),
            control_daikin=AsyncMock(),
        )
        entry = self._runtime_entry(
            ["climate.living_room", "sensor.bedroom_temperature"]
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass, entry, self.coordinator, devices, None
        )
        await shadow.async_start()
        try:
            self.assertEqual(shadow.decision.indoor_temperature, 21.7)
            self.assertEqual(
                shadow.attributes["indoor_temperature_values_c"],
                {
                    "climate.living_room": 22.4,
                    "sensor.bedroom_temperature": 21.7,
                },
            )
            self.assertEqual(
                shadow.attributes["daikin_indoor_temperature_c"], 12
            )
            self.assertEqual(
                shadow.attributes["daikin_indoor_temperature_role"],
                "garage_diagnostic_only",
            )
            room_sensor = self.sensor_module.FloorRoomTemperatureSensor(
                shadow, entry.entry_id
            )
            self.assertTrue(room_sensor.available)
            self.assertEqual(room_sensor.native_value, 21.7)
            self.assertEqual(
                room_sensor.extra_state_attributes["daikin_garage_temperature_c"],
                12,
            )
            devices.control_daikin.assert_not_awaited()
        finally:
            await shadow.async_stop()

    async def test_missing_room_source_is_unavailable_without_garage_fallback(self):
        await self.refresh()
        self.hass.states.async_set(
            "climate.living_room",
            "heat",
            {"current_temperature": 22.4, "temperature_unit": "°C"},
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=self._runtime_status()),
            control_daikin=AsyncMock(),
        )
        entry = self._runtime_entry(
            ["climate.living_room", "sensor.missing_temperature"]
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass, entry, self.coordinator, devices, None
        )
        await shadow.async_start()
        try:
            self.assertEqual(shadow.decision.state, "unavailable")
            self.assertIsNone(shadow.decision.indoor_temperature)
            self.assertIn("missing", shadow.decision.reason)
            self.assertEqual(
                shadow.attributes["daikin_indoor_temperature_c"], 12
            )
            devices.control_daikin.assert_not_awaited()
        finally:
            await shadow.async_stop()

    async def test_runtime_and_sensor_are_strictly_zero_write(self):
        await self.refresh()
        self.hass.states.async_set(
            "sensor.room_temperature",
            "20.5",
            {"unit_of_measurement": "°C"},
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=self._runtime_status()),
            control_daikin=AsyncMock(),
        )
        entry = self._runtime_entry(["sensor.room_temperature"])
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            entry,
            self.coordinator,
            devices,
            None,
        )
        await shadow.async_start()
        try:
            devices.control_daikin.assert_not_awaited()
            self.assertFalse(shadow.attributes["write_allowed"])
            self.assertTrue(shadow.attributes["shadow_zero_writes"])
            self.assertEqual(shadow.attributes["native_minimum_offset_c"], -10)
            self.assertEqual(shadow.attributes["native_maximum_offset_c"], 10)
            sensor = self.sensor_module.FloorHeatingShadowSensor(
                shadow, entry.entry_id
            )
            self.assertEqual(sensor.native_value, shadow.decision.state)
            self.assertEqual(
                sensor.extra_state_attributes["confirmed_writes"], 0
            )
        finally:
            await shadow.async_stop()


if __name__ == "__main__":
    unittest.main()
