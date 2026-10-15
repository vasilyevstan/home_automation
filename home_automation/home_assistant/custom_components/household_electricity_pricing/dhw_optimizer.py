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
    DHW_MODE_PAUSED,
    DHW_MODE_SAFE,
    DHW_MODE_SHADOW,
    DHW_MODE_SOFT,
    DHW_MODES,
    DOMAIN,
    EVENT_DHW_DECISION,
)
from .controls import EnergyDevices, FLAGS
from .coordinator import HouseholdPriceCoordinator


STORE_VERSION = 1
STORE_KEY_SUFFIX = "dhw_optimizer"
NOTIFICATION_ID = DOMAIN + "_dhw_optimizer_fault"
DECISION_HISTORY_LIMIT = 96
HEATING_DURATION_LIMIT = 20
POLICY_REVISION = 2
MINIMUM_TARGET_DWELL = timedelta(minutes=30)


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


def _quarter_start(now: datetime) -> datetime:
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
    current = _quarter_start(now)
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


def _price_gate(
    current: Decimal | None,
    selected: PriceWindowChoice | None,
    policy: DhwPolicy,
) -> tuple[bool, Decimal | None, Decimal | None]:
    if current is None or selected is None:
        return False, None, None
    delta = current - selected.average
    base = max(abs(selected.average), Decimal("0.05"))
    relative = delta / base
    return (
        delta >= policy.price_delta and relative >= policy.price_percent,
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
) -> DhwDecision:
    if now.utcoffset() is None:
        raise HomeAssistantError("DHW planning time must include a UTC offset.")
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
    gate, delta, relative = _price_gate(current_price, selected, policy)
    preheat_gate, preheat_delta, preheat_relative = _price_gate(
        comparison.average if comparison is not None else None,
        selected,
        policy,
    )
    allow_write = mode in (DHW_MODE_SAFE, DHW_MODE_SOFT)

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
    remaining = deadline.astimezone(UTC) - now.astimezone(UTC)
    if remaining <= timedelta(minutes=heating_minutes):
        return decision(
            "readiness",
            "The next readiness deadline is inside the conservative heating allowance.",
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
    if observation.tank_temperature < policy.normal_target and selected.end <= now_utc:
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

    effective_mode = DHW_MODE_SAFE if mode == DHW_MODE_SHADOW else mode
    if effective_mode == DHW_MODE_SOFT:
        if lease_expires is not None:
            if lease_expires <= now_utc:
                return decision(
                    "normal",
                    "The four-hour soft-standby lease expired.",
                    policy.normal_target,
                    urgent=True,
                )
            return decision(
                "soft_standby",
                "The cost gate remains active inside the bounded 30°C soft-standby lease.",
                policy.soft_target,
            )
        if observation.tank_temperature >= policy.normal_target:
            return decision(
                "soft_standby",
                "The cost gate is active and the tank is warm enough to start a bounded lease.",
                policy.soft_target,
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
            "shadow_days": [],
            "heating_durations": [],
            "cycle_started_at": None,
            "cycle_start_temperature": None,
            "last_temperature": None,
            "last_observed_at": None,
            "decision_history": [],
            "confirmed_writes": 0,
            "pilot_started_at": None,
            "deadline_checks": {},
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
            "shadow_days": [],
            "heating_durations": [],
            "cycle_started_at": None,
            "cycle_start_temperature": None,
            "last_temperature": None,
            "last_observed_at": None,
            "decision_history": [],
            "confirmed_writes": 0,
            "pilot_started_at": None,
            "deadline_checks": {},
        })

    async def async_start(self) -> None:
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            stored_revision = stored.get("policy_revision")
            mode = stored.get("mode")
            if mode in DHW_MODES:
                self.mode = mode
            for key in self.state:
                if key in stored:
                    self.state[key] = stored[key]
            if stored_revision != POLICY_REVISION:
                self._reset_policy_evidence()
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
    def _schedule_evaluation(self) -> None:
        self.hass.async_create_task(self.async_evaluate())

    async def _async_interval(self, now: datetime) -> None:
        await self.async_evaluate(now)

    async def _save(self) -> None:
        await self.store.async_save({"mode": self.mode, **self.state})

    @property
    def shadow_ready(self) -> bool:
        checks = self.state["deadline_checks"]
        return (
            len(self.state["shadow_days"]) >= 7
            and len(self.state["heating_durations"]) >= 5
            and len(checks) >= 10
            and all(checks.values())
            and not self.state["fault"]
            and not self.state["manual_override"]
        )

    @property
    def soft_ready(self) -> bool:
        started = _instant(self.state.get("pilot_started_at"))
        checks = self.state["deadline_checks"]
        return (
            started is not None
            and dt_util.utcnow() - started.astimezone(UTC) >= timedelta(days=7)
            and self.state["confirmed_writes"] >= 10
            and not self.state["fault"]
            and bool(checks)
            and all(checks.values())
        )

    @property
    def heating_minutes(self) -> int:
        values = sorted(
            float(value)
            for value in self.state["heating_durations"]
            if isinstance(value, (int, float)) and math.isfinite(float(value))
        )
        if len(values) < 5:
            return self.policy.initial_heat_minutes
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
        for prefix, window in (
            ("planned_window", selected),
            ("planned_comparison", comparison or selected),
        ):
            self.state[prefix + "_start"] = window.start.isoformat()
            self.state[prefix + "_end"] = window.end.isoformat()
            self.state[prefix + "_average"] = format(window.average, "f")
            self.state[prefix + "_intervals"] = window.interval_count

    def _update_learning(self, now: datetime, observation: DhwObservation) -> None:
        local_day = now.astimezone(TALLINN).date().isoformat()
        if self.mode == DHW_MODE_SHADOW and local_day not in self.state["shadow_days"]:
            self.state["shadow_days"] = (self.state["shadow_days"] + [local_day])[-30:]
        temperature = observation.tank_temperature
        target = observation.target_temperature
        if temperature is None or target is None:
            return
        cycle_start = _instant(self.state.get("cycle_started_at"))
        last_temperature = _raw_number(self.state.get("last_temperature"))
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
        local = now.astimezone(TALLINN)
        for ready in self.policy.readiness_times:
            ready_at = datetime.combine(local.date(), ready, tzinfo=TALLINN)
            if ready_at <= local < ready_at + timedelta(minutes=15):
                key = ready_at.isoformat()
                if key not in self.state["deadline_checks"]:
                    ready_met = temperature >= self.policy.normal_target
                    checks = dict(self.state["deadline_checks"])
                    checks[key] = ready_met
                    self.state["deadline_checks"] = dict(list(checks.items())[-30:])
                    if not ready_met:
                        self.state["recovery_latched"] = True
                        self.state["recovery_reason"] = (
                            f"The {ready.strftime('%H:%M')} readiness deadline was "
                            f"missed at {temperature:g}°C; hold 50°C until recovery completes."
                        )

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

    async def async_evaluate(self, now: datetime | None = None) -> None:
        async with self.lock:
            observed_at = (now or dt_util.utcnow()).astimezone(UTC)
            try:
                status = await self.devices.get_daikin_status()
                observation = self._observation(status)
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
        if (
            not decision.allow_write
            or target is None
            or observation.target_temperature is None
            or target == observation.target_temperature
        ):
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
                {"operation": "hot_water_target", "value": target},
                Context(),
            )
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
        self.state["last_owned_target"] = target
        self.state["last_write_at"] = now.isoformat()
        self.state["confirmed_writes"] += int(result.get("changed") is True)
        if target == self.policy.soft_target:
            self.state["lease_expires"] = (now + self.policy.max_lease).isoformat()
        else:
            self.state["lease_expires"] = None
        if self.mode in (DHW_MODE_SAFE, DHW_MODE_SOFT) and self.state["pilot_started_at"] is None:
            self.state["pilot_started_at"] = now.isoformat()
        if self.state["decision_history"]:
            self.state["decision_history"][-1]["write_sent"] = bool(result.get("changed"))

    async def async_set_mode(self, mode: str) -> None:
        if mode not in DHW_MODES:
            raise HomeAssistantError("Unknown DHW optimizer mode.")
        if mode == DHW_MODE_SAFE and not self.shadow_ready:
            raise HomeAssistantError(
                "Shadow promotion requires seven observed days, five usable heating "
                "cycles, and at least ten successful readiness checks with no failure."
            )
        if mode == DHW_MODE_SOFT and not self.soft_ready:
            raise HomeAssistantError(
                "Soft standby requires the completed seven-day safe pilot and ten confirmed writes."
            )
        previous = self.mode
        self.mode = mode
        if mode != previous:
            self.state["last_policy_target"] = None
            self.state["last_policy_transition_at"] = None
        if mode in (DHW_MODE_PAUSED, DHW_MODE_SHADOW) and previous in (
            DHW_MODE_SAFE,
            DHW_MODE_SOFT,
        ):
            await self.async_return_to_normal(evaluate=False, pause=False)
        if mode != DHW_MODE_SOFT:
            self.state["lease_expires"] = None
        await self._save()
        await self.async_evaluate()

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
        try:
            result = await self.devices.control_daikin(
                {"operation": "hot_water_target", "value": self.policy.normal_target},
                Context(),
            )
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
    def attributes(self) -> dict:
        decision = self.decision
        result = decision.attributes() if decision else {
            "mode": self.mode,
            "state": "starting",
            "reason": "The optimizer has not completed its first evaluation.",
        }
        recent_decisions = list(self.state["decision_history"][-24:])
        deadline_checks = self.state["deadline_checks"]
        pilot_started = _instant(self.state.get("pilot_started_at"))
        result.update({
            "shadow_zero_writes": self.mode == DHW_MODE_SHADOW,
            "shadow_days": len(self.state["shadow_days"]),
            "usable_heating_cycles": len(self.state["heating_durations"]),
            "heating_duration_minutes": self.heating_minutes,
            "shadow_ready": self.shadow_ready,
            "soft_standby_ready": self.soft_ready,
            "confirmed_writes": self.state["confirmed_writes"],
            "pilot_days": (
                max(0, (dt_util.utcnow() - pilot_started.astimezone(UTC)).days)
                if pilot_started else 0
            ),
            "readiness_checks": len(deadline_checks),
            "readiness_successes": sum(
                value is True for value in deadline_checks.values()
            ),
            "readiness_failures": sum(
                value is False for value in deadline_checks.values()
            ),
            "recent_recovery_events": sum(
                record.get("state") == "recovery" for record in recent_decisions
            ),
            "recent_override_events": sum(
                record.get("state") == "override" for record in recent_decisions
            ),
            "fault": self.state["fault"],
            "manual_override": self.state["manual_override"],
            "policy_revision": self.state["policy_revision"],
            "recovery_latched": self.state["recovery_latched"],
            "recovery_reason": self.state["recovery_reason"],
            "price_window_anchored": self.state["planned_window_start"] is not None,
            "minimum_target_dwell_minutes": int(
                MINIMUM_TARGET_DWELL.total_seconds() // 60
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
