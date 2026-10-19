"""Deterministic DHW policy and controller tests using real HA APIs."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import importlib
import inspect
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.exceptions import HomeAssistantError

import test_component as base


setUpModule = base.setUpModule
tearDownModule = base.tearDownModule


class DhwOptimizerTests(base.HAComponentTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.module = importlib.import_module(base.COMPONENT + ".dhw_optimizer")
        self.const = importlib.import_module(base.COMPONENT + ".const")
        self.policy = self.module.DhwPolicy.from_options({})
        flags = {name: False for name in importlib.import_module(base.COMPONENT + ".controls").FLAGS}
        self.status = {
            "heating": {
                "states": dict(flags),
                "operations": {"OperationMode": "heating", "Power": "on"},
            },
            "hot_water": {
                "sensors": {"TankTemperature": 51.0},
                "states": {**flags, "WeatherDependentState": False},
                "operations": {
                    "Power": "on",
                    "OperationMode": "heating",
                    "DomesticHotWaterTemperatureHeating": 50.0,
                    "powerful": "0",
                },
            },
            "effective_hot_water_target": 50.0,
        }
        self.writes = []

        async def control(args, context):
            self.writes.append(dict(args))
            value = float(args["value"])
            self.status["hot_water"]["operations"]["DomesticHotWaterTemperatureHeating"] = value
            self.status["effective_hot_water_target"] = value
            return {
                "changed": True,
                "operation": "hot_water_target",
                "previous": 50.0,
                "confirmed": value,
            }

        self.devices = SimpleNamespace(
            get_daikin_status=AsyncMock(side_effect=lambda: self.status),
            control_daikin=AsyncMock(side_effect=control),
        )

    def intervals(self, start: datetime, prices: list[str]):
        return tuple(
            self.module.PriceInterval(
                start + index * timedelta(minutes=15),
                start + (index + 1) * timedelta(minutes=15),
                Decimal(value),
            )
            for index, value in enumerate(prices)
        )

    def observation(self, **changes):
        values = {
            "tank_temperature": 51.0,
            "target_temperature": 50.0,
            "effective_target": 50.0,
            "enabled": True,
            "override_reason": None,
        }
        values.update(changes)
        return self.module.DhwObservation(**values)

    def test_policy_defaults_and_invalid_target_order(self):
        self.assertEqual(self.policy.soft_target, 30)
        self.assertEqual(self.policy.floor_target, 45)
        self.assertEqual(self.policy.normal_target, 50)
        self.assertEqual(self.policy.preheat_target, 55)
        with self.assertRaisesRegex(HomeAssistantError, "soft < floor"):
            self.module.DhwPolicy.from_options({"dhw_soft_target": 46})

    def test_readiness_pilot_only_boosts_inside_final_allowance(self):
        policy = self.module.DhwPolicy.from_options(
            {"dhw_initial_heat_minutes": 90}
        )
        outside = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        prices = self.intervals(
            outside,
            ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 20,
        )
        hold = self.module.plan_dhw(
            now=outside,
            mode=self.const.DHW_MODE_READINESS,
            policy=policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=90,
            lease_expires=None,
        )
        self.assertEqual(hold.state, "readiness_pilot")
        self.assertEqual(hold.desired_target, 50)
        self.assertTrue(hold.allow_write)

        inside = datetime.fromisoformat("2026-10-15T03:45:00+00:00")
        charge = self.module.plan_dhw(
            now=inside,
            mode=self.const.DHW_MODE_READINESS,
            policy=policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=90,
            lease_expires=None,
        )
        self.assertEqual(charge.state, "readiness_charge")
        self.assertEqual(charge.desired_target, 55)
        self.assertTrue(charge.urgent)

        ready = self.module.plan_dhw(
            now=inside,
            mode=self.const.DHW_MODE_READINESS,
            policy=policy,
            observation=self.observation(tank_temperature=50, target_temperature=55),
            intervals=prices,
            heating_minutes=90,
            lease_expires=None,
        )
        self.assertEqual(ready.state, "readiness")
        self.assertEqual(ready.desired_target, 50)
        self.assertTrue(ready.urgent)

    def test_shadow_proposes_same_readiness_boost_without_writes(self):
        now = datetime.fromisoformat("2026-10-15T03:45:00+00:00")
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.module.DhwPolicy.from_options(
                {"dhw_initial_heat_minutes": 90}
            ),
            observation=self.observation(tank_temperature=49),
            intervals=self.intervals(now, ["0.20"] * 16),
            heating_minutes=90,
            lease_expires=None,
        )
        self.assertEqual(decision.state, "readiness_charge")
        self.assertEqual(decision.desired_target, 55)
        self.assertFalse(decision.allow_write)

    def test_shadow_selects_cheapest_contiguous_window_without_writes(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        prices = ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(),
            intervals=self.intervals(now, prices),
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(decision.state, "coast")
        self.assertEqual(decision.desired_target, 45)
        self.assertFalse(decision.allow_write)
        self.assertEqual(decision.selected_window.start, now + timedelta(hours=1))
        self.assertEqual(decision.selected_window.average, Decimal("0.10"))

        in_window = self.module.plan_dhw(
            now=now + timedelta(hours=1),
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(target_temperature=45),
            intervals=self.intervals(now, prices),
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(in_window.state, "preheat")
        self.assertEqual(in_window.desired_target, 55)
        self.assertFalse(in_window.allow_write)

    def test_negative_prices_use_sign_safe_balanced_gate(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        prices = ["-0.04"] * 4 + ["-0.10"] * 4 + ["0.10"] * 8
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(),
            intervals=self.intervals(now, prices),
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertTrue(decision.price_gate)
        self.assertEqual(decision.price_delta, Decimal("0.06"))
        self.assertEqual(decision.state, "coast")

    def test_flat_prices_avoid_unnecessary_55_degree_preheat(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        policy = self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60})
        window = self.module.PriceWindowChoice(
            start=now,
            end=now + timedelta(hours=1),
            average=Decimal("0.20"),
            interval_count=4,
        )
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=policy,
            observation=self.observation(),
            intervals=self.intervals(now, ["0.20"] * 16),
            heating_minutes=60,
            lease_expires=None,
            planned_window=window,
            comparison_window=window,
        )
        self.assertFalse(decision.preheat_price_gate)
        self.assertEqual(decision.state, "normal")
        self.assertEqual(decision.desired_target, 50)
        self.assertIn("unnecessary 55°C preheating", decision.reason)

    def test_anchored_window_does_not_chase_each_remaining_quarter(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        policy = self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60})
        prices = self.intervals(
            now,
            ["0.40"] * 4 + ["0.10"] * 4 + ["0.20"] * 8,
        )
        initial = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=policy,
            observation=self.observation(),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(initial.selected_window.start, now + timedelta(hours=1))
        self.assertEqual(initial.selected_window.end, now + timedelta(hours=2))

        later = now + timedelta(hours=1, minutes=15)
        rolling = self.module.plan_dhw(
            now=later,
            mode=self.const.DHW_MODE_SHADOW,
            policy=policy,
            observation=self.observation(),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(rolling.selected_window.end, now + timedelta(hours=2, minutes=15))

        anchored = self.module.plan_dhw(
            now=later,
            mode=self.const.DHW_MODE_SHADOW,
            policy=policy,
            observation=self.observation(),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=initial.selected_window,
            comparison_window=initial.comparison_window,
        )
        self.assertEqual(anchored.selected_window, initial.selected_window)
        self.assertEqual(anchored.state, "preheat")

        after_window = self.module.plan_dhw(
            now=now + timedelta(hours=2),
            mode=self.const.DHW_MODE_SHADOW,
            policy=policy,
            observation=self.observation(),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=initial.selected_window,
            comparison_window=initial.comparison_window,
        )
        self.assertNotEqual(after_window.state, "preheat")
        self.assertEqual(after_window.selected_window, initial.selected_window)

    def test_completed_preheat_does_not_reenter_post_window_recovery(self):
        start = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        policy = self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60})
        prices = self.intervals(
            start,
            ["0.40"] * 4 + ["0.10"] * 4 + ["0.40"] * 12,
        )
        selected = self.module.PriceWindowChoice(
            start=start + timedelta(hours=1),
            end=start + timedelta(hours=2),
            average=Decimal("0.10"),
            interval_count=4,
        )
        comparison = self.module.PriceWindowChoice(
            start=start + timedelta(hours=2),
            end=start + timedelta(hours=3),
            average=Decimal("0.40"),
            interval_count=4,
        )
        now = start + timedelta(hours=2, minutes=15)

        incomplete = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            comparison_window=comparison,
        )
        self.assertEqual(incomplete.state, "recovery")

        completed = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            comparison_window=comparison,
            preheat_completed=True,
        )
        self.assertEqual(completed.state, "coast")
        self.assertEqual(completed.desired_target, 45)

    def test_preheat_completion_persists_only_for_the_anchored_deadline(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        deadline = datetime.fromisoformat("2026-10-15T18:00:00+00:00")
        selected = self.module.PriceWindowChoice(
            start=deadline - timedelta(hours=4),
            end=deadline - timedelta(hours=3),
            average=Decimal("0.10"),
            interval_count=4,
        )
        optimizer._store_price_plan(deadline, selected, selected)
        optimizer._update_preheat_completion(
            selected.start + timedelta(minutes=30),
            self.observation(tank_temperature=50),
            selected,
        )
        completed_at = optimizer.state["planned_window_completed_at"]
        self.assertIsNotNone(completed_at)
        optimizer._stored_price_plan(deadline)
        self.assertEqual(
            optimizer.state["planned_window_completed_at"],
            completed_at,
        )

        optimizer._stored_price_plan(deadline + timedelta(hours=10))
        self.assertIsNone(optimizer.state["planned_window_completed_at"])

    def test_incomplete_prices_and_comfort_floor_override_shifting(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        incomplete = self.intervals(now, ["0.40"] * 3)
        unavailable = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(target_temperature=45),
            intervals=incomplete,
            heating_minutes=90,
            lease_expires=None,
        )
        self.assertFalse(unavailable.data_complete)
        self.assertEqual(unavailable.desired_target, 50)
        self.assertTrue(unavailable.urgent)

        complete = self.intervals(now, ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8)
        recovery = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(tank_temperature=45, target_temperature=45),
            intervals=complete,
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(recovery.state, "recovery")
        self.assertEqual(recovery.desired_target, 50)
        self.assertTrue(recovery.urgent)

    def test_recovery_latch_holds_50_until_measured_recovery_completes(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        prices = self.intervals(now, ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8)
        recovering = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(tank_temperature=46, target_temperature=45),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            recovery_latched=True,
            recovery_reason="Synthetic latched recovery",
        )
        self.assertEqual(recovering.state, "recovery")
        self.assertEqual(recovering.desired_target, 50)
        self.assertEqual(recovering.reason, "Synthetic latched recovery")

        recovered = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(tank_temperature=50, target_temperature=50),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            recovery_latched=True,
        )
        self.assertNotEqual(recovered.state, "recovery")

    def test_missed_readiness_latches_recovery_for_the_next_cycle(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        now = datetime.fromisoformat("2026-10-15T04:00:00+00:00")
        observation = self.observation(tank_temperature=48)
        optimizer._update_learning(now, observation)
        self.assertEqual(
            optimizer.state["deadline_checks"]["2026-10-15T07:00:00+03:00"],
            self.module.READINESS_FAILURE,
        )
        self.assertTrue(optimizer.state["recovery_latched"])
        self.assertIn("07:00 readiness deadline was missed", optimizer.state["recovery_reason"])

        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.policy,
            observation=observation,
            intervals=self.intervals(now, ["0.20"] * 56),
            heating_minutes=90,
            lease_expires=None,
            recovery_latched=optimizer.state["recovery_latched"],
            recovery_reason=optimizer.state["recovery_reason"],
        )
        self.assertEqual(decision.state, "recovery")
        self.assertEqual(decision.desired_target, 50)

    def test_readiness_delivery_consumed_by_rapid_draw_is_not_a_failure(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_READINESS
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T03:50:00+00:00"),
            self.observation(tank_temperature=50, target_temperature=55),
        )
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T03:58:00+00:00"),
            self.observation(tank_temperature=46, target_temperature=50),
        )
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T04:00:00+00:00"),
            self.observation(tank_temperature=44, target_temperature=50),
        )
        self.assertEqual(
            optimizer.state["deadline_checks"]["2026-10-15T07:00:00+03:00"],
            self.module.READINESS_CONSUMED,
        )
        self.assertTrue(optimizer.state["recovery_latched"])
        self.assertIn("measured before a rapid draw", optimizer.state["recovery_reason"])

    def test_rapid_draw_without_measured_readiness_is_excluded(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_READINESS
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T03:50:00+00:00"),
            self.observation(tank_temperature=49, target_temperature=55),
        )
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T03:58:00+00:00"),
            self.observation(tank_temperature=45, target_temperature=55),
        )
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T04:00:00+00:00"),
            self.observation(tank_temperature=27, target_temperature=55),
        )
        self.assertEqual(
            optimizer.state["deadline_checks"]["2026-10-15T07:00:00+03:00"],
            self.module.READINESS_CONTAMINATED,
        )
        self.assertTrue(optimizer.state["recovery_latched"])
        self.assertIn("rapid draw obscured", optimizer.state["recovery_reason"])

    def test_override_at_deadline_records_unavailable_check(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T04:00:00+00:00"),
            self.observation(
                tank_temperature=49,
                override_reason="Extreme price protection has priority.",
            ),
        )
        self.assertEqual(
            optimizer.state["deadline_checks"]["2026-10-15T07:00:00+03:00"],
            self.module.READINESS_UNAVAILABLE,
        )
        self.assertFalse(optimizer.state["recovery_latched"])

    def test_cost_driven_target_changes_observe_minimum_dwell(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        raw = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(),
            intervals=self.intervals(
                now, ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8
            ),
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(raw.state, "coast")
        held = self.module.stabilize_decision(
            now=now,
            decision=raw,
            last_target=50,
            last_transition=now - timedelta(minutes=15),
        )
        self.assertEqual(held.state, "hold")
        self.assertEqual(held.desired_target, 50)

        released = self.module.stabilize_decision(
            now=now,
            decision=raw,
            last_target=50,
            last_transition=now - timedelta(minutes=30),
        )
        self.assertEqual(released.state, "coast")
        self.assertEqual(released.desired_target, 45)

    def test_soft_standby_requires_warm_tank_and_expires_to_normal(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        prices = self.intervals(now, ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8)
        start = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SOFT,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(tank_temperature=52),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(start.state, "soft_standby")
        self.assertEqual(start.desired_target, 30)

        expired = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SOFT,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(tank_temperature=48, target_temperature=30),
            intervals=prices,
            heating_minutes=60,
            lease_expires=now - timedelta(minutes=1),
        )
        self.assertEqual(expired.state, "normal")
        self.assertEqual(expired.desired_target, 50)
        self.assertTrue(expired.urgent)

    def test_next_readiness_uses_tallinn_dst_offset(self):
        before_transition = datetime.fromisoformat("2026-10-25T03:30:00+00:00")
        deadline = self.module.next_readiness(before_transition, self.policy)
        self.assertEqual(deadline.isoformat(), "2026-10-25T07:00:00+02:00")

    async def test_controller_starts_in_shadow_and_never_writes(self):
        await self.refresh()
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        await optimizer.async_start()
        try:
            self.assertEqual(optimizer.mode, self.const.DHW_MODE_SHADOW)
            self.assertTrue(optimizer.attributes["shadow_zero_writes"])
            self.devices.control_daikin.assert_not_awaited()
            self.assertTrue(optimizer.attributes["recent_decisions"])
            self.assertEqual(optimizer.attributes["pilot_days"], 0)
            self.assertEqual(optimizer.attributes["readiness_checks"], 0)
            self.assertEqual(optimizer.attributes["readiness_successes"], 0)
            self.assertEqual(optimizer.attributes["readiness_failures"], 0)
            self.assertEqual(optimizer.attributes["recent_recovery_events"], 0)
            self.assertEqual(optimizer.attributes["recent_override_events"], 0)
        finally:
            await optimizer.async_stop()

    def test_price_opportunity_records_one_plan_without_claiming_savings(self):
        sensor_module = importlib.import_module(base.COMPONENT + ".sensor")
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.module.DhwPolicy.from_options(
                {"dhw_initial_heat_minutes": 60}
            ),
            observation=self.observation(),
            intervals=self.intervals(
                now, ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8
            ),
            heating_minutes=60,
            lease_expires=None,
        )
        optimizer._record_optimization_opportunity(now, decision)
        optimizer._record_optimization_opportunity(
            now + timedelta(minutes=15), decision
        )
        optimizer.decision = decision

        sensor = sensor_module.DhwOptimizationOpportunitySensor(
            optimizer, self.pricing_entry.entry_id
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

    async def test_confirmed_target_write_sets_ownership_and_lease(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_SOFT
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        observation = self.observation(tank_temperature=52)
        decision = self.module.DhwDecision(
            mode=self.const.DHW_MODE_SOFT,
            state="soft_standby",
            reason="Synthetic eligible lease",
            desired_target=30,
            actual_target=50,
            tank_temperature=52,
            deadline=now + timedelta(hours=4),
            selected_window=None,
            current_price=Decimal("0.40"),
            price_delta=Decimal("0.20"),
            price_delta_percent=Decimal("1"),
            price_gate=True,
            data_complete=True,
            lease_expires=None,
            allow_write=True,
            urgent=False,
        )
        await optimizer._apply_decision(now, observation, decision)
        self.assertEqual(
            self.writes,
            [
                {
                    "operation": "hot_water_target",
                    "value": 30,
                    "expected_previous": 50.0,
                }
            ],
        )
        self.assertEqual(optimizer.state["last_owned_target"], 30)
        self.assertEqual(
            datetime.fromisoformat(optimizer.state["lease_expires"]),
            now + timedelta(hours=4),
        )

    async def test_readiness_write_tracks_separate_pilot_evidence(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_READINESS
        now = datetime.fromisoformat("2026-10-15T03:45:00+00:00")
        decision = self.module.DhwDecision(
            mode=self.const.DHW_MODE_READINESS,
            state="readiness_charge",
            reason="Synthetic readiness boost",
            desired_target=55,
            actual_target=50,
            tank_temperature=49,
            deadline=datetime.fromisoformat("2026-10-15T07:00:00+03:00"),
            selected_window=None,
            current_price=Decimal("0.20"),
            price_delta=None,
            price_delta_percent=None,
            price_gate=False,
            data_complete=True,
            lease_expires=None,
            allow_write=True,
            urgent=True,
        )
        await optimizer._apply_decision(
            now,
            self.observation(tank_temperature=49),
            decision,
        )
        self.assertEqual(optimizer.state["readiness_pilot_confirmed_writes"], 1)
        self.assertEqual(optimizer.state["confirmed_writes"], 1)
        self.assertIsNotNone(optimizer.state["readiness_pilot_started_at"])
        self.assertIsNone(optimizer.state["pilot_started_at"])

    async def test_write_failure_latches_fault_without_retry(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_SAFE
        self.devices.control_daikin.side_effect = HomeAssistantError("Synthetic lost acknowledgement")
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        decision = self.module.DhwDecision(
            mode=self.const.DHW_MODE_SAFE,
            state="coast",
            reason="Synthetic transition",
            desired_target=45,
            actual_target=50,
            tank_temperature=51,
            deadline=now + timedelta(hours=4),
            selected_window=None,
            current_price=Decimal("0.40"),
            price_delta=Decimal("0.20"),
            price_delta_percent=Decimal("1"),
            price_gate=True,
            data_complete=True,
            lease_expires=None,
            allow_write=True,
            urgent=False,
        )
        with patch.object(self.module.persistent_notification, "async_create"):
            await optimizer._apply_decision(now, self.observation(), decision)
        self.assertIn("may have applied", optimizer.state["fault"])
        self.devices.control_daikin.assert_awaited_once()

    async def test_extreme_price_arbitration_blocks_decision_and_write(self):
        await self.refresh()
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_SAFE
        optimizer.set_extreme_protection(
            SimpleNamespace(
                ordinary_control_block_reason=lambda _now: (
                    "Extreme price protection has priority."
                )
            )
        )
        await optimizer.async_evaluate(
            datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        )
        self.assertEqual(optimizer.decision.state, "override")
        self.assertIn("priority", optimizer.decision.reason)
        self.devices.control_daikin.assert_not_awaited()

    def test_revision_five_resets_changed_policy_evidence(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.state.update(
            {
                "policy_revision": 4,
                "shadow_days": ["2026-10-17"],
                "heating_durations": [60],
                "deadline_checks": {
                    "2026-10-17T21:00:00+03:00": self.module.READINESS_SUCCESS,
                },
                "decision_history": [{"state": "normal"}],
                "planned_window_start": "2026-10-17T10:00:00+00:00",
            }
        )
        optimizer._reset_policy_evidence()
        self.assertEqual(optimizer.state["policy_revision"], 5)
        self.assertEqual(optimizer.state["shadow_days"], [])
        self.assertEqual(optimizer.state["heating_durations"], [])
        self.assertEqual(optimizer.state["deadline_checks"], {})
        self.assertEqual(optimizer.state["decision_history"], [])
        self.assertIsNone(optimizer.state["planned_window_start"])

    async def test_revision_change_pauses_a_persisted_write_capable_mode(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.store.async_load = AsyncMock(
            return_value={
                "policy_revision": 4,
                "mode": self.const.DHW_MODE_SAFE,
                "last_owned_target": 45,
            }
        )
        optimizer.store.async_save = AsyncMock()
        optimizer.async_evaluate = AsyncMock()
        await optimizer.async_start()
        try:
            self.assertEqual(optimizer.mode, self.const.DHW_MODE_PAUSED)
            self.assertIn("policy changed", optimizer.state["fault"])
            self.assertEqual(optimizer.state["last_owned_target"], 45)
            optimizer.async_evaluate.assert_awaited_once()
        finally:
            await optimizer.async_stop()

    def test_override_period_does_not_count_readiness_or_heating_evidence(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-17T03:45:00+00:00"),
            self.observation(
                tank_temperature=44,
                override_reason="Extreme price protection has priority.",
            ),
        )
        self.assertEqual(optimizer.state["deadline_checks"], {})
        self.assertEqual(optimizer.state["heating_durations"], [])
        self.assertIsNone(optimizer.state["cycle_started_at"])

    def test_status_attributes_stay_below_recorder_limit(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        optimizer.decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=self.policy,
            observation=self.observation(),
            intervals=self.intervals(
                now,
                ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8,
            ),
            heating_minutes=60,
            lease_expires=None,
        )
        optimizer.state["decision_history"] = [
            {
                "observed_at": now.isoformat(),
                "state": "recovery",
                "reason": "Synthetic detailed decision " + ("x" * 500),
                "write_sent": False,
            }
            for _ in range(24)
        ]
        attributes = optimizer.attributes
        self.assertEqual(len(attributes["recent_decisions"]), 4)
        self.assertLess(len(json.dumps(attributes)), 16000)

    async def test_promotions_are_gated_by_observation_evidence(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        await optimizer.async_set_mode(self.const.DHW_MODE_READINESS)
        self.assertEqual(optimizer.mode, self.const.DHW_MODE_READINESS)
        with self.assertRaisesRegex(HomeAssistantError, "seven observed"):
            await optimizer.async_set_mode(self.const.DHW_MODE_SAFE)
        with self.assertRaisesRegex(HomeAssistantError, "seven-day safe pilot"):
            await optimizer.async_set_mode(self.const.DHW_MODE_SOFT)

    def test_safe_promotion_requires_delivered_readiness_pilot_evidence(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.state["shadow_days"] = [f"2026-10-{day:02d}" for day in range(1, 8)]
        optimizer.state["heating_durations"] = [60, 65, 70, 75, 80]
        optimizer.state["readiness_pilot_started_at"] = (
            "2026-10-01T00:00:00+00:00"
        )
        optimizer.state["readiness_pilot_confirmed_writes"] = 2
        optimizer.state["deadline_checks"] = {
            f"2026-10-{day:02d}T07:00:00+03:00": self.module.READINESS_SUCCESS
            for day in range(1, 11)
        }
        optimizer.state["deadline_checks"]["2026-10-11T07:00:00+03:00"] = (
            self.module.READINESS_CONTAMINATED
        )
        optimizer.state["deadline_checks"]["2026-10-12T07:00:00+03:00"] = (
            self.module.READINESS_UNAVAILABLE
        )
        self.assertTrue(optimizer.shadow_ready)
        optimizer.state["deadline_checks"]["2026-10-10T07:00:00+03:00"] = (
            self.module.READINESS_FAILURE
        )
        self.assertFalse(optimizer.shadow_ready)

    def test_unowned_or_unknown_device_state_is_never_overwritten(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        self.status["hot_water"]["operations"]["DomesticHotWaterTemperatureHeating"] = 45
        self.status["effective_hot_water_target"] = 45
        observation = optimizer._observation(self.status)
        self.assertIn("not optimizer-owned", observation.override_reason)

        self.status["hot_water"]["operations"]["DomesticHotWaterTemperatureHeating"] = 50
        self.status["effective_hot_water_target"] = None
        observation = optimizer._observation(self.status)
        self.assertIn("effective DHW target is unavailable", observation.override_reason)

        self.status["effective_hot_water_target"] = 50
        self.status["hot_water"]["operations"].pop("powerful")
        observation = optimizer._observation(self.status)
        self.assertIn("powerful", observation.override_reason)

    async def test_return_to_normal_pauses_before_restoring_owned_target(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_SAFE
        optimizer.state["last_owned_target"] = 45
        self.status["hot_water"]["operations"]["DomesticHotWaterTemperatureHeating"] = 45
        self.status["effective_hot_water_target"] = 45
        await optimizer.async_return_to_normal()
        self.assertEqual(optimizer.mode, self.const.DHW_MODE_PAUSED)
        self.assertEqual(
            self.writes,
            [
                {
                    "operation": "hot_water_target",
                    "value": 50,
                    "expected_previous": 45.0,
                }
            ],
        )
        self.assertIsNone(optimizer.state["last_owned_target"])

    async def test_options_validate_live_native_target_range(self):
        flow_module = importlib.import_module(base.COMPONENT + ".config_flow")
        self.pricing_entry = base.entry(
            base.DOMAIN,
            "pricing",
            {
                **self.pricing_entry.data,
                "daikin_config_entry": "daikin",
            },
        )
        self.entries["pricing"] = self.pricing_entry
        self.hass.config_entries.async_get_known_entry = self.entries.get
        runtime_devices = SimpleNamespace(
            daikin_api=lambda: (
                SimpleNamespace(entry_id="daikin"),
                SimpleNamespace(water_tank_target_temp_config={
                    "minValue": 30,
                    "maxValue": 55,
                    "stepValue": 1,
                    "settable": True,
                }),
            )
        )
        self.hass.data[base.DOMAIN] = {
            "pricing": {"devices": runtime_devices}
        }
        flow = flow_module.HouseholdPriceOptionsFlow()
        flow.hass = self.hass
        flow.handler = "pricing"
        self.hass.states.async_set(
            "sensor.room_temperature",
            "21.5",
            {"unit_of_measurement": "°C"},
        )
        valid = {
            "dhw_morning_ready": "07:00",
            "dhw_evening_ready": "21:00",
            "dhw_soft_target": 30,
            "dhw_floor_target": 45,
            "dhw_normal_target": 50,
            "dhw_preheat_target": 55,
            "dhw_max_lease_hours": 4,
            "dhw_price_delta_eur_kwh": 0.05,
            "dhw_price_delta_percent": 20,
            "dhw_initial_heat_minutes": 90,
            "dhw_coast_horizon_hours": 8,
            "floor_indoor_temperature_entities": ["sensor.room_temperature"],
        }
        flow._validate(valid)
        with self.assertRaisesRegex(HomeAssistantError, "native range"):
            flow._validate({**valid, "dhw_preheat_target": 60})

    async def test_daikin_entry_starts_shadow_and_forwards_optimizer_entities(self):
        integration = importlib.import_module(base.COMPONENT)
        callbacks = []
        old_coordinator = self.coordinator
        self.pricing_entry = base.entry(
            base.DOMAIN,
            "pricing",
            {
                **self.pricing_entry.data,
                "daikin_config_entry": "daikin",
            },
            ConfigEntryState.SETUP_IN_PROGRESS,
        )
        self.entries["pricing"] = self.pricing_entry
        self.entries["daikin"] = base.entry("daikin_altherma", "daikin", {})
        self.devices.solar_entry = None
        self.devices.daikin_entry = "daikin"
        try:
            with (
                patch.object(integration, "EnergyDevices", return_value=self.devices),
                patch.object(
                    ConfigEntry,
                    "async_on_unload",
                    side_effect=lambda callback: callbacks.append(callback),
                ),
            ):
                self.assertTrue(
                    await integration.async_setup_entry(self.hass, self.pricing_entry)
                )
            self.coordinator = self.pricing_entry.runtime_data
            self.forward.assert_awaited_once_with(
                self.pricing_entry, ["sensor", "select", "button"]
            )
            runtime = self.hass.data[base.DOMAIN]["pricing"]
            self.assertEqual(runtime["optimizer"].mode, self.const.DHW_MODE_SHADOW)
            self.assertIsNotNone(runtime["floor_shadow"])
            self.assertFalse(runtime["floor_shadow"].attributes["write_allowed"])
            self.assertIsNotNone(runtime["extreme_protection"])
            self.assertEqual(
                runtime["extreme_protection"].mode,
                self.const.EXTREME_PRICE_MODE_SHADOW,
            )
            self.assertFalse(
                runtime["extreme_protection"].attributes["write_allowed"]
            )
            self.devices.control_daikin.assert_not_awaited()
            self.assertTrue(
                await integration.async_unload_entry(self.hass, self.pricing_entry)
            )
        finally:
            for callback in reversed(callbacks):
                result = callback()
                if inspect.isawaitable(result):
                    await result
            await old_coordinator.async_shutdown()


if __name__ == "__main__":
    unittest.main()
