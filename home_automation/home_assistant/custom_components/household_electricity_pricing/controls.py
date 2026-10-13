"""Explicit, bounded controls using the existing HA device connections."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

import aiohttp
import probatio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry, llm
from homeassistant.helpers.llm import LLMContext, ToolAnnotations, ToolInput, ToolResult

from .const import CONF_DAIKIN_ENTRY, CONF_SOLAR_ENTRY, CONTROL_API_ID, DOMAIN


LOGGER = logging.getLogger(__name__)
FLAGS = ("ErrorState", "WarningState", "EmergencyState", "InstallerState")


def number(value, label, minimum, maximum, step):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise HomeAssistantError(label + " must be a number, not a string or boolean.")
    try:
        result = Decimal(str(value))
        increment = Decimal(str(step))
        low, high = Decimal(str(minimum)), Decimal(str(maximum))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise HomeAssistantError("Invalid native bounds for " + label) from error
    if not all(item.is_finite() for item in (result, increment, low, high)) or increment <= 0:
        raise HomeAssistantError("Invalid or unavailable native bounds for " + label)
    if not low <= result <= high or (result - low) % increment != 0:
        raise HomeAssistantError(label + " is outside its native range or step.")
    return float(result)


def append_record(path: Path, record: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class EnergyDevices:
    def __init__(self, hass: HomeAssistant, options):
        self.hass = hass
        self.solar_entry = options.get(CONF_SOLAR_ENTRY)
        self.daikin_entry = options.get(CONF_DAIKIN_ENTRY)
        self.solar_lock = asyncio.Lock()
        self.daikin_lock = asyncio.Lock()
        self.journal = Path(hass.config.path(".storage/home_energy_control.jsonl"))

    def entry(self, identifier, domain):
        entry = self.hass.config_entries.async_get_entry(identifier) if identifier else None
        if entry is None or entry.domain != domain or entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError("The explicitly selected " + domain + " integration is not loaded.")
        return entry

    async def record(self, event, record):
        await self.hass.async_add_executor_job(
            append_record, self.journal,
            {**record, "event": event, "observed_at": datetime.now(timezone.utc).isoformat()},
        )

    def solar_device(self):
        entry = self.entry(self.solar_entry, "huawei_solar")
        from huawei_solar import SUN2000Device

        devices = [
            item.device for item in entry.runtime_data["device_datas"]
            if isinstance(item.device, SUN2000Device)
        ]
        if len(devices) != 1:
            raise HomeAssistantError("Select a Huawei entry with exactly one verified SUN2000 inverter.")
        return devices[0]

    async def solar_limit(self, device):
        from huawei_solar import register_names as rn

        serial = (await device.client.get(rn.SERIAL_NUMBER)).value
        if serial != device.serial_number:
            raise HomeAssistantError("Inverter identity changed; no control is permitted.")
        values = await device.client.get_multiple([
            rn.ACTIVE_POWER_ADJUSTMENT_MODE,
            rn.ACTIVE_POWER_ADJUSTMENT_VALUE,
            rn.ACTIVE_POWER_ADJUSTMENT_COMMAND,
        ])
        if len(values) != 3 or any(type(item.value) is not int for item in values):
            raise HomeAssistantError("The active inverter limit is unavailable.")
        mode, active, command = (item.value for item in values)
        maximum = number((await device.client.get(rn.P_MAX)).value, "Pmax", 1, 1e9, 1)
        if (mode, command) == (0, 40125):
            configured = number(
                (await device.client.get(rn.ACTIVE_POWER_PERCENTAGE_DERATING)).value,
                "Configured percentage", 0, 100, 0.1,
            )
            actual = number(active / 10, "Active percentage", 0, 100, 0.1)
            label = "percent"
        elif (mode, command) == (1, 40126):
            configured = number(
                (await device.client.get(rn.ACTIVE_POWER_FIXED_VALUE_DERATING)).value,
                "Configured watt limit", 0, maximum, 1,
            )
            actual = number(active, "Active watt limit", 0, maximum, 1)
            label = "watts"
        else:
            raise HomeAssistantError("Another inverter control mode is active; it will not be replaced.")
        return {
            "mode": label, "configured": configured, "active": actual, "pmax_w": maximum,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    async def get_solar_limit(self):
        try:
            async with self.solar_session() as device:
                return await self.solar_limit(device)
        except TimeoutError as error:
            raise HomeAssistantError(
                "The inverter did not respond within the read deadline; no control command was sent."
            ) from error

    @asynccontextmanager
    async def solar_session(self):
        async with asyncio.timeout(30), self.solar_lock:
            device = self.solar_device()
            # Native coordinator batches use this same lock.
            async with device.update_lock:
                yield device

    async def set_solar_limit(self, args):
        entry = self.entry(self.solar_entry, "huawei_solar")
        from huawei_solar import HuaweiSolarException, register_names as rn

        async with self.solar_session() as device:
            previous = await self.solar_limit(device)
            expected = number(
                args["expected_value"], "Expected limit", 0,
                100 if previous["mode"] == "percent" else previous["pmax_w"],
                0.1 if previous["mode"] == "percent" else 1,
            )
            expected_active = number(
                args["expected_active_value"], "Expected active limit", 0,
                100 if previous["mode"] == "percent" else previous["pmax_w"],
                0.1 if previous["mode"] == "percent" else 1,
            )
            if (
                args["expected_mode"] != previous["mode"]
                or expected != previous["configured"]
                or expected_active != previous["active"]
            ):
                raise HomeAssistantError("The inverter limit changed; read it again before requesting a change.")
            mode = args["mode"]
            if mode not in ("percent", "watts"):
                raise HomeAssistantError("Choose percent or watts.")
            if mode != previous["mode"] and args.get("allow_mode_change") is not True:
                raise HomeAssistantError("Changing the limit mode needs explicit allow_mode_change=true.")
            requested = number(
                args["value"], "Requested limit", 0,
                100 if mode == "percent" else previous["pmax_w"],
                0.1 if mode == "percent" else 1,
            )
            if mode == previous["mode"] and requested == previous["configured"] == previous["active"]:
                return {"changed": False, "previous": previous, "confirmed": previous}
            if entry.data.get("username"):
                if not await device.ensure_logged_in() or not await device.client.heartbeat():
                    raise HomeAssistantError("Configured inverter authentication failed; no cap write was sent.")
                refreshed = await self.solar_limit(device)
                if any(refreshed[key] != previous[key] for key in ("mode", "configured", "active", "pmax_w")):
                    raise HomeAssistantError("The inverter limit changed during authentication; read it again.")
            record = {
                "request_id": str(uuid4()), "device": "solar", "previous": previous,
                "requested": {"mode": mode, "value": requested},
            }
            await self.record("prepared", record)
            register = (
                rn.ACTIVE_POWER_PERCENTAGE_DERATING
                if mode == "percent" else rn.ACTIVE_POWER_FIXED_VALUE_DERATING
            )
            try:
                if not await device.client.set(register, requested):
                    raise HomeAssistantError("The inverter did not acknowledge the requested limit.")
                for attempt in range(4):
                    current = await self.solar_limit(device)
                    if current["mode"] == mode and current["configured"] == requested and current["active"] == requested:
                        await self.record("verified", {**record, "confirmed": current})
                        return {"changed": True, "previous": previous, "confirmed": current}
                    if attempt < 3:
                        await asyncio.sleep(0.5)
                raise HomeAssistantError("The requested configured and active limits were not both confirmed.")
            except asyncio.CancelledError:
                LOGGER.error("Solar request cancelled after dispatch began; its prepared journal record remains uncertain.")
                raise
            except (HomeAssistantError, HuaweiSolarException, TimeoutError, OSError) as error:
                LOGGER.error("Solar command outcome is uncertain; read the current limit before further action.")
                try:
                    await self.record("uncertain", record)
                except OSError:
                    LOGGER.exception("Could not persist the uncertain solar outcome.")
                raise HomeAssistantError(
                    "The inverter command may have applied. Read its limit before any retry or restoration; "
                    "no automatic retry or rollback was performed."
                ) from error

    def daikin_api(self):
        entry = self.entry(self.daikin_entry, "daikin_altherma")
        api = self.hass.data.get("daikin_altherma", {}).get(entry.entry_id)
        if api is None:
            raise HomeAssistantError("The selected Daikin client is unavailable.")
        return entry, api

    async def daikin_observation(self, api):
        observed = await api.device.get_current_state()
        identity = await api.device.device_info()
        if identity.get("serial_number") != api.info.get("serial_number"):
            raise HomeAssistantError("Daikin identity changed; no control is permitted.")
        heating = observed.get("function/SpaceHeating")
        water = observed.get("function/DomesticHotWaterTank", observed.get("function/DomesticHotWater"))
        if not isinstance(heating, dict) or not isinstance(water, dict):
            raise HomeAssistantError("Both Daikin circuit observations are required.")
        return heating, water

    async def get_daikin_status(self):
        async with self.daikin_lock, asyncio.timeout(30):
            _, api = self.daikin_api()
            heating, water = await self.daikin_observation(api)
            mode = await self.daikin_resource(
                api, {"function/SpaceHeating"}, "UnitStatus/ControlModeState"
            )
            return {
                "heating": heating, "hot_water": water, "control_mode": mode,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "measurement_freshness": "Fresh adapter retrieval does not prove the age of every measurement.",
            }

    async def daikin_snapshot(self, api):
        heating, water = await self.daikin_observation(api)
        for circuit in (heating, water):
            if any(circuit.get("states", {}).get(flag) is not False for flag in FLAGS):
                raise HomeAssistantError("A Daikin safety flag is active or unknown.")
        if heating.get("operations", {}).get("OperationMode") != "heating":
            raise HomeAssistantError("These controls require the existing heating mode.")
        return heating, water

    async def daikin_resource(self, api, labels, resource):
        profiles = [item for item in api.device.profiles if item["label"] in labels]
        if len(profiles) != 1:
            raise HomeAssistantError("The selected Daikin unit identity is ambiguous.")
        response = await api.device.ws_connection.request(
            f"[0]/MNAE/{profiles[0]['idx']}/{resource}/la"
        )
        envelope = response.get("m2m:rsp", {})
        if envelope.get("rsc") != 2000:
            raise HomeAssistantError("Required Daikin control metadata is unavailable.")
        return envelope.get("pc", {}).get("m2m:cin", {}).get("con")

    def entity(self, entry_id, domain, suffix=None):
        registry = entity_registry.async_get(self.hass)
        matches = [
            item.entity_id for item in entity_registry.async_entries_for_config_entry(registry, entry_id)
            if item.entity_id.startswith(domain + ".") and item.disabled_by is None
            and (suffix is None or item.unique_id.endswith(suffix))
        ]
        if len(matches) != 1:
            raise HomeAssistantError("The selected Daikin control entity is missing or ambiguous.")
        return matches[0]

    async def control_daikin(self, args, context):
        async with self.daikin_lock, asyncio.timeout(45):
            entry, api = self.daikin_api()
            from pyaltherma.controllers import AlthermaException

            heating, water = await self.daikin_snapshot(api)
            operation = args["operation"]
            value = args["value"]
            if operation == "hot_water_target":
                if water["states"].get("WeatherDependentState") is not False:
                    raise HomeAssistantError("Weather-dependent hot-water control is active or unknown.")
                profile = api.water_tank_target_temp_config
                if not profile or profile.get("settable") is not True:
                    raise HomeAssistantError("The native hot-water target is not settable.")
                requested = number(value, "Hot-water target", profile["minValue"], profile["maxValue"], profile["stepValue"])
                native_key = "DomesticHotWaterTemperatureHeating"
                selected = water
                target = self.entity(entry.entry_id, "water_heater")
                domain, service, data = "water_heater", "set_temperature", {"temperature": requested}
            elif operation == "heating_offset":
                control_mode = await self.daikin_resource(
                    api, {"function/SpaceHeating"}, "UnitStatus/ControlModeState"
                )
                if control_mode != "LWT control":
                    raise HomeAssistantError("Heating-offset changes require freshly verified LWT control.")
                native_key = "LeavingWaterTemperatureOffsetHeating"
                profile = api.device.climate_control.unit.operation_config.get(native_key)
                if not profile or profile.get("settable") is not True:
                    raise HomeAssistantError("The native LWT offset is not settable.")
                requested = number(value, "LWT offset", profile["minValue"], profile["maxValue"], profile["stepValue"])
                selected = heating
                target = self.entity(entry.entry_id, "number", "-SpaceHeating-temp-control")
                domain, service, data = "number", "set_value", {"value": requested}
            elif operation in ("hot_water_enabled", "heating_enabled"):
                if type(value) is not bool:
                    raise HomeAssistantError("Circuit enabled must be an actual boolean.")
                requested = "on" if value else "standby"
                native_key = "Power"
                selected = water if operation == "hot_water_enabled" else heating
                controller = api.device.hot_water_tank if operation == "hot_water_enabled" else api.device.climate_control
                profile = controller.unit.operation_config.get(native_key)
                if not profile or profile.get("settable") is not True:
                    raise HomeAssistantError("The native circuit enable operation is not settable.")
                if operation == "hot_water_enabled":
                    target = self.entity(entry.entry_id, "water_heater")
                    domain, service, data = "water_heater", "set_operation_mode", {"operation_mode": "on" if value else "off"}
                else:
                    target = self.entity(entry.entry_id, "switch")
                    domain, service, data = "switch", "turn_on" if value else "turn_off", {}
            else:
                raise HomeAssistantError("Unsupported Daikin operation.")
            previous = selected.get("operations", {}).get(native_key)
            if previous is None:
                raise HomeAssistantError("The previous native setting is unknown.")
            if operation in ("hot_water_target", "heating_offset"):
                previous = number(previous, "Previous native setting", profile["minValue"], profile["maxValue"], profile["stepValue"])
            elif previous not in ("on", "standby"):
                raise HomeAssistantError("The previous circuit enable state is unknown.")
            if previous == requested:
                if operation == "hot_water_target":
                    effective = await self.daikin_resource(
                        api, {"function/DomesticHotWaterTank", "function/DomesticHotWater"},
                        "Operation/TargetTemperature",
                    )
                    if effective != requested:
                        raise HomeAssistantError("The configured target matches, but its effective target does not; no write was sent.")
                return {"changed": False, "operation": operation, "previous": previous, "confirmed": previous}
            record = {
                "request_id": str(uuid4()), "device": "daikin", "operation": operation,
                "entity_id": target, "previous": previous, "requested": requested,
            }
            await self.record("prepared", record)
            try:
                await self.hass.services.async_call(
                    domain, service, {"entity_id": target, **data}, blocking=True, context=context,
                )
                for attempt in range(7):
                    await asyncio.sleep(2)
                    heating, water = await self.daikin_snapshot(api)
                    selected = water if operation.startswith("hot_water") else heating
                    confirmed = selected.get("operations", {}).get(native_key)
                    effective = (
                        await self.daikin_resource(
                            api, {"function/DomesticHotWaterTank", "function/DomesticHotWater"},
                            "Operation/TargetTemperature",
                        )
                        if operation == "hot_water_target" else None
                    )
                    if confirmed == requested and (operation != "hot_water_target" or effective == requested):
                        await self.record("verified", {**record, "confirmed": confirmed})
                        return {"changed": True, "operation": operation, "previous": previous, "confirmed": confirmed}
                raise HomeAssistantError("Fresh configured/effective read-back did not confirm the request.")
            except asyncio.CancelledError:
                LOGGER.error("Daikin request cancelled after dispatch began; its prepared journal record remains uncertain.")
                raise
            except (HomeAssistantError, AlthermaException, aiohttp.ClientError, TimeoutError, OSError, ValueError, KeyError) as error:
                LOGGER.error("Daikin command outcome is uncertain; read fresh state before further action.")
                try:
                    await self.record("uncertain", record)
                except OSError:
                    LOGGER.exception("Could not persist the uncertain Daikin outcome.")
                raise HomeAssistantError(
                    "The Daikin command may have applied. Read fresh status before retrying; "
                    "no automatic retry, boost, mode change, or rollback was performed."
                ) from error


class SolarLimitRead(llm.Tool):
    name = "GetSolarGenerationLimit"
    title = "Read solar generation limit"
    description = "Read the configured and active percent/watt cap from HA's existing inverter connection."
    integration = DOMAIN
    annotations = ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False)

    def __init__(self, devices):
        self.devices = devices

    async def async_call(self, hass, tool_input, llm_context):
        return ToolResult(data=await self.devices.get_solar_limit())


class SolarLimitWrite(llm.Tool):
    name = "SetSolarGenerationLimit"
    title = "Explicitly set solar generation limit"
    description = "Only for an explicit user request. Uses expected configured/active values, native bounds and fresh read-back. No setup permission-test write is performed. Any command error may mean the limit already changed."
    integration = DOMAIN
    annotations = ToolAnnotations(read_only=False, destructive=False, idempotent=False, open_world=False)
    parameters = probatio.Schema({
        probatio.Required("mode"): probatio.In(("percent", "watts")),
        probatio.Required("value"): probatio.Any(int, float),
        probatio.Required("expected_mode"): probatio.In(("percent", "watts")),
        probatio.Required("expected_value"): probatio.Any(int, float),
        probatio.Required("expected_active_value"): probatio.Any(int, float),
        probatio.Optional("allow_mode_change", default=False): bool,
    })

    def __init__(self, devices):
        self.devices = devices

    async def async_call(self, hass, tool_input, llm_context):
        try:
            return ToolResult(data=await self.devices.set_solar_limit(tool_input.tool_args))
        except TimeoutError as error:
            raise HomeAssistantError(
                "The solar operation timed out and may have applied. Read its limit before retrying."
            ) from error


class DaikinWrite(llm.Tool):
    name = "ControlDaikin"
    title = "Explicitly adjust the selected Daikin"
    description = "Only for an explicit user request. Adjust a native DHW target, LWT offset, or circuit enable/standby. Requires heating mode, known inactive safety flags, valid native range/step and fresh read-back. No boost, operating-mode or protection changes."
    integration = DOMAIN
    annotations = ToolAnnotations(read_only=False, destructive=False, idempotent=False, open_world=False)
    parameters = probatio.Schema({
        probatio.Required("operation"): probatio.In(("hot_water_target", "heating_offset", "hot_water_enabled", "heating_enabled")),
        probatio.Required("value"): probatio.Any(bool, int, float),
    })

    def __init__(self, devices):
        self.devices = devices

    async def async_call(self, hass, tool_input, llm_context):
        try:
            return ToolResult(data=await self.devices.control_daikin(tool_input.tool_args, llm_context.context))
        except TimeoutError as error:
            raise HomeAssistantError(
                "The Daikin operation timed out and may have applied. Read its state before retrying."
            ) from error


class DaikinRead(llm.Tool):
    name = "GetDaikinStatus"
    title = "Read the selected Daikin"
    description = "Read native circuit settings, measurements, safety flags and control mode. Retrieval is not a command or proof that heating is active."
    integration = DOMAIN
    annotations = ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False)

    def __init__(self, devices):
        self.devices = devices

    async def async_call(self, hass, tool_input, llm_context):
        return ToolResult(data=await self.devices.get_daikin_status())


class HomeEnergyDeviceAPI(llm.API):
    def __init__(self, hass, options):
        super().__init__(hass=hass, id=CONTROL_API_ID, name="Home energy devices (guarded)")
        self.devices = EnergyDevices(hass, options)

    async def async_get_api_instance(self, llm_context: LLMContext) -> llm.APIInstance:
        tools = []
        if self.devices.solar_entry:
            tools.extend((SolarLimitRead(self.devices), SolarLimitWrite(self.devices)))
        if self.devices.daikin_entry:
            tools.extend((DaikinRead(self.devices), DaikinWrite(self.devices)))
        return llm.APIInstance(
            api=self, llm_context=llm_context, tools=tools,
            api_prompt="Device writes require an explicit user request. Never optimize, toggle, probe write permission, or change settings for a status/price question. Errors after dispatch can mean a command applied; read state instead of replaying it.",
        )
