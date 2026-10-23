"""Deterministic DHW policy and controller tests using real HA APIs."""

from __future__ import annotations

from dataclasses import replace
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

    def test_readiness_pilot_prefers_50_without_final_55_topup(self):
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
        self.assertEqual(hold.state, "readiness_preferred")
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
        self.assertEqual(charge.desired_target, 50)
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
        self.assertEqual(ready.state, "readiness_preferred")
        self.assertEqual(ready.desired_target, 50)
        self.assertTrue(ready.urgent)

    def test_post_readiness_prefers_50_but_accepts_expensive_45_to_49(self):
        deadline = datetime.fromisoformat("2026-10-15T04:00:00+00:00")
        selected = self.module.PriceWindowChoice(
            start=deadline - timedelta(minutes=90),
            end=deadline - timedelta(minutes=30),
            average=Decimal("0.10"),
            interval_count=4,
        )
        price_start = deadline - timedelta(minutes=90)
        expensive = self.intervals(
            price_start,
            ["0.10"] * 6 + ["0.30"] * 8,
        )

        target_45 = self.module.plan_dhw(
            now=deadline,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(
                tank_temperature=45,
                target_temperature=45,
            ),
            intervals=expensive,
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            comparison_window=selected,
            absolute_cheapest_window=selected,
            planned_deadline=deadline,
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertEqual(target_45.state, "readiness_usable")
        self.assertEqual(target_45.desired_target, 45)
        self.assertTrue(target_45.price_gate)

        target_50 = self.module.plan_dhw(
            now=deadline,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(
                tank_temperature=49,
                target_temperature=50,
            ),
            intervals=expensive,
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            comparison_window=selected,
            absolute_cheapest_window=selected,
            planned_deadline=deadline,
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertEqual(target_50.state, "readiness_usable")
        self.assertEqual(target_50.desired_target, 50)

        reasonable = self.module.plan_dhw(
            now=deadline,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(
                tank_temperature=48,
                target_temperature=45,
            ),
            intervals=self.intervals(
                price_start,
                ["0.10"] * 6 + ["0.13"] * 8,
            ),
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            comparison_window=selected,
            absolute_cheapest_window=selected,
            planned_deadline=deadline,
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertEqual(reasonable.state, "readiness_charge")
        self.assertEqual(reasonable.desired_target, 50)

        incomplete = self.module.plan_dhw(
            now=deadline,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(
                tank_temperature=48,
                target_temperature=45,
            ),
            intervals=(),
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            comparison_window=selected,
            absolute_cheapest_window=selected,
            planned_deadline=deadline,
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertFalse(incomplete.data_complete)
        self.assertEqual(incomplete.state, "readiness_charge")
        self.assertEqual(incomplete.desired_target, 50)

    def test_23_october_uses_absolute_cheapest_0515_block(self):
        start = datetime.fromisoformat("2026-10-23T02:00:00+00:00")
        values = [
            "0.30",
            "0.08277000",
            "0.08536160",
            "0.08750680",
            "0.15036240",
            "0.08717199",
            "0.08717199",
            "0.12593442",
            "0.30",
            "0.30",
            "0.30",
            "0.30",
            "0.30",
            "0.30",
        ]
        prices = self.intervals(start, values)
        initial = self.module.plan_dhw(
            now=start,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=90,
            shallow_heating_minutes=60,
            lease_expires=None,
            retention_loss_rate_c_per_hour=2.0,
            retention_sample_count=9,
            retention_cycle_count=3,
        )
        self.assertEqual(
            initial.selected_window.start,
            datetime.fromisoformat("2026-10-23T02:15:00+00:00"),
        )
        self.assertEqual(
            initial.selected_window.end,
            datetime.fromisoformat("2026-10-23T03:45:00+00:00"),
        )
        self.assertEqual(
            initial.selected_window.average.quantize(Decimal("0.000001")),
            Decimal("0.096724"),
        )
        later_average = sum(
            (Decimal(value) for value in values[2:8]),
            Decimal(0),
        ) / 6
        self.assertEqual(
            later_average.quantize(Decimal("0.000001")),
            Decimal("0.103918"),
        )

        at_start = self.module.plan_dhw(
            now=start + timedelta(minutes=15),
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=90,
            shallow_heating_minutes=60,
            lease_expires=None,
            planned_window=initial.selected_window,
            comparison_window=initial.comparison_window,
            absolute_cheapest_window=initial.absolute_cheapest_window,
            planned_deadline=initial.deadline,
            planned_kind=initial.plan_kind,
            planned_endpoint=initial.protected_endpoint,
            retention_loss_rate_c_per_hour=2.0,
            retention_sample_count=9,
            retention_cycle_count=3,
        )
        self.assertEqual(at_start.state, "economic_storage")
        self.assertEqual(at_start.desired_target, 55)
        self.assertNotEqual(
            at_start.selected_window.start,
            datetime.fromisoformat("2026-10-23T03:00:00+00:00"),
        )

    def test_learned_shallow_allowance_sizes_preferred_readiness_block(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.state["readiness_topup_durations"] = [23.4, 22.4]
        self.assertEqual(optimizer.shallow_heating_minutes, 45)
        prices = self.intervals(
            datetime.fromisoformat("2026-10-21T00:00:00+00:00"),
            ["0.20"] * 40,
        )

        premature = self.module.plan_dhw(
            now=datetime.fromisoformat("2026-10-21T02:15:00+00:00"),
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=105,
            shallow_heating_minutes=optimizer.shallow_heating_minutes,
            lease_expires=None,
        )
        self.assertNotEqual(premature.state, "readiness_charge")
        self.assertEqual(premature.readiness_heating_profile, "shallow")
        self.assertEqual(premature.readiness_heating_minutes, 45)

        final = self.module.plan_dhw(
            now=datetime.fromisoformat("2026-10-21T03:15:00+00:00"),
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=105,
            shallow_heating_minutes=optimizer.shallow_heating_minutes,
            lease_expires=None,
        )
        self.assertEqual(final.state, "readiness_charge")
        self.assertEqual(final.desired_target, 50)
        self.assertEqual(final.readiness_heating_minutes, 45)

    def test_retention_model_requires_three_samples_across_two_cycles(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.state["retention_samples"] = [
            {
                "loss_rate_c_per_hour": 1.0,
                "readiness_deadline": "2026-10-21T21:00:00+03:00",
            },
            {
                "loss_rate_c_per_hour": 1.5,
                "readiness_deadline": "2026-10-21T21:00:00+03:00",
            },
            {
                "loss_rate_c_per_hour": 2.0,
                "readiness_deadline": "2026-10-22T07:00:00+03:00",
            },
        ]
        qualified, rate, sample_count, cycle_count = optimizer.retention_model
        self.assertTrue(qualified)
        self.assertEqual(rate, 2.0)
        self.assertEqual(sample_count, 3)
        self.assertEqual(cycle_count, 2)

        optimizer.state["retention_samples"] = (
            optimizer.state["retention_samples"][:2]
        )
        self.assertFalse(optimizer.retention_model[0])

    def test_qualified_material_economics_allow_55_storage(self):
        start = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        prices = self.intervals(
            start,
            ["0.10"] * 4
            + ["0.40"] * 4
            + ["0.105"] * 4
            + ["0.30"] * 4,
        )
        decision = self.module.plan_dhw(
            now=start + timedelta(hours=2),
            mode=self.const.DHW_MODE_SAFE,
            policy=self.module.DhwPolicy.from_options(
                {"dhw_initial_heat_minutes": 60}
            ),
            observation=self.observation(),
            intervals=prices,
            heating_minutes=60,
            shallow_heating_minutes=45,
            lease_expires=None,
            retention_loss_rate_c_per_hour=1.0,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertEqual(
            decision.selected_window.start,
            start + timedelta(hours=2),
        )
        self.assertEqual(
            decision.absolute_cheapest_window.start,
            start + timedelta(hours=2),
        )
        self.assertEqual(
            decision.comparison_window.start,
            start + timedelta(hours=3),
        )
        self.assertTrue(decision.preheat_price_gate)
        self.assertEqual(decision.state, "economic_storage")
        self.assertEqual(decision.desired_target, 55)
        self.assertEqual(decision.plan_kind, self.module.PLAN_KIND_ECONOMIC)

        rapid_draw = self.module.plan_dhw(
            now=start + timedelta(hours=2),
            mode=self.const.DHW_MODE_SAFE,
            policy=self.module.DhwPolicy.from_options(
                {"dhw_initial_heat_minutes": 60}
            ),
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=60,
            shallow_heating_minutes=45,
            lease_expires=None,
            recent_rapid_draw=True,
            retention_loss_rate_c_per_hour=1.0,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertNotEqual(rapid_draw.desired_target, 55)
        self.assertIsNone(rapid_draw.adaptive_preheat_target_c)

    def test_55_may_protect_an_immediate_expensive_period_endpoint(self):
        now = datetime.fromisoformat("2026-10-15T08:00:00+00:00")
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.module.DhwPolicy.from_options(
                {
                    "dhw_initial_heat_minutes": 60,
                    "dhw_coast_horizon_hours": 8,
                }
            ),
            observation=self.observation(),
            intervals=self.intervals(
                now,
                ["0.05"] * 4
                + ["0.20"] * 4
                + ["0.10"] * 36,
            ),
            heating_minutes=60,
            lease_expires=None,
            retention_loss_rate_c_per_hour=3.0,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertEqual(decision.state, "economic_storage")
        self.assertEqual(decision.desired_target, 55)
        self.assertEqual(
            decision.protected_endpoint,
            now + timedelta(hours=2),
        )
        readiness_hold_hours = (
            decision.deadline.astimezone(
                decision.selected_window.end.tzinfo
            )
            - decision.selected_window.end
        ).total_seconds() / 3600
        self.assertLess(
            55 - 3 * readiness_hold_hours,
            45,
        )

    def test_unqualified_or_excessive_retention_suppresses_discretionary_heat(self):
        start = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        prices = self.intervals(
            start,
            ["0.10"] * 4 + ["0.40"] * 12,
        )
        unqualified = self.module.plan_dhw(
            now=start,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.module.DhwPolicy.from_options(
                {"dhw_initial_heat_minutes": 60}
            ),
            observation=self.observation(),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(unqualified.state, "readiness_preferred")
        self.assertEqual(unqualified.desired_target, 50)
        self.assertNotEqual(unqualified.desired_target, 55)

        excessive = self.module.plan_dhw(
            now=start,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.module.DhwPolicy.from_options(
                {"dhw_initial_heat_minutes": 60}
            ),
            observation=self.observation(),
            intervals=self.intervals(
                start,
                ["0.10"] * 4
                + ["0.14"] * 4
                + ["0.16"] * 8,
            ),
            heating_minutes=60,
            shallow_heating_minutes=45,
            lease_expires=None,
            retention_loss_rate_c_per_hour=3.0,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertEqual(excessive.state, "readiness_preferred")
        self.assertEqual(excessive.desired_target, 50)
        self.assertIsNone(excessive.adaptive_preheat_target_c)
        self.assertEqual(
            excessive.plan_kind,
            self.module.PLAN_KIND_PREFERRED,
        )
        self.assertGreaterEqual(
            excessive.selected_window.start,
            start + timedelta(hours=2, minutes=30),
        )

    def test_economic_storage_horizon_is_capped_at_eight_hours(self):
        now = datetime.fromisoformat("2026-10-15T08:00:00+00:00")
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.module.DhwPolicy.from_options(
                {
                    "dhw_initial_heat_minutes": 60,
                    "dhw_coast_horizon_hours": 12,
                }
            ),
            observation=self.observation(),
            intervals=self.intervals(
                now,
                ["0.05"] * 4
                + ["0.06"] * 4
                + ["0.07"] * 4
                + ["0.08"] * 4
                + ["0.09"] * 4
                + ["0.10"] * 24,
            ),
            heating_minutes=60,
            lease_expires=None,
            retention_loss_rate_c_per_hour=0.0,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertEqual(
            decision.plan_kind,
            self.module.PLAN_KIND_PREFERRED,
        )
        self.assertNotEqual(decision.desired_target, 55)
        self.assertGreaterEqual(
            decision.selected_window.start,
            datetime.fromisoformat("2026-10-15T10:00:00+00:00"),
        )

    def test_anchored_preferred_charge_and_recovery_latch_are_separate(self):
        coast_start = datetime.fromisoformat("2026-10-22T13:45:00+00:00")
        policy = self.module.DhwPolicy.from_options(
            {"dhw_initial_heat_minutes": 60}
        )
        prices = self.intervals(
            coast_start,
            ["0.30"] * 2 + ["0.10"] * 8 + ["0.30"] * 14,
        )
        selected = self.module.PriceWindowChoice(
            start=coast_start + timedelta(minutes=30),
            end=coast_start + timedelta(minutes=90),
            average=Decimal("0.10"),
            interval_count=4,
        )

        coast = self.module.plan_dhw(
            now=coast_start,
            mode=self.const.DHW_MODE_SAFE,
            policy=policy,
            observation=self.observation(tank_temperature=49),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            planned_deadline=self.module.next_readiness(
                coast_start,
                policy,
            ),
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertEqual(coast.state, "coast")
        self.assertEqual(coast.desired_target, 45)

        short_window = self.module.PriceWindowChoice(
            start=coast_start + timedelta(minutes=30),
            end=coast_start + timedelta(minutes=45),
            average=Decimal("0.10"),
            interval_count=1,
        )
        skipped = self.module.plan_dhw(
            now=coast_start + timedelta(minutes=30),
            mode=self.const.DHW_MODE_SAFE,
            policy=policy,
            observation=self.observation(
                tank_temperature=48,
                target_temperature=45,
            ),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=short_window,
            planned_deadline=self.module.next_readiness(
                coast_start,
                policy,
            ),
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertEqual(skipped.state, "readiness_charge")
        self.assertEqual(skipped.desired_target, 50)

        restore = self.module.plan_dhw(
            now=coast_start + timedelta(minutes=30),
            mode=self.const.DHW_MODE_SAFE,
            policy=policy,
            observation=self.observation(
                tank_temperature=48,
                target_temperature=45,
            ),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            planned_deadline=self.module.next_readiness(
                coast_start,
                policy,
            ),
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertEqual(restore.state, "readiness_charge")
        self.assertEqual(restore.desired_target, 50)
        self.assertTrue(restore.urgent)

        moving_window = self.module.PriceWindowChoice(
            start=coast_start + timedelta(minutes=90),
            end=coast_start + timedelta(minutes=150),
            average=Decimal("0.10"),
            interval_count=4,
        )
        held = self.module.plan_dhw(
            now=coast_start + timedelta(minutes=60),
            mode=self.const.DHW_MODE_SAFE,
            policy=policy,
            observation=self.observation(
                tank_temperature=49,
                target_temperature=50,
            ),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=moving_window,
            planned_deadline=self.module.next_readiness(
                coast_start,
                policy,
            ),
            planned_kind=self.module.PLAN_KIND_PREFERRED,
            recovery_latched=True,
            recovery_reason=restore.reason,
        )
        self.assertEqual(held.state, "recovery")
        self.assertEqual(held.desired_target, 50)
        self.assertNotEqual(held.desired_target, 45)

        recovered = self.module.plan_dhw(
            now=coast_start + timedelta(minutes=90),
            mode=self.const.DHW_MODE_SAFE,
            policy=policy,
            observation=self.observation(
                tank_temperature=50,
                target_temperature=50,
            ),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=moving_window,
            planned_deadline=self.module.next_readiness(
                coast_start,
                policy,
            ),
            planned_kind=self.module.PLAN_KIND_PREFERRED,
            recovery_latched=True,
            recovery_reason=restore.reason,
        )
        self.assertNotEqual(recovered.state, "recovery")
        self.assertEqual(recovered.desired_target, 50)

    def test_shadow_proposes_preferred_readiness_without_writes(self):
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
        self.assertEqual(decision.desired_target, 50)
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
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
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
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertEqual(in_window.state, "economic_storage")
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
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
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
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertFalse(decision.preheat_price_gate)
        self.assertEqual(decision.state, "readiness_preferred")
        self.assertEqual(decision.desired_target, 50)
        self.assertNotEqual(decision.desired_target, 55)
        self.assertEqual(
            decision.selected_window.start,
            now + timedelta(hours=3),
        )

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
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
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
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
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
            absolute_cheapest_window=initial.absolute_cheapest_window,
            planned_deadline=initial.deadline,
            planned_kind=initial.plan_kind,
            planned_endpoint=initial.protected_endpoint,
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertEqual(anchored.selected_window, initial.selected_window)
        self.assertEqual(anchored.state, "economic_storage")

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
            absolute_cheapest_window=initial.absolute_cheapest_window,
            planned_deadline=initial.deadline,
            planned_kind=initial.plan_kind,
            planned_endpoint=initial.protected_endpoint,
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertNotEqual(after_window.state, "economic_storage")
        self.assertEqual(after_window.selected_window, initial.selected_window)

    def test_usable_temperature_does_not_become_safety_recovery(self):
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
            planned_deadline=self.module.next_readiness(now, policy),
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertEqual(incomplete.state, "readiness_usable")
        self.assertEqual(incomplete.desired_target, 50)

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
            planned_deadline=self.module.next_readiness(now, policy),
            planned_kind=self.module.PLAN_KIND_PREFERRED,
            preheat_completed=True,
        )
        self.assertEqual(completed.state, "readiness_usable")
        self.assertEqual(completed.desired_target, 50)

        missed_floor = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SHADOW,
            policy=policy,
            observation=self.observation(
                tank_temperature=44,
                target_temperature=45,
            ),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            comparison_window=comparison,
            planned_deadline=self.module.next_readiness(now, policy),
            planned_kind=self.module.PLAN_KIND_PREFERRED,
        )
        self.assertEqual(missed_floor.state, "recovery")
        self.assertEqual(missed_floor.desired_target, 50)

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
        optimizer._store_price_plan(
            deadline,
            selected,
            selected,
            selected,
        )
        optimizer._update_preheat_completion(
            selected.start + timedelta(minutes=30),
            self.observation(tank_temperature=50),
            selected,
            50,
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
        self.assertNotEqual(unavailable.desired_target, 55)
        self.assertTrue(unavailable.urgent)

        complete = self.intervals(now, ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8)
        recovery = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(tank_temperature=44.9, target_temperature=45),
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

    def test_missed_preferred_readiness_above_floor_does_not_latch_recovery(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        now = datetime.fromisoformat("2026-10-15T04:00:00+00:00")
        observation = self.observation(tank_temperature=48)
        optimizer._update_learning(now, observation)
        self.assertEqual(optimizer.state["deadline_checks"], {})
        optimizer._update_learning(now + timedelta(minutes=15), observation)
        self.assertEqual(
            optimizer.state["deadline_checks"]["2026-10-15T07:00:00+03:00"],
            self.module.READINESS_FAILURE,
        )
        self.assertFalse(optimizer.state["recovery_latched"])
        self.assertIsNone(optimizer.state["recovery_reason"])

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
        self.assertEqual(decision.state, "readiness_charge")
        self.assertEqual(decision.desired_target, 50)

    def test_expensive_usable_readiness_is_recorded_truthfully(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        deadline = datetime.fromisoformat("2026-10-15T04:00:00+00:00")
        optimizer.decision = self.module.DhwDecision(
            mode=self.const.DHW_MODE_SAFE,
            state="readiness_usable",
            reason="Synthetic materially expensive readiness.",
            desired_target=45,
            actual_target=45,
            tank_temperature=48,
            deadline=deadline,
            selected_window=None,
            current_price=Decimal("0.30"),
            price_delta=Decimal("0.20"),
            price_delta_percent=Decimal("2"),
            price_gate=True,
            data_complete=True,
            lease_expires=None,
            allow_write=True,
            urgent=False,
        )
        observation = self.observation(
            tank_temperature=48,
            target_temperature=45,
        )
        optimizer._update_learning(deadline, observation)
        optimizer._update_learning(
            deadline + timedelta(minutes=15),
            observation,
        )
        self.assertEqual(
            optimizer.state["deadline_checks"][
                "2026-10-15T07:00:00+03:00"
            ],
            self.module.READINESS_ACCEPTED,
        )
        self.assertFalse(optimizer.state["recovery_latched"])

    def test_deadline_sample_uses_current_usable_decision(self):
        optimizer = self.module.DhwOptimizer(
            self.hass,
            self.pricing_entry,
            self.coordinator,
            self.devices,
        )
        deadline = datetime.fromisoformat("2026-10-15T04:00:00+00:00")
        observation = self.observation(
            tank_temperature=48,
            target_temperature=50,
        )
        optimizer.decision = self.module.DhwDecision(
            mode=self.const.DHW_MODE_SAFE,
            state="readiness_charge",
            reason="Previous-minute preferred charge.",
            desired_target=50,
            actual_target=50,
            tank_temperature=48,
            deadline=deadline,
            selected_window=None,
            current_price=Decimal("0.10"),
            price_delta=None,
            price_delta_percent=None,
            price_gate=False,
            data_complete=True,
            lease_expires=None,
            allow_write=True,
            urgent=False,
        )
        optimizer._update_learning(deadline, observation)
        current = replace(
            optimizer.decision,
            state="readiness_usable",
            reason="Current expensive usable outcome.",
            price_gate=True,
        )
        optimizer._update_deadline_sample_decision(
            deadline,
            current,
        )
        optimizer._update_learning(
            deadline + timedelta(minutes=15),
            observation,
        )

        self.assertEqual(
            optimizer.state["deadline_checks"][
                "2026-10-15T07:00:00+03:00"
            ],
            self.module.READINESS_ACCEPTED,
        )

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
        self.assertEqual(optimizer.state["deadline_checks"], {})
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T04:15:00+00:00"),
            self.observation(tank_temperature=44, target_temperature=50),
        )
        self.assertEqual(
            optimizer.state["deadline_checks"]["2026-10-15T07:00:00+03:00"],
            self.module.READINESS_CONSUMED,
        )
        self.assertTrue(optimizer.state["recovery_latched"])
        self.assertIn("delivered before a rapid draw", optimizer.state["recovery_reason"])

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
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T04:15:00+00:00"),
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
        optimizer._update_learning(
            datetime.fromisoformat("2026-10-15T04:15:00+00:00"),
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
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
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

    def test_legacy_soft_mode_maps_to_safe_target_shift(self):
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        prices = self.intervals(now, ["0.40"] * 4 + ["0.10"] * 4 + ["0.30"] * 8)
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SOFT,
            policy=self.module.DhwPolicy.from_options({"dhw_initial_heat_minutes": 60}),
            observation=self.observation(tank_temperature=52),
            intervals=prices,
            heating_minutes=60,
            lease_expires=None,
        )
        self.assertEqual(decision.mode, self.const.DHW_MODE_SAFE)
        self.assertEqual(decision.state, "coast")
        self.assertEqual(decision.desired_target, 45)

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

    async def test_revision_ten_restart_preserves_anchor_without_startup_write(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        now = datetime.fromisoformat("2026-10-24T00:00:00+00:00")
        deadline = datetime.fromisoformat("2026-10-24T04:00:00+00:00")
        selected = self.module.PriceWindowChoice(
            start=now + timedelta(hours=1),
            end=now + timedelta(hours=2),
            average=Decimal("0.10"),
            interval_count=4,
        )
        comparison = self.module.PriceWindowChoice(
            start=now + timedelta(hours=2),
            end=now + timedelta(hours=3),
            average=Decimal("0.40"),
            interval_count=4,
        )
        optimizer.store.async_load = AsyncMock(
            return_value={
                "policy_revision": 10,
                "mode": self.const.DHW_MODE_SAFE,
                "last_owned_target": 50,
                "planned_deadline": deadline.isoformat(),
                "planned_kind": self.module.PLAN_KIND_PREFERRED,
                "planned_target": 50,
                "planned_endpoint": deadline.isoformat(),
                "planned_window_start": selected.start.isoformat(),
                "planned_window_end": selected.end.isoformat(),
                "planned_window_average": "0.10",
                "planned_window_intervals": 4,
                "planned_comparison_start": comparison.start.isoformat(),
                "planned_comparison_end": comparison.end.isoformat(),
                "planned_comparison_average": "0.40",
                "planned_comparison_intervals": 4,
                "planned_cheapest_start": selected.start.isoformat(),
                "planned_cheapest_end": selected.end.isoformat(),
                "planned_cheapest_average": "0.10",
                "planned_cheapest_intervals": 4,
            }
        )
        optimizer.store.async_save = AsyncMock()
        prices = self.intervals(
            now,
            ["0.30"] * 4
            + ["0.10"] * 4
            + ["0.40"] * 4
            + ["0.30"] * 8,
        )
        with (
            patch.object(self.module.dt_util, "utcnow", return_value=now),
            patch.object(
                self.module,
                "published_intervals",
                return_value=prices,
            ),
        ):
            await optimizer.async_start()
        try:
            self.assertEqual(optimizer.decision.selected_window, selected)
            self.assertEqual(
                optimizer.state["planned_window_start"],
                selected.start.isoformat(),
            )
            self.assertIsNone(optimizer.state["last_policy_target"])
            self.devices.control_daikin.assert_not_awaited()
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
            retention_loss_rate_c_per_hour=0.5,
            retention_sample_count=3,
            retention_cycle_count=2,
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
        self.assertEqual(sensor.native_value, Decimal("0.20"))
        self.assertEqual(sensor.native_unit_of_measurement, "EUR/kWh")
        attributes = sensor.extra_state_attributes
        self.assertEqual(attributes["valuation_status"], "counterfactual")
        self.assertEqual(attributes["qualified_opportunities_recorded"], 1)
        self.assertIsNone(attributes["measured_shifted_energy_kwh"])
        self.assertIsNone(attributes["estimated_savings_eur"])
        self.assertIsNone(attributes["realized_savings_eur"])
        self.assertLess(len(json.dumps(attributes)), 2048)

    async def test_confirmed_target_write_sets_ownership_without_lease(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_SAFE
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        observation = self.observation(tank_temperature=52)
        decision = self.module.DhwDecision(
            mode=self.const.DHW_MODE_SAFE,
            state="coast",
            reason="Synthetic safe price shift",
            desired_target=45,
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
                    "value": 45,
                    "expected_previous": 50.0,
                }
            ],
        )
        self.assertEqual(optimizer.state["last_owned_target"], 45)
        self.assertIsNone(optimizer.state["lease_expires"])

    async def test_readiness_write_tracks_separate_pilot_evidence(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_READINESS
        now = datetime.fromisoformat("2026-10-15T03:45:00+00:00")
        decision = self.module.DhwDecision(
            mode=self.const.DHW_MODE_READINESS,
            state="readiness_charge",
            reason="Synthetic preferred readiness charge",
            desired_target=50,
            actual_target=45,
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
            self.observation(
                tank_temperature=49,
                target_temperature=45,
                effective_target=45,
            ),
            decision,
        )
        self.assertEqual(optimizer.state["readiness_pilot_confirmed_writes"], 1)
        self.assertEqual(optimizer.state["confirmed_writes"], 1)
        self.assertIsNotNone(optimizer.state["readiness_pilot_started_at"])
        self.assertIsNone(optimizer.state["pilot_started_at"])

    async def test_economic_storage_block_end_restores_exact_owned_55(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_SAFE
        optimizer.state["last_owned_target"] = 55
        start = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        now = start + timedelta(hours=1)
        deadline = datetime.fromisoformat("2026-10-15T04:00:00+00:00")
        selected = self.module.PriceWindowChoice(
            start=start,
            end=now,
            average=Decimal("0.10"),
            interval_count=4,
        )
        comparison = self.module.PriceWindowChoice(
            start=now,
            end=now + timedelta(hours=1),
            average=Decimal("0.40"),
            interval_count=4,
        )
        decision = self.module.plan_dhw(
            now=now,
            mode=self.const.DHW_MODE_SAFE,
            policy=self.policy,
            observation=self.observation(
                tank_temperature=55,
                target_temperature=55,
            ),
            intervals=self.intervals(
                start,
                ["0.10"] * 4 + ["0.40"] * 16,
            ),
            heating_minutes=60,
            lease_expires=None,
            planned_window=selected,
            comparison_window=comparison,
            absolute_cheapest_window=selected,
            planned_deadline=deadline,
            planned_kind=self.module.PLAN_KIND_ECONOMIC,
            planned_endpoint=comparison.end,
            retention_loss_rate_c_per_hour=1.0,
            retention_sample_count=3,
            retention_cycle_count=2,
        )
        self.assertEqual(decision.state, "readiness_preferred")
        self.assertEqual(decision.desired_target, 50)
        self.assertTrue(decision.urgent)
        await optimizer._apply_decision(
            now,
            self.observation(
                tank_temperature=55,
                target_temperature=55,
            ),
            decision,
        )
        self.assertEqual(
            self.writes,
            [
                {
                    "operation": "hot_water_target",
                    "value": 50,
                    "expected_previous": 55,
                }
            ],
        )
        self.assertEqual(optimizer.state["last_owned_target"], 50)

    async def test_revision_nine_owned_55_startup_is_command_free(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        self.status["hot_water"]["operations"][
            "DomesticHotWaterTemperatureHeating"
        ] = 55
        self.status["effective_hot_water_target"] = 55
        optimizer.store.async_load = AsyncMock(
            return_value={
                "policy_revision": 9,
                "mode": self.const.DHW_MODE_SAFE,
                "last_owned_target": 55,
                "readiness_final_topup_active": {
                    "2026-10-21T07:00:00+03:00": (
                        "2026-10-21T03:15:00+00:00"
                    ),
                },
            }
        )
        optimizer.store.async_save = AsyncMock()
        await optimizer.async_start()
        try:
            self.assertEqual(optimizer.mode, self.const.DHW_MODE_SHADOW)
            self.assertEqual(optimizer.state["policy_revision"], 10)
            self.assertEqual(optimizer.state["last_owned_target"], 55)
            self.assertIsNone(optimizer.state["last_policy_target"])
            self.assertNotIn(
                "readiness_final_topup_active",
                optimizer.state,
            )
            self.devices.control_daikin.assert_not_awaited()
            await optimizer.async_evaluate(
                datetime.fromisoformat("2026-10-23T18:00:00+00:00")
            )
            self.devices.control_daikin.assert_not_awaited()
            with patch.object(
                self.module.dt_util,
                "utcnow",
                return_value=datetime.fromisoformat(
                    "2026-10-23T18:00:00+00:00"
                ),
            ):
                await optimizer.async_activate_full_control()
            self.devices.control_daikin.assert_awaited_once()
            self.assertEqual(
                self.writes[-1],
                {
                    "operation": "hot_water_target",
                    "value": 50,
                    "expected_previous": 55.0,
                },
            )
        finally:
            await optimizer.async_stop()

    async def test_revision_nine_unowned_55_remains_untouched(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        self.status["hot_water"]["operations"][
            "DomesticHotWaterTemperatureHeating"
        ] = 55
        self.status["effective_hot_water_target"] = 55
        optimizer.store.async_load = AsyncMock(
            return_value={
                "policy_revision": 9,
                "mode": self.const.DHW_MODE_SAFE,
                "last_owned_target": None,
            }
        )
        optimizer.store.async_save = AsyncMock()
        await optimizer.async_start()
        try:
            self.assertEqual(optimizer.mode, self.const.DHW_MODE_SHADOW)
            self.assertEqual(optimizer.state["policy_revision"], 10)
            self.assertEqual(optimizer.decision.state, "override")
            self.devices.control_daikin.assert_not_awaited()
            await optimizer.async_evaluate(
                datetime.fromisoformat("2026-10-23T18:00:00+00:00")
            )
            self.assertEqual(optimizer.decision.state, "override")
            self.devices.control_daikin.assert_not_awaited()
            self.assertEqual(
                self.status["hot_water"]["operations"][
                    "DomesticHotWaterTemperatureHeating"
                ],
                55,
            )
        finally:
            await optimizer.async_stop()

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

    async def test_pre_dispatch_conflict_does_not_latch_uncertain_fault(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_SAFE
        self.devices.control_daikin.side_effect = (
            self.module.DaikinPreDispatchConflict(
                "Synthetic expected-previous conflict."
            )
        )
        now = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        deadline = now + timedelta(minutes=40)
        decision = self.module.DhwDecision(
            mode=self.const.DHW_MODE_SAFE,
            state="economic_storage",
            reason="Synthetic transition",
            desired_target=55,
            actual_target=50,
            tank_temperature=49,
            deadline=deadline,
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
        with patch.object(
            self.module.persistent_notification,
            "async_create",
        ) as notify:
            await optimizer._apply_decision(
                now,
                self.observation(),
                decision,
            )
            self.assertIsNone(optimizer.state["fault"])
            self.assertIsNone(optimizer.state["last_owned_target"])
            self.devices.control_daikin.assert_awaited_once()
        notify.assert_not_called()

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

    async def test_protected_below_floor_measurement_keeps_recovery_latched(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.mode = self.const.DHW_MODE_SAFE
        blocked = {"value": True}
        optimizer.set_extreme_protection(
            SimpleNamespace(
                ordinary_control_block_reason=lambda _now: (
                    "Extreme price protection has priority."
                    if blocked["value"]
                    else None
                )
            )
        )
        self.status["hot_water"]["sensors"]["TankTemperature"] = 44
        await optimizer.async_evaluate(
            datetime.fromisoformat("2026-10-15T00:00:00+00:00")
        )
        self.assertEqual(optimizer.decision.state, "override")
        self.assertTrue(optimizer.state["recovery_latched"])
        self.devices.control_daikin.assert_not_awaited()

        blocked["value"] = False
        self.status["hot_water"]["sensors"]["TankTemperature"] = 46
        await optimizer.async_evaluate(
            datetime.fromisoformat("2026-10-15T00:15:00+00:00")
        )
        self.assertEqual(optimizer.decision.state, "recovery")
        self.assertTrue(optimizer.state["recovery_latched"])

        self.status["hot_water"]["sensors"]["TankTemperature"] = 50
        await optimizer.async_evaluate(
            datetime.fromisoformat("2026-10-15T00:30:00+00:00")
        )
        self.assertFalse(optimizer.state["recovery_latched"])

    def test_revision_ten_resets_changed_policy_evidence(self):
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
        self.assertEqual(optimizer.state["policy_revision"], 10)
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

    async def test_revision_nine_migrates_to_ten_preserving_physical_evidence(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.store.async_load = AsyncMock(
            return_value={
                "policy_revision": 9,
                "mode": self.const.DHW_MODE_SAFE,
                "confirmed_writes": 14,
                "heating_durations": [45, 60],
                "readiness_topup_durations": [30, 45],
                "retention_samples": [
                    {
                        "loss_rate_c_per_hour": 1.25,
                        "readiness_deadline": "2026-10-22T07:00:00+03:00",
                    }
                ],
                "recovery_latched": True,
                "recovery_reason": "Measured recovery is incomplete.",
                "last_owned_target": 55,
                "fault": "Synthetic preserved fault.",
                "manual_override": "Synthetic preserved manual state.",
                "planned_window_start": "2026-10-22T14:15:00+00:00",
                "readiness_final_topup_active": {
                    "2026-10-22T21:00:00+03:00": (
                        "2026-10-22T17:15:00+00:00"
                    )
                },
            }
        )
        optimizer.store.async_save = AsyncMock()
        optimizer.async_evaluate = AsyncMock()
        await optimizer.async_start()
        try:
            self.assertEqual(optimizer.mode, self.const.DHW_MODE_SHADOW)
            self.assertEqual(optimizer.state["policy_revision"], 10)
            self.assertEqual(optimizer.state["confirmed_writes"], 14)
            self.assertEqual(
                optimizer.state["heating_durations"],
                [45, 60],
            )
            self.assertEqual(
                optimizer.state["readiness_topup_durations"],
                [30, 45],
            )
            self.assertEqual(len(optimizer.state["retention_samples"]), 1)
            self.assertTrue(optimizer.state["recovery_latched"])
            self.assertEqual(optimizer.state["last_owned_target"], 55)
            self.assertEqual(
                optimizer.state["fault"],
                "Synthetic preserved fault.",
            )
            self.assertEqual(
                optimizer.state["manual_override"],
                "Synthetic preserved manual state.",
            )
            self.assertIsNone(optimizer.state["planned_window_start"])
            self.assertNotIn(
                "readiness_final_topup_active",
                optimizer.state,
            )
            self.devices.control_daikin.assert_not_awaited()
        finally:
            await optimizer.async_stop()

    async def test_revision_six_migrates_to_ten_and_preserves_physical_evidence(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.store.async_load = AsyncMock(
            return_value={
                "policy_revision": 6,
                "mode": self.const.DHW_MODE_SAFE,
                "lease_expires": "2026-10-20T23:00:00+00:00",
                "confirmed_writes": 12,
                "heating_durations": [20, 22],
                "readiness_topup_durations": [18],
                "readiness_pilot_started_at": (
                    "2026-10-16T05:00:00+00:00"
                ),
                "readiness_pilot_confirmed_writes": 2,
                "pilot_started_at": "2026-10-17T05:00:00+00:00",
                "safe_pilot_confirmed_writes": 10,
                "owner_full_control_approved_at": (
                    "2026-10-18T05:00:00+00:00"
                ),
                "qualified_opportunity_count": 4,
                "last_qualified_opportunity_plan_id": "synthetic-plan",
                "last_qualified_opportunity_at": (
                    "2026-10-20T03:00:00+00:00"
                ),
                "deadline_checks": {
                    "2026-10-20T21:00:00+03:00": (
                        self.module.READINESS_SUCCESS
                    )
                },
                "readiness_window_peaks": {
                    "2026-10-20T21:00:00+03:00": {
                        "temperature": 50,
                        "observed_at": "2026-10-20T17:55:00+00:00",
                    }
                },
                "readiness_rapid_draws": {
                    "2026-10-20T21:00:00+03:00": (
                        "2026-10-20T18:09:00+00:00"
                    )
                },
            }
        )
        optimizer.store.async_save = AsyncMock()
        optimizer.async_evaluate = AsyncMock()
        await optimizer.async_start()
        try:
            self.assertEqual(optimizer.mode, self.const.DHW_MODE_SHADOW)
            self.assertEqual(optimizer.state["policy_revision"], 10)
            self.assertIsNone(optimizer.state["lease_expires"])
            self.assertEqual(optimizer.state["confirmed_writes"], 12)
            self.assertEqual(optimizer.state["heating_durations"], [20, 22])
            self.assertEqual(
                optimizer.state["readiness_topup_durations"],
                [18],
            )
            self.assertEqual(
                optimizer.state["readiness_pilot_confirmed_writes"],
                2,
            )
            self.assertEqual(
                optimizer.state["safe_pilot_confirmed_writes"],
                10,
            )
            self.assertEqual(
                optimizer.state["owner_full_control_approved_at"],
                "2026-10-18T05:00:00+00:00",
            )
            self.assertEqual(
                optimizer.state["qualified_opportunity_count"],
                0,
            )
            self.assertIsNone(
                optimizer.state["last_qualified_opportunity_plan_id"]
            )
            self.assertEqual(optimizer.state["deadline_checks"], {})
            self.assertEqual(
                optimizer.state["readiness_window_peaks"],
                {
                    "2026-10-20T21:00:00+03:00": {
                        "temperature": 50,
                        "observed_at": "2026-10-20T17:55:00+00:00",
                    }
                },
            )
            self.assertEqual(
                optimizer.state["readiness_rapid_draws"],
                {
                    "2026-10-20T21:00:00+03:00": (
                        "2026-10-20T18:09:00+00:00"
                    )
                },
            )
            self.assertNotIn(
                "readiness_final_topup_active",
                optimizer.state,
            )
            self.devices.control_daikin.assert_not_awaited()
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
        with self.assertRaisesRegex(HomeAssistantError, "Unknown"):
            await optimizer.async_set_mode(self.const.DHW_MODE_SOFT)

    async def test_owner_approved_activation_records_override_and_enables_safe_mode(self):
        optimizer = self.module.DhwOptimizer(
            self.hass, self.pricing_entry, self.coordinator, self.devices
        )
        optimizer.async_evaluate = AsyncMock()

        await optimizer.async_activate_full_control()

        self.assertEqual(optimizer.mode, self.const.DHW_MODE_SAFE)
        self.assertIsNotNone(
            optimizer.state["owner_full_control_approved_at"]
        )
        self.assertTrue(optimizer.attributes["owner_full_control_approved"])
        optimizer.async_evaluate.assert_awaited_once()

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
        optimizer.state["deadline_checks"]["2026-10-09T07:00:00+03:00"] = (
            self.module.READINESS_ACCEPTED
        )
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
        self.assertEqual(flow._validate(valid), valid)
        runtime_devices.daikin_api = lambda: (
            SimpleNamespace(entry_id="daikin"),
            SimpleNamespace(water_tank_target_temp_config={
                "minValue": 40,
                "maxValue": 55,
                "stepValue": 1,
                "settable": True,
            }),
        )
        self.assertEqual(
            flow._validate(valid)["dhw_soft_target"],
            40,
        )
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
