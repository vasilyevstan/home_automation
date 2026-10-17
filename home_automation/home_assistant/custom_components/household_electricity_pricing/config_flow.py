"""Configure a private profile and an existing EE/EUR Nord Pool entry."""

from __future__ import annotations

from pathlib import Path

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import ConfigEntrySelector, EntitySelector

from ._vendor.home_automation.electricity_pricing.engine import PricingError, load_profile
from ._vendor.home_automation.household_consumption.client import ConsumptionError
from ._vendor.home_automation.home_assistant.calculator import load_credentials_file
from ._vendor.home_automation.home_assistant.pricing import profile_path
from .const import (
    CONF_DAIKIN_ENTRY, CONF_DHW_COAST_HOURS, CONF_DHW_EVENING_READY,
    CONF_DHW_FLOOR_TARGET, CONF_DHW_INITIAL_HEAT_MINUTES, CONF_DHW_MAX_LEASE_HOURS,
    CONF_DHW_MORNING_READY, CONF_DHW_NORMAL_TARGET, CONF_DHW_PREHEAT_TARGET,
    CONF_DHW_PRICE_DELTA, CONF_DHW_PRICE_PERCENT, CONF_DHW_SOFT_TARGET,
    CONF_ELERING_PATH, CONF_EXTREME_PRICE_CLIMATE_ENTITIES,
    CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES,
    CONF_NORDPOOL_ENTRY, CONF_PROFILE_PATH, CONF_SOLAR_ENTRY,
    DEFAULT_DHW_COAST_HOURS, DEFAULT_DHW_EVENING_READY, DEFAULT_DHW_FLOOR_TARGET,
    DEFAULT_DHW_INITIAL_HEAT_MINUTES, DEFAULT_DHW_MAX_LEASE_HOURS,
    DEFAULT_DHW_MORNING_READY, DEFAULT_DHW_NORMAL_TARGET, DEFAULT_DHW_PREHEAT_TARGET,
    DEFAULT_DHW_PRICE_DELTA, DEFAULT_DHW_PRICE_PERCENT, DEFAULT_DHW_SOFT_TARGET,
    DEFAULT_PROFILE_PATH, DOMAIN, NAME,
)
from .controls import number
from .dhw_optimizer import DhwPolicy
from .floor_heating_shadow import read_room_temperature_source


DATA_SCHEMA = probatio.Schema({
    probatio.Required(CONF_NORDPOOL_ENTRY): ConfigEntrySelector({"integration": "nordpool"}),
    probatio.Required(CONF_PROFILE_PATH, default=DEFAULT_PROFILE_PATH): str,
    probatio.Optional(CONF_ELERING_PATH): str,
    probatio.Optional(CONF_SOLAR_ENTRY): ConfigEntrySelector({"integration": "huawei_solar"}),
    probatio.Optional(CONF_DAIKIN_ENTRY): ConfigEntrySelector({"integration": "daikin_altherma"}),
})


class HouseholdPriceConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, user_input: dict) -> None:
        nordpool = self.hass.config_entries.async_get_entry(user_input[CONF_NORDPOOL_ENTRY])
        if nordpool is None or nordpool.domain != "nordpool":
            raise PricingError("Choose an existing Nord Pool configuration.")
        if nordpool.data.get("currency") != "EUR" or "EE" not in nordpool.data.get("areas", []):
            raise PricingError("Configure the selected Nord Pool entry for EUR and area EE first.")
        path = profile_path(Path(self.hass.config.config_dir), user_input[CONF_PROFILE_PATH])
        await self.hass.async_add_executor_job(load_profile, path)
        if user_input.get(CONF_ELERING_PATH):
            await self.hass.async_add_executor_job(
                load_credentials_file, Path(self.hass.config.config_dir), user_input[CONF_ELERING_PATH],
            )
        for key, domain in ((CONF_SOLAR_ENTRY, "huawei_solar"), (CONF_DAIKIN_ENTRY, "daikin_altherma")):
            if key in user_input:
                selected = self.hass.config_entries.async_get_entry(user_input[key])
                if selected is None or selected.domain != domain:
                    raise PricingError("Select an existing " + domain + " entry for optional guarded controls.")

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        reason = ""
        if user_input is not None:
            try:
                await self._validate(user_input)
            except (PricingError, ConsumptionError) as error:
                errors["base"] = "invalid_configuration"
                reason = str(error)
            else:
                return self.async_create_entry(title=NAME, data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors,
            description_placeholders={"error": reason},
        )

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors = {}
        reason = ""
        if user_input is not None:
            try:
                await self._validate(user_input)
            except (PricingError, ConsumptionError) as error:
                errors["base"] = "invalid_configuration"
                reason = str(error)
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(DATA_SCHEMA, user_input or entry.data),
            errors=errors, description_placeholders={"error": reason},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return HouseholdPriceOptionsFlow()


