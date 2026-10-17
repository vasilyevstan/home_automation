"""Explicit, bounded controls using the existing HA device connections."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import math
import os
from pathlib import Path
from uuid import uuid4

import aiohttp
import probatio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry, llm
from homeassistant.helpers.llm import LLMContext, ToolAnnotations, ToolInput, ToolResult
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DAIKIN_ENTRY,
    CONF_EXTREME_PRICE_CLIMATE_ENTITIES,
    CONF_SOLAR_ENTRY,
    CONTROL_API_ID,
    DOMAIN,
)


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
        climate_entities = options.get(CONF_EXTREME_PRICE_CLIMATE_ENTITIES, ())
        self.climate_entities = frozenset(
            item
            for item in climate_entities
            if isinstance(item, str) and item.startswith("climate.")
        )
        self.solar_lock = asyncio.Lock()
        self.daikin_lock = asyncio.Lock()
        self.climate_lock = asyncio.Lock()
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

    def _climate_identity(self, entity_id: str) -> dict:
        registry = entity_registry.async_get(self.hass)
        try:
            registry_entry = registry.async_get(entity_id)
        except AttributeError:
            registry_entry = None
        return {
            "entity_id": entity_id,
            "platform": (
                registry_entry.platform if registry_entry is not None else None
            ),
            "unique_id": (
                registry_entry.unique_id if registry_entry is not None else None
            ),
        }

    def _climate_status(self, entity_id: str) -> dict:
        if entity_id not in self.climate_entities:
            raise HomeAssistantError(
                "The climate entity is not in the explicit extreme-price allowlist."
            )
        state = self.hass.states.get(entity_id)
        if state is None:
            return {
                "entity_id": entity_id,
                "identity": self._climate_identity(entity_id),
                "available": False,
                "fresh": False,
                "reason": "The climate entity is missing.",
            }
        observed_at = getattr(state, "last_reported", None) or state.last_updated
        age = dt_util.utcnow() - observed_at.astimezone(timezone.utc)
        available = state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        attributes = state.attributes

        def finite(value):
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                return None
            result = float(value)
            return result if math.isfinite(result) else None

        return {
            "entity_id": entity_id,
            "identity": self._climate_identity(entity_id),
            "available": available,
            "fresh": available and age <= timedelta(minutes=30),
            "reason": (
                None
                if available and age <= timedelta(minutes=30)
                else (
                    "The climate entity is unavailable."
                    if not available
                    else "The climate entity state is stale."
                )
            ),
            "state": state.state if available else None,
            "current_temperature": finite(
                attributes.get("current_temperature")
            ),
            "temperature": finite(attributes.get("temperature")),
            "temperature_unit": attributes.get("temperature_unit"),
            "fan_mode": attributes.get("fan_mode"),
            "swing_mode": attributes.get("swing_mode"),
            "swing_horizontal_mode": attributes.get("swing_horizontal_mode"),
            "hvac_modes": list(attributes.get("hvac_modes") or ()),
            "fan_modes": list(attributes.get("fan_modes") or ()),
            "swing_modes": list(attributes.get("swing_modes") or ()),
            "swing_horizontal_modes": list(
                attributes.get("swing_horizontal_modes") or ()
            ),
            "minimum_temperature": finite(attributes.get("min_temp")),
            "maximum_temperature": finite(attributes.get("max_temp")),
            "target_temperature_step": finite(
                attributes.get("target_temp_step")
            ),
            "observed_at": observed_at.isoformat(),
            "confirmation_basis": "home_assistant_cloud_reported_state",
            "physical_hvac_state_verified": False,
        }

    async def get_climate_status(self, entity_id: str) -> dict:
        async with self.climate_lock:
            return self._climate_status(entity_id)

    async def _activate_climate(
        self,
        entity_id: str,
        requested: dict,
        context: Context | None,
    ) -> None:
        temperature = requested.get("temperature")
        if temperature is not None:
            await self._call_climate(
                "set_temperature",
                {
                    "entity_id": entity_id,
                    "temperature": temperature,
                },
                context,
            )
        for service, key in (
            ("set_fan_mode", "fan_mode"),
            ("set_swing_mode", "swing_mode"),
            (
                "set_swing_horizontal_mode",
                "swing_horizontal_mode",
            ),
        ):
            value = requested.get(key)
            if value is not None:
                await self._call_climate(
                    service,
                    {"entity_id": entity_id, key: value},
                    context,
                )
        # Sensibo preserves setting changes while off; power on only after the
        # exact saved cooling/restoration state has been prepared.
        await self._call_climate(
            "set_hvac_mode",
            {
                "entity_id": entity_id,
                "hvac_mode": requested["state"],
            },
            context,
        )

    async def prepare_climate_commissioning(
        self,
        entity_id: str,
        expected: dict,
        context: Context | None,
    ) -> dict:
        if not isinstance(expected, dict):
            raise HomeAssistantError(
                "Climate commissioning needs an expected off-state snapshot."
            )
        async with self.climate_lock, asyncio.timeout(30):
            previous = self._climate_status(entity_id)
            if not previous.get("available") or not previous.get("fresh"):
                raise HomeAssistantError(
                    previous.get("reason")
                    or "Fresh climate state is unavailable; no command was sent."
                )
            if not self._climate_state_matches(previous, expected):
                raise HomeAssistantError(
                    "The climate state changed; read it again before commissioning."
                )
            if previous.get("state") != "off":
                raise HomeAssistantError(
                    "Climate commissioning preparation requires the unit to be off."
                )
            if "cool" not in previous.get("hvac_modes", ()):
                raise HomeAssistantError(
                    "Climate commissioning requires a supported cooling mode."
                )
            target = previous.get("temperature")
            current = previous.get("current_temperature")
            if target is None or current is None or target < current + 1:
                raise HomeAssistantError(
                    "The saved cooling target needs at least 1°C headroom so "
                    "commissioning does not request compressor cooling."
                )
            requested = {**previous, "state": "cool"}
            self._validate_climate_restore(previous, requested)
            record = {
                "request_id": str(uuid4()),
                "device": "climate:" + entity_id,
                "operation": "commissioning_prepare",
                "previous": previous,
                "requested": {
                    key: requested.get(key)
                    for key in (
                        "state",
                        "temperature",
                        "temperature_unit",
                        "fan_mode",
                        "swing_mode",
                        "swing_horizontal_mode",
                    )
                },
                "confirmation_basis": "home_assistant_cloud_reported_state",
            }
            await self.record("prepared", record)
            try:
                await self._activate_climate(
                    entity_id,
                    requested,
                    context,
                )
                for attempt in range(4):
                    confirmed = self._climate_status(entity_id)
                    if (
                        confirmed.get("fresh")
                        and self._climate_state_matches(
                            confirmed,
                            requested,
                        )
                    ):
                        await self.record(
                            "verified",
                            {**record, "confirmed": confirmed},
                        )
                        return {
                            "changed": True,
                            "previous": previous,
                            "confirmed": confirmed,
                        }
                    if attempt < 3:
                        await asyncio.sleep(1)
                raise HomeAssistantError(
                    "Fresh cloud-reported climate state did not confirm "
                    "commissioning preparation."
                )
            except asyncio.CancelledError:
                LOGGER.error(
                    "Climate commissioning was cancelled after dispatch began; "
                    "its prepared journal record remains uncertain."
                )
                raise
            except (HomeAssistantError, TimeoutError, OSError) as error:
                LOGGER.error(
                    "Climate commissioning outcome is uncertain; read fresh "
                    "state before any retry or restoration."
                )
                try:
                    await self.record("uncertain", record)
                except OSError:
                    LOGGER.exception(
                        "Could not persist the uncertain climate outcome."
                    )
                raise HomeAssistantError(
                    "The climate commissioning command may have applied. Read "
                    "fresh state before any retry or restoration; no automatic "
                    "retry or rollback was performed."
                ) from error

    @staticmethod
    def _climate_state_matches(current: dict, expected: dict) -> bool:
        if current.get("identity") != expected.get("identity"):
            return False
        for key in (
            "state",
            "temperature",
            "temperature_unit",
            "fan_mode",
            "swing_mode",
            "swing_horizontal_mode",
        ):
            if current.get(key) != expected.get(key):
                return False
        return True

    def _validate_climate_restore(self, current: dict, requested: dict) -> None:
        mode = requested.get("state")
        if not isinstance(mode, str) or mode == "off":
            raise HomeAssistantError(
                "A climate restore needs the exact prior active HVAC mode."
            )
        if mode not in current.get("hvac_modes", ()):
            raise HomeAssistantError(
                "The prior climate HVAC mode is no longer supported."
            )
        if requested.get("temperature_unit") != current.get("temperature_unit"):
            raise HomeAssistantError(
                "The climate temperature unit changed; restoration is blocked."
            )
        temperature = requested.get("temperature")
        if temperature is not None:
            minimum = current.get("minimum_temperature")
            maximum = current.get("maximum_temperature")
            step = current.get("target_temperature_step") or 1
            if minimum is None or maximum is None:
                raise HomeAssistantError(
                    "The climate target-temperature capability is unavailable."
                )
            number(
                temperature,
                "Climate target temperature",
                minimum,
                maximum,
                step,
            )
        for key, capability_key, label in (
            ("fan_mode", "fan_modes", "fan mode"),
            ("swing_mode", "swing_modes", "swing mode"),
            (
                "swing_horizontal_mode",
                "swing_horizontal_modes",
                "horizontal swing mode",
            ),
        ):
            value = requested.get(key)
            if value is not None and value not in current.get(capability_key, ()):
                raise HomeAssistantError(
                    f"The prior climate {label} is no longer supported."
                )

    async def _call_climate(
        self,
        service: str,
        data: dict,
        context: Context | None,
    ) -> None:
        await self.hass.services.async_call(
            "climate",
            service,
            data,
            blocking=True,
            context=context,
        )

    async def control_climate(
        self,
        args: dict,
        context: Context | None,
    ) -> dict:
        entity_id = args.get("entity_id")
        operation = args.get("operation")
        expected = args.get("expected")
        if (
            not isinstance(entity_id, str)
            or operation not in ("turn_off", "restore")
            or not isinstance(expected, dict)
        ):
            raise HomeAssistantError(
                "Climate control needs an allowlisted entity, operation and expected state."
            )
        async with self.climate_lock, asyncio.timeout(30):
            previous = self._climate_status(entity_id)
            if not previous.get("available") or not previous.get("fresh"):
                raise HomeAssistantError(
                    previous.get("reason")
                    or "Fresh climate state is unavailable; no command was sent."
                )
            if not self._climate_state_matches(previous, expected):
                raise HomeAssistantError(
                    "The climate state changed; read it again before requesting control."
                )
            if operation == "turn_off" and previous.get("state") == "off":
                return {
                    "changed": False,
                    "previous": previous,
                    "confirmed": previous,
                }
            requested = args.get("restore_state")
            if operation == "restore":
                if previous.get("state") != "off" or not isinstance(requested, dict):
                    raise HomeAssistantError(
                        "Climate restoration requires breaker-owned off state and a prior snapshot."
                    )
                if previous.get("identity") != requested.get("identity"):
                    raise HomeAssistantError(
                        "The climate entity identity changed; restoration is blocked."
                    )
                self._validate_climate_restore(previous, requested)
            record = {
                "request_id": str(uuid4()),
                "device": "climate:" + entity_id,
                "previous": previous,
                "requested": (
                    {"state": "off"}
                    if operation == "turn_off"
                    else {
                        key: requested.get(key)
                        for key in (
                            "state",
                            "temperature",
                            "temperature_unit",
                            "fan_mode",
                            "swing_mode",
                            "swing_horizontal_mode",
                        )
                    }
                ),
                "confirmation_basis": "home_assistant_cloud_reported_state",
            }
            await self.record("prepared", record)
            try:
                if operation == "turn_off":
                    await self._call_climate(
                        "turn_off",
                        {"entity_id": entity_id},
                        context,
                    )
                else:
                    await self._activate_climate(
                        entity_id,
                        requested,
                        context,
                    )
                for attempt in range(4):
                    confirmed = self._climate_status(entity_id)
                    matched = (
                        confirmed.get("state") == "off"
                        if operation == "turn_off"
                        else self._climate_state_matches(confirmed, requested)
                    )
                    if confirmed.get("fresh") and matched:
                        await self.record(
                            "verified",
                            {**record, "confirmed": confirmed},
                        )
                        return {
                            "changed": True,
                            "previous": previous,
                            "confirmed": confirmed,
                        }
                    if attempt < 3:
                        await asyncio.sleep(1)
                raise HomeAssistantError(
                    "Fresh cloud-reported climate state did not confirm the request."
                )
            except asyncio.CancelledError:
                LOGGER.error(
                    "Climate request cancelled after dispatch began; its prepared "
                    "journal record remains uncertain."
                )
                raise
            except (HomeAssistantError, TimeoutError, OSError) as error:
                LOGGER.error(
                    "Climate command outcome is uncertain; read fresh state before "
                    "any retry or restoration."
                )
                try:
                    await self.record("uncertain", record)
                except OSError:
                    LOGGER.exception(
                        "Could not persist the uncertain climate outcome."
                    )
                raise HomeAssistantError(
                    "The climate command may have applied. Read fresh state before "
                    "any retry or restoration; no automatic retry or rollback was performed."
                ) from error

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
        from pyaltherma.controllers import AlthermaException

        try:
            async with self.daikin_lock, asyncio.timeout(30):
                _, api = self.daikin_api()
                heating, water = await self.daikin_observation(api)
                mode = await self.daikin_resource(
                    api, {"function/SpaceHeating"}, "UnitStatus/ControlModeState"
                )
                effective = await self.daikin_resource(
                    api, {"function/DomesticHotWaterTank", "function/DomesticHotWater"},
                    "Operation/TargetTemperature",
                )
                return {
                    "heating": heating, "hot_water": water, "control_mode": mode,
                    "effective_hot_water_target": effective,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "measurement_freshness": "Fresh adapter retrieval does not prove the age of every measurement.",
                }
        except (AlthermaException, aiohttp.ClientError, TimeoutError, OSError, ValueError, KeyError) as error:
            raise HomeAssistantError(
                "Fresh Daikin status is unavailable; no control command was sent."
            ) from error

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
    def __init__(self, hass, options, devices=None):
        super().__init__(hass=hass, id=CONTROL_API_ID, name="Home energy devices (guarded)")
        self.devices = devices or EnergyDevices(hass, options)

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
