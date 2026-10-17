"""Expose SDK-free household calculations as Home Assistant sensors."""

from __future__ import annotations

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
    CONF_SOLAR_ENTRY,
    DOMAIN,
    SERVICE_GET_PRICE_FORECAST,
    SERVICE_GET_PRICES,
)
from .calculation_api import CALCULATION_FIELDS
from .controls import EnergyDevices, HomeEnergyDeviceAPI
from .coordinator import HouseholdPriceCoordinator
from .dhw_optimizer import DhwOptimizer
from .floor_heating_shadow import FloorHeatingShadow
from .price_forecast import HouseholdPriceForecastCoordinator
from .price_api import HouseholdPriceAPI


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
    await forecast.async_load()
    devices = EnergyDevices(hass, entry.data)
    optimizer = (
        DhwOptimizer(hass, entry, coordinator, devices)
        if entry.data.get(CONF_DAIKIN_ENTRY) else None
    )
    floor_shadow = (
        FloorHeatingShadow(hass, entry, coordinator, devices, optimizer)
        if optimizer else None
    )
    runtime = {
        "coordinator": coordinator,
        "forecast": forecast,
        "devices": devices,
        "optimizer": optimizer,
        "floor_shadow": floor_shadow,
        "platforms": PLATFORMS if optimizer else ["sensor"],
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    if optimizer:
        await optimizer.async_start()
    if floor_shadow:
        await floor_shadow.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, runtime["platforms"])
    entry.async_on_unload(ha_llm.async_register_api(hass, HouseholdPriceAPI(hass, coordinator)))
    if entry.data.get(CONF_SOLAR_ENTRY) or entry.data.get(CONF_DAIKIN_ENTRY):
        entry.async_on_unload(
            ha_llm.async_register_api(
                hass, HomeEnergyDeviceAPI(hass, entry.data, devices=devices)
            )
        )
    entry.async_on_unload(
        async_track_utc_time_change(
            hass, coordinator.async_quarter_changed, minute=[0, 15, 30, 45], second=0,
        )
    )
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    forecast = runtime.get("forecast")
    if forecast:
        await forecast.async_shutdown()
    floor_shadow = runtime.get("floor_shadow")
    if floor_shadow:
        await floor_shadow.async_stop()
    optimizer = runtime.get("optimizer")
    if optimizer:
        await optimizer.async_stop()
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, runtime.get("platforms", ["sensor"])
    )
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
