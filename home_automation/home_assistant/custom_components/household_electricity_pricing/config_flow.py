"""Configure a private profile and an existing EE/EUR Nord Pool entry."""

from __future__ import annotations

from pathlib import Path

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import ConfigEntrySelector

from ._vendor.home_automation.electricity_pricing.engine import PricingError, load_profile
from ._vendor.home_automation.household_consumption.client import ConsumptionError
from ._vendor.home_automation.home_assistant.calculator import load_credentials_file
from ._vendor.home_automation.home_assistant.pricing import profile_path
from .const import (
    CONF_DAIKIN_ENTRY, CONF_ELERING_PATH, CONF_NORDPOOL_ENTRY, CONF_PROFILE_PATH, CONF_SOLAR_ENTRY,
    DEFAULT_PROFILE_PATH, DOMAIN, NAME,
)


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
