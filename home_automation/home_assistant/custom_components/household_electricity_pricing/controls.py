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
SOLAR_ELEVATED_CONFIGURATION = "enable_parameter_configuration"


class SolarPreDispatchError(HomeAssistantError):
    """Solar operation failed before a device command was dispatched."""


class SolarPostDispatchError(HomeAssistantError):
    """Solar operation failed after dispatch and has an uncertain outcome."""


class SolarDependencyNotReady(SolarPreDispatchError):
    """The selected Huawei runtime has not finished loading."""


class SolarExternalOverrideError(SolarPreDispatchError):
    """Fresh state changed after the caller prepared a solar write."""

    def __init__(self, message: str, status: dict) -> None:
        super().__init__(message)
        self.status = status


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
                LOGGER.exception(
                    "Climate commissioning outcome is uncertain; read fresh "
                    "state before any retry or restoration."
                )
                try:
                    await self.record(
                        "uncertain",
                        {
                            **record,
                            "error": (
                                type(error).__name__ + ": " + str(error)
                            ),
                        },
                    )
                except OSError:
                    LOGGER.exception(
                        "Could not persist the uncertain climate outcome."
                    )
                raise HomeAssistantError(
                    "The climate commissioning command may have applied. Read "
                    "fresh state before any retry or restoration; no automatic "
                    "retry or rollback was performed. Cause: "
                    + str(error)
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
                LOGGER.exception(
                    "Climate command outcome is uncertain; read fresh state before "
                    "any retry or restoration."
                )
                try:
                    await self.record(
                        "uncertain",
                        {
                            **record,
                            "error": (
                                type(error).__name__ + ": " + str(error)
                            ),
                        },
                    )
                except OSError:
                    LOGGER.exception(
                        "Could not persist the uncertain climate outcome."
                    )
                raise HomeAssistantError(
                    "The climate command may have applied. Read fresh state before "
                    "any retry or restoration; no automatic retry or rollback "
                    "was performed. Cause: "
                    + str(error)
                ) from error

    @staticmethod
    def _runtime_value(item, key):
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    def solar_device_data(self):
        entry = (
            self.hass.config_entries.async_get_entry(self.solar_entry)
            if self.solar_entry
            else None
        )
        if entry is None or entry.domain != "huawei_solar":
            raise HomeAssistantError(
                "The explicitly selected huawei_solar integration is "
                "unavailable."
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise SolarDependencyNotReady(
                "The selected Huawei integration is not loaded "
                f"({entry.state.value}); no inverter command was sent."
            )
        from huawei_solar import SUN2000Device

        runtime_data = entry.runtime_data
        device_datas = (
            runtime_data.get("device_datas")
            if isinstance(runtime_data, dict)
            else None
        )
        if not isinstance(device_datas, list):
            raise HomeAssistantError(
                "The selected Huawei entry has no usable device runtime."
            )
        matches = [
            item
            for item in device_datas
            if isinstance(self._runtime_value(item, "device"), SUN2000Device)
        ]
        if len(matches) != 1:
            raise HomeAssistantError(
                "Select a Huawei entry with exactly one verified SUN2000 inverter."
            )
        return entry, matches[0], self._runtime_value(matches[0], "device")

    def solar_device(self):
        return self.solar_device_data()[2]

    def solar_write_capability(self) -> tuple[bool, str]:
        try:
            entry, device_data, device = self.solar_device_data()
        except HomeAssistantError as error:
            return False, str(error)
        if entry.data.get(SOLAR_ELEVATED_CONFIGURATION) is not True:
            return (
                False,
                "Huawei elevated parameter configuration is disabled.",
            )
        coordinator = self._runtime_value(
            device_data, "configuration_update_coordinator"
        )
        if coordinator is None or not callable(
            getattr(coordinator, "async_request_refresh", None)
        ):
            return (
                False,
                "The Huawei configuration coordinator is unavailable.",
            )
        if not callable(getattr(device.client, "set", None)):
            return False, "The Huawei runtime has no usable write operation."
        if entry.data.get("username") and not callable(
            getattr(device, "ensure_logged_in", None)
        ):
            return (
                False,
                "The authenticated Huawei runtime cannot refresh its login.",
            )
        return (
            True,
            "Elevated parameter configuration and the native configuration "
            "coordinator are available.",
        )

    def solar_write_available(self) -> bool:
        return self.solar_write_capability()[0]

    def _require_solar_write_capability(
        self, entry, device_data, device
    ) -> str:
        if entry.data.get(SOLAR_ELEVATED_CONFIGURATION) is not True:
            raise SolarPreDispatchError(
                "Huawei elevated parameter configuration is disabled; no "
                "inverter command was sent."
            )
        coordinator = self._runtime_value(
            device_data, "configuration_update_coordinator"
        )
        if coordinator is None or not callable(
            getattr(coordinator, "async_request_refresh", None)
        ):
            raise SolarPreDispatchError(
                "The Huawei configuration coordinator is unavailable; no "
                "inverter command was sent."
            )
        if not callable(getattr(device.client, "set", None)):
            raise SolarPreDispatchError(
                "The Huawei runtime has no usable write operation; no inverter "
                "command was sent."
            )
        if entry.data.get("username") and not callable(
            getattr(device, "ensure_logged_in", None)
        ):
            raise SolarPreDispatchError(
                "The authenticated Huawei runtime cannot refresh its login; no "
                "inverter command was sent."
            )
        return (
            "elevated_parameter_configuration_with_native_configuration_"
            "coordinator"
        )

    async def solar_limit(
        self,
        device,
        *,
        config_entry_id: str,
        write_enabled: bool,
        write_permission_basis: str,
    ):
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
        active_power = number(
            (await device.client.get(rn.ACTIVE_POWER)).value,
            "Inverter AC active power",
            -1e9,
            1e9,
            1,
        )
        if (mode, command) == (0, 40125):
            configured = number(
                (await device.client.get(rn.ACTIVE_POWER_PERCENTAGE_DERATING)).value,
                "Configured percentage", 0, 100, 0.1,
            )
            actual = number(active / 10, "Active percentage", 0, 100, 0.1)
            label = "percent"
            minimum, limit_maximum, step = 0.0, 100.0, 0.1
        elif (mode, command) == (1, 40126):
            configured = number(
                (await device.client.get(rn.ACTIVE_POWER_FIXED_VALUE_DERATING)).value,
                "Configured watt limit", 0, maximum, 1,
            )
            actual = number(active, "Active watt limit", 0, maximum, 1)
            label = "watts"
            minimum, limit_maximum, step = 0.0, maximum, 1.0
        else:
            raise HomeAssistantError("Another inverter control mode is active; it will not be replaced.")
        observed_at = datetime.now(timezone.utc).isoformat()
        return {
            "config_entry_id": config_entry_id,
            "serial": serial,
            "mode": label,
            "configured": configured,
            "active": actual,
            "pmax_w": maximum,
            "active_power_w": active_power,
            "active_power_observed_at": observed_at,
            "limit_minimum": minimum,
            "limit_maximum": limit_maximum,
            "limit_step": step,
            "write_enabled": write_enabled,
            "write_permission_basis": write_permission_basis,
            "observed_at": observed_at,
        }

    async def get_solar_limit(self):
        from huawei_solar import HuaweiSolarException

        try:
            async with self.solar_session() as (
                entry,
                _device_data,
                device,
            ):
                write_enabled, basis = self.solar_write_capability()
                return await self.solar_limit(
                    device,
                    config_entry_id=entry.entry_id,
                    write_enabled=write_enabled,
                    write_permission_basis=basis,
                )
        except (
            HomeAssistantError,
            HuaweiSolarException,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
        ) as error:
            if isinstance(error, SolarPreDispatchError):
                raise
            raise SolarPreDispatchError(
                "Fresh inverter limit and active power are unavailable; no "
                "control command was sent. Cause: "
                + str(error)
            ) from error

    async def get_solar_status(self):
        """Return one fresh identity, capability, limit, and active-power read."""
        return await self.get_solar_limit()

    @asynccontextmanager
    async def solar_session(self, *, require_write: bool = False):
        async with asyncio.timeout(30), self.solar_lock:
            entry, device_data, device = self.solar_device_data()
            if require_write:
                self._require_solar_write_capability(
                    entry, device_data, device
                )
            # Native polling uses this lock. Native service writes do not, so
            # set_solar_limit also performs an optimistic final exact read.
            async with device.update_lock:
                yield entry, device_data, device

    async def set_solar_limit(self, args, *, automatic: bool = False):
        from huawei_solar import HuaweiSolarException, register_names as rn

        dispatched = False
        record = None
        try:
            async with self.solar_session(require_write=True) as (
                entry,
                _device_data,
                device,
            ):
                permission_basis = (
                    "elevated_parameter_configuration_with_native_"
                    "configuration_coordinator"
                )
                previous = await self.solar_limit(
                    device,
                    config_entry_id=entry.entry_id,
                    write_enabled=True,
                    write_permission_basis=permission_basis,
                )
                expected_entry_id = args.get("expected_config_entry_id")
                expected_serial = args.get("expected_serial")
                expected_pmax = number(
                    args.get("expected_pmax_w"),
                    "Expected Pmax",
                    1,
                    1e9,
                    1,
                )
                if (
                    expected_entry_id != previous["config_entry_id"]
                    or expected_serial != previous["serial"]
                    or expected_pmax != previous["pmax_w"]
                ):
                    raise SolarPreDispatchError(
                        "The inverter config entry, serial, or Pmax changed; "
                        "read it again before requesting a change. No inverter "
                        "command was sent."
                    )
                expected = number(
                    args["expected_value"], "Expected limit", 0,
                    (
                        100
                        if previous["mode"] == "percent"
                        else previous["pmax_w"]
                    ),
                    0.1 if previous["mode"] == "percent" else 1,
                )
                expected_active = number(
                    args["expected_active_value"], "Expected active limit", 0,
                    (
                        100
                        if previous["mode"] == "percent"
                        else previous["pmax_w"]
                    ),
                    0.1 if previous["mode"] == "percent" else 1,
                )
                if (
                    args["expected_mode"] != previous["mode"]
                    or expected != previous["configured"]
                    or expected_active != previous["active"]
                ):
                    raise SolarPreDispatchError(
                        "The inverter limit changed; read it again before "
                        "requesting a change. No inverter command was sent."
                    )
                if automatic and previous["configured"] != previous["active"]:
                    raise SolarPreDispatchError(
                        "Automatic inverter control requires matching "
                        "configured and active limits; no inverter command was "
                        "sent."
                    )
                mode = args["mode"]
                if mode not in ("percent", "watts"):
                    raise SolarPreDispatchError(
                        "Choose percent or watts; no inverter command was sent."
                    )
                if automatic and mode != previous["mode"]:
                    raise SolarPreDispatchError(
                        "Automatic inverter control cannot change the active "
                        "limit mode; no inverter command was sent."
                    )
                if (
                    mode != previous["mode"]
                    and args.get("allow_mode_change") is not True
                ):
                    raise SolarPreDispatchError(
                        "Changing the limit mode needs explicit "
                        "allow_mode_change=true; no inverter command was sent."
                    )
                requested = number(
                    args["value"], "Requested limit", 0,
                    100 if mode == "percent" else previous["pmax_w"],
                    0.1 if mode == "percent" else 1,
                )
                if (
                    mode == previous["mode"]
                    and requested
                    == previous["configured"]
                    == previous["active"]
                ):
                    return {
                        "changed": False,
                        "phase": "no_change",
                        "previous": previous,
                        "confirmed": previous,
                        "audit_persisted": True,
                        "audit_error": None,
                    }
                if entry.data.get("username"):
                    if (
                        not await device.ensure_logged_in()
                        or not await device.client.heartbeat()
                    ):
                        raise SolarPreDispatchError(
                            "Configured inverter authentication failed; no cap "
                            "write was sent."
                        )
                    refreshed = await self.solar_limit(
                        device,
                        config_entry_id=entry.entry_id,
                        write_enabled=True,
                        write_permission_basis=permission_basis,
                    )
                    if any(
                        refreshed[key] != previous[key]
                        for key in (
                            "config_entry_id",
                            "serial",
                            "mode",
                            "configured",
                            "active",
                            "pmax_w",
                            "active_power_w",
                        )
                    ):
                        raise SolarPreDispatchError(
                            "The inverter identity, Pmax, limit, or active "
                            "power changed "
                            "during authentication; read it again. No inverter "
                            "command was sent."
                        )
                record = {
                    "request_id": str(uuid4()),
                    "device": "solar",
                    "identity": {
                        "config_entry_id": previous["config_entry_id"],
                        "serial": previous["serial"],
                        "pmax_w": previous["pmax_w"],
                    },
                    "expected": {
                        "config_entry_id": expected_entry_id,
                        "serial": expected_serial,
                        "pmax_w": expected_pmax,
                        "mode": args["expected_mode"],
                        "configured": expected,
                        "active": expected_active,
                    },
                    "previous": previous,
                    "requested": {"mode": mode, "value": requested},
                    "automatic": automatic,
                }
                await self.record("prepared", record)
                immediate = await self.solar_limit(
                    device,
                    config_entry_id=entry.entry_id,
                    write_enabled=True,
                    write_permission_basis=permission_basis,
                )
                exact_keys = (
                    "config_entry_id",
                    "serial",
                    "mode",
                    "configured",
                    "active",
                    "pmax_w",
                    "active_power_w",
                )
                if any(
                    immediate[key] != previous[key]
                    for key in exact_keys
                ):
                    try:
                        await self.record(
                            "not_dispatched",
                            {
                                **record,
                                "reason": (
                                    "The exact inverter state changed after "
                                    "prepared persistence."
                                ),
                                "observed": immediate,
                            },
                        )
                    except OSError:
                        LOGGER.exception(
                            "The solar command was not dispatched, and its "
                            "not-dispatched audit record could not be "
                            "persisted."
                        )
                    raise SolarExternalOverrideError(
                        "The inverter identity, Pmax, limit, or active power "
                        "changed after the prepared audit record. This is "
                        "treated as an external override and no inverter "
                        "command was sent.",
                        immediate,
                    )
                register = (
                    rn.ACTIVE_POWER_PERCENTAGE_DERATING
                    if mode == "percent"
                    else rn.ACTIVE_POWER_FIXED_VALUE_DERATING
                )
                dispatched = True
                if not await device.client.set(register, requested):
                    raise HomeAssistantError(
                        "The inverter did not acknowledge the requested limit."
                    )
                for attempt in range(4):
                    current = await self.solar_limit(
                        device,
                        config_entry_id=entry.entry_id,
                        write_enabled=True,
                        write_permission_basis=permission_basis,
                    )
                    if (
                        current["config_entry_id"] == expected_entry_id
                        and current["serial"] == expected_serial
                        and current["pmax_w"] == expected_pmax
                        and current["mode"] == mode
                        and current["configured"] == requested
                        and current["active"] == requested
                    ):
                        audit_persisted = True
                        audit_error = None
                        try:
                            await self.record(
                                "verified",
                                {**record, "confirmed": current},
                            )
                        except OSError as error:
                            audit_persisted = False
                            audit_error = (
                                type(error).__name__ + ": " + str(error)
                            )
                            LOGGER.exception(
                                "The inverter limit was exactly confirmed, but "
                                "the verified audit record could not be "
                                "persisted."
                            )
                        return {
                            "changed": True,
                            "phase": "verified",
                            "previous": previous,
                            "confirmed": current,
                            "audit_persisted": audit_persisted,
                            "audit_error": audit_error,
                        }
                    if attempt < 3:
                        await asyncio.sleep(0.5)
                raise HomeAssistantError(
                    "The requested configured and active limits were not both "
                    "confirmed."
                )
        except asyncio.CancelledError:
            if dispatched:
                LOGGER.error("Solar request cancelled after dispatch began; its prepared journal record remains uncertain.")
            raise
        except SolarPostDispatchError:
            raise
        except (
            HomeAssistantError,
            HuaweiSolarException,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
        ) as error:
            if not dispatched:
                if isinstance(error, SolarPreDispatchError):
                    raise
                raise SolarPreDispatchError(
                    "The solar operation failed before dispatch; no inverter "
                    "command was sent. Cause: "
                    + str(error)
                ) from error
            LOGGER.error(
                "Solar command outcome is uncertain; read the current limit "
                "before further action."
            )
            audit_error = None
            if record is not None:
                try:
                    await self.record(
                        "uncertain",
                        {
                            **record,
                            "error": type(error).__name__ + ": " + str(error),
                        },
                    )
                except OSError as journal_error:
                    audit_error = (
                        type(journal_error).__name__ + ": " + str(journal_error)
                    )
                    LOGGER.exception("Could not persist the uncertain solar outcome.")
            message = (
                "The inverter command may have applied. Read its limit before "
                "any controller-level replay, inversion, or restoration. This "
                "controller dispatched one write and did not replay, invert, or "
                "roll it back; the transport's internal retry behavior is not "
                "asserted."
            )
            if audit_error:
                message += " The uncertain audit record also failed: " + audit_error
            raise SolarPostDispatchError(message) from error

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
                if not isinstance(profile, list) or requested not in profile:
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
    description = "Only for an explicit user request. Requires elevated Huawei parameter configuration plus expected config-entry, serial, Pmax, configured and active values, native bounds and exact fresh read-back. No setup permission-test write is performed. Only failures after dispatch are reported as possibly applied."
    integration = DOMAIN
    annotations = ToolAnnotations(read_only=False, destructive=False, idempotent=False, open_world=False)
    parameters = probatio.Schema({
        probatio.Required("mode"): probatio.In(("percent", "watts")),
        probatio.Required("value"): probatio.Any(int, float),
        probatio.Required("expected_mode"): probatio.In(("percent", "watts")),
        probatio.Required("expected_value"): probatio.Any(int, float),
        probatio.Required("expected_active_value"): probatio.Any(int, float),
        probatio.Required("expected_config_entry_id"): str,
        probatio.Required("expected_serial"): str,
        probatio.Required("expected_pmax_w"): probatio.Any(int, float),
        probatio.Optional("allow_mode_change", default=False): bool,
    })

    def __init__(self, devices):
        self.devices = devices

    async def async_call(self, hass, tool_input, llm_context):
        return ToolResult(
            data=await self.devices.set_solar_limit(tool_input.tool_args)
        )


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
            tools.append(SolarLimitRead(self.devices))
            if self.devices.solar_write_available():
                tools.append(SolarLimitWrite(self.devices))
        if self.devices.daikin_entry:
            tools.extend((DaikinRead(self.devices), DaikinWrite(self.devices)))
        return llm.APIInstance(
            api=self, llm_context=llm_context, tools=tools,
            api_prompt="Device writes require an explicit user request. Never optimize, toggle, probe write permission, or change settings for a status/price question. Only a post-dispatch error means a command may have applied. Read state instead of controller-level replay, inversion, or rollback; no claim is made about retries internal to the underlying transport.",
        )
