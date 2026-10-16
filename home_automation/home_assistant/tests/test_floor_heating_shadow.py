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

    async def test_runtime_and_sensor_are_strictly_zero_write(self):
        await self.refresh()
        flags = {
            name: False
            for name in importlib.import_module(base.COMPONENT + ".controls").FLAGS
        }
        status = {
            "heating": {
                "states": flags,
                "operations": {
                    "OperationMode": "heating",
                    "Power": "on",
                    "LeavingWaterTemperatureOffsetHeating": 0,
                },
                "sensors": {
                    "IndoorTemperature": 20.5,
                    "OutdoorTemperature": 8,
                    "LeavingWaterTemperatureCurrent": 31,
                },
            },
            "control_mode": "LWT control",
        }
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=status),
            control_daikin=AsyncMock(),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self.pricing_entry,
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
                shadow, self.pricing_entry.entry_id
            )
            self.assertEqual(sensor.native_value, shadow.decision.state)
            self.assertEqual(
                sensor.extra_state_attributes["confirmed_writes"], 0
            )
        finally:
            await shadow.async_stop()


if __name__ == "__main__":
    unittest.main()
