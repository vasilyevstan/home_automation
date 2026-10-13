"""Expose SDK-free household calculations as Home Assistant sensors."""

from __future__ import annotations

import probatio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv, llm as ha_llm, service
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.selector import ConfigEntrySelector
from homeassistant.helpers.typing import ConfigType

from .const import CONF_DAIKIN_ENTRY, CONF_SOLAR_ENTRY, DOMAIN, SERVICE_GET_PRICES
from .calculation_api import CALCULATION_FIELDS
from .controls import HomeEnergyDeviceAPI
from .coordinator import HouseholdPriceCoordinator
from .price_api import HouseholdPriceAPI


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
GET_PRICES_SCHEMA = probatio.Schema({
    probatio.Required("config_entry"): ConfigEntrySelector({"integration": DOMAIN}),
    probatio.Optional("period", default="today"): probatio.In(("today", "tomorrow")),
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
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    entry.async_on_unload(ha_llm.async_register_api(hass, HouseholdPriceAPI(hass, coordinator)))
    if entry.data.get(CONF_SOLAR_ENTRY) or entry.data.get(CONF_DAIKIN_ENTRY):
        entry.async_on_unload(ha_llm.async_register_api(hass, HomeEnergyDeviceAPI(hass, entry.data)))
    entry.async_on_unload(
        async_track_utc_time_change(
            hass, coordinator.async_quarter_changed, minute=[0, 15, 30, 45], second=0,
        )
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, ["sensor"])
