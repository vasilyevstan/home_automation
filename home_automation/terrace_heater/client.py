"""Identity-bound, local-only Tuya heater readings and explicit controls."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shlex
import stat
import threading
import time
from typing import Callable, Dict, List, Literal, Optional, Tuple, Union


DISCOVERY_SECONDS = 20
SOCKET_TIMEOUT = 4
READBACK_TIMEOUT_SECONDS = 3
READBACK_INTERVAL_SECONDS = 0.2
PROTOCOLS = ("3.3", "3.4", "3.5")
FIELD_CODES = {
    "power": ("switch", "Boolean", True),
    "temperature": ("temp_current", "Integer", False),
    "setpoint": ("temp_set", "Integer", True),
    "mode": ("mode", "Enum", True),
    "power_level": ("level", "Enum", True),
}
Value = Union[bool, int, float, str]


class HeaterError(Exception):
    """An expected failure whose message contains no credentials or raw payload."""


class _ConnectionError(HeaterError):
    """A read-only connection failure that permits bounded rediscovery."""


@dataclass(frozen=True)
class Credentials:
    device_id: str
    local_key: str = field(repr=False)


@dataclass(frozen=True)
class DataPoint:
    dp: str
    kind: str
    writable: bool
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    step: int = 1
    scale: int = 0
    values: Tuple[str, ...] = ()


@dataclass(frozen=True)
class HeaterProfile:
    device_id: str
    name: str
    product_id: str
    protocol_version: str
    address_hint: Optional[str]
    verified_at: str
    standby_settings_verified: bool
    fields: Dict[str, DataPoint]


@dataclass(frozen=True)
class HeaterCapabilities:
    minimum_temperature_c: Optional[float]
    maximum_temperature_c: Optional[float]
    temperature_step_c: Optional[float]
    modes: List[str]
    power_levels: List[str]
    power_level_unit: str
    settings_in_standby_verified: bool
    source: str
    verified_at: str


@dataclass(frozen=True)
class HeaterStatus:
    device_id: str
    name: str
    source: str
    ip: str
    retrieved_at: str
    enabled: bool
    temperature_c: Optional[float]
    temperature_status: Literal["available", "unavailable"]
    temperature_measured_at: Optional[str]
    temperature_freshness: str
    target_temperature_c: Optional[float]
    mode: Optional[str]
    power_level: Optional[str]
    heating_active: Optional[bool]
    capabilities: HeaterCapabilities
    warnings: List[str]


@dataclass(frozen=True)
class HeaterControlResult:
    field: str
    requested_value: Value
    command_sent: bool
    acknowledged: Optional[bool]
    verified: bool
    state: HeaterStatus


def validate_credentials(device_id: object, local_key: object) -> Credentials:
    if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9]{10,64}", device_id):
        raise HeaterError("Set a valid TERRACE_HEATER_DEVICE_ID from verified private provisioning.")
    if (
        not isinstance(local_key, str) or len(local_key) != 16 or not local_key.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in local_key)
    ):
        raise HeaterError("Set a valid TERRACE_HEATER_LOCAL_KEY from verified private provisioning.")
    return Credentials(device_id, local_key)


def load_credentials() -> Credentials:
    names = ("TERRACE_HEATER_DEVICE_ID", "TERRACE_HEATER_LOCAL_KEY")
    if not all(os.environ.get(name, "").strip() for name in names):
        raise HeaterError("Set both TERRACE_HEATER_DEVICE_ID and TERRACE_HEATER_LOCAL_KEY in the process environment.")
    return validate_credentials(os.environ[names[0]], os.environ[names[1]])


def private_text(path: Path) -> str:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        )):
            raise HeaterError("Private heater files must be regular, user-owned files with mode 600.")
        if info.st_size > 256 * 1024:
            raise HeaterError("Private heater configuration exceeds the supported size.")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise HeaterError("Could not read private heater configuration; check its path and permissions.") from None


def load_environment_file(path: Path) -> Dict[str, str]:
    path = path.expanduser()
    if not path.is_absolute():
        raise HeaterError("The private environment file must use an absolute path.")
    expected = {"TERRACE_HEATER_DEVICE_ID", "TERRACE_HEATER_LOCAL_KEY", "TERRACE_HEATER_CONFIG"}
    environment = {}
    try:
        for line in private_text(path).splitlines():
            words = shlex.split(line, comments=True)
            if not words:
                continue
            if len(words) != 2 or words[0] != "export" or "=" not in words[1]:
                raise HeaterError("Unexpected private environment format; no shell content was executed.")
            name, value = words[1].split("=", 1)
            if name not in expected or name in environment:
                raise HeaterError("Unexpected or duplicate private environment field.")
            environment[name] = value
    except ValueError:
        raise HeaterError("The private environment has invalid quoting; no shell content was executed.") from None
    if set(environment) != expected:
        raise HeaterError("The private heater environment is incomplete.")
    validate_credentials(environment["TERRACE_HEATER_DEVICE_ID"], environment["TERRACE_HEATER_LOCAL_KEY"])
    if not environment["TERRACE_HEATER_CONFIG"].strip() or not Path(environment["TERRACE_HEATER_CONFIG"]).expanduser().is_absolute():
        raise HeaterError("The private environment must specify an absolute verified profile path.")
    return environment


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HeaterError("Duplicate configuration or metadata field.")
        result[key] = value
    return result


def parse_json(text: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=_unique_json)
    except (ValueError, TypeError):
        raise HeaterError("Invalid heater configuration or metadata JSON.") from None


def _address(value: object) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.IPv4Address(value)
        allowed = any(address in subnet for subnet in (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        ))
        if not allowed:
            raise ValueError
    except ValueError:
        raise HeaterError("Heater addresses must be private LAN IPv4 addresses.") from None
    return str(address)


def parse_profile(value: object, device_id: str) -> HeaterProfile:
    required = {
        "schema_version", "device_id", "name", "product_id", "protocol_version",
        "address_hint", "verified_at", "standby_settings_verified", "fields",
    }
    if not isinstance(value, dict) or set(value) != required or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise HeaterError("Invalid or unsupported heater profile schema.")
    if value["device_id"] != device_id:
        raise HeaterError("The heater profile does not match the configured Device ID.")
    if not all(isinstance(value[name], str) and value[name].strip() for name in ("name", "product_id", "verified_at")):
        raise HeaterError("The heater profile is missing verified identity or provenance.")
    if value["protocol_version"] not in PROTOCOLS or type(value["standby_settings_verified"]) is not bool:
        raise HeaterError("Unsupported heater protocol or invalid standby control policy.")
    try:
        verified = datetime.fromisoformat(value["verified_at"])
        if verified.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise HeaterError("Heater profile verification time must include a UTC offset.") from None
    address = None if value["address_hint"] is None else _address(value["address_hint"])
    fields = value["fields"]
    if not isinstance(fields, dict) or "power" not in fields or set(fields) - set(FIELD_CODES):
        raise HeaterError("The heater profile has invalid or missing supported data-point mappings.")
    parsed = {}
    used = set()
    for name, item in fields.items():
        _, kind, writable = FIELD_CODES[name]
        if not isinstance(item, dict):
            raise HeaterError("Invalid heater data-point definition.")
        expected = {"dp", "kind", "writable"}
        expected |= {"minimum", "maximum", "step", "scale", "unit"} if kind == "Integer" else set()
        expected |= {"values"} if kind == "Enum" else set()
        dp = item.get("dp")
        if (
            set(item) != expected or not isinstance(dp, str)
            or not re.fullmatch(r"[1-9][0-9]{0,2}", dp) or not 1 <= int(dp) <= 255
            or dp in used or item.get("kind") != kind or item.get("writable") is not writable
        ):
            raise HeaterError("Conflicting, invalid, or unsupported heater data-point mapping.")
        used.add(dp)
        if kind == "Integer":
            if (
                not all(type(item[key]) is int for key in ("minimum", "maximum", "step", "scale"))
                or not -100000 <= item["minimum"] <= item["maximum"] <= 100000
                or item["step"] <= 0 or item["step"] > item["maximum"] - item["minimum"]
                or not 0 <= item["scale"] <= 3 or item["unit"] != "C"
            ):
                raise HeaterError("Invalid heater temperature units, range, step, or scaling.")
            if name == "setpoint" and (
                item["minimum"] / 10 ** item["scale"] < 5
                or item["maximum"] / 10 ** item["scale"] > 40
            ):
                raise HeaterError("Configured setpoint bounds exceed the documented 5-40 C product limits.")
            parsed[name] = DataPoint(dp, kind, writable, item["minimum"], item["maximum"], item["step"], item["scale"])
        elif kind == "Enum":
            options = item["values"]
            if (
                not isinstance(options, list) or not options or len(options) > 32
                or any(not isinstance(option, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", option) for option in options)
                or len(set(options)) != len(options)
            ):
                raise HeaterError("Invalid heater mode or power-level options.")
            parsed[name] = DataPoint(dp, kind, writable, values=tuple(options))
        else:
            parsed[name] = DataPoint(dp, kind, writable)
    return HeaterProfile(
        device_id, value["name"], value["product_id"], value["protocol_version"],
        address, value["verified_at"], value["standby_settings_verified"], parsed,
    )


def load_profile(device_id: str) -> HeaterProfile:
    value = os.environ.get("TERRACE_HEATER_CONFIG", "")
    if not value.strip():
        raise HeaterError("Set TERRACE_HEATER_CONFIG to the absolute private verified device.json path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HeaterError("TERRACE_HEATER_CONFIG must be an absolute path.")
    return parse_profile(parse_json(private_text(path)), device_id)


def _sdk():
    try:
        import tinytuya
    except ImportError:
        raise HeaterError("Install the local heater client with: uv sync --locked --extra heater") from None
    # TinyTuya's diagnostics can include encryption keys, including at ERROR level.
    logger = logging.getLogger("tinytuya")
    logger.propagate = False
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    tinytuya.log.disabled = True
    return tinytuya


def discover_device(device_id: str, product_id: str) -> Tuple[str, str]:
    _sdk()
    from tinytuya import scanner
    try:
        devices = scanner.devices(
            verbose=False, scantime=DISCOVERY_SECONDS, color=False, poll=False,
            forcescan=False, show_timer=False,
            tuyadevices=[{"id": device_id, "name": "", "key": ""}],
        )
    except (OSError, ValueError, RuntimeError):
        raise HeaterError("Tuya LAN discovery failed; check local networking and UDP discovery permissions.") from None
    if not isinstance(devices, dict):
        raise HeaterError("Tuya discovery returned an invalid response.")
    matches = [
        (address, row) for address, row in devices.items()
        if isinstance(row, dict) and (row.get("gwId") or row.get("id")) == device_id
    ]
    if len(matches) != 1:
        raise HeaterError("LAN discovery did not find one unambiguous address for the configured heater; do not assume it is off.")
    address, device = matches[0]
    if device.get("productKey") != product_id:
        raise HeaterError("Discovered heater product identity does not match private provisioning.")
    protocol = str(device.get("version"))
    if protocol not in PROTOCOLS:
        raise HeaterError("Discovered heater protocol is not supported for authenticated local control.")
    return _address(address), protocol


def _device_factory(profile: HeaterProfile, credentials: Credentials, address: str):
    sdk = _sdk()
    try:
        device = sdk.Device(
            credentials.device_id, address=address, local_key=credentials.local_key,
            version=float(profile.protocol_version), connection_timeout=SOCKET_TIMEOUT,
            connection_retry_limit=1, connection_retry_delay=0, persist=True,
        )
    except (OSError, ValueError, TypeError):
        raise HeaterError("Could not initialize the local heater connection; check verified provisioning.") from None
    device.set_retry(False)
    return device


def _raw_status(device, device_id: str) -> dict:
    try:
        result = device.status()
    except (OSError, ValueError, TypeError, UnicodeError):
        raise _ConnectionError("Heater status could not be authenticated or read; check the device, local key, and LAN.") from None
    if (
        not isinstance(result, dict) or "Error" in result or "Err" in result
        or not isinstance(result.get("dps"), dict)
    ):
        raise _ConnectionError("Heater status could not be authenticated or read; check the device, local key, and LAN.")
    if any(result.get(name, device_id) != device_id for name in ("devId", "gwId")):
        raise _ConnectionError("Authenticated reply identifies a different heater; no control command was sent.")
    return result["dps"]


def _decode(point: DataPoint, raw: object) -> Value:
    if point.kind == "Boolean":
        if type(raw) is not bool:
            raise HeaterError("Heater returned an invalid power state.")
        return raw
    if point.kind == "Enum":
        if not isinstance(raw, str) or raw not in point.values:
            raise HeaterError("Heater reported an unverified mode or power level; update verified provisioning before control.")
        return raw
    if type(raw) is not int or not point.minimum <= raw <= point.maximum:
        raise HeaterError("Heater returned an invalid temperature reading or setpoint.")
    return raw / 10 ** point.scale


def build_status(profile: HeaterProfile, dps: dict, address: str) -> HeaterStatus:
    values = {}
    for name, point in profile.fields.items():
        values[name] = _decode(point, dps[point.dp]) if dps.get(point.dp) is not None else None
    if type(values["power"]) is not bool:
        raise HeaterError("Heater power state is unavailable; do not assume it is off.")
    enabled = values["power"]
    temperature = values.get("temperature")
    target = profile.fields.get("setpoint")
    warnings = [
        "Temperature is reported by the heater, not an independently calibrated room sensor.",
        "Retrieval time is not measurement time; the device does not report sensor freshness.",
        "Power levels are native device labels, not verified percentages or measured watts.",
    ]
    if not enabled:
        warnings.append("The heater is off; a returned temperature may be retained from earlier operation.")
    for name in ("temperature", "setpoint", "mode", "power_level"):
        if values.get(name) is None:
            warnings.append("{} is unavailable or unsupported; missing values are not zero.".format(name))
    if not profile.standby_settings_verified:
        warnings.append("Settings changes while off are not verified and are disabled; reads never enable heating.")
    return HeaterStatus(
        device_id=profile.device_id, name=profile.name, source="tuya_lan",
        ip=address, retrieved_at=datetime.now(timezone.utc).isoformat(),
        enabled=enabled, temperature_c=temperature,
        temperature_status="available" if temperature is not None else "unavailable",
        temperature_measured_at=None, temperature_freshness="not_reported",
        target_temperature_c=values.get("setpoint"), mode=values.get("mode"),
        power_level=values.get("power_level"), heating_active=None,
        capabilities=HeaterCapabilities(
            target.minimum / 10 ** target.scale if target else None,
            target.maximum / 10 ** target.scale if target else None,
            target.step / 10 ** target.scale if target else None,
            list(profile.fields["mode"].values) if "mode" in profile.fields else [],
            list(profile.fields["power_level"].values) if "power_level" in profile.fields else [],
            "native_level", profile.standby_settings_verified,
            "tuya_account_schema_and_authenticated_lan", profile.verified_at,
        ),
        warnings=warnings,
    )


def _encode(point: DataPoint, value: object) -> Union[bool, int, str]:
    if not point.writable:
        raise HeaterError("This heater data point is read-only.")
    if point.kind == "Boolean":
        if type(value) is not bool:
            raise HeaterError("Power must be an explicit boolean, never a toggle.")
        return value
    if point.kind == "Enum":
        if not isinstance(value, str) or value not in point.values:
            raise HeaterError("Choose one of the verified native options: {}.".format(", ".join(point.values)))
        return value
    if type(value) not in (int, float):
        raise HeaterError("Temperature must be a finite number in degrees Celsius.")
    try:
        number = Decimal(str(value))
        raw = number * (10 ** point.scale)
        if (
            not number.is_finite() or raw != raw.to_integral_value()
            or not point.minimum <= raw <= point.maximum
            or (raw - point.minimum) % point.step
        ):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise HeaterError("Temperature must respect the verified minimum, maximum, and step; values are not rounded or clamped.") from None
    return int(raw)


class HeaterClient:
    def __init__(
        self, credentials_provider: Callable = load_credentials,
        profile_provider: Callable = load_profile, discoverer: Callable = discover_device,
        device_factory: Callable = _device_factory,
    ):
        self._credentials = credentials_provider
        self._profile = profile_provider
        self._discover = discoverer
        self._factory = device_factory
        self._addresses = {}
        self._lock = threading.Lock()

    def _open(self, profile: HeaterProfile, credentials: Credentials):
        address = self._addresses.get(profile.device_id) or profile.address_hint
        for discovered in (False, True):
            if discovered or address is None:
                address, version = self._discover(profile.device_id, profile.product_id)
                if version != profile.protocol_version:
                    raise HeaterError("Heater protocol changed; verify provisioning again before control.")
                discovered = True
            device = self._factory(profile, credentials, _address(address))
            try:
                dps = _raw_status(device, credentials.device_id)
                state = build_status(profile, dps, address)
            except _ConnectionError:
                device.close()
                self._addresses.pop(profile.device_id, None)
                if discovered:
                    raise
                continue
            except HeaterError:
                device.close()
                raise
            self._addresses[profile.device_id] = address
            return device, dps, state
        raise HeaterError("The configured heater is unavailable; no control command was sent.")

    def get_status(self) -> HeaterStatus:
        with self._lock:
            credentials = self._credentials()
            profile = self._profile(credentials.device_id)
            device, _, state = self._open(profile, credentials)
            device.close()
            return state

    def set_value(self, name: str, value: object) -> HeaterControlResult:
        with self._lock:
            credentials = self._credentials()
            profile = self._profile(credentials.device_id)
            point = profile.fields.get(name)
            if point is None:
                raise HeaterError("The requested heater control is not supported by the verified profile.")
            requested = _encode(point, value)
            device, before, state = self._open(profile, credentials)
            try:
                if type(before.get(point.dp)) is type(requested) and before.get(point.dp) == requested:
                    return HeaterControlResult(name, value, False, None, True, state)
                if name != "power" and not state.enabled and not profile.standby_settings_verified:
                    raise HeaterError("Changing settings while off is not verified for this heater. No command was sent; enable heating only with an explicit user request.")
                if device.socket is None:
                    raise HeaterError("The authenticated heater connection closed before control; no command was sent.")
                # A zero limit on this already-open socket prevents TinyTuya replaying a write.
                device.set_socketRetryLimit(0)
                try:
                    response = device.set_value(point.dp, requested)
                    acknowledged = (
                        "Error" not in response and "Err" not in response
                        if isinstance(response, dict) else None
                    )
                except (OSError, ValueError, TypeError, UnicodeError):
                    acknowledged = False
                finally:
                    device.set_socketRetryLimit(1)
                deadline = time.monotonic() + READBACK_TIMEOUT_SECONDS
                while True:
                    # Discard queued pre-command reports; MCU state can also settle asynchronously.
                    device.close()
                    try:
                        after = _raw_status(device, credentials.device_id)
                        verified = build_status(profile, after, state.ip)
                    except HeaterError:
                        raise HeaterError("A heater command was attempted but its outcome is unconfirmed. It may have taken effect; check the device before retrying.") from None
                    if name != "power" and verified.enabled != state.enabled:
                        raise HeaterError("A settings command unexpectedly changed heater power. Check the physical device immediately; no automatic follow-up command was sent.")
                    if type(after.get(point.dp)) is type(requested) and after.get(point.dp) == requested:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise HeaterError("The heater did not report the requested setting within the read-back window. The command may have taken effect temporarily; do not assume the requested state.")
                    time.sleep(min(READBACK_INTERVAL_SECONDS, remaining))
                if acknowledged is not True:
                    verified.warnings.append("The write acknowledgement was missing or unsuccessful; a separate read-back observed the requested state.")
                return HeaterControlResult(name, value, True, acknowledged, True, verified)
            finally:
                device.close()
