"""Expose SDK-free household calculations as Home Assistant sensors."""

from __future__ import annotations

import asyncio
import inspect
import logging

import probatio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, llm as ha_llm, service
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.selector import ConfigEntrySelector
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DAIKIN_ENTRY,
    CONF_EXTREME_PRICE_CLIMATE_ENTITIES,
    CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES,
    CONF_SOLAR_ENTRY,
    DOMAIN,
    SERVICE_GET_PRICE_FORECAST,
    SERVICE_GET_PRICES,
)
from .calculation_api import CALCULATION_FIELDS
from .controls import EnergyDevices, HomeEnergyDeviceAPI
from .coordinator import HouseholdPriceCoordinator
from .dhw_optimizer import DhwOptimizer
from .extreme_price_protection import ExtremePriceProtection
from .floor_heating_shadow import FloorHeatingShadow
from .price_forecast import HouseholdPriceForecastCoordinator
from .price_api import HouseholdPriceAPI
from .solar_curtailment import SolarCurtailment


LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = ["sensor", "select", "button"]
GET_PRICES_SCHEMA = probatio.Schema({
    probatio.Required("config_entry"): ConfigEntrySelector({"integration": DOMAIN}),
    probatio.Optional("period", default="today"): probatio.In(("today", "tomorrow")),
})
GET_PRICE_FORECAST_SCHEMA = probatio.Schema({
    probatio.Required("config_entry"): ConfigEntrySelector({"integration": DOMAIN}),
    probatio.Optional("view", default="current"): probatio.In(("current", "archive")),
    probatio.Optional("history_days", default=7): probatio.All(
        int, probatio.Range(min=1, max=90)
    ),
    probatio.Optional("horizon_days", default=1): probatio.All(
        int, probatio.Range(min=1, max=7)
    ),
    probatio.Optional("include_future_days", default=0): probatio.All(
        int, probatio.Range(min=0, max=7)
    ),
})


