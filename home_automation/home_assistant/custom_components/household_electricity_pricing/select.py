"""Runtime mode selection for the domestic-hot-water optimizer."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DHW_MODES,
    DOMAIN,
    EXTREME_PRICE_MODE_ARMED,
    EXTREME_PRICE_MODE_OFF,
    EXTREME_PRICE_MODE_SHADOW,
    FLOOR_MODE_ACTIVE,
    FLOOR_MODE_PAUSED,
    FLOOR_MODE_SHADOW,
    NAME,
    SOLAR_CURTAILMENT_MODE_ARMED,
    SOLAR_CURTAILMENT_MODE_OFF,
    SOLAR_CURTAILMENT_MODE_SHADOW,
)


MODE_LABELS = {
    "paused": "Paused",
    "shadow": "Shadow (no writes)",
    "readiness_pilot": "Readiness pilot (55→50°C)",
    "safe_target_shift": "Safe target shift",
    "soft_standby": "Soft standby (30°C)",
}
LABEL_MODES = {label: mode for mode, label in MODE_LABELS.items()}
EXTREME_MODE_LABELS = {
    EXTREME_PRICE_MODE_OFF: "Off",
    EXTREME_PRICE_MODE_SHADOW: "Shadow (no writes)",
    EXTREME_PRICE_MODE_ARMED: "Armed",
}
EXTREME_LABEL_MODES = {
    label: mode for mode, label in EXTREME_MODE_LABELS.items()
}
SOLAR_MODE_LABELS = {
    SOLAR_CURTAILMENT_MODE_OFF: "Off",
    SOLAR_CURTAILMENT_MODE_SHADOW: "Shadow (no writes)",
    SOLAR_CURTAILMENT_MODE_ARMED: "Armed",
}
SOLAR_LABEL_MODES = {
    label: mode for mode, label in SOLAR_MODE_LABELS.items()
}
FLOOR_MODE_LABELS = {
    FLOOR_MODE_PAUSED: "Paused",
    FLOOR_MODE_SHADOW: "Shadow (no writes)",
    FLOOR_MODE_ACTIVE: "Active price shift",
}
FLOOR_LABEL_MODES = {
    label: mode for mode, label in FLOOR_MODE_LABELS.items()
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities = []
    optimizer = runtime.get("optimizer")
    if optimizer is not None:
        entities.append(
            DhwOptimizerModeSelect(optimizer, entry.entry_id)
        )
    floor_controller = runtime.get("floor_shadow")
    if floor_controller is not None:
        entities.append(
            FloorHeatingModeSelect(
                floor_controller,
                entry.entry_id,
            )
        )
    protection = runtime.get("extreme_protection")
    if protection is not None:
        entities.append(
            ExtremePriceProtectionModeSelect(
                protection,
                entry.entry_id,
            )
        )
    solar_curtailment = runtime.get("solar_curtailment")
    if solar_curtailment is not None:
        entities.append(
            SolarCurtailmentModeSelect(
                solar_curtailment,
                entry.entry_id,
            )
        )
    async_add_entities(entities)


class DhwOptimizerModeSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Hot water optimizer mode"
    _attr_icon = "mdi:tune-variant"
    _attr_options = [MODE_LABELS[mode] for mode in DHW_MODES]

    def __init__(self, optimizer, entry_id: str) -> None:
        self.optimizer = optimizer
        self._attr_unique_id = entry_id + "_dhw_optimizer_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name=NAME, manufacturer="home_automation"
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.optimizer.async_add_listener(self.async_write_ha_state)
        )

    @property
    def current_option(self) -> str:
        return MODE_LABELS[self.optimizer.mode]

    async def async_select_option(self, option: str) -> None:
        await self.optimizer.async_set_mode(LABEL_MODES[option])


class ExtremePriceProtectionModeSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Extreme price protection mode"
    _attr_icon = "mdi:shield-lightning"

    def __init__(self, protection, entry_id: str) -> None:
        self.protection = protection
        self._attr_unique_id = (
            entry_id + "_extreme_price_protection_mode"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.protection.async_add_listener(self.async_write_ha_state)
        )

    @property
    def options(self) -> list[str]:
        return [
            EXTREME_MODE_LABELS[mode]
            for mode in self.protection.available_modes
        ]

    @property
    def current_option(self) -> str:
        return EXTREME_MODE_LABELS[self.protection.mode]

    async def async_select_option(self, option: str) -> None:
        await self.protection.async_set_mode(
            EXTREME_LABEL_MODES[option]
        )


class FloorHeatingModeSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Floor heating optimizer mode"
    _attr_icon = "mdi:heating-coil"
    _attr_options = list(FLOOR_MODE_LABELS.values())

    def __init__(self, controller, entry_id: str) -> None:
        self.controller = controller
        self._attr_unique_id = entry_id + "_floor_heating_optimizer_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.controller.async_add_listener(
                self.async_write_ha_state
            )
        )

    @property
    def current_option(self) -> str:
        return FLOOR_MODE_LABELS[self.controller.mode]

    async def async_select_option(self, option: str) -> None:
        await self.controller.async_set_mode(
            FLOOR_LABEL_MODES[option]
        )


class SolarCurtailmentModeSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Solar curtailment mode"
    _attr_icon = "mdi:solar-power-variant-outline"

    def __init__(self, controller, entry_id: str) -> None:
        self.controller = controller
        self._attr_unique_id = entry_id + "_solar_curtailment_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.controller.async_add_listener(
                self.async_write_ha_state
            )
        )

    @property
    def options(self) -> list[str]:
        return [
            SOLAR_MODE_LABELS[mode]
            for mode in self.controller.available_modes
        ]

    @property
    def current_option(self) -> str:
        return SOLAR_MODE_LABELS[self.controller.mode]

    async def async_select_option(self, option: str) -> None:
        await self.controller.async_set_mode(
            SOLAR_LABEL_MODES[option]
        )
