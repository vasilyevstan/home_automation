"""Conservative open-loop Huawei inverter curtailment."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import logging
import math
import os
from pathlib import Path
import stat
from typing import Callable
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    SOLAR_CURTAILMENT_CAP_W,
    SOLAR_CURTAILMENT_ENTRY_EUR_KWH,
    SOLAR_CURTAILMENT_MAX_LEASE_MINUTES,
    SOLAR_CURTAILMENT_MIN_REMAINING_MINUTES,
    SOLAR_CURTAILMENT_MODE_ARMED,
    SOLAR_CURTAILMENT_MODE_OFF,
    SOLAR_CURTAILMENT_MODE_SHADOW,
    SOLAR_CURTAILMENT_MODES,
    SOLAR_CURTAILMENT_OUTPUT_GATE_W,
    SOLAR_CURTAILMENT_OUTPUT_SAMPLE_SECONDS,
    SOLAR_CURTAILMENT_QUALIFICATION_SECONDS,
)
from .controls import (
    EnergyDevices,
    SolarDependencyNotReady,
    SolarExternalOverrideError,
    SolarPostDispatchError,
    SolarPreDispatchError,
)
from .coordinator import HouseholdPriceCoordinator


LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
STATE_VERSION = 1
POLICY_REVISION = 1
STORE_KEY_SUFFIX = "solar_curtailment"
ACTIVE_POWER_MAX_AGE_SECONDS = 45
HISTORY_LIMIT = 64
STARTUP_DEPENDENCY_DEFERRAL_SECONDS = 60


def _open_private_directory(directory: Path) -> int:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise OSError("The private state directory cannot be a symlink.")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(directory, flags)


def _validate_private_filename(filename: str) -> None:
    if (
        not filename
        or filename in (".", "..")
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("Invalid private state filename.")


def _load_private_json(directory: Path, filename: str) -> dict | None:
    _validate_private_filename(filename)
    directory_fd = _open_private_directory(directory)
    try:
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("The private state path is not a regular file.")
            if metadata.st_mode & 0o077:
                raise PermissionError(
                    "The private state file must not be accessible by group "
                    "or other users."
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                result = json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(directory_fd)
    if not isinstance(result, dict):
        raise ValueError("The private solar state must be a JSON object.")
    return result


def _save_private_json(
    directory: Path,
    filename: str,
    value: dict,
) -> None:
    _validate_private_filename(filename)
    payload = (
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    directory_fd = _open_private_directory(directory)
    temporary = filename + "." + uuid4().hex + ".tmp"
    descriptor = -1
    replaced = False
    try:
        try:
            existing = os.stat(
                filename,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError(
                "The private state destination is not a regular file."
            )
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


class AcknowledgedStateFile:
    """Private atomic JSON state whose write errors reach the caller."""

    def __init__(
        self,
        hass: HomeAssistant,
        filename: str,
    ) -> None:
        _validate_private_filename(filename)
        self.hass = hass
        self.directory = Path(hass.config.path(".storage"))
        self.filename = filename

    async def async_load(self) -> dict | None:
        return await self.hass.async_add_executor_job(
            _load_private_json,
            self.directory,
            self.filename,
        )

    async def async_save(self, value: dict) -> None:
        await self.hass.async_add_executor_job(
            _save_private_json,
            self.directory,
            self.filename,
            value,
        )


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    if result.utcoffset() is None:
        return None
    return result.astimezone(timezone.utc)


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def is_shadow_opportunity(export_earnings: Decimal | None) -> bool:
    return export_earnings is not None and export_earnings < 0


def is_armed_entry_price(export_earnings: Decimal | None) -> bool:
    return (
        export_earnings is not None
        and export_earnings
        <= Decimal(str(SOLAR_CURTAILMENT_ENTRY_EUR_KWH))
    )


@dataclass(frozen=True)
class ExportBlock:
    start: datetime
    end: datetime
    end_known: bool
    interval_count: int
    profile_signature: str

    @property
    def identity(self) -> str:
        return "|".join(
            (
                self.profile_signature,
                self.start.isoformat(),
                self.end.isoformat(),
            )
        )

    def remaining_minutes(self, now: datetime) -> float:
        return max(0.0, (self.end - now).total_seconds() / 60)


@dataclass(frozen=True)
class PriceContext:
    valid: bool
    reason: str | None
    spot: Decimal | None
    import_price: Decimal | None
    export_earnings: Decimal | None
    period_start: datetime | None
    period_end: datetime | None
    profile_signature: str | None
    block: ExportBlock | None


@dataclass(frozen=True)
class SolarCurtailmentDecision:
    mode: str
    state: str
    reason: str


def _profile_signature(snapshot) -> str:
    return "|".join(
        (
            str(getattr(snapshot, "profile_id", "")),
            str(getattr(snapshot, "profile_version", "")),
            str(getattr(snapshot, "profile_sha256", "")),
        )
    )


def price_context(snapshot, now: datetime) -> PriceContext:
    """Build the current exact-price context and contiguous negative block."""
    if (
        snapshot is None
        or now.utcoffset() is None
        or not callable(getattr(snapshot, "contains", None))
        or not snapshot.contains(now)
    ):
        return PriceContext(
            False,
            "The canonical current price is stale or unavailable.",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    current = getattr(snapshot, "current", None)
    start = _instant(getattr(current, "start", None))
    end = _instant(getattr(current, "end", None))
    spot = _decimal(getattr(current, "spot_eur_kwh", None))
    import_price = _decimal(getattr(current, "import_eur_kwh", None))
    export = _decimal(
        getattr(current, "export_earnings_eur_kwh", None)
    )
    if (
        start is None
        or end is None
        or not start <= now.astimezone(timezone.utc) < end
        or spot is None
        or import_price is None
        or export is None
    ):
        return PriceContext(
            False,
            "The exact current spot, import, or export price is invalid.",
            spot,
            import_price,
            export,
            start,
            end,
            None,
            None,
        )
    signature = _profile_signature(snapshot)
    if export >= 0:
        return PriceContext(
            True,
            None,
            spot,
            import_price,
            export,
            start,
            end,
            signature,
            None,
        )

    rows: dict[datetime, tuple[datetime, Decimal]] = {}
    for window_name in ("today", "tomorrow"):
        window = getattr(snapshot, window_name, None)
        for row in getattr(window, "prices", ()) or ():
            row_start = _instant(getattr(row, "start", None))
            row_end = _instant(getattr(row, "end", None))
            row_export = _decimal(
                getattr(row, "export_earnings_eur_kwh", None)
            )
            if (
                row_start is None
                or row_end is None
                or row_end <= row_start
                or row_export is None
                or row_start in rows
            ):
                continue
            rows[row_start] = (row_end, row_export)
    ordered = sorted(rows.items())
    index = next(
        (
            position
            for position, (row_start, (row_end, row_export)) in enumerate(
                ordered
            )
            if row_start == start
            and row_end == end
            and row_export == export
        ),
        None,
    )
    if index is None:
        return PriceContext(
            False,
            "The exact current export price is missing from the published "
            "quarter sequence.",
            spot,
            import_price,
            export,
            start,
            end,
            signature,
            None,
        )
    block_start = start
    count = 1
    cursor = index - 1
    while cursor >= 0:
        previous_start, (previous_end, previous_export) = ordered[cursor]
        if previous_end != block_start or previous_export >= 0:
            break
        block_start = previous_start
        count += 1
        cursor -= 1
    block_end = end
    cursor = index + 1
    end_known = False
    while cursor < len(ordered):
        next_start, (next_end, next_export) = ordered[cursor]
        if next_start != block_end:
            break
        if next_export >= 0:
            end_known = True
            break
        block_end = next_end
        count += 1
        cursor += 1
    block = ExportBlock(
        block_start,
        block_end,
        end_known,
        count,
        signature,
    )
    return PriceContext(
        True,
        None,
        spot,
        import_price,
        export,
        start,
        end,
        signature,
        block,
    )


def _native_value(status: dict, key: str) -> Decimal | None:
    return _decimal(status.get(key))


def inverter_unrestricted(status: dict) -> bool:
    configured = _native_value(status, "configured")
    active = _native_value(status, "active")
    pmax = _native_value(status, "pmax_w")
    if configured is None or active is None or pmax is None:
        return False
    if configured != active:
        return False
    if status.get("mode") == "percent":
        return configured == Decimal("100")
    if status.get("mode") == "watts":
        return configured == pmax
    return False


def target_cap(status: dict) -> dict | None:
    """Return a native-step cap preserving the current limit mode."""
    pmax = _native_value(status, "pmax_w")
    minimum = _native_value(status, "limit_minimum")
    maximum = _native_value(status, "limit_maximum")
    step = _native_value(status, "limit_step")
    if (
        pmax is None
        or minimum is None
        or maximum is None
        or step is None
        or pmax <= SOLAR_CURTAILMENT_CAP_W
        or step <= 0
    ):
        return None
    mode = status.get("mode")
    if mode == "percent":
        raw = (
            Decimal(SOLAR_CURTAILMENT_CAP_W)
            * Decimal(100)
            / pmax
        )
    elif mode == "watts":
        raw = Decimal(SOLAR_CURTAILMENT_CAP_W)
    else:
        return None
    steps = ((raw - minimum) / step).to_integral_value(
        rounding=ROUND_FLOOR
    )
    value = minimum + steps * step
    if not minimum <= value <= maximum:
        return None
    unrestricted = Decimal(100) if mode == "percent" else pmax
    if value >= unrestricted:
        return None
    return {
        "mode": mode,
        "value": float(value),
        "equivalent_w": float(value * pmax / Decimal(100))
        if mode == "percent"
        else float(value),
        "step": float(step),
        "minimum": float(minimum),
        "maximum": float(maximum),
    }


def commissioning_replay() -> dict:
    """Run deterministic policy boundary checks without touching a device."""
    percent = target_cap(
        {
            "mode": "percent",
            "pmax_w": 8000,
            "limit_minimum": 0,
            "limit_maximum": 100,
            "limit_step": 0.1,
        }
    )
    watts = target_cap(
        {
            "mode": "watts",
            "pmax_w": 8000,
            "limit_minimum": 0,
            "limit_maximum": 8000,
            "limit_step": 1,
        }
    )
    checks = {
        "shadow_opportunity_below_zero": is_shadow_opportunity(
            Decimal("-0.0001")
        ),
        "shadow_not_at_zero": not is_shadow_opportunity(Decimal("0")),
        "armed_threshold_inclusive": is_armed_entry_price(
            Decimal("-0.010")
        ),
        "armed_threshold_rejects_less_negative": not is_armed_entry_price(
            Decimal("-0.009")
        ),
        "percentage_cap_8kw_is_50_percent": (
            percent is not None and percent["value"] == 50.0
        ),
        "watt_cap_is_4000w": (
            watts is not None and watts["value"] == 4000.0
        ),
        "lease_is_bounded": SOLAR_CURTAILMENT_MAX_LEASE_MINUTES == 120,
        "remaining_gate_is_30_minutes": (
            SOLAR_CURTAILMENT_MIN_REMAINING_MINUTES == 30
        ),
    }
    return {"verified": all(checks.values()), "checks": checks}


class SolarCurtailment:
    """Deterministic policy revision 1 runtime."""

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
        self.store = Store(
            hass,
            STORE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.{STORE_KEY_SUFFIX}",
            private=True,
        )
        self.state_file = AcknowledgedStateFile(
            hass,
            f"{DOMAIN}.{entry.entry_id}.{STORE_KEY_SUFFIX}.state.json",
        )
        self.lock = asyncio.Lock()
        self.stopping = False
        self.stopped = False
        self._state_loaded = False
        self.evaluation_tasks: set[asyncio.Task[None]] = set()
        self.active_evaluation_task: asyncio.Task | None = None
        self.startup_reconcile_pending = False
        self.startup_dependency_deferred_until: datetime | None = None
        self.mode = SOLAR_CURTAILMENT_MODE_SHADOW
        self.decision: SolarCurtailmentDecision | None = None
        self.listeners: list[Callable[[], None]] = []
        self.unsubscribers: list[Callable[[], None]] = []
        self.last_price: PriceContext | None = None
        self.last_status: dict | None = None
        self.last_target: dict | None = None
        self.state = {
            "state_version": STATE_VERSION,
            "policy_revision": POLICY_REVISION,
            "ownership": None,
            "fault": None,
            "persistence_fault": None,
            "external_override": None,
            "suppressed_blocks": [],
            "attempted_blocks": [],
            "qualification": None,
            "eligible_since": None,
            "last_operation": None,
            "commissioning": {
                "policy_replay_verified": False,
                "write_permission_verified": False,
                "identity_verified": False,
                "pmax_verified": False,
                "mode_preservation_verified": False,
                "stable_unrestricted_verified": False,
                "observed_identity": None,
                "stable_observation": None,
                "replay": None,
            },
        }

    async def async_start(self) -> None:
        stored = await self.state_file.async_load()
        if stored is None:
            stored = await self.store.async_load()
        if isinstance(stored, dict):
            same_revision = (
                stored.get("state_version") == STATE_VERSION
                and stored.get("policy_revision") == POLICY_REVISION
            )
            ownership = stored.get("ownership")
            fault = stored.get("fault")
            external_override = stored.get("external_override")
            if same_revision:
                mode = stored.get("mode")
                if mode in SOLAR_CURTAILMENT_MODES:
                    self.mode = mode
                for key in self.state:
                    if key in stored:
                        if key == "commissioning":
                            commissioning = stored.get(key)
                            if isinstance(commissioning, dict):
                                self.state[key].update(commissioning)
                        else:
                            self.state[key] = stored[key]
            else:
                if isinstance(ownership, dict):
                    self.state["ownership"] = ownership
                if isinstance(fault, str):
                    self.state["fault"] = fault
                if isinstance(external_override, dict):
                    self.state["external_override"] = external_override
                self.mode = SOLAR_CURTAILMENT_MODE_SHADOW
        if (
            self.mode == SOLAR_CURTAILMENT_MODE_ARMED
            and not self.commissioning_complete
        ):
            self.mode = SOLAR_CURTAILMENT_MODE_SHADOW
        self._state_loaded = True
        await self._startup_reconcile(dt_util.utcnow())
        self.unsubscribers.append(
            self.coordinator.async_add_listener(self._schedule_evaluation)
        )
        self.unsubscribers.append(
            async_track_time_interval(
                self.hass,
                self._schedule_evaluation,
                timedelta(seconds=30),
                name=DOMAIN + " solar curtailment",
            )
        )
        await self.async_evaluate()

    async def async_stop(self) -> None:
        if self.stopped:
            return
        self.stopping = True
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        current_task = asyncio.current_task()
        active_task = self.active_evaluation_task
        pending_tasks = [
            task
            for task in tuple(self.evaluation_tasks)
            if task is not current_task and task is not active_task
        ]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if (
            active_task is not None
            and active_task is not current_task
            and not active_task.done()
        ):
            await asyncio.gather(active_task, return_exceptions=True)
        if not self._state_loaded:
            self.stopped = True
            self._notify_listeners()
            return
        async with self.lock:
            self._reset_qualification()
            owner = self._ownership()
            if owner is not None and owner.get("phase") == "owned":
                try:
                    status = await self.devices.get_solar_status()
                except HomeAssistantError as error:
                    self.state["last_operation"] = {
                        "operation": "restore",
                        "phase": "pre_dispatch",
                        "success": False,
                        "reason": "integration_unload",
                        "error": str(error),
                        "at": dt_util.utcnow().isoformat(),
                    }
                else:
                    if self._owner_identity_matches(owner, status):
                        if self._limit_matches(
                            status, owner.get("target")
                        ):
                            await self._restore(
                                status,
                                "The integration is unloading.",
                                dt_util.utcnow(),
                            )
                        elif self._limit_matches(
                            status, owner.get("original")
                        ):
                            self.state["ownership"] = None
                        else:
                            self._cancel_for_external_change(
                                owner,
                                status,
                                "The inverter limit changed outside controller "
                                "ownership before unload.",
                                dt_util.utcnow(),
                            )
            await self._save()
            self.stopped = True
        self._notify_listeners()

    @callback
    def async_add_listener(
        self, listener: Callable[[], None]
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
    def _schedule_evaluation(self, now: datetime | None = None) -> None:
        if self.stopping:
            return
        task = self.entry.async_create_task(
            self.hass,
            self.async_evaluate(now),
            name="solar curtailment evaluation",
            eager_start=False,
        )
        self.evaluation_tasks.add(task)
        task.add_done_callback(self.evaluation_tasks.discard)

    async def _save(self) -> None:
        await self.state_file.async_save(
            {"mode": self.mode, **self.state}
        )

    def _ownership(self) -> dict | None:
        owner = self.state.get("ownership")
        return owner if isinstance(owner, dict) else None

    @staticmethod
    def _identity(status: dict) -> dict:
        return {
            "config_entry_id": status.get("config_entry_id"),
            "serial": status.get("serial"),
            "pmax_w": status.get("pmax_w"),
        }

    @staticmethod
    def _limit(status: dict) -> dict:
        return {
            "mode": status.get("mode"),
            "configured": status.get("configured"),
            "active": status.get("active"),
        }

    @staticmethod
    def _limit_matches(status: dict, expected: object) -> bool:
        if not isinstance(expected, dict):
            return False
        return (
            status.get("mode") == expected.get("mode")
            and _decimal(status.get("configured"))
            == _decimal(expected.get("configured"))
            and _decimal(status.get("active"))
            == _decimal(expected.get("active"))
        )

    def _owner_identity_matches(self, owner: dict, status: dict) -> bool:
        return owner.get("identity") == self._identity(status)

    @staticmethod
    def _request(status: dict, mode: str, value: float) -> dict:
        return {
            "mode": mode,
            "value": value,
            "expected_mode": status["mode"],
            "expected_value": status["configured"],
            "expected_active_value": status["active"],
            "expected_config_entry_id": status["config_entry_id"],
            "expected_serial": status["serial"],
            "expected_pmax_w": status["pmax_w"],
        }

    @property
    def commissioning_gates(self) -> dict:
        commissioning = self.state["commissioning"]
        return {
            key: commissioning.get(key) is True
            for key in (
                "policy_replay_verified",
                "write_permission_verified",
                "identity_verified",
                "pmax_verified",
                "mode_preservation_verified",
                "stable_unrestricted_verified",
            )
        }

    @property
    def commissioning_complete(self) -> bool:
        return (
            all(self.commissioning_gates.values())
            and not self.state.get("fault")
            and not self.state.get("persistence_fault")
        )

    @property
    def available_modes(self) -> tuple[str, ...]:
        if (
            self.commissioning_complete
            or self.mode == SOLAR_CURTAILMENT_MODE_ARMED
        ):
            return SOLAR_CURTAILMENT_MODES
        return (
            SOLAR_CURTAILMENT_MODE_OFF,
            SOLAR_CURTAILMENT_MODE_SHADOW,
        )

    def _trim(self, key: str) -> None:
        values = self.state.get(key)
        if isinstance(values, list) and len(values) > HISTORY_LIMIT:
            self.state[key] = values[-HISTORY_LIMIT:]

    def _suppress(self, block_id: object) -> None:
        if not isinstance(block_id, str):
            return
        values = self.state["suppressed_blocks"]
        if block_id not in values:
            values.append(block_id)
            self._trim("suppressed_blocks")

    def _attempted(self, block_id: object) -> None:
        if not isinstance(block_id, str):
            return
        values = self.state["attempted_blocks"]
        if block_id not in values:
            values.append(block_id)
            self._trim("attempted_blocks")

    def _forget_attempt(self, block_id: str) -> None:
        self.state["attempted_blocks"] = [
            item
            for item in self.state["attempted_blocks"]
            if item != block_id
        ]

    def _set_decision(self, state: str, reason: str) -> None:
        self.decision = SolarCurtailmentDecision(
            self.mode, state, reason
        )

    def _set_persistence_fault(
        self,
        *,
        kind: str,
        operation: str,
        message: str,
        now: datetime,
        error: str | None = None,
    ) -> None:
        self.state["persistence_fault"] = {
            "kind": kind,
            "operation": operation,
            "message": message,
            "error": error,
            "at": now.astimezone(timezone.utc).isoformat(),
        }
        self._set_decision("persistence_fault", message)

    def _reset_qualification(self) -> None:
        self.state["qualification"] = None
        self.state["eligible_since"] = None

    def _fresh_active_power(
        self, status: dict, now: datetime
    ) -> tuple[float | None, float | None, bool]:
        active_power = _finite_number(status.get("active_power_w"))
        observed = _instant(status.get("active_power_observed_at"))
        if active_power is None or observed is None:
            return active_power, None, False
        age = (now.astimezone(timezone.utc) - observed).total_seconds()
        return (
            active_power,
            age,
            -5 <= age <= ACTIVE_POWER_MAX_AGE_SECONDS,
        )

    def _update_commissioning_observation(
        self, status: dict, now: datetime
    ) -> None:
        commissioning = self.state["commissioning"]
        identity = {
            **self._identity(status),
            "mode": status.get("mode"),
        }
        previous_identity = commissioning.get("observed_identity")
        if previous_identity not in (None, identity):
            replay = commissioning.get("replay")
            replay_verified = commissioning.get(
                "policy_replay_verified", False
            )
            commissioning.update(
                {
                    "write_permission_verified": False,
                    "identity_verified": False,
                    "pmax_verified": False,
                    "mode_preservation_verified": False,
                    "stable_unrestricted_verified": False,
                    "observed_identity": identity,
                    "stable_observation": None,
                    "replay": replay,
                    "policy_replay_verified": replay_verified,
                }
            )
        else:
            commissioning["observed_identity"] = identity
        commissioning["write_permission_verified"] = (
            status.get("write_enabled") is True
            and self.devices.solar_write_available()
        )
        commissioning["identity_verified"] = (
            isinstance(status.get("config_entry_id"), str)
            and bool(status.get("config_entry_id"))
            and isinstance(status.get("serial"), str)
            and bool(status.get("serial"))
        )
        pmax = _native_value(status, "pmax_w")
        commissioning["pmax_verified"] = (
            pmax is not None and pmax > SOLAR_CURTAILMENT_CAP_W
        )
        cap = target_cap(status)
        commissioning["mode_preservation_verified"] = (
            cap is not None and cap["mode"] == status.get("mode")
        )
        if not (
            commissioning["write_permission_verified"]
            and commissioning["identity_verified"]
            and commissioning["pmax_verified"]
            and commissioning["mode_preservation_verified"]
            and inverter_unrestricted(status)
        ):
            commissioning["stable_observation"] = None
            commissioning["stable_unrestricted_verified"] = False
            return
        observation = commissioning.get("stable_observation")
        if (
            commissioning.get("stable_unrestricted_verified") is True
            and isinstance(observation, dict)
            and observation.get("identity") == identity
        ):
            return
        stamp = now.astimezone(timezone.utc)
        if (
            not isinstance(observation, dict)
            or observation.get("identity") != identity
        ):
            observation = {
                "identity": identity,
                "first_at": stamp.isoformat(),
                "last_at": stamp.isoformat(),
                "count": 1,
            }
        else:
            last = _instant(observation.get("last_at"))
            if (
                last is None
                or (stamp - last).total_seconds()
                >= SOLAR_CURTAILMENT_OUTPUT_SAMPLE_SECONDS
            ):
                observation["last_at"] = stamp.isoformat()
                observation["count"] = int(observation.get("count", 0)) + 1
        commissioning["stable_observation"] = observation
        first = _instant(observation.get("first_at"))
        last = _instant(observation.get("last_at"))
        commissioning["stable_unrestricted_verified"] = (
            int(observation.get("count", 0)) >= 2
            and first is not None
            and last is not None
            and (last - first).total_seconds()
            >= SOLAR_CURTAILMENT_OUTPUT_SAMPLE_SECONDS
        )

    def _update_qualification(
        self,
        status: dict,
        block: ExportBlock,
        now: datetime,
    ) -> tuple[bool, str]:
        active_power, _age, fresh = self._fresh_active_power(status, now)
        identity = {
            **self._identity(status),
            "mode": status.get("mode"),
            "original": self._limit(status),
        }
        if (
            not fresh
            or active_power is None
            or active_power < SOLAR_CURTAILMENT_OUTPUT_GATE_W
            or not inverter_unrestricted(status)
        ):
            self._reset_qualification()
            return False, (
                "Fresh inverter AC active power must be at least "
                f"{SOLAR_CURTAILMENT_OUTPUT_GATE_W} W while the configured and "
                "active original limits match and are unrestricted."
            )
        stamp = now.astimezone(timezone.utc)
        qualification = self.state.get("qualification")
        if (
            not isinstance(qualification, dict)
            or qualification.get("block_id") != block.identity
            or qualification.get("identity") != identity
        ):
            qualification = {
                "block_id": block.identity,
                "identity": identity,
                "started_at": stamp.isoformat(),
                "last_sample_at": stamp.isoformat(),
                "sample_count": 1,
            }
            self.state["qualification"] = qualification
            self.state["eligible_since"] = stamp.isoformat()
            return False, "The 60-second qualification dwell has started."
        last = _instant(qualification.get("last_sample_at"))
        if (
            last is None
            or (stamp - last).total_seconds()
            >= SOLAR_CURTAILMENT_OUTPUT_SAMPLE_SECONDS
        ):
            qualification["last_sample_at"] = stamp.isoformat()
            qualification["sample_count"] = (
                int(qualification.get("sample_count", 0)) + 1
            )
        started = _instant(qualification.get("started_at"))
        elapsed = (
            (stamp - started).total_seconds() if started is not None else 0
        )
        ready = (
            int(qualification.get("sample_count", 0)) >= 2
            and elapsed >= SOLAR_CURTAILMENT_QUALIFICATION_SECONDS
        )
        return (
            ready,
            (
                "The output and dwell gates are qualified."
                if ready
                else "Waiting for two high-output samples at least 30 seconds "
                "apart and the full 60-second dwell."
            ),
        )

    async def _startup_reconcile(self, now: datetime) -> bool:
        owner = self._ownership()
        if owner is None:
            try:
                status = await self.devices.get_solar_status()
            except SolarDependencyNotReady as error:
                self.state["last_operation"] = {
                    "operation": "startup_observation",
                    "phase": "dependency_deferred",
                    "success": False,
                    "error": str(error),
                    "at": now.isoformat(),
                }
            except HomeAssistantError as error:
                self.state["last_operation"] = {
                    "operation": "startup_observation",
                    "phase": "pre_dispatch",
                    "success": False,
                    "error": str(error),
                    "at": now.isoformat(),
                }
            else:
                self.last_status = status
                self.last_target = target_cap(status)
                self._update_commissioning_observation(status, now)
            await self._save()
            return True
        phase = owner.get("phase")
        if phase != "owned":
            self.state["fault"] = (
                "A persisted solar operation was pending when Home Assistant "
                "stopped. The device outcome is uncertain; use the resume "
                "button after reviewing the inverter limit."
            )
            self._set_decision("fault", self.state["fault"])
            await self._save()
            return True
        try:
            status = await self.devices.get_solar_status()
        except SolarDependencyNotReady as error:
            self.startup_reconcile_pending = True
            if self.startup_dependency_deferred_until is None:
                self.startup_dependency_deferred_until = (
                    now
                    + timedelta(
                        seconds=STARTUP_DEPENDENCY_DEFERRAL_SECONDS
                    )
                )
            bounded = now <= self.startup_dependency_deferred_until
            reason = (
                "Startup ownership reconciliation is deferred while the "
                "selected Huawei integration finishes loading."
                if bounded
                else "The bounded startup dependency wait elapsed. Persisted "
                "ownership remains unchanged and periodic reconciliation will "
                "continue without latching device uncertainty."
            )
            self.state["last_operation"] = {
                "operation": "startup_reconciliation",
                "phase": "dependency_deferred",
                "success": False,
                "error": str(error),
                "deferred_until": (
                    self.startup_dependency_deferred_until.isoformat()
                ),
                "at": now.isoformat(),
            }
            self._set_decision("startup_deferred", reason)
            await self._save()
            return False
        except HomeAssistantError as error:
            self.startup_reconcile_pending = False
            self.state["fault"] = (
                "Startup could not reconcile persisted solar ownership: "
                + str(error)
            )
            self._set_decision("fault", self.state["fault"])
            await self._save()
            return True
        self.startup_reconcile_pending = False
        self.startup_dependency_deferred_until = None
        self.last_status = status
        if not self._owner_identity_matches(owner, status):
            self._cancel_for_external_change(
                owner,
                status,
                "The persisted inverter identity or Pmax no longer matches.",
                now,
            )
        elif self._limit_matches(status, owner.get("target")):
            self._set_decision(
                "owned",
                "The persisted target cap is still exact; ownership was "
                "retained for policy reconciliation.",
            )
        elif self._limit_matches(status, owner.get("original")):
            self.state["ownership"] = None
            self._set_decision(
                "reconciled",
                "The inverter is exactly at the persisted original limit; "
                "ownership was cleared.",
            )
        else:
            self._cancel_for_external_change(
                owner,
                status,
                "The inverter is at neither the persisted target nor original "
                "limit.",
                now,
            )
        await self._save()
        return True

    def _cancel_for_external_change(
        self,
        owner: dict,
        status: dict,
        reason: str,
        now: datetime,
    ) -> None:
        self._suppress(owner.get("block_id"))
        self.state["external_override"] = {
            "detected_at": now.isoformat(),
            "block_id": owner.get("block_id"),
            "reason": reason,
            "observed_identity": self._identity(status),
            "observed_limit": self._limit(status),
        }
        self.state["ownership"] = None
        self._reset_qualification()
        self._set_decision("external_override", reason)

    async def _acquire(
        self,
        status: dict,
        target: dict,
        block: ExportBlock,
        now: datetime,
    ) -> None:
        if self.stopping:
            self._reset_qualification()
            self._set_decision(
                "stopping",
                "The integration is stopping; no inverter command was sent.",
            )
            return
        lease_end = min(
            now + timedelta(minutes=SOLAR_CURTAILMENT_MAX_LEASE_MINUTES),
            block.end,
        )
        owner = {
            "phase": "pending_acquire",
            "identity": self._identity(status),
            "block_id": block.identity,
            "block_end": block.end.isoformat(),
            "profile_signature": block.profile_signature,
            "original": self._limit(status),
            "target": {
                "mode": target["mode"],
                "configured": target["value"],
                "active": target["value"],
            },
            "lease_expires": lease_end.isoformat(),
            "pending_at": now.isoformat(),
            "acquired_at": None,
        }
        self.state["ownership"] = owner
        self._attempted(block.identity)
        try:
            await self._save()
        except Exception as error:
            self._forget_attempt(block.identity)
            self.state["ownership"] = None
            self.state["last_operation"] = {
                "operation": "acquire",
                "phase": "pre_dispatch_persistence",
                "success": False,
                "block_id": block.identity,
                "error": str(error),
                "at": now.isoformat(),
            }
            self._reset_qualification()
            self._set_decision(
                "pre_dispatch_failed",
                "The pending ownership record could not be persisted, so no "
                "inverter command was sent.",
            )
            LOGGER.error(
                "Could not persist pending solar acquisition; no command was "
                "sent",
                exc_info=True,
            )
            return
        if self.stopping:
            self._forget_attempt(block.identity)
            self.state["ownership"] = None
            self._reset_qualification()
            self.state["last_operation"] = {
                "operation": "acquire",
                "phase": "stopping_before_dispatch",
                "success": False,
                "block_id": block.identity,
                "at": now.isoformat(),
            }
            try:
                await self._save()
            except Exception as error:
                self._set_persistence_fault(
                    kind="state",
                    operation="acquire",
                    message=(
                        "No inverter command was sent during shutdown, but "
                        "clearing the prepared ownership state could not be "
                        "persisted."
                    ),
                    now=now,
                    error=type(error).__name__ + ": " + str(error),
                )
                LOGGER.exception(
                    "Could not clear the undispatched solar acquisition "
                    "during shutdown"
                )
                return
            self._set_decision(
                "stopping",
                "The integration began stopping after preparation; no "
                "inverter command was sent.",
            )
            return
        request = self._request(status, target["mode"], target["value"])
        try:
            result = await self.devices.set_solar_limit(
                request, automatic=True
            )
        except SolarExternalOverrideError as error:
            self._forget_attempt(block.identity)
            self._cancel_for_external_change(
                owner,
                error.status,
                str(error),
                now,
            )
            self.state["last_operation"] = {
                "operation": "acquire",
                "phase": "pre_dispatch_external_override",
                "success": False,
                "block_id": block.identity,
                "error": str(error),
                "at": now.isoformat(),
            }
            await self._save()
            return
        except SolarPreDispatchError as error:
            self._forget_attempt(block.identity)
            self.state["ownership"] = None
            self.state["last_operation"] = {
                "operation": "acquire",
                "phase": "pre_dispatch",
                "success": False,
                "block_id": block.identity,
                "error": str(error),
                "at": now.isoformat(),
            }
            self._reset_qualification()
            await self._save()
            self._set_decision(
                "pre_dispatch_failed",
                "The acquisition failed before dispatch and may requalify: "
                + str(error),
            )
            return
        except SolarPostDispatchError as error:
            self.state["fault"] = str(error)
            self.state["last_operation"] = {
                "operation": "acquire",
                "phase": "post_dispatch_uncertain",
                "success": False,
                "block_id": block.identity,
                "error": str(error),
                "at": now.isoformat(),
            }
            await self._save()
            self._set_decision("fault", str(error))
            return
        owner.update(
            {
                "phase": "owned",
                "acquired_at": now.isoformat(),
                "confirmed": result.get("confirmed"),
                "audit_persisted": result.get("audit_persisted"),
                "audit_error": result.get("audit_error"),
            }
        )
        self.state["ownership"] = owner
        self.state["last_operation"] = {
            "operation": "acquire",
            "phase": "verified",
            "success": True,
            "changed": result.get("changed"),
            "block_id": block.identity,
            "audit_persisted": result.get("audit_persisted"),
            "audit_error": result.get("audit_error"),
            "at": now.isoformat(),
            "device_outcome": "target_verified",
            "state_persisted": True,
        }
        self.state["qualification"] = None
        audit_failed = result.get("audit_persisted") is False
        if audit_failed:
            self.state["last_operation"]["phase"] = (
                "verified_audit_persistence_failed"
            )
            self._set_persistence_fault(
                kind="audit",
                operation="acquire",
                message=(
                    "The inverter exactly verified the owned target, but the "
                    "final verified audit record could not be persisted."
                ),
                now=now,
                error=result.get("audit_error"),
            )
        try:
            await self._save()
        except Exception as error:
            self.state["last_operation"]["state_persisted"] = False
            self.state["last_operation"]["phase"] = (
                "verified_state_and_audit_persistence_failed"
                if audit_failed
                else "verified_state_persistence_failed"
            )
            self.state["last_operation"]["error"] = str(error)
            self._set_persistence_fault(
                kind="state_and_audit" if audit_failed else "state",
                operation="acquire",
                message=(
                    "The inverter exactly verified the owned target, but the "
                    "confirmed controller state"
                    + (
                        " and final verified audit record"
                        if audit_failed
                        else ""
                    )
                    + " could not be persisted. The physical target remains "
                    "classified as verified and owned in this runtime."
                ),
                now=now,
                error=type(error).__name__ + ": " + str(error),
            )
            LOGGER.error(
                "Solar acquisition was device-verified but final ownership "
                "persistence failed",
                exc_info=True,
            )
            return
        if audit_failed:
            return
        self._set_decision(
            "owned",
            "The exact mode-preserving cap is verified and owned until the "
            "negative block or lease ends.",
        )

    async def _restore(
        self,
        status: dict,
        reason: str,
        now: datetime,
    ) -> None:
        owner = self._ownership()
        if owner is None or owner.get("phase") != "owned":
            return
        original = owner.get("original")
        if (
            not self._owner_identity_matches(owner, status)
            or not self._limit_matches(status, owner.get("target"))
            or not isinstance(original, dict)
            or original.get("configured") != original.get("active")
        ):
            self._cancel_for_external_change(
                owner,
                status,
                "Exact owned target and stable original state are required "
                "before restoration.",
                now,
            )
            await self._save()
            return
        owner["phase"] = "pending_restore"
        owner["restore_reason"] = reason
        owner["pending_at"] = now.isoformat()
        try:
            await self._save()
        except Exception as error:
            owner["phase"] = "owned"
            owner.pop("restore_reason", None)
            self.state["last_operation"] = {
                "operation": "restore",
                "phase": "pre_dispatch_persistence",
                "success": False,
                "reason": reason,
                "error": str(error),
                "at": now.isoformat(),
            }
            self._set_decision(
                "restore_wait",
                "The pending restoration record could not be persisted, so "
                "no inverter command was sent.",
            )
            LOGGER.error(
                "Could not persist pending solar restoration; no command was "
                "sent",
                exc_info=True,
            )
            return
        request = self._request(
            status,
            original["mode"],
            original["configured"],
        )
        try:
            result = await self.devices.set_solar_limit(
                request, automatic=True
            )
        except SolarExternalOverrideError as error:
            owner["phase"] = "owned"
            owner.pop("restore_reason", None)
            self.state["ownership"] = owner
            if (
                not self._owner_identity_matches(owner, error.status)
                or not self._limit_matches(
                    error.status, owner.get("target")
                )
            ):
                self._cancel_for_external_change(
                    owner,
                    error.status,
                    str(error),
                    now,
                )
            else:
                self.state["external_override"] = {
                    "detected_at": now.isoformat(),
                    "block_id": owner.get("block_id"),
                    "reason": str(error),
                    "observed_identity": self._identity(error.status),
                    "observed_limit": self._limit(error.status),
                    "ownership_retained": True,
                }
                self._set_decision(
                    "restore_wait",
                    "Fresh inverter state changed immediately before restore. "
                    "No command was sent and exact target ownership is "
                    "retained for a later fresh attempt.",
                )
            self.state["last_operation"] = {
                "operation": "restore",
                "phase": "pre_dispatch_external_override",
                "success": False,
                "reason": reason,
                "error": str(error),
                "at": now.isoformat(),
            }
            await self._save()
            return
        except SolarPreDispatchError as error:
            owner["phase"] = "owned"
            owner.pop("restore_reason", None)
            self.state["ownership"] = owner
            self.state["last_operation"] = {
                "operation": "restore",
                "phase": "pre_dispatch",
                "success": False,
                "reason": reason,
                "error": str(error),
                "at": now.isoformat(),
            }
            await self._save()
            self._set_decision(
                "restore_wait",
                "Restoration failed before dispatch and will be reconsidered "
                "from fresh state: "
                + str(error),
            )
            return
        except SolarPostDispatchError as error:
            self.state["fault"] = str(error)
            self.state["last_operation"] = {
                "operation": "restore",
                "phase": "post_dispatch_uncertain",
                "success": False,
                "reason": reason,
                "error": str(error),
                "at": now.isoformat(),
            }
            await self._save()
            self._set_decision("fault", str(error))
            return
        self.state["ownership"] = None
        self.state["last_operation"] = {
            "operation": "restore",
            "phase": "verified",
            "success": True,
            "changed": result.get("changed"),
            "reason": reason,
            "audit_persisted": result.get("audit_persisted"),
            "audit_error": result.get("audit_error"),
            "at": now.isoformat(),
            "device_outcome": "original_verified",
            "state_persisted": True,
        }
        self._reset_qualification()
        audit_failed = result.get("audit_persisted") is False
        if audit_failed:
            self.state["last_operation"]["phase"] = (
                "verified_audit_persistence_failed"
            )
            self._set_persistence_fault(
                kind="audit",
                operation="restore",
                message=(
                    "The inverter exactly verified the original limit and "
                    "ownership is cleared, but the final verified audit record "
                    "could not be persisted."
                ),
                now=now,
                error=result.get("audit_error"),
            )
        try:
            await self._save()
        except Exception as error:
            self.state["last_operation"]["state_persisted"] = False
            self.state["last_operation"]["phase"] = (
                "verified_state_and_audit_persistence_failed"
                if audit_failed
                else "verified_state_persistence_failed"
            )
            self.state["last_operation"]["error"] = str(error)
            self._set_persistence_fault(
                kind="state_and_audit" if audit_failed else "state",
                operation="restore",
                message=(
                    "The inverter exactly verified the original limit and "
                    "ownership remains cleared in this runtime, but the "
                    "confirmed controller state"
                    + (
                        " and final verified audit record"
                        if audit_failed
                        else ""
                    )
                    + " could not be persisted."
                ),
                now=now,
                error=type(error).__name__ + ": " + str(error),
            )
            LOGGER.error(
                "Solar restoration was device-verified but final ownership "
                "persistence failed",
                exc_info=True,
            )
            return
        if audit_failed:
            return
        self._set_decision("restored", reason)

    def _refresh_transition_retains_ownership(
        self,
        owner: dict,
        now: datetime,
    ) -> bool:
        transition = getattr(
            self.coordinator,
            "in_quarter_refresh_transition",
            None,
        )
        if (
            self.mode != SOLAR_CURTAILMENT_MODE_ARMED
            or not callable(transition)
            or not transition(now)
        ):
            return False
        block_end = _instant(owner.get("block_end"))
        lease = _instant(owner.get("lease_expires"))
        snapshot = self.coordinator.data
        return (
            block_end is not None
            and lease is not None
            and now < block_end
            and now < lease
            and snapshot is not None
            and _profile_signature(snapshot)
            == owner.get("profile_signature")
        )

    async def _evaluate_owned(
        self, context: PriceContext, now: datetime
    ) -> None:
        owner = self._ownership()
        if owner is None:
            return
        if owner.get("phase") != "owned":
            self.state["fault"] = (
                self.state.get("fault")
                or "A solar operation is pending with an uncertain outcome; "
                "use the resume button after review."
            )
            self._set_decision("fault", self.state["fault"])
            return
        if self.state.get("fault"):
            self._set_decision("fault", self.state["fault"])
            return
        persistence_fault = self.state.get("persistence_fault")
        if isinstance(persistence_fault, dict):
            self._set_decision(
                "persistence_fault",
                str(
                    persistence_fault.get(
                        "message",
                        "Solar controller state persistence failed.",
                    )
                ),
            )
            return
        try:
            status = await self.devices.get_solar_status()
        except HomeAssistantError as error:
            self._set_decision(
                "owned_unverified",
                "Ownership is retained because fresh inverter state is "
                "unavailable; no blind restoration was attempted. Cause: "
                + str(error),
            )
            return
        self.last_status = status
        self.last_target = target_cap(status)
        if not self._owner_identity_matches(owner, status):
            self._cancel_for_external_change(
                owner,
                status,
                "The inverter config entry, serial, or Pmax changed during "
                "ownership.",
                now,
            )
            return
        if not self._limit_matches(status, owner.get("target")):
            if self._limit_matches(status, owner.get("original")):
                reason = (
                    "The original inverter limit was restored outside "
                    "controller ownership."
                )
            else:
                reason = (
                    "The inverter limit changed outside controller ownership."
                )
            self._cancel_for_external_change(owner, status, reason, now)
            return
        if (
            not context.valid
            and self._refresh_transition_retains_ownership(owner, now)
        ):
            self._set_decision(
                "owned_refreshing",
                "The previous quarter just expired while the bounded canonical "
                "price refresh is in progress. Exact target ownership remains "
                "authorized by the persisted block, pricing profile, and "
                "lease until refreshed data arrives.",
            )
            return
        restore_reason = None
        if self.mode != SOLAR_CURTAILMENT_MODE_ARMED:
            restore_reason = "Solar curtailment mode changed."
        elif not context.valid:
            restore_reason = (
                "The exact export price is stale or invalid; restore the "
                "original limit."
            )
        elif (
            context.profile_signature
            != owner.get("profile_signature")
        ):
            if context.block is not None:
                self._suppress(context.block.identity)
            restore_reason = (
                "The effective pricing profile changed; restore the original "
                "limit."
            )
        elif (
            context.export_earnings is None
            or context.export_earnings >= 0
        ):
            restore_reason = (
                "Net export earnings are no longer negative; restore the "
                "original limit."
            )
        elif (
            context.block is None
            or context.block.identity != owner.get("block_id")
            or now >= context.block.end
        ):
            if context.block is not None:
                self._suppress(context.block.identity)
            restore_reason = (
                "The owned contiguous negative-export block ended."
            )
        else:
            lease = _instant(owner.get("lease_expires"))
            if lease is None or now >= lease:
                restore_reason = (
                    "The maximum solar-curtailment lease expired."
                )
        if restore_reason is not None:
            await self._restore(status, restore_reason, now)
            return
        self._set_decision(
            "owned",
            "The mode-preserving cap remains owned. Lower PV output does not "
            "cause early restoration because the cap may simply be non-binding.",
        )

    async def _evaluate_unowned(
        self, context: PriceContext, now: datetime
    ) -> None:
        if self.state.get("fault"):
            self._set_decision("fault", self.state["fault"])
            return
        persistence_fault = self.state.get("persistence_fault")
        if isinstance(persistence_fault, dict):
            self._set_decision(
                "persistence_fault",
                str(
                    persistence_fault.get(
                        "message",
                        "Solar controller state persistence failed.",
                    )
                ),
            )
            return
        if not context.valid:
            self._reset_qualification()
            self._set_decision(
                "blocked",
                context.reason
                or "The exact current export price is unavailable.",
            )
            return
        if not is_shadow_opportunity(context.export_earnings):
            self._reset_qualification()
            self._set_decision(
                "idle",
                "Net export earnings are zero or positive; no curtailment "
                "opportunity exists.",
            )
            return
        block = context.block
        if block is None:
            self._reset_qualification()
            self._set_decision(
                "blocked",
                "The contiguous negative-export block is unavailable.",
            )
            return
        try:
            status = await self.devices.get_solar_status()
        except HomeAssistantError as error:
            self._reset_qualification()
            self._set_decision(
                "blocked",
                "Fresh inverter capability and active power are unavailable: "
                + str(error),
            )
            return
        self.last_status = status
        self.last_target = target_cap(status)
        self._update_commissioning_observation(status, now)
        remaining = block.remaining_minutes(now)
        if self.mode == SOLAR_CURTAILMENT_MODE_OFF:
            self._reset_qualification()
            self._set_decision(
                "off",
                "Solar curtailment is disabled.",
            )
            return
        if self.mode == SOLAR_CURTAILMENT_MODE_SHADOW:
            self._reset_qualification()
            self._set_decision(
                "shadow_opportunity",
                "Exact net export earnings are negative. Shadow mode records "
                "the opportunity and performs zero writes.",
            )
            return
        if block.identity in self.state["suppressed_blocks"]:
            self._reset_qualification()
            self._set_decision(
                "suppressed",
                "An external/manual limit change suppresses re-entry for this "
                "contiguous block.",
            )
            return
        if block.identity in self.state["attempted_blocks"]:
            self._reset_qualification()
            self._set_decision(
                "attempted",
                "The one dispatched acquisition attempt allowed for this "
                "contiguous block has already occurred.",
            )
            return
        if not self.commissioning_complete:
            self._reset_qualification()
            self._set_decision(
                "blocked",
                "Armed control is unavailable until every commissioning gate "
                "is complete.",
            )
            return
        if not is_armed_entry_price(context.export_earnings):
            self._reset_qualification()
            self._set_decision(
                "below_entry_threshold",
                "The negative export price is a Shadow opportunity but is not "
                "at or below the -0.010 EUR/kWh Armed entry threshold.",
            )
            return
        if (
            not block.end_known
            or remaining
            < SOLAR_CURTAILMENT_MIN_REMAINING_MINUTES
        ):
            self._reset_qualification()
            self._set_decision(
                "blocked",
                "The contiguous negative-export block must have a known end "
                "and at least 30 minutes remaining.",
            )
            return
        if status.get("write_enabled") is not True:
            self._reset_qualification()
            self._set_decision(
                "blocked",
                "The selected Huawei entry does not expose elevated native "
                "parameter configuration.",
            )
            return
        if not inverter_unrestricted(status):
            self._reset_qualification()
            self._set_decision(
                "blocked",
                "Automatic ownership requires matching configured and active "
                "limits at 100% or Pmax.",
            )
            return
        target = self.last_target
        if target is None:
            self._reset_qualification()
            self._set_decision(
                "non_binding",
                "Pmax or native limit capabilities do not permit a distinct "
                "4,000 W-equivalent cap.",
            )
            return
        ready, qualification_reason = self._update_qualification(
            status, block, now
        )
        if not ready:
            self._set_decision("qualifying", qualification_reason)
            return
        await self._acquire(status, target, block, now)

    async def async_evaluate(self, now: datetime | None = None) -> None:
        now = (now or dt_util.utcnow()).astimezone(timezone.utc)
        current_task = asyncio.current_task()
        async with self.lock:
            self.active_evaluation_task = current_task
            try:
                before = deepcopy(self.state)
                if self.stopping:
                    self._reset_qualification()
                    self._set_decision(
                        "stopping",
                        "The integration is stopping; no new inverter "
                        "ownership can be acquired.",
                    )
                else:
                    reconciled = True
                    if self.startup_reconcile_pending:
                        reconciled = await self._startup_reconcile(now)
                    if reconciled:
                        context = price_context(
                            self.coordinator.data, now
                        )
                        self.last_price = context
                        if self._ownership() is not None:
                            await self._evaluate_owned(context, now)
                        else:
                            await self._evaluate_unowned(context, now)
                if self.state != before:
                    await self._save()
            finally:
                if self.active_evaluation_task is current_task:
                    self.active_evaluation_task = None
        self._notify_listeners()

    async def async_set_mode(self, mode: str) -> None:
        if mode not in SOLAR_CURTAILMENT_MODES:
            raise HomeAssistantError("Unknown solar curtailment mode.")
        if (
            mode == SOLAR_CURTAILMENT_MODE_ARMED
            and not self.commissioning_complete
        ):
            raise HomeAssistantError(
                "Armed mode requires complete deterministic replay, elevated "
                "write permission, bound inverter identity/Pmax, preserved "
                "native mode, and stable unrestricted observations."
            )
        async with self.lock:
            if mode != self.mode:
                self._reset_qualification()
            self.mode = mode
            await self._save()
        await self.async_evaluate()

    async def async_verify_commissioning(self) -> None:
        if self.mode != SOLAR_CURTAILMENT_MODE_SHADOW:
            raise HomeAssistantError(
                "Solar curtailment commissioning requires Shadow mode."
            )
        if self.state.get("fault") or self._ownership() is not None:
            raise HomeAssistantError(
                "Resolve solar ownership or fault state before commissioning."
            )
        replay = commissioning_replay()
        if replay["verified"] is not True:
            raise HomeAssistantError(
                "The deterministic solar policy replay failed."
            )
        status = await self.devices.get_solar_status()
        async with self.lock:
            self.state["commissioning"]["replay"] = replay
            self.state["commissioning"][
                "policy_replay_verified"
            ] = True
            self.last_status = status
            self.last_target = target_cap(status)
            self._update_commissioning_observation(
                status, dt_util.utcnow()
            )
            await self._save()
        self._notify_listeners()
        if not self.commissioning_complete:
            missing = [
                key
                for key, passed in self.commissioning_gates.items()
                if not passed
            ]
            raise HomeAssistantError(
                "Commissioning evidence is incomplete: "
                + ", ".join(missing)
                + "."
            )

    async def async_resume_after_review(self) -> None:
        if (
            not self.state.get("fault")
            and not self.state.get("persistence_fault")
        ):
            return
        status = await self.devices.get_solar_status()
        now = dt_util.utcnow().astimezone(timezone.utc)
        async with self.lock:
            owner = self._ownership()
            if owner is not None:
                block_id = owner.get("block_id")
                self._attempted(block_id)
                if not self._owner_identity_matches(owner, status):
                    self._cancel_for_external_change(
                        owner,
                        status,
                        "Explicit resume found a different inverter identity "
                        "or Pmax.",
                        now,
                    )
                elif self._limit_matches(status, owner.get("target")):
                    owner["phase"] = "owned"
                    owner.pop("restore_reason", None)
                    self.state["ownership"] = owner
                elif self._limit_matches(status, owner.get("original")):
                    self.state["ownership"] = None
                    self._suppress(block_id)
                else:
                    self._cancel_for_external_change(
                        owner,
                        status,
                        "Explicit resume found a manual/external inverter "
                        "limit.",
                        now,
                    )
            self.state["fault"] = None
            self.state["persistence_fault"] = None
            self.state["last_operation"] = {
                "operation": "resume",
                "phase": "reviewed",
                "success": True,
                "at": now.isoformat(),
            }
            await self._save()
        await self.async_evaluate(now)

    @property
    def attributes(self) -> dict:
        now = dt_util.utcnow().astimezone(timezone.utc)
        context = self.last_price
        status = self.last_status or {}
        target = self.last_target
        owner = self._ownership()
        block = context.block if context is not None else None
        qualification = self.state.get("qualification")
        if not isinstance(qualification, dict):
            qualification = {}
        started = _instant(qualification.get("started_at"))
        dwell = (
            max(0, int((now - started).total_seconds()))
            if started is not None
            else 0
        )
        eligible_since = _instant(self.state.get("eligible_since"))
        eligible_minutes = (
            max(0, int((now - eligible_since).total_seconds() // 60))
            if eligible_since is not None
            else 0
        )
        acquired = (
            _instant(owner.get("acquired_at"))
            if owner is not None
            else None
        )
        owned_minutes = (
            max(0, int((now - acquired).total_seconds() // 60))
            if acquired is not None
            else 0
        )
        active_power, active_age, active_fresh = (
            self._fresh_active_power(status, now)
            if status
            else (None, None, False)
        )
        export = context.export_earnings if context is not None else None
        cost_per_avoided = (
            -export if export is not None and export < 0 else None
        )
        decision = self.decision or SolarCurtailmentDecision(
            self.mode,
            "starting",
            "The solar curtailment runtime has not evaluated yet.",
        )
        original = owner.get("original") if owner is not None else None
        owned_target = owner.get("target") if owner is not None else None
        return {
            "mode": self.mode,
            "state": decision.state,
            "reason": decision.reason,
            "fault": self.state.get("fault"),
            "persistence_fault": self.state.get("persistence_fault"),
            "stopping": self.stopping,
            "spot_price_eur_kwh": (
                format(context.spot, "f")
                if context is not None and context.spot is not None
                else None
            ),
            "import_price_eur_kwh": (
                format(context.import_price, "f")
                if context is not None
                and context.import_price is not None
                else None
            ),
            "export_earnings_eur_kwh": (
                format(export, "f") if export is not None else None
            ),
            "price_valid": context.valid if context is not None else False,
            "price_error": context.reason if context is not None else None,
            "pricing_profile_signature": (
                context.profile_signature if context is not None else None
            ),
            "negative_block_id": block.identity if block is not None else None,
            "negative_block_start": (
                block.start.isoformat() if block is not None else None
            ),
            "negative_block_end": (
                block.end.isoformat() if block is not None else None
            ),
            "negative_block_end_known": (
                block.end_known if block is not None else False
            ),
            "negative_block_remaining_minutes": (
                round(block.remaining_minutes(now), 2)
                if block is not None
                else None
            ),
            "active_power_w": active_power,
            "active_power_age_seconds": (
                round(active_age, 2) if active_age is not None else None
            ),
            "active_power_fresh": active_fresh,
            "inverter_config_entry_id": status.get("config_entry_id"),
            "inverter_serial": status.get("serial"),
            "pmax_w": status.get("pmax_w"),
            "output_gate_w": SOLAR_CURTAILMENT_OUTPUT_GATE_W,
            "output_gate_passed": (
                active_fresh
                and active_power is not None
                and active_power >= SOLAR_CURTAILMENT_OUTPUT_GATE_W
            ),
            "configured_limit_mode": status.get("mode"),
            "configured_limit": status.get("configured"),
            "active_limit": status.get("active"),
            "limit_minimum": status.get("limit_minimum"),
            "limit_maximum": status.get("limit_maximum"),
            "limit_step": status.get("limit_step"),
            "native_write_enabled": status.get("write_enabled"),
            "native_write_permission_basis": status.get(
                "write_permission_basis"
            ),
            "target_cap_mode": (
                target.get("mode") if isinstance(target, dict) else None
            ),
            "target_cap": (
                target.get("value") if isinstance(target, dict) else None
            ),
            "target_cap_equivalent_w": (
                target.get("equivalent_w")
                if isinstance(target, dict)
                else None
            ),
            "qualification_sample_count": int(
                qualification.get("sample_count", 0)
            ),
            "qualification_dwell_seconds": dwell,
            "qualification_required_seconds": (
                SOLAR_CURTAILMENT_QUALIFICATION_SECONDS
            ),
            "ownership": owner is not None,
            "ownership_phase": (
                owner.get("phase") if owner is not None else None
            ),
            "ownership_identity": (
                owner.get("identity") if owner is not None else None
            ),
            "original_limit": original,
            "owned_target_cap": owned_target,
            "lease_expires": (
                owner.get("lease_expires") if owner is not None else None
            ),
            "last_operation_result": self.state.get("last_operation"),
            "external_override": self.state.get("external_override"),
            "commissioning_complete": self.commissioning_complete,
            "commissioning_gates": self.commissioning_gates,
            "commissioned_identity": self.state["commissioning"].get(
                "observed_identity"
            ),
            "available_modes": list(self.available_modes),
            "write_allowed": (
                self.mode == SOLAR_CURTAILMENT_MODE_ARMED
                and self.commissioning_complete
                and not self.state.get("fault")
            ),
            "shadow_zero_writes": (
                self.mode == SOLAR_CURTAILMENT_MODE_SHADOW
            ),
            "policy_revision": POLICY_REVISION,
            "maximum_lease_minutes": (
                SOLAR_CURTAILMENT_MAX_LEASE_MINUTES
            ),
            "minimum_block_remaining_minutes": (
                SOLAR_CURTAILMENT_MIN_REMAINING_MINUTES
            ),
            "entry_threshold_eur_kwh": (
                SOLAR_CURTAILMENT_ENTRY_EUR_KWH
            ),
            "cost_per_avoided_exported_kwh_eur": (
                format(cost_per_avoided, "f")
                if cost_per_avoided is not None
                else None
            ),
            "eligible_minutes": eligible_minutes,
            "owned_minutes": owned_minutes,
            "value_basis": (
                "Exact current export price and elapsed eligibility/ownership "
                "time only. No signed grid-power measurement is available."
            ),
            "measured_grid_export_available": False,
        }
