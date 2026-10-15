"""Explicit recovery actions for the domestic-hot-water optimizer."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    optimizer = hass.data[DOMAIN][entry.entry_id]["optimizer"]
    async_add_entities([
        DhwOptimizerResumeButton(optimizer, entry.entry_id),
        DhwOptimizerNormalButton(optimizer, entry.entry_id),
    ])


class DhwOptimizerButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, optimizer, entry_id: str, suffix: str) -> None:
        self.optimizer = optimizer
        self._attr_unique_id = entry_id + "_dhw_optimizer_" + suffix
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name=NAME, manufacturer="home_automation"
        )


class DhwOptimizerResumeButton(DhwOptimizerButton):
    _attr_name = "Resume hot water optimizer after review"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, optimizer, entry_id: str) -> None:
        super().__init__(optimizer, entry_id, "resume")

    async def async_press(self) -> None:
        await self.optimizer.async_resume()


class DhwOptimizerNormalButton(DhwOptimizerButton):
    _attr_name = "Return hot water to normal"
    _attr_icon = "mdi:restore"

    def __init__(self, optimizer, entry_id: str) -> None:
        super().__init__(optimizer, entry_id, "normal")

    async def async_press(self) -> None:
        await self.optimizer.async_return_to_normal()