async def _async_cleanup_runtime(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: dict,
    *,
    remove_data: bool = True,
) -> None:
    """Idempotently stop every runtime started for one config entry."""
    async with runtime["_cleanup_lock"]:
        cancelled = False
        if not runtime.get("_callbacks_cleaned"):
            for remove in reversed(runtime["_cleanup_callbacks"]):
                try:
                    result = remove()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    LOGGER.exception(
                        "Could not remove a config-entry callback during "
                        "cleanup"
                    )
            runtime["_cleanup_callbacks"].clear()
            runtime["_callbacks_cleaned"] = True
        if not runtime.get("_runtimes_stopped"):
            for name, stop in reversed(runtime["_started_runtimes"]):
                try:
                    await stop()
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    LOGGER.exception(
                        "Could not stop the %s runtime during cleanup",
                        name,
                    )
            runtime["_started_runtimes"].clear()
            runtime["_runtimes_stopped"] = True
        if remove_data:
            domain_data = hass.data.get(DOMAIN, {})
            if domain_data.get(entry.entry_id) is runtime:
                domain_data.pop(entry.entry_id, None)
            runtime["_data_removed"] = True
        if cancelled:
            raise asyncio.CancelledError


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def get_prices(call: ServiceCall) -> dict:
        entry = service.async_get_config_entry(hass, DOMAIN, call.data["config_entry"])
        coordinator: HouseholdPriceCoordinator = entry.runtime_data
        return await coordinator.async_price_response(call.data["period"])

    hass.services.async_register(
        DOMAIN, SERVICE_GET_PRICES, get_prices,
        schema=GET_PRICES_SCHEMA, supports_response=SupportsResponse.ONLY,
    )

    async def get_price_forecast(call: ServiceCall) -> dict:
        entry = service.async_get_config_entry(
            hass, DOMAIN, call.data["config_entry"]
        )
        runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        forecast = runtime.get("forecast")
        if forecast is None:
            raise ServiceValidationError(
                "The household price forecast runtime is unavailable."
            )
        return await forecast.async_response(
            call.data["view"],
            call.data["history_days"],
            call.data["horizon_days"],
            call.data["include_future_days"],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PRICE_FORECAST,
        get_price_forecast,
        schema=GET_PRICE_FORECAST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    for action, fields in CALCULATION_FIELDS.items():
        async def calculate(call: ServiceCall) -> dict:
            entry = service.async_get_config_entry(hass, DOMAIN, call.data["config_entry"])
            coordinator: HouseholdPriceCoordinator = entry.runtime_data
            arguments = {key: value for key, value in call.data.items() if key != "config_entry"}
            return await coordinator.async_calculate_response(call.service, arguments)

        hass.services.async_register(
            DOMAIN, action, calculate,
            schema=probatio.Schema({
                probatio.Required("config_entry"): ConfigEntrySelector({"integration": DOMAIN}),
                **fields,
            }),
            supports_response=SupportsResponse.ONLY,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = HouseholdPriceCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    forecast = HouseholdPriceForecastCoordinator(hass, entry, coordinator)
    configured_climates = entry.options.get(
        CONF_EXTREME_PRICE_CLIMATE_ENTITIES
    )
    if not configured_climates:
        configured_climates = [
            entity_id
            for entity_id in entry.options.get(
                CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES,
                (),
            )
            if isinstance(entity_id, str)
            and entity_id.startswith("climate.")
        ]
    devices = EnergyDevices(
        hass,
        {
            **entry.data,
            CONF_EXTREME_PRICE_CLIMATE_ENTITIES: configured_climates,
        },
    )
    optimizer = (
        DhwOptimizer(hass, entry, coordinator, devices)
        if entry.data.get(CONF_DAIKIN_ENTRY) else None
    )
    floor_shadow = (
        FloorHeatingShadow(hass, entry, coordinator, devices, optimizer)
        if optimizer else None
    )
    extreme_protection = (
        ExtremePriceProtection(
            hass,
            entry,
            coordinator,
            devices,
            optimizer,
            floor_shadow,
        )
        if optimizer else None
    )
    if optimizer is not None and extreme_protection is not None:
        optimizer.set_extreme_protection(extreme_protection)
    if floor_shadow is not None and extreme_protection is not None:
        floor_shadow.set_extreme_protection(extreme_protection)
    solar_curtailment = (
        SolarCurtailment(hass, entry, coordinator, devices)
        if entry.data.get(CONF_SOLAR_ENTRY)
        else None
    )
    runtime = {
        "coordinator": coordinator,
        "forecast": forecast,
        "devices": devices,
        "optimizer": optimizer,
        "floor_shadow": floor_shadow,
        "extreme_protection": extreme_protection,
        "solar_curtailment": solar_curtailment,
        "platforms": (
            PLATFORMS
            if optimizer or solar_curtailment
            else ["sensor"]
        ),
        "_cleanup_lock": asyncio.Lock(),
        "_cleanup_callbacks": [],
        "_started_runtimes": [],
        "_callbacks_cleaned": False,
        "_runtimes_stopped": False,
        "_data_removed": False,
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    entry.async_on_unload(
        lambda: _async_cleanup_runtime(hass, entry, runtime)
    )
    try:
        runtime["_started_runtimes"].append(
            ("price coordinator", coordinator.async_shutdown)
        )
        runtime["_started_runtimes"].append(
            ("price forecast", forecast.async_shutdown)
        )
        await forecast.async_load()
        for name, component in (
            ("extreme-price protection", extreme_protection),
            ("DHW optimizer", optimizer),
            ("floor-heating shadow", floor_shadow),
            ("solar curtailment", solar_curtailment),
        ):
            if component is None:
                continue
            runtime["_started_runtimes"].append(
                (name, component.async_stop)
            )
            await component.async_start()
        await hass.config_entries.async_forward_entry_setups(
            entry, runtime["platforms"]
        )
        runtime["_cleanup_callbacks"].append(
            ha_llm.async_register_api(
                hass, HouseholdPriceAPI(hass, coordinator)
            )
        )
        if entry.data.get(CONF_SOLAR_ENTRY) or entry.data.get(
            CONF_DAIKIN_ENTRY
        ):
            runtime["_cleanup_callbacks"].append(
                ha_llm.async_register_api(
                    hass,
                    HomeEnergyDeviceAPI(
                        hass, entry.data, devices=devices
                    ),
                )
            )
        runtime["_cleanup_callbacks"].append(
            async_track_utc_time_change(
                hass,
                coordinator.async_quarter_changed,
                minute=[0, 15, 30, 45],
                second=0,
            )
        )
        runtime["_cleanup_callbacks"].append(
            entry.add_update_listener(async_reload_entry)
        )
    except BaseException:
        try:
            await hass.config_entries.async_unload_platforms(
                entry, runtime["platforms"]
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception(
                "Could not unload partially forwarded pricing platforms"
            )
        await _async_cleanup_runtime(hass, entry, runtime)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, runtime.get("platforms", ["sensor"])
    )
    if unloaded and runtime:
        await _async_cleanup_runtime(hass, entry, runtime)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
