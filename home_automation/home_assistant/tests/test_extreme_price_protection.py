"""Extreme-price protection policy and runtime tests using real HA APIs."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import importlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError

import test_component as base


setUpModule = base.setUpModule
tearDownModule = base.tearDownModule


class ExtremePriceProtectionTests(base.HAComponentTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.module = importlib.import_module(
            base.COMPONENT + ".extreme_price_protection"
        )
        self.dhw_module = importlib.import_module(
            base.COMPONENT + ".dhw_optimizer"
        )
        self.const = importlib.import_module(base.COMPONENT + ".const")

    def intervals(
        self,
        start: datetime,
        values: list[str],
    ):
        return tuple(
            self.module.PriceInterval(
                start + index * timedelta(minutes=15),
                start + (index + 1) * timedelta(minutes=15),
                Decimal(value),
            )
            for index, value in enumerate(values)
        )

    def plan(self, *, values, now=None, **changes):
        start = datetime.fromisoformat("2026-10-17T00:00:00+00:00")
        arguments = {
            "now": now or start,
            "mode": self.const.EXTREME_PRICE_MODE_SHADOW,
            "intervals": self.intervals(start, values),
            "water_enabled": True,
            "tank_temperature": 50,
            "water_guard": None,
            "readiness_active": False,
            "heating_enabled": True,
            "room_temperature": 22,
            "heating_guard": None,
            "climates": (),
        }
        arguments.update(changes)
        return self.module.plan_extreme_price_protection(**arguments)

    def test_hard_stop_boundary_and_contiguous_block_identity(self):
        below = self.plan(values=["1.499999"])
        self.assertEqual(below.state, "clear")
        self.assertIsNone(below.active_block)

        exact = self.plan(values=["1.5", "1.8", "1.5"])
        self.assertEqual(exact.state, "active")
        self.assertEqual(exact.active_block.interval_count, 3)
        self.assertEqual(exact.active_block.maximum, Decimal("1.8"))
        self.assertEqual(
            exact.active_block.duration,
            timedelta(minutes=45),
        )
        self.assertIn("1.5|", exact.active_block.identity)

    def test_future_hard_stop_block_is_published_before_it_starts(self):
        start = datetime.fromisoformat("2026-10-17T00:00:00+00:00")
        decision = self.plan(
            values=["0.2", "1.5", "1.8"],
            now=start,
        )
        self.assertEqual(decision.state, "scheduled")
        self.assertIsNone(decision.active_block)
        self.assertEqual(
            decision.next_hard_block.start,
            start + timedelta(minutes=15),
        )
        self.assertEqual(
            decision.attributes()["next_hard_block_end"],
            (start + timedelta(minutes=45)).isoformat(),
        )
        self.assertIn("scheduled", decision.reason)

    def test_warning_requires_two_contiguous_hours(self):
        short = self.plan(values=["0.5"] * 7 + ["0.1"])
        self.assertIsNone(short.warning_block)
        full = self.plan(values=["0.5"] * 8 + ["0.1"])
        self.assertIsNotNone(full.warning_block)
        self.assertEqual(
            full.warning_block.duration,
            timedelta(hours=2),
        )
        self.assertEqual(full.state, "warning")

    def test_shadow_proposes_each_load_without_noop_ir(self):
        climates = (
            {
                "entity_id": "climate.active",
                "available": True,
                "fresh": True,
                "state": "heat",
            },
            {
                "entity_id": "climate.off",
                "available": True,
                "fresh": True,
                "state": "off",
            },
            {
                "entity_id": "climate.missing",
                "available": False,
                "fresh": False,
                "state": None,
                "reason": "Synthetic missing state.",
            },
        )
        decision = self.plan(values=["1.5"], climates=climates)
        actions = {
            action.load_id: action for action in decision.actions
        }
        self.assertEqual(
            actions[self.module.LOAD_HOT_WATER].operation,
            "standby",
        )
        self.assertEqual(
            actions[self.module.LOAD_FLOOR_HEATING].operation,
            "standby",
        )
        self.assertEqual(actions["climate.active"].operation, "turn_off")
        self.assertEqual(actions["climate.off"].operation, "no_op")
        self.assertEqual(
            actions["climate.missing"].operation,
            "unavailable",
        )
        self.assertTrue(decision.partial)
        self.assertIn("Simulation only", decision.reason)

    def test_comfort_and_readiness_guards_override_extreme_price(self):
        decision = self.plan(
            values=["1.5"],
            tank_temperature=45,
            room_temperature=20.5,
        )
        actions = {
            action.load_id: action for action in decision.actions
        }
        self.assertEqual(
            actions[self.module.LOAD_HOT_WATER].operation,
            "keep_on",
        )
        self.assertEqual(
            actions[self.module.LOAD_FLOOR_HEATING].operation,
            "keep_on",
        )
        readiness = self.plan(
            values=["1.5"],
            readiness_active=True,
        )
        action = next(
            item
            for item in readiness.actions
            if item.load_id == self.module.LOAD_HOT_WATER
        )
        self.assertEqual(action.operation, "keep_on")
        self.assertIn("readiness", action.reason)

    def test_unknown_circuit_state_is_unavailable_not_already_stopped(self):
        decision = self.plan(
            values=["1.5"],
            water_enabled=None,
            heating_enabled=None,
        )
        actions = {
            action.load_id: action for action in decision.actions
        }
        self.assertEqual(
            actions[self.module.LOAD_HOT_WATER].operation,
            "unavailable",
        )
        self.assertEqual(
            actions[self.module.LOAD_FLOOR_HEATING].operation,
            "unavailable",
        )
        self.assertTrue(decision.partial)

    def test_already_stopped_circuits_are_noops_without_ownership(self):
        decision = self.plan(
            values=["1.5"],
            water_enabled=False,
            heating_enabled=False,
        )
        actions = {
            action.load_id: action for action in decision.actions
        }
        self.assertEqual(
            actions[self.module.LOAD_HOT_WATER].operation,
            "no_op",
        )
        self.assertEqual(
            actions[self.module.LOAD_FLOOR_HEATING].operation,
            "no_op",
        )
        self.assertFalse(decision.partial)

    def test_owned_loads_restore_when_block_ends_or_comfort_requires(self):
        clear = self.plan(
            values=["0.2"],
            owned_loads=(
                self.module.LOAD_HOT_WATER,
                "climate.bedroom",
            ),
        )
        self.assertEqual(
            {action.operation for action in clear.actions},
            {"restore"},
        )
        active = self.plan(
            values=["1.5"],
            water_enabled=False,
            tank_temperature=44,
            owned_loads=(self.module.LOAD_HOT_WATER,),
        )
        action = next(
            item
            for item in active.actions
            if item.load_id == self.module.LOAD_HOT_WATER
        )
        self.assertEqual(action.operation, "restore")

        unavailable = self.plan(
            values=["1.5"],
            water_enabled=None,
            owned_loads=(self.module.LOAD_HOT_WATER,),
        )
        action = next(
            item
            for item in unavailable.actions
            if item.load_id == self.module.LOAD_HOT_WATER
        )
        self.assertEqual(action.operation, "unavailable")
        self.assertTrue(unavailable.partial)

    def test_suppression_prevents_restopping_within_same_block(self):
        first = self.plan(values=["1.5"])
        block = first.active_block.identity
        suppressed = self.plan(
            values=["1.5"],
            suppressed_load_blocks=(
                block + "|" + self.module.LOAD_HOT_WATER,
            ),
        )
        action = next(
            item
            for item in suppressed.actions
            if item.load_id == self.module.LOAD_HOT_WATER
        )
        self.assertEqual(action.operation, "keep_on")

    def test_two_euro_price_is_alert_severity_not_a_second_algorithm(self):
        decision = self.plan(values=["2.0"])
        self.assertTrue(decision.critical)
        self.assertEqual(decision.active_block.threshold, Decimal("1.5"))
        self.assertEqual(decision.state, "critical")

    def test_deterministic_commissioning_replay_covers_rare_event_paths(self):
        receipt = self.module.verify_commissioning_replay()
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["coverage_days"], 7)
        self.assertEqual(receipt["scenario_count"], 8)
        self.assertIn("hard_stop_boundary", receipt["scenarios"])
        self.assertIn("ownership_restore", receipt["scenarios"])

    def runtime_status(self, *, water="on", heating="on"):
        flags = {
            name: False
            for name in importlib.import_module(
                base.COMPONENT + ".controls"
            ).FLAGS
        }
        return {
            "heating": {
                "states": dict(flags),
                "operations": {
                    "OperationMode": "heating",
                    "Power": heating,
                    "LeavingWaterTemperatureOffsetHeating": 0,
                },
                "sensors": {
                    "OutdoorTemperature": 5,
                    "LeavingWaterTemperatureCurrent": 31,
                },
            },
            "hot_water": {
                "states": {
                    **flags,
                    "WeatherDependentState": False,
                },
                "operations": {
                    "Power": water,
                    "DomesticHotWaterTemperatureHeating": 50,
                    "powerful": 0,
                },
                "sensors": {"TankTemperature": 50},
            },
            "control_mode": "LWT control",
            "effective_hot_water_target": 50,
        }

    def climate_snapshot(self, state="heat"):
        return {
            "entity_id": "climate.bedroom",
            "identity": {
                "entity_id": "climate.bedroom",
                "platform": "sensibo",
                "unique_id": "synthetic",
            },
            "available": True,
            "fresh": True,
            "reason": None,
            "state": state,
            "current_temperature": 20,
            "temperature": 22,
            "temperature_unit": "°C",
            "fan_mode": "auto",
            "swing_mode": "stopped",
            "swing_horizontal_mode": "stopped",
            "hvac_modes": ["off", "heat"],
            "fan_modes": ["auto"],
            "swing_modes": ["stopped"],
            "swing_horizontal_modes": ["stopped"],
            "minimum_temperature": 16,
            "maximum_temperature": 30,
            "target_temperature_step": 1,
            "observed_at": self.now.isoformat(),
        }

    def runtime(self, devices, floor=None):
        entry = SimpleNamespace(
            entry_id="pricing",
            options={
                "floor_indoor_temperature_entities": [
                    "climate.bedroom"
                ],
                "extreme_price_climate_entities": [
                    "climate.bedroom"
                ],
            },
        )
        policy = self.dhw_module.DhwPolicy.from_options({})
        optimizer = SimpleNamespace(
            policy=policy,
            heating_minutes=90,
            async_add_listener=lambda _listener: lambda: None,
        )
        return self.module.ExtremePriceProtection(
            self.hass,
            entry,
            self.coordinator,
            devices,
            optimizer,
            floor or SimpleNamespace(set_external_influence=Mock()),
        )

    def test_missing_daikin_circuits_keep_power_observation_shape(self):
        protection = self.runtime(SimpleNamespace())
        observation = protection._daikin_observation({}, self.now)
        self.assertIsNone(observation["water_enabled"])
        self.assertIsNone(observation["water_power"])
        self.assertIsNone(observation["heating_enabled"])
        self.assertIsNone(observation["heating_power"])

    async def test_runtime_shadow_is_strictly_zero_write(self):
        self.hass.states.async_set(
            "climate.bedroom",
            "heat",
            {
                "current_temperature": 22,
                "temperature_unit": "°C",
            },
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(
                return_value=self.runtime_status()
            ),
            get_climate_status=AsyncMock(
                return_value=self.climate_snapshot()
            ),
            control_daikin=AsyncMock(),
            control_climate=AsyncMock(),
        )
        protection = self.runtime(devices)
        high = self.intervals(self.now, ["1.5"] * 4)
        with (
            patch.object(
                self.module,
                "published_intervals",
                return_value=high,
            ),
            patch.object(
                protection,
                "_alert_decision",
                new=AsyncMock(),
            ),
        ):
            await protection.async_evaluate(self.now)
        self.assertEqual(protection.decision.state, "partial")
        self.assertTrue(protection.attributes["shadow_zero_writes"])
        self.assertFalse(protection.attributes["write_allowed"])
        devices.control_daikin.assert_not_awaited()
        devices.control_climate.assert_not_awaited()
        self.assertLess(len(json.dumps(protection.attributes)), 16000)

    def test_ordinary_heating_arbitration_uses_current_price_and_ownership(self):
        protection = self.runtime(SimpleNamespace())
        protection.mode = self.const.EXTREME_PRICE_MODE_ARMED
        with patch.object(
            self.module,
            "published_intervals",
            return_value=self.intervals(self.now, ["1.5"]),
        ):
            reason = protection.ordinary_control_block_reason(self.now)
        self.assertIn("priority", reason)

        with patch.object(
            self.module,
            "published_intervals",
            return_value=self.intervals(self.now, ["0.2"]),
        ):
            self.assertIsNone(
                protection.ordinary_control_block_reason(self.now)
            )
        protection.state["ownership"] = {
            self.module.LOAD_HOT_WATER: {"block_id": "synthetic"}
        }
        self.assertIn(
            "owns",
            protection.ordinary_control_block_reason(self.now),
        )

    async def test_partial_and_early_restore_alerts_are_explicit(self):
        devices = SimpleNamespace()
        protection = self.runtime(devices)
        partial = self.plan(
            values=["1.5"],
            climates=(
                {
                    "entity_id": "climate.missing",
                    "available": False,
                    "fresh": False,
                    "state": None,
                    "reason": "Synthetic missing state.",
                },
            ),
        )
        active_restore = self.plan(
            values=["1.5"],
            water_enabled=False,
            tank_temperature=44,
            owned_loads=(self.module.LOAD_HOT_WATER,),
        )
        with patch.object(
            protection,
            "_send_alert",
            new=AsyncMock(),
        ) as send_alert:
            await protection._alert_decision(partial)
            self.assertEqual(
                send_alert.await_args_list[0].kwargs["title"],
                "Extreme price protection partial",
            )
            send_alert.reset_mock()
            await protection._alert_decision(active_restore)
        self.assertEqual(send_alert.await_count, 2)
        self.assertEqual(
            send_alert.await_args_list[1].kwargs["title"],
            "Extreme price protection early restore",
        )

    async def test_armed_stop_is_sequential_and_floor_waits_for_room_hvac(self):
        status = self.runtime_status()
        self.hass.states.async_set(
            "climate.bedroom",
            "heat",
            {
                "current_temperature": 22,
                "temperature_unit": "°C",
            },
        )

        async def control_daikin(request, _context):
            operation = request["operation"]
            if operation == "hot_water_enabled":
                status["hot_water"]["operations"]["Power"] = (
                    "on" if request["value"] else "standby"
                )
            elif operation == "heating_enabled":
                status["heating"]["operations"]["Power"] = (
                    "on" if request["value"] else "standby"
                )
            return {
                "changed": True,
                "confirmed": (
                    "on" if request["value"] else "standby"
                ),
            }

        async def climate_status(_entity_id):
            state = self.hass.states.get("climate.bedroom").state
            return self.climate_snapshot(state)

        async def control_climate(request, _context):
            self.hass.states.async_set(
                request["entity_id"],
                "off",
                {
                    "current_temperature": 22,
                    "temperature_unit": "°C",
                },
            )
            return {
                "changed": True,
                "confirmed": self.climate_snapshot("off"),
            }

        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(side_effect=lambda: status),
            get_climate_status=AsyncMock(side_effect=climate_status),
            control_daikin=AsyncMock(side_effect=control_daikin),
            control_climate=AsyncMock(side_effect=control_climate),
        )
        protection = self.runtime(devices)
        protection.mode = self.const.EXTREME_PRICE_MODE_ARMED
        protection.state["shadow_days"] = [
            f"2026-10-{day:02d}" for day in range(1, 8)
        ]
        high = self.intervals(self.now, ["1.5"] * 8)
        with (
            patch.object(
                self.module,
                "published_intervals",
                return_value=high,
            ),
            patch.object(
                protection,
                "_alert_decision",
                new=AsyncMock(),
            ),
        ):
            await protection.async_evaluate(self.now)
            self.assertEqual(
                set(protection.state["ownership"]),
                {
                    self.module.LOAD_HOT_WATER,
                    "climate.bedroom",
                },
            )
            await protection.async_evaluate(
                self.now + timedelta(minutes=1)
            )
        self.assertEqual(
            set(protection.state["ownership"]),
            {
                self.module.LOAD_HOT_WATER,
                self.module.LOAD_FLOOR_HEATING,
                "climate.bedroom",
            },
        )
        self.assertEqual(
            [
                call.args[0]["operation"]
                for call in devices.control_daikin.await_args_list
            ],
            ["hot_water_enabled", "heating_enabled"],
        )
        devices.control_climate.assert_awaited_once()

    async def test_armed_failure_faults_without_retry_or_rollback(self):
        self.hass.states.async_set(
            "climate.bedroom",
            "off",
            {
                "current_temperature": 20.5,
                "temperature_unit": "°C",
            },
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(
                return_value=self.runtime_status()
            ),
            get_climate_status=AsyncMock(
                return_value=self.climate_snapshot("off")
            ),
            control_daikin=AsyncMock(
                side_effect=HomeAssistantError("Synthetic uncertain write")
            ),
            control_climate=AsyncMock(),
        )
        protection = self.runtime(devices)
        protection.mode = self.const.EXTREME_PRICE_MODE_ARMED
        high = self.intervals(self.now, ["1.5"])
        with (
            patch.object(
                self.module,
                "published_intervals",
                return_value=high,
            ),
            patch.object(
                protection,
                "_alert_decision",
                new=AsyncMock(),
            ),
            patch.object(
                protection,
                "_send_alert",
                new=AsyncMock(),
            ),
        ):
            await protection.async_evaluate(self.now)
            await protection.async_evaluate(self.now)
        self.assertIn("may have applied", protection.state["fault"])
        devices.control_daikin.assert_awaited_once()
        devices.control_climate.assert_not_awaited()

    async def test_external_climate_change_cancels_ownership(self):
        self.hass.states.async_set(
            "climate.bedroom",
            "heat",
            {
                "current_temperature": 22,
                "temperature_unit": "°C",
            },
        )
        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(
                return_value=self.runtime_status()
            ),
            get_climate_status=AsyncMock(
                return_value=self.climate_snapshot("heat")
            ),
            control_daikin=AsyncMock(
                return_value={"changed": True, "confirmed": "standby"}
            ),
            control_climate=AsyncMock(),
        )
        protection = self.runtime(devices)
        protection.mode = self.const.EXTREME_PRICE_MODE_ARMED
        high = self.intervals(self.now, ["1.5"] * 4)
        block = self.module.contiguous_price_blocks(
            high,
            Decimal("1.5"),
        )[0]
        protection.state["ownership"] = {
            "climate.bedroom": {
                "block_id": block.identity,
                "lease_expires": (
                    self.now + timedelta(hours=1)
                ).isoformat(),
                "previous": self.climate_snapshot("heat"),
                "stopped_at": self.now.isoformat(),
            }
        }
        with (
            patch.object(
                self.module,
                "published_intervals",
                return_value=high,
            ),
            patch.object(
                protection,
                "_alert_decision",
                new=AsyncMock(),
            ),
        ):
            await protection.async_evaluate(self.now)
        self.assertNotIn(
            "climate.bedroom",
            protection.state["ownership"],
        )
        self.assertIn(
            block.identity + "|climate.bedroom",
            protection.state["suppressed_load_blocks"],
        )
        devices.control_climate.assert_not_awaited()

    async def test_expired_lease_restores_once_and_suppresses_same_block(self):
        status = self.runtime_status(water="standby")
        self.hass.states.async_set(
            "climate.bedroom",
            "off",
            {
                "current_temperature": 20.5,
                "temperature_unit": "°C",
            },
        )

        async def control_daikin(request, _context):
            status["hot_water"]["operations"]["Power"] = "on"
            return {"changed": True, "confirmed": "on"}

        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(side_effect=lambda: status),
            get_climate_status=AsyncMock(
                return_value=self.climate_snapshot("off")
            ),
            control_daikin=AsyncMock(side_effect=control_daikin),
            control_climate=AsyncMock(),
        )
        protection = self.runtime(devices)
        protection.mode = self.const.EXTREME_PRICE_MODE_ARMED
        high = self.intervals(self.now, ["1.5"] * 12)
        block = self.module.contiguous_price_blocks(
            high,
            Decimal("1.5"),
        )[0]
        protection.state["ownership"] = {
            self.module.LOAD_HOT_WATER: {
                "block_id": block.identity,
                "lease_expires": (
                    self.now - timedelta(minutes=1)
                ).isoformat(),
                "previous": {"power": "on"},
                "stopped_at": (
                    self.now - timedelta(hours=2)
                ).isoformat(),
            }
        }
        with (
            patch.object(
                self.module,
                "published_intervals",
                return_value=high,
            ),
            patch.object(
                protection,
                "_alert_decision",
                new=AsyncMock(),
            ),
        ):
            await protection.async_evaluate(self.now)
        self.assertNotIn(
            self.module.LOAD_HOT_WATER,
            protection.state["ownership"],
        )
        self.assertIn(
            block.identity + "|" + self.module.LOAD_HOT_WATER,
            protection.state["suppressed_load_blocks"],
        )
        self.assertEqual(
            devices.control_daikin.await_args_list[0].args[0],
            {
                "operation": "hot_water_enabled",
                "value": True,
                "expected_previous": "standby",
            },
        )

    async def test_commissioning_replay_can_replace_uneventful_live_days(self):
        protection = self.runtime(SimpleNamespace())
        await protection.async_verify_commissioning_replay()
        self.assertTrue(protection.state["replay_verified"])
        self.assertEqual(protection.state["replay_coverage_days"], 7)
        self.assertTrue(protection.observation_qualified)
        self.assertFalse(protection.armed_ready)
        protection.state.update(
            alert_delivery_verified=True,
            daikin_exercises_verified=True,
            climate_exercises_verified=True,
        )
        self.assertTrue(protection.armed_ready)
        self.assertIn(
            self.const.EXTREME_PRICE_MODE_ARMED,
            protection.available_modes,
        )
        with patch.object(
            protection,
            "async_evaluate",
            new=AsyncMock(),
        ):
            await protection.async_set_mode(
                self.const.EXTREME_PRICE_MODE_ARMED
            )
        self.assertEqual(
            protection.mode,
            self.const.EXTREME_PRICE_MODE_ARMED,
        )

    async def test_alert_commissioning_records_service_acceptance(self):
        protection = self.runtime(SimpleNamespace())
        delivered = []

        async def notify(call):
            delivered.append(dict(call.data))

        self.hass.services.async_register(
            "notify",
            self.module.MOBILE_NOTIFY_SERVICE,
            notify,
        )
        await protection.async_verify_alert_delivery()
        self.assertTrue(protection.state["alert_delivery_verified"])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(
            protection.state["commissioning_evidence"][
                "alert_delivery"
            ]["basis"],
            "home_assistant_service_accepted",
        )

    async def test_daikin_commissioning_exercises_each_circuit_and_restores(self):
        status = self.runtime_status()
        self.hass.states.async_set(
            "climate.bedroom",
            "off",
            {
                "current_temperature": 22,
                "temperature_unit": "°C",
            },
        )

        async def control_daikin(request, _context):
            operation = request["operation"]
            value = request["value"]
            if operation == "hot_water_enabled":
                status["hot_water"]["operations"]["Power"] = (
                    "on" if value else "standby"
                )
            else:
                status["heating"]["operations"]["Power"] = (
                    "on" if value else "standby"
                )
            return {
                "changed": True,
                "confirmed": "on" if value else "standby",
            }

        devices = SimpleNamespace(
            get_daikin_status=AsyncMock(side_effect=lambda: status),
            get_climate_status=AsyncMock(
                return_value=self.climate_snapshot("off")
            ),
            control_daikin=AsyncMock(side_effect=control_daikin),
        )
        protection = self.runtime(devices)
        protection.optimizer.decision = SimpleNamespace(state="recovery")
        self.assertTrue(protection._readiness_active(self.now))
        with patch.object(
            protection,
            "_readiness_lead_time_active",
            return_value=False,
        ) as readiness_lead_time:
            await protection.async_exercise_daikin()
        readiness_lead_time.assert_called_once()
        self.assertTrue(protection.state["daikin_exercises_verified"])
        self.assertEqual(
            [
                (call.args[0]["operation"], call.args[0]["value"])
                for call in devices.control_daikin.await_args_list
            ],
            [
                ("hot_water_enabled", False),
                ("hot_water_enabled", True),
                ("heating_enabled", False),
                ("heating_enabled", True),
            ],
        )
        self.assertEqual(
            status["hot_water"]["operations"]["Power"],
            "on",
        )
        self.assertEqual(
            status["heating"]["operations"]["Power"],
            "on",
        )

    async def test_climate_commissioning_exercises_restore_and_returns_off(self):
        active = self.climate_snapshot("cool")
        off = self.climate_snapshot("off")

        async def control_climate(request, _context):
            if request["operation"] == "restore":
                return {
                    "changed": True,
                    "confirmed": request["restore_state"],
                }
            return {"changed": True, "confirmed": off}

        devices = SimpleNamespace(
            get_climate_status=AsyncMock(return_value=off),
            prepare_climate_commissioning=AsyncMock(
                return_value={
                    "changed": True,
                    "previous": off,
                    "confirmed": active,
                }
            ),
            control_climate=AsyncMock(side_effect=control_climate),
        )
        floor = SimpleNamespace(set_external_influence=Mock())
        protection = self.runtime(devices, floor=floor)
        with patch.object(
            self.module.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            await protection.async_exercise_climate()
        self.assertTrue(protection.state["climate_exercises_verified"])
        self.assertFalse(
            protection.state["thermal_contamination_active"]
        )
        self.assertEqual(sleep.await_count, 3)
        self.assertTrue(
            all(
                call.args
                == (
                    self.module.CLIMATE_COMMISSIONING_DWELL_SECONDS,
                )
                for call in sleep.await_args_list
            )
        )
        self.assertEqual(devices.control_climate.await_count, 3)
        self.assertEqual(
            [
                call.args[0]["operation"]
                for call in devices.control_climate.await_args_list
            ],
            ["turn_off", "restore", "turn_off"],
        )
        self.assertEqual(
            protection.state["commissioning_evidence"]["climate"][
                "final_state"
            ],
            "off",
        )
        self.assertEqual(
            protection.state["commissioning_evidence"]["climate"][
                "stabilization_dwell_seconds"
            ],
            self.module.CLIMATE_COMMISSIONING_DWELL_SECONDS,
        )

    async def test_armed_mode_is_not_exposed_before_promotion_gates(self):
        devices = SimpleNamespace()
        protection = self.runtime(devices)
        self.assertEqual(
            protection.available_modes,
            (
                self.const.EXTREME_PRICE_MODE_OFF,
                self.const.EXTREME_PRICE_MODE_SHADOW,
            ),
        )
        with self.assertRaisesRegex(HomeAssistantError, "requires"):
            await protection.async_set_mode(
                self.const.EXTREME_PRICE_MODE_ARMED
            )


if __name__ == "__main__":
    unittest.main()
