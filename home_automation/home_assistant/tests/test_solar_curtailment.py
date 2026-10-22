"""Solar curtailment policy and runtime tests using real HA APIs."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError

import test_component as base


setUpModule = base.setUpModule
tearDownModule = base.tearDownModule


class SolarCurtailmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.HAComponentTests.asyncSetUp(self)
        self.module = importlib.import_module(
            base.COMPONENT + ".solar_curtailment"
        )
        self.const = importlib.import_module(base.COMPONENT + ".const")
        self.runtime_now = datetime(
            2026, 10, 19, 8, 0, tzinfo=timezone.utc
        )
        self.hass.states.async_set("sun.sun", "above_horizon")
        self.status = {
            "config_entry_id": "solar-entry",
            "serial": "synthetic-inverter",
            "mode": "percent",
            "configured": 100.0,
            "active": 100.0,
            "pmax_w": 8000.0,
            "active_power_w": 6200.0,
            "active_power_observed_at": self.runtime_now.isoformat(),
            "limit_minimum": 0.0,
            "limit_maximum": 100.0,
            "limit_step": 0.1,
            "write_enabled": True,
            "write_permission_basis": "synthetic_elevated",
            "observed_at": self.runtime_now.isoformat(),
        }
        self.device_writes = []

        async def get_status():
            self.status["active_power_observed_at"] = (
                self.runtime_now.isoformat()
            )
            self.status["observed_at"] = self.runtime_now.isoformat()
            return deepcopy(self.status)

        async def set_limit(arguments, *, automatic=False):
            self.assertTrue(automatic)
            self.device_writes.append(deepcopy(arguments))
            self.assertEqual(
                arguments["expected_config_entry_id"],
                self.status["config_entry_id"],
            )
            self.assertEqual(
                arguments["expected_serial"],
                self.status["serial"],
            )
            self.assertEqual(
                arguments["expected_pmax_w"],
                self.status["pmax_w"],
            )
            previous = deepcopy(self.status)
            self.status["mode"] = arguments["mode"]
            self.status["configured"] = arguments["value"]
            self.status["active"] = arguments["value"]
            confirmed = deepcopy(self.status)
            return {
                "changed": True,
                "phase": "verified",
                "previous": previous,
                "confirmed": confirmed,
                "audit_persisted": True,
                "audit_error": None,
            }

        self.set_limit = set_limit
        self.devices = SimpleNamespace(
            get_solar_status=AsyncMock(side_effect=get_status),
            set_solar_limit=AsyncMock(side_effect=set_limit),
            solar_write_available=Mock(return_value=True),
        )

    async def asyncTearDown(self):
        await base.HAComponentTests.asyncTearDown(self)

    def snapshot(
        self,
        exports: list[str],
        *,
        current_index: int = 1,
        profile_hash: str = "synthetic-profile",
    ):
        start = self.runtime_now - timedelta(minutes=15 * current_index)
        rows = tuple(
            self.pricing.UnitPrices(
                start=(start + index * timedelta(minutes=15)).isoformat(),
                end=(
                    start + (index + 1) * timedelta(minutes=15)
                ).isoformat(),
                spot_eur_kwh="0.05",
                import_eur_kwh="0.20",
                export_earnings_eur_kwh=value,
                evidence_bases=("published",),
            )
            for index, value in enumerate(exports)
        )
        window = self.pricing.PriceWindow(
            delivery_date="2026-10-19",
            start=rows[0].start,
            end=rows[-1].end,
            expected_intervals=len(rows),
            missing_market_intervals=0,
            complete=True,
            unavailable_reason=None,
            prices=rows,
        )
        empty = self.pricing.PriceWindow(
            delivery_date="2026-10-20",
            start=rows[-1].end,
            end=rows[-1].end,
            expected_intervals=0,
            missing_market_intervals=0,
            complete=False,
            unavailable_reason="synthetic",
            prices=(),
        )
        current = rows[current_index]
        return self.pricing.PriceSnapshot(
            calculated_at=self.runtime_now,
            valid_from=datetime.fromisoformat(current.start),
            valid_until=datetime.fromisoformat(current.end),
            profile_id="synthetic",
            profile_version="1",
            profile_sha256=profile_hash,
            current=current,
            today=window,
            tomorrow=empty,
        )

    def controller(self):
        return self.module.SolarCurtailment(
            self.hass,
            self.pricing_entry,
            self.coordinator,
            self.devices,
        )

    def commission(
        self,
        controller,
        *,
        enable_production_control=True,
    ):
        controller.production_control_enabled = enable_production_control
        identity = {
            "config_entry_id": self.status["config_entry_id"],
            "serial": self.status["serial"],
            "pmax_w": self.status["pmax_w"],
            "mode": self.status["mode"],
        }
        controller.state["commissioning"].update(
            {
                "policy_replay_verified": True,
                "write_permission_verified": True,
                "identity_verified": True,
                "pmax_verified": True,
                "mode_preservation_verified": True,
                "stable_unrestricted_verified": True,
                "observed_identity": identity,
                "stable_observation": {
                    "identity": identity,
                    "first_at": (
                        self.runtime_now - timedelta(seconds=30)
                    ).isoformat(),
                    "last_at": self.runtime_now.isoformat(),
                    "count": 2,
                },
            }
        )

    async def evaluate(self, controller, instant):
        self.runtime_now = instant
        self.now = instant
        await controller.async_evaluate(instant)

    def test_price_thresholds_and_native_mode_preserving_caps(self):
        self.assertTrue(
            self.module.is_shadow_opportunity(Decimal("-0.0001"))
        )
        self.assertFalse(
            self.module.is_armed_entry_price(Decimal("-0.009"))
        )
        self.assertTrue(
            self.module.is_armed_entry_price(Decimal("-0.010"))
        )
        percent = self.module.target_cap(self.status)
        self.assertEqual(percent["mode"], "percent")
        self.assertEqual(percent["value"], 50.0)
        self.assertEqual(percent["equivalent_w"], 4000.0)
        watts = self.module.target_cap(
            {
                **self.status,
                "mode": "watts",
                "configured": 8000,
                "active": 8000,
                "limit_maximum": 8000,
                "limit_step": 1,
            }
        )
        self.assertEqual(watts["mode"], "watts")
        self.assertEqual(watts["value"], 4000.0)

    def test_active_power_entity_changes_trigger_direct_observation(self):
        controller = self.controller()
        controller.devices.solar_entry = "solar-entry"
        entries = [
            SimpleNamespace(
                entity_id="sensor.inverter_active_power",
                unique_id="synthetic_active_power",
                original_name="Active power",
            ),
            SimpleNamespace(
                entity_id="sensor.inverter_daily_yield",
                unique_id="synthetic_daily_yield",
                original_name="Daily yield",
            ),
            SimpleNamespace(
                entity_id="number.inverter_active_power_limit",
                unique_id="synthetic_active_power_limit",
                original_name="Active power limit",
            ),
        ]
        with (
            patch.object(
                self.module.entity_registry,
                "async_get",
                return_value=object(),
            ),
            patch.object(
                self.module.entity_registry,
                "async_entries_for_config_entry",
                return_value=entries,
            ),
        ):
            self.assertEqual(
                controller._solar_observation_entities(),
                ("sensor.inverter_active_power",),
            )

        controller._schedule_evaluation = Mock()
        controller._inverter_observation_changed(None)
        controller._schedule_evaluation.assert_called_once_with()

    def test_negative_block_requires_known_end_and_exact_current_price(self):
        snapshot = self.snapshot(
            ["0.01", "-0.02", "-0.03", "0.00"]
        )
        context = self.module.price_context(
            snapshot, self.runtime_now
        )
        self.assertTrue(context.valid)
        self.assertEqual(context.export_earnings, Decimal("-0.02"))
        self.assertTrue(context.block.end_known)
        self.assertEqual(context.block.interval_count, 2)
        self.assertEqual(
            context.block.remaining_minutes(self.runtime_now),
            30,
        )
        unknown = self.module.price_context(
            self.snapshot(["0.01", "-0.02", "-0.03"]),
            self.runtime_now,
        )
        self.assertFalse(unknown.block.end_known)

    def test_negative_export_opportunity_never_claims_realized_savings(self):
        controller = self.controller()
        controller.last_price = self.module.price_context(
            self.snapshot(["0.01", "-0.02", "-0.03", "0.00"]),
            self.runtime_now,
        )

        self.assertEqual(
            controller.optimization_value,
            Decimal("0.02"),
        )
        attributes = controller.optimization_value_attributes
        self.assertEqual(attributes["valuation_status"], "counterfactual")
        self.assertEqual(
            attributes["opportunity_eur_per_kwh"],
            "0.02",
        )
        self.assertIsNone(attributes["measured_curtailed_energy_kwh"])
        self.assertIsNone(attributes["estimated_savings_eur"])
        self.assertIsNone(attributes["realized_savings_eur"])

    async def test_armed_is_gated_and_never_selected_automatically(self):
        controller = self.controller()
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "0.01"]
        )
        self.assertEqual(
            controller.available_modes,
            (
                self.const.SOLAR_CURTAILMENT_MODE_OFF,
                self.const.SOLAR_CURTAILMENT_MODE_SHADOW,
            ),
        )
        with self.assertRaisesRegex(
            HomeAssistantError, "Armed mode is disabled"
        ):
            await controller.async_set_mode(
                self.const.SOLAR_CURTAILMENT_MODE_ARMED
            )
        self.commission(
            controller,
            enable_production_control=False,
        )
        self.assertEqual(
            controller.mode,
            self.const.SOLAR_CURTAILMENT_MODE_SHADOW,
        )
        self.assertNotIn(
            self.const.SOLAR_CURTAILMENT_MODE_ARMED,
            controller.available_modes,
        )
        self.assertFalse(controller.attributes["write_allowed"])
        self.assertTrue(controller.attributes["shadow_zero_writes"])

    async def test_shadow_night_records_price_only_without_watt_proposal(self):
        controller = self.controller()
        self.hass.states.async_set("sun.sun", "below_horizon")
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )

        await self.evaluate(controller, self.runtime_now)

        self.assertEqual(
            controller.decision.state,
            "price_only_opportunity",
        )
        attributes = controller.attributes
        self.assertTrue(attributes["price_only_opportunity"])
        self.assertEqual(
            attributes["shadow_blocked_reason"],
            "below_horizon",
        )
        self.assertIsNone(
            attributes["qualified_proposed_limit_equivalent_w"]
        )
        self.assertIsNone(attributes["requested_limit_equivalent_w"])
        self.assertFalse(attributes["shadow_relevance_ready"])
        self.assertFalse(attributes["write_allowed"])
        self.devices.set_solar_limit.assert_not_awaited()

    async def test_shadow_daylight_dwell_exposes_only_qualified_proposal(self):
        controller = self.controller()
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now

        await self.evaluate(controller, start)
        self.assertEqual(controller.decision.state, "shadow_qualifying")
        self.assertIsNone(
            controller.attributes[
                "qualified_proposed_limit_equivalent_w"
            ]
        )
        await self.evaluate(controller, start + timedelta(seconds=30))
        self.assertEqual(controller.decision.state, "shadow_qualifying")
        await self.evaluate(controller, start + timedelta(seconds=60))

        self.assertEqual(controller.decision.state, "shadow_qualified")
        attributes = controller.attributes
        self.assertTrue(attributes["shadow_relevance_ready"])
        self.assertEqual(
            attributes["last_shadow_relevance_ready_at"],
            (start + timedelta(seconds=60)).isoformat(),
        )
        self.assertEqual(
            attributes["qualified_proposed_limit_equivalent_w"],
            4000.0,
        )
        self.assertIsNone(attributes["requested_limit_equivalent_w"])
        self.assertIsNone(attributes["shadow_blocked_reason"])
        self.assertFalse(attributes["write_allowed"])
        self.assertIsNone(controller.state["ownership"])
        self.devices.set_solar_limit.assert_not_awaited()

        self.status["active_power_w"] = 4000.0
        await self.evaluate(controller, start + timedelta(seconds=90))
        attributes = controller.attributes
        self.assertEqual(controller.decision.state, "shadow_blocked")
        self.assertFalse(attributes["shadow_relevance_ready"])
        self.assertEqual(
            attributes["last_shadow_relevance_ready_at"],
            (start + timedelta(seconds=60)).isoformat(),
        )
        self.assertEqual(
            attributes["shadow_blocked_reason"],
            "output_below_gate",
        )
        self.assertIsNone(
            attributes["qualified_proposed_limit_equivalent_w"]
        )
        self.assertIsNone(attributes["requested_limit_equivalent_w"])
        self.devices.set_solar_limit.assert_not_awaited()

    async def test_positive_price_still_refreshes_shadow_relevance(self):
        controller = self.controller()
        self.coordinator.data = self.snapshot(
            ["0.01", "0.01", "0.01", "0.01"]
        )
        start = self.runtime_now

        for seconds in (0, 30, 60):
            await self.evaluate(
                controller,
                start + timedelta(seconds=seconds),
            )

        self.assertEqual(controller.decision.state, "idle")
        self.assertTrue(
            controller.attributes["shadow_relevance_ready"]
        )
        self.assertEqual(
            self.devices.get_solar_status.await_count,
            3,
        )
        self.devices.set_solar_limit.assert_not_awaited()

    async def test_revision_two_ownerless_armed_state_migrates_without_write(self):
        controller = self.controller()
        self.coordinator.data = self.snapshot(
            ["0.01", "0.01", "0.01", "0.01"]
        )
        stored = {
            "mode": self.const.SOLAR_CURTAILMENT_MODE_ARMED,
            **controller.state,
            "policy_revision": 2,
            "ownership": None,
        }
        await controller.state_file.async_save(stored)

        await controller.async_start()

        self.assertEqual(
            controller.mode,
            self.const.SOLAR_CURTAILMENT_MODE_SHADOW,
        )
        self.assertEqual(
            controller.available_modes,
            (
                self.const.SOLAR_CURTAILMENT_MODE_OFF,
                self.const.SOLAR_CURTAILMENT_MODE_SHADOW,
            ),
        )
        self.assertFalse(controller.attributes["write_allowed"])
        self.devices.set_solar_limit.assert_not_awaited()
        for unsubscribe in controller.unsubscribers:
            unsubscribe()
        controller.unsubscribers.clear()

    async def test_revision_two_armed_migration_blocks_restricted_baseline(self):
        controller = self.controller()
        self.status["configured"] = 80.0
        self.status["active"] = 80.0
        self.coordinator.data = self.snapshot(
            ["0.01", "0.01", "0.01", "0.01"]
        )
        stored = {
            "mode": self.const.SOLAR_CURTAILMENT_MODE_ARMED,
            **controller.state,
            "policy_revision": 2,
            "ownership": None,
        }
        await controller.state_file.async_save(stored)

        await controller.async_start()

        self.assertEqual(
            controller.mode,
            self.const.SOLAR_CURTAILMENT_MODE_SHADOW,
        )
        self.assertEqual(controller.decision.state, "migration_blocked")
        self.assertIsNotNone(
            controller.attributes["migration_blocked_reason"]
        )
        self.assertEqual(
            controller.attributes["shadow_blocked_reason"],
            None,
        )
        self.assertFalse(controller.attributes["write_allowed"])
        self.devices.set_solar_limit.assert_not_awaited()
        for unsubscribe in controller.unsubscribers:
            unsubscribe()
        controller.unsubscribers.clear()

    async def test_dwell_two_samples_and_one_mode_preserving_acquisition(self):
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        await self.evaluate(controller, start)
        self.assertEqual(controller.decision.state, "qualifying")
        await self.evaluate(controller, start + timedelta(seconds=30))
        self.assertEqual(controller.decision.state, "qualifying")
        self.devices.set_solar_limit.assert_not_awaited()
        await self.evaluate(controller, start + timedelta(seconds=60))
        self.devices.set_solar_limit.assert_awaited_once()
        self.assertEqual(controller.decision.state, "owned")
        request = self.device_writes[0]
        self.assertEqual(request["mode"], "percent")
        self.assertEqual(request["value"], 50.0)
        self.assertNotIn("allow_mode_change", request)
        owner = controller.state["ownership"]
        self.assertEqual(owner["original"]["configured"], 100.0)
        self.assertEqual(owner["target"]["configured"], 50.0)
        self.assertEqual(
            owner["identity"],
            {
                "config_entry_id": "solar-entry",
                "serial": "synthetic-inverter",
                "pmax_w": 8000.0,
            },
        )
        self.assertEqual(
            controller.attributes["configured_limit_equivalent_w"],
            4000.0,
        )
        self.assertEqual(
            controller.attributes["active_limit_equivalent_w"],
            4000.0,
        )
        self.assertEqual(
            controller.attributes["requested_limit_equivalent_w"],
            4000.0,
        )
        self.assertTrue(controller.attributes["daylight_gate_passed"])
        self.assertFalse(controller.attributes["acquisition_allowed"])

    async def test_daylight_and_output_reset_acquisition_qualification(self):
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        self.hass.states.async_set("sun.sun", "below_horizon")
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        self.assertEqual(controller.decision.state, "qualifying")
        self.assertIn("above the horizon", controller.decision.reason)
        self.assertIsNone(controller.state["qualification"])
        self.devices.set_solar_limit.assert_not_awaited()

        self.hass.states.async_set("sun.sun", "above_horizon")
        await self.evaluate(controller, start + timedelta(seconds=90))
        self.assertEqual(controller.decision.state, "qualifying")
        self.assertEqual(
            controller.state["qualification"]["sample_count"],
            1,
        )
        self.status["active_power_w"] = 1000
        await self.evaluate(controller, start + timedelta(seconds=120))
        self.assertIsNone(controller.state["qualification"])
        self.devices.set_solar_limit.assert_not_awaited()

    def test_commissioning_stability_requires_daylight_output_and_full_dwell(self):
        controller = self.controller()
        self.hass.states.async_set("sun.sun", "below_horizon")
        controller._update_commissioning_observation(
            deepcopy(self.status),
            self.runtime_now,
        )
        self.assertFalse(
            controller.state["commissioning"][
                "stable_unrestricted_verified"
            ]
        )

        self.hass.states.async_set("sun.sun", "above_horizon")
        for seconds in (0, 30):
            status = deepcopy(self.status)
            status["active_power_observed_at"] = (
                self.runtime_now + timedelta(seconds=seconds)
            ).isoformat()
            controller._update_commissioning_observation(
                status,
                self.runtime_now + timedelta(seconds=seconds),
            )
        self.assertFalse(
            controller.state["commissioning"][
                "stable_unrestricted_verified"
            ]
        )
        status = deepcopy(self.status)
        status["active_power_observed_at"] = (
            self.runtime_now + timedelta(seconds=60)
        ).isoformat()
        controller._update_commissioning_observation(
            status,
            self.runtime_now + timedelta(seconds=60),
        )
        self.assertTrue(
            controller.state["commissioning"][
                "stable_unrestricted_verified"
            ]
        )

    async def test_revision_one_migration_requalifies_daylight_commissioning(self):
        controller = self.controller()
        self.commission(controller)
        stored = {
            "mode": self.const.SOLAR_CURTAILMENT_MODE_ARMED,
            **deepcopy(controller.state),
            "policy_revision": 1,
        }
        controller.state_file = SimpleNamespace(
            async_load=AsyncMock(return_value=stored),
            async_save=AsyncMock(),
        )
        controller.store = SimpleNamespace(
            async_load=AsyncMock(return_value=None),
        )
        controller.async_evaluate = AsyncMock()

        await controller.async_start()
        try:
            self.assertEqual(
                controller.mode,
                self.const.SOLAR_CURTAILMENT_MODE_SHADOW,
            )
            commissioning = controller.state["commissioning"]
            self.assertFalse(
                commissioning["stable_unrestricted_verified"]
            )
            self.assertIsNone(commissioning["stable_observation"])
            self.assertEqual(
                commissioning["daylight_state"],
                "above_horizon",
            )
            self.assertEqual(commissioning["active_power_w"], 6200.0)
            self.assertFalse(commissioning["active_power_fresh"])
        finally:
            await controller.async_stop()

    async def test_watt_mode_is_preserved(self):
        self.status.update(
            {
                "mode": "watts",
                "configured": 8000.0,
                "active": 8000.0,
                "limit_maximum": 8000.0,
                "limit_step": 1.0,
            }
        )
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        self.assertEqual(self.device_writes[0]["mode"], "watts")
        self.assertEqual(self.device_writes[0]["value"], 4000.0)

    async def test_clouds_after_acquisition_do_not_restore_but_lease_does(self):
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        self.status["active_power_w"] = 1000
        await self.evaluate(controller, start + timedelta(seconds=75))
        self.assertEqual(controller.decision.state, "owned")
        self.assertEqual(len(self.device_writes), 1)
        controller.state["ownership"]["lease_expires"] = (
            start + timedelta(seconds=80)
        ).isoformat()
        await self.evaluate(controller, start + timedelta(seconds=81))
        self.assertEqual(controller.decision.state, "restored")
        self.assertEqual(len(self.device_writes), 2)
        self.assertEqual(self.device_writes[-1]["value"], 100.0)
        self.assertIsNone(controller.state["ownership"])
        history = controller.operation_history
        self.assertEqual(
            [
                (item["operation"], item["phase"], item["success"])
                for item in history
            ],
            [
                ("acquire", "verified", True),
                ("restore", "verified", True),
            ],
        )
        self.assertEqual(
            history[0]["previous"]["configured"],
            100.0,
        )
        self.assertEqual(
            history[0]["confirmed"]["configured"],
            50.0,
        )
        self.assertEqual(
            history[1]["confirmed"]["configured"],
            100.0,
        )

    async def test_after_sunset_zero_output_does_not_block_safe_restore(self):
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        self.assertIsNotNone(controller.state["ownership"])

        self.hass.states.async_set("sun.sun", "below_horizon")
        self.status["active_power_w"] = 0
        self.coordinator.data = self.snapshot(
            ["0.01", "0.01", "0.01", "0.01", "0.01"]
        )
        await self.evaluate(controller, start + timedelta(seconds=75))

        self.assertEqual(controller.decision.state, "restored")
        self.assertIsNone(controller.state["ownership"])
        self.assertEqual(self.device_writes[-1]["value"], 100.0)

    async def test_profile_change_restores_exact_original(self):
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"],
            profile_hash="changed-profile",
        )
        await self.evaluate(controller, start + timedelta(seconds=75))
        self.assertEqual(controller.decision.state, "restored")
        self.assertEqual(self.device_writes[-1]["value"], 100.0)
        self.assertIn(
            "profile changed",
            controller.state["last_operation"]["reason"],
        )
        self.assertIn(
            controller.last_price.block.identity,
            controller.state["suppressed_blocks"],
        )

    async def test_external_override_cancels_and_suppresses_block(self):
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        block_id = controller.state["ownership"]["block_id"]
        self.status["configured"] = 75.0
        self.status["active"] = 75.0
        await self.evaluate(controller, start + timedelta(seconds=75))
        self.assertEqual(controller.decision.state, "external_override")
        self.assertIsNone(controller.state["ownership"])
        self.assertIn(block_id, controller.state["suppressed_blocks"])
        self.assertEqual(len(self.device_writes), 1)
        await self.evaluate(controller, start + timedelta(seconds=90))
        self.assertEqual(controller.decision.state, "suppressed")
        self.assertEqual(len(self.device_writes), 1)

    async def test_predispatch_failure_requalifies_without_consuming_attempt(self):
        calls = 0

        async def predispatch_once(arguments, *, automatic=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise self.module.SolarPreDispatchError(
                    "Synthetic pre-dispatch failure."
                )
            return await self.set_limit(
                arguments,
                automatic=automatic,
            )

        self.devices.set_solar_limit.side_effect = predispatch_once
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        self.assertEqual(
            controller.decision.state,
            "pre_dispatch_failed",
        )
        self.assertEqual(controller.state["attempted_blocks"], [])
        self.assertIsNone(controller.state["ownership"])
        for seconds in (90, 120, 150):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        self.assertEqual(controller.decision.state, "owned")
        self.assertEqual(calls, 2)

    async def test_acknowledged_state_failure_prevents_dispatch_even_if_store_swallows(self):
        controller = self.controller()
        controller.production_control_enabled = True
        controller.store.async_save = AsyncMock(return_value=None)
        controller.state_file.async_save = AsyncMock(
            side_effect=OSError("Synthetic atomic state failure.")
        )
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        block = self.module.price_context(
            self.coordinator.data, self.runtime_now
        ).block
        target = self.module.target_cap(self.status)
        with self.assertLogs(self.module.LOGGER, level="ERROR"):
            await controller._acquire(
                deepcopy(self.status),
                target,
                block,
                self.runtime_now,
            )
        self.assertEqual(
            self.devices.set_solar_limit.await_count,
            0,
        )
        controller.store.async_save.assert_not_awaited()
        self.assertIsNone(controller.state["ownership"])
        self.assertEqual(controller.state["attempted_blocks"], [])
        self.assertEqual(
            controller.state["last_operation"]["phase"],
            "pre_dispatch_persistence",
        )

    async def test_acknowledged_state_file_is_private_and_serialization_strict(self):
        controller = self.controller()
        value = {"mode": controller.mode, **controller.state}
        await controller.state_file.async_save(value)
        path = (
            Path(self.config_directory.name)
            / ".storage"
            / controller.state_file.filename
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(await controller.state_file.async_load(), value)
        with self.assertRaises(TypeError):
            await controller.state_file.async_save(
                {"not_json": object()}
            )
        self.assertEqual(await controller.state_file.async_load(), value)

    async def test_failed_state_load_cleanup_preserves_unread_ownership_file(self):
        controller = self.controller()
        value = {
            "mode": self.const.SOLAR_CURTAILMENT_MODE_ARMED,
            **controller.state,
            "ownership": {
                "phase": "owned",
                "identity": {
                    "config_entry_id": "solar-entry",
                    "serial": "synthetic-inverter",
                    "pmax_w": 8000.0,
                },
            },
        }
        await controller.state_file.async_save(value)
        path = (
            Path(self.config_directory.name)
            / ".storage"
            / controller.state_file.filename
        )
        original = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            await controller.async_start()
        await controller.async_stop()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertTrue(controller.stopped)

    async def test_final_state_failure_preserves_verified_owned_state(self):
        controller = self.controller()
        controller.production_control_enabled = True
        controller.state_file.async_save = AsyncMock(
            side_effect=[
                None,
                OSError("Synthetic atomic state failure."),
            ]
        )
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        block = self.module.price_context(
            self.coordinator.data, self.runtime_now
        ).block
        target = self.module.target_cap(self.status)
        with self.assertLogs(self.module.LOGGER, level="ERROR"):
            await controller._acquire(
                deepcopy(self.status),
                target,
                block,
                self.runtime_now,
            )
        self.assertEqual(
            self.devices.set_solar_limit.await_count,
            1,
        )
        self.assertEqual(
            controller.state["ownership"]["phase"], "owned"
        )
        self.assertIsNone(controller.state["fault"])
        self.assertEqual(
            controller.state["persistence_fault"]["kind"], "state"
        )
        self.assertEqual(controller.decision.state, "persistence_fault")
        self.assertEqual(
            controller.state["last_operation"]["phase"],
            "verified_state_persistence_failed",
        )
        self.assertTrue(
            controller.state["last_operation"]["success"]
        )
        self.assertFalse(
            controller.state["last_operation"]["state_persisted"]
        )

    async def test_verified_audit_failure_is_separate_from_device_state(self):
        async def verified_without_audit(arguments, *, automatic=False):
            result = await self.set_limit(
                arguments,
                automatic=automatic,
            )
            result["audit_persisted"] = False
            result["audit_error"] = "OSError: synthetic audit failure"
            return result

        self.devices.set_solar_limit.side_effect = verified_without_audit
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        block = self.module.price_context(
            self.coordinator.data, self.runtime_now
        ).block
        await controller._acquire(
            deepcopy(self.status),
            self.module.target_cap(self.status),
            block,
            self.runtime_now,
        )
        self.assertEqual(
            controller.state["ownership"]["phase"], "owned"
        )
        self.assertIsNone(controller.state["fault"])
        self.assertEqual(
            controller.state["persistence_fault"]["kind"], "audit"
        )
        self.assertEqual(
            controller.state["last_operation"]["device_outcome"],
            "target_verified",
        )
        self.assertEqual(controller.decision.state, "persistence_fault")
        button_module = importlib.import_module(base.COMPONENT + ".button")
        resume = button_module.SolarCurtailmentResumeButton(
            controller,
            self.pricing_entry.entry_id,
        )
        self.assertTrue(resume.available)
        self.now = self.runtime_now
        await resume.async_press()
        self.assertFalse(resume.available)
        self.assertIsNone(controller.state["persistence_fault"])
        self.assertIsNotNone(controller.state["ownership"])
        self.assertEqual(controller.decision.state, "owned")

    async def test_restore_state_failure_keeps_verified_original_classification(self):
        controller = self.controller()
        controller.production_control_enabled = True
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        block = self.module.price_context(
            self.coordinator.data, self.runtime_now
        ).block
        await controller._acquire(
            deepcopy(self.status),
            self.module.target_cap(self.status),
            block,
            self.runtime_now,
        )
        controller.state_file.async_save = AsyncMock(
            side_effect=[
                None,
                OSError("Synthetic restore state failure."),
            ]
        )
        await controller._restore(
            deepcopy(self.status),
            "Synthetic policy exit.",
            self.runtime_now + timedelta(seconds=1),
        )
        self.assertEqual(self.status["configured"], 100.0)
        self.assertEqual(self.status["active"], 100.0)
        self.assertIsNone(controller.state["ownership"])
        self.assertIsNone(controller.state["fault"])
        self.assertEqual(
            controller.state["persistence_fault"]["kind"], "state"
        )
        self.assertEqual(
            controller.state["last_operation"]["device_outcome"],
            "original_verified",
        )
        self.assertEqual(
            controller.state["last_operation"]["phase"],
            "verified_state_persistence_failed",
        )

    async def test_final_recheck_override_suppresses_without_dispatch(self):
        controller = self.controller()
        controller.production_control_enabled = True
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        block = self.module.price_context(
            self.coordinator.data, self.runtime_now
        ).block
        changed = {
            **self.status,
            "active_power_w": 6100.0,
        }
        self.devices.set_solar_limit.side_effect = (
            self.module.SolarExternalOverrideError(
                "Synthetic final recheck override.",
                changed,
            )
        )
        await controller._acquire(
            deepcopy(self.status),
            self.module.target_cap(self.status),
            block,
            self.runtime_now,
        )
        self.assertEqual(self.device_writes, [])
        self.assertIsNone(controller.state["ownership"])
        self.assertIn(
            block.identity, controller.state["suppressed_blocks"]
        )
        self.assertNotIn(
            block.identity, controller.state["attempted_blocks"]
        )
        self.assertEqual(
            controller.state["last_operation"]["phase"],
            "pre_dispatch_external_override",
        )

    async def test_stop_cancels_queued_evaluations_and_rejects_acquisition(self):
        controller = self.controller()
        controller._state_loaded = True
        controller.state["qualification"] = {
            "block_id": "synthetic",
        }
        await controller.lock.acquire()
        controller._schedule_evaluation()
        controller._schedule_evaluation()
        await asyncio.sleep(0)
        stop_task = asyncio.create_task(controller.async_stop())
        await asyncio.sleep(0)
        self.assertTrue(controller.stopping)
        controller.lock.release()
        await stop_task
        self.assertTrue(controller.stopped)
        self.assertEqual(controller.evaluation_tasks, set())
        self.assertIsNone(controller.state["qualification"])

        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        block = self.module.price_context(
            self.coordinator.data, self.runtime_now
        ).block
        await controller._acquire(
            deepcopy(self.status),
            self.module.target_cap(self.status),
            block,
            self.runtime_now,
        )
        self.devices.set_solar_limit.assert_not_awaited()
        self.assertIsNone(controller.state["ownership"])
        self.assertEqual(controller.decision.state, "stopping")

    async def test_startup_dependency_defers_before_loaded_read_failure(self):
        controller = self.controller()
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        context = self.module.price_context(
            self.coordinator.data, self.runtime_now
        )
        owner = {
            "phase": "owned",
            "identity": {
                "config_entry_id": "solar-entry",
                "serial": "synthetic-inverter",
                "pmax_w": 8000.0,
            },
            "block_id": context.block.identity,
            "block_end": context.block.end.isoformat(),
            "profile_signature": context.profile_signature,
            "original": {
                "mode": "percent",
                "configured": 100.0,
                "active": 100.0,
            },
            "target": {
                "mode": "percent",
                "configured": 50.0,
                "active": 50.0,
            },
            "lease_expires": (
                self.runtime_now + timedelta(minutes=60)
            ).isoformat(),
            "pending_at": self.runtime_now.isoformat(),
            "acquired_at": self.runtime_now.isoformat(),
        }
        stored = {
            "mode": self.const.SOLAR_CURTAILMENT_MODE_ARMED,
            **controller.state,
            "ownership": owner,
        }
        stored["commissioning"] = {
            **stored["commissioning"],
            **{
                key: True
                for key in controller.commissioning_gates
            },
        }
        await controller.state_file.async_save(stored)
        self.devices.get_solar_status.side_effect = (
            self.module.SolarDependencyNotReady(
                "Synthetic Huawei setup in progress."
            )
        )
        restarted = self.controller()
        await restarted.async_start()
        self.assertTrue(restarted.startup_reconcile_pending)
        self.assertIsNone(restarted.state["fault"])
        self.assertEqual(restarted.decision.state, "startup_deferred")

        self.devices.get_solar_status.side_effect = HomeAssistantError(
            "Synthetic loaded reconciliation read failure."
        )
        await restarted.async_evaluate(
            self.runtime_now + timedelta(seconds=1)
        )
        self.assertIn(
            "reconciliation read failure", restarted.state["fault"]
        )
        self.assertEqual(restarted.decision.state, "fault")
        for unsubscribe in restarted.unsubscribers:
            unsubscribe()
        restarted.unsubscribers.clear()

    async def test_quarter_rollover_holds_owned_cap_until_refresh_result(self):
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        block_id = controller.state["ownership"]["block_id"]
        rollover = start + timedelta(minutes=15)
        self.coordinator._quarter_refresh_started_at = rollover
        self.coordinator._quarter_refresh_hold_until = (
            rollover + timedelta(seconds=60)
        )
        await self.evaluate(controller, rollover)
        self.assertEqual(controller.decision.state, "owned_refreshing")
        self.assertEqual(len(self.device_writes), 1)
        self.assertEqual(
            controller.state["ownership"]["block_id"], block_id
        )
        self.assertEqual(controller.state["suppressed_blocks"], [])

        self.runtime_now = rollover
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "0.01", "0.01"],
            current_index=2,
        )
        await self.evaluate(
            controller, rollover + timedelta(seconds=1)
        )
        self.assertEqual(controller.decision.state, "restored")
        self.assertEqual(len(self.device_writes), 2)
        self.assertIsNone(controller.state["ownership"])
        self.assertEqual(controller.state["suppressed_blocks"], [])

    async def test_postdispatch_failure_faults_without_controller_retry(self):
        async def uncertain(_arguments, *, automatic=False):
            self.assertTrue(automatic)
            raise self.module.SolarPostDispatchError(
                "Synthetic uncertain dispatch."
            )

        self.devices.set_solar_limit.side_effect = uncertain
        controller = self.controller()
        controller.mode = self.const.SOLAR_CURTAILMENT_MODE_ARMED
        self.commission(controller)
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        start = self.runtime_now
        for seconds in (0, 30, 60):
            await self.evaluate(
                controller, start + timedelta(seconds=seconds)
            )
        self.assertEqual(controller.decision.state, "fault")
        self.assertIn("uncertain", controller.state["fault"])
        self.assertEqual(
            controller.state["ownership"]["phase"],
            "pending_acquire",
        )
        self.assertEqual(
            self.devices.set_solar_limit.await_count,
            1,
        )
        await self.evaluate(controller, start + timedelta(seconds=90))
        self.assertEqual(
            self.devices.set_solar_limit.await_count,
            1,
        )

    async def test_revision_three_restart_restores_owned_cap_and_clears_original(self):
        controller = self.controller()
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "-0.02", "0.01"]
        )
        context = self.module.price_context(
            self.coordinator.data, self.runtime_now
        )
        owner = {
            "phase": "owned",
            "identity": {
                "config_entry_id": "solar-entry",
                "serial": "synthetic-inverter",
                "pmax_w": 8000.0,
            },
            "block_id": context.block.identity,
            "block_end": context.block.end.isoformat(),
            "profile_signature": context.profile_signature,
            "original": {
                "mode": "percent",
                "configured": 100.0,
                "active": 100.0,
            },
            "target": {
                "mode": "percent",
                "configured": 50.0,
                "active": 50.0,
            },
            "lease_expires": (
                self.runtime_now + timedelta(minutes=60)
            ).isoformat(),
            "pending_at": self.runtime_now.isoformat(),
            "acquired_at": self.runtime_now.isoformat(),
        }
        stored = {
            "mode": self.const.SOLAR_CURTAILMENT_MODE_ARMED,
            **controller.state,
            "ownership": owner,
        }
        stored["commissioning"] = {
            **stored["commissioning"],
            **{
                key: True
                for key in controller.commissioning_gates
            },
        }
        await controller.state_file.async_save(stored)
        self.status["configured"] = 50.0
        self.status["active"] = 50.0
        self.now = self.runtime_now
        restarted = self.controller()
        await restarted.async_start()
        self.assertIsNone(restarted.state["ownership"])
        self.assertEqual(
            restarted.mode,
            self.const.SOLAR_CURTAILMENT_MODE_SHADOW,
        )
        self.assertEqual(restarted.decision.state, "restored")
        self.assertEqual(self.status["configured"], 100.0)
        self.assertEqual(self.status["active"], 100.0)
        for unsubscribe in restarted.unsubscribers:
            unsubscribe()
        restarted.unsubscribers.clear()

        self.devices.set_solar_limit.reset_mock()
        stored["mode"] = self.const.SOLAR_CURTAILMENT_MODE_SHADOW
        await controller.state_file.async_save(stored)
        self.now = self.runtime_now
        restored = self.controller()
        await restored.async_start()
        self.assertIsNone(restored.state["ownership"])
        self.assertEqual(restored.mode, self.const.SOLAR_CURTAILMENT_MODE_SHADOW)
        self.devices.set_solar_limit.assert_not_awaited()
        for unsubscribe in restored.unsubscribers:
            unsubscribe()
        restored.unsubscribers.clear()

    async def test_pending_restart_latches_fault_until_explicit_resume(self):
        controller = self.controller()
        owner = {
            "phase": "pending_acquire",
            "identity": {
                "config_entry_id": "solar-entry",
                "serial": "synthetic-inverter",
                "pmax_w": 8000.0,
            },
            "block_id": "synthetic-block",
            "original": {
                "mode": "percent",
                "configured": 100.0,
                "active": 100.0,
            },
            "target": {
                "mode": "percent",
                "configured": 50.0,
                "active": 50.0,
            },
            "lease_expires": (
                self.runtime_now + timedelta(minutes=60)
            ).isoformat(),
        }
        await controller.state_file.async_save(
            {
                "mode": self.const.SOLAR_CURTAILMENT_MODE_SHADOW,
                **controller.state,
                "ownership": owner,
            }
        )
        self.now = self.runtime_now
        restarted = self.controller()
        await restarted.async_start()
        self.assertIn("uncertain", restarted.state["fault"])
        self.assertEqual(restarted.decision.state, "fault")
        self.devices.set_solar_limit.assert_not_awaited()
        for unsubscribe in restarted.unsubscribers:
            unsubscribe()
        restarted.unsubscribers.clear()

    async def test_attributes_are_honest_and_do_not_claim_energy_or_savings(self):
        controller = self.controller()
        self.coordinator.data = self.snapshot(
            ["0.01", "-0.02", "-0.02", "0.01"]
        )
        await self.evaluate(controller, self.runtime_now)
        attributes = controller.attributes
        self.assertEqual(
            attributes["cost_per_avoided_exported_kwh_eur"],
            "0.02",
        )
        self.assertIn("eligible_minutes", attributes)
        self.assertIn("owned_minutes", attributes)
        self.assertFalse(attributes["measured_grid_export_available"])
        keys = " ".join(attributes).lower()
        self.assertNotIn("curtailed_kwh", keys)
        self.assertNotIn("savings_eur", keys)


if __name__ == "__main__":
    unittest.main()
