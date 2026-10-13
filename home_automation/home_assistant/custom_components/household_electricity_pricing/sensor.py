"""Exact variable-cost rates, not reconstructed supplier invoices."""

from __future__ import annotations

from decimal import Decimal

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .coordinator import HouseholdPriceCoordinator


DESCRIPTIONS = (
    SensorEntityDescription(
        key="spot_eur_kwh", name="Spot price excluding VAT",
        native_unit_of_measurement="EUR/kWh", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
    ),
    SensorEntityDescription(
        key="import_eur_kwh", name="All-in import price",
        native_unit_of_measurement="EUR/kWh", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
    ),
    SensorEntityDescription(
        key="export_earnings_eur_kwh", name="Net export earnings",
        native_unit_of_measurement="EUR/kWh", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        HouseholdPriceSensor(entry.runtime_data, entry.entry_id, description)
        for description in DESCRIPTIONS
    )


class HouseholdPriceSensor(CoordinatorEntity[HouseholdPriceCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: HouseholdPriceCoordinator, entry_id: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = entry_id + "_" + description.key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name=NAME, manufacturer="home_automation"
        )

    @property
    def available(self) -> bool:
        return (
            super().available and self.coordinator.data is not None
            and self.coordinator.data.contains(dt_util.utcnow())
        )

    @property
    def native_value(self) -> Decimal | None:
        if not self.available:
            return None
        return Decimal(getattr(self.coordinator.data.current, self.entity_description.key))

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            **self.coordinator.data.attributes(),
            "exact_eur_kwh": (
                getattr(self.coordinator.data.current, self.entity_description.key)
                if self.available else None
            ),
            "market_errors": list(self.coordinator.market_errors.values()),
            "price_source": "published_nordpool_ee",
        }
