"""Exact variable-cost rates, not reconstructed supplier invoices."""

from __future__ import annotations

from datetime import timedelta, timezone
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
    solar_curtailment = hass.data.get(DOMAIN, {}).get(
        entry.entry_id, {}
    ).get("solar_curtailment")
    if solar_curtailment:
        entities.append(
            SolarCurtailmentSensor(
                solar_curtailment,
                entry.entry_id,
            )
        )
    activity_controllers = [
        controller
        for controller in (
            optimizer,
            floor_shadow,
            extreme_protection,
        )
        if controller is not None
    ]
    if activity_controllers:
        entities.append(
            EnergyControlActivitySensor(
                optimizer,
                floor_shadow,
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


class EnergyControlActivitySensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Energy control activity"
    _attr_icon = "mdi:timeline-check-outline"

    def __init__(
        self,
        optimizer,
        floor_shadow,
        extreme_protection,
        entry_id: str,
    ) -> None:
        self.optimizer = optimizer
        self.floor_shadow = floor_shadow
        self.extreme_protection = extreme_protection
        self._attr_unique_id = entry_id + "_energy_control_activity"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        for controller in (
            self.optimizer,
            self.floor_shadow,
            self.extreme_protection,
        ):
            if controller is not None:
                self.async_on_remove(
                    controller.async_add_listener(self.async_write_ha_state)
                )

    @staticmethod
    def _timestamp(value: object):
        if not isinstance(value, str):
            return None
        parsed = dt_util.parse_datetime(value)
        return parsed.astimezone(timezone.utc) if parsed is not None else None

    @staticmethod
    def _temperature_change(
        actual: object,
        proposed: object,
        label: str,
    ) -> str:
        before = "—" if actual is None else f"{actual}°C"
        after = "—" if proposed is None else f"{proposed}°C"
        return f"{label} {before} → {after}"

    def _events(self) -> list[dict]:
        now = dt_util.utcnow().astimezone(timezone.utc)
        cutoff = now - timedelta(hours=24)
        events = []

        if self.optimizer is not None:
            pilot_started = self._timestamp(
                self.optimizer.state.get("readiness_pilot_started_at")
            )
            for record in self.optimizer.state.get("decision_history", []):
                observed_at = self._timestamp(record.get("observed_at"))
                if observed_at is None or observed_at < cutoff:
                    continue
                state = record.get("state")
                if state == "unavailable":
                    continue
                write_sent = bool(record.get("write_sent"))
                live = (
                    pilot_started is not None
                    and observed_at >= pilot_started
                )
                if not write_sent:
                    if live and state not in (
                        "readiness_pilot",
                        "recovery",
                        "fault",
                        "manual_override",
                    ):
                        continue
                    if not live and state == "normal":
                        continue
                kind = (
                    "verified_command"
                    if write_sent
                    else "no_command" if live
                    else "simulation"
                )
                events.append({
                    "observed_at": observed_at.isoformat(),
                    "system": "Hot water",
                    "kind": kind,
                    "state": state,
                    "change": self._temperature_change(
                        record.get("actual_target_c"),
                        record.get("proposed_target_c"),
                        "Target",
                    ),
                    "reason": record.get("reason"),
                    "result": (
                        "Guarded command confirmed by native Daikin read-back."
                        if write_sent
                        else (
                            "No device command was sent."
                            if live
                            else "Not sent; the controller was in Shadow."
                        )
                    ),
                })

        if self.floor_shadow is not None:
            for record in self.floor_shadow.state.get(
                "decision_history",
                [],
            ):
                observed_at = self._timestamp(record.get("observed_at"))
                if observed_at is None or observed_at < cutoff:
                    continue
                state = record.get("state")
                if state in ("unavailable", "hold"):
                    continue
                events.append({
                    "observed_at": observed_at.isoformat(),
                    "system": "Floor heating",
                    "kind": "simulation",
                    "state": state,
                    "change": self._temperature_change(
                        record.get("actual_offset_c"),
                        record.get("proposed_offset_c"),
                        "Offset",
                    ),
                    "reason": record.get("reason"),
                    "result": "Not sent; floor heating remains Shadow-only.",
                })

        if self.extreme_protection is not None:
            for record in self.extreme_protection.state.get(
                "decision_history",
                [],
            ):
                observed_at = self._timestamp(record.get("observed_at"))
                if observed_at is None or observed_at < cutoff:
                    continue
                state = record.get("state")
                actions = record.get("actions") or []
                write_sent = bool(record.get("write_sent"))
                if (
                    state in ("clear", "unavailable")
                    and not actions
                    and not write_sent
                ):
                    continue
                events.append({
                    "observed_at": observed_at.isoformat(),
                    "system": "Extreme protection",
                    "kind": (
                        "verified_command"
                        if write_sent
                        else "no_command"
                    ),
                    "state": state,
                    "change": (
                        ", ".join(
                            "{}: {}".format(
                                action.get("load_id", "load"),
                                action.get("operation", "review"),
                            )
                            for action in actions
                        )
                        or "No protected-load change"
                    ),
                    "reason": record.get("reason"),
                    "result": (
                        "Sequential protected-load commands were confirmed."
                        if write_sent
                        else "No protected-load command was sent."
                    ),
                })

        events.sort(key=lambda item: item["observed_at"])
        deduplicated = []
        last_signature_by_system = {}
        for event in events:
            signature = tuple(
                event.get(key)
                for key in (
                    "kind",
                    "state",
                    "change",
                    "reason",
                    "result",
                )
            )
            if last_signature_by_system.get(event["system"]) == signature:
                continue
            last_signature_by_system[event["system"]] = signature
            deduplicated.append(event)
        return deduplicated[-12:]

    @property
    def native_value(self) -> str:
        events = self._events()
        return events[-1]["kind"] if events else "no_recent_activity"

    @property
    def extra_state_attributes(self) -> dict:
        events = self._events()
        return {
            "window_hours": 24,
            "events": events,
            "verified_command_count": sum(
                event["kind"] == "verified_command" for event in events
            ),
            "simulation_count": sum(
                event["kind"] == "simulation" for event in events
            ),
            "no_command_count": sum(
                event["kind"] == "no_command" for event in events
            ),
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


class SolarCurtailmentSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Solar curtailment"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, controller, entry_id: str) -> None:
        self.controller = controller
        self._attr_unique_id = entry_id + "_solar_curtailment"
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
    def native_value(self) -> str:
        return (
            self.controller.decision.state
            if self.controller.decision is not None
            else "starting"
        )

    @property
    def extra_state_attributes(self) -> dict:
        return self.controller.attributes
