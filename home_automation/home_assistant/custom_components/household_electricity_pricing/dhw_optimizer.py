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
    DHW_MODE_READINESS,
    DHW_MODE_SAFE,
    DHW_MODE_SHADOW,
    DHW_MODE_SOFT,
    DHW_MODES,
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
RETENTION_SAMPLE_LIMIT = 20
POLICY_REVISION = 10
MINIMUM_TARGET_DWELL = timedelta(minutes=30)
POST_READINESS_HOLD = timedelta(
    minutes=DHW_POST_READINESS_HOLD_MINUTES
)
MAX_STORAGE_HORIZON = timedelta(hours=8)
SHALLOW_HEATING_FALLBACK_MINUTES = 45
SHALLOW_HEATING_MIN_MINUTES = 30
SHALLOW_HEATING_MAX_MINUTES = 60
RETENTION_MINIMUM_DURATION = timedelta(minutes=30)
RETENTION_MAX_LOSS_RATE_C_PER_HOUR = 3.0
RETENTION_REQUIRED_SAMPLES = 3
RETENTION_REQUIRED_CYCLES = 2
RECENT_DECISIONS_ATTRIBUTE_LIMIT = 4
PLAN_KIND_ECONOMIC = "economic_storage"
PLAN_KIND_PREFERRED = "preferred_readiness"
READINESS_SUCCESS = "success"
READINESS_CONSUMED = "consumed"
READINESS_ACCEPTED = "usable_below_preferred"
READINESS_CONTAMINATED = "draw_contaminated"
READINESS_FAILURE = "failure"
READINESS_UNAVAILABLE = "unavailable"
READINESS_USABLE = frozenset(
    (
        READINESS_SUCCESS,
        READINESS_CONSUMED,
        READINESS_ACCEPTED,
        READINESS_FAILURE,
    )
)
READINESS_DELIVERED = frozenset(
    (READINESS_SUCCESS, READINESS_CONSUMED, READINESS_ACCEPTED)
)


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
    absolute_cheapest_window: PriceWindowChoice | None = None
    readiness_heating_minutes: int | None = None
    readiness_heating_profile: str | None = None
    retention_qualified: bool = False
    retention_sample_count: int = 0
    retention_cycle_count: int = 0
    retention_loss_rate_c_per_hour: float | None = None
    adaptive_preheat_target_c: float | None = None
    plan_kind: str | None = None
    planned_target_c: float | None = None
    protected_endpoint: datetime | None = None

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
            "readiness_heating_minutes": self.readiness_heating_minutes,
            "readiness_heating_profile": self.readiness_heating_profile,
            "retention_model_qualified": self.retention_qualified,
            "retention_sample_count": self.retention_sample_count,
            "retention_cycle_count": self.retention_cycle_count,
            "retention_loss_rate_c_per_hour": (
                round(self.retention_loss_rate_c_per_hour, 3)
                if self.retention_loss_rate_c_per_hour is not None
                else None
            ),
            "adaptive_preheat_target_c": self.adaptive_preheat_target_c,
            "price_plan_kind": self.plan_kind,
            "planned_target_c": self.planned_target_c,
            "protected_endpoint": (
                self.protected_endpoint.isoformat()
                if self.protected_endpoint is not None
                else None
            ),
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
        if self.absolute_cheapest_window is None:
            result.update({
                "absolute_cheapest_window_start": None,
                "absolute_cheapest_window_end": None,
                "absolute_cheapest_window_average_eur_kwh": None,
            })
        else:
            result.update({
                "absolute_cheapest_window_start": (
                    self.absolute_cheapest_window.start.isoformat()
                ),
                "absolute_cheapest_window_end": (
                    self.absolute_cheapest_window.end.isoformat()
                ),
                "absolute_cheapest_window_average_eur_kwh": format(
                    self.absolute_cheapest_window.average,
                    "f",
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


def quarter_ceiling(instant: datetime) -> datetime:
    start = quarter_start(instant)
    return (
        start
        if start == instant.astimezone(UTC)
        else start + MARKET_INTERVAL
    )


def _price_map(intervals: Iterable[PriceInterval]) -> dict[datetime, PriceInterval]:
    result = {}
    for item in intervals:
        if item.start in result:
            return {}
        result[item.start] = item
    return result


def _complete_price_schedule(
    now: datetime,
    deadline: datetime,
    intervals: Iterable[PriceInterval],
) -> tuple[dict[datetime, PriceInterval], Decimal | None, bool]:
    prices = _price_map(intervals)
    current = quarter_start(now)
    current_row = prices.get(current)
    if (
        current_row is None
        or current_row.end != current + MARKET_INTERVAL
    ):
        return prices, None, False
    deadline_utc = deadline.astimezone(UTC)
    cursor = current
    while cursor < deadline_utc:
        row = prices.get(cursor)
        if row is None or row.end != cursor + MARKET_INTERVAL:
            return prices, current_row.price, False
        cursor += MARKET_INTERVAL
    return prices, current_row.price, True


def _window_choice(
    prices: Mapping[datetime, PriceInterval],
    start: datetime,
    interval_count: int,
) -> PriceWindowChoice | None:
    rows = [
        prices.get(start + index * MARKET_INTERVAL)
        for index in range(interval_count)
    ]
    if not all(row is not None for row in rows):
        return None
    total = sum(
        (row.price for row in rows if row is not None),
        Decimal(0),
    )
    return PriceWindowChoice(
        start=start,
        end=start + interval_count * MARKET_INTERVAL,
        average=total / interval_count,
        interval_count=interval_count,
    )


def _window_matches_prices(
    window: PriceWindowChoice,
    prices: Mapping[datetime, PriceInterval],
) -> bool:
    current = _window_choice(
        prices,
        window.start,
        window.interval_count,
    )
    return (
        current is not None
        and current.end == window.end
        and current.average == window.average
    )


def _window_choices(
    prices: Mapping[datetime, PriceInterval],
    first: datetime,
    last: datetime,
    duration_minutes: int,
) -> tuple[PriceWindowChoice, ...]:
    interval_count = max(1, math.ceil(duration_minutes / 15))
    result = []
    candidate = first
    while candidate <= last:
        choice = _window_choice(prices, candidate, interval_count)
        if choice is not None:
            result.append(choice)
        candidate += MARKET_INTERVAL
    return tuple(result)


def _minimum_average_choice(
    choices: Iterable[PriceWindowChoice],
) -> PriceWindowChoice | None:
    values = tuple(choices)
    if not values:
        return None
    return min(
        values,
        key=lambda choice: (
            choice.average,
            -choice.start.timestamp(),
        ),
    )


def _retention_horizon(
    policy: DhwPolicy,
    target: float,
    loss_rate_c_per_hour: float,
) -> timedelta:
    configured = min(policy.coast_horizon, MAX_STORAGE_HORIZON)
    if loss_rate_c_per_hour == 0:
        return configured
    thermal_hours = max(
        0.0,
        (target - policy.floor_target) / loss_rate_c_per_hour,
    )
    return min(configured, timedelta(hours=thermal_hours))


def _predicted_endpoint_temperature(
    target: float,
    window: PriceWindowChoice,
    endpoint: datetime,
    loss_rate_c_per_hour: float,
) -> float:
    hold_hours = max(
        0.0,
        (
            endpoint.astimezone(UTC) - window.end.astimezone(UTC)
        ).total_seconds()
        / 3600,
    )
    return target - loss_rate_c_per_hour * hold_hours


def select_price_window(
    now: datetime,
    deadline: datetime,
    intervals: Iterable[PriceInterval],
    duration_minutes: int,
    coast_horizon: timedelta,
    planned_window: PriceWindowChoice | None = None,
    comparison_window: PriceWindowChoice | None = None,
    absolute_cheapest_window: PriceWindowChoice | None = None,
    comparison_duration_minutes: int | None = None,
    prefer_latest_near_cheapest: bool = False,
) -> tuple[
    PriceWindowChoice | None,
    PriceWindowChoice | None,
    PriceWindowChoice | None,
    Decimal | None,
    bool,
]:
    _ = prefer_latest_near_cheapest
    prices, current_price, complete = _complete_price_schedule(
        now,
        deadline,
        intervals,
    )
    if not complete:
        return None, None, None, current_price, False
    current = quarter_start(now)
    deadline_utc = deadline.astimezone(UTC)
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
        ):
            comparison = planned_window
        cheapest = absolute_cheapest_window
        if (
            cheapest is None
            or cheapest.start < horizon_start
            or cheapest.end > deadline_utc
            or cheapest.start >= cheapest.end
        ):
            cheapest = planned_window
        return planned_window, comparison, cheapest, current_price, True
    interval_count = max(1, math.ceil(duration_minutes / 15))
    duration = interval_count * MARKET_INTERVAL
    first = quarter_ceiling(max(current, horizon_start))
    last = deadline_utc - duration
    if last < first:
        return None, None, None, current_price, True
    choices = _window_choices(
        prices,
        first,
        last,
        duration_minutes,
    )
    if not choices:
        return None, None, None, current_price, True
    cheapest = _minimum_average_choice(choices)
    selected = cheapest
    if selected is None:
        return None, None, None, current_price, True

    if comparison_duration_minutes is None:
        comparison = max(
            choices,
            key=lambda choice: (
                choice.average,
                choice.start.timestamp(),
            ),
        )
    else:
        comparison_count = max(
            1,
            math.ceil(comparison_duration_minutes / 15),
        )
        comparison_duration = comparison_count * MARKET_INTERVAL
        comparison_start = deadline_utc - comparison_duration
        rows = [
            prices.get(comparison_start + index * MARKET_INTERVAL)
            for index in range(comparison_count)
        ]
        if (
            comparison_start >= first
            and all(row is not None for row in rows)
        ):
            total = sum(
                (row.price for row in rows if row is not None),
                Decimal(0),
            )
            comparison = PriceWindowChoice(
                start=comparison_start,
                end=deadline_utc,
                average=total / comparison_count,
                interval_count=comparison_count,
            )
        else:
            comparison = max(
                choices,
                key=lambda choice: choice.start,
            )
    return selected, comparison, cheapest, current_price, True


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


def _preferred_readiness_plan(
    *,
    current: datetime,
    deadline: datetime,
    prices: Mapping[datetime, PriceInterval],
    duration_minutes: int,
    policy: DhwPolicy,
    retention_qualified: bool,
    retention_loss_rate_c_per_hour: float | None,
) -> tuple[
    PriceWindowChoice | None,
    PriceWindowChoice | None,
    PriceWindowChoice | None,
    datetime,
]:
    deadline_utc = deadline.astimezone(UTC)
    interval_count = max(1, math.ceil(duration_minutes / 15))
    duration = interval_count * MARKET_INTERVAL
    selected = None
    if (
        retention_qualified
        and retention_loss_rate_c_per_hour is not None
    ):
        horizon = _retention_horizon(
            policy,
            policy.normal_target,
            retention_loss_rate_c_per_hour,
        )
        first = quarter_ceiling(
            max(current, deadline_utc - horizon)
        )
        last = deadline_utc - duration
        choices = (
            choice
            for choice in _window_choices(
                prices,
                first,
                last,
                duration_minutes,
            )
            if _predicted_endpoint_temperature(
                policy.normal_target,
                choice,
                deadline_utc,
                retention_loss_rate_c_per_hour,
            )
            >= policy.floor_target
        )
        selected = _minimum_average_choice(choices)
    if selected is None and current < deadline_utc:
        final_start = deadline_utc - duration
        selected = _window_choice(
            prices,
            final_start,
            interval_count,
        )
    return selected, selected, selected, deadline_utc


def _economic_storage_plan(
    *,
    current: datetime,
    deadline: datetime,
    prices: Mapping[datetime, PriceInterval],
    duration_minutes: int,
    comparison_duration_minutes: int,
    policy: DhwPolicy,
    retention_loss_rate_c_per_hour: float,
) -> tuple[
    PriceWindowChoice | None,
    PriceWindowChoice | None,
    PriceWindowChoice | None,
    datetime | None,
]:
    deadline_utc = deadline.astimezone(UTC)
    interval_count = max(1, math.ceil(duration_minutes / 15))
    duration = interval_count * MARKET_INTERVAL
    comparison_count = max(
        1,
        math.ceil(comparison_duration_minutes / 15),
    )
    horizon = _retention_horizon(
        policy,
        policy.preheat_target,
        retention_loss_rate_c_per_hour,
    )
    first = current
    last = deadline_utc - duration - MARKET_INTERVAL
    if last < first:
        return None, None, None, None
    choices = _window_choices(
        prices,
        first,
        last,
        duration_minutes,
    )
    if not choices:
        return None, None, None, None
    final_comparison = _window_choice(
        prices,
        deadline_utc - comparison_count * MARKET_INTERVAL,
        comparison_count,
    )
    opportunities = []
    for choice in choices:
        comparisons: list[tuple[PriceWindowChoice, datetime]] = []
        if (
            final_comparison is not None
            and choice.start < final_comparison.start
            and choice.end < deadline_utc
        ):
            comparisons.append((final_comparison, deadline_utc))
        immediate = _window_choice(
            prices,
            choice.end,
            comparison_count,
        )
        if immediate is not None:
            comparisons.append((immediate, immediate.end))
        qualified = []
        for comparison, endpoint in comparisons:
            gate, delta, relative = price_gate(
                comparison.average,
                choice,
                policy.price_delta,
                policy.price_percent,
            )
            if (
                gate
                and delta is not None
                and relative is not None
                and choice.start >= endpoint - horizon
                and _predicted_endpoint_temperature(
                    policy.preheat_target,
                    choice,
                    endpoint,
                    retention_loss_rate_c_per_hour,
                )
                >= policy.floor_target
            ):
                qualified.append((
                    delta,
                    relative,
                    endpoint.timestamp(),
                    comparison,
                    endpoint,
                ))
        if qualified:
            _, _, _, comparison, endpoint = max(
                qualified,
                key=lambda item: (item[0], item[1], item[2]),
            )
            opportunities.append((choice, comparison, endpoint))
    if not opportunities:
        return None, None, None, None
    selected, comparison, endpoint = min(
        opportunities,
        key=lambda item: (
            item[0].average,
            -item[0].start.timestamp(),
        ),
    )
    return selected, comparison, selected, endpoint


def plan_dhw(
    *,
    now: datetime,
    mode: str,
    policy: DhwPolicy,
    observation: DhwObservation,
    intervals: Iterable[PriceInterval],
    heating_minutes: int,
    shallow_heating_minutes: int | None = None,
    lease_expires: datetime | None,
    planned_window: PriceWindowChoice | None = None,
    comparison_window: PriceWindowChoice | None = None,
    absolute_cheapest_window: PriceWindowChoice | None = None,
    planned_deadline: datetime | None = None,
    planned_kind: str | None = None,
    planned_endpoint: datetime | None = None,
    recovery_latched: bool = False,
    recovery_reason: str | None = None,
    preheat_completed: bool = False,
    recent_rapid_draw: bool = False,
    retention_loss_rate_c_per_hour: float | None = None,
    retention_sample_count: int = 0,
    retention_cycle_count: int = 0,
) -> DhwDecision:
    if now.utcoffset() is None:
        raise HomeAssistantError("DHW planning time must include a UTC offset.")
    if mode == DHW_MODE_SOFT:
        mode = DHW_MODE_SAFE
    if mode not in DHW_MODES:
        raise HomeAssistantError("Unknown DHW optimizer mode.")
    _ = preheat_completed
    next_deadline = next_readiness(now, policy)
    deadline = next_deadline
    planned_deadline_valid = (
        planned_deadline is not None
        and (
            planned_deadline.astimezone(UTC)
            == next_deadline.astimezone(UTC)
            or (
                planned_deadline.astimezone(UTC)
                <= now.astimezone(UTC)
                < (
                    planned_deadline.astimezone(UTC)
                    + POST_READINESS_HOLD
                )
            )
        )
    )
    if planned_deadline_valid and planned_deadline is not None:
        deadline = planned_deadline
    post_hold_until = post_readiness_hold_until(now, policy)
    post_readiness = (
        deadline.astimezone(UTC)
        <= now.astimezone(UTC)
        < deadline.astimezone(UTC) + POST_READINESS_HOLD
    )
    unanchored_post_readiness = (
        post_hold_until is not None
        and not planned_deadline_valid
    )
    shallow_minutes = (
        shallow_heating_minutes
        if isinstance(shallow_heating_minutes, int)
        and not isinstance(shallow_heating_minutes, bool)
        and shallow_heating_minutes > 0
        else heating_minutes
    )
    shallow_eligible = (
        observation.tank_temperature is not None
        and policy.floor_target
        <= observation.tank_temperature
        < policy.normal_target
        and not recovery_latched
        and not recent_rapid_draw
    )
    readiness_heating_minutes = (
        shallow_minutes if shallow_eligible else heating_minutes
    )
    readiness_heating_profile = "shallow" if shallow_eligible else "deep"
    retention_qualified = (
        retention_loss_rate_c_per_hour is not None
        and math.isfinite(retention_loss_rate_c_per_hour)
        and retention_loss_rate_c_per_hour >= 0
        and retention_sample_count >= RETENTION_REQUIRED_SAMPLES
        and retention_cycle_count >= RETENTION_REQUIRED_CYCLES
    )
    prices, current_price, complete = _complete_price_schedule(
        now,
        deadline,
        intervals,
    )
    current = quarter_start(now)
    selected = None
    comparison = None
    cheapest = None
    plan_kind = None
    endpoint = None
    planned_economic_expired = (
        planned_kind == PLAN_KIND_ECONOMIC
        and planned_endpoint is not None
        and planned_endpoint.astimezone(UTC)
        < deadline.astimezone(UTC)
        and now.astimezone(UTC)
        >= planned_endpoint.astimezone(UTC)
    )
    if (
        planned_deadline_valid
        and planned_window is not None
        and not planned_economic_expired
        and complete
        and _window_matches_prices(planned_window, prices)
        and (
            comparison_window is None
            or _window_matches_prices(comparison_window, prices)
        )
        and (
            absolute_cheapest_window is None
            or _window_matches_prices(
                absolute_cheapest_window,
                prices,
            )
        )
    ):
        selected = planned_window
        comparison = comparison_window or planned_window
        cheapest = absolute_cheapest_window or planned_window
        plan_kind = (
            planned_kind
            if planned_kind in (PLAN_KIND_ECONOMIC, PLAN_KIND_PREFERRED)
            else PLAN_KIND_PREFERRED
        )
        endpoint = planned_endpoint or deadline
    elif (
        not unanchored_post_readiness
        and complete
        and current < deadline.astimezone(UTC)
    ):
        preferred = _preferred_readiness_plan(
            current=current,
            deadline=deadline,
            prices=prices,
            duration_minutes=readiness_heating_minutes,
            policy=policy,
            retention_qualified=retention_qualified,
            retention_loss_rate_c_per_hour=(
                retention_loss_rate_c_per_hour
                if retention_qualified
                else None
            ),
        )
        economic = (None, None, None, None)
        if (
            mode in (DHW_MODE_SHADOW, DHW_MODE_SAFE)
            and retention_qualified
            and not recent_rapid_draw
            and retention_loss_rate_c_per_hour is not None
            and policy.preheat_target > policy.normal_target
        ):
            economic = _economic_storage_plan(
                current=current,
                deadline=deadline,
                prices=prices,
                duration_minutes=heating_minutes,
                comparison_duration_minutes=readiness_heating_minutes,
                policy=policy,
                retention_loss_rate_c_per_hour=(
                    retention_loss_rate_c_per_hour
                ),
            )
        if economic[0] is not None:
            selected, comparison, cheapest, endpoint = economic
            plan_kind = PLAN_KIND_ECONOMIC
        else:
            selected, comparison, cheapest, endpoint = preferred
            if selected is not None:
                plan_kind = PLAN_KIND_PREFERRED
    gate, delta, relative = price_gate(
        current_price,
        selected,
        policy.price_delta,
        policy.price_percent,
    )
    economic_gate, economic_delta, economic_relative = price_gate(
        comparison.average if comparison is not None else None,
        selected,
        policy.price_delta,
        policy.price_percent,
    )
    economic_feasible = (
        plan_kind == PLAN_KIND_ECONOMIC
        and economic_gate
        and complete
        and retention_qualified
        and not recent_rapid_draw
        and selected is not None
        and endpoint is not None
        and retention_loss_rate_c_per_hour is not None
        and selected.end < deadline.astimezone(UTC)
        and selected.start
        >= endpoint.astimezone(UTC)
        - _retention_horizon(
            policy,
            policy.preheat_target,
            retention_loss_rate_c_per_hour,
        )
        and _predicted_endpoint_temperature(
            policy.preheat_target,
            selected,
            endpoint,
            retention_loss_rate_c_per_hour,
        )
        >= policy.floor_target
    )
    if not economic_feasible:
        economic_gate = False
    adaptive_preheat_target = (
        policy.preheat_target if economic_feasible else None
    )
    planned_target = (
        policy.preheat_target
        if plan_kind == PLAN_KIND_ECONOMIC
        and selected is not None
        else policy.normal_target
        if selected is not None
        else None
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
            preheat_price_delta=economic_delta,
            preheat_price_delta_percent=economic_relative,
            preheat_price_gate=economic_gate,
            absolute_cheapest_window=cheapest,
            readiness_heating_minutes=readiness_heating_minutes,
            readiness_heating_profile=readiness_heating_profile,
            retention_qualified=retention_qualified,
            retention_sample_count=retention_sample_count,
            retention_cycle_count=retention_cycle_count,
            retention_loss_rate_c_per_hour=retention_loss_rate_c_per_hour,
            adaptive_preheat_target_c=adaptive_preheat_target,
            plan_kind=plan_kind,
            planned_target_c=planned_target,
            protected_endpoint=endpoint,
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
    if observation.tank_temperature < policy.floor_target:
        return decision(
            "recovery",
            "Tank temperature fell below the 45°C comfort floor; request "
            "50°C immediately and keep measured recovery latched.",
            policy.normal_target,
            urgent=True,
        )
    now_utc = now.astimezone(UTC)
    inside_selected = (
        selected is not None
        and selected.start <= now_utc < selected.end
    )
    if (
        economic_feasible
        and inside_selected
    ):
        return decision(
            "economic_storage",
            "The current quarter is inside the absolute minimum-average "
            "feasible storage block, both material price gates pass, and "
            "qualified retention protects the 45°C floor through the "
            "relevant endpoint.",
            policy.preheat_target,
            urgent=True,
        )
    if observation.target_temperature > policy.normal_target:
        return decision(
            "readiness_preferred",
            "The economic-storage block is not active; restore the "
            "optimizer-owned elevated target to the preferred 50°C target.",
            policy.normal_target,
            urgent=True,
        )
    if not complete or current_price is None:
        return decision(
            (
                "readiness_charge"
                if observation.tank_temperature < policy.normal_target
                else "readiness_preferred"
            ),
            "Published all-in prices are incomplete for the protected "
            "readiness period; fail safe to the preferred 50°C target.",
            policy.normal_target,
            urgent=True,
        )
    if unanchored_post_readiness:
        return decision(
            (
                "readiness_charge"
                if observation.tank_temperature < policy.normal_target
                else "readiness_preferred"
            ),
            "No complete anchored plan is available for the just-finished "
            "readiness period; fail safe to the preferred 50°C target.",
            policy.normal_target,
            urgent=True,
        )
    if post_readiness:
        if observation.tank_temperature < policy.normal_target:
            if gate:
                usable_target = observation.target_temperature
                if not (
                    policy.floor_target
                    <= observation.target_temperature
                    <= policy.normal_target
                ):
                    usable_target = (
                        policy.normal_target
                        if observation.target_temperature
                        > policy.normal_target
                        else policy.floor_target
                    )
                return decision(
                    "readiness_usable",
                    "The tank remains at or above the usable 45°C floor, "
                    "while the current price is materially expensive relative "
                    "to the anchored feasible block. Accept usable water below "
                    "the preferred 50°C target without toggling native "
                    "hysteresis.",
                    usable_target,
                )
            return decision(
                "readiness_charge",
                "The tank is usable but below the preferred 50°C readiness "
                "target, and the current price is not materially expensive "
                "relative to the anchored feasible block.",
                policy.normal_target,
                urgent=True,
            )
        return decision(
            "readiness_preferred",
            "Measured readiness is at or above the preferred 50°C target.",
            policy.normal_target,
            urgent=True,
        )
    if mode == DHW_MODE_READINESS:
        if inside_selected and observation.tank_temperature < policy.normal_target:
            return decision(
                "readiness_charge",
                "The current quarter is inside the anchored preferred "
                "readiness block; request 50°C.",
                policy.normal_target,
                urgent=True,
            )
        return decision(
            "readiness_preferred",
            "The readiness-only pilot keeps the preferred 50°C target; "
            "economic storage and price coasting remain disabled.",
            policy.normal_target,
            urgent=True,
        )
    if selected is None:
        return decision(
            (
                "readiness_charge"
                if observation.tank_temperature < policy.normal_target
                else "readiness_preferred"
            ),
            "No complete feasible charging block remains before the next "
            "readiness deadline; fail safe to the preferred 50°C target.",
            policy.normal_target,
            urgent=True,
        )
    if inside_selected:
        if observation.tank_temperature < policy.normal_target:
            return decision(
                "readiness_charge",
                "The current quarter is inside the anchored absolute "
                "minimum-average feasible readiness block; request the "
                "preferred 50°C target.",
                policy.normal_target,
                urgent=True,
            )
        return decision(
            "readiness_preferred",
            "The current quarter is inside the anchored readiness block and "
            "the tank already satisfies the preferred 50°C target.",
            policy.normal_target,
            urgent=True,
        )
    if selected.end <= now_utc:
        if observation.tank_temperature < policy.normal_target:
            if gate:
                usable_target = observation.target_temperature
                if not (
                    policy.floor_target
                    <= observation.target_temperature
                    <= policy.normal_target
                ):
                    usable_target = policy.floor_target
                return decision(
                    "readiness_usable",
                    "The anchored block has ended and the tank remains usable "
                    "at 45–49°C. The current price is materially expensive, so "
                    "do not chase the preferred target.",
                    usable_target,
                )
            return decision(
                "readiness_charge",
                "The anchored block ended below the preferred target, and "
                "current energy is not materially expensive; request 50°C.",
                policy.normal_target,
                urgent=True,
            )
        return decision(
            "readiness_preferred",
            "The anchored block has ended and the tank remains at or above "
            "the preferred 50°C target.",
            policy.normal_target,
        )
    if not gate:
        if (
            observation.target_temperature < policy.normal_target
            and observation.tank_temperature < policy.normal_target
        ):
            return decision(
                "readiness_charge",
                "The tank is below the preferred target and the current price "
                "is not materially expensive relative to the anchored block.",
                policy.normal_target,
            )
        return decision(
            "readiness_preferred",
            "The current price does not exceed both material gates; preserve "
            "the preferred 50°C target.",
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
        or decision.state == "economic_storage"
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
            "planned_kind": None,
            "planned_target": None,
            "planned_endpoint": None,
            "planned_window_start": None,
            "planned_window_end": None,
            "planned_window_average": None,
            "planned_window_intervals": None,
            "planned_comparison_start": None,
            "planned_comparison_end": None,
            "planned_comparison_average": None,
            "planned_comparison_intervals": None,
            "planned_cheapest_start": None,
            "planned_cheapest_end": None,
            "planned_cheapest_average": None,
            "planned_cheapest_intervals": None,
            "planned_window_completed_at": None,
            "shadow_days": [],
            "readiness_pilot_days": [],
            "heating_durations": [],
            "readiness_topup_durations": [],
            "retention_samples": [],
            "retention_started_at": None,
            "retention_start_temperature": None,
            "retention_target": None,
            "retention_deadline": None,
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
            "planned_kind": None,
            "planned_target": None,
            "planned_endpoint": None,
            "planned_window_start": None,
            "planned_window_end": None,
            "planned_window_average": None,
            "planned_window_intervals": None,
            "planned_comparison_start": None,
            "planned_comparison_end": None,
            "planned_comparison_average": None,
            "planned_comparison_intervals": None,
            "planned_cheapest_start": None,
            "planned_cheapest_end": None,
            "planned_cheapest_average": None,
            "planned_cheapest_intervals": None,
            "planned_window_completed_at": None,
            "shadow_days": [],
            "readiness_pilot_days": [],
            "heating_durations": [],
            "readiness_topup_durations": [],
            "retention_samples": [],
            "retention_started_at": None,
            "retention_start_temperature": None,
            "retention_target": None,
            "retention_deadline": None,
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
            "qualified_opportunity_count": 0,
            "last_qualified_opportunity_plan_id": None,
            "last_qualified_opportunity_at": None,
        })

    def _migrate_revision_eight(self) -> None:
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
            "readiness_window_peaks": dict(
                self.state["readiness_window_peaks"]
            ),
            "readiness_rapid_draws": dict(
                self.state["readiness_rapid_draws"]
            ),
            "readiness_deadline_samples": dict(
                self.state["readiness_deadline_samples"]
            ),
        }
        self._reset_policy_evidence()
        self.state.update(preserved)
        self.state["lease_expires"] = None
        if self.mode in (DHW_MODE_READINESS, DHW_MODE_SAFE):
            self.mode = DHW_MODE_SHADOW

    def _migrate_revision_ten(self) -> None:
        self.state.update({
            "policy_revision": POLICY_REVISION,
            "lease_expires": None,
            "last_policy_target": None,
            "last_policy_transition_at": None,
            "shadow_days": [],
            "readiness_pilot_days": [],
            "retention_started_at": None,
            "retention_start_temperature": None,
            "retention_target": None,
            "retention_deadline": None,
            "cycle_started_at": None,
            "cycle_start_temperature": None,
            "topup_started_at": None,
            "topup_start_temperature": None,
            "last_temperature": None,
            "last_observed_at": None,
            "decision_history": [],
            "deadline_checks": {},
            "readiness_window_peaks": {},
            "readiness_rapid_draws": {},
            "readiness_deadline_samples": {},
            "qualified_opportunity_count": 0,
            "last_qualified_opportunity_plan_id": None,
            "last_qualified_opportunity_at": None,
        })
        self._clear_price_plan()
        if self.mode in (DHW_MODE_READINESS, DHW_MODE_SAFE):
            self.mode = DHW_MODE_SHADOW

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
            if stored_revision in (5, 6, 7):
                self._migrate_revision_eight()
            elif stored_revision in (8, 9):
                self._migrate_revision_ten()
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
        await self.async_evaluate(suppress_write=True)

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

    @property
    def shallow_heating_minutes(self) -> int:
        values = sorted(
            float(value)
            for value in self.state["readiness_topup_durations"]
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
        if len(values) < 2:
            return SHALLOW_HEATING_FALLBACK_MINUTES
        index = max(0, math.ceil(len(values) * 0.9) - 1)
        learned = math.ceil((values[index] + 15) / 15) * 15
        return max(
            SHALLOW_HEATING_MIN_MINUTES,
            min(SHALLOW_HEATING_MAX_MINUTES, learned),
        )

    @property
    def retention_model(self) -> tuple[bool, float | None, int, int]:
        samples = []
        cycles = set()
        for record in self.state["retention_samples"]:
            if not isinstance(record, dict):
                continue
            rate = _raw_number(record.get("loss_rate_c_per_hour"))
            deadline = record.get("readiness_deadline")
            if (
                rate is None
                or rate < 0
                or rate > RETENTION_MAX_LOSS_RATE_C_PER_HOUR
                or not isinstance(deadline, str)
            ):
                continue
            samples.append(rate)
            cycles.add(deadline)
        if not samples:
            return False, None, 0, 0
        samples.sort()
        index = max(0, math.ceil(len(samples) * 0.9) - 1)
        qualified = (
            len(samples) >= RETENTION_REQUIRED_SAMPLES
            and len(cycles) >= RETENTION_REQUIRED_CYCLES
        )
        return qualified, samples[index], len(samples), len(cycles)

    def _recent_rapid_draw(self, deadline: datetime) -> bool:
        return (
            _instant(
                self.state["readiness_rapid_draws"].get(
                    deadline.isoformat()
                )
            )
            is not None
        )

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
            "planned_kind",
            "planned_target",
            "planned_endpoint",
            "planned_window_start",
            "planned_window_end",
            "planned_window_average",
            "planned_window_intervals",
            "planned_comparison_start",
            "planned_comparison_end",
            "planned_comparison_average",
            "planned_comparison_intervals",
            "planned_cheapest_start",
            "planned_cheapest_end",
            "planned_cheapest_average",
            "planned_cheapest_intervals",
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
        self,
        deadline: datetime,
        now: datetime | None = None,
    ) -> tuple[
        PriceWindowChoice | None,
        PriceWindowChoice | None,
        PriceWindowChoice | None,
    ]:
        stored_deadline = _instant(self.state.get("planned_deadline"))
        deadline_matches = (
            stored_deadline is not None
            and stored_deadline.astimezone(UTC)
            == deadline.astimezone(UTC)
        )
        retained_post_readiness = (
            stored_deadline is not None
            and now is not None
            and stored_deadline.astimezone(UTC)
            <= now.astimezone(UTC)
            < stored_deadline.astimezone(UTC) + POST_READINESS_HOLD
        )
        if not deadline_matches and not retained_post_readiness:
            self._clear_price_plan()
            return None, None, None
        selected = self._stored_window("planned_window")
        if (
            selected is None
            or stored_deadline is None
            or selected.end > stored_deadline.astimezone(UTC)
        ):
            self._clear_price_plan()
            return None, None, None
        comparison = self._stored_window("planned_comparison") or selected
        cheapest = self._stored_window("planned_cheapest") or selected
        kind = self.state.get("planned_kind")
        endpoint = _instant(self.state.get("planned_endpoint"))
        target = _raw_number(self.state.get("planned_target"))
        if (
            kind not in (PLAN_KIND_ECONOMIC, PLAN_KIND_PREFERRED)
            or endpoint is None
            or endpoint.astimezone(UTC) < selected.end
            or (
                kind == PLAN_KIND_ECONOMIC
                and endpoint.astimezone(UTC) < comparison.end
            )
            or target is None
            or (
                kind == PLAN_KIND_ECONOMIC
                and target != self.policy.preheat_target
            )
            or (
                kind == PLAN_KIND_PREFERRED
                and target != self.policy.normal_target
            )
        ):
            self._clear_price_plan()
            return None, None, None
        if (
            now is not None
            and kind == PLAN_KIND_ECONOMIC
            and endpoint.astimezone(UTC)
            < stored_deadline.astimezone(UTC)
            and now.astimezone(UTC) >= endpoint.astimezone(UTC)
        ):
            self._clear_price_plan()
            return None, None, None
        return selected, comparison, cheapest

    def _store_price_plan(
        self,
        deadline: datetime,
        selected: PriceWindowChoice,
        comparison: PriceWindowChoice | None,
        cheapest: PriceWindowChoice | None,
        *,
        kind: str = PLAN_KIND_PREFERRED,
        target: float | None = None,
        endpoint: datetime | None = None,
    ) -> None:
        self.state["planned_deadline"] = deadline.isoformat()
        self.state["planned_kind"] = (
            kind
            if kind in (PLAN_KIND_ECONOMIC, PLAN_KIND_PREFERRED)
            else PLAN_KIND_PREFERRED
        )
        self.state["planned_target"] = (
            target if target is not None else self.policy.normal_target
        )
        self.state["planned_endpoint"] = (
            endpoint or deadline
        ).isoformat()
        self.state["planned_window_completed_at"] = None
        for prefix, window in (
            ("planned_window", selected),
            ("planned_comparison", comparison or selected),
            ("planned_cheapest", cheapest or selected),
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
        required_target: float | None,
    ) -> None:
        if (
            selected is None
            or required_target is None
            or self.state.get("planned_window_completed_at") is not None
            or now.astimezone(UTC) < selected.start.astimezone(UTC)
            or observation.tank_temperature is None
            or observation.tank_temperature < required_target
        ):
            return
        self.state["planned_window_completed_at"] = now.astimezone(UTC).isoformat()

    def _reset_retention_segment(self) -> None:
        self.state["retention_started_at"] = None
        self.state["retention_start_temperature"] = None
        self.state["retention_target"] = None
        self.state["retention_deadline"] = None

    def _start_retention_segment(
        self,
        now: datetime,
        temperature: float,
        target: float,
    ) -> None:
        self.state["retention_started_at"] = now.isoformat()
        self.state["retention_start_temperature"] = temperature
        self.state["retention_target"] = target
        self.state["retention_deadline"] = next_readiness(
            now,
            self.policy,
        ).isoformat()

    def _update_retention_learning(
        self,
        now: datetime,
        observation: DhwObservation,
        last_temperature: float | None,
        last_observed: datetime | None,
    ) -> None:
        temperature = observation.tank_temperature
        target = observation.target_temperature
        if (
            observation.override_reason is not None
            or temperature is None
            or target is None
            or observation.enabled is not True
            or temperature < self.policy.normal_target
            or target > self.policy.normal_target
        ):
            self._reset_retention_segment()
            return
        if (
            last_temperature is not None
            and last_observed is not None
            and timedelta(0)
            <= now - last_observed.astimezone(UTC)
            <= timedelta(minutes=15)
            and last_temperature - temperature >= 3
        ):
            self._reset_retention_segment()
            return

        started = _instant(self.state.get("retention_started_at"))
        start_temperature = _raw_number(
            self.state.get("retention_start_temperature")
        )
        segment_target = _raw_number(self.state.get("retention_target"))
        deadline = self.state.get("retention_deadline")
        if (
            started is None
            or start_temperature is None
            or segment_target != target
            or not isinstance(deadline, str)
        ):
            if temperature > self.policy.normal_target:
                self._start_retention_segment(now, temperature, target)
            else:
                self._reset_retention_segment()
            return
        if temperature > start_temperature:
            self._start_retention_segment(now, temperature, target)
            return

        elapsed = now - started.astimezone(UTC)
        if elapsed < RETENTION_MINIMUM_DURATION:
            return
        hours = elapsed.total_seconds() / 3600
        loss = start_temperature - temperature
        rate = loss / hours if hours > 0 else math.inf
        if (
            not math.isfinite(rate)
            or rate < 0
            or rate > RETENTION_MAX_LOSS_RATE_C_PER_HOUR
        ):
            if temperature > self.policy.normal_target:
                self._start_retention_segment(now, temperature, target)
            else:
                self._reset_retention_segment()
            return
        samples = list(self.state["retention_samples"])
        samples.append({
            "observed_at": now.isoformat(),
            "duration_minutes": round(elapsed.total_seconds() / 60, 1),
            "start_temperature_c": start_temperature,
            "end_temperature_c": temperature,
            "loss_rate_c_per_hour": round(rate, 3),
            "readiness_deadline": deadline,
        })
        self.state["retention_samples"] = samples[-RETENTION_SAMPLE_LIMIT:]
        if temperature > self.policy.normal_target:
            self._start_retention_segment(now, temperature, target)
        else:
            self._reset_retention_segment()

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
                    decision_state = None
                    if (
                        self.decision is not None
                        and self.decision.deadline is not None
                        and self.decision.deadline.astimezone(UTC)
                        == ready_at.astimezone(UTC)
                    ):
                        decision_state = self.decision.state
                    samples[key] = {
                        "temperature": temperature,
                        "target": target,
                        "override_reason": observation.override_reason,
                        "decision_state": decision_state,
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
                sample_decision_state = (
                    sample.get("decision_state")
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
                elif (
                    sample_temperature >= self.policy.floor_target
                    and sample_decision_state == "readiness_usable"
                ):
                    outcome = READINESS_ACCEPTED
                else:
                    outcome = READINESS_FAILURE
                checks[key] = outcome
                checks = dict(list(checks.items())[-30:])
                if (
                    temperature is not None
                    and temperature < self.policy.floor_target
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
        self._update_retention_learning(
            now,
            observation,
            last_temperature,
            last_observed,
        )
        if (
            observation.override_reason is not None
            or temperature is None
            or target is None
        ):
            return
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

    def _update_deadline_sample_decision(
        self,
        now: datetime,
        decision: DhwDecision,
    ) -> None:
        ready_at = previous_readiness(now, self.policy)
        if not (
            ready_at <= now.astimezone(TALLINN)
            < ready_at + timedelta(minutes=15)
        ):
            return
        key = ready_at.isoformat()
        sample = self.state["readiness_deadline_samples"].get(key)
        if not isinstance(sample, dict):
            return
        sample["decision_state"] = decision.state

    def _record_decision(self, now: datetime, decision: DhwDecision) -> None:
        previous = self.decision
        if (
            previous is not None
            and previous.mode == decision.mode
            and previous.state == decision.state
            and previous.desired_target == decision.desired_target
            and previous.reason == decision.reason
            and previous.selected_window == decision.selected_window
            and previous.comparison_window == decision.comparison_window
            and previous.preheat_price_gate == decision.preheat_price_gate
            and previous.plan_kind == decision.plan_kind
            and previous.protected_endpoint == decision.protected_endpoint
        ):
            return
        record = {
            "observed_at": now.isoformat(),
            "mode": decision.mode,
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
            "price_plan_kind": decision.plan_kind,
            "planned_target_c": decision.planned_target_c,
            "protected_endpoint": (
                decision.protected_endpoint.isoformat()
                if decision.protected_endpoint is not None
                else None
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
            or not decision.retention_qualified
            or decision.adaptive_preheat_target_c is None
            or decision.adaptive_preheat_target_c
            <= self.policy.normal_target
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

    async def async_evaluate(
        self,
        now: datetime | None = None,
        *,
        suppress_write: bool = False,
    ) -> None:
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
                elif (
                    observation.tank_temperature is not None
                    and observation.tank_temperature
                    < self.policy.floor_target
                ):
                    self.state["recovery_latched"] = True
                    if self.state.get("recovery_reason") is None:
                        self.state["recovery_reason"] = (
                            "Tank temperature fell below the 45°C comfort "
                            "floor; hold 50°C until measured recovery "
                            "completes."
                        )
                lease = _instant(self.state.get("lease_expires"))
                intervals = published_intervals(self.coordinator)
                deadline = next_readiness(observed_at, self.policy)
                (
                    planned_window,
                    comparison_window,
                    absolute_cheapest_window,
                ) = self._stored_price_plan(deadline, observed_at)
                stored_deadline = (
                    _instant(self.state.get("planned_deadline"))
                    if planned_window is not None
                    else None
                )
                stored_endpoint = (
                    _instant(self.state.get("planned_endpoint"))
                    if planned_window is not None
                    else None
                )
                stored_kind = (
                    self.state.get("planned_kind")
                    if planned_window is not None
                    else None
                )
                (
                    retention_qualified,
                    retention_rate,
                    retention_sample_count,
                    retention_cycle_count,
                ) = self.retention_model
                decision = plan_dhw(
                    now=observed_at,
                    mode=self.mode,
                    policy=self.policy,
                    observation=observation,
                    intervals=intervals,
                    heating_minutes=self.heating_minutes,
                    shallow_heating_minutes=self.shallow_heating_minutes,
                    lease_expires=lease.astimezone(UTC) if lease else None,
                    planned_window=planned_window,
                    comparison_window=comparison_window,
                    absolute_cheapest_window=absolute_cheapest_window,
                    planned_deadline=stored_deadline,
                    planned_kind=stored_kind,
                    planned_endpoint=stored_endpoint,
                    recovery_latched=self.state["recovery_latched"] is True,
                    recovery_reason=self.state.get("recovery_reason"),
                    preheat_completed=(
                        self.state.get("planned_window_completed_at") is not None
                    ),
                    recent_rapid_draw=self._recent_rapid_draw(
                        stored_deadline or deadline
                    ),
                    retention_loss_rate_c_per_hour=(
                        retention_rate if retention_qualified else None
                    ),
                    retention_sample_count=retention_sample_count,
                    retention_cycle_count=retention_cycle_count,
                )
                if (
                    planned_window is None
                    and decision.data_complete
                    and decision.selected_window is not None
                    and decision.deadline is not None
                    and decision.deadline.astimezone(UTC) > observed_at
                    and decision.plan_kind is not None
                ):
                    self._store_price_plan(
                        decision.deadline,
                        decision.selected_window,
                        decision.comparison_window,
                        decision.absolute_cheapest_window,
                        kind=decision.plan_kind,
                        target=decision.planned_target_c,
                        endpoint=decision.protected_endpoint,
                    )
                self._update_preheat_completion(
                    observed_at,
                    observation,
                    decision.selected_window,
                    decision.planned_target_c,
                )
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
                    decision.state == "recovery"
                    and observation.tank_temperature is not None
                    and observation.tank_temperature < self.policy.normal_target
                ):
                    self.state["recovery_latched"] = True
                    self.state["recovery_reason"] = decision.reason
                self._update_deadline_sample_decision(
                    observed_at,
                    decision,
                )
                if (
                    not suppress_write
                    and decision.desired_target is not None
                    and decision.desired_target != last_target
                ):
                    self.state["last_policy_target"] = decision.desired_target
                    self.state["last_policy_transition_at"] = observed_at.isoformat()
                self._record_optimization_opportunity(observed_at, decision)
                self._record_decision(observed_at, decision)
                self.decision = decision
                if not suppress_write:
                    await self._apply_decision(
                        observed_at,
                        observation,
                        decision,
                    )
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
            and decision.state == "readiness_charge"
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
        (
            retention_qualified,
            retention_rate,
            retention_sample_count,
            retention_cycle_count,
        ) = self.retention_model
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
            "deep_heating_duration_minutes": self.heating_minutes,
            "shallow_heating_duration_minutes": (
                self.shallow_heating_minutes
            ),
            "retention_model_qualified": retention_qualified,
            "retention_sample_count": retention_sample_count,
            "retention_cycle_count": retention_cycle_count,
            "retention_loss_rate_c_per_hour": (
                round(retention_rate, 3)
                if retention_rate is not None
                else None
            ),
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
            "readiness_accepted_below_preferred": sum(
                value == READINESS_ACCEPTED
                for value in deadline_checks.values()
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
            "economic_storage_horizon_cap_hours": (
                MAX_STORAGE_HORIZON.total_seconds() / 3600
            ),
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
