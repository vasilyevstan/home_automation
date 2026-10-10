"""Local Starlink telemetry and explicitly enabled, single-attempt controls."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import ipaddress
from math import isfinite
import os
import re
import threading
import time
from typing import Callable, Dict, List, Literal, Optional, Union


SOURCE = "starlink_local_grpc"
TARGET = "192.168.100.1:9200"
RPC_TIMEOUT = 5
READBACK_SECONDS = 15
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SAMPLES = 900
_SERVICE = "SpaceX.API.Device.Device"
_ACTIONS = {
    "reboot": "reboot",
    "sleep": "dish_power_save",
    "snow_melt": "dish_set_config",
}
_RESPONSES = {
    "get_status": "dish_get_status",
    "get_history": "dish_get_history",
    "dish_get_config": "dish_get_config",
    "dish_get_obstruction_map": "dish_get_obstruction_map",
    "reboot": "reboot",
    "dish_power_save": "dish_power_save",
    "dish_set_config": "dish_set_config",
}
_ID = re.compile(r"ut[0-9a-fA-F]{8}-[0-9a-fA-F]{8}-[0-9a-fA-F]{8}\Z")
Availability = Literal["available", "unsupported", "unavailable"]
Scalar = Union[str, bool, int, float]


class StarlinkError(Exception):
    """An expected failure safe to show without raw device/debug data."""


class RpcFailure(StarlinkError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Starlink RPC failed ({code}).")


class Unsupported(StarlinkError):
    """This firmware does not expose the requested operation."""


@dataclass(frozen=True)
class Settings:
    target: str = TARGET
    expected_device_id: Optional[str] = None
    allowed_actions: frozenset[str] = frozenset()


def load_settings() -> Settings:
    target = os.environ.get("STARLINK_TARGET", TARGET)
    try:
        host, port = target.split(":")
        address = ipaddress.IPv4Address(host)
        local = any(address in ipaddress.IPv4Network(network) for network in (
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        ))
        if not local or not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError
    except (ValueError, ipaddress.AddressValueError):
        raise StarlinkError("STARLINK_TARGET must be a private LAN IPv4 address and port.") from None
    device_id = os.environ.get("STARLINK_EXPECTED_DEVICE_ID")
    if device_id is not None and _ID.fullmatch(device_id) is None:
        raise StarlinkError("STARLINK_EXPECTED_DEVICE_ID must be an exact Starlink terminal ID.")
    raw_actions = os.environ.get("STARLINK_ALLOWED_ACTIONS", "")
    actions = frozenset(part.strip() for part in raw_actions.split(",")) if raw_actions else frozenset()
    if not actions.issubset(_ACTIONS):
        raise StarlinkError("STARLINK_ALLOWED_ACTIONS accepts only reboot,sleep,snow_melt, or an empty value.")
    if actions and device_id is None:
        raise StarlinkError("Enabling controls requires STARLINK_EXPECTED_DEVICE_ID.")
    return Settings(f"{address}:{int(port)}", device_id, actions)


def ensure_runtime() -> None:
    try:
        importlib.import_module("grpc")
        importlib.import_module("grpc_reflection.v1alpha.reflection_pb2")
        importlib.import_module("google.protobuf.message_factory")
    except ModuleNotFoundError:
        raise StarlinkError("Install the Starlink runtime with 'uv sync --locked --extra starlink'.") from None


class Connection(AbstractContextManager):
    """A fresh channel and reflected schema per operation; never retry a write."""

    def __init__(self, target: str):
        self.target = target
        self.channel = None

    def __enter__(self):
        ensure_runtime()
        import grpc
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
        from google.protobuf.message import DecodeError
        from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

        self.channel = grpc.insecure_channel(self.target, options=[
            ("grpc.enable_retries", 0),
            ("grpc.max_receive_message_length", MAX_RESPONSE_BYTES),
        ])
        try:
            grpc.channel_ready_future(self.channel).result(timeout=RPC_TIMEOUT)
            reflection = reflection_pb2_grpc.ServerReflectionStub(self.channel)
            query = reflection_pb2.ServerReflectionRequest(file_containing_symbol=_SERVICE)
            files = {}
            size = 0
            for reply in reflection.ServerReflectionInfo(iter([query]), timeout=RPC_TIMEOUT):
                if reply.HasField("error_response"):
                    raise Unsupported("Starlink protocol reflection is unavailable.")
                for blob in reply.file_descriptor_response.file_descriptor_proto:
                    size += len(blob)
                    if size > MAX_RESPONSE_BYTES:
                        raise StarlinkError("Starlink protocol description is too large.")
                    descriptor = descriptor_pb2.FileDescriptorProto.FromString(blob)
                    if descriptor.name in files and files[descriptor.name] != descriptor:
                        raise StarlinkError("Conflicting Starlink protocol descriptions.")
                    files[descriptor.name] = descriptor
            pool = descriptor_pool.DescriptorPool()
            loaded = set()
            while files:
                ready = [name for name, file in files.items() if all(dep in loaded for dep in file.dependency)]
                if not ready:
                    raise StarlinkError("Starlink reflection returned incomplete protocol dependencies.")
                for name in ready:
                    pool.Add(files.pop(name))
                    loaded.add(name)
            method = pool.FindServiceByName(_SERVICE).methods_by_name["Handle"]
            self.request_class = message_factory.GetMessageClass(method.input_type)
            response_class = message_factory.GetMessageClass(method.output_type)
            self.request_descriptor = method.input_type
            self.response_descriptor = method.output_type
            self._handle = self.channel.unary_unary(
                f"/{_SERVICE}/Handle",
                request_serializer=self.request_class.SerializeToString,
                response_deserializer=response_class.FromString,
            )
            return self
        except grpc.FutureTimeoutError:
            self.close()
            raise StarlinkError("Starlink connection timed out. Check the LAN route to the dish.") from None
        except grpc.RpcError as error:
            self.close()
            raise RpcFailure(error.code().name) from None
        except (KeyError, TypeError, ValueError, DecodeError):
            self.close()
            raise StarlinkError("Starlink returned an incompatible protocol description.") from None
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.channel is not None:
            self.channel.close()

    def __exit__(self, *args):
        self.close()

    def request_field(self, name: str):
        return self.request_descriptor.fields_by_name.get(name)

    def call(self, name: str, values: Optional[dict] = None, *, timeout: float = RPC_TIMEOUT):
        import grpc
        from google.protobuf import json_format
        from google.protobuf.message import DecodeError

        if name not in _RESPONSES or self.request_field(name) is None:
            raise Unsupported(f"Starlink firmware does not expose {name}.")
        request = self.request_class()
        try:
            json_format.ParseDict({name: {} if values is None else values}, request)
        except (json_format.ParseError, ValueError, TypeError):
            raise Unsupported(f"Starlink firmware has an incompatible {name} schema.") from None
        try:
            response = self._handle(request, timeout=timeout)
        except grpc.RpcError as error:
            raise RpcFailure(error.code().name) from None
        except DecodeError:
            raise StarlinkError("Starlink returned an invalid protocol response.") from None
        status = _message(response, "status")
        if status is not None and _scalar(status, "code") not in (None, 0):
            raise RpcFailure(f"DEVICE_STATUS_{_scalar(status, 'code')}")
        expected = _RESPONSES[name]
        result = _message(response, expected)
        if result is not None:
            return result
        # Some firmware acknowledges controls with only the response envelope.
        if name in _ACTIONS.values() and not any(
            f.containing_oneof is not None for f, _ in response.ListFields()
        ):
            return None
        raise StarlinkError(f"Starlink response did not contain {expected}.")


def _field(message, name):
    return message.DESCRIPTOR.fields_by_name.get(name) if message is not None else None


def _message(message, name):
    descriptor = _field(message, name)
    if descriptor is None or descriptor.message_type is None or descriptor.is_repeated:
        return None
    return getattr(message, name) if message.HasField(name) else None


def _scalar(message, name):
    descriptor = _field(message, name)
    if descriptor is None or descriptor.message_type is not None or descriptor.is_repeated:
        return None
    if descriptor.has_presence and not message.HasField(name):
        return None
    return getattr(message, name)


def _enum(message, name) -> Optional[str]:
    descriptor = _field(message, name)
    value = _scalar(message, name)
    if descriptor is None or descriptor.enum_type is None or value is None:
        return None
    member = descriptor.enum_type.values_by_number.get(value)
    return member.name if member else f"UNKNOWN_{value}"


def _text(message, name) -> Optional[str]:
    value = _scalar(message, name)
    return value if isinstance(value, str) and value else None


def _bool(message, name) -> Optional[bool]:
    value = _scalar(message, name)
    return value if type(value) is bool else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Reading:
    value: Optional[float]
    unit: str
    status: Availability
    reason: Optional[str] = None


def _reading(value, unit, *, supported=True, maximum=None) -> Reading:
    if not supported:
        return Reading(None, unit, "unsupported", "Field absent from this firmware's schema.")
    if type(value) not in (int, float) or not isfinite(value) or value < 0 or (
        maximum is not None and value > maximum
    ):
        return Reading(None, unit, "unavailable", "No valid device measurement.")
    return Reading(float(value), unit, "available")


def _metric(message, name, unit, *, maximum=None) -> Reading:
    if message is None:
        return Reading(None, unit, "unavailable", "The device did not return this group of measurements.")
    return _reading(_scalar(message, name), unit, supported=_field(message, name) is not None, maximum=maximum)


@dataclass(frozen=True)
class Throughput:
    bps: Optional[float]
    mbps: Optional[float]
    status: Availability
    reason: Optional[str] = None


def _throughput(reading: Reading) -> Throughput:
    return Throughput(
        reading.value, None if reading.value is None else reading.value / 1_000_000,
        reading.status, reading.reason,
    )


@dataclass(frozen=True)
class Identity:
    device_id: str
    hardware_version: Optional[str]
    software_version: Optional[str]


@dataclass(frozen=True)
class StarlinkStatus:
    source: str
    retrieved_at: str
    device: Identity
    measurement_time: Optional[str]
    connection_state: str
    disablement_reason: Optional[str]
    uptime_seconds: Reading
    boot_count: Optional[int]
    download: Throughput
    upload: Throughput
    latency: Reading
    packet_loss: Reading
    ethernet_speed: Reading
    power: Reading
    heating: Optional[bool]
    has_actuators: Optional[bool]
    active_alerts: List[str]
    alerts_available: bool
    warnings: List[str]


def _identity(raw) -> Identity:
    info = _message(raw, "device_info")
    device_id = _text(info, "id")
    if device_id is None or _ID.fullmatch(device_id) is None:
        raise StarlinkError("Starlink did not return a valid terminal identity.")
    return Identity(device_id, _text(info, "hardware_version"), _text(info, "software_version"))


def status_data(raw, retrieved_at: str) -> StarlinkStatus:
    device = _identity(raw)
    alerts = _message(raw, "alerts")
    active_alerts = [] if alerts is None else [
        f.name for f in alerts.DESCRIPTOR.fields if _bool(alerts, f.name) is True
    ]
    outage = _message(raw, "outage")
    disablement = _enum(raw, "disablement_code")
    if outage is not None:
        state = _enum(outage, "cause") or "unknown"
    elif disablement not in (None, "UNKNOWN", "OKAY"):
        state = "disabled"
    elif _field(raw, "outage") is not None:
        state = "connected"
    else:
        state = "unknown"
    has_actuators = _enum(raw, "has_actuators")
    warnings = [
        "Throughput is aggregate traffic through the dish, not a speed test or available bandwidth.",
        "Retrieval time is not the device's measurement time.",
    ]
    if alerts is None:
        warnings.append("Device alerts are unavailable.")
    if "dbf_telem_stale" in active_alerts:
        warnings.append("The dish reports stale telemetry; readings may not represent current conditions.")
    boot_count = _scalar(_message(raw, "device_info"), "bootcount")
    loss = _metric(raw, "pop_ping_drop_rate", "ratio", maximum=1)
    latency = _metric(raw, "pop_ping_latency_ms", "ms")
    if loss.value == 1:
        latency = Reading(None, "ms", "unavailable", "No successful ping in the reported interval.")
    return StarlinkStatus(
        SOURCE, retrieved_at, device, None, state, disablement,
        _metric(_message(raw, "device_state"), "uptime_s", "s"),
        boot_count if type(boot_count) is int and boot_count >= 0 else None,
        _throughput(_metric(raw, "downlink_throughput_bps", "bps")),
        _throughput(_metric(raw, "uplink_throughput_bps", "bps")),
        latency, loss,
        _metric(raw, "eth_speed_mbps", "Mbps"),
        Reading(None, "W", "unavailable", "Use recent history for reported power samples, not instantaneous power."),
        _bool(alerts, "is_heating"),
        {"HAS_ACTUATORS_YES": True, "HAS_ACTUATORS_NO": False}.get(has_actuators),
        active_alerts, alerts is not None, warnings,
    )


@dataclass(frozen=True)
class Configuration:
    snow_melt_mode: Optional[str]
    sleep_enabled: Optional[bool]
    sleep_start_minutes_utc: Optional[int]
    sleep_duration_minutes: Optional[int]
    warnings: List[str]


def config_data(raw) -> Configuration:
    config = _message(raw, "dish_config")
    if config is None:
        raise StarlinkError("Starlink response did not contain dish configuration.")

    def setting(name):
        if _bool(config, f"apply_{name}") is False:
            return None
        return _scalar(config, name)

    mode = None if setting("snow_melt_mode") is None else _enum(config, "snow_melt_mode")
    mode = {"AUTO": "auto", "ALWAYS_ON": "always_on", "ALWAYS_OFF": "always_off"}.get(mode)
    enabled = setting("power_save_mode")
    start, duration = setting("power_save_start_minutes"), setting("power_save_duration_minutes")
    enabled = enabled if type(enabled) is bool else None
    start = start if type(start) is int and 0 <= start < 1440 else None
    duration = duration if type(duration) is int and 0 <= duration <= 1440 else None
    warnings = ["Sleep start is minutes after UTC midnight; it does not follow local daylight-saving changes."]
    if mode is None or enabled is None or start is None or duration is None:
        warnings.append("Some settings are unavailable, unsupported, or use unknown enum values.")
    return Configuration(mode, enabled, start, duration, warnings)


@dataclass(frozen=True)
class ControlCapability:
    action: str
    schema_present: bool
    locally_enabled: bool
    execution_support: str
    reason: str


@dataclass(frozen=True)
class Capabilities:
    source: str
    retrieved_at: str
    device: Identity
    configuration: Optional[Configuration]
    read_requests: Dict[str, bool]
    controls: List[ControlCapability]
    warnings: List[str]


@dataclass(frozen=True)
class Summary:
    unit: str
    status: Availability
    valid_samples: int
    missing_samples: int
    average: Optional[float]
    minimum: Optional[float]
    peak: Optional[float]


@dataclass(frozen=True)
class ThroughputSummary:
    bps: Summary
    mbps: Summary


def _summary(readings: List[Reading], unit: str, *, supported: bool, scale=1.0) -> Summary:
    values = [r.value / scale for r in readings if r.value is not None]
    return Summary(
        unit, ("available" if values else "unavailable") if supported else "unsupported",
        len(values), len(readings) - len(values),
        sum(values) / len(values) if values else None,
        min(values) if values else None, max(values) if values else None,
    )


@dataclass(frozen=True)
class HistorySample:
    counter: int
    seconds_before_latest: int
    download: Throughput
    upload: Throughput
    latency: Reading
    packet_loss: Reading
    power: Reading


@dataclass(frozen=True)
class Outage:
    cause: str
    start_timestamp_ns: Optional[int]
    duration_seconds: Reading


@dataclass(frozen=True)
class History:
    source: str
    retrieved_at: str
    device: Identity
    requested_samples: int
    returned_samples: int
    available_samples: int
    sample_period_seconds: int
    counter_end: int
    complete: bool
    download: ThroughputSummary
    upload: ThroughputSummary
    latency: Summary
    packet_loss: Summary
    power: Summary
    samples: Optional[List[HistorySample]]
    retained_outages: List[Outage]
    warnings: List[str]


def history_data(raw, device: Identity, retrieved_at: str, samples: int, include_samples: bool) -> History:
    current = _scalar(raw, "current")
    ping_field = _field(raw, "pop_ping_drop_rate")
    if type(current) is not int or current < 0 or ping_field is None or not ping_field.is_repeated:
        raise StarlinkError("Starlink history is missing its sample counter or primary ring buffer.")
    capacity = len(raw.pop_ping_drop_rate)
    if capacity > 100_000:
        raise StarlinkError("Starlink history exceeds the supported buffer size.")
    available = min(current, capacity)
    count = min(samples, available)
    warnings = [
        "History is the retained device buffer, not a long-term archive; counters reset on reboot.",
        "Samples are nominally one second apart. Relative offsets are not authoritative wall-clock timestamps.",
        "Throughput measures aggregate dish traffic, not connection capacity or per-client traffic.",
        "Retained outage records are reported separately and may extend outside the selected sample window.",
    ]
    fields = (
        ("downlink_throughput_bps", "bps", None),
        ("uplink_throughput_bps", "bps", None),
        ("pop_ping_latency_ms", "ms", None),
        ("pop_ping_drop_rate", "ratio", 1),
        ("power_in", "W", None),
    )
    columns = {}
    supported_fields = {}
    for name, unit, maximum in fields:
        descriptor = _field(raw, name)
        supported = descriptor is not None and descriptor.is_repeated
        supported_fields[name] = supported
        data = getattr(raw, name) if supported else []
        aligned = len(data) == capacity
        if supported and not aligned:
            warnings.append(f"{name} has a different buffer length; its sample alignment is unknown.")
        columns[name] = [
            _reading(data[index % capacity] if aligned else None, unit, supported=supported, maximum=maximum)
            for index in range(current - count, current)
        ]
    down, up, latency, loss, power = [columns[name] for name, _, _ in fields]
    for index, lost in enumerate(loss):
        if lost.value is None or lost.value == 1:
            latency[index] = Reading(None, "ms", "unavailable", "No successful ping for this sample.")
    ordered = [
        HistorySample(
            current - count + index + 1, count - index - 1,
            _throughput(down[index]), _throughput(up[index]), latency[index], loss[index], power[index],
        )
        for index in range(count)
    ] if include_samples else None
    outages = []
    if _field(raw, "outages") is not None:
        for outage in raw.outages:
            duration = _metric(outage, "duration_ns", "ns")
            start = _scalar(outage, "start_timestamp_ns")
            outages.append(Outage(
                _enum(outage, "cause") or "unknown",
                start if type(start) is int and start > 0 else None,
                Reading(None if duration.value is None else duration.value / 1_000_000_000,
                        "s", duration.status, duration.reason),
            ))
    if count < samples:
        warnings.append("The device retained fewer samples than requested; no samples were padded.")
    if any(r.value is None for data in columns.values() for r in data):
        warnings.append("Some metrics are unavailable; summaries use only their valid samples.")

    def summary(name, unit, scale=1.0):
        return _summary(columns[name], unit, supported=supported_fields[name], scale=scale)

    return History(
        SOURCE, retrieved_at, device, samples, count, available, 1, current,
        count == samples and all(r.value is not None for data in columns.values() for r in data),
        ThroughputSummary(summary(fields[0][0], "bps"), summary(fields[0][0], "Mbps", 1_000_000)),
        ThroughputSummary(summary(fields[1][0], "bps"), summary(fields[1][0], "Mbps", 1_000_000)),
        summary(fields[2][0], "ms"), summary(fields[3][0], "ratio"), summary(fields[4][0], "W"),
        ordered, outages, warnings,
    )


@dataclass(frozen=True)
class Obstructions:
    source: str
    retrieved_at: str
    device: Identity
    currently_obstructed: Optional[bool]
    fraction_obstructed: Reading
    collection_seconds: Reading
    average_prolonged_duration: Reading
    average_prolonged_interval: Reading
    map_rows: Optional[int]
    map_columns: Optional[int]
    map_reference_frame: Optional[str]
    warnings: List[str]


@dataclass(frozen=True)
class ControlResult:
    source: str
    retrieved_at: str
    device: Identity
    action: str
    requested: Dict[str, Scalar]
    before: Dict[str, Optional[Scalar]]
    observed: Dict[str, Optional[Scalar]]
    command_sent: bool
    acknowledged: Optional[bool]
    verified: bool
    warnings: List[str]


def _control_schema(connection, action: str) -> bool:
    descriptor = connection.request_field(_ACTIONS[action])
    if descriptor is None or descriptor.message_type is None:
        return False
    fields = descriptor.message_type.fields_by_name
    if action == "sleep":
        return all(name in fields for name in (
            "power_save_start_minutes", "power_save_duration_minutes", "enable_power_save",
        ))
    if action == "snow_melt":
        config = fields.get("dish_config")
        if config is None or config.message_type is None:
            return False
        fields = config.message_type.fields_by_name
        mode = fields.get("snow_melt_mode")
        return (
            mode is not None and mode.enum_type is not None and "apply_snow_melt_mode" in fields
            and all(name in mode.enum_type.values_by_name for name in ("AUTO", "ALWAYS_ON", "ALWAYS_OFF"))
        )
    return True


class StarlinkClient:
    def __init__(
        self, settings: Callable[[], Settings] = load_settings,
        connection_factory=Connection, clock: Callable[[], str] = _now,
    ):
        self._settings = settings
        self._connect = connection_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._evidence: Dict[tuple, str] = {}

    def _status(self, connection, settings) -> StarlinkStatus:
        state = status_data(connection.call("get_status"), self._clock())
        if settings.expected_device_id is not None and state.device.device_id != settings.expected_device_id:
            raise StarlinkError("Starlink identity does not match STARLINK_EXPECTED_DEVICE_ID; no control was sent.")
        return state

    def get_status(self) -> StarlinkStatus:
        settings = self._settings()
        with self._connect(settings.target) as connection:
            return self._status(connection, settings)

    def get_capabilities(self) -> Capabilities:
        settings = self._settings()
        warnings = ["Schema presence does not prove that firmware permits a control; reads never test writes."]
        with self._lock, self._connect(settings.target) as connection:
            state = self._status(connection, settings)
            try:
                config = config_data(connection.call("dish_get_config"))
            except (Unsupported, RpcFailure) as error:
                config = None
                warnings.append(f"Configuration unavailable: {error}")
            controls = [
                ControlCapability(
                    action, _control_schema(connection, action),
                    action in settings.allowed_actions,
                    self._evidence.get((state.device, action), "unverified"),
                    "Execution requires local opt-in, matching fresh identity, and an explicit user request.",
                )
                for action in _ACTIONS
            ]
            return Capabilities(
                SOURCE, self._clock(), state.device, config,
                {name: connection.request_field(name) is not None for name in (
                    "get_status", "get_history", "dish_get_config", "dish_get_obstruction_map",
                )}, controls, warnings,
            )

    def get_history(self, samples: int = MAX_SAMPLES, include_samples: bool = False) -> History:
        if type(samples) is not int or not 1 <= samples <= MAX_SAMPLES or type(include_samples) is not bool:
            raise StarlinkError("samples must be an integer from 1 to 900; include_samples must be boolean.")
        settings = self._settings()
        with self._connect(settings.target) as connection:
            state = self._status(connection, settings)
            raw = connection.call("get_history")
            return history_data(raw, state.device, self._clock(), samples, include_samples)

    def get_obstructions(self) -> Obstructions:
        settings = self._settings()
        warnings = []
        with self._connect(settings.target) as connection:
            raw = connection.call("get_status")
            device = _identity(raw)
            if settings.expected_device_id is not None and device.device_id != settings.expected_device_id:
                raise StarlinkError("Starlink identity does not match STARLINK_EXPECTED_DEVICE_ID.")
            stats = _message(raw, "obstruction_stats")
            rows, columns, frame = None, None, None
            try:
                obstruction_map = connection.call("dish_get_obstruction_map")
                map_rows = _scalar(obstruction_map, "num_rows")
                map_columns = _scalar(obstruction_map, "num_cols")
                if (
                    type(map_rows) is not int or type(map_columns) is not int
                    or map_rows <= 0 or map_columns <= 0 or map_rows * map_columns > 250_000
                ):
                    raise StarlinkError("Starlink returned invalid obstruction map dimensions.")
                cells = _field(obstruction_map, "snr")
                if cells is None or not cells.is_repeated:
                    raise Unsupported("Starlink firmware does not expose obstruction map cells.")
                if len(obstruction_map.snr) != map_rows * map_columns:
                    raise StarlinkError("Starlink returned an incomplete obstruction map.")
                rows, columns = map_rows, map_columns
                frame = _enum(obstruction_map, "map_reference_frame")
            except (Unsupported, RpcFailure) as error:
                warnings.append(f"Obstruction map unavailable: {error}")
            duration = _metric(stats, "avg_prolonged_obstruction_duration_s", "s")
            interval = _metric(stats, "avg_prolonged_obstruction_interval_s", "s")
            if _bool(stats, "avg_prolonged_obstruction_valid") is not True:
                duration = Reading(None, "s", "unavailable", "Prolonged obstruction statistics are not ready.")
                interval = duration
            if stats is None:
                warnings.append("Device obstruction statistics are unavailable.")
            collection = _metric(stats, "valid_s", "s")
            fraction = _metric(stats, "fraction_obstructed", "ratio", maximum=1)
            if collection.value == 0:
                fraction = Reading(None, "ratio", "unavailable", "The dish has not collected valid obstruction data yet.")
                warnings.append("Obstruction collection is not ready; an initial zero is not evidence of a clear view.")
            warnings.append("The fraction is device-reported, not inferred from map cells or throughput.")
            return Obstructions(
                SOURCE, self._clock(), device, _bool(stats, "currently_obstructed"),
                fraction, collection, duration, interval,
                rows, columns, frame, warnings,
            )

    def _authorize(self, action: str) -> Settings:
        settings = self._settings()
        if action not in settings.allowed_actions:
            raise StarlinkError(f"Starlink {action} is disabled; explicitly enable it in STARLINK_ALLOWED_ACTIONS.")
        if settings.expected_device_id is None:
            raise StarlinkError("Starlink controls require STARLINK_EXPECTED_DEVICE_ID.")
        return settings

    def _send(self, connection, state, action, values) -> Optional[bool]:
        try:
            connection.call(_ACTIONS[action], values)
            self._evidence[(state.device, action)] = "accepted"
            return True
        except Unsupported:
            self._evidence[(state.device, action)] = "unsupported"
            raise
        except RpcFailure as error:
            if error.code in ("PERMISSION_DENIED", "UNAUTHENTICATED", "UNIMPLEMENTED", "INVALID_ARGUMENT",
                              "DEVICE_STATUS_3", "DEVICE_STATUS_7", "DEVICE_STATUS_12", "DEVICE_STATUS_16"):
                self._evidence[(state.device, action)] = "denied"
                raise
            return None
        except StarlinkError:
            # A malformed acknowledgement is not proof that the command failed.
            return None

    def set_snow_melt(self, mode: str) -> ControlResult:
        if type(mode) is not str or mode not in ("auto", "always_on", "always_off"):
            raise StarlinkError("Snow-melt mode must be auto, always_on, or always_off.")
        return self._set("snow_melt", {"snow_melt_mode": mode})

    def set_sleep(self, enabled: bool, start_minutes_utc=None, duration_minutes=None) -> ControlResult:
        if type(enabled) is not bool:
            raise StarlinkError("enabled must be an explicit boolean.")
        if enabled:
            if (
                type(start_minutes_utc) is not int or not 0 <= start_minutes_utc < 1440
                or type(duration_minutes) is not int or not 1 <= duration_minutes <= 1440
            ):
                raise StarlinkError("Enabling sleep requires UTC start 0..1439 and duration 1..1440 minutes.")
            requested = {
                "sleep_enabled": True, "sleep_start_minutes_utc": start_minutes_utc,
                "sleep_duration_minutes": duration_minutes,
            }
        else:
            if start_minutes_utc is not None or duration_minutes is not None:
                raise StarlinkError("Disabling sleep must omit start and duration.")
            requested = {"sleep_enabled": False}
        return self._set("sleep", requested)

    def _set(self, action, requested) -> ControlResult:
        settings = self._authorize(action)
        with self._lock:
            with self._connect(settings.target) as connection:
                state = self._status(connection, settings)
                if not _control_schema(connection, action):
                    raise Unsupported(f"Starlink firmware lacks the required {action} control schema.")
                config = config_data(connection.call("dish_get_config"))
                before = {name: getattr(config, name) for name in requested}
                if any(value is None for value in before.values()):
                    raise Unsupported("The current setting cannot be read reliably; no control was sent.")
                if before == requested:
                    return ControlResult(
                        SOURCE, self._clock(), state.device, action, requested, before, before,
                        False, None, True, ["Already in the requested state; no command was sent."],
                    )
                if action == "snow_melt":
                    values = {"dish_config": {
                        "snow_melt_mode": requested["snow_melt_mode"].upper(), "apply_snow_melt_mode": True,
                    }}
                    warnings = ["Forced snow melting can increase power use. Mode is not proof of active heating."]
                else:
                    start = requested.get("sleep_start_minutes_utc", config.sleep_start_minutes_utc)
                    duration = requested.get("sleep_duration_minutes", config.sleep_duration_minutes)
                    if start is None or duration is None or duration < 1:
                        raise Unsupported("A valid existing sleep schedule is required to preserve it when disabling.")
                    values = {
                        "enable_power_save": requested["sleep_enabled"],
                        "power_save_start_minutes": start, "power_save_duration_minutes": duration,
                    }
                    warnings = ["Sleep can interrupt internet access. The schedule uses UTC, not local wall time."]
                acknowledged = self._send(connection, state, action, values)
            try:
                with self._connect(settings.target) as connection:
                    observed_state = self._status(connection, settings)
                    observed_config = config_data(connection.call("dish_get_config"))
                observed = {name: getattr(observed_config, name) for name in requested}
            except StarlinkError:
                raise StarlinkError(
                    "Starlink command may have taken effect; outcome unconfirmed. Read status before any retry."
                ) from None
            if observed != requested:
                raise StarlinkError(
                    "Starlink command may have taken effect, but read-back did not match; do not retry automatically."
                )
            if acknowledged is None:
                warnings.append("Acknowledgement was lost; a fresh read confirmed the requested setting.")
            return ControlResult(
                SOURCE, self._clock(), observed_state.device, action, requested, before, observed,
                True, acknowledged, True, warnings,
            )

    def reboot(self, confirm_disruption: bool) -> ControlResult:
        if confirm_disruption is not True:
            raise StarlinkError("Reboot requires confirm_disruption=true and an explicit request to interrupt internet.")
        settings = self._authorize("reboot")
        with self._lock:
            with self._connect(settings.target) as connection:
                state = self._status(connection, settings)
                if not _control_schema(connection, "reboot"):
                    raise Unsupported("Starlink firmware does not expose reboot.")
                acknowledged = self._send(connection, state, "reboot", {})
            warnings = ["Reboot interrupts internet and may apply a pending firmware update."]
            if "install_pending" in state.active_alerts:
                warnings.append("The dish reported a pending software installation before reboot.")
            deadline = time.monotonic() + READBACK_SECONDS
            while time.monotonic() < deadline:
                try:
                    after = self.get_status()
                except StarlinkError:
                    after = None
                if after is not None and after.device.device_id == state.device.device_id and (
                    state.boot_count is not None and after.boot_count is not None and after.boot_count > state.boot_count
                    or state.uptime_seconds.value is not None and after.uptime_seconds.value is not None
                    and after.uptime_seconds.value < state.uptime_seconds.value
                ):
                    if acknowledged is None:
                        warnings.append("Acknowledgement was lost; a fresh read observed a restart.")
                    return ControlResult(
                        SOURCE, self._clock(), after.device, "reboot", {"reboot": True},
                        {"uptime_seconds": state.uptime_seconds.value, "boot_count": state.boot_count},
                        {"uptime_seconds": after.uptime_seconds.value, "boot_count": after.boot_count},
                        True, acknowledged, True, warnings,
                    )
                time.sleep(1)
            raise StarlinkError(
                "Starlink reboot may have taken effect; restart unconfirmed within the read-back window. "
                "A disconnect is not proof of reboot. Read status later; never retry automatically."
            )
