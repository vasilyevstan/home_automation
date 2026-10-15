"""Guard and no-write setup tests using real HA APIs and mocked device clients."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError

import test_component as base


setUpModule = base.setUpModule
tearDownModule = base.tearDownModule


class EnergyControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.HAComponentTests.asyncSetUp(self)
        self.controls = importlib.import_module(base.COMPONENT + ".controls")
        self.devices = self.controls.EnergyDevices(self.hass, {})
        self.devices.record = AsyncMock()
        self.solar_state = {
            "mode": 0, "active": 1000, "command": 40125,
            "percent": 100.0, "watts": 8000, "maximum": 8000,
            "serial": "synthetic-inverter",
        }
        from huawei_solar import register_names as rn
        self.rn = rn

        async def get(name):
            field = {
                rn.SERIAL_NUMBER: "serial", rn.P_MAX: "maximum",
                rn.ACTIVE_POWER_PERCENTAGE_DERATING: "percent",
                rn.ACTIVE_POWER_FIXED_VALUE_DERATING: "watts",
            }[name]
            return SimpleNamespace(value=self.solar_state[field])

        async def get_multiple(names):
            self.assertEqual(names, [
                rn.ACTIVE_POWER_ADJUSTMENT_MODE,
                rn.ACTIVE_POWER_ADJUSTMENT_VALUE,
                rn.ACTIVE_POWER_ADJUSTMENT_COMMAND,
            ])
            return [SimpleNamespace(value=self.solar_state[key]) for key in ("mode", "active", "command")]

        async def set_limit(name, value):
            if name == rn.ACTIVE_POWER_PERCENTAGE_DERATING:
                self.solar_state.update(mode=0, active=round(value * 10), command=40125, percent=value)
            else:
                self.solar_state.update(mode=1, active=int(value), command=40126, watts=value)
            return True

        self.solar = SimpleNamespace(
            serial_number="synthetic-inverter",
            update_lock=asyncio.Lock(),
            client=SimpleNamespace(get=AsyncMock(side_effect=get), get_multiple=AsyncMock(side_effect=get_multiple)),
            set=AsyncMock(side_effect=set_limit),
            has_write_permission=Mock(side_effect=AssertionError("A setup write probe must never run")),
        )
        self.solar.client.set = self.solar.set
        self.devices.solar_device = Mock(return_value=self.solar)
        self.devices.entry = Mock(return_value=SimpleNamespace(data={}))
        flags = {name: False for name in self.controls.FLAGS}
        self.daikin_state = {
            "function/SpaceHeating": {
                "states": dict(flags),
                "operations": {"OperationMode": "heating", "Power": "on", "LeavingWaterTemperatureOffsetHeating": 0.0},
            },
            "function/DomesticHotWaterTank": {
                "states": {**flags, "WeatherDependentState": False},
                "operations": {"Power": "on", "DomesticHotWaterTemperatureHeating": 50.0},
            },
        }
        self.control_mode = "LWT control"
        self.effective_target = 50.0
        self.offset_profile = {"minValue": -10, "maxValue": 10, "stepValue": 1, "settable": True}
        self.water_profile = {"minValue": 30, "maxValue": 55, "stepValue": 1, "settable": True}

        async def resource(path):
            if path.endswith("UnitStatus/ControlModeState/la"):
                value = self.control_mode
            elif path.endswith("Operation/TargetTemperature/la"):
                value = self.effective_target
            else:
                raise AssertionError("Unexpected device resource")
            return {"m2m:rsp": {"rsc": 2000, "pc": {"m2m:cin": {"con": value}}}}

        self.daikin = SimpleNamespace(
            info={"serial_number": "synthetic-adapter"},
            water_tank_target_temp_config=self.water_profile,
            device=SimpleNamespace(
                get_current_state=AsyncMock(side_effect=lambda: deepcopy(self.daikin_state)),
                device_info=AsyncMock(return_value={"serial_number": "synthetic-adapter"}),
                profiles=[
                    {"label": "function/SpaceHeating", "idx": 1},
                    {"label": "function/DomesticHotWaterTank", "idx": 2},
                ],
                ws_connection=SimpleNamespace(request=AsyncMock(side_effect=resource)),
                climate_control=SimpleNamespace(unit=SimpleNamespace(operation_config={
                    "LeavingWaterTemperatureOffsetHeating": self.offset_profile,
                    "Power": {"settable": True},
                })),
                hot_water_tank=SimpleNamespace(unit=SimpleNamespace(operation_config={"Power": {"settable": True}})),
            ),
        )
        self.devices.daikin_api = Mock(return_value=(SimpleNamespace(entry_id="daikin"), self.daikin))
        self.devices.entity = Mock(side_effect=lambda entry_id, domain, suffix=None: domain + ".synthetic")
        self.writes = []

        async def write(call):
            self.writes.append((call.domain, call.service, dict(call.data)))
            if call.domain == "number":
                self.daikin_state["function/SpaceHeating"]["operations"]["LeavingWaterTemperatureOffsetHeating"] = call.data["value"]
            elif call.service == "set_temperature":
                self.effective_target = call.data["temperature"]
                self.daikin_state["function/DomesticHotWaterTank"]["operations"]["DomesticHotWaterTemperatureHeating"] = call.data["temperature"]
            elif call.domain == "switch":
                self.daikin_state["function/SpaceHeating"]["operations"]["Power"] = "on" if call.service == "turn_on" else "standby"
            else:
                self.daikin_state["function/DomesticHotWaterTank"]["operations"]["Power"] = "on" if call.data["operation_mode"] == "on" else "standby"

        for domain, service in (
            ("number", "set_value"), ("water_heater", "set_temperature"),
            ("water_heater", "set_operation_mode"), ("switch", "turn_on"), ("switch", "turn_off"),
        ):
            self.hass.services.async_register(domain, service, write)

    async def asyncTearDown(self):
        await base.HAComponentTests.asyncTearDown(self)

    def solar_request(self, **changes):
        return {
            "mode": "percent", "value": 50.0,
            "expected_mode": "percent", "expected_value": 100.0, "expected_active_value": 100.0,
            **changes,
        }

    async def test_solar_reads_and_setup_never_probe_write_permission(self):
        result = await self.devices.get_solar_limit()
        self.assertEqual(result["configured"], 100.0)
        self.assertEqual(result["active"], 100.0)
        self.solar.set.assert_not_awaited()
        self.solar.has_write_permission.assert_not_called()

    async def test_solar_read_waits_for_the_native_coordinator_lock(self):
        await self.solar.update_lock.acquire()
        task = asyncio.create_task(self.devices.get_solar_limit())
        await asyncio.sleep(0)
        self.solar.client.get.assert_not_awaited()
        self.assertFalse(task.done())
        self.solar.update_lock.release()
        result = await task
        self.assertEqual(result["active"], 100.0)
        self.assertFalse(self.solar.update_lock.locked())

    async def test_solar_write_holds_the_native_lock_through_readback(self):
        native_set = self.solar.set.side_effect
        async def write(name, value):
            self.assertTrue(self.solar.update_lock.locked())
            return await native_set(name, value)
        self.solar.set.side_effect = write
        result = await self.devices.set_solar_limit(self.solar_request())
        self.assertTrue(result["changed"])
        self.assertFalse(self.solar.update_lock.locked())

    async def test_solar_read_timeout_is_explicit_and_releases_native_lock(self):
        self.solar.client.get.side_effect = TimeoutError()
        with self.assertRaisesRegex(HomeAssistantError, "no control command was sent"):
            await self.devices.get_solar_limit()
        self.assertFalse(self.solar.update_lock.locked())
        self.solar.set.assert_not_awaited()
    async def test_solar_noop_is_read_only(self):
        result = await self.devices.set_solar_limit(self.solar_request(value=100.0))
        self.assertFalse(result["changed"])
        self.solar.set.assert_not_awaited()
        self.devices.record.assert_not_awaited()

    async def test_solar_single_requested_write_and_exact_readback(self):
        result = await self.devices.set_solar_limit(self.solar_request())
        self.assertTrue(result["changed"])
        self.assertEqual(result["confirmed"]["active"], 50.0)
        self.solar.set.assert_awaited_once_with(self.rn.ACTIVE_POWER_PERCENTAGE_DERATING, 50.0)
        self.solar.has_write_permission.assert_not_called()
        self.assertEqual([call.args[0] for call in self.devices.record.await_args_list], ["prepared", "verified"])

    async def test_solar_bounds_expected_state_and_mode_changes_fail_before_write(self):
        cases = (
            {"value": True}, {"value": float("inf")}, {"value": -1},
            {"value": 100.1}, {"value": 0.01}, {"expected_value": True},
            {"expected_active_value": 99.0}, {"expected_value": 99.0},
            {"mode": "watts", "value": 1000},
            {"mode": "watts", "value": 8001, "allow_mode_change": True},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(HomeAssistantError):
                await self.devices.set_solar_limit(self.solar_request(**changes))
        self.solar.set.assert_not_awaited()

    async def test_solar_identity_and_unsupported_control_modes_are_rejected(self):
        self.solar_state["serial"] = "different-inverter"
        with self.assertRaisesRegex(HomeAssistantError, "identity"):
            await self.devices.get_solar_limit()
        self.solar_state.update(serial="synthetic-inverter", mode=9)
        with self.assertRaisesRegex(HomeAssistantError, "control mode"):
            await self.devices.get_solar_limit()

    async def test_solar_explicit_mode_change_uses_native_pmax_and_one_write(self):
        result = await self.devices.set_solar_limit(
            self.solar_request(mode="watts", value=3000, allow_mode_change=True)
        )
        self.assertEqual(result["confirmed"]["mode"], "watts")
        self.assertEqual(result["confirmed"]["active"], 3000)
        self.solar.set.assert_awaited_once_with(self.rn.ACTIVE_POWER_FIXED_VALUE_DERATING, 3000.0)

    async def test_solar_active_mismatch_is_not_a_false_noop(self):
        self.solar_state["active"] = 500
        result = await self.devices.set_solar_limit(
            self.solar_request(value=100, expected_active_value=50)
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["confirmed"]["active"], 100)
        self.solar.set.assert_awaited_once()

    async def test_solar_write_error_is_uncertain_and_not_retried(self):
        self.solar.set.side_effect = OSError("Synthetic lost acknowledgement")
        with self.assertRaisesRegex(HomeAssistantError, "may have applied"):
            await self.devices.set_solar_limit(self.solar_request())
        self.solar.set.assert_awaited_once()
        self.assertEqual(self.devices.record.await_args_list[-1].args[0], "uncertain")

    async def test_daikin_noop_and_invalid_inputs_do_not_write(self):
        result = await self.devices.control_daikin({"operation": "hot_water_target", "value": 50}, None)
        self.assertFalse(result["changed"])
        for operation, value in (
            ("hot_water_target", 60), ("hot_water_target", 50.5),
            ("heating_offset", -11), ("heating_offset", True),
            ("heating_enabled", 1), ("hot_water_enabled", "true"),
        ):
            with self.subTest(operation=operation, value=value), self.assertRaises(HomeAssistantError):
                await self.devices.control_daikin({"operation": operation, "value": value}, None)
        self.assertEqual(self.writes, [])

    async def test_daikin_requires_all_known_inactive_flags(self):
        for flag in self.controls.FLAGS:
            for value in (True, None):
                with self.subTest(flag=flag, value=value):
                    self.daikin_state["function/SpaceHeating"]["states"][flag] = value
                    with self.assertRaisesRegex(HomeAssistantError, "safety flag"):
                        await self.devices.control_daikin({"operation": "heating_offset", "value": -2}, None)
                    self.daikin_state["function/SpaceHeating"]["states"][flag] = False
        self.assertEqual(self.writes, [])

    async def test_daikin_monitoring_reports_flags_without_enabling_anything(self):
        self.daikin_state["function/SpaceHeating"]["states"]["ErrorState"] = True
        result = await self.devices.get_daikin_status()
        self.assertTrue(result["heating"]["states"]["ErrorState"])
        self.assertEqual(result["control_mode"], "LWT control")
        self.assertEqual(self.writes, [])

    async def test_daikin_native_modes_and_identity_are_required(self):
        self.control_mode = "Room temperature control"
        with self.assertRaisesRegex(HomeAssistantError, "LWT"):
            await self.devices.control_daikin({"operation": "heating_offset", "value": -2}, None)
        self.daikin_state["function/DomesticHotWaterTank"]["states"]["WeatherDependentState"] = True
        with self.assertRaisesRegex(HomeAssistantError, "Weather-dependent"):
            await self.devices.control_daikin({"operation": "hot_water_target", "value": 51}, None)
        self.daikin.device.device_info.return_value = {"serial_number": "different"}
        with self.assertRaisesRegex(HomeAssistantError, "identity"):
            await self.devices.control_daikin({"operation": "heating_enabled", "value": False}, None)
        self.assertEqual(self.writes, [])

    async def test_daikin_fresh_readback_confirms_one_native_service_call(self):
        with patch.object(self.controls.asyncio, "sleep", new=AsyncMock()):
            result = await self.devices.control_daikin({"operation": "hot_water_target", "value": 51}, None)
        self.assertTrue(result["changed"])
        self.assertEqual(result["confirmed"], 51)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0][:2], ("water_heater", "set_temperature"))

    async def test_daikin_offset_and_standby_are_not_room_or_boost_commands(self):
        with patch.object(self.controls.asyncio, "sleep", new=AsyncMock()):
            result = await self.devices.control_daikin({"operation": "heating_offset", "value": -2}, None)
            self.assertEqual(result["confirmed"], -2)
            result = await self.devices.control_daikin({"operation": "hot_water_enabled", "value": False}, None)
            self.assertEqual(result["confirmed"], "standby")
        self.assertEqual([item[:2] for item in self.writes], [
            ("number", "set_value"), ("water_heater", "set_operation_mode"),
        ])
        self.assertEqual(self.writes[-1][2]["operation_mode"], "off")

    async def test_daikin_service_failure_is_uncertain_and_not_retried(self):
        async def failed(call):
            self.writes.append((call.domain, call.service))
            raise HomeAssistantError("Synthetic missing acknowledgement")
        self.hass.services.async_register("number", "set_value", failed)
        with self.assertRaisesRegex(HomeAssistantError, "may have applied"):
            await self.devices.control_daikin({"operation": "heating_offset", "value": -2}, None)
        self.assertEqual(self.writes, [("number", "set_value")])
        self.assertEqual(self.devices.record.await_args_list[-1].args[0], "uncertain")

    async def test_daikin_effective_target_must_match_even_for_a_noop(self):
        self.effective_target = 49
        with self.assertRaisesRegex(HomeAssistantError, "effective target"):
            await self.devices.control_daikin({"operation": "hot_water_target", "value": 50}, None)
        self.assertEqual(self.writes, [])

    async def test_daikin_unconfirmed_effective_target_is_uncertain(self):
        async def partial(call):
            self.writes.append((call.domain, call.service))
            self.daikin_state["function/DomesticHotWaterTank"]["operations"]["DomesticHotWaterTemperatureHeating"] = call.data["temperature"]
        self.hass.services.async_register("water_heater", "set_temperature", partial)
        with patch.object(self.controls.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(HomeAssistantError, "may have applied"):
                await self.devices.control_daikin({"operation": "hot_water_target", "value": 51}, None)
        self.assertEqual(self.writes, [("water_heater", "set_temperature")])
    async def test_control_api_is_separate_and_empty_without_explicit_bindings(self):
        api = self.controls.HomeEnergyDeviceAPI(self.hass, {})
        self.assertEqual((await api.async_get_api_instance(None)).tools, [])
        api = self.controls.HomeEnergyDeviceAPI(self.hass, {
            "solar_config_entry": "solar", "daikin_config_entry": "daikin",
        })
        tools = (await api.async_get_api_instance(None)).tools
        self.assertEqual([tool.name for tool in tools], [
            "GetSolarGenerationLimit", "SetSolarGenerationLimit", "GetDaikinStatus", "ControlDaikin",
        ])
        self.assertTrue(tools[0].annotations.read_only)
        self.assertFalse(tools[1].annotations.read_only)
        self.assertTrue(tools[2].annotations.read_only)
        self.assertFalse(tools[3].annotations.read_only)

    async def test_control_api_can_share_one_serialized_device_runtime(self):
        api = self.controls.HomeEnergyDeviceAPI(
            self.hass,
            {"daikin_config_entry": "daikin"},
            devices=self.devices,
        )
        self.assertIs(api.devices, self.devices)

    def test_control_journal_is_private_and_records_only_explicit_events(self):
        path = Path(self.config_directory.name) / ".storage/control.jsonl"
        self.controls.append_record(path, {"event": "prepared", "request_id": "synthetic"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["event"], "prepared")


if __name__ == "__main__":
    unittest.main()
