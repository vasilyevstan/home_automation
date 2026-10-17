"""Deterministic, zero-write floor-heating price-shift evaluator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
from typing import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ._vendor.home_automation.electricity_market.prices import (
    MARKET_INTERVAL,
    TALLINN,
    UTC,
    market_bounds,
)
from .const import (
    CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES,
    DOMAIN,
    EVENT_FLOOR_SHADOW_DECISION,
    FLOOR_COMFORT_FLOOR,
    FLOOR_HORIZON_HOURS,
    FLOOR_MAX_OUTDOOR_TEMPERATURE,
    FLOOR_MAX_SHIFT_HOURS,
    FLOOR_MIN_LEAVING_WATER_TEMPERATURE,
    FLOOR_MIN_OFFSET,
    FLOOR_NATIVE_MAX_OFFSET,
    FLOOR_NATIVE_MIN_OFFSET,
    FLOOR_NEUTRAL_OFFSET,
    FLOOR_PREHEAT_CEILING,
    FLOOR_PREHEAT_OFFSET,
    FLOOR_PRICE_DELTA,
    FLOOR_PRICE_PERCENT,
    FLOOR_ROOM_TEMPERATURE_MAX_AGE_MINUTES,
    FLOOR_SHADOW_MODE,
    FLOOR_WINDOW_MINUTES,
)
from .controls import EnergyDevices, FLAGS
from .coordinator import HouseholdPriceCoordinator
from .dhw_optimizer import (
    DhwOptimizer,
    PriceInterval,
    PriceWindowChoice,
    price_gate,
    published_intervals,
    quarter_start,
)


STORE_VERSION = 1
STORE_KEY_SUFFIX = "floor_heating_shadow"
POLICY_REVISION = 3
DECISION_HISTORY_LIMIT = 96


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.utcoffset() is not None else None


def _temperature_celsius(value: object, unit: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    if unit in ("°C", "C"):
        return result
    if unit in ("°F", "F"):
        return (result - 32) * 5 / 9
    if unit in ("K", "kelvin"):
        return result - 273.15
    return None


def read_room_temperature_source(
    hass: HomeAssistant,
    entity_id: object,
    *,
    now: datetime | None = None,
    maximum_age: timedelta | None = timedelta(
        minutes=FLOOR_ROOM_TEMPERATURE_MAX_AGE_MINUTES
    ),
) -> tuple[float | None, datetime | None, str | None]:
    if not isinstance(entity_id, str) or "." not in entity_id:
        return None, None, "The occupied-room temperature entity ID is invalid."
    state = hass.states.get(entity_id)
    if state is None:
        return None, None, f"Occupied-room temperature entity {entity_id} is missing."
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None, None, f"Occupied-room temperature entity {entity_id} is unavailable."
    domain = entity_id.split(".", 1)[0]
    if domain == "climate":
        raw_value = state.attributes.get("current_temperature")
        unit = (
            state.attributes.get("temperature_unit")
            or hass.config.units.temperature_unit
        )
    elif domain == "sensor":
        raw_value = state.state
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    else:
        return None, None, f"{entity_id} is not a sensor or climate entity."
    value = _temperature_celsius(raw_value, unit)
    if value is None:
        return (
            None,
            None,
            f"Occupied-room temperature entity {entity_id} has no supported temperature.",
        )
    updated = getattr(state, "last_reported", None) or state.last_updated
    if maximum_age is not None and now is not None:
        age = now.astimezone(UTC) - updated.astimezone(UTC)
        if age > maximum_age:
            return (
                None,
                updated,
                f"Occupied-room temperature entity {entity_id} is stale.",
            )
    return value, updated, None


@dataclass(frozen=True)
class FloorHeatingPolicy:
    minimum_offset: float = FLOOR_MIN_OFFSET
    neutral_offset: float = FLOOR_NEUTRAL_OFFSET
    preheat_offset: float = FLOOR_PREHEAT_OFFSET
    comfort_floor: float = FLOOR_COMFORT_FLOOR
    preheat_ceiling: float = FLOOR_PREHEAT_CEILING
    maximum_outdoor_temperature: float = FLOOR_MAX_OUTDOOR_TEMPERATURE
    minimum_leaving_water_temperature: float = FLOOR_MIN_LEAVING_WATER_TEMPERATURE
    window_minutes: int = FLOOR_WINDOW_MINUTES
    horizon: timedelta = timedelta(hours=FLOOR_HORIZON_HOURS)
    maximum_shift: timedelta = timedelta(hours=FLOOR_MAX_SHIFT_HOURS)
    price_delta: Decimal = Decimal(str(FLOOR_PRICE_DELTA))
    price_percent: Decimal = Decimal(str(FLOOR_PRICE_PERCENT)) / Decimal(100)

    def __post_init__(self) -> None:
        if not self.minimum_offset <= self.neutral_offset < self.preheat_offset:
            raise HomeAssistantError("Floor offsets must satisfy minimum <= neutral < preheat.")
        if self.minimum_offset < -2 or self.preheat_offset > 1:
            raise HomeAssistantError("The initial floor Shadow must remain inside -2°C to +1°C.")
        if self.comfort_floor >= self.preheat_ceiling:
            raise HomeAssistantError("The floor comfort threshold must be below the preheat ceiling.")
        if self.window_minutes <= 0 or self.window_minutes % 15:
            raise HomeAssistantError("The floor shift window must use whole 15-minute intervals.")
        if self.horizon < timedelta(hours=12) or self.maximum_shift <= timedelta(0):
            raise HomeAssistantError("The floor planning horizon or shift span is invalid.")


@dataclass(frozen=True)
class FloorShiftPlan:
    cheap_window: PriceWindowChoice
    peak_window: PriceWindowChoice


@dataclass(frozen=True)
class FloorHeatingObservation:
    indoor_temperature: float | None
    outdoor_temperature: float | None
    leaving_water_temperature: float | None
    actual_offset: float | None
    enabled: bool | None
    override_reason: str | None
    unavailable_reason: str | None = None
    external_influence_reason: str | None = None


@dataclass(frozen=True)
class FloorHeatingDecision:
    state: str
    reason: str
    actual_offset: float | None
    proposed_offset: float | None
    aggressive_candidate_offset: float | None
    indoor_temperature: float | None
    outdoor_temperature: float | None
    leaving_water_temperature: float | None
    current_price: Decimal | None
    plan: FloorShiftPlan | None
    price_delta: Decimal | None
    price_delta_percent: Decimal | None
    price_gate: bool
    data_complete: bool
    dhw_state: str | None
    dhw_proposed_target: float | None
    compressor_conflict: bool

    def attributes(self) -> dict:
        result = {
            "mode": FLOOR_SHADOW_MODE,
            "state": self.state,
            "reason": self.reason,
            "actual_offset_c": self.actual_offset,
            "proposed_offset_c": self.proposed_offset,
            "aggressive_candidate_offset_c": self.aggressive_candidate_offset,
            "indoor_temperature_c": self.indoor_temperature,
            "outdoor_temperature_c": self.outdoor_temperature,
            "leaving_water_temperature_c": self.leaving_water_temperature,
            "current_price_eur_kwh": (
                format(self.current_price, "f") if self.current_price is not None else None
            ),
            "price_advantage_eur_kwh": (
                format(self.price_delta, "f") if self.price_delta is not None else None
            ),
            "price_advantage_percent": (
                format(self.price_delta_percent * Decimal(100), "f")
                if self.price_delta_percent is not None else None
            ),
            "price_gate": self.price_gate,
            "data_complete": self.data_complete,
            "dhw_state": self.dhw_state,
            "dhw_proposed_target_c": self.dhw_proposed_target,
            "compressor_conflict": self.compressor_conflict,
            "write_allowed": False,
            "shadow_zero_writes": True,
        }
        if self.plan is None:
            result.update({
                "cheap_window_start": None,
                "cheap_window_end": None,
                "cheap_window_average_eur_kwh": None,
                "peak_window_start": None,
                "peak_window_end": None,
                "peak_window_average_eur_kwh": None,
            })
        else:
            result.update({
                "cheap_window_start": self.plan.cheap_window.start.isoformat(),
                "cheap_window_end": self.plan.cheap_window.end.isoformat(),
                "cheap_window_average_eur_kwh": format(
                    self.plan.cheap_window.average, "f"
                ),
                "peak_window_start": self.plan.peak_window.start.isoformat(),
                "peak_window_end": self.plan.peak_window.end.isoformat(),
                "peak_window_average_eur_kwh": format(
                    self.plan.peak_window.average, "f"
                ),
            })
        return result


def _price_map(intervals: Iterable[PriceInterval]) -> dict[datetime, PriceInterval]:
    result = {}
    for item in intervals:
        if item.start in result:
            return {}
        result[item.start] = item
    return result


def _window(
    prices: dict[datetime, PriceInterval],
    start: datetime,
    interval_count: int,
) -> PriceWindowChoice | None:
    rows = [prices.get(start + index * MARKET_INTERVAL) for index in range(interval_count)]
    if not all(row is not None for row in rows):
        return None
    total = sum((row.price for row in rows if row is not None), Decimal(0))
    return PriceWindowChoice(
        start=start,
        end=start + interval_count * MARKET_INTERVAL,
        average=total / interval_count,
        interval_count=interval_count,
    )


def _coverage_complete(
    prices: dict[datetime, PriceInterval],
    start: datetime,
    end: datetime,
) -> bool:
    cursor = quarter_start(start)
    boundary = end.astimezone(UTC)
    while cursor < boundary:
        row = prices.get(cursor)
        if row is None or row.end != cursor + MARKET_INTERVAL:
            return False
        cursor += MARKET_INTERVAL
    return True


def select_floor_shift_plan(
    now: datetime,
    intervals: Iterable[PriceInterval],
    policy: FloorHeatingPolicy,
    planned: FloorShiftPlan | None = None,
) -> tuple[FloorShiftPlan | None, Decimal | None, bool]:
    if now.utcoffset() is None:
        raise HomeAssistantError("Floor planning time must include a UTC offset.")
    prices = _price_map(intervals)
    current = quarter_start(now)
    current_price = prices[current].price if current in prices else None
    horizon_end = current + policy.horizon
    if current_price is None:
        return None, None, False

    if planned is not None and current < planned.peak_window.end:
        interval_count = policy.window_minutes // 15
        cheap = _window(prices, planned.cheap_window.start, interval_count)
        peak = _window(prices, planned.peak_window.start, interval_count)
        if (
            cheap == planned.cheap_window
            and peak == planned.peak_window
        ):
            return planned, current_price, True

    current_day = current.astimezone(TALLINN).date()
    _, current_day_end = market_bounds(current_day)
    if not _coverage_complete(prices, current, current_day_end):
        return None, current_price, False
    published_ranges = [(current, current_day_end)]
    next_day = current_day + timedelta(days=1)
    while True:
        day_start, day_end = market_bounds(next_day)
        if day_start >= horizon_end:
            break
        if _coverage_complete(prices, day_start, day_end):
            published_ranges.append((day_start, day_end))
        next_day += timedelta(days=1)

    interval_count = policy.window_minutes // 15
    duration = interval_count * MARKET_INTERVAL
    candidates = []
    candidate_start = current
    while candidate_start + duration <= horizon_end:
        choice = _window(prices, candidate_start, interval_count)
        if choice is not None and any(
            choice.start >= range_start and choice.end <= range_end
            for range_start, range_end in published_ranges
        ):
            candidates.append(choice)
        candidate_start += MARKET_INTERVAL

    pairs = []
    for peak in candidates:
        preceding = [
            cheap
            for cheap in candidates
            if cheap.end <= peak.start
            and peak.start - cheap.end <= policy.maximum_shift
            and cheap.start.astimezone(TALLINN).date()
            == peak.start.astimezone(TALLINN).date()
        ]
        if not preceding:
            continue
        cheap = min(preceding, key=lambda item: (item.average, -item.start.timestamp()))
        delta = peak.average - cheap.average
        pairs.append((delta, -peak.start.timestamp(), FloorShiftPlan(cheap, peak)))
    if not pairs:
        return None, current_price, True
    return max(pairs, key=lambda item: (item[0], item[1]))[2], current_price, True


def plan_floor_heating(
    *,
    now: datetime,
    policy: FloorHeatingPolicy,
    observation: FloorHeatingObservation,
    intervals: Iterable[PriceInterval],
    planned: FloorShiftPlan | None = None,
    dhw_state: str | None = None,
    dhw_proposed_target: float | None = None,
) -> FloorHeatingDecision:
    plan, current_price, complete = select_floor_shift_plan(
        now, intervals, policy, planned
    )
    gate, delta, relative = price_gate(
        plan.peak_window.average if plan is not None else None,
        plan.cheap_window if plan is not None else None,
        policy.price_delta,
        policy.price_percent,
    )
    busy = dhw_state in {"preheat", "recovery", "readiness"}

    def decision(
        state: str,
        reason: str,
        proposed_offset: float | None,
        *,
        aggressive_candidate_offset: float | None = None,
        conflict: bool = False,
    ) -> FloorHeatingDecision:
        return FloorHeatingDecision(
            state=state,
            reason=reason,
            actual_offset=observation.actual_offset,
            proposed_offset=proposed_offset,
            aggressive_candidate_offset=aggressive_candidate_offset,
            indoor_temperature=observation.indoor_temperature,
            outdoor_temperature=observation.outdoor_temperature,
            leaving_water_temperature=observation.leaving_water_temperature,
            current_price=current_price,
            plan=plan,
            price_delta=delta,
            price_delta_percent=relative,
            price_gate=gate,
            data_complete=complete,
            dhw_state=dhw_state,
            dhw_proposed_target=dhw_proposed_target,
            compressor_conflict=conflict,
        )

    if observation.override_reason:
        return decision("yield", observation.override_reason, observation.actual_offset)
    if (
        observation.indoor_temperature is None
        or observation.outdoor_temperature is None
        or observation.leaving_water_temperature is None
        or observation.actual_offset is None
    ):
        return decision(
            "unavailable",
            observation.unavailable_reason
            or "Fresh indoor, outdoor, leaving-water and LWT-offset observations are required.",
            None,
        )
    if not policy.minimum_offset <= observation.actual_offset <= policy.preheat_offset:
        return decision(
            "yield",
            "The native/manual LWT offset is outside the initial -2°C to +1°C Shadow envelope.",
            observation.actual_offset,
        )
    if observation.indoor_temperature <= policy.comfort_floor:
        return decision(
            "comfort_guard",
            "Indoor temperature is at the 20°C guard; no price-driven setback is proposed.",
            policy.neutral_offset,
        )
    if observation.external_influence_reason is not None:
        return decision(
            "yield",
            observation.external_influence_reason,
            observation.actual_offset,
        )
    if not complete or current_price is None:
        return decision(
            "unavailable",
            "Complete published all-in prices are required through the current "
            "Tallinn delivery day.",
            observation.actual_offset,
        )
    if plan is None:
        return decision(
            "hold",
            "No complete cheap-before-peak floor-shift pair exists in the planning horizon.",
            observation.actual_offset,
        )
    if (
        observation.outdoor_temperature >= policy.maximum_outdoor_temperature
        or observation.leaving_water_temperature
        < policy.minimum_leaving_water_temperature
    ):
        return decision(
            "hold",
            "Outdoor and leaving-water conditions do not provide evidence of useful space-heating demand.",
            observation.actual_offset,
        )
    now_utc = now.astimezone(UTC)
    if plan.cheap_window.start <= now_utc < plan.cheap_window.end:
        if not gate:
            return decision(
                "hold",
                "The cheap-to-peak all-in price advantage does not pass both materiality gates.",
                observation.actual_offset,
            )
        if busy:
            return decision(
                "deferred",
                "DHW comfort/readiness has compressor priority during this candidate window.",
                observation.actual_offset,
                conflict=True,
            )
        if observation.indoor_temperature >= policy.preheat_ceiling:
            return decision(
                "preheat",
                "Use the observed 0°C envelope in the cheap block; the room is too warm to score +1°C.",
                policy.neutral_offset,
            )
        return decision(
            "preheat",
            "The current quarter is inside the selected cheap block; 0°C is the primary proposal and +1°C is scored separately.",
            policy.neutral_offset,
            aggressive_candidate_offset=policy.preheat_offset,
        )
    if plan.peak_window.start <= now_utc < plan.peak_window.end:
        if not gate:
            return decision(
                "hold",
                "The cheap-to-peak all-in price advantage does not pass both materiality gates.",
                observation.actual_offset,
            )
        return decision(
            "coast",
            "The current quarter is inside the paired expensive block; -2°C stays within the observed native envelope.",
            policy.minimum_offset,
        )
    return decision(
        "hold",
        "Keep the observed native offset outside the selected cheap and peak blocks.",
        observation.actual_offset,
    )


class FloorHeatingShadow:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: HouseholdPriceCoordinator,
        devices: EnergyDevices,
        dhw_optimizer: DhwOptimizer | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.devices = devices
        self.dhw_optimizer = dhw_optimizer
        self.policy = FloorHeatingPolicy()
        configured_entities = entry.options.get(
            CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES, []
        )
        self.room_temperature_entities = (
            tuple(
                entity_id
                for entity_id in configured_entities
                if isinstance(entity_id, str)
            )
            if isinstance(configured_entities, (list, tuple))
            else ()
        )
        self.room_temperature_details = {
            "indoor_temperature_source": "minimum_of_configured_occupied_rooms",
            "indoor_temperature_entities": list(self.room_temperature_entities),
            "indoor_temperature_values_c": {},
            "indoor_temperature_oldest_update": None,
            "indoor_temperature_max_age_minutes": (
                FLOOR_ROOM_TEMPERATURE_MAX_AGE_MINUTES
            ),
            "indoor_temperature_source_error": None,
            "active_room_hvac_entities": [],
            "external_room_hvac_influence": False,
            "thermal_response_evidence_eligible": False,
            "daikin_indoor_temperature_c": None,
            "daikin_indoor_temperature_role": "garage_diagnostic_only",
        }
        self.store = Store(
            hass,
            STORE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.{STORE_KEY_SUFFIX}",
            private=True,
        )
        self.lock = asyncio.Lock()
        self.decision: FloorHeatingDecision | None = None
        self.listeners: list[Callable[[], None]] = []
        self.unsubscribers: list[Callable[[], None]] = []
        self.state = {
            "policy_revision": POLICY_REVISION,
            "shadow_days": [],
            "candidate_cycles": 0,
            "contaminated_candidate_cycles": 0,
            "candidate_cycle_started_at": None,
            "candidate_cycle_peak_end": None,
            "candidate_cycle_contaminated": False,
            "comfort_blocks": 0,
            "decision_history": [],
            "planned_cheap_start": None,
            "planned_cheap_end": None,
            "planned_cheap_average": None,
            "planned_cheap_intervals": None,
            "planned_peak_start": None,
            "planned_peak_end": None,
            "planned_peak_average": None,
            "planned_peak_intervals": None,
            "confirmed_writes": 0,
        }

    async def async_start(self) -> None:
        stored = await self.store.async_load()
        if isinstance(stored, dict) and stored.get("policy_revision") == POLICY_REVISION:
            for key in self.state:
                if key in stored:
                    self.state[key] = stored[key]
        self.unsubscribers.append(
            self.coordinator.async_add_listener(self._schedule_evaluation)
        )
        if self.dhw_optimizer is not None:
            self.unsubscribers.append(
                self.dhw_optimizer.async_add_listener(self._schedule_evaluation)
            )
        if self.room_temperature_entities:
            self.unsubscribers.append(
                async_track_state_change_event(
                    self.hass,
                    self.room_temperature_entities,
                    self._room_temperature_changed,
                )
            )
        self.unsubscribers.append(
            async_track_time_interval(
                self.hass,
                self._async_interval,
                timedelta(minutes=5),
                name=DOMAIN + " floor-heating Shadow",
            )
        )
        await self.async_evaluate()

    async def async_stop(self) -> None:
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        await self._save()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return remove

    @callback
    def _notify_listeners(self) -> None:
        for listener in tuple(self.listeners):
            listener()

    @callback
    def _schedule_evaluation(self) -> None:
        self.hass.async_create_task(self.async_evaluate())

    @callback
    def _room_temperature_changed(self, _event) -> None:
        self._schedule_evaluation()

    async def _async_interval(self, now: datetime) -> None:
        await self.async_evaluate(now)

    async def _save(self) -> None:
        await self.store.async_save(self.state)

    def _room_temperature(
        self, observed_at: datetime
    ) -> tuple[float | None, str | None, tuple[str, ...]]:
        values = {}
        updates = []
        errors = []
        active_hvac_entities = []
        if not self.room_temperature_entities:
            errors.append(
                "Configure at least one occupied-room temperature entity; "
                "the Daikin garage sensor is diagnostic only."
            )
        for entity_id in self.room_temperature_entities:
            value, updated, reason = read_room_temperature_source(
                self.hass,
                entity_id,
                now=observed_at,
            )
            if reason is not None or value is None:
                errors.append(reason or f"{entity_id} is unavailable.")
                continue
            values[entity_id] = round(value, 3)
            if updated is not None:
                updates.append(updated)
            if entity_id.startswith("climate."):
                source_state = self.hass.states.get(entity_id)
                if (
                    source_state is not None
                    and source_state.state
                    not in ("off", STATE_UNKNOWN, STATE_UNAVAILABLE)
                ):
                    active_hvac_entities.append(entity_id)
        self.room_temperature_details.update({
            "indoor_temperature_entities": list(self.room_temperature_entities),
            "indoor_temperature_values_c": values,
            "indoor_temperature_oldest_update": (
                min(updates).isoformat() if updates else None
            ),
            "indoor_temperature_source_error": "; ".join(errors) or None,
            "active_room_hvac_entities": active_hvac_entities,
            "external_room_hvac_influence": bool(active_hvac_entities),
            "thermal_response_evidence_eligible": (
                bool(values)
                and not errors
                and not active_hvac_entities
            ),
        })
        if errors:
            return None, "; ".join(errors), tuple(active_hvac_entities)
        return min(values.values()), None, tuple(active_hvac_entities)

    def _observation(
        self, status: dict, observed_at: datetime
    ) -> FloorHeatingObservation:
        heating = status.get("heating")
        if not isinstance(heating, dict):
            return FloorHeatingObservation(
                None,
                None,
                None,
                None,
                None,
                "Fresh Daikin space-heating data is unavailable.",
            )
        states = heating.get("states", {})
        operations = heating.get("operations", {})
        sensors = heating.get("sensors", {})
        daikin_indoor = _number(sensors.get("IndoorTemperature"))
        self.room_temperature_details["daikin_indoor_temperature_c"] = daikin_indoor
        indoor, indoor_reason, active_room_hvac = self._room_temperature(
            observed_at
        )
        outdoor = _number(sensors.get("OutdoorTemperature"))
        leaving = _number(
            sensors.get(
                "LeavingWaterTemperatureCurrent",
                sensors.get("LeavingWaterTemperature"),
            )
        )
        offset = _number(operations.get("LeavingWaterTemperatureOffsetHeating"))
        power = operations.get("Power")
        enabled = power == "on" if power in ("on", "standby") else None
        reason = None
        if not isinstance(states, dict) or any(states.get(flag) is not False for flag in FLAGS):
            reason = "A Daikin safety flag is active or unknown; floor Shadow yielded."
        elif states.get("TargetTemperatureOverruledState") not in (None, False):
            reason = "Daikin is temporarily overriding the space-heating target."
        elif status.get("control_mode") != "LWT control":
            reason = "Floor Shadow requires freshly verified LWT control."
        elif operations.get("OperationMode") != "heating":
            reason = "Daikin is not in heating operation mode."
        elif enabled is not True:
            reason = "The space-heating circuit is in standby or its state is unknown."
        external_influence_reason = None
        if active_room_hvac:
            external_influence_reason = (
                "Room-level HVAC is enabled for "
                + ", ".join(active_room_hvac)
                + "; floor Shadow yields to external room control."
            )
        return FloorHeatingObservation(
            indoor,
            outdoor,
            leaving,
            offset,
            enabled,
            reason,
            indoor_reason,
            external_influence_reason,
        )

    def _clear_plan(self) -> None:
        for key in (
            "planned_cheap_start",
            "planned_cheap_end",
            "planned_cheap_average",
            "planned_cheap_intervals",
            "planned_peak_start",
            "planned_peak_end",
            "planned_peak_average",
            "planned_peak_intervals",
        ):
            self.state[key] = None

    def _stored_window(self, prefix: str) -> PriceWindowChoice | None:
        start = _instant(self.state.get(prefix + "_start"))
        end = _instant(self.state.get(prefix + "_end"))
        count = self.state.get(prefix + "_intervals")
        try:
            average = Decimal(str(self.state.get(prefix + "_average")))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if (
            start is None
            or end is None
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or not average.is_finite()
        ):
            return None
        return PriceWindowChoice(
            start.astimezone(UTC),
            end.astimezone(UTC),
            average,
            count,
        )

    def _stored_plan(self, now: datetime) -> FloorShiftPlan | None:
        cheap = self._stored_window("planned_cheap")
        peak = self._stored_window("planned_peak")
        if cheap is None or peak is None or peak.end <= now.astimezone(UTC):
            self._clear_plan()
            return None
        return FloorShiftPlan(cheap, peak)

    def _store_plan(self, plan: FloorShiftPlan) -> None:
        for prefix, window in (
            ("planned_cheap", plan.cheap_window),
            ("planned_peak", plan.peak_window),
        ):
            self.state[prefix + "_start"] = window.start.isoformat()
            self.state[prefix + "_end"] = window.end.isoformat()
            self.state[prefix + "_average"] = format(window.average, "f")
            self.state[prefix + "_intervals"] = window.interval_count

    def _update_candidate_evidence(
        self,
        now: datetime,
        decision: FloorHeatingDecision,
    ) -> None:
        active_peak_end = _instant(self.state.get("candidate_cycle_peak_end"))
        if active_peak_end is not None and now >= active_peak_end.astimezone(UTC):
            if self.state["candidate_cycle_contaminated"]:
                self.state["contaminated_candidate_cycles"] += 1
            else:
                self.state["candidate_cycles"] += 1
            self.state["candidate_cycle_started_at"] = None
            self.state["candidate_cycle_peak_end"] = None
            self.state["candidate_cycle_contaminated"] = False
            active_peak_end = None

        evidence_eligible = self.room_temperature_details[
            "thermal_response_evidence_eligible"
        ]
        if (
            active_peak_end is None
            and decision.state == "preheat"
            and decision.plan is not None
        ):
            self.state["candidate_cycle_started_at"] = now.isoformat()
            self.state["candidate_cycle_peak_end"] = (
                decision.plan.peak_window.end.isoformat()
            )
            self.state["candidate_cycle_contaminated"] = not evidence_eligible
            active_peak_end = decision.plan.peak_window.end

        if (
            active_peak_end is not None
            and (
                not evidence_eligible
                or decision.state
                in {"yield", "unavailable", "deferred", "comfort_guard"}
            )
        ):
            self.state["candidate_cycle_contaminated"] = True

    def _record(self, now: datetime, decision: FloorHeatingDecision) -> None:
        self._update_candidate_evidence(now, decision)
        previous = self.decision
        if (
            previous is not None
            and previous.state == decision.state
            and previous.actual_offset == decision.actual_offset
            and previous.proposed_offset == decision.proposed_offset
            and previous.aggressive_candidate_offset
            == decision.aggressive_candidate_offset
            and previous.reason == decision.reason
            and previous.plan == decision.plan
            and previous.dhw_state == decision.dhw_state
        ):
            return
        if decision.state == "comfort_guard" and (
            previous is None or previous.state != "comfort_guard"
        ):
            self.state["comfort_blocks"] += 1
        record = {
            "observed_at": now.isoformat(),
            "state": decision.state,
            "reason": decision.reason,
            "actual_offset_c": decision.actual_offset,
            "proposed_offset_c": decision.proposed_offset,
            "aggressive_candidate_offset_c": decision.aggressive_candidate_offset,
            "indoor_temperature_c": decision.indoor_temperature,
            "current_price_eur_kwh": (
                format(decision.current_price, "f")
                if decision.current_price is not None else None
            ),
            "price_gate": decision.price_gate,
            "dhw_state": decision.dhw_state,
            "compressor_conflict": decision.compressor_conflict,
            "write_sent": False,
        }
        self.state["decision_history"] = (
            self.state["decision_history"] + [record]
        )[-DECISION_HISTORY_LIMIT:]
        self.hass.bus.async_fire(EVENT_FLOOR_SHADOW_DECISION, record)

    async def async_evaluate(self, now: datetime | None = None) -> None:
        async with self.lock:
            observed_at = (now or dt_util.utcnow()).astimezone(UTC)
            try:
                status = await self.devices.get_daikin_status()
                observation = self._observation(status, observed_at)
                planned = self._stored_plan(observed_at)
                dhw_decision = (
                    self.dhw_optimizer.decision
                    if self.dhw_optimizer is not None else None
                )
                decision = plan_floor_heating(
                    now=observed_at,
                    policy=self.policy,
                    observation=observation,
                    intervals=published_intervals(self.coordinator),
                    planned=planned,
                    dhw_state=dhw_decision.state if dhw_decision else None,
                    dhw_proposed_target=(
                        dhw_decision.desired_target if dhw_decision else None
                    ),
                )
                if planned is None and decision.plan is not None:
                    self._store_plan(decision.plan)
                local_day = observed_at.astimezone(TALLINN).date().isoformat()
                if local_day not in self.state["shadow_days"]:
                    self.state["shadow_days"] = (
                        self.state["shadow_days"] + [local_day]
                    )[-45:]
                self._record(observed_at, decision)
                self.decision = decision
            except (HomeAssistantError, TimeoutError, ValueError, KeyError) as error:
                self.decision = FloorHeatingDecision(
                    state="unavailable",
                    reason=str(error),
                    actual_offset=None,
                    proposed_offset=None,
                    aggressive_candidate_offset=None,
                    indoor_temperature=None,
                    outdoor_temperature=None,
                    leaving_water_temperature=None,
                    current_price=None,
                    plan=None,
                    price_delta=None,
                    price_delta_percent=None,
                    price_gate=False,
                    data_complete=False,
                    dhw_state=None,
                    dhw_proposed_target=None,
                    compressor_conflict=False,
                )
            await self._save()
            self._notify_listeners()

    @property
    def attributes(self) -> dict:
        if self.decision is None:
            return {
                "mode": FLOOR_SHADOW_MODE,
                "reason": "Floor Shadow has not evaluated yet.",
                "write_allowed": False,
                "shadow_zero_writes": True,
            }
        result = self.decision.attributes()
        result.update({
            "policy_revision": POLICY_REVISION,
            "native_minimum_offset_c": FLOOR_NATIVE_MIN_OFFSET,
            "native_maximum_offset_c": FLOOR_NATIVE_MAX_OFFSET,
            "primary_candidate_minimum_offset_c": self.policy.minimum_offset,
            "primary_candidate_maximum_offset_c": self.policy.neutral_offset,
            "configured_aggressive_candidate_offset_c": self.policy.preheat_offset,
            "minimum_offset_c": self.policy.minimum_offset,
            "neutral_offset_c": self.policy.neutral_offset,
            "preheat_offset_c": self.policy.preheat_offset,
            "comfort_floor_c": self.policy.comfort_floor,
            "preheat_ceiling_c": self.policy.preheat_ceiling,
            "window_minutes": self.policy.window_minutes,
            "planning_horizon_hours": int(self.policy.horizon.total_seconds() // 3600),
            "planning_basis": "complete_tallinn_delivery_days",
            "maximum_shift_hours": int(self.policy.maximum_shift.total_seconds() // 3600),
            "observed_days": len(self.state["shadow_days"]),
            "candidate_cycles": self.state["candidate_cycles"],
            "contaminated_candidate_cycles": self.state[
                "contaminated_candidate_cycles"
            ],
            "candidate_cycle_in_progress": (
                self.state["candidate_cycle_peak_end"] is not None
            ),
            "candidate_cycle_contaminated": self.state[
                "candidate_cycle_contaminated"
            ],
            "comfort_blocks": self.state["comfort_blocks"],
            "thermal_response_evidence_eligible": self.room_temperature_details[
                "thermal_response_evidence_eligible"
            ],
            "confirmed_writes": self.state["confirmed_writes"],
            "pilot_eligible": False,
            "pilot_eligibility_reason": (
                "Write control remains disabled until at least 14 observed days, "
                "five complete candidate cycles, thermal-lag review and explicit "
                "room-comfort bounds are approved."
            ),
            "solar_dispatch_available": False,
            "solar_dispatch_reason": (
                "Fresh signed net-grid power is unavailable; PV generation alone "
                "cannot prove household surplus."
            ),
            **self.room_temperature_details,
            "recent_decisions": self.state["decision_history"][-12:],
        })
        return result
