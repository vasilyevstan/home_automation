"""Deterministic, zero-write floor-heating price-shift evaluator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
from typing import Callable, Iterable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, HomeAssistant, callback
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
    FLOOR_COAST_ROOM_SHIFT_LIMITS,
    FLOOR_HORIZON_HOURS,
    FLOOR_MAX_OUTDOOR_TEMPERATURE,
    FLOOR_MAX_SHIFT_HOURS,
    FLOOR_MODE_ACTIVE,
    FLOOR_MODE_PAUSED,
    FLOOR_MODE_SHADOW,
    FLOOR_MODES,
    FLOOR_MIN_LEAVING_WATER_TEMPERATURE,
    FLOOR_MIN_OFFSET,
    FLOOR_NATIVE_MAX_OFFSET,
    FLOOR_NATIVE_MIN_OFFSET,
    FLOOR_NEUTRAL_OFFSET,
    FLOOR_PREHEAT_CEILING,
    FLOOR_PREHEAT_OFFSET,
    FLOOR_PREHEAT_ROOM_SHIFT_LIMITS,
    FLOOR_PRICE_DELTA,
    FLOOR_PRICE_PERCENT,
    FLOOR_PRICE_SHIFT_TIERS,
    FLOOR_ROOM_TEMPERATURE_MAX_AGE_MINUTES,
    FLOOR_SHADOW_MODE,
    FLOOR_SHIFT_DELTA,
    FLOOR_WINDOW_MINUTES,
)
from .controls import DaikinPreDispatchConflict, EnergyDevices, FLAGS
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
POLICY_REVISION = 7
DECISION_HISTORY_LIMIT = 96
RECENT_DECISIONS_ATTRIBUTE_LIMIT = 4
NOTIFICATION_ID = DOMAIN + "_floor_heating_fault"
ACTIVE_STATES = {"preheat", "coast", "comfort_guard"}
ACCEPTED_NATIVE_BASELINES = frozenset((-2.0, 0.0))
NATIVE_TRANSITION_HISTORY_LIMIT = 24
NATIVE_TRANSITION_STABILITY_WINDOW = timedelta(hours=1)
NATIVE_TRANSITION_STABILITY_LIMIT = 3


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
    shift_delta: float = FLOOR_SHIFT_DELTA
    comfort_floor: float = FLOOR_COMFORT_FLOOR
    preheat_ceiling: float = FLOOR_PREHEAT_CEILING
    maximum_outdoor_temperature: float = FLOOR_MAX_OUTDOOR_TEMPERATURE
    minimum_leaving_water_temperature: float = FLOOR_MIN_LEAVING_WATER_TEMPERATURE
    window_minutes: int = FLOOR_WINDOW_MINUTES
    horizon: timedelta = timedelta(hours=FLOOR_HORIZON_HOURS)
    maximum_shift: timedelta = timedelta(hours=FLOOR_MAX_SHIFT_HOURS)
    price_delta: Decimal = Decimal(str(FLOOR_PRICE_DELTA))
    price_percent: Decimal = Decimal(str(FLOOR_PRICE_PERCENT)) / Decimal(100)
    price_shift_tiers: tuple[tuple[float, float, float], ...] = (
        FLOOR_PRICE_SHIFT_TIERS
    )
    coast_room_shift_limits: tuple[tuple[float, float], ...] = (
        FLOOR_COAST_ROOM_SHIFT_LIMITS
    )
    preheat_room_shift_limits: tuple[tuple[float, float], ...] = (
        FLOOR_PREHEAT_ROOM_SHIFT_LIMITS
    )

    def __post_init__(self) -> None:
        if not self.minimum_offset <= self.neutral_offset < self.preheat_offset:
            raise HomeAssistantError("Floor offsets must satisfy minimum <= neutral < preheat.")
        if (
            self.minimum_offset < FLOOR_NATIVE_MIN_OFFSET
            or self.preheat_offset > FLOOR_NATIVE_MAX_OFFSET
        ):
            raise HomeAssistantError(
                "The floor optimizer must remain inside the native -10°C to +10°C range."
            )
        if self.shift_delta != 4:
            raise HomeAssistantError(
                "The floor optimizer keeps 4°C as the minimum material shift."
            )
        if tuple(shift for _, _, shift in self.price_shift_tiers) != (
            4,
            6,
            8,
            10,
        ):
            raise HomeAssistantError("The adaptive floor price tiers are invalid.")
        if self.comfort_floor >= self.preheat_ceiling:
            raise HomeAssistantError("The floor comfort threshold must be below the preheat ceiling.")
        if self.window_minutes <= 0 or self.window_minutes % 15:
            raise HomeAssistantError("The floor shift window must use whole 15-minute intervals.")
        if self.horizon < timedelta(hours=12) or self.maximum_shift <= timedelta(0):
            raise HomeAssistantError("The floor planning horizon or shift span is invalid.")


def _price_shift_magnitude(
    policy: FloorHeatingPolicy,
    delta: Decimal | None,
    relative: Decimal | None,
) -> float:
    if delta is None or relative is None:
        return 0
    selected = 0.0
    for absolute_gate, percent_gate, shift in policy.price_shift_tiers:
        if (
            delta >= Decimal(str(absolute_gate))
            and relative >= Decimal(str(percent_gate)) / Decimal(100)
        ):
            selected = shift
    return selected


def _room_shift_limit(
    temperature: float,
    limits: tuple[tuple[float, float], ...],
) -> float:
    for maximum_temperature, shift in limits:
        if temperature <= maximum_temperature:
            return shift
    return 0


def _round_toward_baseline(target: float, baseline: float) -> float:
    if target < baseline:
        return float(math.ceil(target))
    if target > baseline:
        return float(math.floor(target))
    return float(target)


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
    busy = dhw_state in {
        "economic_storage",
        "readiness_charge",
        "recovery",
    }

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
    if not (
        FLOOR_NATIVE_MIN_OFFSET
        <= observation.actual_offset
        <= FLOOR_NATIVE_MAX_OFFSET
    ):
        return decision(
            "yield",
            "The native/manual LWT offset is outside the native -10°C to +10°C range.",
            observation.actual_offset,
        )
    comfort_guard = observation.indoor_temperature <= policy.comfort_floor
    if observation.external_influence_reason is not None:
        if comfort_guard:
            return decision(
                "comfort_guard",
                "Indoor temperature is at the 20°C guard; restore at least the neutral native offset while external room HVAC is active.",
                max(observation.actual_offset, policy.neutral_offset),
            )
        return decision(
            "yield",
            observation.external_influence_reason,
            observation.actual_offset,
        )
    if not complete or current_price is None:
        if comfort_guard:
            return decision(
                "comfort_guard",
                "Indoor temperature is at the 20°C guard; restore at least the neutral native offset.",
                max(observation.actual_offset, policy.neutral_offset),
            )
        return decision(
            "unavailable",
            "Complete published all-in prices are required through the current "
            "Tallinn delivery day.",
            observation.actual_offset,
        )
    if plan is None:
        if comfort_guard:
            return decision(
                "comfort_guard",
                "Indoor temperature is at the 20°C guard; restore at least the neutral native offset.",
                max(observation.actual_offset, policy.neutral_offset),
            )
        return decision(
            "hold",
            "No complete cheap-before-peak floor-shift pair exists in the planning horizon.",
            observation.actual_offset,
        )
    now_utc = now.astimezone(UTC)
    cheap_active = plan.cheap_window.start <= now_utc < plan.cheap_window.end
    peak_active = plan.peak_window.start <= now_utc < plan.peak_window.end
    if (
        observation.outdoor_temperature >= policy.maximum_outdoor_temperature
        or observation.leaving_water_temperature
        < policy.minimum_leaving_water_temperature
    ):
        if comfort_guard:
            return decision(
                "comfort_guard",
                "Indoor temperature is at the 20°C guard; restore at least the neutral native offset.",
                max(observation.actual_offset, policy.neutral_offset),
            )
        return decision(
            "hold",
            "Outdoor and leaving-water conditions do not provide evidence of useful space-heating demand.",
            observation.actual_offset,
        )
    shift = _price_shift_magnitude(policy, delta, relative)
    if cheap_active:
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
        room_limit = _room_shift_limit(
            observation.indoor_temperature,
            policy.preheat_room_shift_limits,
        )
        aggressive = min(
            policy.preheat_offset,
            observation.actual_offset + shift,
        )
        if room_limit <= 0:
            return decision(
                "preheat_limited",
                "The paired cheap block is active, but the room is already at or "
                "above the 21°C preheat ceiling; preserve the native offset "
                "instead of adding heat into closing thermostatic valves.",
                observation.actual_offset,
                aggressive_candidate_offset=aggressive,
            )
        proposed = min(
            policy.preheat_offset,
            observation.actual_offset + min(shift, room_limit),
        )
        proposed = _round_toward_baseline(
            proposed,
            observation.actual_offset,
        )
        if proposed == observation.actual_offset:
            return decision(
                "preheat_limited",
                "Room-temperature headroom leaves no useful positive LWT-offset shift.",
                observation.actual_offset,
                aggressive_candidate_offset=aggressive,
            )
        return decision(
            "preheat",
            "The current quarter is inside the selected cheap block; apply the "
            f"adaptive +{proposed - observation.actual_offset:g}°C overlay "
            "allowed by price strength and room-temperature headroom.",
            proposed,
            aggressive_candidate_offset=aggressive,
        )
    if peak_active:
        if not gate:
            return decision(
                "hold",
                "The cheap-to-peak all-in price advantage does not pass both materiality gates.",
                observation.actual_offset,
            )
        if comfort_guard:
            return decision(
                "comfort_guard",
                "Indoor temperature is at the 20°C guard; no price-driven setback is proposed.",
                max(observation.actual_offset, policy.neutral_offset),
            )
        room_limit = _room_shift_limit(
            observation.indoor_temperature,
            policy.coast_room_shift_limits,
        )
        if observation.indoor_temperature > policy.coast_room_shift_limits[-1][0]:
            room_limit = policy.price_shift_tiers[-1][2]
        lwt_limit = max(
            0,
            observation.leaving_water_temperature
            - policy.minimum_leaving_water_temperature,
        )
        aggressive = max(
            policy.minimum_offset,
            observation.actual_offset - shift,
        )
        proposed = max(
            policy.minimum_offset,
            observation.actual_offset - min(shift, room_limit, lwt_limit),
        )
        proposed = _round_toward_baseline(
            proposed,
            observation.actual_offset,
        )
        if proposed == observation.actual_offset:
            return decision(
                "coast_limited",
                "Room-temperature or projected leaving-water headroom leaves "
                "no safe negative LWT-offset shift.",
                observation.actual_offset,
                aggressive_candidate_offset=aggressive,
            )
        return decision(
            "coast",
            "The current quarter is inside the paired expensive block; apply the "
            f"adaptive {proposed - observation.actual_offset:g}°C overlay "
            "allowed by price strength, room headroom and the 25°C projected-LWT guard.",
            proposed,
            aggressive_candidate_offset=aggressive,
        )
    if comfort_guard:
        return decision(
            "comfort_guard",
            "Indoor temperature is at the 20°C guard; restore at least the neutral native offset.",
            max(observation.actual_offset, policy.neutral_offset),
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
        self.started = False
        self.extreme_protection = None
        self.mode = FLOOR_MODE_SHADOW
        self.decision: FloorHeatingDecision | None = None
        self.listeners: list[Callable[[], None]] = []
        self.unsubscribers: list[Callable[[], None]] = []
        self.external_influences: dict[str, str] = {}
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
            "owner_activation_approved_at": None,
            "active_started_at": None,
            "ownership": None,
            "pending_operation": None,
            "last_write_at": None,
            "fault": None,
            "manual_override": None,
            "activation_hold": False,
            "schedule_learning_hold_until": None,
            "schedule_learning_reason": None,
            "native_schedule_transitions": [],
            "last_native_offset": None,
            "qualified_opportunity_count": 0,
            "last_qualified_opportunity_plan_id": None,
            "last_qualified_opportunity_at": None,
        }

    async def async_start(self) -> None:
        stored = await self.store.async_load()
        if isinstance(stored, dict) and stored.get("policy_revision") in (
            4,
            5,
            6,
            POLICY_REVISION,
        ):
            stored_revision = stored.get("policy_revision")
            mode = stored.get("mode")
            if mode in FLOOR_MODES:
                self.mode = mode
            for key in self.state:
                if key in stored:
                    self.state[key] = stored[key]
            if stored_revision in (4, 5, 6):
                self.state["policy_revision"] = POLICY_REVISION
                self.mode = FLOOR_MODE_SHADOW
                self.state["activation_hold"] = False
                self.state["schedule_learning_hold_until"] = None
                self.state["schedule_learning_reason"] = None
                self.state["candidate_cycles"] = 0
                self.state["contaminated_candidate_cycles"] = 0
                self.state["candidate_cycle_started_at"] = None
                self.state["candidate_cycle_peak_end"] = None
                self.state["candidate_cycle_contaminated"] = False
                self.state["qualified_opportunity_count"] = 0
                self.state["last_qualified_opportunity_plan_id"] = None
                self.state["last_qualified_opportunity_at"] = None
                self._clear_plan()
            if (
                self.state.get("pending_operation") is not None
                and not self.state.get("fault")
            ):
                self.state["fault"] = (
                    "A persisted floor-offset command has an uncertain outcome. "
                    "Read fresh state, then use Resume after review."
                )
            elif (
                self._ownership() is not None
                and not self.state.get("fault")
                and stored_revision in (4, 5, 6)
            ):
                self.state["fault"] = (
                    "The floor policy changed while an optimizer-owned offset "
                    "was active. Ownership was preserved in Shadow; reconcile "
                    "fresh native state before restoring or resuming."
                )
        self.started = True
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
        try:
            if self.state.get("ownership") is not None:
                await self.async_return_to_baseline(
                    pause=False,
                    evaluate=False,
                )
        finally:
            self.started = False
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
    def set_external_influence(
        self,
        source: str,
        active: bool,
        reason: str,
    ) -> None:
        if active:
            self.external_influences[source] = reason
        else:
            self.external_influences.pop(source, None)
        if self.started:
            self._schedule_evaluation()

    @callback
    def set_extreme_protection(self, protection) -> None:
        self.extreme_protection = protection

    @callback
    def _schedule_evaluation(self) -> None:
        self.hass.async_create_task(self.async_evaluate())

    @callback
    def _room_temperature_changed(self, _event) -> None:
        self._schedule_evaluation()

    async def _async_interval(self, now: datetime) -> None:
        await self.async_evaluate(now)

    async def _save(self) -> None:
        await self.store.async_save({"mode": self.mode, **self.state})

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
            "external_room_hvac_influence": bool(
                active_hvac_entities or self.external_influences
            ),
            "external_influence_reasons": list(
                self.external_influences.values()
            ),
            "thermal_response_evidence_eligible": (
                bool(values)
                and not errors
                and not active_hvac_entities
                and not self.external_influences
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
            reason = "A Daikin safety flag is active or unknown; the floor optimizer yielded."
        elif states.get("TargetTemperatureOverruledState") not in (None, False):
            reason = "Daikin is temporarily overriding the space-heating target."
        elif status.get("control_mode") != "LWT control":
            reason = "The floor optimizer requires freshly verified LWT control."
        elif operations.get("OperationMode") != "heating":
            reason = "Daikin is not in heating operation mode."
        elif enabled is not True:
            reason = "The space-heating circuit is in standby or its state is unknown."
        external_influence_reason = None
        if active_room_hvac:
            external_influence_reason = (
                "Room-level HVAC is enabled for "
                + ", ".join(active_room_hvac)
                + "; the floor optimizer yields to external room control."
            )
        elif self.external_influences:
            external_influence_reason = "; ".join(
                self.external_influences.values()
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
            and decision.state in {"preheat", "preheat_limited"}
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
                in {
                    "yield",
                    "unavailable",
                    "deferred",
                    "comfort_guard",
                    "schedule_learning_hold",
                }
            )
        ):
            self.state["candidate_cycle_contaminated"] = True

    def _opportunity_plan_id(
        self, decision: FloorHeatingDecision
    ) -> str | None:
        if decision.plan is None:
            return None
        return "|".join((
            decision.plan.cheap_window.start.isoformat(),
            decision.plan.cheap_window.end.isoformat(),
            decision.plan.peak_window.start.isoformat(),
            decision.plan.peak_window.end.isoformat(),
        ))

    def _record_optimization_opportunity(
        self, now: datetime, decision: FloorHeatingDecision
    ) -> None:
        plan_id = self._opportunity_plan_id(decision)
        if (
            plan_id is None
            or not decision.price_gate
            or decision.price_delta is None
            or decision.price_delta <= 0
        ):
            return
        if plan_id == self.state.get("last_qualified_opportunity_plan_id"):
            return
        count = self.state.get("qualified_opportunity_count")
        self.state["qualified_opportunity_count"] = (
            count + 1
            if isinstance(count, int) and not isinstance(count, bool)
            else 1
        )
        self.state["last_qualified_opportunity_plan_id"] = plan_id
        self.state["last_qualified_opportunity_at"] = now.isoformat()

    def _record(self, now: datetime, decision: FloorHeatingDecision) -> None:
        self._update_candidate_evidence(now, decision)
        self._record_optimization_opportunity(now, decision)
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
            "mode": self.mode,
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

    @property
    def pilot_eligible(self) -> bool:
        return (
            len(self.state["shadow_days"]) >= 14
            and self.state["candidate_cycles"] >= 5
            and not self.state.get("fault")
            and not self.state.get("manual_override")
        )

    def _ownership(self) -> dict | None:
        owner = self.state.get("ownership")
        if not isinstance(owner, dict):
            return None
        if (
            _number(owner.get("baseline_offset")) is None
            or _number(owner.get("target_offset")) is None
            or owner.get("phase") not in ACTIVE_STATES
        ):
            return None
        return owner

    def _extreme_block_reason(self, now: datetime) -> str | None:
        if self.extreme_protection is None:
            return None
        return self.extreme_protection.ordinary_control_block_reason(now)

    def _normalize_owned_decision(
        self,
        now: datetime,
        observation: FloorHeatingObservation,
        decision: FloorHeatingDecision,
    ) -> FloorHeatingDecision:
        owner = self._ownership()
        if owner is None or observation.actual_offset is None:
            return decision
        baseline = _number(owner.get("baseline_offset"))
        target = _number(owner.get("target_offset"))
        if baseline is None or target is None:
            return decision
        if observation.actual_offset != target:
            accepted = observation.actual_offset in ACCEPTED_NATIVE_BASELINES
            classification = (
                "accepted_schedule_transition"
                if accepted
                else "unexpected_owned_change"
            )
            unstable = self._record_native_transition(
                now=now,
                previous=target,
                current=observation.actual_offset,
                classification=classification,
                owned=True,
            )
            self.state["ownership"] = None
            self.state["candidate_cycle_contaminated"] = True
            if accepted and not unstable:
                hold_until = self._schedule_window_end(now, decision)
                self.state["schedule_learning_hold_until"] = (
                    hold_until.isoformat()
                )
                self.state["schedule_learning_reason"] = (
                    "Daikin's accepted native schedule changed the offset to "
                    f"{observation.actual_offset:g}°C. The optimizer preserved "
                    "it and will not reapply an overlay until the current "
                    "price window ends."
                )
                return replace(
                    decision,
                    state="schedule_learning_hold",
                    reason=str(self.state["schedule_learning_reason"]),
                    proposed_offset=observation.actual_offset,
                )
            self.state["manual_override"] = (
                "The native floor offset changed unexpectedly while the "
                f"optimizer owned {target:g}°C. The new "
                f"{observation.actual_offset:g}°C value was preserved and "
                "automatic floor writes returned to Shadow."
            )
            if unstable:
                self.state["manual_override"] = (
                    "Accepted native floor offsets changed repeatedly inside "
                    "one hour. The unstable schedule was preserved and "
                    "automatic floor writes returned to Shadow."
                )
            self.mode = FLOOR_MODE_SHADOW
            return replace(
                decision,
                state="yield",
                reason=self.state["manual_override"],
                proposed_offset=observation.actual_offset,
            )
        if decision.state == "yield":
            return replace(decision, proposed_offset=target)
        owner_phase = str(owner["phase"])
        if decision.state == owner_phase:
            return replace(decision, proposed_offset=target)
        if decision.state in ACTIVE_STATES:
            phase_end = self._owned_phase_window_end(
                now,
                decision,
                owner_phase,
            )
            if now.astimezone(UTC) < phase_end:
                self.state["schedule_learning_hold_until"] = (
                    phase_end.isoformat()
                )
                self.state["schedule_learning_reason"] = (
                    f"The owned {owner_phase} episode ended early when "
                    f"{decision.state} took priority. The optimizer restored "
                    "the native baseline and will not reacquire during the same "
                    "price window."
                )
            return replace(
                decision,
                state="hold",
                reason=(
                    f"The owned {owner_phase} episode ended; restore its exact "
                    "native baseline before another floor phase can acquire control."
                ),
                proposed_offset=baseline,
            )
        phase_end = self._owned_phase_window_end(
            now,
            decision,
            owner_phase,
        )
        if now.astimezone(UTC) < phase_end:
            self.state["schedule_learning_hold_until"] = phase_end.isoformat()
            self.state["schedule_learning_reason"] = (
                f"The owned {owner_phase} episode exited early because "
                f"{decision.reason} The optimizer restored the native baseline "
                "and will not reacquire during the same price window."
            )
        return replace(decision, proposed_offset=baseline)

    def _owned_phase_window_end(
        self,
        now: datetime,
        decision: FloorHeatingDecision,
        phase: str,
    ) -> datetime:
        if decision.plan is not None:
            if phase == "preheat":
                return decision.plan.cheap_window.end
            if phase == "coast":
                return decision.plan.peak_window.end
            if phase == "comfort_guard":
                return self._schedule_window_end(now, decision)
        return now.astimezone(UTC)

    def _schedule_window_end(
        self,
        now: datetime,
        decision: FloorHeatingDecision,
    ) -> datetime:
        now_utc = now.astimezone(UTC)
        if decision.plan is not None:
            for window in (
                decision.plan.cheap_window,
                decision.plan.peak_window,
            ):
                if window.start <= now_utc < window.end:
                    return window.end
            if now_utc < decision.plan.peak_window.end:
                return decision.plan.peak_window.end
        return now_utc + MARKET_INTERVAL

    def _record_native_transition(
        self,
        *,
        now: datetime,
        previous: float,
        current: float,
        classification: str,
        owned: bool,
    ) -> bool:
        records = list(self.state["native_schedule_transitions"])
        records.append({
            "observed_at": now.isoformat(),
            "local_time": now.astimezone(TALLINN).isoformat(),
            "previous_offset_c": previous,
            "native_offset_c": current,
            "classification": classification,
            "ownership_released": owned,
        })
        self.state["native_schedule_transitions"] = records[
            -NATIVE_TRANSITION_HISTORY_LIMIT:
        ]
        self.state["last_native_offset"] = current
        accepted_recent = 0
        for record in reversed(records):
            observed_at = _instant(record.get("observed_at"))
            if (
                observed_at is None
                or now.astimezone(UTC) - observed_at.astimezone(UTC)
                > NATIVE_TRANSITION_STABILITY_WINDOW
            ):
                break
            if record.get("classification") in (
                "accepted_schedule_transition",
                "accepted_native_baseline",
            ):
                accepted_recent += 1
        return accepted_recent >= NATIVE_TRANSITION_STABILITY_LIMIT

    def _normalize_schedule_learning(
        self,
        now: datetime,
        observation: FloorHeatingObservation,
        decision: FloorHeatingDecision,
    ) -> FloorHeatingDecision:
        current = observation.actual_offset
        if current is None:
            return decision
        previous = _number(self.state.get("last_native_offset"))
        if previous is None:
            self.state["last_native_offset"] = current
        elif previous != current and self._ownership() is None:
            accepted = current in ACCEPTED_NATIVE_BASELINES
            unstable = self._record_native_transition(
                now=now,
                previous=previous,
                current=current,
                classification=(
                    "accepted_native_baseline"
                    if accepted
                    else "unexpected_native_value"
                ),
                owned=False,
            )
            if self.mode == FLOOR_MODE_ACTIVE and (
                not accepted or unstable
            ):
                self.mode = FLOOR_MODE_SHADOW
                self.state["manual_override"] = (
                    "The native floor schedule became unstable; its latest "
                    "value was preserved and automatic writes returned to "
                    "Shadow."
                    if unstable
                    else (
                        f"The unowned native floor offset changed to "
                        f"{current:g}°C outside the accepted -2/0°C schedule. "
                        "It was preserved and automatic writes returned to "
                        "Shadow."
                    )
                )
                return replace(
                    decision,
                    state="yield",
                    reason=str(self.state["manual_override"]),
                    proposed_offset=current,
                )
        hold_until = _instant(
            self.state.get("schedule_learning_hold_until")
        )
        if hold_until is not None:
            if now.astimezone(UTC) < hold_until.astimezone(UTC):
                return replace(
                    decision,
                    state="schedule_learning_hold",
                    reason=str(
                        self.state.get("schedule_learning_reason")
                        or "The optimizer is learning the native schedule."
                    ),
                    proposed_offset=current,
                )
            self.state["schedule_learning_hold_until"] = None
            self.state["schedule_learning_reason"] = None
        if self.mode == FLOOR_MODE_ACTIVE and self.state["activation_hold"]:
            self.state["activation_hold"] = False
            if decision.state in ACTIVE_STATES:
                hold_until = self._schedule_window_end(now, decision)
                self.state["schedule_learning_hold_until"] = (
                    hold_until.isoformat()
                )
                self.state["schedule_learning_reason"] = (
                    "Active mode started from fresh ownerless state during a "
                    "candidate window. The current window is observation-only."
                )
                return replace(
                    decision,
                    state="schedule_learning_hold",
                    reason=str(self.state["schedule_learning_reason"]),
                    proposed_offset=current,
                )
        if (
            self.mode == FLOOR_MODE_ACTIVE
            and self._ownership() is None
            and current not in ACCEPTED_NATIVE_BASELINES
        ):
            self.mode = FLOOR_MODE_SHADOW
            self.state["manual_override"] = (
                f"The ownerless native floor offset is {current:g}°C, outside "
                "the accepted -2/0°C schedule baselines. It was preserved and "
                "automatic writes returned to Shadow."
            )
            return replace(
                decision,
                state="yield",
                reason=str(self.state["manual_override"]),
                proposed_offset=current,
            )
        return decision

    async def _dispatch_offset(
        self,
        *,
        now: datetime,
        previous: float,
        target: float,
        baseline: float,
        phase: str,
        restoring: bool,
    ) -> bool:
        block_reason = self._extreme_block_reason(now)
        if (
            block_reason is not None
            or self.state.get("pending_operation") is not None
        ):
            return False
        pending = {
            "previous_offset": previous,
            "target_offset": target,
            "baseline_offset": baseline,
            "phase": phase,
            "restoring": restoring,
            "prepared_at": now.isoformat(),
        }
        self.state["pending_operation"] = pending
        await self._save()
        try:
            result = await self.devices.control_daikin(
                {
                    "operation": "heating_offset",
                    "value": target,
                    "expected_previous": previous,
                },
                Context(),
            )
        except DaikinPreDispatchConflict:
            self.state["pending_operation"] = None
            await self._save()
            return False
        except (HomeAssistantError, TimeoutError) as error:
            self.state["fault"] = (
                "A guarded floor-offset command failed and may have applied. "
                "Read fresh state, then use Resume after review. " + str(error)
            )
            persistent_notification.async_create(
                self.hass,
                self.state["fault"],
                "Floor-heating optimizer paused",
                NOTIFICATION_ID,
            )
            await self._save()
            return False
        confirmed = _number(result.get("confirmed"))
        if confirmed != target:
            self.state["fault"] = (
                "The guarded floor-offset command returned an unexpected "
                "confirmation; inspect fresh state before resuming."
            )
            persistent_notification.async_create(
                self.hass,
                self.state["fault"],
                "Floor-heating optimizer paused",
                NOTIFICATION_ID,
            )
            await self._save()
            return False
        self.state["pending_operation"] = None
        self.state["last_write_at"] = now.isoformat()
        self.state["last_native_offset"] = target
        self.state["confirmed_writes"] += int(
            result.get("changed") is True
        )
        if restoring:
            self.state["ownership"] = None
        else:
            self.state["ownership"] = {
                "baseline_offset": baseline,
                "target_offset": target,
                "phase": phase,
                "acquired_at": (
                    self._ownership() or {}
                ).get("acquired_at", now.isoformat()),
                "updated_at": now.isoformat(),
            }
        if self.state["decision_history"]:
            self.state["decision_history"][-1]["write_sent"] = bool(
                result.get("changed")
            )
        return True

    async def _apply_decision(
        self,
        now: datetime,
        observation: FloorHeatingObservation,
        decision: FloorHeatingDecision,
    ) -> None:
        if (
            self.mode != FLOOR_MODE_ACTIVE
            or self.state.get("fault")
            or self.state.get("manual_override")
            or self.state.get("pending_operation") is not None
            or observation.actual_offset is None
            or decision.proposed_offset is None
        ):
            return
        current = observation.actual_offset
        target = decision.proposed_offset
        owner = self._ownership()
        if owner is None:
            if decision.state not in ACTIVE_STATES or target == current:
                return
            await self._dispatch_offset(
                now=now,
                previous=current,
                target=target,
                baseline=current,
                phase=decision.state,
                restoring=False,
            )
            return
        baseline = _number(owner.get("baseline_offset"))
        if baseline is None:
            return
        restoring = decision.state not in ACTIVE_STATES
        if target == current:
            if restoring and target == baseline:
                self.state["ownership"] = None
            return
        await self._dispatch_offset(
            now=now,
            previous=current,
            target=target,
            baseline=baseline,
            phase=(
                decision.state
                if decision.state in ACTIVE_STATES
                else str(owner["phase"])
            ),
            restoring=restoring,
        )

    async def async_evaluate(self, now: datetime | None = None) -> None:
        async with self.lock:
            observed_at = (now or dt_util.utcnow()).astimezone(UTC)
            try:
                status = await self.devices.get_daikin_status()
                observation = self._observation(status, observed_at)
                if (
                    observation.override_reason is None
                    and self.extreme_protection is not None
                ):
                    block_reason = (
                        self.extreme_protection.ordinary_control_block_reason(
                            observed_at
                        )
                    )
                    if block_reason is not None:
                        observation = replace(
                            observation,
                            override_reason=block_reason,
                        )
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
                if self.state.get("fault"):
                    decision = replace(
                        decision,
                        state="fault",
                        reason=str(self.state["fault"]),
                        proposed_offset=observation.actual_offset,
                    )
                elif self.state.get("manual_override"):
                    decision = replace(
                        decision,
                        state="yield",
                        reason=str(self.state["manual_override"]),
                        proposed_offset=observation.actual_offset,
                    )
                else:
                    decision = self._normalize_owned_decision(
                        observed_at,
                        observation,
                        decision,
                    )
                    if not self.state.get("manual_override"):
                        decision = self._normalize_schedule_learning(
                            observed_at,
                            observation,
                            decision,
                        )
                local_day = observed_at.astimezone(TALLINN).date().isoformat()
                if local_day not in self.state["shadow_days"]:
                    self.state["shadow_days"] = (
                        self.state["shadow_days"] + [local_day]
                    )[-45:]
                self._record(observed_at, decision)
                self.decision = decision
                await self._apply_decision(
                    observed_at,
                    observation,
                    decision,
                )
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

    async def async_set_mode(
        self,
        mode: str,
        *,
        owner_approved: bool = False,
    ) -> None:
        if mode not in FLOOR_MODES:
            raise HomeAssistantError("Unknown floor-heating optimizer mode.")
        approved = self.state.get("owner_activation_approved_at")
        if (
            mode == FLOOR_MODE_ACTIVE
            and not self.pilot_eligible
            and not approved
            and not owner_approved
        ):
            raise HomeAssistantError(
                "Active floor shifting requires 14 observed days, five clean "
                "candidate cycles, or an explicit owner-approved activation."
            )
        if mode == FLOOR_MODE_ACTIVE:
            status = await self.devices.get_daikin_status()
            observation = self._observation(
                status,
                dt_util.utcnow().astimezone(UTC),
            )
            if observation.override_reason is not None:
                raise HomeAssistantError(observation.override_reason)
            if observation.actual_offset is None:
                raise HomeAssistantError(
                    "A fresh native floor offset is required before activation."
                )
            if (
                observation.actual_offset
                not in ACCEPTED_NATIVE_BASELINES
            ):
                raise HomeAssistantError(
                    "Owner-approved Active mode must start from a fresh "
                    "ownerless -2°C or 0°C native schedule baseline."
                )
            self.state["last_native_offset"] = (
                observation.actual_offset
            )
        if owner_approved:
            self.state["owner_activation_approved_at"] = (
                dt_util.utcnow().astimezone(UTC).isoformat()
            )
            self.state["manual_override"] = None
        if mode != FLOOR_MODE_ACTIVE and self._ownership() is not None:
            await self.async_return_to_baseline(
                pause=False,
                evaluate=False,
            )
        async with self.lock:
            self.mode = mode
            if mode == FLOOR_MODE_ACTIVE and self.state["active_started_at"] is None:
                self.state["active_started_at"] = (
                    dt_util.utcnow().astimezone(UTC).isoformat()
                )
            if mode == FLOOR_MODE_ACTIVE:
                self.state["activation_hold"] = True
                self.state["ownership"] = None
                self.state["schedule_learning_hold_until"] = None
                self.state["schedule_learning_reason"] = None
            await self._save()
        await self.async_evaluate()

    async def async_activate_owner_approved(self) -> None:
        await self.async_set_mode(
            FLOOR_MODE_ACTIVE,
            owner_approved=True,
        )

    async def async_resume_after_review(self) -> None:
        status = await self.devices.get_daikin_status()
        observed_at = dt_util.utcnow().astimezone(UTC)
        observation = self._observation(status, observed_at)
        if observation.actual_offset is None:
            raise HomeAssistantError(
                "A fresh native floor offset is required before resume."
            )
        async with self.lock:
            pending = self.state.get("pending_operation")
            if isinstance(pending, dict):
                previous = _number(pending.get("previous_offset"))
                target = _number(pending.get("target_offset"))
                baseline = _number(pending.get("baseline_offset"))
                if observation.actual_offset == target and baseline is not None:
                    if pending.get("restoring"):
                        self.state["ownership"] = None
                    else:
                        self.state["ownership"] = {
                            "baseline_offset": baseline,
                            "target_offset": target,
                            "phase": pending.get("phase"),
                            "acquired_at": pending.get("prepared_at"),
                            "updated_at": observed_at.isoformat(),
                        }
                    self.state["pending_operation"] = None
                elif observation.actual_offset == previous:
                    self.state["pending_operation"] = None
                else:
                    self.mode = FLOOR_MODE_SHADOW
                    self.state["ownership"] = None
                    self.state["pending_operation"] = None
                    self.state["manual_override"] = (
                        "Fresh state after an uncertain command matched neither "
                        "the previous nor requested offset; the external value "
                        "was preserved."
                    )
                    await self._save()
                    raise HomeAssistantError(
                        self.state["manual_override"]
                    )
            owner = self._ownership()
            if owner is not None:
                target = _number(owner.get("target_offset"))
                baseline = _number(owner.get("baseline_offset"))
                if observation.actual_offset == baseline:
                    self.state["ownership"] = None
                elif observation.actual_offset != target:
                    raise HomeAssistantError(
                        "The native floor offset no longer matches optimizer "
                        "ownership; the external value was preserved."
                    )
            self.state["fault"] = None
            self.state["manual_override"] = None
            await self._save()
        persistent_notification.async_dismiss(
            self.hass,
            NOTIFICATION_ID,
        )
        await self.async_evaluate(observed_at)

    async def async_return_to_baseline(
        self,
        *,
        pause: bool = True,
        evaluate: bool = True,
    ) -> None:
        status = await self.devices.get_daikin_status()
        observed_at = dt_util.utcnow().astimezone(UTC)
        observation = self._observation(status, observed_at)
        if observation.actual_offset is None:
            raise HomeAssistantError(
                "A fresh native floor offset is required before restore."
            )
        async with self.lock:
            if self.state.get("pending_operation") is not None:
                raise HomeAssistantError(
                    "Resolve the uncertain floor command before restoring."
                )
            owner = self._ownership()
            if owner is not None:
                baseline = _number(owner.get("baseline_offset"))
                target = _number(owner.get("target_offset"))
                if observation.actual_offset == target and baseline is not None:
                    restored = await self._dispatch_offset(
                        now=observed_at,
                        previous=observation.actual_offset,
                        target=baseline,
                        baseline=baseline,
                        phase=str(owner["phase"]),
                        restoring=True,
                    )
                    if not restored:
                        await self._save()
                        raise HomeAssistantError(
                            self.state.get("fault")
                            or self._extreme_block_reason(observed_at)
                            or "The floor baseline restore was not confirmed."
                        )
                elif observation.actual_offset == baseline:
                    self.state["ownership"] = None
                elif (
                    observation.actual_offset
                    in ACCEPTED_NATIVE_BASELINES
                ):
                    self._record_native_transition(
                        now=observed_at,
                        previous=target,
                        current=observation.actual_offset,
                        classification="accepted_schedule_transition",
                        owned=True,
                    )
                    self.state["ownership"] = None
                else:
                    self.mode = FLOOR_MODE_SHADOW
                    self.state["ownership"] = None
                    self.state["manual_override"] = (
                        "The native floor offset changed outside optimizer "
                        "ownership; it was preserved instead of restoring."
                    )
                    await self._save()
                    raise HomeAssistantError(
                        self.state["manual_override"]
                    )
            if pause:
                self.mode = FLOOR_MODE_SHADOW
            await self._save()
        if evaluate:
            await self.async_evaluate(observed_at)

    @property
    def optimization_value(self) -> Decimal | None:
        decision = self.decision
        if decision is None or decision.plan is None:
            return None
        return decision.price_delta

    @property
    def optimization_value_attributes(self) -> dict:
        decision = self.decision
        plan_id = self._opportunity_plan_id(decision) if decision else None
        if decision is None or decision.plan is None:
            return {
                "metric": "price_opportunity_per_shifted_kwh",
                "controller": "floor_heating",
                "valuation_status": "unavailable",
                "plan_id": None,
                "measured_shifted_energy_kwh": None,
                "estimated_savings_eur": None,
                "realized_savings_eur": None,
                "savings_unavailable_reason": (
                    "No paired floor-heating price plan is currently available."
                ),
            }
        if self.state["candidate_cycle_peak_end"] is not None:
            plan_status = (
                "candidate_contaminated"
                if self.state["candidate_cycle_contaminated"]
                else "candidate_in_progress"
            )
        else:
            plan_status = decision.state
        return {
            "metric": "price_opportunity_per_shifted_kwh",
            "controller": "floor_heating",
            "valuation_status": (
                "live_action_unattributed"
                if self.mode == FLOOR_MODE_ACTIVE
                else "counterfactual"
            ),
            "operating_mode": self.mode,
            "shadow_zero_writes": self.mode == FLOOR_MODE_SHADOW,
            "plan_id": plan_id,
            "plan_status": plan_status,
            "opportunity_qualified": decision.price_gate,
            "opportunity_eur_per_kwh": (
                format(decision.price_delta, "f")
                if decision.price_delta is not None else None
            ),
            "opportunity_percent": (
                format(decision.price_delta_percent * Decimal(100), "f")
                if decision.price_delta_percent is not None else None
            ),
            "cheap_window_start": decision.plan.cheap_window.start.isoformat(),
            "cheap_window_end": decision.plan.cheap_window.end.isoformat(),
            "cheap_window_average_eur_kwh": format(
                decision.plan.cheap_window.average, "f"
            ),
            "peak_window_start": decision.plan.peak_window.start.isoformat(),
            "peak_window_end": decision.plan.peak_window.end.isoformat(),
            "peak_window_average_eur_kwh": format(
                decision.plan.peak_window.average, "f"
            ),
            "qualified_opportunities_recorded": self.state[
                "qualified_opportunity_count"
            ],
            "last_qualified_opportunity_at": self.state[
                "last_qualified_opportunity_at"
            ],
            "thermal_response_evidence_eligible": self.room_temperature_details[
                "thermal_response_evidence_eligible"
            ],
            "candidate_cycle_in_progress": (
                self.state["candidate_cycle_peak_end"] is not None
            ),
            "candidate_cycle_contaminated": self.state[
                "candidate_cycle_contaminated"
            ],
            "confirmed_writes": self.state["confirmed_writes"],
            "ownership": self._ownership() is not None,
            "owned_action_started_at": (
                self._ownership().get("acquired_at")
                if self._ownership() is not None
                else None
            ),
            "measured_shifted_energy_kwh": None,
            "estimated_savings_eur": None,
            "realized_savings_eur": None,
            "savings_unavailable_reason": (
                "Whole-heat-pump electrical energy attributable to this paired "
                "cycle is not measured."
            ),
        }

    @property
    def attributes(self) -> dict:
        if self.decision is None:
            return {
                "mode": self.mode,
                "reason": "Floor heating has not evaluated yet.",
                "write_allowed": False,
                "shadow_zero_writes": self.mode == FLOOR_MODE_SHADOW,
                "owner_activation_approved": bool(
                    self.state["owner_activation_approved_at"]
                ),
                "owner_activation_approved_at": self.state[
                    "owner_activation_approved_at"
                ],
                "fault": self.state["fault"],
                "manual_override": self.state["manual_override"],
            }
        result = self.decision.attributes()
        result.update({
            "mode": self.mode,
            "write_allowed": (
                self.mode == FLOOR_MODE_ACTIVE
                and not self.state.get("fault")
                and not self.state.get("manual_override")
            ),
            "shadow_zero_writes": self.mode == FLOOR_MODE_SHADOW,
            "policy_revision": POLICY_REVISION,
            "native_minimum_offset_c": FLOOR_NATIVE_MIN_OFFSET,
            "native_maximum_offset_c": FLOOR_NATIVE_MAX_OFFSET,
            "primary_candidate_minimum_offset_c": self.policy.minimum_offset,
            "primary_candidate_maximum_offset_c": self.policy.preheat_offset,
            "configured_aggressive_candidate_offset_c": self.policy.preheat_offset,
            "minimum_offset_c": self.policy.minimum_offset,
            "neutral_offset_c": self.policy.neutral_offset,
            "preheat_offset_c": self.policy.preheat_offset,
            "shift_delta_c": self.policy.shift_delta,
            "price_shift_tiers": [
                {
                    "minimum_price_delta_eur_kwh": absolute,
                    "minimum_price_delta_percent": percent,
                    "shift_c": shift,
                }
                for absolute, percent, shift in self.policy.price_shift_tiers
            ],
            "coast_room_shift_limits": [
                {
                    "maximum_room_temperature_c": temperature,
                    "maximum_shift_c": shift,
                }
                for temperature, shift in self.policy.coast_room_shift_limits
            ],
            "preheat_room_shift_limits": [
                {
                    "maximum_room_temperature_c": temperature,
                    "maximum_shift_c": shift,
                }
                for temperature, shift in self.policy.preheat_room_shift_limits
            ],
            "offset_policy": (
                "adaptive_price_tier_clamped_by_room_and_projected_lwt"
            ),
            "comfort_floor_c": self.policy.comfort_floor,
            "preheat_ceiling_c": self.policy.preheat_ceiling,
            "minimum_leaving_water_temperature_c": (
                self.policy.minimum_leaving_water_temperature
            ),
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
            "pilot_eligible": self.pilot_eligible,
            "pilot_eligibility_reason": (
                "The evidence gate requires at least 14 observed days and five "
                "clean revision-7 candidate cycles. Explicit owner-approved "
                "activation is recorded separately and does not relabel the "
                "evidence gate as passed."
            ),
            "owner_activation_approved": bool(
                self.state["owner_activation_approved_at"]
            ),
            "owner_activation_approved_at": self.state[
                "owner_activation_approved_at"
            ],
            "active_started_at": self.state["active_started_at"],
            "ownership": self._ownership() is not None,
            "ownership_details": self._ownership(),
            "pending_operation": self.state["pending_operation"],
            "last_write_at": self.state["last_write_at"],
            "fault": self.state["fault"],
            "manual_override": self.state["manual_override"],
            "schedule_learning_hold": (
                _instant(self.state["schedule_learning_hold_until"])
                is not None
                and dt_util.utcnow().astimezone(UTC)
                < _instant(
                    self.state["schedule_learning_hold_until"]
                ).astimezone(UTC)
            ),
            "schedule_learning_hold_until": self.state[
                "schedule_learning_hold_until"
            ],
            "schedule_learning_reason": self.state[
                "schedule_learning_reason"
            ],
            "accepted_native_baselines_c": sorted(
                ACCEPTED_NATIVE_BASELINES
            ),
            "native_schedule_transitions": list(
                self.state["native_schedule_transitions"][-8:]
            ),
            "solar_dispatch_available": False,
            "solar_dispatch_reason": (
                "Fresh signed net-grid power is unavailable; PV generation alone "
                "cannot prove household surplus."
            ),
            **self.room_temperature_details,
            "recent_decisions": self.state["decision_history"][
                -RECENT_DECISIONS_ATTRIBUTE_LIMIT:
            ],
        })
        return result
