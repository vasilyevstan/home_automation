"""Runtime mode selection for the domestic-hot-water optimizer."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DHW_MODES, DOMAIN, NAME


MODE_LABELS = {
    "paused": "Paused",
    "shadow": "Shadow (no writes)",
    "safe_target_shift": "Safe target shift",
    "soft_standby": "Soft standby (30°C)",
}
LABEL_MODES = {label: mode for mode, label in MODE_LABELS.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    optimizer = hass.data[DOMAIN][entry.entry_id]["optimizer"]
    async_add_entities([DhwOptimizerModeSelect(optimizer, entry.entry_id)])


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
