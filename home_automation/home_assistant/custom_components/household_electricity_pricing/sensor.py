"""Exact variable-cost rates, not reconstructed supplier invoices."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import math
from pathlib import Path

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .coordinator import HouseholdPriceCoordinator
from .price_forecast import HouseholdPriceForecastCoordinator


_LOGGER = logging.getLogger(__name__)


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
        entities.append(
            SolarCurtailmentOpportunitySensor(
                solar_curtailment,
                entry.entry_id,
            )
        )
        entities.append(
            SolarRelevanceSensor(
                solar_curtailment,
                entry.entry_id,
            )
        )
        entities.extend(
            (
                SolarConfiguredPowerLimitSensor(
                    solar_curtailment,
                    entry.entry_id,
                ),
                SolarActivePowerLimitSensor(
                    solar_curtailment,
                    entry.entry_id,
                ),
                SolarControllerPowerRequestSensor(
                    solar_curtailment,
                    entry.entry_id,
                ),
                SolarQualifiedShadowProposalSensor(
                    solar_curtailment,
                    entry.entry_id,
                ),
            )
        )
    activity_controllers = [
        controller
        for controller in (
            optimizer,
            floor_shadow,
            extreme_protection,
            solar_curtailment,
        )
        if controller is not None
    ]
    if activity_controllers:
        activity = EnergyControlActivitySensor(
            optimizer,
            floor_shadow,
            extreme_protection,
            solar_curtailment,
            entry.entry_id,
        )
        entities.append(activity)
        entities.extend(
            EnergyControlAuthoritySensor(
                activity,
                key,
                name,
                icon,
                entry.entry_id,
            )
            for key, name, icon in (
                (
                    "hot_water",
                    "Hot water authority",
                    "mdi:water-boiler-auto",
                ),
                (
                    "floor_heating",
                    "Floor heating authority",
                    "mdi:heating-coil",
                ),
                (
                    "extreme_protection",
                    "Extreme protection authority",
                    "mdi:shield-lightning",
                ),
                (
                    "solar",
                    "Solar authority",
                    "mdi:solar-power-variant",
                ),
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
    _attr_name = "Floor heating optimizer"
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
    _EVENT_LIMIT = 16
    _COMMAND_EVENT_LIMIT = 8
    _ACTION_EPISODE_LIMIT = 32
    _STATUS_EPISODE_LIMIT = 12
    _COMMAND_KINDS = frozenset({"verified_command", "review_required"})

    _attr_has_entity_name = True
    _attr_name = "Energy control activity"
    _attr_icon = "mdi:timeline-check-outline"

    def __init__(
        self,
        optimizer,
        floor_shadow,
        extreme_protection,
        solar_curtailment,
        entry_id: str,
    ) -> None:
        self.optimizer = optimizer
        self.floor_shadow = floor_shadow
        self.extreme_protection = extreme_protection
        self.solar_curtailment = solar_curtailment
        self._audit_events: list[dict] = []
        self._audit_error: str | None = None
        self._audit_refresh_task = None
        self._attr_unique_id = entry_id + "_energy_control_activity"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh_audit_events(write_state=False)
        for controller in (
            self.optimizer,
            self.floor_shadow,
            self.extreme_protection,
            self.solar_curtailment,
        ):
            if controller is not None:
                self.async_on_remove(
                    controller.async_add_listener(self._handle_controller_update)
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

    @staticmethod
    def _solar_limit(value: object) -> str:
        if not isinstance(value, dict):
            return "—"
        configured = value.get("configured")
        if configured is None:
            return "—"
        suffix = "%" if value.get("mode") == "percent" else " W"
        try:
            display = f"{float(configured):g}"
        except (TypeError, ValueError):
            display = str(configured)
        return f"{display}{suffix}"

    @staticmethod
    def _numeric(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    @staticmethod
    def _journal_path(*controllers) -> Path | None:
        for controller in controllers:
            journal = getattr(
                getattr(controller, "devices", None),
                "journal",
                None,
            )
            if journal is not None:
                return Path(journal)
        return None

    @classmethod
    def _read_verified_audit_events(
        cls,
        journal_path: Path,
        cutoff: datetime,
    ) -> tuple[list[dict], list[str]]:
        if not journal_path.exists():
            return [], []

        events: list[dict] = []
        episode_seeds: dict[str, dict] = {}
        errors: list[str] = []
        with journal_path.open(encoding="utf-8") as journal:
            for line_number, line in enumerate(journal, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as err:
                    errors.append(f"line {line_number}: invalid JSON ({err.msg})")
                    continue
                if not isinstance(record, dict):
                    errors.append(f"line {line_number}: audit record is not an object")
                    continue
                operation = record.get("operation")
                if (
                    record.get("event") != "verified"
                    or operation
                    not in (
                        "hot_water_target",
                        "heating_offset",
                        "hot_water_enabled",
                        "heating_enabled",
                    )
                ):
                    continue
                timestamp = cls._timestamp(record.get("observed_at"))
                if timestamp is None:
                    errors.append(
                        f"line {line_number}: verified {operation} has no valid "
                        "observed_at"
                    )
                    continue
                if operation in ("hot_water_target", "heating_offset"):
                    previous = cls._numeric(record.get("previous"))
                    confirmed = cls._numeric(record.get("confirmed"))
                    if previous is None or confirmed is None:
                        errors.append(
                            f"line {line_number}: verified {operation} has "
                            "invalid values"
                        )
                        continue
                    unit = "°C"
                else:
                    previous = record.get("previous")
                    confirmed = record.get("confirmed")
                    if previous not in ("on", "standby") or confirmed not in (
                        "on",
                        "standby",
                    ):
                        errors.append(
                            f"line {line_number}: verified {operation} has "
                            "invalid circuit states"
                        )
                        continue
                    unit = None
                if operation.startswith("hot_water"):
                    system = "Hot water"
                    title = (
                        "Target confirmed"
                        if operation == "hot_water_target"
                        else "Circuit state confirmed"
                    )
                    label = (
                        "Target"
                        if operation == "hot_water_target"
                        else "Circuit"
                    )
                else:
                    system = "Floor heating"
                    title = (
                        "Offset confirmed"
                        if operation == "heating_offset"
                        else "Circuit state confirmed"
                    )
                    label = (
                        "Offset"
                        if operation == "heating_offset"
                        else "Circuit"
                    )
                change = (
                    cls._temperature_change(previous, confirmed, label)
                    if unit == "°C"
                    else f"{label} {previous} → {confirmed}"
                )
                event = {
                    "observed_at": timestamp.isoformat(),
                    "system": system,
                    "kind": "verified_command",
                    "title": title,
                    "state": "verified",
                    "reason": (
                        "Guarded Daikin command confirmed by native read-back."
                    ),
                    "result": "confirmed",
                    "before_value": previous,
                    "after_value": confirmed,
                    "change": change,
                    "unit": unit,
                    "decision_price_eur_kwh": None,
                    "command_source": "guarded_daikin_audit",
                    "operation": operation,
                    "request_id": record.get("request_id"),
                }
                if timestamp < cutoff:
                    episode_seeds[system + "|" + str(operation)] = {
                        **event,
                        "episode_seed": True,
                    }
                else:
                    events.append(event)
        events.extend(episode_seeds.values())
        events.sort(key=lambda event: event["observed_at"])
        return events, errors

    async def _async_refresh_audit_events(
        self,
        *,
        write_state: bool = True,
    ) -> None:
        journal_path = self._journal_path(self.optimizer, self.floor_shadow)
        if journal_path is None:
            self._audit_events = []
            self._audit_error = None
        else:
            cutoff = dt_util.utcnow() - timedelta(hours=24)
            try:
                events, errors = await self.hass.async_add_executor_job(
                    self._read_verified_audit_events,
                    journal_path,
                    cutoff,
                )
            except (OSError, UnicodeError) as err:
                error_message = f"{type(err).__name__}: {err}"
                if self._audit_error != error_message:
                    _LOGGER.error(
                        "Unable to read the energy-control audit journal %s: %s",
                        journal_path,
                        err,
                    )
                self._audit_error = error_message
            else:
                self._audit_events = events
                if errors:
                    visible_errors = "; ".join(errors[:3])
                    if len(errors) > 3:
                        visible_errors += f"; plus {len(errors) - 3} more"
                    if self._audit_error != visible_errors:
                        _LOGGER.error(
                            "Energy-control audit journal %s contains invalid "
                            "records: %s",
                            journal_path,
                            visible_errors,
                        )
                    self._audit_error = visible_errors
                else:
                    self._audit_error = None
        if write_state:
            self.async_write_ha_state()

    def _handle_controller_update(self) -> None:
        self.async_write_ha_state()
        if self._audit_refresh_task is None or self._audit_refresh_task.done():
            self._audit_refresh_task = self.hass.async_create_task(
                self._async_refresh_audit_events()
            )

    @classmethod
    def _is_duplicate_command(cls, candidate: dict, events: list[dict]) -> bool:
        candidate_timestamp = cls._timestamp(candidate.get("observed_at"))
        if candidate_timestamp is None:
            return False
        for event in events:
            if event.get("kind") != "verified_command":
                continue
            if event.get("system") != candidate.get("system"):
                continue
            if event.get("before_value") != candidate.get("before_value"):
                continue
            if event.get("after_value") != candidate.get("after_value"):
                continue
            event_timestamp = cls._timestamp(event.get("observed_at"))
            if event_timestamp is None:
                continue
            if abs((event_timestamp - candidate_timestamp).total_seconds()) <= 60:
                return True
        return False

    @classmethod
    def _event_values(
        cls,
        before: object,
        after: object,
        unit: str,
    ) -> dict:
        return {
            "before_value": cls._numeric(before),
            "after_value": cls._numeric(after),
            "unit": unit,
        }

    @staticmethod
    def _compact_reason(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        compact = " ".join(value.split())
        return compact[:177] + "..." if len(compact) > 180 else compact

    @classmethod
    def _nearest_record(
        cls,
        records: list[dict],
        observed_at: datetime,
        *,
        maximum_seconds: int = 300,
    ) -> dict | None:
        nearest = None
        nearest_seconds = maximum_seconds + 1
        for record in records:
            record_at = cls._timestamp(record.get("observed_at"))
            if record_at is None:
                continue
            distance = abs((record_at - observed_at).total_seconds())
            if distance <= maximum_seconds and distance < nearest_seconds:
                nearest = record
                nearest_seconds = distance
        return nearest

    @classmethod
    def _transition_exists(
        cls,
        transitions: list[dict],
        *,
        track: str,
        observed_at: datetime,
        before: object,
        after: object,
        tolerance_seconds: int = 60,
    ) -> bool:
        for transition in transitions:
            if transition["_track"] != track:
                continue
            if (
                transition["before_value"] != before
                or transition["after_value"] != after
            ):
                continue
            if (
                abs(
                    (
                        transition["_observed_at"] - observed_at
                    ).total_seconds()
                )
                <= tolerance_seconds
            ):
                return True
        return False

    def _episode_transitions(self, now: datetime) -> list[dict]:
        transitions: list[dict] = []

        def add_transition(
            *,
            track: str,
            subsystem: str,
            observed_at: datetime,
            before: object,
            after: object,
            unit: str | None,
            source: str,
            result: str,
            price: object,
            reason: object,
            operation: str,
        ) -> None:
            if observed_at > now + timedelta(minutes=5) or after is None:
                return
            if self._transition_exists(
                transitions,
                track=track,
                observed_at=observed_at,
                before=before,
                after=after,
            ):
                return
            transitions.append(
                {
                    "_track": track,
                    "_observed_at": observed_at,
                    "subsystem": subsystem,
                    "before_value": before,
                    "after_value": after,
                    "unit": unit,
                    "source": source,
                    "result": result,
                    "action_price_eur_kwh": self._numeric(price),
                    "reason": self._compact_reason(reason),
                    "operation": operation,
                }
            )

        dhw_records = (
            list(self.optimizer.state.get("decision_history", []))
            if self.optimizer is not None
            else []
        )
        floor_records = (
            list(self.floor_shadow.state.get("decision_history", []))
            if self.floor_shadow is not None
            else []
        )
        protection_records = (
            list(
                self.extreme_protection.state.get(
                    "decision_history",
                    [],
                )
            )
            if self.extreme_protection is not None
            else []
        )

        for event in self._audit_events:
            observed_at = self._timestamp(event.get("observed_at"))
            if observed_at is None:
                continue
            operation = str(event.get("operation") or "")
            if operation == "hot_water_target":
                track = "hot_water_target"
                subsystem = "hot_water"
                records = dhw_records
            elif operation == "heating_offset":
                track = "floor_offset"
                subsystem = "floor_heating"
                records = floor_records
            elif operation == "hot_water_enabled":
                track = "hot_water_circuit"
                subsystem = "hot_water"
                records = protection_records
            elif operation == "heating_enabled":
                track = "floor_heating_circuit"
                subsystem = "floor_heating"
                records = protection_records
            else:
                continue
            match = self._nearest_record(records, observed_at)
            protected = (
                operation in ("hot_water_enabled", "heating_enabled")
                and match is not None
                and bool(match.get("write_sent"))
            )
            add_transition(
                track=track,
                subsystem=subsystem,
                observed_at=observed_at,
                before=event.get("before_value"),
                after=event.get("after_value"),
                unit=event.get("unit"),
                source="protection" if protected else "optimizer",
                result="verified",
                price=(
                    match.get("current_price_eur_kwh")
                    if match is not None
                    else event.get("decision_price_eur_kwh")
                ),
                reason=(
                    match.get("reason")
                    if match is not None
                    else event.get("reason")
                ),
                operation=operation,
            )

        known_dhw_target = None
        for record in sorted(
            dhw_records,
            key=lambda item: item.get("observed_at") or "",
        ):
            observed_at = self._timestamp(record.get("observed_at"))
            actual = self._numeric(record.get("actual_target_c"))
            proposed = self._numeric(record.get("proposed_target_c"))
            if observed_at is None:
                continue
            if known_dhw_target is None and actual is not None:
                known_dhw_target = actual
            if (
                bool(record.get("write_sent"))
                and actual is not None
                and proposed is not None
                and actual != proposed
            ):
                add_transition(
                    track="hot_water_target",
                    subsystem="hot_water",
                    observed_at=observed_at,
                    before=actual,
                    after=proposed,
                    unit="°C",
                    source="optimizer",
                    result="verified",
                    price=record.get("current_price_eur_kwh"),
                    reason=record.get("reason"),
                    operation=str(record.get("state") or "target_change"),
                )
                known_dhw_target = proposed
            elif (
                actual is not None
                and known_dhw_target is not None
                and actual != known_dhw_target
            ):
                add_transition(
                    track="hot_water_target",
                    subsystem="hot_water",
                    observed_at=observed_at,
                    before=known_dhw_target,
                    after=actual,
                    unit="°C",
                    source="manual_external",
                    result="verified",
                    price=record.get("current_price_eur_kwh"),
                    reason=record.get("reason"),
                    operation="observed_target_change",
                )
                known_dhw_target = actual

        for record in floor_records:
            if not bool(record.get("write_sent")):
                continue
            observed_at = self._timestamp(record.get("observed_at"))
            before = self._numeric(record.get("actual_offset_c"))
            after = self._numeric(record.get("proposed_offset_c"))
            if (
                observed_at is None
                or before is None
                or after is None
                or before == after
            ):
                continue
            add_transition(
                track="floor_offset",
                subsystem="floor_heating",
                observed_at=observed_at,
                before=before,
                after=after,
                unit="°C",
                source="optimizer",
                result="verified",
                price=record.get("current_price_eur_kwh"),
                reason=record.get("reason"),
                operation=str(record.get("state") or "offset_change"),
            )

        if self.floor_shadow is not None:
            for record in self.floor_shadow.state.get(
                "native_schedule_transitions",
                [],
            ):
                observed_at = self._timestamp(record.get("observed_at"))
                before = self._numeric(record.get("previous_offset_c"))
                after = self._numeric(record.get("native_offset_c"))
                if (
                    observed_at is None
                    or before is None
                    or after is None
                    or before == after
                ):
                    continue
                classification = str(
                    record.get("classification") or "native_change"
                )
                nearest = self._nearest_record(
                    floor_records,
                    observed_at,
                )
                add_transition(
                    track="floor_offset",
                    subsystem="floor_heating",
                    observed_at=observed_at,
                    before=before,
                    after=after,
                    unit="°C",
                    source=(
                        "native_schedule"
                        if classification.startswith("accepted_")
                        else "manual_external"
                    ),
                    result="verified",
                    price=(
                        nearest.get("current_price_eur_kwh")
                        if nearest is not None
                        else None
                    ),
                    reason=(
                        nearest.get("reason")
                        if nearest is not None
                        else classification.replace("_", " ")
                    ),
                    operation=classification,
                )

        command_operations = {"standby", "restore", "turn_off"}
        for record in protection_records:
            if not bool(record.get("write_sent")):
                continue
            observed_at = self._timestamp(record.get("observed_at"))
            if observed_at is None:
                continue
            for action in record.get("actions") or []:
                operation = str(action.get("operation") or "")
                before = action.get("current_state")
                after = action.get("proposed_state")
                if (
                    operation not in command_operations
                    or after is None
                    or before == after
                ):
                    continue
                load_id = str(action.get("load_id") or "protected_load")
                if load_id == "daikin_hot_water":
                    subsystem = "hot_water"
                    track = "hot_water_circuit"
                elif load_id == "daikin_floor_heating":
                    subsystem = "floor_heating"
                    track = "floor_heating_circuit"
                else:
                    subsystem = "protection"
                    track = "protection|" + load_id
                add_transition(
                    track=track,
                    subsystem=subsystem,
                    observed_at=observed_at,
                    before=before,
                    after=after,
                    unit=None,
                    source="protection",
                    result="verified",
                    price=record.get("current_price_eur_kwh"),
                    reason=record.get("reason"),
                    operation=load_id + ":" + operation,
                )

        if self.solar_curtailment is not None:
            for record in self.solar_curtailment.operation_history:
                observed_at = self._timestamp(record.get("at"))
                operation = str(record.get("operation") or "")
                if (
                    observed_at is None
                    or record.get("success") is not True
                    or record.get("phase") != "verified"
                    or operation not in ("acquire", "restore")
                ):
                    continue
                previous = record.get("previous")
                confirmed = record.get("confirmed")
                if not isinstance(previous, dict) or not isinstance(
                    confirmed,
                    dict,
                ):
                    continue
                before = self._numeric(previous.get("configured"))
                after = self._numeric(confirmed.get("configured"))
                if before is None or after is None or before == after:
                    continue
                mode = confirmed.get("mode")
                add_transition(
                    track="solar_limit",
                    subsystem="solar",
                    observed_at=observed_at,
                    before=before,
                    after=after,
                    unit="%" if mode == "percent" else "W",
                    source=(
                        "optimizer"
                        if operation == "acquire"
                        else "protection"
                    ),
                    result="verified",
                    price=record.get("decision_price_eur_kwh"),
                    reason=record.get("reason") or operation,
                    operation=operation,
                )

        transitions.sort(
            key=lambda transition: transition["_observed_at"]
        )
        return transitions

    @staticmethod
    def _is_status_track(track: str) -> bool:
        return (
            track in {"hot_water_circuit", "floor_heating_circuit"}
            or track.startswith("protection|")
        )

    @staticmethod
    def _bounded_episodes(
        episodes: list[dict],
        limit: int,
    ) -> tuple[list[dict], int]:
        total = len(episodes)
        if total <= limit:
            return episodes, 0
        boundary_episodes = [
            episode
            for episode in episodes
            if episode.get("clipped_start") is True
        ][-limit:]
        boundary_ids = {id(episode) for episode in boundary_episodes}
        remaining_slots = limit - len(boundary_episodes)
        recent_episodes = (
            [
                episode
                for episode in episodes
                if id(episode) not in boundary_ids
            ][-remaining_slots:]
            if remaining_slots
            else []
        )
        visible = sorted(
            [*boundary_episodes, *recent_episodes],
            key=lambda episode: episode["start"],
        )
        return visible, total - len(visible)

    def _episode_collections(
        self,
    ) -> tuple[list[dict], int, list[dict], int]:
        now = dt_util.utcnow().astimezone(timezone.utc)
        cutoff = now - timedelta(hours=24)
        grouped: dict[str, list[dict]] = {}
        for transition in self._episode_transitions(now):
            if transition["_observed_at"] > now:
                continue
            grouped.setdefault(transition["_track"], []).append(
                transition
            )

        action_episodes = []
        status_episodes = []
        for track, transitions in grouped.items():
            for index, transition in enumerate(transitions):
                original_start = transition["_observed_at"]
                next_start = (
                    transitions[index + 1]["_observed_at"]
                    if index + 1 < len(transitions)
                    else None
                )
                effective_end = next_start or now
                if effective_end <= cutoff:
                    continue
                clipped_start = max(original_start, cutoff)
                if effective_end < clipped_start:
                    continue
                duration_seconds = max(
                    0,
                    int((effective_end - clipped_start).total_seconds()),
                )
                episode: dict = {
                    "subsystem": transition["subsystem"],
                    "start": clipped_start.isoformat(),
                    "end": (
                        next_start.isoformat()
                        if next_start is not None
                        else None
                    ),
                    "duration_minutes": round(
                        duration_seconds / 60,
                        3,
                    ),
                    "before_value": transition["before_value"],
                    "after_value": transition["after_value"],
                    "source": transition["source"],
                    "operation": transition["operation"],
                    "clipped_start": original_start < cutoff,
                }
                if transition["unit"] is not None:
                    episode["unit"] = transition["unit"]
                if transition["action_price_eur_kwh"] is not None:
                    episode["action_price_eur_kwh"] = transition[
                        "action_price_eur_kwh"
                    ]
                if original_start < cutoff:
                    episode["transition_at"] = (
                        original_start.isoformat()
                    )
                if self._is_status_track(track):
                    episode["active"] = next_start is None
                    status_episodes.append(episode)
                else:
                    action_episodes.append(episode)

        action_episodes.sort(key=lambda episode: episode["start"])
        status_episodes.sort(key=lambda episode: episode["start"])
        visible_actions, truncated_actions = self._bounded_episodes(
            action_episodes,
            self._ACTION_EPISODE_LIMIT,
        )
        visible_statuses, truncated_statuses = self._bounded_episodes(
            status_episodes,
            self._STATUS_EPISODE_LIMIT,
        )
        return (
            visible_actions,
            truncated_actions,
            visible_statuses,
            truncated_statuses,
        )

    @staticmethod
    def _controller_authority(
        controller,
        *,
        live_modes: tuple[str, ...],
        shadow_modes: tuple[str, ...],
        learning_states: tuple[str, ...] = (),
    ) -> str:
        if controller is None:
            return "unavailable"
        state = getattr(getattr(controller, "decision", None), "state", None)
        stored = getattr(controller, "state", {})
        if isinstance(stored, dict) and stored.get("fault"):
            return "fault"
        if state in ("fault", "persistence_fault"):
            return "fault"
        if state == "unavailable":
            return "unavailable"
        if state in learning_states:
            return "learning"
        mode = getattr(controller, "mode", None)
        if mode in shadow_modes:
            return "shadow"
        if mode in live_modes:
            return "live"
        return "blocked"

    def _authorities(self) -> dict:
        return {
            "hot_water": self._controller_authority(
                self.optimizer,
                live_modes=("readiness_pilot", "safe_target_shift"),
                shadow_modes=("shadow",),
            ),
            "floor_heating": self._controller_authority(
                self.floor_shadow,
                live_modes=("active",),
                shadow_modes=("shadow",),
                learning_states=("schedule_learning_hold",),
            ),
            "extreme_protection": self._controller_authority(
                self.extreme_protection,
                live_modes=("armed",),
                shadow_modes=("shadow",),
            ),
            "solar": self._controller_authority(
                self.solar_curtailment,
                live_modes=("armed",),
                shadow_modes=("shadow",),
                learning_states=("qualifying",),
            ),
        }

    def _event_collections(
        self,
    ) -> tuple[list[dict], list[dict], dict[str, int]]:
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
                    "authority": (
                        "shadow" if kind == "simulation" else "live"
                    ),
                    "state": state,
                    "change": self._temperature_change(
                        record.get("actual_target_c"),
                        record.get("proposed_target_c"),
                        "Target",
                    ),
                    "reason": record.get("reason"),
                    "decision_price_eur_kwh": self._numeric(
                        record.get("current_price_eur_kwh")
                    ),
                    "command_source": "dhw_optimizer",
                    **self._event_values(
                        record.get("actual_target_c"),
                        record.get("proposed_target_c"),
                        "°C",
                    ),
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
                write_sent = bool(record.get("write_sent"))
                mode = record.get("mode")
                events.append({
                    "observed_at": observed_at.isoformat(),
                    "system": "Floor heating",
                    "kind": (
                        "verified_command"
                        if write_sent
                        else (
                            "simulation"
                            if mode in (None, "shadow")
                            else "no_command"
                        )
                    ),
                    "authority": (
                        "shadow"
                        if mode in (None, "shadow")
                        else (
                            "learning"
                            if state == "schedule_learning_hold"
                            else "live"
                        )
                    ),
                    "state": state,
                    "change": self._temperature_change(
                        record.get("actual_offset_c"),
                        record.get("proposed_offset_c"),
                        "Offset",
                    ),
                    "reason": record.get("reason"),
                    "decision_price_eur_kwh": self._numeric(
                        record.get("current_price_eur_kwh")
                    ),
                    "command_source": "floor_heating_optimizer",
                    **self._event_values(
                        record.get("actual_offset_c"),
                        record.get("proposed_offset_c"),
                        "°C",
                    ),
                    "result": (
                        "Guarded command confirmed by native Daikin read-back."
                        if write_sent
                        else (
                            "Not sent; floor heating was in Shadow."
                            if mode in (None, "shadow")
                            else "No floor-offset command was required."
                        )
                    ),
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
                    "authority": (
                        "live"
                        if write_sent
                        else "blocked"
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
                    "decision_price_eur_kwh": self._numeric(
                        record.get("current_price_eur_kwh")
                    ),
                    "command_source": "extreme_price_protection",
                    "before_value": None,
                    "after_value": None,
                    "unit": None,
                    "result": (
                        "Sequential protected-load commands were confirmed."
                        if write_sent
                        else "No protected-load command was sent."
                    ),
                })

        if self.solar_curtailment is not None:
            for record in self.solar_curtailment.operation_history:
                observed_at = self._timestamp(record.get("at"))
                if observed_at is None or observed_at < cutoff:
                    continue
                phase = str(record.get("phase") or "unknown")
                success = record.get("success") is True
                operation = str(record.get("operation") or "review")
                command_confirmed = (
                    success
                    and phase == "verified"
                    and operation in ("acquire", "restore")
                )
                if command_confirmed:
                    kind = "verified_command"
                    result = (
                        "Guarded inverter command confirmed by native Huawei "
                        "read-back."
                    )
                elif "post_dispatch" in phase:
                    kind = "review_required"
                    result = (
                        "The inverter command outcome is uncertain and "
                        "requires fresh-state review."
                    )
                else:
                    kind = "no_command"
                    result = "No inverter command was sent."
                events.append({
                    "observed_at": observed_at.isoformat(),
                    "system": "Solar",
                    "kind": kind,
                    "authority": (
                        "fault"
                        if kind == "review_required"
                        else "live"
                    ),
                    "state": phase,
                    "change": (
                        "Limit {} → {}".format(
                            self._solar_limit(record.get("previous")),
                            self._solar_limit(record.get("confirmed")),
                        )
                        if command_confirmed
                        else (
                            "Apply controller cap"
                            if operation == "acquire"
                            else (
                                "Restore original limit"
                                if operation == "restore"
                                else "Fresh-state reconciliation"
                            )
                        )
                    ),
                    "reason": (
                        record.get("reason")
                        or record.get("error")
                        or operation.replace("_", " ").title()
                    ),
                    "decision_price_eur_kwh": self._numeric(
                        record.get("decision_price_eur_kwh")
                    ),
                    "command_source": "solar_curtailment",
                    **self._event_values(
                        (
                            record.get("previous", {}).get(
                                "configured"
                            )
                            if isinstance(
                                record.get("previous"), dict
                            )
                            else None
                        ),
                        (
                            record.get("confirmed", {}).get(
                                "configured"
                            )
                            if isinstance(
                                record.get("confirmed"), dict
                            )
                            else None
                        ),
                        (
                            "%"
                            if (
                                isinstance(
                                    record.get("confirmed"), dict
                                )
                                and record["confirmed"].get("mode")
                                == "percent"
                            )
                            else "W"
                        ),
                    ),
                    "result": result,
                })
            for record in getattr(
                self.solar_curtailment,
                "state",
                {},
            ).get(
                "decision_history",
                [],
            ):
                observed_at = self._timestamp(record.get("observed_at"))
                if observed_at is None or observed_at < cutoff:
                    continue
                state = str(record.get("state") or "")
                if state not in {
                    "price_only_opportunity",
                    "shadow_blocked",
                    "shadow_qualifying",
                    "shadow_qualified",
                    "migration_blocked",
                }:
                    continue
                proposal = self._numeric(
                    record.get("proposed_limit_w")
                )
                qualified = (
                    state == "shadow_qualified"
                    and proposal is not None
                )
                events.append({
                    "observed_at": observed_at.isoformat(),
                    "system": "Solar",
                    "kind": (
                        "simulation" if qualified else "no_command"
                    ),
                    "authority": "shadow",
                    "state": state,
                    "change": (
                        f"Qualified Shadow proposal {proposal:g} W"
                        if qualified
                        else (
                            "Price opportunity only"
                            if state == "price_only_opportunity"
                            else "No watt-valued proposal"
                        )
                    ),
                    "reason": record.get("reason"),
                    "decision_price_eur_kwh": self._numeric(
                        record.get("export_earnings_eur_kwh")
                    ),
                    "price_basis": "export_earnings",
                    "command_source": "solar_shadow",
                    **self._event_values(
                        None,
                        proposal if qualified else None,
                        "W",
                    ),
                    "result": "No inverter command was sent.",
                    "blocked_reason": record.get("blocked_reason"),
                })

        for audit_event in self._audit_events:
            observed_at = self._timestamp(audit_event.get("observed_at"))
            if (
                observed_at is None
                or observed_at < cutoff
                or observed_at > now + timedelta(minutes=5)
            ):
                continue
            if not self._is_duplicate_command(audit_event, events):
                events.append(dict(audit_event))

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
                    "authority",
                    "before_value",
                    "after_value",
                    "decision_price_eur_kwh",
                )
            )
            if last_signature_by_system.get(event["system"]) == signature:
                continue
            last_signature_by_system[event["system"]] = signature
            deduplicated.append(event)

        all_command_events = [
            event
            for event in deduplicated
            if event.get("kind") in self._COMMAND_KINDS
        ]
        command_events = all_command_events[-self._COMMAND_EVENT_LIMIT:]
        all_context_events = [
            event
            for event in deduplicated
            if event.get("kind") not in self._COMMAND_KINDS
        ]
        counts = {
            "event_count": len(all_context_events),
            "command_event_count": len(all_command_events),
            "verified_command_count": sum(
                event.get("kind") == "verified_command"
                for event in all_command_events
            ),
            "simulation_count": sum(
                event.get("kind") == "simulation"
                for event in all_context_events
            ),
            "no_command_count": sum(
                event.get("kind") == "no_command"
                for event in all_context_events
            ),
            "review_required_count": sum(
                event.get("kind") == "review_required"
                for event in all_command_events
            ),
        }
        visible_events = all_context_events[-self._EVENT_LIMIT:]
        return visible_events, command_events, counts

    def _events(self) -> list[dict]:
        events, command_events, _ = self._event_collections()
        return sorted(
            [*events, *command_events],
            key=lambda event: event["observed_at"],
        )

    @property
    def native_value(self) -> str:
        events = self._events()
        return events[-1]["kind"] if events else "no_recent_activity"

    @property
    def extra_state_attributes(self) -> dict:
        events, command_events, event_counts = self._event_collections()
        (
            action_episodes,
            truncated_actions,
            status_episodes,
            truncated_statuses,
        ) = self._episode_collections()
        return {
            "window_hours": 24,
            "event_limit": self._EVENT_LIMIT,
            "command_event_limit": self._COMMAND_EVENT_LIMIT,
            "action_episode_limit": self._ACTION_EPISODE_LIMIT,
            "status_episode_limit": self._STATUS_EPISODE_LIMIT,
            "events": events,
            "command_events": command_events,
            "event_count": event_counts["event_count"],
            "event_retained_count": len(events),
            "event_truncated_count": (
                event_counts["event_count"] - len(events)
            ),
            "command_event_count": event_counts[
                "command_event_count"
            ],
            "command_event_retained_count": len(command_events),
            "command_event_truncated_count": (
                event_counts["command_event_count"]
                - len(command_events)
            ),
            "action_episodes": action_episodes,
            "action_episode_count": (
                len(action_episodes) + truncated_actions
            ),
            "action_episode_retained_count": len(action_episodes),
            "action_episode_truncated_count": truncated_actions,
            "status_episodes": status_episodes,
            "status_episode_count": (
                len(status_episodes) + truncated_statuses
            ),
            "status_episode_retained_count": len(status_episodes),
            "status_episode_truncated_count": truncated_statuses,
            "audit_error": self._audit_error,
            "authority": self._authorities(),
            "verified_command_count": event_counts[
                "verified_command_count"
            ],
            "simulation_count": event_counts["simulation_count"],
            "no_command_count": event_counts["no_command_count"],
            "review_required_count": event_counts[
                "review_required_count"
            ],
            "uncertain_command_count": event_counts[
                "review_required_count"
            ],
        }


class EnergyControlAuthoritySensor(SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        activity: EnergyControlActivitySensor,
        authority_key: str,
        name: str,
        icon: str,
        entry_id: str,
    ) -> None:
        self.activity = activity
        self.authority_key = authority_key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = (
            entry_id + "_energy_control_" + authority_key + "_authority"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="home_automation",
        )

    async def async_added_to_hass(self) -> None:
        for controller in (
            self.activity.optimizer,
            self.activity.floor_shadow,
            self.activity.extreme_protection,
            self.activity.solar_curtailment,
        ):
            if controller is not None:
                self.async_on_remove(
                    controller.async_add_listener(
                        self.async_write_ha_state
                    )
                )

    @property
    def native_value(self) -> str:
        return self.activity._authorities()[self.authority_key]

    @property
    def extra_state_attributes(self) -> dict:
        controller = {
            "hot_water": self.activity.optimizer,
            "floor_heating": self.activity.floor_shadow,
            "extreme_protection": self.activity.extreme_protection,
            "solar": self.activity.solar_curtailment,
        }[self.authority_key]
        decision = getattr(controller, "decision", None)
        return {
            "controller_mode": getattr(controller, "mode", None),
            "controller_state": getattr(decision, "state", None),
            "reason": getattr(decision, "reason", None),
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


class SolarCurtailmentOpportunitySensor(OptimizationOpportunitySensor):
    _attr_name = "Solar curtailment price opportunity"

    def __init__(self, controller, entry_id: str) -> None:
        super().__init__(
            controller,
            entry_id,
            "solar_curtailment_price_opportunity",
        )


class SolarRelevanceSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Solar relevance"
    _attr_icon = "mdi:weather-sunny-alert"

    def __init__(self, controller, entry_id: str) -> None:
        self.controller = controller
        self._attr_unique_id = entry_id + "_solar_relevance"
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
        attributes = self.controller.attributes
        daylight = attributes.get("sun_state")
        active_power = attributes.get("active_power_w")
        output_gate = attributes.get("output_gate_w")
        if daylight is None:
            return "unavailable"
        if daylight != "above_horizon":
            return "waiting_for_daylight"
        if attributes.get("active_power_fresh") is not True:
            return "waiting_for_fresh_output"
        if active_power is None or output_gate is None:
            return "unavailable"
        if attributes.get("shadow_relevance_ready"):
            return "shadow_ready"
        if float(active_power) >= float(output_gate):
            return "qualifying"
        return "waiting_for_output"

    @property
    def extra_state_attributes(self) -> dict:
        attributes = self.controller.attributes
        active_power = attributes.get("active_power_w")
        output_gate = attributes.get("output_gate_w")
        return {
            "ready": False,
            "shadow_ready": self.native_value == "shadow_ready",
            "last_shadow_relevance_ready_at": attributes.get(
                "last_shadow_relevance_ready_at"
            ),
            "daylight_state": attributes.get("sun_state"),
            "active_power_w": active_power,
            "active_power_fresh": attributes.get("active_power_fresh"),
            "output_gate_w": output_gate,
            "daylight_required": True,
            "write_authority": "shadow_only",
            "write_allowed": attributes.get("write_allowed"),
            "grid_export_meter_available": attributes.get(
                "grid_export_meter_available"
            ),
            "grid_export_meter_blocker": attributes.get(
                "grid_export_meter_blocker"
            ),
        }


class SolarPowerLimitSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        controller,
        entry_id: str,
        unique_suffix: str,
        attribute: str,
    ) -> None:
        self.controller = controller
        self.attribute = attribute
        self._attr_unique_id = entry_id + "_" + unique_suffix
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
    def available(self) -> bool:
        return self.controller.attributes.get(self.attribute) is not None

    @property
    def native_value(self) -> float | None:
        value = self.controller.attributes.get(self.attribute)
        return float(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        attributes = self.controller.attributes
        return {
            "native_limit_mode": attributes.get("configured_limit_mode"),
            "inverter_pmax_w": attributes.get("pmax_w"),
        }


class SolarConfiguredPowerLimitSensor(SolarPowerLimitSensor):
    _attr_name = "Solar configured power limit"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, controller, entry_id: str) -> None:
        super().__init__(
            controller,
            entry_id,
            "solar_configured_power_limit",
            "configured_limit_equivalent_w",
        )


class SolarActivePowerLimitSensor(SolarPowerLimitSensor):
    _attr_name = "Solar active power limit"
    _attr_icon = "mdi:solar-power"

    def __init__(self, controller, entry_id: str) -> None:
        super().__init__(
            controller,
            entry_id,
            "solar_active_power_limit",
            "active_limit_equivalent_w",
        )


class SolarControllerPowerRequestSensor(SolarPowerLimitSensor):
    _attr_name = "Solar controller power request"
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, controller, entry_id: str) -> None:
        super().__init__(
            controller,
            entry_id,
            "solar_controller_power_request",
            "requested_limit_equivalent_w",
        )


class SolarQualifiedShadowProposalSensor(SolarPowerLimitSensor):
    _attr_name = "Solar qualified Shadow proposal"
    _attr_icon = "mdi:weather-sunny-alert"

    def __init__(self, controller, entry_id: str) -> None:
        super().__init__(
            controller,
            entry_id,
            "solar_qualified_shadow_proposal",
            "qualified_proposed_limit_equivalent_w",
        )
