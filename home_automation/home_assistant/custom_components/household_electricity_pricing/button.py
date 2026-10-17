"""Explicit recovery actions for the domestic-hot-water optimizer."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EXTREME_PRICE_MODE_SHADOW, NAME


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    optimizer = runtime["optimizer"]
    entities = [
        DhwOptimizerResumeButton(optimizer, entry.entry_id),
        DhwOptimizerNormalButton(optimizer, entry.entry_id),
    ]
    protection = runtime.get("extreme_protection")
    if protection is not None:
        entities.extend(
            (
                ExtremePriceProtectionReplayButton(
                    protection,
                    entry.entry_id,
                ),
                ExtremePriceProtectionAlertButton(
                    protection,
                    entry.entry_id,
                ),
                ExtremePriceProtectionDaikinButton(
                    protection,
                    entry.entry_id,
                ),
                ExtremePriceProtectionClimateButton(
                    protection,
                    entry.entry_id,
                ),
                ExtremePriceProtectionResumeButton(
                    protection,
                    entry.entry_id,
                ),
            )
        )
    async_add_entities(entities)


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


class ExtremePriceProtectionButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        protection,
        entry_id: str,
        suffix: str,
    ) -> None:
        self.protection = protection
        self._attr_unique_id = (
            entry_id + "_extreme_price_protection_" + suffix
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.protection.async_add_listener(
                self.async_write_ha_state
            )
        )

    @property
    def available(self) -> bool:
        return (
            self.protection.mode == EXTREME_PRICE_MODE_SHADOW
            and not self.protection.state.get("fault")
            and not self.protection.state.get("ownership")
        )


class ExtremePriceProtectionReplayButton(ExtremePriceProtectionButton):
    _attr_name = "Verify extreme price deterministic replay"
    _attr_icon = "mdi:history"

    def __init__(self, protection, entry_id: str) -> None:
        super().__init__(protection, entry_id, "verify_replay")

    @property
    def available(self) -> bool:
        return (
            super().available
            and not self.protection.state.get("replay_verified")
        )

    async def async_press(self) -> None:
        await self.protection.async_verify_commissioning_replay()


class ExtremePriceProtectionAlertButton(ExtremePriceProtectionButton):
    _attr_name = "Verify extreme price alert delivery"
    _attr_icon = "mdi:bell-check-outline"

    def __init__(self, protection, entry_id: str) -> None:
        super().__init__(protection, entry_id, "verify_alert")

    @property
    def available(self) -> bool:
        return (
            super().available
            and not self.protection.state.get("alert_delivery_verified")
        )

    async def async_press(self) -> None:
        await self.protection.async_verify_alert_delivery()


class ExtremePriceProtectionDaikinButton(ExtremePriceProtectionButton):
    _attr_name = "Exercise extreme price Daikin circuits"
    _attr_icon = "mdi:heat-pump-outline"

    def __init__(self, protection, entry_id: str) -> None:
        super().__init__(protection, entry_id, "exercise_daikin")

    @property
    def available(self) -> bool:
        return (
            super().available
            and not self.protection.state.get("daikin_exercises_verified")
        )

    async def async_press(self) -> None:
        await self.protection.async_exercise_daikin()


class ExtremePriceProtectionClimateButton(ExtremePriceProtectionButton):
    _attr_name = "Exercise extreme price climate restoration"
    _attr_icon = "mdi:air-conditioner"

    def __init__(self, protection, entry_id: str) -> None:
        super().__init__(protection, entry_id, "exercise_climate")

    @property
    def available(self) -> bool:
        return (
            super().available
            and bool(self.protection.climate_entities)
            and not self.protection.state.get("climate_exercises_verified")
        )

    async def async_press(self) -> None:
        await self.protection.async_exercise_climate()


class ExtremePriceProtectionResumeButton(ExtremePriceProtectionButton):
    _attr_name = "Resume extreme price protection after review"
    _attr_icon = "mdi:shield-refresh"

    def __init__(self, protection, entry_id: str) -> None:
        super().__init__(protection, entry_id, "resume")

    @property
    def available(self) -> bool:
        return bool(self.protection.state.get("fault"))

    async def async_press(self) -> None:
        await self.protection.async_resume_after_review()