OPTIONS_SCHEMA = probatio.Schema({
    probatio.Required(CONF_DHW_MORNING_READY, default=DEFAULT_DHW_MORNING_READY): str,
    probatio.Required(CONF_DHW_EVENING_READY, default=DEFAULT_DHW_EVENING_READY): str,
    probatio.Required(CONF_DHW_SOFT_TARGET, default=DEFAULT_DHW_SOFT_TARGET): probatio.Any(int, float),
    probatio.Required(CONF_DHW_FLOOR_TARGET, default=DEFAULT_DHW_FLOOR_TARGET): probatio.Any(int, float),
    probatio.Required(CONF_DHW_NORMAL_TARGET, default=DEFAULT_DHW_NORMAL_TARGET): probatio.Any(int, float),
    probatio.Required(CONF_DHW_PREHEAT_TARGET, default=DEFAULT_DHW_PREHEAT_TARGET): probatio.Any(int, float),
    probatio.Required(CONF_DHW_MAX_LEASE_HOURS, default=DEFAULT_DHW_MAX_LEASE_HOURS): probatio.Any(int, float),
    probatio.Required(CONF_DHW_PRICE_DELTA, default=DEFAULT_DHW_PRICE_DELTA): probatio.Any(int, float),
    probatio.Required(CONF_DHW_PRICE_PERCENT, default=DEFAULT_DHW_PRICE_PERCENT): probatio.Any(int, float),
    probatio.Required(CONF_DHW_INITIAL_HEAT_MINUTES, default=DEFAULT_DHW_INITIAL_HEAT_MINUTES): probatio.Any(int, float),
    probatio.Required(CONF_DHW_COAST_HOURS, default=DEFAULT_DHW_COAST_HOURS): probatio.Any(int, float),
    probatio.Required(CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES): EntitySelector({
        "domain": ["sensor", "climate"],
        "multiple": True,
    }),
    probatio.Optional(CONF_EXTREME_PRICE_CLIMATE_ENTITIES): EntitySelector({
        "domain": "climate",
        "multiple": True,
    }),
})


class HouseholdPriceOptionsFlow(OptionsFlow):
    def _validate(self, user_input: dict) -> None:
        if not self.config_entry.data.get(CONF_DAIKIN_ENTRY):
            raise PricingError("Select a Daikin entry before configuring the DHW optimizer.")
        policy = DhwPolicy.from_options(user_input)
        runtime = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        devices = runtime.get("devices")
        if devices is None:
            raise PricingError("The configured Daikin runtime is unavailable.")
        _, api = devices.daikin_api()
        profile = api.water_tank_target_temp_config
        if not profile or profile.get("settable") is not True:
            raise PricingError("The native Daikin DHW target is not settable.")
        for label, value in (
            ("Soft-standby target", policy.soft_target),
            ("Comfort target", policy.floor_target),
            ("Normal target", policy.normal_target),
            ("Preheat target", policy.preheat_target),
        ):
            number(value, label, profile["minValue"], profile["maxValue"], profile["stepValue"])
        room_entities = user_input.get(CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES)
        if not isinstance(room_entities, list) or not room_entities:
            raise PricingError(
                "Select at least one occupied-room temperature entity for floor Shadow."
            )
        for entity_id in room_entities:
            value, _, reason = read_room_temperature_source(
                self.hass, entity_id, maximum_age=None
            )
            if value is None:
                raise PricingError(reason or f"{entity_id} is not a usable temperature.")
        climate_entities = user_input.get(CONF_EXTREME_PRICE_CLIMATE_ENTITIES)
        if climate_entities is not None:
            if not isinstance(climate_entities, list) or not climate_entities:
                raise PricingError(
                    "Select at least one climate entity for extreme-price protection."
                )
            if len(set(climate_entities)) != len(climate_entities):
                raise PricingError(
                    "Extreme-price climate entities must be unique."
                )
            for entity_id in climate_entities:
                state = self.hass.states.get(entity_id)
                if (
                    not isinstance(entity_id, str)
                    or not entity_id.startswith("climate.")
                    or state is None
                    or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
                ):
                    raise PricingError(
                        f"{entity_id} is not an available climate entity."
                    )

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        reason = ""
        if user_input is not None:
            try:
                self._validate(user_input)
            except (HomeAssistantError, PricingError) as error:
                errors["base"] = "invalid_optimizer"
                reason = str(error)
            else:
                return self.async_create_entry(data=user_input)
        suggested = self.add_suggested_values_to_schema(
            OPTIONS_SCHEMA, user_input or self.config_entry.options
        )
        return self.async_show_form(
            step_id="init",
            data_schema=suggested,
            errors=errors,
            description_placeholders={"error": reason},
        )
