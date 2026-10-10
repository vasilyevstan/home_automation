"""Local Govee LAN discovery, status, and explicit verified light controls."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import select
import socket
import struct
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Literal, Optional, Protocol, Sequence

from ..electricity_market.prices import UTC


MULTICAST_GROUP = "239.255.255.250"
BROADCAST_ADDRESS = "255.255.255.255"
DISCOVERY_PORT = 4001
RESPONSE_PORT = 4002
CONTROL_PORT = 4003
DISCOVERY_TIMEOUT = 3.0
REQUEST_TIMEOUT = 3.0
MAX_PACKET_SIZE = 8192
ControlName = Literal["power", "brightness", "color", "color_temperature"]
ColorMode = Literal["rgb", "color_temperature", "unknown"]
_DEVICE_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}")
_SKU = re.compile(r"[A-Za-z0-9_-]{1,32}")


class GoveeError(Exception):
    """An expected local protocol or device failure safe to display."""


class _PacketError(Exception):
    """An invalid UDP response that should not expose its raw contents."""


@dataclass(frozen=True)
class Datagram:
    payload: bytes
    source_ip: str
    source_port: int


@dataclass(frozen=True)
class RGBColor:
    red: int
    green: int
    blue: int


@dataclass(frozen=True)
class UnavailableField:
    field: str
    reason: str


@dataclass(frozen=True)
class GoveeDevice:
    device_id: str
    ip: str
    sku: str
    ble_hardware_version: Optional[str]
    ble_software_version: Optional[str]
    wifi_hardware_version: Optional[str]
    wifi_software_version: Optional[str]
    last_seen: str


@dataclass(frozen=True)
class DeviceInventory:
    source: str
    retrieved_at: str
    discovery_performed: bool
    devices: List[GoveeDevice]
    warnings: List[str]


@dataclass(frozen=True)
class DeviceState:
    device_id: str
    ip: str
    sku: str
    is_on: bool
    brightness: Optional[int]
    color: Optional[RGBColor]
    color_temperature_kelvin: Optional[int]
    active_color_mode: ColorMode
    retrieved_at: str
    unavailable: List[UnavailableField]
    warnings: List[str]


@dataclass(frozen=True)
class RequestedControl:
    on: Optional[bool] = None
    brightness: Optional[int] = None
    color: Optional[RGBColor] = None
    color_temperature_kelvin: Optional[int] = None


@dataclass(frozen=True)
class ControlResult:
    device_id: str
    control: ControlName
    requested: RequestedControl
    before_state: DeviceState
    command_sent: bool
    acknowledged: Optional[bool]
    verified: bool
    state: DeviceState
    warnings: List[str]


class DatagramTransport(Protocol):
    async def exchange(
        self, payload: bytes, targets: Sequence[tuple[str, int]], timeout: float
    ) -> List[Datagram]:
        ...

    async def send(self, payload: bytes, target: tuple[str, int]) -> None:
        ...


class SocketDatagramTransport:
    """Bounded UDP transport for the documented Govee LAN ports."""

    @staticmethod
    def _listener() -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            listener.bind(("", RESPONSE_PORT))
            listener.setblocking(False)
            return listener
        except OSError:
            listener.close()
            raise GoveeError(
                "UDP response port 4002 could not be opened. Close another Govee "
                "LAN listener or allow local-network access, then retry."
            ) from None

    @staticmethod
    def _sender() -> socket.socket:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sender.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2)
            )
            return sender
        except OSError:
            sender.close()
            raise GoveeError("A local Govee UDP sender could not be created.") from None

    @classmethod
    def _exchange_sync(
        cls, payload: bytes, targets: Sequence[tuple[str, int]], timeout: float
    ) -> List[Datagram]:
        listener = cls._listener()
        sender = cls._sender()
        try:
            sent = 0
            for target in targets:
                try:
                    sender.sendto(payload, target)
                    sent += 1
                except OSError:
                    continue
            if sent == 0:
                raise GoveeError(
                    "The local Govee UDP request could not be sent on any configured route."
                )
            responses = []
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    ready, _, _ = select.select([listener], [], [], remaining)
                except OSError:
                    raise GoveeError("The local Govee UDP listener failed.") from None
                if not ready:
                    break
                try:
                    packet, address = listener.recvfrom(MAX_PACKET_SIZE)
                except OSError:
                    raise GoveeError("A Govee UDP response could not be received.") from None
                responses.append(Datagram(packet, address[0], address[1]))
            return responses
        finally:
            sender.close()
            listener.close()

    @classmethod
    def _send_sync(cls, payload: bytes, target: tuple[str, int]) -> None:
        sender = cls._sender()
        try:
            sender.sendto(payload, target)
        except OSError:
            raise GoveeError(
                "The Govee LAN command could not be dispatched to the selected device."
            ) from None
        finally:
            sender.close()

    async def exchange(
        self, payload: bytes, targets: Sequence[tuple[str, int]], timeout: float
    ) -> List[Datagram]:
        return await asyncio.to_thread(self._exchange_sync, payload, targets, timeout)

    async def send(self, payload: bytes, target: tuple[str, int]) -> None:
        await asyncio.to_thread(self._send_sync, payload, target)


def normalize_device_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _DEVICE_ID.fullmatch(value) is None
    ):
        raise GoveeError(
            "device_id must be the exact non-empty identifier returned by Govee discovery."
        )
    return value


def _local_ip(value: object) -> str:
    if not isinstance(value, str):
        raise GoveeError("A Govee device address must be a local IPv4 address.")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise GoveeError("A Govee device address must be a local IPv4 address.") from None
    if (
        address.is_global
        or address.is_loopback
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise GoveeError("Refusing a non-local or unusable Govee device address.")
    return str(address)


def _sku(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or _SKU.fullmatch(value) is None:
        raise GoveeError("A Govee discovery response contained an invalid SKU.")
    return value


def _version(value: object) -> Optional[str]:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GoveeError("A Govee discovery response contained invalid version metadata.")
    return value


def _strict_integer(value: object, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise GoveeError(f"{name} must be an integer from {minimum} through {maximum}.")
    return value


def _encode(command: str, data: dict) -> bytes:
    return json.dumps(
        {"msg": {"cmd": command, "data": data}},
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _decode(packet: Datagram) -> tuple[str, dict]:
    if not packet.payload or len(packet.payload) >= MAX_PACKET_SIZE:
        raise _PacketError
    try:
        value = json.loads(packet.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _PacketError from None
    if not isinstance(value, dict):
        raise _PacketError
    message = value.get("msg")
    if not isinstance(message, dict):
        raise _PacketError
    command, data = message.get("cmd"), message.get("data")
    if not isinstance(command, str) or not command or not isinstance(data, dict):
        raise _PacketError
    return command, data


def _scan_packet() -> bytes:
    return _encode("scan", {"account_topic": "reserve"})


def _status_packet() -> bytes:
    return _encode("devStatus", {})


def _turn_packet(on: bool) -> bytes:
    return _encode("turn", {"value": 1 if on else 0})


def _brightness_packet(brightness: int) -> bytes:
    return _encode("brightness", {"value": brightness})


def _color_packet(color: RGBColor) -> bytes:
    return _encode(
        "colorwc",
        {
            "color": {"r": color.red, "g": color.green, "b": color.blue},
            "colorTemInKelvin": 0,
        },
    )


def _temperature_packet(kelvin: int) -> bytes:
    return _encode(
        "colorwc",
        {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": kelvin},
    )


def _device_from_scan(packet: Datagram, data: dict, observed_at: str) -> GoveeDevice:
    source_ip = _local_ip(packet.source_ip)
    advertised_ip = _local_ip(data.get("ip"))
    if advertised_ip != source_ip:
        raise GoveeError("A Govee discovery response advertised a conflicting source address.")
    return GoveeDevice(
        device_id=normalize_device_id(data.get("device")),
        ip=source_ip,
        sku=_sku(data.get("sku")),
        ble_hardware_version=_version(data.get("bleVersionHard")),
        ble_software_version=_version(data.get("bleVersionSoft")),
        wifi_hardware_version=_version(data.get("wifiVersionHard")),
        wifi_software_version=_version(data.get("wifiVersionSoft")),
        last_seen=observed_at,
    )


def _optional_integer(
    data: dict, key: str, minimum: int, maximum: int, label: str
) -> tuple[Optional[int], Optional[UnavailableField]]:
    value = data.get(key)
    if key not in data:
        return None, UnavailableField(label, "The device omitted this status field.")
    if type(value) is not int or not minimum <= value <= maximum:
        return None, UnavailableField(label, "The device returned an invalid value.")
    return value, None


def _status_from_data(
    device: GoveeDevice, data: dict, retrieved_at: str, warnings: Sequence[str]
) -> DeviceState:
    on_off = data.get("onOff")
    if type(on_off) is not int or on_off not in (0, 1):
        raise GoveeError("The Govee device did not report a valid power state.")

    unavailable = []
    brightness, missing = _optional_integer(data, "brightness", 0, 100, "brightness")
    if missing is not None:
        unavailable.append(missing)

    color = None
    color_data = data.get("color")
    if not isinstance(color_data, dict):
        unavailable.append(UnavailableField("color", "The device omitted a valid RGB object."))
    else:
        channels = []
        for key in ("r", "g", "b"):
            value = color_data.get(key)
            if type(value) is not int or not 0 <= value <= 255:
                channels = []
                break
            channels.append(value)
        if channels:
            color = RGBColor(*channels)
        else:
            unavailable.append(UnavailableField("color", "The device returned invalid RGB channels."))

    color_temperature = None
    mode: ColorMode = "unknown"
    raw_temperature = data.get("colorTemInKelvin")
    if "colorTemInKelvin" not in data:
        unavailable.append(
            UnavailableField(
                "color_temperature_kelvin", "The device omitted this status field."
            )
        )
    elif type(raw_temperature) is not int or not (
        raw_temperature == 0 or 2000 <= raw_temperature <= 9000
    ):
        unavailable.append(
            UnavailableField(
                "color_temperature_kelvin", "The device returned an invalid Kelvin value."
            )
        )
    elif raw_temperature == 0:
        mode = "rgb"
    else:
        color_temperature = raw_temperature
        mode = "color_temperature"

    result_warnings = list(warnings)
    if unavailable:
        result_warnings.append(
            "Some supported status fields are unavailable; null values are not zero."
        )
    return DeviceState(
        device.device_id,
        device.ip,
        device.sku,
        bool(on_off),
        brightness,
        color,
        color_temperature,
        mode,
        retrieved_at,
        unavailable,
        sorted(set(result_warnings)),
    )


class GoveeClient:
    def __init__(
        self,
        transport: Optional[DatagramTransport] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.transport = transport if transport is not None else SocketDatagramTransport()
        self.clock = clock
        self._lock = asyncio.Lock()
        self._cache: Dict[str, GoveeDevice] = {}

    def _now(self) -> str:
        return self.clock().astimezone(UTC).isoformat()

    async def _discover(self) -> tuple[List[GoveeDevice], List[str], str]:
        try:
            packets = await self.transport.exchange(
                _scan_packet(),
                (
                    (MULTICAST_GROUP, DISCOVERY_PORT),
                    (BROADCAST_ADDRESS, DISCOVERY_PORT),
                ),
                DISCOVERY_TIMEOUT,
            )
        except GoveeError:
            raise

        observed_at = self._now()
        devices: Dict[str, GoveeDevice] = {}
        addresses: Dict[str, str] = {}
        invalid = 0
        for packet in packets:
            try:
                command, data = _decode(packet)
                if command != "scan":
                    continue
                device = _device_from_scan(packet, data, observed_at)
            except (_PacketError, GoveeError):
                invalid += 1
                continue
            existing = devices.get(device.device_id)
            address_owner = addresses.get(device.ip)
            if (
                existing is not None
                and (existing.ip != device.ip or existing.sku != device.sku)
            ) or (address_owner is not None and address_owner != device.device_id):
                raise GoveeError(
                    "Govee discovery reported conflicting device identities or addresses."
                )
            devices[device.device_id] = device
            addresses[device.ip] = device.device_id

        warnings = []
        if invalid:
            warnings.append(
                f"Ignored {invalid} invalid or conflicting-address Govee discovery "
                "response(s) without exposing their contents."
            )
        if not devices:
            warnings.append(
                "No Govee devices responded. This does not establish power state; "
                "enable LAN Control in Govee Home and check model support, client "
                "isolation, multicast, firewall, and local-network permissions."
            )
        ordered = [devices[device_id] for device_id in sorted(devices)]
        self._cache = {device.device_id: device for device in ordered}
        return ordered, warnings, observed_at

    async def list_devices(self, refresh: bool = True) -> DeviceInventory:
        if type(refresh) is not bool:
            raise GoveeError("refresh must be a boolean.")
        async with self._lock:
            if refresh:
                devices, warnings, retrieved_at = await self._discover()
                return DeviceInventory(
                    "govee_lan", retrieved_at, True, devices, warnings
                )
            warnings = [
                "Cached inventory only: reachability and current state were not refreshed."
            ]
            if not self._cache:
                warnings.append(
                    "No devices are cached in this MCP process; run discovery with refresh=true."
                )
            return DeviceInventory(
                "govee_lan",
                self._now(),
                False,
                [self._cache[key] for key in sorted(self._cache)],
                warnings,
            )

    async def _resolve(
        self, device_id: str
    ) -> tuple[GoveeDevice, List[str]]:
        devices, warnings, _ = await self._discover()
        selected = next(
            (device for device in devices if device.device_id == device_id), None
        )
        if selected is None:
            raise GoveeError(
                "The requested Govee device ID was not found in bounded LAN "
                "discovery. Check LAN Control and connectivity."
            )
        return selected, warnings

    async def _read(
        self, device: GoveeDevice, warnings: Sequence[str] = ()
    ) -> DeviceState:
        try:
            packets = await self.transport.exchange(
                _status_packet(), ((device.ip, CONTROL_PORT),), REQUEST_TIMEOUT
            )
        except GoveeError:
            raise
        invalid = 0
        for packet in packets:
            if packet.source_ip != device.ip:
                continue
            try:
                command, data = _decode(packet)
            except _PacketError:
                invalid += 1
                continue
            if command != "devStatus":
                continue
            try:
                extra = list(warnings)
                if invalid:
                    extra.append(
                        "Ignored invalid status responses from the selected address."
                    )
                return _status_from_data(device, data, self._now(), extra)
            except GoveeError:
                invalid += 1
        if invalid:
            raise GoveeError(
                "The selected Govee device returned no valid status response."
            )
        raise GoveeError(
            "The selected Govee device did not answer the bounded status request."
        )

    async def get_device(self, device_id: str) -> DeviceState:
        device_id = normalize_device_id(device_id)
        async with self._lock:
            device, warnings = await self._resolve(device_id)
            return await self._read(device, warnings)

    @staticmethod
    def _matches(
        state: DeviceState, control: ControlName, requested: RequestedControl
    ) -> bool:
        if control == "power":
            return state.is_on == requested.on
        if not state.is_on:
            return False
        if control == "brightness":
            return state.brightness == requested.brightness
        if control == "color":
            return (
                state.active_color_mode == "rgb"
                and state.color == requested.color
            )
        return (
            state.active_color_mode == "color_temperature"
            and state.color_temperature_kelvin
            == requested.color_temperature_kelvin
        )

    @staticmethod
    def _require_supported(state: DeviceState, control: ControlName) -> None:
        missing = {field.field for field in state.unavailable}
        field_name = {
            "brightness": "brightness",
            "color": "color",
            "color_temperature": "color_temperature_kelvin",
        }.get(control)
        if field_name is not None and field_name in missing:
            raise GoveeError(
                f"The selected device did not expose a valid {field_name} status; "
                "no command was sent."
            )
        if control != "power" and not state.is_on:
            raise GoveeError(
                "The selected Govee light is off. Explicitly turn it on before "
                "changing brightness or color; no command was sent."
            )

    async def _set(
        self,
        device_id: str,
        control: ControlName,
        requested: RequestedControl,
        payload: bytes,
    ) -> ControlResult:
        device_id = normalize_device_id(device_id)
        async with self._lock:
            device, discovery_warnings = await self._resolve(device_id)
            before = await self._read(device, discovery_warnings)
            if self._matches(before, control, requested):
                return ControlResult(
                    device_id,
                    control,
                    requested,
                    before,
                    False,
                    None,
                    True,
                    before,
                    [],
                )
            self._require_supported(before, control)
            try:
                await self.transport.send(payload, (device.ip, CONTROL_PORT))
            except GoveeError:
                raise GoveeError(
                    "The Govee command may have taken effect, but dispatch did not "
                    "complete cleanly. No automatic command retry was made; read "
                    "the device state before deciding on another action."
                ) from None
            try:
                after = await self._read(device, discovery_warnings)
            except GoveeError:
                raise GoveeError(
                    "The Govee command may have taken effect, but its outcome could "
                    "not be verified. No automatic command retry was made; read the "
                    "device state before deciding on another action."
                ) from None
            if not self._matches(after, control, requested):
                raise GoveeError(
                    "Fresh Govee status did not confirm the requested state. The "
                    "outcome is uncertain and no automatic command retry was made."
                )
            return ControlResult(
                device_id,
                control,
                requested,
                before,
                True,
                None,
                True,
                after,
                [
                    "The LAN protocol provides no reliable command acknowledgement; "
                    "a fresh device status read confirmed the requested state."
                ],
            )

    async def set_power(self, device_id: str, on: bool) -> ControlResult:
        if type(on) is not bool:
            raise GoveeError("on must be an explicit boolean desired state, not a toggle.")
        return await self._set(
            device_id,
            "power",
            RequestedControl(on=on),
            _turn_packet(on),
        )

    async def set_brightness(
        self, device_id: str, brightness: int
    ) -> ControlResult:
        brightness = _strict_integer(brightness, 1, 100, "brightness")
        return await self._set(
            device_id,
            "brightness",
            RequestedControl(brightness=brightness),
            _brightness_packet(brightness),
        )

    async def set_color(
        self, device_id: str, red: int, green: int, blue: int
    ) -> ControlResult:
        color = RGBColor(
            _strict_integer(red, 0, 255, "red"),
            _strict_integer(green, 0, 255, "green"),
            _strict_integer(blue, 0, 255, "blue"),
        )
        return await self._set(
            device_id,
            "color",
            RequestedControl(color=color),
            _color_packet(color),
        )

    async def set_color_temperature(
        self, device_id: str, kelvin: int
    ) -> ControlResult:
        kelvin = _strict_integer(kelvin, 2000, 9000, "kelvin")
        return await self._set(
            device_id,
            "color_temperature",
            RequestedControl(color_temperature_kelvin=kelvin),
            _temperature_packet(kelvin),
        )
