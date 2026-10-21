"""Deterministic, opt-in domestic-hot-water price shifting."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import math
from typing import Callable, Iterable, Mapping

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ._vendor.home_automation.electricity_market.prices import MARKET_INTERVAL, TALLINN, UTC
from .const import (
    CONF_DHW_COAST_HOURS,
    CONF_DHW_EVENING_READY,
    CONF_DHW_FLOOR_TARGET,
    CONF_DHW_INITIAL_HEAT_MINUTES,
    CONF_DHW_MAX_LEASE_HOURS,
    CONF_DHW_MORNING_READY,
    CONF_DHW_NORMAL_TARGET,
    CONF_DHW_PREHEAT_TARGET,
    CONF_DHW_PRICE_DELTA,
    CONF_DHW_PRICE_PERCENT,
    CONF_DHW_SOFT_TARGET,
    DEFAULT_DHW_COAST_HOURS,
    DEFAULT_DHW_EVENING_READY,
    DEFAULT_DHW_FLOOR_TARGET,
    DEFAULT_DHW_INITIAL_HEAT_MINUTES,
    DEFAULT_DHW_MAX_LEASE_HOURS,
    DEFAULT_DHW_MORNING_READY,
    DEFAULT_DHW_NORMAL_TARGET,
    DEFAULT_DHW_PREHEAT_TARGET,
    DEFAULT_DHW_PRICE_DELTA,
    DEFAULT_DHW_PRICE_PERCENT,
    DEFAULT_DHW_SOFT_TARGET,
    DHW_FINAL_TOPUP_MINUTES,
    DHW_MODE_PAUSED,
    DHW_MODE_READINESS,
    DHW_MODE_SAFE,
    DHW_MODE_SHADOW,
    DHW_MODE_SOFT,
    DHW_MODES,
    DHW_POST_DELIVERY_REARM_C,
    DHW_POST_READINESS_HOLD_MINUTES,
    DOMAIN,
    EVENT_DHW_DECISION,
)
from .controls import DaikinPreDispatchConflict, EnergyDevices, FLAGS
from .coordinator import HouseholdPriceCoordinator


STORE_VERSION = 1
STORE_KEY_SUFFIX = "dhw_optimizer"
NOTIFICATION_ID = DOMAIN + "_dhw_optimizer_fault"
DECISION_HISTORY_LIMIT = 96
HEATING_DURATION_LIMIT = 20
POLICY_REVISION = 7
MINIMUM_TARGET_DWELL = timedelta(minutes=30)
POST_READINESS_HOLD = timedelta(
    minutes=DHW_POST_READINESS_HOLD_MINUTES
)
FINAL_TOPUP_WINDOW = timedelta(minutes=DHW_FINAL_TOPUP_MINUTES)
RECENT_DECISIONS_ATTRIBUTE_LIMIT = 4
READINESS_SUCCESS = "success"
READINESS_CONSUMED = "consumed"
READINESS_CONTAMINATED = "draw_contaminated"
READINESS_FAILURE = "failure"
READINESS_UNAVAILABLE = "unavailable"
READINESS_USABLE = frozenset(
    (READINESS_SUCCESS, READINESS_CONSUMED, READINESS_FAILURE)
)
READINESS_DELIVERED = frozenset((READINESS_SUCCESS, READINESS_CONSUMED))


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise HomeAssistantError(label + " must be a number.")
    result = float(value)
    if not math.isfinite(result):
        raise HomeAssistantError(label + " must be finite.")
    return result


def _clock(value: object, label: str) -> time:
    if not isinstance(value, str):
        raise HomeAssistantError(label + " must use HH:MM.")
    try:
        result = time.fromisoformat(value)
    except ValueError as error:
        raise HomeAssistantError(label + " must use HH:MM.") from error
    if result.second or result.microsecond or result.tzinfo is not None:
        raise HomeAssistantError(label + " must be a whole local minute.")
    return result


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.utcoffset() is not None else None


def _raw_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class DhwPolicy:
    morning_ready: time
    evening_ready: time
    soft_target: float
    floor_target: float
    normal_target: float
    preheat_target: float
    max_lease: timedelta
    price_delta: Decimal
    price_percent: Decimal
    initial_heat_minutes: int
    coast_horizon: timedelta

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> "DhwPolicy":
        heat_minutes = _number(
            options.get(
                CONF_DHW_INITIAL_HEAT_MINUTES,
                DEFAULT_DHW_INITIAL_HEAT_MINUTES,
            ),
            "Initial heating duration",
        )
        if heat_minutes != int(heat_minutes) or int(heat_minutes) % 15:
            raise HomeAssistantError(
                "Initial DHW heating duration must use whole 15-minute intervals."
            )
        policy = cls(
            morning_ready=_clock(
                options.get(CONF_DHW_MORNING_READY, DEFAULT_DHW_MORNING_READY),
                "Morning readiness time",
            ),
            evening_ready=_clock(
                options.get(CONF_DHW_EVENING_READY, DEFAULT_DHW_EVENING_READY),
                "Evening readiness time",
            ),
            soft_target=_number(
                options.get(CONF_DHW_SOFT_TARGET, DEFAULT_DHW_SOFT_TARGET),
                "Soft-standby target",
            ),
            floor_target=_number(
                options.get(CONF_DHW_FLOOR_TARGET, DEFAULT_DHW_FLOOR_TARGET),
                "Comfort target",
            ),
            normal_target=_number(
                options.get(CONF_DHW_NORMAL_TARGET, DEFAULT_DHW_NORMAL_TARGET),
                "Normal target",
            ),
            preheat_target=_number(
                options.get(CONF_DHW_PREHEAT_TARGET, DEFAULT_DHW_PREHEAT_TARGET),
                "Preheat target",
            ),
            max_lease=timedelta(hours=_number(
                options.get(CONF_DHW_MAX_LEASE_HOURS, DEFAULT_DHW_MAX_LEASE_HOURS),
                "Maximum soft-standby lease",
            )),
            price_delta=Decimal(str(_number(
                options.get(CONF_DHW_PRICE_DELTA, DEFAULT_DHW_PRICE_DELTA),
                "Absolute price gate",
            ))),
            price_percent=Decimal(str(_number(
                options.get(CONF_DHW_PRICE_PERCENT, DEFAULT_DHW_PRICE_PERCENT),
                "Relative price gate",
            ))) / Decimal(100),
            initial_heat_minutes=int(heat_minutes),
            coast_horizon=timedelta(hours=_number(
                options.get(CONF_DHW_COAST_HOURS, DEFAULT_DHW_COAST_HOURS),
                "Coast horizon",
            )),
        )
        if not (
            policy.soft_target < policy.floor_target
            <= policy.normal_target
            <= policy.preheat_target
        ):
            raise HomeAssistantError(
                "DHW targets must satisfy soft < floor <= normal <= preheat."
            )
        if policy.max_lease <= timedelta(0) or policy.max_lease > timedelta(hours=4):
            raise HomeAssistantError("The soft-standby lease must be over 0 and at most 4 hours.")
        if policy.price_delta < 0 or policy.price_percent < 0:
            raise HomeAssistantError("DHW price gates cannot be negative.")
        if not 15 <= policy.initial_heat_minutes <= 180:
            raise HomeAssistantError("Initial DHW heating duration must be 15 to 180 minutes.")
        if not timedelta(hours=1) <= policy.coast_horizon <= timedelta(hours=12):
            raise HomeAssistantError("DHW coast horizon must be 1 to 12 hours.")
        if policy.morning_ready == policy.evening_ready:
            raise HomeAssistantError("Morning and evening readiness times must differ.")
        return policy

    @property
    def readiness_times(self) -> tuple[time, time]:
        return tuple(sorted((self.morning_ready, self.evening_ready)))


@dataclass(frozen=True)
class PriceInterval:
    start: datetime
    end: datetime
    price: Decimal


@dataclass(frozen=True)
class PriceWindowChoice:
    start: datetime
    end: datetime
    average: Decimal
    interval_count: int


@dataclass(frozen=True)
class DhwObservation:
    tank_temperature: float | None
    target_temperature: float | None
    effective_target: float | None
    enabled: bool | None
    override_reason: str | None


@dataclass(frozen=True)
class DhwDecision:
    mode: str
    state: str
    reason: str
    desired_target: float | None
    actual_target: float | None
    tank_temperature: float | None
    deadline: datetime | None
    selected_window: PriceWindowChoice | None
    current_price: Decimal | None
    price_delta: Decimal | None
    price_delta_percent: Decimal | None
    price_gate: bool
    data_complete: bool
    lease_expires: datetime | None
    allow_write: bool
    urgent: bool
    comparison_window: PriceWindowChoice | None = None
    preheat_price_delta: Decimal | None = None
    preheat_price_delta_percent: Decimal | None = None
    preheat_price_gate: bool = False

    def attributes(self) -> dict:
        result = {
            "mode": self.mode,
            "state": self.state,
            "reason": self.reason,
            "desired_target_c": self.desired_target,
            "actual_target_c": self.actual_target,
            "tank_temperature_c": self.tank_temperature,
            "next_readiness": self.deadline.isoformat() if self.deadline else None,
            "current_price_eur_kwh": (
                format(self.current_price, "f") if self.current_price is not None else None
            ),
            "price_delta_eur_kwh": (
                format(self.price_delta, "f") if self.price_delta is not None else None
            ),
            "price_delta_percent": (
                format(self.price_delta_percent * Decimal(100), "f")
                if self.price_delta_percent is not None else None
            ),
            "price_gate": self.price_gate,
            "preheat_price_advantage_eur_kwh": (
                format(self.preheat_price_delta, "f")
                if self.preheat_price_delta is not None else None
            ),
            "preheat_price_advantage_percent": (
                format(self.preheat_price_delta_percent * Decimal(100), "f")
                if self.preheat_price_delta_percent is not None else None
            ),
            "preheat_price_gate": self.preheat_price_gate,
            "price_data_complete": self.data_complete,
            "lease_expires": self.lease_expires.isoformat() if self.lease_expires else None,
            "write_allowed": self.allow_write,
            "urgent": self.urgent,
        }
        if self.selected_window is None:
            result.update({
                "selected_window_start": None,
                "selected_window_end": None,
                "selected_window_average_eur_kwh": None,
                "selected_window_intervals": 0,
            })
        else:
            result.update({
                "selected_window_start": self.selected_window.start.isoformat(),
                "selected_window_end": self.selected_window.end.isoformat(),
                "selected_window_average_eur_kwh": format(
                    self.selected_window.average, "f"
                ),
                "selected_window_intervals": self.selected_window.interval_count,
            })
        if self.comparison_window is None:
            result.update({
                "comparison_window_start": None,
                "comparison_window_end": None,
                "comparison_window_average_eur_kwh": None,
            })
        else:
            result.update({
                "comparison_window_start": self.comparison_window.start.isoformat(),
                "comparison_window_end": self.comparison_window.end.isoformat(),
                "comparison_window_average_eur_kwh": format(
                    self.comparison_window.average, "f"
                ),
            })
        return result


def next_readiness(now: datetime, policy: DhwPolicy) -> datetime:
    local_now = now.astimezone(TALLINN)
    for day_offset in (0, 1, 2):
        day = local_now.date() + timedelta(days=day_offset)
        for ready in policy.readiness_times:
            candidate = datetime.combine(day, ready, tzinfo=TALLINN)
            if candidate > local_now:
                return candidate
    raise HomeAssistantError("Could not resolve the next DHW readiness time.")


def previous_readiness(now: datetime, policy: DhwPolicy) -> datetime:
    local_now = now.astimezone(TALLINN)
    candidates = []
    for day_offset in (-1, 0):
        day = local_now.date() + timedelta(days=day_offset)
        for ready in policy.readiness_times:
            candidate = datetime.combine(day, ready, tzinfo=TALLINN)
            if candidate <= local_now:
                candidates.append(candidate)
    if not candidates:
        raise HomeAssistantError(
            "Could not resolve the previous DHW readiness time."
        )
    return max(candidates)


def post_readiness_hold_until(
    now: datetime, policy: DhwPolicy
) -> datetime | None:
    ready_at = previous_readiness(now, policy)
    hold_until = ready_at + POST_READINESS_HOLD
    return hold_until if now.astimezone(TALLINN) < hold_until else None


def published_intervals(coordinator: HouseholdPriceCoordinator) -> tuple[PriceInterval, ...]:
    snapshot = coordinator.data
    if snapshot is None:
        return ()
    result = []
    try:
        for window in (snapshot.today, snapshot.tomorrow):
            for row in window.prices:
                start = datetime.fromisoformat(row.start).astimezone(UTC)
                end = datetime.fromisoformat(row.end).astimezone(UTC)
                price = Decimal(row.import_eur_kwh)
                if (
                    end - start != MARKET_INTERVAL
                    or start.timestamp() % MARKET_INTERVAL.total_seconds()
                    or not price.is_finite()
                ):
                    return ()
                result.append(PriceInterval(start, end, price))
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return ()
    return tuple(sorted(result, key=lambda item: item.start))


def quarter_start(now: datetime) -> datetime:
    stamp = int(now.astimezone(UTC).timestamp())
    seconds = int(MARKET_INTERVAL.total_seconds())
    return datetime.fromtimestamp(stamp // seconds * seconds, UTC)


def _price_map(intervals: Iterable[PriceInterval]) -> dict[datetime, PriceInterval]:
    result = {}
    for item in intervals:
        if item.start in result:
            return {}
        result[item.start] = item
    return result


def select_price_window(
    now: datetime,
    deadline: datetime,
    intervals: Iterable[PriceInterval],
    duration_minutes: int,
    coast_horizon: timedelta,
    planned_window: PriceWindowChoice | None = None,
    comparison_window: PriceWindowChoice | None = None,
) -> tuple[
    PriceWindowChoice | None,
    PriceWindowChoice | None,
    Decimal | None,
    bool,
]:
    prices = _price_map(intervals)
    current = quarter_start(now)
    deadline_utc = deadline.astimezone(UTC)
    cursor = current
    while cursor < deadline_utc:
        row = prices.get(cursor)
        if row is None or row.end != cursor + MARKET_INTERVAL:
            return None, None, None, False
        cursor += MARKET_INTERVAL
    current_price = prices[current].price if current in prices else None
    horizon_start = deadline_utc - coast_horizon
    if (
        planned_window is not None
        and planned_window.start >= horizon_start
        and planned_window.end <= deadline_utc
        and planned_window.start < planned_window.end
        and planned_window.interval_count > 0
    ):
        comparison = comparison_window
        if (
            comparison is None
            or comparison.start < horizon_start
            or comparison.end > deadline_utc
            or comparison.start >= comparison.end
            or comparison.interval_count != planned_window.interval_count
        ):
            comparison = planned_window
        return planned_window, comparison, current_price, True
    interval_count = max(1, math.ceil(duration_minutes / 15))
    duration = interval_count * MARKET_INTERVAL
    first = max(current, horizon_start)
    last = deadline_utc - duration
    if last < first:
        return None, None, current_price, True
    choices = []
    candidate = first
    while candidate <= last:
        rows = [prices.get(candidate + index * MARKET_INTERVAL) for index in range(interval_count)]
        if all(row is not None for row in rows):
            total = sum((row.price for row in rows if row is not None), Decimal(0))
            choices.append((
                total,
                -candidate.timestamp(),
                PriceWindowChoice(
                    start=candidate,
                    end=candidate + duration,
                    average=total / interval_count,
                    interval_count=interval_count,
                ),
            ))
        candidate += MARKET_INTERVAL
    if not choices:
        return None, None, current_price, True
    selected = min(choices, key=lambda item: (item[0], item[1]))[2]
    comparison = max(choices, key=lambda item: (item[0], item[2].start.timestamp()))[2]
    return selected, comparison, current_price, True


def price_gate(
    current: Decimal | None,
    selected: PriceWindowChoice | None,
    absolute_delta: Decimal,
    relative_delta: Decimal,
) -> tuple[bool, Decimal | None, Decimal | None]:
    if current is None or selected is None:
        return False, None, None
    delta = current - selected.average
    base = max(abs(selected.average), Decimal("0.05"))
    relative = delta / base
    return (
        delta >= absolute_delta and relative >= relative_delta,
        delta,
        relative,
    )


def plan_dhw(
    *,
    now: datetime,
    mode: str,
    policy: DhwPolicy,
    observation: DhwObservation,
    intervals: Iterable[PriceInterval],
    heating_minutes: int,
    lease_expires: datetime | None,
    planned_window: PriceWindowChoice | None = None,
    comparison_window: PriceWindowChoice | None = None,
    recovery_latched: bool = False,
    recovery_reason: str | None = None,
    preheat_completed: bool = False,
    readiness_delivered: bool = False,
    final_topup_used: bool = False,
    final_topup_active: bool = False,
) -> DhwDecision:
    if now.utcoffset() is None:
        raise HomeAssistantError("DHW planning time must include a UTC offset.")
    if mode == DHW_MODE_SOFT:
        mode = DHW_MODE_SAFE
    if mode not in DHW_MODES:
        raise HomeAssistantError("Unknown DHW optimizer mode.")
    deadline = next_readiness(now, policy)
    selected, comparison, current_price, complete = select_price_window(
        now,
        deadline,
        intervals,
        heating_minutes,
        policy.coast_horizon,
        planned_window,
        comparison_window,
    )
    gate, delta, relative = price_gate(
        current_price,
        selected,
        policy.price_delta,
        policy.price_percent,
    )
    preheat_gate, preheat_delta, preheat_relative = price_gate(
        comparison.average if comparison is not None else None,
        selected,
        policy.price_delta,
        policy.price_percent,
    )
    allow_write = mode in (DHW_MODE_READINESS, DHW_MODE_SAFE)

    def decision(
        state: str,
        reason: str,
        target: float | None,
        *,
        urgent: bool = False,
        write: bool = allow_write,
    ) -> DhwDecision:
        return DhwDecision(
            mode=mode,
            state=state,
            reason=reason,
            desired_target=target,
            actual_target=observation.target_temperature,
            tank_temperature=observation.tank_temperature,
            deadline=deadline,
            selected_window=selected,
            current_price=current_price,
            price_delta=delta,
            price_delta_percent=relative,
            price_gate=gate,
            data_complete=complete,
            lease_expires=lease_expires,
            allow_write=write,
            urgent=urgent,
            comparison_window=comparison,
            preheat_price_delta=preheat_delta,
            preheat_price_delta_percent=preheat_relative,
            preheat_price_gate=preheat_gate,
        )

    if mode == DHW_MODE_PAUSED:
        return decision("paused", "Optimizer is paused; no automatic action is allowed.", None, write=False)
    if observation.override_reason:
        return decision("override", observation.override_reason, None, write=False)
    if observation.tank_temperature is None or observation.target_temperature is None:
        return decision(
            "unavailable",
            "Fresh tank and target temperatures are required.",
            None,
            write=False,
        )
    remaining = deadline.astimezone(UTC) - now.astimezone(UTC)
    if final_topup_active and remaining > timedelta(0):
        return decision(
            "readiness_final_topup",
            "Keep the final 55°C readiness charge latched through the "
            "deadline to avoid native target hysteresis.",
            policy.preheat_target,
            urgent=True,
        )
    if remaining <= timedelta(minutes=heating_minutes):
        if observation.tank_temperature < policy.normal_target:
            if (
                readiness_delivered
                and observation.tank_temperature
                > DHW_POST_DELIVERY_REARM_C
                and (
                    remaining > FINAL_TOPUP_WINDOW
                    or (
                        final_topup_used
                        and not final_topup_active
                    )
                )
            ):
                return decision(
                    "readiness_hold",
                    "Measured readiness was already delivered. Hold 50°C "
                    "unless the tank reaches the 48°C re-arm threshold or "
                    "one final top-up is needed inside the last 45 minutes.",
                    policy.normal_target,
                    urgent=True,
                )
            return decision(
                (
                    "readiness_final_topup"
                    if readiness_delivered
                    and remaining <= FINAL_TOPUP_WINDOW
                    else "readiness_charge"
                ),
                (
                    "The readiness deadline is inside the final 45-minute "
                    "window; keep the 55°C charge latched through the deadline."
                    if readiness_delivered
                    and remaining <= FINAL_TOPUP_WINDOW
                    else (
                        "The readiness deadline is inside the conservative "
                        "heating allowance; request 55°C until the measured "
                        "tank reaches 50°C."
                    )
                ),
                policy.preheat_target,
                urgent=True,
            )
        return decision(
            "readiness",
            "Measured readiness is satisfied; hold the normal 50°C target.",
            policy.normal_target,
            urgent=True,
        )
    if (
        recovery_latched
        and observation.tank_temperature < policy.normal_target
    ):
        return decision(
            "recovery",
            recovery_reason
            or "Recovery remains latched until the measured tank reaches the normal target.",
            policy.normal_target,
            urgent=True,
        )
    if observation.tank_temperature <= policy.floor_target:
        return decision(
            "recovery",
            "Tank temperature reached the 45°C comfort threshold; price shifting is bypassed.",
            policy.normal_target,
            urgent=True,
        )
    hold_until = post_readiness_hold_until(now, policy)
    if hold_until is not None:
        return decision(
            "usage_hold",
            "The two-hour post-readiness demand hold keeps the normal 50°C "
            "target and blocks price-driven coasting.",
            policy.normal_target,
            urgent=True,
        )
    if mode == DHW_MODE_READINESS:
        return decision(
            "readiness_pilot",
            "The readiness-only pilot keeps the normal target outside the final "
            "readiness allowance; price coasting and soft standby remain disabled.",
            policy.normal_target,
            urgent=True,
        )
    if not complete or current_price is None:
        return decision(
            "normal",
            "Published all-in prices are incomplete through the next readiness deadline.",
            policy.normal_target,
            urgent=True,
        )
    if selected is None:
        return decision(
            "normal",
            "No complete preheat window remains before the next readiness deadline.",
            policy.normal_target,
            urgent=True,
        )
    now_utc = now.astimezone(UTC)
    if selected.start <= now_utc < selected.end:
        if not preheat_gate:
            return decision(
                "normal",
                "The cheapest feasible block does not beat the highest-cost alternative "
                "by both price gates, so unnecessary 55°C preheating is avoided.",
                policy.normal_target,
            )
        return decision(
            "preheat",
            "The current quarter is inside the anchored cheapest block and its "
            "all-in price advantage justifies 55°C preheating.",
            policy.preheat_target,
        )
    if (
        observation.tank_temperature < policy.normal_target
        and selected.end <= now_utc
        and not preheat_completed
    ):
        return decision(
            "recovery",
            "The selected cheap window ended before the tank reached the normal target.",
            policy.normal_target,
            urgent=True,
        )
    if not gate:
        return decision(
            "normal",
            "The current price does not exceed the balanced absolute and relative gates.",
            policy.normal_target,
        )

    return decision(
        "coast",
        "The cost gate is active; allow natural cooling while Daikin enforces the 45°C target.",
        policy.floor_target,
    )


def stabilize_decision(
    *,
    now: datetime,
    decision: DhwDecision,
    last_target: float | None,
    last_transition: datetime | None,
    minimum_dwell: timedelta = MINIMUM_TARGET_DWELL,
) -> DhwDecision:
    target = decision.desired_target
    if (
        target is None
        or decision.urgent
        or decision.state == "preheat"
        or last_target is None
        or last_transition is None
        or target == last_target
        or now.astimezone(UTC) - last_transition.astimezone(UTC) >= minimum_dwell
    ):
        return decision
    return replace(
        decision,
        state="hold",
        reason=(
            f"The {int(minimum_dwell.total_seconds() // 60)}-minute target dwell "
            f"is active; hold {last_target:g}°C before another cost-driven transition."
        ),
        desired_target=last_target,
    )


class DhwOptimizer:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: HouseholdPriceCoordinator,
        devices: EnergyDevices,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.devices = devices
        self.policy = DhwPolicy.from_options(entry.options)
        self.store = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.{STORE_KEY_SUFFIX}", private=True
        )
        self.lock = asyncio.Lock()
        self.extreme_protection = None
        self.mode = DHW_MODE_SHADOW
        self.decision: DhwDecision | None = None
        self.listeners: list[Callable[[], None]] = []
        self.unsubscribers: list[Callable[[], None]] = []
        self.state: dict = {
            "policy_revision": POLICY_REVISION,
            "lease_expires": None,
            "last_owned_target": None,
            "last_write_at": None,
            "last_policy_target": None,
            "last_policy_transition_at": None,
            "fault": None,
            "manual_override": None,
            "recovery_latched": False,
            "recovery_reason": None,
            "planned_deadline": None,
            "planned_window_start": None,
            "planned_window_end": None,
            "planned_window_average": None,
            "planned_window_intervals": None,
            "planned_comparison_start": None,
            "planned_comparison_end": None,
            "planned_comparison_average": None,
            "planned_comparison_intervals": None,
            "planned_window_completed_at": None,
            "shadow_days": [],
            "readiness_pilot_days": [],
            "heating_durations": [],
            "readiness_topup_durations": [],
            "cycle_started_at": None,
            "cycle_start_temperature": None,
            "topup_started_at": None,
            "topup_start_temperature": None,
            "last_temperature": None,
            "last_observed_at": None,
            "decision_history": [],
            "confirmed_writes": 0,
            "readiness_pilot_started_at": None,
            "readiness_pilot_confirmed_writes": 0,
            "pilot_started_at": None,
            "safe_pilot_confirmed_writes": 0,
            "owner_full_control_approved_at": None,
            "deadline_checks": {},
            "readiness_window_peaks": {},
            "readiness_rapid_draws": {},
            "readiness_deadline_samples": {},
            "readiness_final_topups": {},
            "readiness_final_topup_active": {},
            "qualified_opportunity_count": 0,
            "last_qualified_opportunity_plan_id": None,
            "last_qualified_opportunity_at": None,
        }

    def _reset_policy_evidence(self) -> None:
        self.state.update({
            "policy_revision": POLICY_REVISION,
            "last_policy_target": None,
            "last_policy_transition_at": None,
            "recovery_latched": False,
            "recovery_reason": None,
            "planned_deadline": None,
            "planned_window_start": None,
            "planned_window_end": None,
            "planned_window_average": None,
            "planned_window_intervals": None,
            "planned_comparison_start": None,
            "planned_comparison_end": None,
            "planned_comparison_average": None,
            "planned_comparison_intervals": None,
            "planned_window_completed_at": None,
            "shadow_days": [],
            "readiness_pilot_days": [],
            "heating_durations": [],
            "readiness_topup_durations": [],
            "cycle_started_at": None,
            "cycle_start_temperature": None,
            "topup_started_at": None,
            "topup_start_temperature": None,
            "last_temperature": None,
            "last_observed_at": None,
            "decision_history": [],
            "confirmed_writes": 0,
            "readiness_pilot_started_at": None,
            "readiness_pilot_confirmed_writes": 0,
            "pilot_started_at": None,
            "safe_pilot_confirmed_writes": 0,
            "owner_full_control_approved_at": None,
            "deadline_checks": {},
            "readiness_window_peaks": {},
            "readiness_rapid_draws": {},
            "readiness_deadline_samples": {},
            "readiness_final_topups": {},
            "readiness_final_topup_active": {},
            "qualified_opportunity_count": 0,
            "last_qualified_opportunity_plan_id": None,
            "last_qualified_opportunity_at": None,
        })

    def _migrate_revision_seven(self) -> None:
        preserved = {
            "heating_durations": list(self.state["heating_durations"]),
            "readiness_topup_durations": list(
                self.state["readiness_topup_durations"]
            ),
            "confirmed_writes": self.state["confirmed_writes"],
            "readiness_pilot_started_at": self.state[
                "readiness_pilot_started_at"
            ],
            "readiness_pilot_confirmed_writes": self.state[
                "readiness_pilot_confirmed_writes"
            ],
            "pilot_started_at": self.state["pilot_started_at"],
            "safe_pilot_confirmed_writes": self.state[
                "safe_pilot_confirmed_writes"
            ],
            "owner_full_control_approved_at": self.state[
                "owner_full_control_approved_at"
            ],
            "qualified_opportunity_count": self.state[
                "qualified_opportunity_count"
            ],
            "last_qualified_opportunity_plan_id": self.state[
                "last_qualified_opportunity_plan_id"
            ],
            "last_qualified_opportunity_at": self.state[
                "last_qualified_opportunity_at"
            ],
        }
        self._reset_policy_evidence()
        self.state.update(preserved)
        self.state["lease_expires"] = None

    async def async_start(self) -> None:
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            stored_revision = stored.get("policy_revision")
            mode = stored.get("mode")
            if mode == DHW_MODE_SOFT:
                self.mode = DHW_MODE_SAFE
            elif mode in DHW_MODES:
                self.mode = mode
            for key in self.state:
                if key in stored:
                    self.state[key] = stored[key]
            if stored_revision in (5, 6):
                self._migrate_revision_seven()
            elif stored_revision != POLICY_REVISION:
                write_capable_mode = self.mode in (
                    DHW_MODE_READINESS,
                    DHW_MODE_SAFE,
                )
                self._reset_policy_evidence()
                if write_capable_mode:
                    self.mode = DHW_MODE_PAUSED
                    if self.state["fault"] is None:
                        self.state["fault"] = (
                            "The DHW policy changed while automatic writes were "
                            "enabled. The optimizer was paused; inspect fresh device "
                            "state before resuming or selecting a write-capable mode."
                        )
        self.unsubscribers.append(self.coordinator.async_add_listener(self._schedule_evaluation))
        self.unsubscribers.append(
            async_track_time_interval(
                self.hass,
                self._async_interval,
                timedelta(minutes=1),
                name=DOMAIN + " DHW optimizer",
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
    def set_extreme_protection(self, protection) -> None:
        self.extreme_protection = protection

    def _extreme_block_reason(self, now: datetime) -> str | None:
        if self.extreme_protection is None:
            return None
        return self.extreme_protection.ordinary_control_block_reason(now)

    @callback
    def _schedule_evaluation(self) -> None:
        self.hass.async_create_task(self.async_evaluate())

    async def _async_interval(self, now: datetime) -> None:
        await self.async_evaluate(now)

    async def _save(self) -> None:
        await self.store.async_save({"mode": self.mode, **self.state})

    @property
    def shadow_ready(self) -> bool:
        checks = self.state["deadline_checks"]
        usable_checks = [
            value for value in checks.values() if value in READINESS_USABLE
        ]
        qualification_days = set(self.state["shadow_days"]) | set(
            self.state["readiness_pilot_days"]
        )
        return (
            len(qualification_days) >= 7
            and len(self.state["heating_durations"]) >= 5
            and len(usable_checks) >= 10
            and all(value in READINESS_DELIVERED for value in usable_checks)
            and self.state["readiness_pilot_started_at"] is not None
            and self.state["readiness_pilot_confirmed_writes"] >= 2
            and not self.state["fault"]
            and not self.state["manual_override"]
        )

    @property
    def soft_ready(self) -> bool:
        return False

    @property
    def heating_minutes(self) -> int:
        values = sorted(
            float(value)
            for value in self.state["heating_durations"]
            if isinstance(value, (int, float)) and math.isfinite(float(value))
        )
        if len(values) < 5:
            return max(120, self.policy.initial_heat_minutes)
        index = max(0, math.ceil(len(values) * 0.9) - 1)
        learned = math.ceil((values[index] + 15) / 15) * 15
        return max(45, min(120, learned))

    def _observation(self, status: dict) -> DhwObservation:
        heating = status.get("heating")
        water = status.get("hot_water")
        if not isinstance(heating, dict) or not isinstance(water, dict):
            return DhwObservation(None, None, None, None, "Fresh Daikin circuits are unavailable.")
        water_states = water.get("states", {})
        heating_states = heating.get("states", {})
        water_operations = water.get("operations", {})
        heating_operations = heating.get("operations", {})
        sensors = water.get("sensors", {})
        tank = _raw_number(sensors.get("TankTemperature"))
        target = _raw_number(water_operations.get("DomesticHotWaterTemperatureHeating"))
        effective = _raw_number(status.get("effective_hot_water_target"))
        enabled_value = water_operations.get("Power")
        enabled = enabled_value == "on" if enabled_value in ("on", "standby") else None
        reason = None
        for circuit in (heating_states, water_states):
            if not isinstance(circuit, dict) or any(circuit.get(flag) is not False for flag in FLAGS):
                reason = "A Daikin safety flag is active or unknown; optimizer ownership is suspended."
                break
        if reason is None and heating_operations.get("OperationMode") != "heating":
            reason = "Daikin is not in the required heating operation mode."
        if reason is None and water_states.get("WeatherDependentState") is not False:
            reason = "Weather-dependent domestic-hot-water control is active or unknown."
        powerful = water_operations.get("powerful")
        if reason is None and powerful not in (0, 0.0, "0", "0.0", False):
            reason = "Daikin powerful/hygiene-like operation is active; optimizer yielded."
        if reason is None and enabled is not True:
            reason = "The DHW circuit is in standby or its enabled state is unknown."
        if reason is None and effective is None:
            reason = "The effective DHW target is unavailable; optimizer yielded."
        if (
            reason is None
            and target is not None
            and effective is not None
            and target != effective
        ):
            reason = "Configured and effective DHW targets differ; optimizer yielded."
        owned = _raw_number(self.state.get("last_owned_target"))
        if reason is None and owned is not None and target is not None and target != owned:
            reason = "The DHW target changed outside optimizer ownership."
            self.state["manual_override"] = reason
        if (
            reason is None
            and owned is None
            and target is not None
            and target != self.policy.normal_target
        ):
            reason = "The current DHW target is not optimizer-owned; it was preserved."
        if reason is None and self.state.get("manual_override"):
            reason = self.state["manual_override"]
        return DhwObservation(tank, target, effective, enabled, reason)

    def _clear_price_plan(self) -> None:
        for key in (
            "planned_deadline",
            "planned_window_start",
            "planned_window_end",
            "planned_window_average",
            "planned_window_intervals",
            "planned_comparison_start",
            "planned_comparison_end",
            "planned_comparison_average",
            "planned_comparison_intervals",
            "planned_window_completed_at",
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
            or end.astimezone(UTC) - start.astimezone(UTC) != count * MARKET_INTERVAL
        ):
            return None
        return PriceWindowChoice(
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            average=average,
            interval_count=count,
        )

    def _stored_price_plan(
        self, deadline: datetime
    ) -> tuple[PriceWindowChoice | None, PriceWindowChoice | None]:
        if self.state.get("planned_deadline") != deadline.isoformat():
            self._clear_price_plan()
            return None, None
        selected = self._stored_window("planned_window")
        if selected is None:
            self._clear_price_plan()
            return None, None
        comparison = self._stored_window("planned_comparison") or selected
        return selected, comparison

    def _store_price_plan(
        self,
        deadline: datetime,
        selected: PriceWindowChoice,
        comparison: PriceWindowChoice | None,
    ) -> None:
        self.state["planned_deadline"] = deadline.isoformat()
        self.state["planned_window_completed_at"] = None
        for prefix, window in (
            ("planned_window", selected),
            ("planned_comparison", comparison or selected),
        ):
            self.state[prefix + "_start"] = window.start.isoformat()
            self.state[prefix + "_end"] = window.end.isoformat()
            self.state[prefix + "_average"] = format(window.average, "f")
            self.state[prefix + "_intervals"] = window.interval_count

    def _update_preheat_completion(
        self,
        now: datetime,
        observation: DhwObservation,
        selected: PriceWindowChoice | None,
    ) -> None:
        if (
            selected is None
            or self.state.get("planned_window_completed_at") is not None
            or now.astimezone(UTC) < selected.start.astimezone(UTC)
            or observation.tank_temperature is None
            or observation.tank_temperature < self.policy.normal_target
        ):
            return
        self.state["planned_window_completed_at"] = now.astimezone(UTC).isoformat()

    def _update_learning(self, now: datetime, observation: DhwObservation) -> None:
        local_day = now.astimezone(TALLINN).date().isoformat()
        if self.mode == DHW_MODE_SHADOW and local_day not in self.state["shadow_days"]:
            self.state["shadow_days"] = (self.state["shadow_days"] + [local_day])[-30:]
        if (
            self.mode == DHW_MODE_READINESS
            and local_day not in self.state["readiness_pilot_days"]
        ):
            self.state["readiness_pilot_days"] = (
                self.state["readiness_pilot_days"] + [local_day]
            )[-30:]
        temperature = observation.tank_temperature
        target = observation.target_temperature
        local = now.astimezone(TALLINN)
        last_temperature = _raw_number(self.state.get("last_temperature"))
        last_observed = _instant(self.state.get("last_observed_at"))
        checks = dict(self.state["deadline_checks"])
        peaks = dict(self.state["readiness_window_peaks"])
        rapid_draws = dict(self.state["readiness_rapid_draws"])
        samples = dict(self.state["readiness_deadline_samples"])
        for day_offset in (-1, 0):
            day = local.date() + timedelta(days=day_offset)
            for ready in self.policy.readiness_times:
                ready_at = datetime.combine(day, ready, tzinfo=TALLINN)
                key = ready_at.isoformat()
                final_start = ready_at - timedelta(
                    minutes=self.heating_minutes
                )
                check_end = ready_at + timedelta(minutes=15)
                if (
                    temperature is not None
                    and final_start <= local < check_end
                ):
                    peak_record = peaks.get(key)
                    previous_peak = (
                        _raw_number(peak_record.get("temperature"))
                        if isinstance(peak_record, dict)
                        else None
                    )
                    peak_at = (
                        _instant(peak_record.get("observed_at"))
                        if isinstance(peak_record, dict)
                        else None
                    )
                    if previous_peak is None or temperature >= previous_peak:
                        previous_peak = temperature
                        peak_at = now
                        peaks[key] = {
                            "temperature": temperature,
                            "observed_at": now.isoformat(),
                        }
                    since_peak = (
                        now - peak_at.astimezone(UTC)
                        if peak_at is not None
                        else None
                    )
                    if (
                        last_temperature is not None
                        and last_observed is not None
                        and timedelta(0)
                        <= now - last_observed.astimezone(UTC)
                        <= timedelta(minutes=15)
                        and since_peak is not None
                        and timedelta(0)
                        <= since_peak
                        <= timedelta(minutes=15)
                        and previous_peak - temperature >= 3
                    ):
                        rapid_draws[key] = now.isoformat()
                if (
                    ready_at <= local < check_end
                    and key not in samples
                ):
                    samples[key] = {
                        "temperature": temperature,
                        "target": target,
                        "override_reason": observation.override_reason,
                        "observed_at": now.isoformat(),
                    }
                if local < check_end or key in checks:
                    continue
                if (
                    key not in samples
                    and key not in peaks
                    and key not in rapid_draws
                ):
                    continue
                sample = samples.get(key)
                sample_temperature = (
                    _raw_number(sample.get("temperature"))
                    if isinstance(sample, dict)
                    else None
                )
                sample_target = (
                    _raw_number(sample.get("target"))
                    if isinstance(sample, dict)
                    else None
                )
                sample_override = (
                    sample.get("override_reason")
                    if isinstance(sample, dict)
                    else None
                )
                peak_record = peaks.get(key)
                peak = (
                    _raw_number(peak_record.get("temperature"))
                    if isinstance(peak_record, dict)
                    else None
                )
                if (
                    sample_override is not None
                    or sample_temperature is None
                    or sample_target is None
                ):
                    outcome = READINESS_UNAVAILABLE
                elif key in rapid_draws and (
                    peak is not None
                    and peak >= self.policy.normal_target
                ):
                    outcome = READINESS_CONSUMED
                elif key in rapid_draws:
                    outcome = READINESS_CONTAMINATED
                elif sample_temperature >= self.policy.normal_target:
                    outcome = READINESS_SUCCESS
                else:
                    outcome = READINESS_FAILURE
                checks[key] = outcome
                checks = dict(list(checks.items())[-30:])
                if (
                    temperature is not None
                    and temperature < self.policy.normal_target
                    and outcome != READINESS_UNAVAILABLE
                ):
                    self.state["recovery_latched"] = True
                    if outcome == READINESS_CONSUMED:
                        self.state["recovery_reason"] = (
                            f"The {ready.strftime('%H:%M')} readiness target "
                            "was delivered before a rapid draw; hold 50°C "
                            "while recovering."
                        )
                    elif outcome == READINESS_CONTAMINATED:
                        self.state["recovery_reason"] = (
                            f"A rapid draw obscured the "
                            f"{ready.strftime('%H:%M')} readiness check; hold "
                            "50°C while recovering."
                        )
                    else:
                        self.state["recovery_reason"] = (
                            f"The {ready.strftime('%H:%M')} readiness deadline "
                            f"was missed at {sample_temperature:g}°C; hold "
                            "50°C until recovery completes."
                        )
        self.state["deadline_checks"] = checks
        self.state["readiness_window_peaks"] = dict(list(peaks.items())[-30:])
        self.state["readiness_rapid_draws"] = dict(
            list(rapid_draws.items())[-30:]
        )
        self.state["readiness_deadline_samples"] = dict(
            list(samples.items())[-30:]
        )
        if (
            observation.override_reason is not None
            or temperature is None
            or target is None
        ):
            return
        active_final_topups = dict(
            self.state["readiness_final_topup_active"]
        )
        completed_final_topups = dict(
            self.state["readiness_final_topups"]
        )
        for key in tuple(active_final_topups):
            deadline = _instant(key)
            if deadline is None:
                active_final_topups.pop(key, None)
            elif now.astimezone(UTC) >= deadline.astimezone(UTC):
                active_final_topups.pop(key, None)
                completed_final_topups[key] = now.isoformat()
        self.state["readiness_final_topup_active"] = dict(
            list(active_final_topups.items())[-30:]
        )
        self.state["readiness_final_topups"] = dict(
            list(completed_final_topups.items())[-30:]
        )
        cycle_start = _instant(self.state.get("cycle_started_at"))
        topup_start = _instant(self.state.get("topup_started_at"))
        if topup_start is not None and temperature >= self.policy.normal_target:
            duration = (now - topup_start).total_seconds() / 60
            if 5 <= duration <= 120:
                self.state["readiness_topup_durations"] = (
                    self.state["readiness_topup_durations"]
                    + [round(duration, 1)]
                )[-HEATING_DURATION_LIMIT:]
            self.state["topup_started_at"] = None
            self.state["topup_start_temperature"] = None
        if (
            cycle_start is None
            and temperature <= self.policy.floor_target
            and target >= self.policy.normal_target
            and observation.enabled is True
        ):
            self.state["cycle_started_at"] = now.isoformat()
            self.state["cycle_start_temperature"] = temperature
        elif cycle_start is not None:
            if last_temperature is not None and temperature < last_temperature - 2:
                self.state["cycle_started_at"] = now.isoformat()
                self.state["cycle_start_temperature"] = temperature
            elif temperature >= self.policy.normal_target:
                duration = (now - cycle_start).total_seconds() / 60
                start_temperature = _raw_number(self.state.get("cycle_start_temperature"))
                if start_temperature is not None and start_temperature <= self.policy.floor_target:
                    if 10 <= duration <= 180:
                        self.state["heating_durations"] = (
                            self.state["heating_durations"] + [round(duration, 1)]
                        )[-HEATING_DURATION_LIMIT:]
                self.state["cycle_started_at"] = None
                self.state["cycle_start_temperature"] = None
        self.state["last_temperature"] = temperature
        self.state["last_observed_at"] = now.isoformat()

    def _readiness_delivered(self, deadline: datetime) -> bool:
        peak_record = self.state["readiness_window_peaks"].get(
            deadline.isoformat()
        )
        peak = (
            _raw_number(peak_record.get("temperature"))
            if isinstance(peak_record, dict)
            else None
        )
        return peak is not None and peak >= self.policy.normal_target

    def _record_decision(self, now: datetime, decision: DhwDecision) -> None:
        previous = self.decision
        if (
            previous is not None
            and previous.state == decision.state
            and previous.desired_target == decision.desired_target
            and previous.reason == decision.reason
            and previous.selected_window == decision.selected_window
            and previous.comparison_window == decision.comparison_window
            and previous.preheat_price_gate == decision.preheat_price_gate
        ):
            return
        record = {
            "observed_at": now.isoformat(),
            "actual_target_c": decision.actual_target,
            "proposed_target_c": decision.desired_target,
            "state": decision.state,
            "reason": decision.reason,
            "price_gate": decision.price_gate,
            "preheat_price_gate": decision.preheat_price_gate,
            "preheat_price_advantage_eur_kwh": (
                format(decision.preheat_price_delta, "f")
                if decision.preheat_price_delta is not None else None
            ),
            "current_price_eur_kwh": (
                format(decision.current_price, "f")
                if decision.current_price is not None else None
            ),
            "selected_window_average_eur_kwh": (
                format(decision.selected_window.average, "f")
                if decision.selected_window else None
            ),
            "comparison_window_average_eur_kwh": (
                format(decision.comparison_window.average, "f")
                if decision.comparison_window else None
            ),
            "selected_window_start": (
                decision.selected_window.start.isoformat()
                if decision.selected_window else None
            ),
            "selected_window_end": (
                decision.selected_window.end.isoformat()
                if decision.selected_window else None
            ),
            "next_readiness": (
                decision.deadline.isoformat() if decision.deadline else None
            ),
            "write_sent": False,
        }
        self.state["decision_history"] = (
            self.state["decision_history"] + [record]
        )[-DECISION_HISTORY_LIMIT:]
        self.hass.bus.async_fire(EVENT_DHW_DECISION, record)

    def _opportunity_plan_id(self, decision: DhwDecision) -> str | None:
        if (
            decision.deadline is None
            or decision.selected_window is None
            or decision.comparison_window is None
        ):
            return None
        return "|".join((
            decision.deadline.isoformat(),
            decision.selected_window.start.isoformat(),
            decision.selected_window.end.isoformat(),
            decision.comparison_window.start.isoformat(),
            decision.comparison_window.end.isoformat(),
        ))

    def _record_optimization_opportunity(
        self, now: datetime, decision: DhwDecision
    ) -> None:
        plan_id = self._opportunity_plan_id(decision)
        if (
            plan_id is None
            or not decision.preheat_price_gate
            or decision.preheat_price_delta is None
            or decision.preheat_price_delta <= 0
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

    async def async_evaluate(self, now: datetime | None = None) -> None:
        async with self.lock:
            observed_at = (now or dt_util.utcnow()).astimezone(UTC)
            try:
                status = await self.devices.get_daikin_status()
                observation = self._observation(status)
                block_reason = self._extreme_block_reason(observed_at)
                if (
                    block_reason is not None
                    and observation.override_reason is None
                ):
                    observation = replace(
                        observation,
                        override_reason=block_reason,
                    )
                self._update_learning(observed_at, observation)
                if (
                    observation.tank_temperature is not None
                    and observation.tank_temperature >= self.policy.normal_target
                ):
                    self.state["recovery_latched"] = False
                    self.state["recovery_reason"] = None
                lease = _instant(self.state.get("lease_expires"))
                intervals = published_intervals(self.coordinator)
                deadline = next_readiness(observed_at, self.policy)
                planned_window, comparison_window = self._stored_price_plan(deadline)
                self._update_preheat_completion(
                    observed_at,
                    observation,
                    planned_window,
                )
                decision = plan_dhw(
                    now=observed_at,
                    mode=self.mode,
                    policy=self.policy,
                    observation=observation,
                    intervals=intervals,
                    heating_minutes=self.heating_minutes,
                    lease_expires=lease.astimezone(UTC) if lease else None,
                    planned_window=planned_window,
                    comparison_window=comparison_window,
                    recovery_latched=self.state["recovery_latched"] is True,
                    recovery_reason=self.state.get("recovery_reason"),
                    preheat_completed=(
                        self.state.get("planned_window_completed_at") is not None
                    ),
                    readiness_delivered=self._readiness_delivered(deadline),
                    final_topup_used=bool(
                        self.state["readiness_final_topups"].get(
                            deadline.isoformat()
                        )
                    ),
                    final_topup_active=bool(
                        self.state[
                            "readiness_final_topup_active"
                        ].get(deadline.isoformat())
                    ),
                )
                if (
                    planned_window is None
                    and decision.data_complete
                    and decision.selected_window is not None
                    and decision.deadline is not None
                ):
                    self._store_price_plan(
                        decision.deadline,
                        decision.selected_window,
                        decision.comparison_window,
                    )
                    self._update_preheat_completion(
                        observed_at,
                        observation,
                        decision.selected_window,
                    )
                if (
                    decision.state == "recovery"
                    and observation.tank_temperature is not None
                    and observation.tank_temperature < self.policy.normal_target
                ):
                    self.state["recovery_latched"] = True
                    self.state["recovery_reason"] = decision.reason
                if self.state.get("fault"):
                    decision = replace(
                        decision,
                        state="fault",
                        reason=self.state["fault"],
                        desired_target=None,
                        allow_write=False,
                    )
                last_transition = _instant(
                    self.state.get("last_policy_transition_at")
                )
                last_target = _raw_number(self.state.get("last_policy_target"))
                decision = stabilize_decision(
                    now=observed_at,
                    decision=decision,
                    last_target=last_target,
                    last_transition=last_transition,
                )
                if (
                    decision.desired_target is not None
                    and decision.desired_target != last_target
                ):
                    self.state["last_policy_target"] = decision.desired_target
                    self.state["last_policy_transition_at"] = observed_at.isoformat()
                self._record_optimization_opportunity(observed_at, decision)
                self._record_decision(observed_at, decision)
                self.decision = decision
                await self._apply_decision(observed_at, observation, decision)
            except (HomeAssistantError, TimeoutError, ValueError, KeyError) as error:
                self.decision = DhwDecision(
                    mode=self.mode,
                    state="unavailable",
                    reason=str(error),
                    desired_target=None,
                    actual_target=None,
                    tank_temperature=None,
                    deadline=None,
                    selected_window=None,
                    current_price=None,
                    price_delta=None,
                    price_delta_percent=None,
                    price_gate=False,
                    data_complete=False,
                    lease_expires=_instant(self.state.get("lease_expires")),
                    allow_write=False,
                    urgent=False,
                )
            await self._save()
            self._notify_listeners()

    async def _apply_decision(
        self,
        now: datetime,
        observation: DhwObservation,
        decision: DhwDecision,
    ) -> None:
        target = decision.desired_target
        final_topup_key = (
            decision.deadline.isoformat()
            if (
                decision.allow_write
                and decision.state == "readiness_final_topup"
                and decision.deadline is not None
            )
            else None
        )
        if (
            final_topup_key is not None
            and target == observation.target_temperature
            and target == self.policy.preheat_target
            and self.state.get("last_owned_target") == target
        ):
            active = dict(self.state["readiness_final_topup_active"])
            active[final_topup_key] = now.isoformat()
            self.state["readiness_final_topup_active"] = dict(
                list(active.items())[-30:]
            )
        if (
            not decision.allow_write
            or target is None
            or observation.target_temperature is None
            or target == observation.target_temperature
        ):
            return
        if self._extreme_block_reason(now) is not None:
            return
        last_write = _instant(self.state.get("last_write_at"))
        if (
            not decision.urgent
            and last_write is not None
            and now - last_write.astimezone(UTC) < MARKET_INTERVAL
        ):
            return
        try:
            result = await self.devices.control_daikin(
                {
                    "operation": "hot_water_target",
                    "value": target,
                    "expected_previous": observation.target_temperature,
                },
                Context(),
            )
        except DaikinPreDispatchConflict:
            return
        except (HomeAssistantError, TimeoutError) as error:
            self.state["fault"] = (
                "A guarded DHW command failed and may have applied. "
                "Read fresh state, then use Resume after review. " + str(error)
            )
            persistent_notification.async_create(
                self.hass,
                self.state["fault"],
                "Hot-water optimizer paused",
                NOTIFICATION_ID,
            )
            return
        confirmed = _raw_number(result.get("confirmed"))
        if confirmed != target:
            self.state["fault"] = "The guarded DHW command returned an unexpected confirmation."
            return
        if final_topup_key is not None:
            active = dict(self.state["readiness_final_topup_active"])
            active[final_topup_key] = now.isoformat()
            self.state["readiness_final_topup_active"] = dict(
                list(active.items())[-30:]
            )
        self.state["last_owned_target"] = target
        self.state["last_write_at"] = now.isoformat()
        changed = int(result.get("changed") is True)
        self.state["confirmed_writes"] += changed
        if self.mode == DHW_MODE_READINESS:
            if self.state["readiness_pilot_started_at"] is None:
                self.state["readiness_pilot_started_at"] = now.isoformat()
            self.state["readiness_pilot_confirmed_writes"] += changed
        if self.mode == DHW_MODE_SAFE:
            self.state["safe_pilot_confirmed_writes"] += changed
        self.state["lease_expires"] = None
        if (
            changed
            and decision.state
            in ("readiness_charge", "readiness_final_topup")
            and observation.tank_temperature is not None
            and observation.tank_temperature > self.policy.floor_target
            and self.state["topup_started_at"] is None
        ):
            self.state["topup_started_at"] = now.isoformat()
            self.state["topup_start_temperature"] = (
                observation.tank_temperature
            )
        if (
            self.mode == DHW_MODE_SAFE
            and self.state["pilot_started_at"] is None
        ):
            self.state["pilot_started_at"] = now.isoformat()
        if self.state["decision_history"]:
            self.state["decision_history"][-1]["write_sent"] = bool(result.get("changed"))

    async def async_set_mode(
        self,
        mode: str,
        *,
        owner_approved: bool = False,
    ) -> None:
        if mode not in DHW_MODES:
            raise HomeAssistantError("Unknown DHW optimizer mode.")
        approval = self.state.get("owner_full_control_approved_at")
        if (
            mode == DHW_MODE_SAFE
            and not self.shadow_ready
            and not approval
            and not owner_approved
        ):
            raise HomeAssistantError(
                "Safe target shift requires seven observed Shadow/readiness-pilot "
                "days, five usable heating cycles, ten delivered readiness checks "
                "with no genuine failure, and at least two confirmed readiness-pilot "
                "writes."
            )
        if owner_approved:
            status = await self.devices.get_daikin_status()
            observation = self._observation(status)
            if observation.override_reason is not None:
                raise HomeAssistantError(observation.override_reason)
            if observation.enabled is not True:
                raise HomeAssistantError(
                    "The DHW circuit must be on before owner-approved activation."
                )
            self.state["owner_full_control_approved_at"] = (
                dt_util.utcnow().astimezone(UTC).isoformat()
            )
        previous = self.mode
        self.mode = mode
        if mode != previous:
            self.state["last_policy_target"] = None
            self.state["last_policy_transition_at"] = None
        if mode in (DHW_MODE_PAUSED, DHW_MODE_SHADOW) and previous in (
            DHW_MODE_READINESS,
            DHW_MODE_SAFE,
        ):
            await self.async_return_to_normal(evaluate=False, pause=False)
        if (
            mode == DHW_MODE_READINESS
            and self.state["readiness_pilot_started_at"] is None
        ):
            self.state["readiness_pilot_started_at"] = (
                dt_util.utcnow().astimezone(UTC).isoformat()
            )
        self.state["lease_expires"] = None
        await self._save()
        await self.async_evaluate()

    async def async_activate_full_control(self) -> None:
        await self.async_set_mode(DHW_MODE_SAFE, owner_approved=True)

    async def async_resume(self) -> None:
        await self.devices.get_daikin_status()
        self.state["fault"] = None
        self.state["manual_override"] = None
        self.state["last_policy_target"] = None
        self.state["last_policy_transition_at"] = None
        persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
        await self._save()
        await self.async_evaluate()

    async def async_return_to_normal(
        self, *, evaluate: bool = True, pause: bool = True
    ) -> None:
        if pause:
            self.mode = DHW_MODE_PAUSED
            await self._save()
        status = await self.devices.get_daikin_status()
        observation = self._observation(status)
        owned = _raw_number(self.state.get("last_owned_target"))
        current = observation.target_temperature
        if current == self.policy.normal_target:
            self.state["last_owned_target"] = None
            self.state["lease_expires"] = None
            self.state["manual_override"] = None
            self.state["last_policy_target"] = None
            self.state["last_policy_transition_at"] = None
            await self._save()
            if evaluate:
                await self.async_evaluate()
            return
        if owned is None or current != owned:
            raise HomeAssistantError(
                "The optimizer does not own the current DHW target; it was preserved."
            )
        block_reason = self._extreme_block_reason(dt_util.utcnow())
        if block_reason is not None:
            raise HomeAssistantError(block_reason)
        try:
            result = await self.devices.control_daikin(
                {
                    "operation": "hot_water_target",
                    "value": self.policy.normal_target,
                    "expected_previous": current,
                },
                Context(),
            )
        except DaikinPreDispatchConflict:
            await self._save()
            raise
        except (HomeAssistantError, TimeoutError) as error:
            self.state["fault"] = (
                "Return to normal failed and may have applied; inspect fresh state. " + str(error)
            )
            await self._save()
            raise
        if _raw_number(result.get("confirmed")) != self.policy.normal_target:
            raise HomeAssistantError("Return to normal was not confirmed.")
        self.state["last_owned_target"] = None
        self.state["lease_expires"] = None
        self.state["manual_override"] = None
        self.state["last_policy_target"] = None
        self.state["last_policy_transition_at"] = None
        self.state["last_write_at"] = dt_util.utcnow().isoformat()
        await self._save()
        if evaluate:
            await self.async_evaluate()

    @property
    def optimization_value(self) -> Decimal | None:
        decision = self.decision
        if (
            decision is None
            or decision.selected_window is None
            or decision.comparison_window is None
        ):
            return None
        return decision.preheat_price_delta

    @property
    def optimization_value_attributes(self) -> dict:
        decision = self.decision
        plan_id = self._opportunity_plan_id(decision) if decision else None
        if (
            decision is None
            or decision.selected_window is None
            or decision.comparison_window is None
        ):
            return {
                "metric": "price_opportunity_per_shifted_kwh",
                "controller": "domestic_hot_water",
                "valuation_status": "unavailable",
                "plan_id": None,
                "measured_shifted_energy_kwh": None,
                "estimated_savings_eur": None,
                "realized_savings_eur": None,
                "savings_unavailable_reason": (
                    "No anchored DHW price plan is currently available."
                ),
            }
        now = dt_util.utcnow().astimezone(UTC)
        completed_at = self.state.get("planned_window_completed_at")
        if completed_at is not None:
            plan_status = "observed_target_reached"
        elif now < decision.selected_window.start:
            plan_status = "planned"
        elif now < decision.selected_window.end:
            plan_status = "active"
        else:
            plan_status = "window_elapsed"
        return {
            "metric": "price_opportunity_per_shifted_kwh",
            "controller": "domestic_hot_water",
            "valuation_status": (
                "counterfactual"
                if self.mode in (DHW_MODE_SHADOW, DHW_MODE_READINESS)
                else "live_action_unattributed"
            ),
            "operating_mode": self.mode,
            "shadow_zero_writes": self.mode == DHW_MODE_SHADOW,
            "plan_id": plan_id,
            "plan_status": plan_status,
            "opportunity_qualified": decision.preheat_price_gate,
            "opportunity_eur_per_kwh": (
                format(decision.preheat_price_delta, "f")
                if decision.preheat_price_delta is not None else None
            ),
            "opportunity_percent": (
                format(decision.preheat_price_delta_percent * Decimal(100), "f")
                if decision.preheat_price_delta_percent is not None else None
            ),
            "cheap_window_start": decision.selected_window.start.isoformat(),
            "cheap_window_end": decision.selected_window.end.isoformat(),
            "cheap_window_average_eur_kwh": format(
                decision.selected_window.average, "f"
            ),
            "comparison_window_start": (
                decision.comparison_window.start.isoformat()
            ),
            "comparison_window_end": decision.comparison_window.end.isoformat(),
            "comparison_window_average_eur_kwh": format(
                decision.comparison_window.average, "f"
            ),
            "readiness_deadline": (
                decision.deadline.isoformat() if decision.deadline else None
            ),
            "qualified_opportunities_recorded": self.state[
                "qualified_opportunity_count"
            ],
            "last_qualified_opportunity_at": self.state[
                "last_qualified_opportunity_at"
            ],
            "observed_target_reached": completed_at is not None,
            "observed_target_reached_at": completed_at,
            "confirmed_writes": self.state["confirmed_writes"],
            "measured_shifted_energy_kwh": None,
            "estimated_savings_eur": None,
            "realized_savings_eur": None,
            "savings_unavailable_reason": (
                "Whole-Daikin electrical energy attributable to this plan is "
                "not measured."
            ),
        }

    @property
    def attributes(self) -> dict:
        decision = self.decision
        result = decision.attributes() if decision else {
            "mode": self.mode,
            "state": "starting",
            "reason": "The optimizer has not completed its first evaluation.",
        }
        recent_event_window = list(self.state["decision_history"][-24:])
        recent_decisions = list(
            self.state["decision_history"][
                -RECENT_DECISIONS_ATTRIBUTE_LIMIT:
            ]
        )
        deadline_checks = self.state["deadline_checks"]
        qualification_days = set(self.state["shadow_days"]) | set(
            self.state["readiness_pilot_days"]
        )
        pilot_started = _instant(self.state.get("pilot_started_at"))
        result.update({
            "shadow_zero_writes": self.mode == DHW_MODE_SHADOW,
            "shadow_days": len(self.state["shadow_days"]),
            "readiness_pilot_days": len(self.state["readiness_pilot_days"]),
            "qualification_days": len(qualification_days),
            "usable_heating_cycles": len(self.state["heating_durations"]),
            "usable_readiness_topups": len(
                self.state["readiness_topup_durations"]
            ),
            "heating_duration_minutes": self.heating_minutes,
            "shadow_ready": self.shadow_ready,
            "soft_standby_ready": False,
            "automatic_soft_standby_enabled": False,
            "confirmed_writes": self.state["confirmed_writes"],
            "readiness_pilot_confirmed_writes": self.state[
                "readiness_pilot_confirmed_writes"
            ],
            "safe_pilot_confirmed_writes": self.state[
                "safe_pilot_confirmed_writes"
            ],
            "owner_full_control_approved_at": self.state[
                "owner_full_control_approved_at"
            ],
            "owner_full_control_approved": bool(
                self.state["owner_full_control_approved_at"]
            ),
            "pilot_days": (
                max(0, (dt_util.utcnow() - pilot_started.astimezone(UTC)).days)
                if pilot_started else 0
            ),
            "readiness_checks": len(deadline_checks),
            "readiness_usable_checks": sum(
                value in READINESS_USABLE for value in deadline_checks.values()
            ),
            "readiness_successes": sum(
                value == READINESS_SUCCESS for value in deadline_checks.values()
            ),
            "readiness_consumed_after_delivery": sum(
                value == READINESS_CONSUMED for value in deadline_checks.values()
            ),
            "readiness_draw_contaminated": sum(
                value == READINESS_CONTAMINATED
                for value in deadline_checks.values()
            ),
            "readiness_failures": sum(
                value == READINESS_FAILURE for value in deadline_checks.values()
            ),
            "readiness_unavailable": sum(
                value == READINESS_UNAVAILABLE for value in deadline_checks.values()
            ),
            "recent_recovery_events": sum(
                record.get("state") == "recovery"
                for record in recent_event_window
            ),
            "recent_override_events": sum(
                record.get("state") == "override"
                for record in recent_event_window
            ),
            "fault": self.state["fault"],
            "manual_override": self.state["manual_override"],
            "policy_revision": self.state["policy_revision"],
            "recovery_latched": self.state["recovery_latched"],
            "recovery_reason": self.state["recovery_reason"],
            "price_window_anchored": self.state["planned_window_start"] is not None,
            "price_window_completed": (
                self.state["planned_window_completed_at"] is not None
            ),
            "price_window_completed_at": self.state[
                "planned_window_completed_at"
            ],
            "minimum_target_dwell_minutes": int(
                MINIMUM_TARGET_DWELL.total_seconds() // 60
            ),
            "post_readiness_hold_minutes": DHW_POST_READINESS_HOLD_MINUTES,
            "post_delivery_rearm_c": DHW_POST_DELIVERY_REARM_C,
            "final_topup_window_minutes": DHW_FINAL_TOPUP_MINUTES,
            "readiness_evidence_delay_minutes": 15,
            "extreme_price_block_reason": self._extreme_block_reason(
                dt_util.utcnow()
            ),
            "recent_decisions": recent_decisions,
            "policy": {
                "morning_ready": self.policy.morning_ready.isoformat(timespec="minutes"),
                "evening_ready": self.policy.evening_ready.isoformat(timespec="minutes"),
                "soft_target_c": self.policy.soft_target,
                "floor_target_c": self.policy.floor_target,
                "normal_target_c": self.policy.normal_target,
                "preheat_target_c": self.policy.preheat_target,
                "max_lease_hours": self.policy.max_lease.total_seconds() / 3600,
                "price_delta_eur_kwh": format(self.policy.price_delta, "f"),
                "price_delta_percent": format(
                    self.policy.price_percent * Decimal(100), "f"
                ),
                "coast_horizon_hours": self.policy.coast_horizon.total_seconds() / 3600,
            },
        })
        return result
