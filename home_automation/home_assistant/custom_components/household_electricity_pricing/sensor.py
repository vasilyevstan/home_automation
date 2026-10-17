"""Exact variable-cost rates, not reconstructed supplier invoices."""

from __future__ import annotations

from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .coordinator import HouseholdPriceCoordinator
from .price_forecast import HouseholdPriceForecastCoordinator


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
    entities = [
        HouseholdPriceSensor(entry.runtime_data, entry.entry_id, description)
        for description in DESCRIPTIONS
    ]
    forecast = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("forecast")
    if forecast:
        entities.append(PriceForecastSensor(forecast, entry.entry_id))
    optimizer = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("optimizer")
    if optimizer:
        entities.append(DhwOptimizerSensor(optimizer, entry.entry_id))
        entities.append(DhwOptimizationOpportunitySensor(optimizer, entry.entry_id))
    floor_shadow = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get(
        "floor_shadow"
    )
    if floor_shadow:
        entities.append(FloorHeatingShadowSensor(floor_shadow, entry.entry_id))
        entities.append(FloorRoomTemperatureSensor(floor_shadow, entry.entry_id))
        entities.append(
            FloorHeatingOptimizationOpportunitySensor(
                floor_shadow, entry.entry_id
            )
        )
    extreme_protection = hass.data.get(DOMAIN, {}).get(
        entry.entry_id, {}
    ).get("extreme_protection")
    if extreme_protection:
        entities.append(
            ExtremePriceProtectionSensor(
                extreme_protection,
                entry.entry_id,
            )
        )
    async_add_entities(entities)


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
        exact = getattr(self.coordinator.data.current, self.entity_description.key) if self.available else None
        return {
            **self.coordinator.data.attributes(),
            "exact_eur_kwh": exact,
            # InfluxDB converts numeric strings to floats; this companion keeps the decimal text.
            "exact_price_text": exact + " EUR/kWh" if exact is not None else None,
            "market_errors": list(self.coordinator.market_errors.values()),
            "price_source": "published_nordpool_ee",
        }


class PriceForecastSensor(
    CoordinatorEntity[HouseholdPriceForecastCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "Seven-day price forecast"
    _attr_icon = "mdi:chart-bell-curve"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 5

    def __init__(
        self,
        coordinator: HouseholdPriceForecastCoordinator,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry_id + "_seven_day_price_forecast"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.data is None:
            self.coordinator.hass.async_create_task(
                self.coordinator.async_request_refresh()
            )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.next_prediction is not None
        )

    @property
    def native_value(self) -> Decimal | None:
        if not self.available:
            return None
        return self.coordinator.data.next_prediction

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {
                "refresh_status": (
                    "failed" if self.coordinator.refresh_error else "pending"
                ),
                "refresh_error": self.coordinator.refresh_error,
            }
        return {
            **self.coordinator.data.attributes,
            "refresh_status": (
                "failed" if self.coordinator.refresh_error else "ok"
            ),
            "refresh_error": self.coordinator.refresh_error,
        }


class DhwOptimizerSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Hot water optimizer"
    _attr_icon = "mdi:water-boiler-auto"

    def __init__(self, optimizer, entry_id: str) -> None:
        self.optimizer = optimizer
        self._attr_unique_id = entry_id + "_dhw_optimizer"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name=NAME, manufacturer="home_automation"
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.optimizer.async_add_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> str:
        return self.optimizer.decision.state if self.optimizer.decision else "starting"

    @property
    def extra_state_attributes(self) -> dict:
        return self.optimizer.attributes


class OptimizationOpportunitySensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:piggy-bank-outline"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, controller, entry_id: str, unique_suffix: str) -> None:
        self.controller = controller
        self._attr_unique_id = entry_id + "_" + unique_suffix
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.controller.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self.controller.optimization_value is not None

    @property
    def native_value(self) -> Decimal | None:
        return self.controller.optimization_value

    @property
    def extra_state_attributes(self) -> dict:
        return self.controller.optimization_value_attributes


class DhwOptimizationOpportunitySensor(OptimizationOpportunitySensor):
    _attr_name = "Hot water price opportunity"

    def __init__(self, optimizer, entry_id: str) -> None:
        super().__init__(optimizer, entry_id, "dhw_price_opportunity")


class FloorHeatingShadowSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Floor heating shadow"
    _attr_icon = "mdi:heating-coil"

    def __init__(self, shadow, entry_id: str) -> None:
        self.shadow = shadow
        self._attr_unique_id = entry_id + "_floor_heating_shadow"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name=NAME, manufacturer="home_automation"
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.shadow.async_add_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> str:
        return self.shadow.decision.state if self.shadow.decision else "starting"

    @property
    def extra_state_attributes(self) -> dict:
        return self.shadow.attributes


class FloorHeatingOptimizationOpportunitySensor(OptimizationOpportunitySensor):
    _attr_name = "Floor heating price opportunity"

    def __init__(self, shadow, entry_id: str) -> None:
        super().__init__(shadow, entry_id, "floor_heating_price_opportunity")


class FloorRoomTemperatureSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Floor heating room temperature"
    _attr_icon = "mdi:home-thermometer"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, shadow, entry_id: str) -> None:
        self.shadow = shadow
        self._attr_unique_id = entry_id + "_floor_heating_room_temperature"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name=NAME, manufacturer="home_automation"
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.shadow.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return (
            self.shadow.decision is not None
            and self.shadow.decision.indoor_temperature is not None
        )

    @property
    def native_value(self) -> float | None:
        if not self.available:
            return None
        return self.shadow.decision.indoor_temperature

    @property
    def extra_state_attributes(self) -> dict:
        details = self.shadow.room_temperature_details
        return {
            "source": details["indoor_temperature_source"],
            "source_entities": details["indoor_temperature_entities"],
            "source_values_c": details["indoor_temperature_values_c"],
            "oldest_source_update": details["indoor_temperature_oldest_update"],
            "source_error": details["indoor_temperature_source_error"],
            "active_room_hvac_entities": details["active_room_hvac_entities"],
            "external_room_hvac_influence": details[
                "external_room_hvac_influence"
            ],
            "thermal_response_evidence_eligible": details[
                "thermal_response_evidence_eligible"
            ],
            "daikin_garage_temperature_c": details["daikin_indoor_temperature_c"],
        }


class ExtremePriceProtectionSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Extreme price protection"
    _attr_icon = "mdi:flash-alert"

    def __init__(self, protection, entry_id: str) -> None:
        self.protection = protection
        self._attr_unique_id = entry_id + "_extreme_price_protection"
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
    def native_value(self) -> str:
        return (
            self.protection.decision.state
            if self.protection.decision is not None
            else "starting"
        )

    @property
    def extra_state_attributes(self) -> dict:
        return self.protection.attributes
