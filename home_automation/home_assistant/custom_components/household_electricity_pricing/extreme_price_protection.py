"""Independent catastrophic-price protection for heating and climate loads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import math
from typing import Callable, Iterable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
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
)
from .const import (
    CONF_EXTREME_PRICE_CLIMATE_ENTITIES,
    CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES,
    DOMAIN,
    EVENT_EXTREME_PRICE_DECISION,
    EXTREME_PRICE_CRITICAL_EUR_KWH,
    EXTREME_PRICE_FLOOR_RESTORE_C,
    EXTREME_PRICE_MAX_LEASE_MINUTES,
    EXTREME_PRICE_MODE_ARMED,
    EXTREME_PRICE_MODE_OFF,
    EXTREME_PRICE_MODE_SHADOW,
    EXTREME_PRICE_MODES,
    EXTREME_PRICE_THRESHOLD_EUR_KWH,
    EXTREME_PRICE_WARNING_EUR_KWH,
    EXTREME_PRICE_WARNING_MINUTES,
)
from .controls import EnergyDevices, FLAGS
from .coordinator import HouseholdPriceCoordinator
from .dhw_optimizer import (
    DhwOptimizer,
    PriceInterval,
    next_readiness,
    published_intervals,
)
from .floor_heating_shadow import read_room_temperature_source


LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
STORE_KEY_SUFFIX = "extreme_price_protection"
POLICY_REVISION = 1
DECISION_HISTORY_LIMIT = 64
NOTIFICATION_ID = DOMAIN + "_extreme_price_protection"
MOBILE_NOTIFY_SERVICE = "mobile_app_bananastan"
LOAD_HOT_WATER = "daikin_hot_water"
LOAD_FLOOR_HEATING = "daikin_floor_heating"


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


@dataclass(frozen=True)
class PriceBlock:
    threshold: Decimal
    start: datetime
    end: datetime
    maximum: Decimal
    interval_count: int

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def identity(self) -> str:
        return "|".join(
            (
                format(self.threshold, "f"),
                self.start.isoformat(),
                self.end.isoformat(),
            )
        )

    def attributes(self, prefix: str) -> dict:
        return {
            prefix + "_id": self.identity,
            prefix + "_start": self.start.isoformat(),
            prefix + "_end": self.end.isoformat(),
            prefix + "_maximum_eur_kwh": format(self.maximum, "f"),
            prefix + "_duration_minutes": int(
                self.duration.total_seconds() // 60
            ),
        }


@dataclass(frozen=True)
class ProtectionAction:
    load_id: str
    load_type: str
    operation: str
    current_state: str | None
    proposed_state: str | None
    reason: str

    def attributes(self) -> dict:
        return {
            "load_id": self.load_id,
            "load_type": self.load_type,
            "operation": self.operation,
            "current_state": self.current_state,
            "proposed_state": self.proposed_state,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProtectionDecision:
    mode: str
    state: str
    reason: str
    current_price: Decimal | None
    active_block: PriceBlock | None
    warning_block: PriceBlock | None
    critical: bool
    data_complete: bool
    actions: tuple[ProtectionAction, ...]
    partial: bool
    next_hard_block: PriceBlock | None = None

    def attributes(self) -> dict:
        result = {
            "mode": self.mode,
            "state": self.state,
            "reason": self.reason,
            "current_price_eur_kwh": (
                format(self.current_price, "f")
                if self.current_price is not None
                else None
            ),
            "critical_price": self.critical,
            "data_complete": self.data_complete,
            "partial_shutdown": self.partial,
            "proposed_actions": [
                action.attributes() for action in self.actions
            ],
        }
        if self.active_block is None:
            result.update(
                {
                    "active_block_id": None,
                    "active_block_start": None,
                    "active_block_end": None,
                    "active_block_maximum_eur_kwh": None,
                    "active_block_duration_minutes": None,
                }
            )
        else:
            result.update(self.active_block.attributes("active_block"))
        if self.next_hard_block is None:
            result.update(
                {
                    "next_hard_block_id": None,
                    "next_hard_block_start": None,
                    "next_hard_block_end": None,
                    "next_hard_block_maximum_eur_kwh": None,
                    "next_hard_block_duration_minutes": None,
                }
            )
        else:
            result.update(
                self.next_hard_block.attributes("next_hard_block")
            )
        if self.warning_block is None:
            result.update(
                {
                    "warning_block_id": None,
                    "warning_block_start": None,
                    "warning_block_end": None,
                    "warning_block_maximum_eur_kwh": None,
                    "warning_block_duration_minutes": None,
                }
            )
        else:
            result.update(self.warning_block.attributes("warning_block"))
        return result


def contiguous_price_blocks(
    intervals: Iterable[PriceInterval],
    threshold: Decimal,
) -> tuple[PriceBlock, ...]:
    selected = sorted(
        (
            row
            for row in intervals
            if row.price >= threshold
            and row.end - row.start == MARKET_INTERVAL
        ),
        key=lambda row: row.start,
    )
    blocks = []
    start = end = None
    maximum = None
    count = 0
    for row in selected:
        if start is None or row.start != end:
            if start is not None and end is not None and maximum is not None:
                blocks.append(
                    PriceBlock(threshold, start, end, maximum, count)
                )
            start = row.start
            end = row.end
            maximum = row.price
            count = 1
            continue
        end = row.end
        maximum = max(maximum, row.price)
        count += 1
    if start is not None and end is not None and maximum is not None:
        blocks.append(PriceBlock(threshold, start, end, maximum, count))
    return tuple(blocks)


def _current_price(
    now: datetime,
    intervals: Iterable[PriceInterval],
) -> Decimal | None:
    instant = now.astimezone(UTC)
    for row in intervals:
        if row.start <= instant < row.end:
            return row.price
    return None


def _active_block(
    now: datetime,
    blocks: Iterable[PriceBlock],
) -> PriceBlock | None:
    instant = now.astimezone(UTC)
    return next(
        (
            block
            for block in blocks
            if block.start <= instant < block.end
        ),
        None,
    )


def _next_warning_block(
    now: datetime,
    blocks: Iterable[PriceBlock],
    minimum_duration: timedelta,
) -> PriceBlock | None:
    instant = now.astimezone(UTC)
    return next(
        (
            block
            for block in blocks
            if block.end > instant and block.duration >= minimum_duration
        ),
        None,
    )


def plan_extreme_price_protection(
    *,
    now: datetime,
    mode: str,
    intervals: Iterable[PriceInterval],
    water_enabled: bool | None,
    tank_temperature: float | None,
    water_guard: str | None,
    readiness_active: bool,
    heating_enabled: bool | None,
    room_temperature: float | None,
    heating_guard: str | None,
    climates: Iterable[dict],
    owned_loads: Iterable[str] = (),
    suppressed_load_blocks: Iterable[str] = (),
) -> ProtectionDecision:
    prices = tuple(intervals)
    hard_threshold = Decimal(str(EXTREME_PRICE_THRESHOLD_EUR_KWH))
    warning_threshold = Decimal(str(EXTREME_PRICE_WARNING_EUR_KWH))
    hard_blocks = contiguous_price_blocks(prices, hard_threshold)
    warning_blocks = contiguous_price_blocks(prices, warning_threshold)
    next_hard = next(
        (block for block in hard_blocks if block.end > now),
        None,
    )
    active = _active_block(now, hard_blocks)
    warning = _next_warning_block(
        now,
        warning_blocks,
        timedelta(minutes=EXTREME_PRICE_WARNING_MINUTES),
    )
    current = _current_price(now, prices)
    owned = set(owned_loads)
    suppressed = set(suppressed_load_blocks)
    critical = (
        current is not None
        and current >= Decimal(str(EXTREME_PRICE_CRITICAL_EUR_KWH))
    )

    if mode == EXTREME_PRICE_MODE_OFF:
        return ProtectionDecision(
            mode,
            "off",
            "Extreme price protection is disabled.",
            current,
            active,
            warning,
            critical,
            current is not None,
            (),
            False,
            next_hard,
        )

    actions = []
    if active is None:
        for load_id in sorted(owned):
            actions.append(
                ProtectionAction(
                    load_id,
                    "climate" if load_id.startswith("climate.") else "daikin",
                    "restore",
                    "breaker_owned_standby",
                    "prior_state",
                    (
                        "The published extreme-price block ended, or the "
                        "current published price is unavailable."
                    ),
                )
            )
        if current is None:
            state = "unavailable"
            reason = (
                "The current canonical published all-in price is unavailable; "
                "no new shutdown is proposed."
            )
        elif warning is not None:
            state = "warning"
            reason = (
                "A published all-in price block at or above "
                f"€{EXTREME_PRICE_WARNING_EUR_KWH:g}/kWh lasts at least "
                f"{EXTREME_PRICE_WARNING_MINUTES // 60} hours."
            )
        elif next_hard is not None:
            state = "scheduled"
            reason = (
                "A published catastrophic-price block is scheduled; "
                "no shutdown is proposed before it begins."
            )
        else:
            state = "clear"
            reason = (
                "No published catastrophic-price block is active or scheduled."
            )
        return ProtectionDecision(
            mode,
            state,
            reason,
            current,
            None,
            warning,
            critical,
            current is not None,
            tuple(actions),
            current is None and bool(owned),
            next_hard,
        )

    block_id = active.identity

    def suppressed_for(load_id: str) -> bool:
        return block_id + "|" + load_id in suppressed

    if LOAD_HOT_WATER in owned:
        if water_enabled is None:
            operation = "unavailable"
            reason = "Fresh breaker-owned DHW circuit state is unavailable."
        elif water_enabled is True:
            operation = "preserve"
            reason = (
                "DHW no longer matches breaker-owned standby; ownership is cancelled."
            )
        elif water_guard is not None:
            operation = "unavailable"
            reason = water_guard
        elif tank_temperature is not None and tank_temperature <= 45:
            operation = "restore"
            reason = "DHW reached the 45°C comfort exit."
        elif readiness_active:
            operation = "restore"
            reason = "The next DHW readiness lead time is active."
        else:
            operation = "hold"
            reason = "Breaker-owned DHW standby remains within its lease."
        actions.append(
            ProtectionAction(
                LOAD_HOT_WATER,
                "daikin_hot_water",
                operation,
                "standby",
                "on" if operation == "restore" else "standby",
                reason,
            )
        )
    elif suppressed_for(LOAD_HOT_WATER):
        actions.append(
            ProtectionAction(
                LOAD_HOT_WATER,
                "daikin_hot_water",
                "keep_on",
                "on" if water_enabled else "standby",
                "on",
                "DHW already exited this published block and will not be stopped again.",
            )
        )
    elif water_guard is not None:
        actions.append(
            ProtectionAction(
                LOAD_HOT_WATER,
                "daikin_hot_water",
                "yield",
                "on" if water_enabled else None,
                None,
                water_guard,
            )
        )
    elif water_enabled is None:
        actions.append(
            ProtectionAction(
                LOAD_HOT_WATER,
                "daikin_hot_water",
                "unavailable",
                None,
                None,
                "Fresh DHW circuit power state is unavailable.",
            )
        )
    elif tank_temperature is None:
        actions.append(
            ProtectionAction(
                LOAD_HOT_WATER,
                "daikin_hot_water",
                "unavailable",
                "on" if water_enabled else None,
                None,
                "Fresh DHW tank temperature is unavailable.",
            )
        )
    elif tank_temperature <= 45 or readiness_active:
        actions.append(
            ProtectionAction(
                LOAD_HOT_WATER,
                "daikin_hot_water",
                "keep_on",
                "on" if water_enabled else "standby",
                "on",
                (
                    "DHW comfort is at or below 45°C."
                    if tank_temperature <= 45
                    else "The next DHW readiness lead time is active."
                ),
            )
        )
    elif water_enabled is True:
        actions.append(
            ProtectionAction(
                LOAD_HOT_WATER,
                "daikin_hot_water",
                "standby",
                "on",
                "standby",
                "Published all-in price is at or above the hard-stop threshold.",
            )
        )
    else:
        actions.append(
            ProtectionAction(
                LOAD_HOT_WATER,
                "daikin_hot_water",
                "no_op",
                "standby",
                "standby",
                "DHW is already in standby; the breaker claims no ownership.",
            )
        )

    if LOAD_FLOOR_HEATING in owned:
        if heating_enabled is None:
            operation = "unavailable"
            reason = (
                "Fresh breaker-owned floor-heating circuit state is unavailable."
            )
        elif heating_enabled is True:
            operation = "preserve"
            reason = (
                "Floor heating no longer matches breaker-owned standby; "
                "ownership is cancelled."
            )
        elif heating_guard is not None:
            operation = "unavailable"
            reason = heating_guard
        elif (
            room_temperature is not None
            and room_temperature <= EXTREME_PRICE_FLOOR_RESTORE_C
        ):
            operation = "restore"
            reason = (
                "The coldest occupied room reached the "
                f"{EXTREME_PRICE_FLOOR_RESTORE_C:g}°C restore guard."
            )
        else:
            operation = "hold"
            reason = "Breaker-owned floor-heating standby remains within its lease."
        actions.append(
            ProtectionAction(
                LOAD_FLOOR_HEATING,
                "daikin_floor_heating",
                operation,
                "standby",
                "on" if operation == "restore" else "standby",
                reason,
            )
        )
    elif suppressed_for(LOAD_FLOOR_HEATING):
        actions.append(
            ProtectionAction(
                LOAD_FLOOR_HEATING,
                "daikin_floor_heating",
                "keep_on",
                "on" if heating_enabled else "standby",
                "on",
                "Floor heating already exited this published block.",
            )
        )
    elif heating_guard is not None:
        actions.append(
            ProtectionAction(
                LOAD_FLOOR_HEATING,
                "daikin_floor_heating",
                "yield",
                "on" if heating_enabled else None,
                None,
                heating_guard,
            )
        )
    elif heating_enabled is None:
        actions.append(
            ProtectionAction(
                LOAD_FLOOR_HEATING,
                "daikin_floor_heating",
                "unavailable",
                None,
                None,
                "Fresh floor-heating circuit power state is unavailable.",
            )
        )
    elif room_temperature is None:
        actions.append(
            ProtectionAction(
                LOAD_FLOOR_HEATING,
                "daikin_floor_heating",
                "unavailable",
                "on" if heating_enabled else None,
                None,
                "Fresh occupied-room temperatures are unavailable.",
            )
        )
    elif room_temperature <= EXTREME_PRICE_FLOOR_RESTORE_C:
        actions.append(
            ProtectionAction(
                LOAD_FLOOR_HEATING,
                "daikin_floor_heating",
                "keep_on",
                "on" if heating_enabled else "standby",
                "on",
                (
                    "The coldest occupied room has insufficient headroom for "
                    "floor-heating standby."
                ),
            )
        )
    elif heating_enabled is True:
        actions.append(
            ProtectionAction(
                LOAD_FLOOR_HEATING,
                "daikin_floor_heating",
                "standby",
                "on",
                "standby",
                "Published all-in price is at or above the hard-stop threshold.",
            )
        )
    else:
        actions.append(
            ProtectionAction(
                LOAD_FLOOR_HEATING,
                "daikin_floor_heating",
                "no_op",
                "standby",
                "standby",
                "Floor heating is already in standby; the breaker claims no ownership.",
            )
        )

    for climate in climates:
        entity_id = climate["entity_id"]
        state = climate.get("state")
        if entity_id in owned:
            if not climate.get("available") or not climate.get("fresh"):
                actions.append(
                    ProtectionAction(
                        entity_id,
                        "climate",
                        "unavailable",
                        state,
                        None,
                        climate.get("reason")
                        or "Fresh breaker-owned climate state is unavailable.",
                    )
                )
            elif state != "off":
                actions.append(
                    ProtectionAction(
                        entity_id,
                        "climate",
                        "preserve",
                        state,
                        state,
                        "Climate no longer matches breaker-owned off state.",
                    )
                )
            else:
                actions.append(
                    ProtectionAction(
                        entity_id,
                        "climate",
                        "hold",
                        state,
                        "off",
                        "Breaker-owned cloud-reported off state remains within its lease.",
                    )
                )
        elif suppressed_for(entity_id):
            actions.append(
                ProtectionAction(
                    entity_id,
                    "climate",
                    "preserve",
                    state,
                    state,
                    "This climate unit already exited the current published block.",
                )
            )
        elif not climate.get("available") or not climate.get("fresh"):
            actions.append(
                ProtectionAction(
                    entity_id,
                    "climate",
                    "unavailable",
                    state,
                    None,
                    climate.get("reason")
                    or "Fresh cloud-reported climate state is unavailable.",
                )
            )
        elif state == "off":
            actions.append(
                ProtectionAction(
                    entity_id,
                    "climate",
                    "no_op",
                    "off",
                    "off",
                    "The climate unit is already off; no infrared command is proposed.",
                )
            )
        else:
            actions.append(
                ProtectionAction(
                    entity_id,
                    "climate",
                    "turn_off",
                    state,
                    "off",
                    (
                        "Published all-in price is at or above the hard-stop "
                        "threshold; cloud state is the confirmation basis."
                    ),
                )
            )

    partial = any(
        action.operation in {"unavailable", "yield", "keep_on", "preserve"}
        for action in actions
    )
    return ProtectionDecision(
        mode,
        "critical" if critical else ("partial" if partial else "active"),
        (
            "Published canonical all-in price is at or above "
            f"€{EXTREME_PRICE_THRESHOLD_EUR_KWH:g}/kWh. "
            + (
                "Simulation only — no device commands are sent."
                if mode == EXTREME_PRICE_MODE_SHADOW
                else "Each eligible load is controlled and confirmed independently."
            )
        ),
        current,
        active,
        warning,
        critical,
        True,
        tuple(actions),
        partial,
        next_hard,
    )


class ExtremePriceProtection:
    """HA runtime for the independent catastrophic-price policy."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: HouseholdPriceCoordinator,
        devices: EnergyDevices,
        optimizer: DhwOptimizer,
        floor_shadow,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.devices = devices
        self.optimizer = optimizer
        self.floor_shadow = floor_shadow
        configured = entry.options.get(CONF_EXTREME_PRICE_CLIMATE_ENTITIES)
        if not configured:
            configured = [
                entity_id
                for entity_id in entry.options.get(
                    CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES,
                    (),
                )
                if isinstance(entity_id, str)
                and entity_id.startswith("climate.")
            ]
        self.climate_entities = tuple(dict.fromkeys(configured or ()))
        self.room_entities = tuple(
            dict.fromkeys(
                entry.options.get(
                    CONF_FLOOR_INDOOR_TEMPERATURE_ENTITIES,
                    (),
                )
            )
        )
        self.store = Store(
            hass,
            STORE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.{STORE_KEY_SUFFIX}",
            private=True,
        )
        self.lock = asyncio.Lock()
        self.mode = EXTREME_PRICE_MODE_SHADOW
        self.decision: ProtectionDecision | None = None
        self.listeners: list[Callable[[], None]] = []
        self.unsubscribers: list[Callable[[], None]] = []
        self.state = {
            "policy_revision": POLICY_REVISION,
            "ownership": {},
            "suppressed_load_blocks": [],
            "fault": None,
            "last_alert_error": None,
            "alerted_blocks": [],
            "alert_attempts": {},
            "shadow_days": [],
            "decision_history": [],
            "confirmed_stops": 0,
            "confirmed_restores": 0,
            "thermal_contamination_active": False,
            "replay_verified": False,
            "alert_delivery_verified": False,
            "daikin_exercises_verified": False,
            "climate_exercises_verified": False,
        }

    async def async_start(self) -> None:
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            if stored.get("policy_revision") == POLICY_REVISION:
                mode = stored.get("mode")
                if mode in EXTREME_PRICE_MODES:
                    self.mode = mode
                for key in self.state:
                    if key in stored:
                        self.state[key] = stored[key]
            else:
                ownership = stored.get("ownership")
                if isinstance(ownership, dict):
                    self.state["ownership"] = ownership
                if isinstance(stored.get("fault"), str):
                    self.state["fault"] = stored["fault"]
                self.state["thermal_contamination_active"] = bool(
                    stored.get("thermal_contamination_active")
                )
        self._sync_floor_contamination()
        self.unsubscribers.append(
            self.coordinator.async_add_listener(self._schedule_evaluation)
        )
        self.unsubscribers.append(
            self.optimizer.async_add_listener(self._schedule_evaluation)
        )
        watched = tuple(
            dict.fromkeys(self.climate_entities + self.room_entities)
        )
        if watched:
            self.unsubscribers.append(
                async_track_state_change_event(
                    self.hass,
                    watched,
                    self._state_changed,
                )
            )
        self.unsubscribers.append(
            async_track_time_interval(
                self.hass,
                self._async_interval,
                timedelta(minutes=1),
                name=DOMAIN + " extreme price protection",
            )
        )
        await self.async_evaluate()

    async def async_stop(self) -> None:
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        await self._save()

    @callback
    def async_add_listener(
        self,
        listener: Callable[[], None],
    ) -> Callable[[], None]:
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
    def _state_changed(self, _event) -> None:
        self._schedule_evaluation()

    async def _async_interval(self, now: datetime) -> None:
        await self.async_evaluate(now)

    async def _save(self) -> None:
        await self.store.async_save({"mode": self.mode, **self.state})

    @property
    def armed_ready(self) -> bool:
        return (
            len(self.state["shadow_days"]) >= 7
            and self.state["replay_verified"] is True
            and self.state["alert_delivery_verified"] is True
            and self.state["daikin_exercises_verified"] is True
            and self.state["climate_exercises_verified"] is True
            and not self.state["fault"]
        )

    @property
    def available_modes(self) -> tuple[str, ...]:
        if self.armed_ready or self.mode == EXTREME_PRICE_MODE_ARMED:
            return EXTREME_PRICE_MODES
        return (EXTREME_PRICE_MODE_OFF, EXTREME_PRICE_MODE_SHADOW)

    def _sync_floor_contamination(self) -> None:
        if self.floor_shadow is None:
            return
        setter = getattr(self.floor_shadow, "set_external_influence", None)
        if setter is not None:
            setter(
                "extreme_price_protection",
                self.state["thermal_contamination_active"] is True,
                (
                    "Extreme-price protection involved active room HVAC; the "
                    "stop and recovery interval is not clean floor-response evidence."
                ),
            )

    def _room_observation(
        self,
        now: datetime,
    ) -> tuple[float | None, str | None, tuple[str, ...]]:
        values = []
        errors = []
        active = []
        if not self.room_entities:
            errors.append("No occupied-room temperature entities are configured.")
        for entity_id in self.room_entities:
            value, _updated, reason = read_room_temperature_source(
                self.hass,
                entity_id,
                now=now,
            )
            if value is None or reason is not None:
                errors.append(reason or f"{entity_id} is unavailable.")
                continue
            values.append(value)
            if entity_id.startswith("climate."):
                state = self.hass.states.get(entity_id)
                if (
                    state is not None
                    and state.state not in ("off", "unknown", "unavailable")
                ):
                    active.append(entity_id)
        if errors:
            return None, "; ".join(errors), tuple(active)
        return min(values), None, tuple(active)

    def _daikin_observation(
        self,
        status: dict,
        now: datetime,
    ) -> dict:
        heating = status.get("heating")
        water = status.get("hot_water")
        if not isinstance(heating, dict) or not isinstance(water, dict):
            return {
                "water_enabled": None,
                "tank_temperature": None,
                "water_guard": "Fresh Daikin circuit state is unavailable.",
                "heating_enabled": None,
                "room_temperature": None,
                "heating_guard": "Fresh Daikin circuit state is unavailable.",
            }
        heating_states = heating.get("states", {})
        water_states = water.get("states", {})
        heating_operations = heating.get("operations", {})
        water_operations = water.get("operations", {})
        water_power = water_operations.get("Power")
        heating_power = heating_operations.get("Power")
        water_enabled = (
            water_power == "on" if water_power in ("on", "standby") else None
        )
        heating_enabled = (
            heating_power == "on"
            if heating_power in ("on", "standby")
            else None
        )
        safety_guard = None
        for circuit in (heating_states, water_states):
            if not isinstance(circuit, dict) or any(
                circuit.get(flag) is not False for flag in FLAGS
            ):
                safety_guard = (
                    "A Daikin safety flag is active or unknown; protection yielded."
                )
                break
        water_guard = safety_guard
        if water_guard is None and water_enabled is None:
            water_guard = "Fresh DHW circuit power state is unavailable."
        if (
            water_guard is None
            and water_states.get("WeatherDependentState") is not False
        ):
            water_guard = (
                "Weather-dependent DHW state is active or unknown."
            )
        powerful = water_operations.get("powerful")
        if (
            water_guard is None
            and powerful not in (0, 0.0, "0", "0.0", False)
        ):
            water_guard = (
                "Daikin powerful/disinfection-like DHW operation is active."
            )
        configured_target = _number(
            water_operations.get("DomesticHotWaterTemperatureHeating")
        )
        effective_target = _number(
            status.get("effective_hot_water_target")
        )
        if (
            water_guard is None
            and (
                configured_target is None
                or effective_target is None
                or configured_target != effective_target
            )
        ):
            water_guard = (
                "Configured and effective DHW targets differ or are unknown."
            )
        heating_guard = safety_guard
        if heating_guard is None and heating_enabled is None:
            heating_guard = (
                "Fresh floor-heating circuit power state is unavailable."
            )
        room_temperature, room_error, active_hvac = self._room_observation(now)
        if heating_guard is None and status.get("control_mode") != "LWT control":
            heating_guard = "Floor protection requires verified LWT control."
        if (
            heating_guard is None
            and heating_operations.get("OperationMode") != "heating"
        ):
            heating_guard = "Daikin is not in heating operation mode."
        if (
            heating_guard is None
            and heating_states.get("TargetTemperatureOverruledState")
            not in (None, False)
        ):
            heating_guard = (
                "Daikin is temporarily overriding the space-heating target."
            )
        if heating_guard is None and room_error is not None:
            heating_guard = room_error
        if heating_guard is None and active_hvac:
            heating_guard = (
                "Room HVAC is active on "
                + ", ".join(active_hvac)
                + "; turn those units off before floor standby."
            )
        return {
            "water_enabled": water_enabled,
            "tank_temperature": _number(
                water.get("sensors", {}).get("TankTemperature")
            ),
            "water_guard": water_guard,
            "heating_enabled": heating_enabled,
            "room_temperature": room_temperature,
            "heating_guard": heating_guard,
        }

    async def _climate_observations(self) -> tuple[dict, ...]:
        result = []
        for entity_id in self.climate_entities:
            result.append(await self.devices.get_climate_status(entity_id))
        return tuple(result)

    def _readiness_active(self, now: datetime) -> bool:
        optimizer_decision = getattr(self.optimizer, "decision", None)
        if (
            optimizer_decision is not None
            and optimizer_decision.state in {"readiness", "recovery"}
        ):
            return True
        deadline = next_readiness(now, self.optimizer.policy)
        return deadline - now.astimezone(TALLINN) <= timedelta(
            minutes=self.optimizer.heating_minutes
        )

    def _record(self, now: datetime, decision: ProtectionDecision) -> None:
        previous = self.decision
        if (
            previous is not None
            and previous.state == decision.state
            and previous.reason == decision.reason
            and previous.active_block == decision.active_block
            and previous.next_hard_block == decision.next_hard_block
            and previous.warning_block == decision.warning_block
            and previous.actions == decision.actions
        ):
            return
        record = {
            "observed_at": now.isoformat(),
            "mode": decision.mode,
            "state": decision.state,
            "reason": decision.reason,
            "current_price_eur_kwh": (
                format(decision.current_price, "f")
                if decision.current_price is not None
                else None
            ),
            "active_block_id": (
                decision.active_block.identity
                if decision.active_block is not None
                else None
            ),
            "next_hard_block_id": (
                decision.next_hard_block.identity
                if decision.next_hard_block is not None
                else None
            ),
            "partial_shutdown": decision.partial,
            "actions": [
                {
                    "load_id": action.load_id,
                    "operation": action.operation,
                }
                for action in decision.actions
            ],
            "write_sent": False,
        }
        self.state["decision_history"] = (
            self.state["decision_history"] + [record]
        )[-DECISION_HISTORY_LIMIT:]
        self.hass.bus.async_fire(EVENT_EXTREME_PRICE_DECISION, record)

    async def _send_alert(
        self,
        *,
        title: str,
        message: str,
        dedupe_key: str | None,
    ) -> None:
        if dedupe_key is not None and dedupe_key in self.state["alerted_blocks"]:
            return
        attempts = self.state.get("alert_attempts")
        if not isinstance(attempts, dict):
            attempts = {}
            self.state["alert_attempts"] = attempts
        if dedupe_key is not None:
            last_attempt = _instant(attempts.get(dedupe_key))
            now = dt_util.utcnow().astimezone(UTC)
            if (
                last_attempt is not None
                and now - last_attempt.astimezone(UTC)
                < timedelta(minutes=15)
            ):
                return
            attempts[dedupe_key] = now.isoformat()
            self.state["alert_attempts"] = dict(
                list(attempts.items())[-64:]
            )
        persistent_notification.async_create(
            self.hass,
            message,
            title,
            NOTIFICATION_ID,
        )
        try:
            await self.hass.services.async_call(
                "notify",
                MOBILE_NOTIFY_SERVICE,
                {"title": title, "message": message},
                blocking=True,
            )
        except HomeAssistantError as error:
            self.state["last_alert_error"] = str(error)
            LOGGER.error("Extreme-price mobile notification failed: %s", error)
        else:
            self.state["last_alert_error"] = None
            if dedupe_key is not None:
                self.state["alerted_blocks"] = (
                    self.state["alerted_blocks"] + [dedupe_key]
                )[-64:]
                attempts.pop(dedupe_key, None)

    async def _alert_decision(self, decision: ProtectionDecision) -> None:
        if self.mode == EXTREME_PRICE_MODE_OFF:
            if not self.state["fault"]:
                persistent_notification.async_dismiss(
                    self.hass,
                    NOTIFICATION_ID,
                )
            return
        mode_note = (
            " Shadow simulation only; no device commands are sent."
            if self.mode == EXTREME_PRICE_MODE_SHADOW
            else " Loads are controlled and confirmed independently."
        )
        if decision.active_block is not None:
            block = decision.active_block
            await self._send_alert(
                title=(
                    "Critical electricity price"
                    if decision.critical
                    else (
                        "Extreme price protection partial"
                        if decision.partial
                        else "Extreme price protection active"
                    )
                ),
                message=(
                    f"Published household price reached "
                    f"€{format(decision.current_price, 'f')}/kWh. "
                    f"Block: {block.start.astimezone(TALLINN):%d %b %H:%M}"
                    f"–{block.end.astimezone(TALLINN):%H:%M}."
                    + (
                        " One or more loads cannot be fully protected."
                        if decision.partial
                        else ""
                    )
                    + mode_note
                ),
                dedupe_key=block.identity + "|active",
            )
            for action in decision.actions:
                if action.operation != "restore":
                    continue
                await self._send_alert(
                    title="Extreme price protection early restore",
                    message=(
                        f"{action.load_id} must restore before the published "
                        f"extreme-price block ends: {action.reason}"
                        + mode_note
                    ),
                    dedupe_key=(
                        block.identity
                        + "|early_restore|"
                        + action.load_id
                    ),
                )
        elif decision.warning_block is not None:
            block = decision.warning_block
            await self._send_alert(
                title="Extended high-price warning",
                message=(
                    f"Published prices stay at or above "
                    f"€{EXTREME_PRICE_WARNING_EUR_KWH:g}/kWh for "
                    f"{int(block.duration.total_seconds() // 60)} minutes from "
                    f"{block.start.astimezone(TALLINN):%d %b %H:%M}."
                    + mode_note
                ),
                dedupe_key=block.identity + "|warning",
            )
        elif not self.state["fault"]:
            persistent_notification.async_dismiss(
                self.hass,
                NOTIFICATION_ID,
            )

    def _suppression_key(self, block_id: str, load_id: str) -> str:
        return block_id + "|" + load_id

    def _suppress(self, block_id: str, load_id: str) -> None:
        key = self._suppression_key(block_id, load_id)
        if key not in self.state["suppressed_load_blocks"]:
            self.state["suppressed_load_blocks"] = (
                self.state["suppressed_load_blocks"] + [key]
            )[-128:]

    def _ownership(self) -> dict:
        ownership = self.state.get("ownership")
        if not isinstance(ownership, dict):
            ownership = {}
            self.state["ownership"] = ownership
        return ownership

    def _reconcile_ownership(
        self,
        daikin: dict,
        climate_rows: tuple[dict, ...],
    ) -> None:
        ownership = self._ownership()
        climates = {row["entity_id"]: row for row in climate_rows}
        for load_id, owner in tuple(ownership.items()):
            if not isinstance(owner, dict):
                ownership.pop(load_id, None)
                continue
            changed = False
            if load_id == LOAD_HOT_WATER:
                changed = daikin["water_enabled"] is True
            elif load_id == LOAD_FLOOR_HEATING:
                changed = daikin["heating_enabled"] is True
            else:
                current = climates.get(load_id)
                previous = owner.get("previous")
                changed = (
                    current is not None
                    and current.get("available")
                    and current.get("fresh")
                    and (
                        current.get("state") != "off"
                        or not isinstance(previous, dict)
                        or current.get("identity")
                        != previous.get("identity")
                    )
                )
            if not changed:
                continue
            ownership.pop(load_id, None)
            block_id = owner.get("block_id")
            if isinstance(block_id, str):
                self._suppress(block_id, load_id)

    async def _fault(self, message: str) -> None:
        self.state["fault"] = message
        await self._send_alert(
            title="Extreme price protection fault",
            message=message,
            dedupe_key=None,
        )

    async def _restore_load(
        self,
        load_id: str,
        owner: dict,
        daikin: dict,
        climates: dict[str, dict],
        block_id: str | None,
        reason: str,
    ) -> bool:
        if load_id == LOAD_HOT_WATER:
            if daikin["water_enabled"] is None:
                raise HomeAssistantError(
                    "Fresh DHW circuit state is unavailable."
                )
            if daikin["water_enabled"] is True:
                self._ownership().pop(load_id, None)
                if block_id is not None:
                    self._suppress(block_id, load_id)
                return False
            result = await self.devices.control_daikin(
                {"operation": "hot_water_enabled", "value": True},
                Context(),
            )
            if result.get("confirmed") != "on":
                raise HomeAssistantError("DHW re-enable was not confirmed.")
        elif load_id == LOAD_FLOOR_HEATING:
            if daikin["heating_enabled"] is None:
                raise HomeAssistantError(
                    "Fresh floor-heating circuit state is unavailable."
                )
            if daikin["heating_enabled"] is True:
                self._ownership().pop(load_id, None)
                if block_id is not None:
                    self._suppress(block_id, load_id)
                return False
            result = await self.devices.control_daikin(
                {"operation": "heating_enabled", "value": True},
                Context(),
            )
            if result.get("confirmed") != "on":
                raise HomeAssistantError(
                    "Floor-heating re-enable was not confirmed."
                )
        else:
            current = climates.get(load_id)
            if current is None or not current.get("fresh"):
                raise HomeAssistantError(
                    f"Fresh cloud state for {load_id} is unavailable."
                )
            if current.get("state") != "off":
                self._ownership().pop(load_id, None)
                if block_id is not None:
                    self._suppress(block_id, load_id)
                return False
            result = await self.devices.control_climate(
                {
                    "entity_id": load_id,
                    "operation": "restore",
                    "expected": current,
                    "restore_state": owner["previous"],
                },
                Context(),
            )
            if not result.get("changed"):
                raise HomeAssistantError(
                    f"Climate restoration for {load_id} was not confirmed."
                )
        self._ownership().pop(load_id, None)
        if block_id is not None and reason != "block_end":
            self._suppress(block_id, load_id)
        self.state["confirmed_restores"] += 1
        return True

    async def _stop_load(
        self,
        action: ProtectionAction,
        block: PriceBlock,
        now: datetime,
        daikin: dict,
        climates: dict[str, dict],
    ) -> bool:
        lease_expires = min(
            block.end,
            now + timedelta(minutes=EXTREME_PRICE_MAX_LEASE_MINUTES),
        )
        if action.load_id == LOAD_HOT_WATER:
            result = await self.devices.control_daikin(
                {"operation": "hot_water_enabled", "value": False},
                Context(),
            )
            if result.get("confirmed") != "standby":
                raise HomeAssistantError("DHW standby was not confirmed.")
            previous = {"power": "on"}
        elif action.load_id == LOAD_FLOOR_HEATING:
            result = await self.devices.control_daikin(
                {"operation": "heating_enabled", "value": False},
                Context(),
            )
            if result.get("confirmed") != "standby":
                raise HomeAssistantError(
                    "Floor-heating standby was not confirmed."
                )
            previous = {"power": "on"}
        else:
            previous = climates[action.load_id]
            result = await self.devices.control_climate(
                {
                    "entity_id": action.load_id,
                    "operation": "turn_off",
                    "expected": previous,
                },
                Context(),
            )
            if not result.get("changed"):
                return False
            self.state["thermal_contamination_active"] = True
            self._sync_floor_contamination()
        self._ownership()[action.load_id] = {
            "block_id": block.identity,
            "lease_expires": lease_expires.isoformat(),
            "previous": previous,
            "stopped_at": now.isoformat(),
        }
        self.state["confirmed_stops"] += 1
        return True

    async def _apply(
        self,
        now: datetime,
        decision: ProtectionDecision,
        daikin: dict,
        climate_rows: tuple[dict, ...],
    ) -> None:
        if self.state["fault"]:
            return
        ownership = self._ownership()
        climates = {row["entity_id"]: row for row in climate_rows}
        active_id = (
            decision.active_block.identity
            if decision.active_block is not None
            else None
        )
        actions = {action.load_id: action for action in decision.actions}
        restored_or_stopped = False

        for load_id in (
            LOAD_HOT_WATER,
            LOAD_FLOOR_HEATING,
            *self.climate_entities,
        ):
            owner = ownership.get(load_id)
            if not isinstance(owner, dict):
                continue
            owner_block = owner.get("block_id")
            lease_expires = _instant(owner.get("lease_expires"))
            action = actions.get(load_id)
            reason = None
            if self.mode != EXTREME_PRICE_MODE_ARMED:
                reason = "mode_change"
            elif decision.active_block is None or owner_block != active_id:
                reason = "block_end"
            elif lease_expires is None or now >= lease_expires.astimezone(UTC):
                reason = "lease_expiry"
            elif action is not None and action.operation == "restore":
                reason = "comfort_exit"
            elif action is not None and action.operation == "unavailable":
                reason = "stale_input"
            if reason is None:
                continue
            try:
                restored_or_stopped |= await self._restore_load(
                    load_id,
                    owner,
                    daikin,
                    climates,
                    owner_block if isinstance(owner_block, str) else active_id,
                    reason,
                )
            except (HomeAssistantError, TimeoutError) as error:
                await self._fault(
                    f"Restore for {load_id} failed and may have applied. "
                    f"Inspect fresh state before review. {error}"
                )
                return

        if (
            self.mode != EXTREME_PRICE_MODE_ARMED
            or decision.active_block is None
            or self.state["fault"]
        ):
            if (
                decision.active_block is None
                and not self._ownership()
                and self.state["thermal_contamination_active"]
            ):
                self.state["thermal_contamination_active"] = False
                self._sync_floor_contamination()
            if restored_or_stopped and self.state["decision_history"]:
                self.state["decision_history"][-1]["write_sent"] = True
            return

        stop_order = (
            LOAD_HOT_WATER,
            *self.climate_entities,
            LOAD_FLOOR_HEATING,
        )
        for load_id in stop_order:
            action = actions.get(load_id)
            if (
                action is None
                or action.operation not in ("standby", "turn_off")
                or load_id in self._ownership()
                or self._suppression_key(active_id, load_id)
                in self.state["suppressed_load_blocks"]
            ):
                continue
            try:
                restored_or_stopped |= await self._stop_load(
                    action,
                    decision.active_block,
                    now,
                    daikin,
                    climates,
                )
            except (HomeAssistantError, TimeoutError) as error:
                await self._fault(
                    f"Stop for {load_id} failed and may have applied. "
                    f"The combined shutdown is partial. {error}"
                )
                return
        if restored_or_stopped and self.state["decision_history"]:
            self.state["decision_history"][-1]["write_sent"] = True

    async def async_evaluate(self, now: datetime | None = None) -> None:
        async with self.lock:
            observed_at = (now or dt_util.utcnow()).astimezone(UTC)
            try:
                status = await self.devices.get_daikin_status()
                daikin = self._daikin_observation(status, observed_at)
                climates = await self._climate_observations()
                climate_floor_guards = [
                    row["entity_id"]
                    for row in climates
                    if (
                        not row.get("available")
                        or not row.get("fresh")
                        or row.get("state") != "off"
                    )
                ]
                if (
                    daikin["heating_guard"] is None
                    and climate_floor_guards
                ):
                    daikin["heating_guard"] = (
                        "Protected climate state must be fresh and off before "
                        "floor standby: "
                        + ", ".join(climate_floor_guards)
                        + "."
                    )
                self._reconcile_ownership(daikin, climates)
                decision = plan_extreme_price_protection(
                    now=observed_at,
                    mode=self.mode,
                    intervals=published_intervals(self.coordinator),
                    water_enabled=daikin["water_enabled"],
                    tank_temperature=daikin["tank_temperature"],
                    water_guard=daikin["water_guard"],
                    readiness_active=self._readiness_active(observed_at),
                    heating_enabled=daikin["heating_enabled"],
                    room_temperature=daikin["room_temperature"],
                    heating_guard=daikin["heating_guard"],
                    climates=climates,
                    owned_loads=self._ownership(),
                    suppressed_load_blocks=self.state[
                        "suppressed_load_blocks"
                    ],
                )
                local_day = observed_at.astimezone(TALLINN).date().isoformat()
                if (
                    self.mode == EXTREME_PRICE_MODE_SHADOW
                    and local_day not in self.state["shadow_days"]
                ):
                    self.state["shadow_days"] = (
                        self.state["shadow_days"] + [local_day]
                    )[-45:]
                self._record(observed_at, decision)
                self.decision = decision
                await self._alert_decision(decision)
                await self._apply(observed_at, decision, daikin, climates)
            except (HomeAssistantError, TimeoutError, ValueError, KeyError) as error:
                self.decision = ProtectionDecision(
                    self.mode,
                    "unavailable",
                    str(error),
                    None,
                    None,
                    None,
                    False,
                    False,
                    (),
                    bool(self._ownership()),
                )
            await self._save()
            self._notify_listeners()

    async def async_set_mode(self, mode: str) -> None:
        if mode not in EXTREME_PRICE_MODES:
            raise HomeAssistantError(
                "Unknown extreme price protection mode."
            )
        if mode == EXTREME_PRICE_MODE_ARMED and not self.armed_ready:
            raise HomeAssistantError(
                "Armed mode requires seven Shadow days, deterministic replay, "
                "verified alert delivery, and controlled Daikin and climate exercises."
            )
        self.mode = mode
        await self._save()
        await self.async_evaluate()

    async def async_resume_after_review(self) -> None:
        if not self.state["fault"]:
            return
        await self.devices.get_daikin_status()
        for entity_id in self.climate_entities:
            current = await self.devices.get_climate_status(entity_id)
            if not current.get("available") or not current.get("fresh"):
                raise HomeAssistantError(
                    f"Fresh climate state for {entity_id} is unavailable."
                )
        self.state["fault"] = None
        await self._save()
        await self.async_evaluate()

    @property
    def attributes(self) -> dict:
        decision = self.decision
        result = (
            decision.attributes()
            if decision is not None
            else {
                "mode": self.mode,
                "state": "starting",
                "reason": "The protection runtime has not evaluated yet.",
            }
        )
        ownership = self._ownership()
        result.update(
            {
                "hard_stop_threshold_eur_kwh": (
                    EXTREME_PRICE_THRESHOLD_EUR_KWH
                ),
                "critical_alert_threshold_eur_kwh": (
                    EXTREME_PRICE_CRITICAL_EUR_KWH
                ),
                "warning_threshold_eur_kwh": (
                    EXTREME_PRICE_WARNING_EUR_KWH
                ),
                "warning_duration_minutes": (
                    EXTREME_PRICE_WARNING_MINUTES
                ),
                "maximum_lease_minutes": EXTREME_PRICE_MAX_LEASE_MINUTES,
                "floor_restore_temperature_c": (
                    EXTREME_PRICE_FLOOR_RESTORE_C
                ),
                "write_allowed": (
                    self.mode == EXTREME_PRICE_MODE_ARMED
                    and not self.state["fault"]
                ),
                "shadow_zero_writes": (
                    self.mode == EXTREME_PRICE_MODE_SHADOW
                ),
                "armed_ready": self.armed_ready,
                "available_modes": list(self.available_modes),
                "shadow_days": len(self.state["shadow_days"]),
                "policy_revision": self.state["policy_revision"],
                "climate_entities": list(self.climate_entities),
                "owned_loads": [
                    {
                        "load_id": load_id,
                        "block_id": owner.get("block_id"),
                        "lease_expires": owner.get("lease_expires"),
                    }
                    for load_id, owner in ownership.items()
                    if isinstance(owner, dict)
                ],
                "confirmed_stops": self.state["confirmed_stops"],
                "confirmed_restores": self.state["confirmed_restores"],
                "fault": self.state["fault"],
                "last_alert_error": self.state["last_alert_error"],
                "thermal_response_contaminated": self.state[
                    "thermal_contamination_active"
                ],
                "confirmation_basis": (
                    "Daikin native read-back; climate cloud-reported state. "
                    "Physical HVAC power is not independently verified."
                ),
                "savings_status": "unavailable_without_attributable_energy",
                "promotion_requirements": {
                    "seven_shadow_days": len(self.state["shadow_days"]) >= 7,
                    "replay_verified": self.state["replay_verified"],
                    "alert_delivery_verified": self.state[
                        "alert_delivery_verified"
                    ],
                    "daikin_exercises_verified": self.state[
                        "daikin_exercises_verified"
                    ],
                    "climate_exercises_verified": self.state[
                        "climate_exercises_verified"
                    ],
                },
                "recent_decisions": self.state["decision_history"][-12:],
            }
        )
        return result
