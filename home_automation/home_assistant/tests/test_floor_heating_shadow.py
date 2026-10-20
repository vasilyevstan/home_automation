"""Floor-heating Shadow policy and runtime tests using real HA APIs."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import importlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_component as base


setUpModule = base.setUpModule
tearDownModule = base.tearDownModule


class FloorHeatingShadowTests(base.HAComponentTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.module = importlib.import_module(
            base.COMPONENT + ".floor_heating_shadow"
        )
        self.const = importlib.import_module(base.COMPONENT + ".const")
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

    def delivery_day_intervals(self, delivery_day: date):
        start, end = self.module.market_bounds(delivery_day)
        count = int((end - start) / timedelta(minutes=15))
        values = ["0.20"] * count
        for index in range(40, 48):
            values[index] = "0.10"
        for index in range(64, 72):
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
        self.assertEqual(self.policy.minimum_offset, -6)
        self.assertEqual(self.policy.neutral_offset, 0)
        self.assertEqual(self.policy.preheat_offset, 4)
        self.assertEqual(self.policy.shift_delta, 4)
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
        self.assertEqual(preheat.proposed_offset, 2)
        self.assertIsNone(preheat.aggressive_candidate_offset)

        coast = self.module.plan_floor_heating(
            now=start + timedelta(hours=18),
            policy=self.policy,
            observation=self.observation(actual_offset=0),
            intervals=prices,
            planned=initial.plan,
        )
        self.assertEqual(coast.state, "coast")
        self.assertEqual(coast.proposed_offset, -4)

    def test_four_degree_overlay_is_bounded_and_warm_room_suppresses_preheat(self):
        start = datetime.fromisoformat("2026-10-17T00:00:00+00:00")
        prices = self.intervals(start)
        initial = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(),
            intervals=prices,
        )
        bounded_preheat = self.module.plan_floor_heating(
            now=start + timedelta(hours=12),
            policy=self.policy,
            observation=self.observation(actual_offset=2),
            intervals=prices,
            planned=initial.plan,
        )
        self.assertEqual(bounded_preheat.proposed_offset, 4)

        bounded_coast = self.module.plan_floor_heating(
            now=start + timedelta(hours=18),
            policy=self.policy,
            observation=self.observation(actual_offset=-4),
            intervals=prices,
            planned=initial.plan,
        )
        self.assertEqual(bounded_coast.proposed_offset, -6)

        warm = self.module.plan_floor_heating(
            now=start + timedelta(hours=12),
            policy=self.policy,
            observation=self.observation(
                actual_offset=-2,
                indoor_temperature=21,
            ),
            intervals=prices,
            planned=initial.plan,
        )
        self.assertEqual(warm.state, "preheat_limited")
        self.assertEqual(warm.proposed_offset, -2)

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

        satisfied = self.module.plan_floor_heating(
            now=start + timedelta(hours=12),
            policy=self.policy,
            observation=self.observation(),
            intervals=prices,
            planned=initial.plan,
            dhw_state="readiness",
            dhw_proposed_target=50,
        )
        self.assertNotEqual(satisfied.state, "deferred")
        self.assertFalse(satisfied.compressor_conflict)

        readiness_charge = self.module.plan_floor_heating(
            now=start + timedelta(hours=12),
            policy=self.policy,
            observation=self.observation(),
            intervals=prices,
            planned=initial.plan,
            dhw_state="readiness_charge",
            dhw_proposed_target=55,
        )
        self.assertEqual(readiness_charge.state, "deferred")
        self.assertTrue(readiness_charge.compressor_conflict)

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
            observation=self.observation(actual_offset=-7),
            intervals=self.intervals(start),
        )
        self.assertEqual(external.state, "yield")
        self.assertEqual(external.proposed_offset, -7)
        self.assertIn("optimizer", external.reason)

    def test_complete_current_delivery_day_does_not_require_partial_tomorrow(self):
        start = datetime.fromisoformat("2026-10-17T00:00:00+00:00")
        decision = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(),
            intervals=self.intervals(start, hours=21),
        )
        self.assertTrue(decision.data_complete)
        self.assertIsNotNone(decision.plan)
        self.assertEqual(
            decision.plan.cheap_window.start,
            start + timedelta(hours=12),
        )
        self.assertEqual(
            decision.plan.peak_window.start,
            start + timedelta(hours=18),
        )

    def test_delivery_day_completeness_is_dst_aware(self):
        for delivery_day, expected_quarters in (
            (date(2026, 3, 29), 92),
            (date(2026, 10, 25), 100),
        ):
            with self.subTest(delivery_day=delivery_day):
                prices = self.delivery_day_intervals(delivery_day)
                self.assertEqual(len(prices), expected_quarters)
                decision = self.module.plan_floor_heating(
                    now=prices[0].start,
                    policy=self.policy,
                    observation=self.observation(),
                    intervals=prices,
                )
                self.assertTrue(decision.data_complete)
                self.assertIsNotNone(decision.plan)
                self.assertEqual(
                    decision.plan.cheap_window.start.astimezone(
                        self.module.TALLINN
                    ).date(),
                    delivery_day,
                )
                self.assertEqual(
                    decision.plan.peak_window.start.astimezone(
                        self.module.TALLINN
                    ).date(),
                    delivery_day,
                )

    def test_external_hvac_contaminates_in_progress_candidate_evidence(self):
        start = datetime.fromisoformat("2026-10-17T00:00:00+00:00")
        prices = self.intervals(start)
        initial = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(),
            intervals=prices,
        )
        preheat = self.module.plan_floor_heating(
            now=initial.plan.cheap_window.start,
            policy=self.policy,
            observation=self.observation(actual_offset=-2),
            intervals=prices,
            planned=initial.plan,
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry(["climate.bedroom"]),
            self.coordinator,
            SimpleNamespace(),
            None,
        )
        shadow.room_temperature_details[
            "thermal_response_evidence_eligible"
        ] = True
        shadow._update_candidate_evidence(
            initial.plan.cheap_window.start,
            preheat,
        )
        self.assertEqual(shadow.state["candidate_cycles"], 0)
        self.assertIsNotNone(shadow.state["candidate_cycle_peak_end"])

        influenced = self.module.plan_floor_heating(
            now=initial.plan.cheap_window.start + timedelta(minutes=15),
            policy=self.policy,
            observation=self.observation(
                actual_offset=-2,
                external_influence_reason="Synthetic room HVAC influence.",
            ),
            intervals=prices,
            planned=initial.plan,
        )
        shadow.room_temperature_details[
            "thermal_response_evidence_eligible"
        ] = False
        shadow._update_candidate_evidence(
            initial.plan.cheap_window.start + timedelta(minutes=15),
            influenced,
        )
        shadow._update_candidate_evidence(
            initial.plan.peak_window.end,
            influenced,
        )
        self.assertEqual(shadow.state["candidate_cycles"], 0)
        self.assertEqual(shadow.state["contaminated_candidate_cycles"], 1)
        self.assertIsNone(shadow.state["candidate_cycle_peak_end"])

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
            "off",
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
            self.assertTrue(
                shadow.attributes["thermal_response_evidence_eligible"]
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

    async def test_active_room_hvac_yields_without_discarding_temperature(self):
        await self.refresh()
        self.hass.states.async_set(
            "climate.bedroom",
            "cool",
            {"current_temperature": 22.8, "temperature_unit": "°C"},
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=self._runtime_status()),
            control_daikin=AsyncMock(),
        )
        entry = self._runtime_entry(["climate.bedroom"])
        shadow = self.module.FloorHeatingShadow(
            self.hass, entry, self.coordinator, devices, None
        )
        await shadow.async_start()
        try:
            self.assertEqual(shadow.decision.indoor_temperature, 22.8)
            self.assertEqual(shadow.decision.state, "yield")
            self.assertIn("external room control", shadow.decision.reason)
            self.assertEqual(
                shadow.attributes["active_room_hvac_entities"],
                ["climate.bedroom"],
            )
            self.assertTrue(
                shadow.attributes["external_room_hvac_influence"]
            )
            self.assertFalse(
                shadow.attributes["thermal_response_evidence_eligible"]
            )
            self.assertEqual(shadow.state["candidate_cycles"], 0)
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
            self.assertEqual(shadow.attributes["shift_delta_c"], 4)
            sensor = self.sensor_module.FloorHeatingShadowSensor(
                shadow, entry.entry_id
            )
            self.assertEqual(sensor.native_value, shadow.decision.state)
            self.assertEqual(
                sensor.extra_state_attributes["confirmed_writes"], 0
            )
            self.assertLess(
                len(json.dumps(sensor.extra_state_attributes)),
                16000,
            )
        finally:
            await shadow.async_stop()

    async def test_owner_approved_active_mode_writes_once_and_restores_baseline(self):
        await self.refresh()
        self.hass.states.async_set(
            "sensor.room_temperature",
            "20.5",
            {"unit_of_measurement": "°C"},
        )
        delivery_day = date(2026, 10, 20)
        start, _end = self.module.market_bounds(delivery_day)
        prices = self.delivery_day_intervals(delivery_day)
        status = self._runtime_status()
        status["heating"]["operations"][
            "LeavingWaterTemperatureOffsetHeating"
        ] = -2

        async def control(args, _context):
            previous = status["heating"]["operations"][
                "LeavingWaterTemperatureOffsetHeating"
            ]
            status["heating"]["operations"][
                "LeavingWaterTemperatureOffsetHeating"
            ] = float(args["value"])
            return {
                "changed": True,
                "operation": "heating_offset",
                "previous": previous,
                "confirmed": float(args["value"]),
            }

        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=status),
            control_daikin=AsyncMock(side_effect=control),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry(["sensor.room_temperature"]),
            self.coordinator,
            devices,
            None,
        )
        shadow.mode = self.const.FLOOR_MODE_ACTIVE
        shadow.state["owner_activation_approved_at"] = (
            start.isoformat()
        )

        with (
            patch.object(
                self.module,
                "published_intervals",
                return_value=prices,
            ),
            patch.object(
                self.module,
                "read_room_temperature_source",
                side_effect=lambda *_args, now=None, **_kwargs: (
                    20.5,
                    now,
                    None,
                ),
            ),
        ):
            await shadow.async_evaluate(start + timedelta(hours=10))
            self.assertEqual(shadow.decision.state, "preheat")
            self.assertEqual(shadow.decision.proposed_offset, 2)
            self.assertEqual(
                status["heating"]["operations"][
                    "LeavingWaterTemperatureOffsetHeating"
                ],
                2,
            )
            self.assertEqual(devices.control_daikin.await_count, 1)
            self.assertIsNotNone(shadow.state["ownership"])

            await shadow.async_evaluate(
                start + timedelta(hours=10, minutes=15)
            )
            self.assertEqual(shadow.decision.proposed_offset, 2)
            self.assertEqual(devices.control_daikin.await_count, 1)

            await shadow.async_evaluate(start + timedelta(hours=13))
            self.assertEqual(shadow.decision.state, "hold")
            self.assertEqual(shadow.decision.proposed_offset, -2)
            self.assertEqual(
                status["heating"]["operations"][
                    "LeavingWaterTemperatureOffsetHeating"
                ],
                -2,
            )
            self.assertEqual(devices.control_daikin.await_count, 2)
            self.assertIsNone(shadow.state["ownership"])
            self.assertEqual(shadow.state["confirmed_writes"], 2)

    async def test_floor_active_mode_requires_evidence_or_owner_approval(self):
        await self.refresh()
        self.hass.states.async_set(
            "sensor.room_temperature",
            "20.5",
            {"unit_of_measurement": "°C"},
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(
                return_value=self._runtime_status()
            ),
            control_daikin=AsyncMock(),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry(["sensor.room_temperature"]),
            self.coordinator,
            devices,
            None,
        )
        shadow.async_evaluate = AsyncMock()

        with self.assertRaisesRegex(
            self.module.HomeAssistantError,
            "owner-approved",
        ):
            await shadow.async_set_mode(self.const.FLOOR_MODE_ACTIVE)

        await shadow.async_set_mode(
            self.const.FLOOR_MODE_ACTIVE,
            owner_approved=True,
        )
        self.assertEqual(shadow.mode, self.const.FLOOR_MODE_ACTIVE)
        self.assertIsNotNone(
            shadow.state["owner_activation_approved_at"]
        )
        self.assertTrue(shadow.attributes["owner_activation_approved"])

    async def test_uncertain_acquisition_reconciles_without_replaying(self):
        self.hass.states.async_set(
            "sensor.room_temperature",
            "20.5",
            {"unit_of_measurement": "°C"},
        )
        status = self._runtime_status()
        status["heating"]["operations"][
            "LeavingWaterTemperatureOffsetHeating"
        ] = -2

        async def uncertain_control(args, _context):
            status["heating"]["operations"][
                "LeavingWaterTemperatureOffsetHeating"
            ] = float(args["value"])
            raise self.module.HomeAssistantError(
                "Synthetic post-dispatch uncertainty."
            )

        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=status),
            control_daikin=AsyncMock(side_effect=uncertain_control),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry(["sensor.room_temperature"]),
            self.coordinator,
            devices,
            None,
        )
        shadow.mode = self.const.FLOOR_MODE_ACTIVE
        shadow._save = AsyncMock()
        now = self.now.astimezone(self.module.UTC)

        with patch.object(
            self.module.persistent_notification,
            "async_create",
        ):
            confirmed = await shadow._dispatch_offset(
                now=now,
                previous=-2,
                target=2,
                baseline=-2,
                phase="preheat",
                restoring=False,
            )
        self.assertFalse(confirmed)
        self.assertIsNotNone(shadow.state["fault"])
        self.assertIsNotNone(shadow.state["pending_operation"])
        self.assertIsNone(shadow.state["ownership"])
        self.assertEqual(devices.control_daikin.await_count, 1)

        shadow.async_evaluate = AsyncMock()
        with patch.object(
            self.module.persistent_notification,
            "async_dismiss",
        ):
            await shadow.async_resume_after_review()
        self.assertIsNone(shadow.state["fault"])
        self.assertIsNone(shadow.state["pending_operation"])
        self.assertEqual(
            shadow.state["ownership"]["baseline_offset"],
            -2,
        )
        self.assertEqual(
            shadow.state["ownership"]["target_offset"],
            2,
        )
        self.assertEqual(devices.control_daikin.await_count, 1)

    async def test_accepted_native_schedule_change_relinquishes_ownership(self):
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry([]),
            self.coordinator,
            SimpleNamespace(),
            None,
        )
        shadow.mode = self.const.FLOOR_MODE_ACTIVE
        shadow.state["ownership"] = {
            "baseline_offset": -2,
            "target_offset": 2,
            "phase": "preheat",
            "acquired_at": self.now.isoformat(),
            "updated_at": self.now.isoformat(),
        }
        delivery_day = date(2026, 10, 20)
        start, _end = self.module.market_bounds(delivery_day)
        decision = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(actual_offset=0),
            intervals=self.delivery_day_intervals(delivery_day),
        )

        normalized = shadow._normalize_owned_decision(
            start,
            self.observation(actual_offset=0),
            decision,
        )

        self.assertEqual(normalized.state, "schedule_learning_hold")
        self.assertEqual(normalized.proposed_offset, 0)
        self.assertEqual(shadow.mode, self.const.FLOOR_MODE_ACTIVE)
        self.assertIsNone(shadow.state["ownership"])
        self.assertIsNone(shadow.state["manual_override"])
        self.assertIsNotNone(
            shadow.state["schedule_learning_hold_until"]
        )
        self.assertTrue(shadow.state["candidate_cycle_contaminated"])
        self.assertEqual(
            shadow.state["native_schedule_transitions"][-1][
                "classification"
            ],
            "accepted_schedule_transition",
        )

    async def test_unexpected_owned_change_fails_to_shadow(self):
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry([]),
            self.coordinator,
            SimpleNamespace(),
            None,
        )
        shadow.mode = self.const.FLOOR_MODE_ACTIVE
        shadow.state["ownership"] = {
            "baseline_offset": -2,
            "target_offset": 2,
            "phase": "preheat",
            "acquired_at": self.now.isoformat(),
            "updated_at": self.now.isoformat(),
        }
        delivery_day = date(2026, 10, 20)
        start, _end = self.module.market_bounds(delivery_day)
        decision = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(actual_offset=-1),
            intervals=self.delivery_day_intervals(delivery_day),
        )

        normalized = shadow._normalize_owned_decision(
            start,
            self.observation(actual_offset=-1),
            decision,
        )

        self.assertEqual(normalized.state, "yield")
        self.assertEqual(shadow.mode, self.const.FLOOR_MODE_SHADOW)
        self.assertIsNone(shadow.state["ownership"])
        self.assertIn("unexpectedly", shadow.state["manual_override"])

    async def test_uncertain_restore_reconciles_at_baseline_without_replay(self):
        self.hass.states.async_set(
            "sensor.room_temperature",
            "20.5",
            {"unit_of_measurement": "°C"},
        )
        status = self._runtime_status()
        status["heating"]["operations"][
            "LeavingWaterTemperatureOffsetHeating"
        ] = 2

        async def uncertain_restore(args, _context):
            status["heating"]["operations"][
                "LeavingWaterTemperatureOffsetHeating"
            ] = float(args["value"])
            raise self.module.HomeAssistantError(
                "Synthetic restore uncertainty."
            )

        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=status),
            control_daikin=AsyncMock(side_effect=uncertain_restore),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry(["sensor.room_temperature"]),
            self.coordinator,
            devices,
            None,
        )
        shadow.mode = self.const.FLOOR_MODE_ACTIVE
        shadow.state["ownership"] = {
            "baseline_offset": -2,
            "target_offset": 2,
            "phase": "preheat",
            "acquired_at": self.now.isoformat(),
            "updated_at": self.now.isoformat(),
        }
        shadow._save = AsyncMock()

        with (
            patch.object(
                self.module.persistent_notification,
                "async_create",
            ),
            self.assertRaisesRegex(
                self.module.HomeAssistantError,
                "may have applied",
            ),
        ):
            await shadow.async_return_to_baseline(evaluate=False)
        self.assertIsNotNone(shadow.state["pending_operation"])
        self.assertIsNotNone(shadow.state["ownership"])
        self.assertEqual(devices.control_daikin.await_count, 1)

        shadow.async_evaluate = AsyncMock()
        with patch.object(
            self.module.persistent_notification,
            "async_dismiss",
        ):
            await shadow.async_resume_after_review()
        self.assertIsNone(shadow.state["pending_operation"])
        self.assertIsNone(shadow.state["ownership"])
        self.assertIsNone(shadow.state["fault"])
        self.assertEqual(devices.control_daikin.await_count, 1)

    async def test_pre_dispatch_conflict_clears_pending_without_fault(self):
        devices = SimpleNamespace(
            control_daikin=AsyncMock(
                side_effect=self.module.DaikinPreDispatchConflict(
                    "Synthetic expected-previous conflict."
                )
            ),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry([]),
            self.coordinator,
            devices,
            None,
        )
        shadow.mode = self.const.FLOOR_MODE_ACTIVE
        shadow._save = AsyncMock()

        with patch.object(
            self.module.persistent_notification,
            "async_create",
        ) as notify:
            confirmed = await shadow._dispatch_offset(
                now=self.now.astimezone(self.module.UTC),
                previous=-2,
                target=2,
                baseline=-2,
                phase="preheat",
                restoring=False,
            )

        self.assertFalse(confirmed)
        self.assertIsNone(shadow.state["pending_operation"])
        self.assertIsNone(shadow.state["fault"])
        self.assertIsNone(shadow.state["ownership"])
        devices.control_daikin.assert_awaited_once()
        notify.assert_not_called()

    async def test_mode_change_restores_exact_owned_baseline(self):
        self.hass.states.async_set(
            "sensor.room_temperature",
            "20.5",
            {"unit_of_measurement": "°C"},
        )
        status = self._runtime_status()
        status["heating"]["operations"][
            "LeavingWaterTemperatureOffsetHeating"
        ] = 2

        async def control(args, _context):
            previous = status["heating"]["operations"][
                "LeavingWaterTemperatureOffsetHeating"
            ]
            status["heating"]["operations"][
                "LeavingWaterTemperatureOffsetHeating"
            ] = float(args["value"])
            return {
                "changed": True,
                "operation": "heating_offset",
                "previous": previous,
                "confirmed": float(args["value"]),
            }

        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(return_value=status),
            control_daikin=AsyncMock(side_effect=control),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry(["sensor.room_temperature"]),
            self.coordinator,
            devices,
            None,
        )
        shadow.mode = self.const.FLOOR_MODE_ACTIVE
        shadow.state["ownership"] = {
            "baseline_offset": -2,
            "target_offset": 2,
            "phase": "preheat",
            "acquired_at": self.now.isoformat(),
            "updated_at": self.now.isoformat(),
        }
        shadow._save = AsyncMock()
        shadow.async_evaluate = AsyncMock()

        await shadow.async_set_mode(self.const.FLOOR_MODE_SHADOW)

        self.assertEqual(shadow.mode, self.const.FLOOR_MODE_SHADOW)
        self.assertIsNone(shadow.state["ownership"])
        self.assertEqual(
            status["heating"]["operations"][
                "LeavingWaterTemperatureOffsetHeating"
            ],
            -2,
        )
        devices.control_daikin.assert_awaited_once()
        self.assertEqual(
            devices.control_daikin.await_args.args[0],
            {
                "operation": "heating_offset",
                "value": -2,
                "expected_previous": 2,
            },
        )

    async def test_startup_latches_persisted_pending_command_without_write(self):
        await self.refresh()
        self.hass.states.async_set(
            "sensor.room_temperature",
            "20.5",
            {"unit_of_measurement": "°C"},
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(
                return_value=self._runtime_status()
            ),
            control_daikin=AsyncMock(),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry(["sensor.room_temperature"]),
            self.coordinator,
            devices,
            None,
        )
        stored = {
            **shadow.state,
            "mode": self.const.FLOOR_MODE_ACTIVE,
            "pending_operation": {
                "previous_offset": -2,
                "target_offset": 2,
                "baseline_offset": -2,
                "phase": "preheat",
                "restoring": False,
                "prepared_at": self.now.isoformat(),
            },
        }
        shadow.store = SimpleNamespace(
            async_load=AsyncMock(return_value=stored),
            async_save=AsyncMock(),
        )

        await shadow.async_start()
        try:
            self.assertEqual(shadow.mode, self.const.FLOOR_MODE_ACTIVE)
            self.assertIn("uncertain outcome", shadow.state["fault"])
            self.assertEqual(shadow.decision.state, "fault")
            devices.control_daikin.assert_not_awaited()
        finally:
            await shadow.async_stop()

    async def test_revision_five_migration_preserves_owned_offset_for_review(self):
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(
                return_value=self._runtime_status()
            ),
            control_daikin=AsyncMock(),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry([]),
            self.coordinator,
            devices,
            None,
        )
        stored = {
            **shadow.state,
            "policy_revision": 5,
            "mode": self.const.FLOOR_MODE_ACTIVE,
            "ownership": {
                "baseline_offset": -2,
                "target_offset": 2,
                "phase": "preheat",
                "acquired_at": self.now.isoformat(),
                "updated_at": self.now.isoformat(),
            },
        }
        shadow.store = SimpleNamespace(
            async_load=AsyncMock(return_value=stored),
            async_save=AsyncMock(),
        )

        await shadow.async_start()
        try:
            self.assertEqual(shadow.mode, self.const.FLOOR_MODE_SHADOW)
            self.assertEqual(
                shadow.state["ownership"]["target_offset"],
                2,
            )
            self.assertIn(
                "Ownership was preserved",
                shadow.state["fault"],
            )
            devices.control_daikin.assert_not_awaited()
        finally:
            shadow.state["ownership"] = None
            await shadow.async_stop()

    async def test_revision_five_migration_preserves_pending_operation(self):
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(
                return_value=self._runtime_status()
            ),
            control_daikin=AsyncMock(),
        )
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry([]),
            self.coordinator,
            devices,
            None,
        )
        pending = {
            "previous_offset": -2,
            "target_offset": 2,
            "baseline_offset": -2,
            "phase": "preheat",
            "restoring": False,
            "prepared_at": self.now.isoformat(),
        }
        shadow.store = SimpleNamespace(
            async_load=AsyncMock(return_value={
                **shadow.state,
                "policy_revision": 5,
                "mode": self.const.FLOOR_MODE_ACTIVE,
                "pending_operation": pending,
            }),
            async_save=AsyncMock(),
        )

        await shadow.async_start()
        try:
            self.assertEqual(shadow.mode, self.const.FLOOR_MODE_SHADOW)
            self.assertEqual(shadow.state["pending_operation"], pending)
            self.assertIn("uncertain outcome", shadow.state["fault"])
            devices.control_daikin.assert_not_awaited()
        finally:
            await shadow.async_stop()

    async def test_extreme_price_arbitration_yields_before_floor_evidence(self):
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
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            self._runtime_entry(["sensor.room_temperature"]),
            self.coordinator,
            devices,
            None,
        )
        shadow.set_extreme_protection(
            SimpleNamespace(
                ordinary_control_block_reason=lambda _now: (
                    "Extreme price protection has priority."
                )
            )
        )
        await shadow.async_start()
        try:
            self.assertEqual(shadow.decision.state, "yield")
            self.assertIn("priority", shadow.decision.reason)
            devices.control_daikin.assert_not_awaited()
        finally:
            await shadow.async_stop()

    def test_price_opportunity_records_one_pair_without_claiming_savings(self):
        entry = self._runtime_entry([])
        shadow = self.module.FloorHeatingShadow(
            self.hass,
            entry,
            self.coordinator,
            SimpleNamespace(),
            None,
        )
        delivery_day = date(2026, 10, 17)
        start, _end = self.module.market_bounds(delivery_day)
        decision = self.module.plan_floor_heating(
            now=start,
            policy=self.policy,
            observation=self.observation(),
            intervals=self.delivery_day_intervals(delivery_day),
        )
        self.assertTrue(decision.price_gate)
        shadow.room_temperature_details[
            "thermal_response_evidence_eligible"
        ] = True
        shadow._record_optimization_opportunity(start, decision)
        shadow._record_optimization_opportunity(
            start + timedelta(minutes=15), decision
        )
        shadow.decision = decision

        sensor = (
            self.sensor_module.FloorHeatingOptimizationOpportunitySensor(
                shadow, entry.entry_id
            )
        )
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, Decimal("0.30"))
        self.assertEqual(sensor.native_unit_of_measurement, "EUR/kWh")
        attributes = sensor.extra_state_attributes
        self.assertEqual(attributes["valuation_status"], "counterfactual")
        self.assertEqual(attributes["qualified_opportunities_recorded"], 1)
        self.assertIsNone(attributes["measured_shifted_energy_kwh"])
        self.assertIsNone(attributes["estimated_savings_eur"])
        self.assertIsNone(attributes["realized_savings_eur"])
        self.assertLess(len(json.dumps(attributes)), 2048)

        shadow.mode = self.const.FLOOR_MODE_ACTIVE
        live_attributes = sensor.extra_state_attributes
        self.assertEqual(
            live_attributes["valuation_status"],
            "live_action_unattributed",
        )


if __name__ == "__main__":
    unittest.main()
