"""Local Sonos discovery, stable identities, state, and explicit controls."""

from __future__ import annotations

import importlib.util
import ipaddress
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Callable, Literal, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import Field

from ..electricity_market.prices import UTC

if TYPE_CHECKING:
    from soco.data_structures import DidlObject


DISCOVERY_TIMEOUT = 5
REQUEST_TIMEOUT = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ZONES = 64
VERIFY_ATTEMPTS = 3
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
DEVICE_NS = "urn:schemas-upnp-org:device-1-0"
_UID = re.compile(r"RINCON_[0-9A-F]{12,24}")
_FAVORITE_ID = re.compile(r"FV:2/[A-Za-z0-9._:-]{1,128}")
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
PlaybackAction = Literal["play", "pause", "stop", "next", "previous"]


class SonosError(Exception):
    """An expected failure whose message is safe to display."""


class _UPnPError(SonosError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"Sonos rejected the request (UPnP code {code}).")


@dataclass(frozen=True)
class SpeakerIdentity:
    speaker_id: str
    ip: str
    room_name: str
    model_name: str
    model_number: Optional[str]
    software_version: Optional[str]
    hardware_version: Optional[str]
    last_seen: str


@dataclass(frozen=True)
class Zone:
    speaker_id: str
    ip: str
    room_name: str
    coordinator_id: str
    room_id: Optional[str]
    visible: bool
    is_bridge: bool
    channel: Optional[str]


@dataclass(frozen=True)
class SonosGroup:
    coordinator_id: str
    speaker_ids: list[str]
    room_ids: list[str]


@dataclass(frozen=True)
class ListedSpeaker:
    identity: SpeakerIdentity
    availability: Literal["seen", "not_seen", "not_checked"]
    cache_age_seconds: float
    topology: Optional[Zone]


@dataclass(frozen=True)
class SpeakerInventory:
    source: str
    retrieved_at: str
    discovery_performed: bool
    topology_observed_at: Optional[str]
    speakers: list[ListedSpeaker]
    groups: list[SonosGroup]
    warnings: list[str]


@dataclass(frozen=True)
class RenderingState:
    room_id: str
    volume: Optional[int]
    muted: Optional[bool]
    warnings: list[str]


@dataclass(frozen=True)
class Track:
    title: Optional[str]
    artist: Optional[str]
    album: Optional[str]
    duration_seconds: Optional[int]
    position_seconds: Optional[int]
    queue_index: Optional[int]
    uri: Optional[str]


@dataclass(frozen=True)
class PlaybackState:
    coordinator_id: str
    transport_state: str
    transport_status: str
    source_uri: Optional[str]
    track: Optional[Track]
    available_actions: Optional[list[str]]
    warnings: list[str]


@dataclass(frozen=True)
class SpeakerState:
    source: str
    retrieved_at: str
    identity: SpeakerIdentity
    topology: Zone
    group: SonosGroup
    rendering: Optional[RenderingState]
    playback: Optional[PlaybackState]
    warnings: list[str]


@dataclass(frozen=True)
class QueueRevision:
    coordinator_id: Annotated[str, Field(strict=True)]
    update_id: Annotated[int, Field(strict=True, ge=0, le=4294967295)]


@dataclass(frozen=True)
class QueueItem:
    index: int
    item_id: str
    title: Optional[str]
    artist: Optional[str]
    album: Optional[str]
    uri: Optional[str]


@dataclass(frozen=True)
class QueuePage:
    speaker_id: str
    coordinator_id: str
    retrieved_at: str
    queue_revision: QueueRevision
    start: int
    limit: int
    number_returned: int
    total_matches: int
    next_start: Optional[int]
    items: list[QueueItem]


@dataclass(frozen=True)
class FavoriteItem:
    favorite_id: str
    title: Optional[str]
    media_type: Optional[str]
    playback_mode: Literal["stream", "queue", "unsupported"]
    reason: Optional[str]


@dataclass(frozen=True)
class FavoritesPage:
    speaker_id: str
    retrieved_at: str
    start: int
    limit: int
    number_returned: int
    total_matches: int
    next_start: Optional[int]
    items: list[FavoriteItem]
    warnings: list[str]


@dataclass(frozen=True)
class QueueChange:
    previous_size: int
    new_size: int
    index: Optional[int] = None
    from_index: Optional[int] = None
    to_index: Optional[int] = None
    tracks_added: Optional[int] = None


@dataclass(frozen=True)
class ControlResult:
    action: str
    requested_speaker_id: str
    execution_speaker_id: str
    affected_speaker_ids: list[str]
    command_sent: bool
    acknowledged: Optional[bool]
    verified: bool
    completed_steps: list[str]
    before_groups: list[SonosGroup]
    after_groups: list[SonosGroup]
    state: Optional[SpeakerState]
    queue: Optional[QueuePage]
    queue_change: Optional[QueueChange]
    warnings: list[str]


@dataclass
class _Topology:
    zones: dict[str, Zone]
    groups: dict[str, SonosGroup]
    observed_at: str


@dataclass
class _Context:
    requested: SpeakerIdentity
    target: SpeakerIdentity
    topology: _Topology
    watched_groups: list[SonosGroup]
    room_only: bool = False


@dataclass
class _Snapshot:
    state: SpeakerState
    source_uri: Optional[str]
    track_uri: Optional[str]


@dataclass
class _Mutation:
    sent: list[str] = field(default_factory=list)
    acknowledged: list[str] = field(default_factory=list)


def ensure_runtime() -> None:
    if importlib.util.find_spec("soco") is None:
        raise SonosError("Install the optional Sonos client with 'uv sync --locked --extra sonos'.")
    from soco import config

    config.REQUEST_TIMEOUT = REQUEST_TIMEOUT
    config.CACHE_ENABLED = False
    config.ZGT_EVENT_FALLBACK = False
    logger = logging.getLogger("soco")
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    logger.propagate = False


def normalize_id(value: object) -> str:
    if not isinstance(value, str) or _UID.fullmatch(value.strip().upper()) is None:
        raise SonosError("speaker_id must be an exact Sonos RINCON UID from discovery, not a name or IP.")
    return value.strip().upper()


def _ip(value: object) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.IPv4Address(value)
    except (ValueError, ipaddress.AddressValueError):
        raise SonosError("A Sonos address must be a private LAN IPv4 address.") from None
    if not any(address in network for network in _PRIVATE_NETWORKS):
        raise SonosError("Refusing a non-private Sonos address.")
    return str(address)


def _integer(value: object, name: str, minimum: int = 0, maximum: Optional[int] = None) -> int:
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        bound = f"{minimum}-{maximum}" if maximum is not None else f">= {minimum}"
        raise SonosError(f"{name} must be an integer {bound}, not a boolean or string.")
    return value


def _number(value: object, name: str, maximum: Optional[int] = None) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise SonosError(f"Sonos returned an invalid {name}.")
    return _integer(int(value), name, maximum=maximum)


def _text(value: object, *, required: bool = False) -> Optional[str]:
    if value is None or value == "" or value == "NOT_IMPLEMENTED":
        if required:
            raise SonosError("Sonos omitted a required text field.")
        return None
    if not isinstance(value, str) or len(value) > MAX_RESPONSE_BYTES:
        raise SonosError("Sonos returned an invalid text field.")
    return value


def validate_uri(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192 or any(
        ord(char) <= 32 or ord(char) == 127 for char in value
    ):
        raise SonosError("uri must be a valid HTTP(S) media URL.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None and not 1 <= parsed.port <= 65535
        ):
            raise ValueError
    except ValueError:
        raise SonosError("Use an HTTP(S) media URL without embedded credentials or Sonos control schemes.") from None
    return value


def _redact_uri(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        return urlunsplit((
            parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path,
            "redacted" if parsed.query else "", "",
        ))
    except ValueError:
        return "[invalid media URI]"


def _xml(value: bytes | str) -> ET.Element:
    try:
        text = value.decode("utf-8-sig") if isinstance(value, bytes) else value
        if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES or any(
            marker in text.upper() for marker in ("<!DOCTYPE", "<!ENTITY")
        ):
            raise SonosError("Sonos XML exceeds the limit or contains forbidden declarations.")
        return ET.fromstring(text)
    except (ET.ParseError, UnicodeError):
        raise SonosError("Sonos returned invalid XML.") from None


def _duration(value: object) -> Optional[int]:
    text = _text(value)
    if text is None:
        return None
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)", text)
    if match is None:
        raise SonosError("Sonos returned an invalid playback duration.")
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SonosTransport:
    """Use SoCo discovery/serialization with bounded, non-retrying LAN requests."""

    def __init__(self, opener=None) -> None:
        self.opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())

    def discover(self) -> list[str]:
        ensure_runtime()
        from requests.exceptions import RequestException
        from soco import discover
        from soco.exceptions import SoCoException

        try:
            devices = discover(
                timeout=DISCOVERY_TIMEOUT, include_invisible=True, allow_network_scan=False,
            )
            addresses = sorted({_ip(device.ip_address) for device in devices or ()})
        except (SoCoException, RequestException, OSError, ET.ParseError):
            raise SonosError("Sonos discovery failed or timed out; check LAN connectivity and multicast permissions.") from None
        if len(addresses) > MAX_ZONES:
            raise SonosError("Sonos discovery exceeded the supported bounded inventory size.")
        return addresses

    def _request(self, ip: str, path: str, body: Optional[bytes] = None, headers=None) -> tuple[int, bytes]:
        host = _ip(ip)
        if not path.startswith("/") or path.startswith("//"):
            raise SonosError("Refusing an invalid Sonos service path.")
        request = Request(f"http://{host}:1400{path}", data=body, headers=headers or {})
        try:
            try:
                response = self.opener.open(request, timeout=REQUEST_TIMEOUT)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except (URLError, OSError, TimeoutError):
            raise SonosError("Sonos request failed or timed out.") from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise SonosError("Sonos response exceeded the size limit.")
        if 300 <= status < 400:
            raise SonosError("Sonos redirected a LAN request; no redirected request was sent.")
        return status, data

    def describe(self, ip: str) -> dict[str, Optional[str]]:
        status, payload = self._request(ip, "/xml/device_description.xml")
        if status != 200:
            raise SonosError(f"Sonos identity request failed with HTTP {status}.")
        device = _xml(payload).find(f"{{{DEVICE_NS}}}device")
        if device is None or device.findtext(f"{{{DEVICE_NS}}}deviceType") != "urn:schemas-upnp-org:device:ZonePlayer:1":
            raise SonosError("The address did not identify a Sonos ZonePlayer.")
        fields = ("UDN", "roomName", "modelName", "modelNumber", "softwareVersion", "hardwareVersion")
        return {name: device.findtext(f"{{{DEVICE_NS}}}{name}") for name in fields}

    def call(self, ip: str, service_name: str, action: str, args: list[tuple[str, object]]) -> dict[str, str]:
        ensure_runtime()
        from soco import SoCo

        device = SoCo(_ip(ip))
        service = getattr(device, service_name)
        # SoCo's high-level HTTP calls follow redirects and its uid is IP-cached.
        # Keep serialization, but dispatch exactly once through the bounded opener.
        headers, body = service.build_command(action, args)
        status, payload = self._request(ip, service.control_url, body.encode("utf-8"), headers)
        root = _xml(payload)
        fault = root.find(f".//{{{SOAP_NS}}}Fault")
        if fault is not None:
            code = fault.findtext(".//{urn:schemas-upnp-org:control-1-0}errorCode")
            raise _UPnPError(_number(code, "UPnP error code"))
        if status != 200:
            raise SonosError(f"Sonos request failed with HTTP {status}.")
        response = root.find(f"{{{SOAP_NS}}}Body")
        if response is None or len(response) != 1 or response[0].tag.split("}")[-1] != action + "Response":
            raise SonosError("Sonos returned an unexpected SOAP response.")
        return {child.tag.split("}")[-1]: child.text or "" for child in response[0]}


def _didl(value: str) -> list[DidlObject]:
    ensure_runtime()
    from soco.data_structures_entry import from_didl_string
    from soco.exceptions import SoCoException

    if not value:
        return []
    _xml(value)
    try:
        return from_didl_string(value)
    except (SoCoException, ValueError, KeyError, TypeError):
        raise SonosError("Sonos returned unsupported or malformed media metadata.") from None


def _resource(item: DidlObject) -> str:
    resources = item.resources
    if not resources:
        raise SonosError("This media item has no playable resource.")
    value = _text(resources[0].uri, required=True)
    if value is None:
        raise SonosError("This media item has no playable resource.")
    return value


def _artist(item: Optional[DidlObject]) -> Optional[str]:
    return _text(getattr(item, "creator", None)) or _text(getattr(item, "artist", None))


def _favorite_reference(item: DidlObject) -> tuple[DidlObject, Literal["stream", "queue"]]:
    from soco.data_structures import DidlAudioBroadcast, DidlFavorite
    from soco.exceptions import SoCoException

    try:
        if isinstance(item, DidlFavorite):
            metadata = getattr(item, "resource_meta_data", None)
            if not metadata:
                raise SonosError("This favorite has no referenced media metadata.")
            _xml(metadata)
            reference = item.reference
        else:
            reference = item
        uri = _resource(reference)
        scheme = urlsplit(uri).scheme.lower()
    except (SoCoException, ValueError, KeyError, TypeError):
        raise SonosError("This favorite has unsupported or malformed referenced metadata.") from None
    if scheme in ("x-rincon", "x-rincon-queue", "x-sonos-htastream", "x-rincon-stream"):
        raise SonosError("This favorite is a device/control source, not supported media.")
    mode = "stream" if isinstance(reference, DidlAudioBroadcast) or scheme in (
        "x-sonosapi-stream", "x-sonosapi-radio", "x-rincon-mp3radio", "hls-radio",
    ) else "queue"
    return reference, mode


class SonosClient:
    def __init__(
        self, transport: Optional[SonosTransport] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport if transport is not None else SonosTransport()
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.RLock()
        self._known: dict[str, SpeakerIdentity] = {}
        self._last_topology: Optional[_Topology] = None

    def _now(self) -> str:
        return self.clock().astimezone(UTC).isoformat()

    def _describe(self, ip: str, expected: Optional[str] = None) -> SpeakerIdentity:
        host = _ip(ip)
        data = self.transport.describe(host)
        udn = _text(data.get("UDN"), required=True)
        if udn is None or not udn.startswith("uuid:"):
            raise SonosError("Sonos did not return a valid device UDN.")
        uid = normalize_id(udn[5:])
        if expected is not None and uid != expected:
            raise SonosError("The Sonos address now belongs to a different device.")
        name = _text(data.get("roomName"), required=True)
        model = _text(data.get("modelName"), required=True)
        if name is None or model is None:
            raise SonosError("Sonos omitted its room or model.")
        return SpeakerIdentity(
            uid, host, name, model, _text(data.get("modelNumber")),
            _text(data.get("softwareVersion")), _text(data.get("hardwareVersion")), self._now(),
        )

    def _topology(self, identity: SpeakerIdentity) -> _Topology:
        result = self.transport.call(identity.ip, "zoneGroupTopology", "GetZoneGroupState", [])
        payload = result.get("ZoneGroupState")
        if not payload:
            raise SonosError("Sonos omitted group topology.")
        root = _xml(payload)
        zones: dict[str, Zone] = {}
        groups: dict[str, SonosGroup] = {}
        bonds: dict[str, str] = {}
        channels: dict[str, str] = {}
        for group in root.iter("ZoneGroup"):
            coordinator = normalize_id(group.get("Coordinator"))
            if coordinator in groups:
                raise SonosError("Sonos topology contains duplicate coordinators.")
            members = []
            for member in group.findall("ZoneGroupMember"):
                primary = normalize_id(member.get("UUID"))
                for node in [member, *member.findall("Satellite")]:
                    uid = normalize_id(node.get("UUID"))
                    if uid in zones:
                        raise SonosError("Sonos topology contains duplicate device identities.")
                    try:
                        location = urlsplit(node.get("Location", ""))
                        if location.scheme != "http" or location.port != 1400 or location.username is not None:
                            raise ValueError
                        host = _ip(location.hostname)
                    except ValueError:
                        raise SonosError("Sonos topology contains an invalid device location.") from None
                    visible = node.get("Invisible", "0") == "0" and node is member
                    bridge = node.get("IsZoneBridge", "0") == "1"
                    name = _text(node.get("ZoneName"), required=True)
                    if name is None:
                        raise SonosError("Sonos topology omitted a zone name.")
                    zones[uid] = Zone(uid, host, name, coordinator, uid if visible and not bridge else None, visible, bridge, None)
                    members.append(uid)
                    if node is not member:
                        bonds[uid] = primary
                if zones[primary].visible and not zones[primary].is_bridge:
                    for attribute in ("ChannelMapSet", "HTSatChanMapSet"):
                        for entry in filter(None, member.get(attribute, "").split(";")):
                            uid, separator, channel = entry.partition(":")
                            if not separator:
                                raise SonosError("Sonos returned an invalid bonded channel map.")
                            uid = normalize_id(uid)
                            if uid != primary:
                                if uid in bonds and bonds[uid] != primary:
                                    raise SonosError("Sonos returned conflicting bonded rooms.")
                                bonds[uid] = primary
                            channels[uid] = channel
            if coordinator not in members:
                raise SonosError("Sonos topology omitted a group's coordinator.")
            groups[coordinator] = SonosGroup(coordinator, sorted(members), [])
        if not zones or identity.speaker_id not in zones or len(zones) > MAX_ZONES:
            raise SonosError("Sonos topology is missing the requested device or exceeds the inventory limit.")
        for uid, primary in bonds.items():
            if uid not in zones or primary not in zones or zones[uid].coordinator_id != zones[primary].coordinator_id:
                raise SonosError("Sonos returned an incomplete or conflicting bonded room.")
            zones[uid] = replace(zones[uid], room_id=primary)
        for uid, channel in channels.items():
            if uid not in zones:
                raise SonosError("Sonos channel map references a missing device.")
            zones[uid] = replace(zones[uid], channel=channel)
        addresses = [zone.ip for zone in zones.values()]
        if len(set(addresses)) != len(addresses):
            raise SonosError("Sonos topology assigns the same address to different identities.")
        for coordinator, group in groups.items():
            groups[coordinator] = replace(group, room_ids=sorted({
                zones[uid].room_id for uid in group.speaker_ids if zones[uid].room_id is not None
            }))
        topology = _Topology(zones, groups, self._now())
        self._last_topology = topology
        return topology

    def _discover(self) -> tuple[set[str], list[str]]:
        addresses = self.transport.discover()
        found: dict[str, SpeakerIdentity] = {}
        warnings = []
        topology = None
        for ip in addresses:
            try:
                identity = self._describe(ip)
            except SonosError as error:
                warnings.append(f"A discovered Sonos device could not be identified: {error}")
                continue
            if identity.speaker_id in found and found[identity.speaker_id].ip != identity.ip:
                raise SonosError("Discovery returned conflicting Sonos UID/address identities.")
            found[identity.speaker_id] = identity
            if topology is None:
                try:
                    topology = self._topology(identity)
                except SonosError as error:
                    warnings.append(f"Group topology unavailable: {error}")
        if topology is not None:
            for uid, zone in topology.zones.items():
                if uid in found:
                    if found[uid].ip != zone.ip:
                        raise SonosError("Discovery and topology disagree about a Sonos address.")
                    continue
                try:
                    found[uid] = self._describe(zone.ip, uid)
                except SonosError as error:
                    warnings.append(f"Topology member {uid} is unavailable: {error}")
        self._known.update(found)
        if not found:
            warnings.append("No Sonos speakers could be verified; that does not establish their power state.")
        return set(found), warnings

    def list_speakers(self, refresh: bool = True) -> SpeakerInventory:
        if type(refresh) is not bool:
            raise SonosError("refresh must be a boolean.")
        with self._lock:
            seen, warnings = self._discover() if refresh else (set(), ["Cached inventory only; devices and topology were not refreshed."])
            topology = self._last_topology
            now = self.clock().astimezone(UTC)
            speakers = [
                ListedSpeaker(
                    identity, "seen" if uid in seen else "not_seen" if refresh else "not_checked",
                    max(0.0, (now - datetime.fromisoformat(identity.last_seen)).total_seconds()),
                    topology.zones.get(uid) if topology else None,
                )
                for uid, identity in sorted(self._known.items())
            ]
            if any(speaker.availability == "not_seen" for speaker in speakers):
                warnings.append("Unseen speakers retain last-known metadata; they are not assumed powered off.")
            return SpeakerInventory(
                "local_sonos", now.isoformat(), refresh, topology.observed_at if topology else None,
                speakers, list(topology.groups.values()) if topology else [], warnings,
            )

    def _resolve(self, speaker_id: str) -> SpeakerIdentity:
        uid = normalize_id(speaker_id)
        previous = self._known.get(uid)
        if previous is not None:
            try:
                identity = self._describe(previous.ip, uid)
            except SonosError:
                identity = None
            if identity is not None:
                self._known[uid] = identity
                return identity
        seen, _ = self._discover()
        if uid not in seen:
            raise SonosError("The requested Sonos UID was not verified in bounded discovery; refresh inventory and check connectivity.")
        return self._known[uid]

    def _member(self, topology: _Topology, uid: str, requested: SpeakerIdentity) -> SpeakerIdentity:
        zone = topology.zones.get(uid)
        if zone is None:
            raise SonosError("The requested room or coordinator is absent from fresh topology.")
        if requested.speaker_id == uid and requested.ip == zone.ip:
            return requested
        identity = self._describe(zone.ip, uid)
        self._known[uid] = identity
        return identity

    def _rendering(self, identity: SpeakerIdentity) -> RenderingState:
        warnings = []
        volume, muted = None, None
        try:
            result = self.transport.call(identity.ip, "renderingControl", "GetVolume", [("InstanceID", 0), ("Channel", "Master")])
            volume = _number(result.get("CurrentVolume"), "volume", 100)
        except SonosError as error:
            warnings.append(f"Volume unavailable: {error}")
        try:
            result = self.transport.call(identity.ip, "renderingControl", "GetMute", [("InstanceID", 0), ("Channel", "Master")])
            muted = bool(_number(result.get("CurrentMute"), "mute", 1))
        except SonosError as error:
            warnings.append(f"Mute unavailable: {error}")
        return RenderingState(identity.speaker_id, volume, muted, warnings)

    def _playback(self, identity: SpeakerIdentity) -> tuple[PlaybackState, Optional[str], Optional[str]]:
        result = self.transport.call(identity.ip, "avTransport", "GetTransportInfo", [("InstanceID", 0)])
        state = _text(result.get("CurrentTransportState"), required=True)
        status = _text(result.get("CurrentTransportStatus"), required=True)
        if state is None or status is None:
            raise SonosError("Sonos omitted its current transport state.")
        warnings = []
        source_uri, track_uri, track, actions = None, None, None, None
        try:
            media = self.transport.call(identity.ip, "avTransport", "GetMediaInfo", [("InstanceID", 0)])
            source_uri = _text(media.get("CurrentURI"))
        except SonosError as error:
            warnings.append(f"Current source unavailable: {error}")
        try:
            position = self.transport.call(identity.ip, "avTransport", "GetPositionInfo", [("InstanceID", 0)])
            track_uri = _text(position.get("TrackURI"))
            number = _number(position.get("Track"), "track number")
            items = _didl(_text(position.get("TrackMetaData")) or "")
            item = items[0] if items else None
            track = Track(
                _text(getattr(item, "title", None)), _artist(item),
                _text(getattr(item, "album", None)), _duration(position.get("TrackDuration")),
                _duration(position.get("RelTime")),
                number - 1 if number and source_uri == f"x-rincon-queue:{identity.speaker_id}#0" else None,
                _redact_uri(track_uri),
            )
        except SonosError as error:
            warnings.append(f"Current track unavailable: {error}")
        try:
            actions = self._actions(identity)
        except SonosError as error:
            warnings.append(f"Available transport actions unavailable: {error}")
        return PlaybackState(identity.speaker_id, state, status, _redact_uri(source_uri), track, actions, warnings), source_uri, track_uri

    def _actions(self, identity: SpeakerIdentity) -> list[str]:
        result = self.transport.call(identity.ip, "avTransport", "GetCurrentTransportActions", [("InstanceID", 0)])
        value = result.get("Actions")
        if not isinstance(value, str):
            raise SonosError("Sonos omitted current transport actions.")
        return sorted({action.strip().split("_")[-1] for action in value.split(",") if action.strip()})

    def _snapshot(self, identity: SpeakerIdentity) -> _Snapshot:
        topology = self._topology(identity)
        zone = topology.zones[identity.speaker_id]
        if zone.ip != identity.ip:
            raise SonosError("The requested Sonos identity moved during the read.")
        group = topology.groups[zone.coordinator_id]
        rendering, playback, source_uri, track_uri = None, None, None, None
        warnings = []
        if zone.room_id is None:
            warnings.append("This device is not an independently controllable audio room.")
        else:
            room = self._member(topology, zone.room_id, identity)
            coordinator = self._member(topology, group.coordinator_id, identity)
            rendering = self._rendering(room)
            playback, source_uri, track_uri = self._playback(coordinator)
            if zone.room_id != identity.speaker_id:
                warnings.append("Rendering and playback describe the bonded room, not independent satellite controls.")
        return _Snapshot(SpeakerState(
            "local_sonos", self._now(), identity, zone, group, rendering, playback, warnings,
        ), source_uri, track_uri)

    def get_speaker(self, speaker_id: str) -> SpeakerState:
        uid = normalize_id(speaker_id)
        with self._lock:
            return self._snapshot(self._resolve(uid)).state

    def _context(self, speaker_id: str, *, room_only: bool = False, control: bool = True) -> _Context:
        identity = self._resolve(speaker_id)
        topology = self._topology(identity)
        zone = topology.zones[identity.speaker_id]
        if zone.ip != identity.ip:
            raise SonosError("The requested Sonos identity moved during resolution.")
        if control and (not zone.visible or zone.is_bridge or zone.room_id != identity.speaker_id):
            raise SonosError("Control requires the exact visible room UID; do not independently control a bridge or bonded secondary.")
        group = topology.groups[zone.coordinator_id]
        target = identity if room_only else self._member(topology, group.coordinator_id, identity)
        return _Context(identity, target, topology, [group], room_only)

    def _browse(self, identity: SpeakerIdentity, object_id: str, start: int, limit: int, *, metadata: bool = False):
        result = self.transport.call(identity.ip, "contentDirectory", "Browse", [
            ("ObjectID", object_id), ("BrowseFlag", "BrowseMetadata" if metadata else "BrowseDirectChildren"),
            ("Filter", "*"), ("StartingIndex", start), ("RequestedCount", limit), ("SortCriteria", ""),
        ])
        count = _number(result.get("NumberReturned"), "returned item count")
        total = _number(result.get("TotalMatches"), "total item count")
        update_id = _number(result.get("UpdateID"), "queue update ID", 4294967295)
        payload = result.get("Result")
        if not isinstance(payload, str):
            raise SonosError("Sonos omitted browse results.")
        items = _didl(payload)
        if len(items) != count or count > limit or count > max(0, total - start):
            raise SonosError("Sonos browse counts are inconsistent.")
        if not count and start < total:
            raise SonosError("Sonos returned an empty page before the end of the results.")
        return items, total, update_id

    def _queue(self, identity: SpeakerIdentity, requested_id: str, start: int = 0, limit: int = 50) -> tuple[QueuePage, list[DidlObject]]:
        items, total, update_id = self._browse(identity, "Q:0", start, limit)
        page = QueuePage(
            requested_id, identity.speaker_id, self._now(), QueueRevision(identity.speaker_id, update_id),
            start, limit, len(items), total, start + len(items) if start + len(items) < total else None,
            [QueueItem(
                start + index, item.item_id, _text(getattr(item, "title", None)),
                _artist(item), _text(getattr(item, "album", None)),
                _redact_uri(_resource(item)) if item.resources else None,
            ) for index, item in enumerate(items)],
        )
        return page, items

    def get_queue(self, speaker_id: str, start: int = 0, limit: int = 50) -> QueuePage:
        uid = normalize_id(speaker_id)
        _integer(start, "start")
        _integer(limit, "limit", 1, 100)
        with self._lock:
            context = self._context(uid, control=False)
            return self._queue(context.target, uid, start, limit)[0]

    def list_favorites(self, speaker_id: str, start: int = 0, limit: int = 50) -> FavoritesPage:
        uid = normalize_id(speaker_id)
        _integer(start, "start")
        _integer(limit, "limit", 1, 100)
        with self._lock:
            context = self._context(uid, control=False)
            items, total, _ = self._browse(context.target, "FV:2", start, limit)
            favorites = []
            for item in items:
                try:
                    reference, mode = _favorite_reference(item)
                    favorites.append(FavoriteItem(item.item_id, _text(item.title), reference.item_class, mode, None))
                except SonosError as error:
                    favorites.append(FavoriteItem(item.item_id, _text(item.title), None, "unsupported", str(error)))
            return FavoritesPage(
                uid, self._now(), start, limit, len(items), total,
                start + len(items) if start + len(items) < total else None, favorites,
                ["Favorites depend on the speaker's existing music-service authorization; listing does not verify playback."],
            )

    def _check_context(self, context: _Context) -> _Topology:
        requested = self._describe(context.requested.ip, context.requested.speaker_id)
        if context.target.speaker_id != requested.speaker_id:
            self._describe(context.target.ip, context.target.speaker_id)
        topology = self._topology(requested)
        for identity in (requested, context.target):
            zone = topology.zones.get(identity.speaker_id)
            if zone is None or zone.ip != identity.ip:
                raise SonosError("A Sonos execution target changed address; refresh state before another command.")
        for group in context.watched_groups:
            if topology.groups.get(group.coordinator_id) != group:
                raise SonosError("Sonos group membership or coordinator changed; refresh state before another command.")
        return topology

    def _send(
        self, context: _Context, mutation: _Mutation, service: str,
        action: str, args: list[tuple[str, object]], *,
        guard: Optional[Callable[[], None]] = None,
    ) -> dict[str, str]:
        self._check_context(context)
        if guard is not None:
            guard()
        mutation.sent.append(action)
        result = self.transport.call(context.target.ip, service, action, args)
        mutation.acknowledged.append(action)
        return result

    def _observe(self, context: _Context) -> tuple[_Topology, _Snapshot]:
        self._check_context(context)
        snapshot = self._snapshot(self._describe(context.requested.ip, context.requested.speaker_id))
        topology = self._last_topology
        if topology is None or any(
            topology.groups.get(group.coordinator_id) != group for group in context.watched_groups
        ):
            raise SonosError("Sonos topology changed while verifying the command.")
        return topology, snapshot

    def _execute(
        self, context: _Context, action: str,
        operation: Callable[[_Mutation], Optional[QueueChange]],
        verify: Callable[[Optional[QueueChange]], tuple[_Topology, Optional[SpeakerState], Optional[QueuePage]]],
        *, group_change: bool = False,
    ) -> ControlResult:
        mutation = _Mutation()
        try:
            change = operation(mutation)
        except SonosError as error:
            if not mutation.sent:
                raise
            raise SonosError(
                f"{action} may have partially or fully taken effect; "
                f"{len(mutation.acknowledged)} of {len(mutation.sent)} dispatched steps were acknowledged. "
                f"{error} No command was automatically retried. Read current state before deciding what to do next."
            ) from None
        last_error = "The resulting state was not confirmed."
        for attempt in range(VERIFY_ATTEMPTS):
            try:
                topology, state, queue = verify(change)
                break
            except SonosError as error:
                last_error = str(error)
                if attempt < VERIFY_ATTEMPTS - 1:
                    self.sleep(0.2)
        else:
            outcome = "The command may have taken effect" if mutation.sent else "No command was sent"
            raise SonosError(
                f"{outcome}, but verification failed: {last_error} "
                "No command was automatically retried. Read current state before another action."
            ) from None
        if context.room_only and not group_change:
            affected = sorted(
                uid for uid, zone in context.topology.zones.items()
                if zone.room_id == context.requested.speaker_id
            )
        else:
            affected = sorted({uid for group in context.watched_groups for uid in group.speaker_ids})
        after = [group for group in topology.groups.values() if set(group.speaker_ids).intersection(affected)]
        warnings = ["Verification reports device state, not audible sound. Other controllers can change it afterward."]
        if group_change:
            warnings.append("Grouping can interrupt playback and change queue ownership; audio and queues were not restored.")
        return ControlResult(
            action, context.requested.speaker_id, context.target.speaker_id, affected,
            bool(mutation.sent), True if mutation.sent else None, True, mutation.acknowledged,
            context.watched_groups, after, state, queue, change, warnings,
        )

    def _set_rendering(self, speaker_id: str, value: int | bool, *, mute: bool) -> ControlResult:
        uid = normalize_id(speaker_id)
        if mute:
            if type(value) is not bool:
                raise SonosError("muted must be an explicit boolean.")
        else:
            _integer(value, "volume", 0, 100)
        with self._lock:
            context = self._context(uid, room_only=True)
            before = self._rendering(context.target)
            current = before.muted if mute else before.volume
            if current is None:
                raise SonosError("The requested rendering setting could not be read; no command was sent.")

            def operation(mutation):
                if current != value:
                    self._send(context, mutation, "renderingControl", "SetMute" if mute else "SetVolume", [
                        ("InstanceID", 0), ("Channel", "Master"),
                        ("DesiredMute" if mute else "DesiredVolume", int(value)),
                    ])

            def verify(change):
                topology, snapshot = self._observe(context)
                rendering = snapshot.state.rendering
                observed = None if rendering is None else rendering.muted if mute else rendering.volume
                if observed != value:
                    raise SonosError("The requested room-level rendering setting was not observed.")
                return topology, snapshot.state, None

            return self._execute(context, "set_mute" if mute else "set_volume", operation, verify)

    def set_volume(self, speaker_id: str, volume: int) -> ControlResult:
        return self._set_rendering(speaker_id, volume, mute=False)

    def set_mute(self, speaker_id: str, muted: bool) -> ControlResult:
        return self._set_rendering(speaker_id, muted, mute=True)

    def set_playback(self, speaker_id: str, action: PlaybackAction) -> ControlResult:
        uid = normalize_id(speaker_id)
        if not isinstance(action, str) or action not in ("play", "pause", "stop", "next", "previous"):
            raise SonosError("action must be play, pause, stop, next, or previous.")
        with self._lock:
            context = self._context(uid)
            before, _, before_uri = self._playback(context.target)
            desired = {"play": "PLAYING", "pause": "PAUSED_PLAYBACK", "stop": "STOPPED"}.get(action)
            unchanged = desired is not None and before.transport_state == desired
            if not unchanged and (before.available_actions is None or action.capitalize() not in before.available_actions):
                raise SonosError("This playback action is not currently available for the selected Sonos group.")

            def operation(mutation):
                if not unchanged:
                    args = [("InstanceID", 0)]
                    if action == "play":
                        args.append(("Speed", 1))
                    self._send(context, mutation, "avTransport", action.capitalize(), args)

            def verify(change):
                topology, snapshot = self._observe(context)
                playback = snapshot.state.playback
                if playback is None:
                    raise SonosError("Playback state is unavailable.")
                if desired is not None:
                    if playback.transport_state != desired:
                        raise SonosError("The requested playback state was not observed.")
                else:
                    old = before.track
                    new = playback.track
                    changed = new is not None and (
                        snapshot.track_uri != before_uri or old is None
                        or (new.queue_index, new.title, new.artist) != (old.queue_index, old.title, old.artist)
                    )
                    restarted = action == "previous" and old is not None and new is not None and (
                        old.position_seconds is not None and new.position_seconds is not None
                        and new.position_seconds < old.position_seconds
                    )
                    if not changed and not restarted:
                        raise SonosError("The step was acknowledged, but a track change or previous-track restart could not be confirmed.")
                return topology, snapshot.state, None

            return self._execute(context, action, operation, verify)

    def _play_uri_steps(self, context: _Context, mutation: _Mutation, uri: str, metadata: str = "") -> None:
        self._send(context, mutation, "avTransport", "SetAVTransportURI", [
            ("InstanceID", 0), ("CurrentURI", uri), ("CurrentURIMetaData", metadata),
        ])
        def source_matches():
            current = self.transport.call(context.target.ip, "avTransport", "GetMediaInfo", [("InstanceID", 0)])
            if current.get("CurrentURI") != uri:
                raise SonosError("The selected media source was not confirmed; playback was not started.")

        self._send(context, mutation, "avTransport", "Play", [("InstanceID", 0), ("Speed", 1)], guard=source_matches)

    def play_uri(self, speaker_id: str, uri: str) -> ControlResult:
        uid, uri = normalize_id(speaker_id), validate_uri(uri)
        with self._lock:
            context = self._context(uid)

            def operation(mutation):
                self._play_uri_steps(context, mutation, uri)

            def verify(change):
                topology, snapshot = self._observe(context)
                playback = snapshot.state.playback
                if snapshot.source_uri != uri or playback is None or playback.transport_state != "PLAYING":
                    raise SonosError("The requested media source and playing state were not confirmed.")
                return topology, snapshot.state, None

            return self._execute(context, "play_uri", operation, verify)

    def _favorite(self, context: _Context, favorite_id: str) -> tuple[DidlObject, Literal["stream", "queue"]]:
        items, _, _ = self._browse(context.target, favorite_id, 0, 1, metadata=True)
        if len(items) != 1 or items[0].item_id != favorite_id:
            raise SonosError("The exact Sonos favorite was not found; list favorites again.")
        return _favorite_reference(items[0])

    def _media(
        self, context: _Context, uri: Optional[str], favorite_id: Optional[str],
    ) -> tuple[str, str, Literal["stream", "queue"]]:
        if uri is not None:
            return uri, "", "queue"
        if favorite_id is None:
            raise SonosError("Supply exactly one of uri or favorite_id.")
        from soco.data_structures import to_didl_string

        reference, mode = self._favorite(context, favorite_id)
        return _resource(reference), to_didl_string(reference), mode

    @staticmethod
    def _validate_favorite_id(favorite_id: object) -> str:
        if not isinstance(favorite_id, str) or _FAVORITE_ID.fullmatch(favorite_id) is None:
            raise SonosError("favorite_id must be an exact FV:2/... ID from list_sonos_favorites.")
        return favorite_id

    @staticmethod
    def _revision(revision: QueueRevision) -> QueueRevision:
        if not isinstance(revision, QueueRevision):
            raise SonosError("queue_revision must contain coordinator_id and update_id from get_sonos_queue.")
        return QueueRevision(
            normalize_id(revision.coordinator_id),
            _integer(revision.update_id, "queue update ID", 0, 4294967295),
        )

    @staticmethod
    def _require_atomic_revision(revision: QueueRevision) -> None:
        if revision.update_id == 0:
            raise SonosError(
                "Sonos UpdateID 0 disables the device-side queue guard. "
                "A nonzero revision is required for a destructive queue edit; no command was sent."
            )

    def _checked_queue(self, context: _Context, revision: QueueRevision, index: Optional[int] = None) -> tuple[QueuePage, list[DidlObject]]:
        self._check_context(context)
        page, items = self._queue(context.target, context.requested.speaker_id, index if index is not None else 0, 1)
        if page.queue_revision != revision:
            raise SonosError("The queue revision or coordinator changed; read the queue again before editing or selecting an item.")
        if index is not None and (index >= page.total_matches or not items):
            raise SonosError("The selected queue index does not exist.")
        return page, items

    def _enqueue(
        self, context: _Context, mutation: _Mutation, uri: str, metadata: str,
    ) -> tuple[QueueChange, QueueRevision, str]:
        before, _ = self._queue(context.target, context.requested.speaker_id, 0, 1)
        response = self._send(context, mutation, "avTransport", "AddURIToQueue", [
            ("InstanceID", 0), ("EnqueuedURI", uri), ("EnqueuedURIMetaData", metadata),
            ("DesiredFirstTrackNumberEnqueued", 0), ("EnqueueAsNext", 0),
        ])
        first = _number(response.get("FirstTrackNumberEnqueued"), "insertion position")
        added = _number(response.get("NumTracksAdded"), "added track count")
        size = _number(response.get("NewQueueLength"), "new queue length")
        if first != before.total_matches + 1 or not added or size != before.total_matches + added:
            raise SonosError("The append result was inconsistent or the queue changed concurrently.")
        after, items = self._queue(context.target, context.requested.speaker_id, first - 1, 1)
        if after.total_matches != size or after.queue_revision == before.queue_revision or not items:
            raise SonosError("The appended queue content could not be confirmed.")
        return QueueChange(before.total_matches, size, index=first - 1, tracks_added=added), after.queue_revision, _resource(items[0])

    def _select_queue(
        self, context: _Context, mutation: _Mutation, index: int, revision: QueueRevision,
    ) -> None:
        _, selected = self._checked_queue(context, revision, index)
        expected_uri = _resource(selected[0])
        self._send(context, mutation, "avTransport", "SetAVTransportURI", [
            ("InstanceID", 0), ("CurrentURI", f"x-rincon-queue:{context.target.speaker_id}#0"),
            ("CurrentURIMetaData", ""),
        ])
        def queue_source_matches():
            self._checked_queue(context, revision, index)
            current = self.transport.call(context.target.ip, "avTransport", "GetMediaInfo", [("InstanceID", 0)])
            if current.get("CurrentURI") != f"x-rincon-queue:{context.target.speaker_id}#0":
                raise SonosError("The queue source was not confirmed; no further seek or play command was sent.")

        def selection_matches():
            queue_source_matches()
            position = self.transport.call(context.target.ip, "avTransport", "GetPositionInfo", [("InstanceID", 0)])
            if _number(position.get("Track"), "track number") != index + 1 or position.get("TrackURI") != expected_uri:
                raise SonosError("The selected queue item was not confirmed; playback was not started.")

        self._send(
            context, mutation, "avTransport", "Seek",
            [("InstanceID", 0), ("Unit", "TRACK_NR"), ("Target", index + 1)], guard=queue_source_matches,
        )
        self._send(context, mutation, "avTransport", "Play", [("InstanceID", 0), ("Speed", 1)], guard=selection_matches)

    def add_queue_item(self, speaker_id: str, uri: Optional[str] = None, favorite_id: Optional[str] = None) -> ControlResult:
        uid = normalize_id(speaker_id)
        if (uri is None) == (favorite_id is None):
            raise SonosError("Supply exactly one of uri or favorite_id.")
        if uri is not None:
            uri = validate_uri(uri)
        if favorite_id is not None:
            favorite_id = self._validate_favorite_id(favorite_id)
        with self._lock:
            context = self._context(uid)
            media_uri, metadata, mode = self._media(context, uri, favorite_id)
            if mode == "stream":
                raise SonosError("This favorite is a stream, not a queueable item; use play_sonos_favorite.")
            appended_revision = None
            appended_uri = None

            def operation(mutation):
                nonlocal appended_revision, appended_uri
                change, appended_revision, appended_uri = self._enqueue(context, mutation, media_uri, metadata)
                return change

            def verify(change):
                topology = self._check_context(context)
                if change is None or change.index is None or appended_revision is None:
                    raise SonosError("The queue append did not establish an insertion position.")
                page, items = self._checked_queue(context, appended_revision, change.index)
                if page.total_matches != change.new_size or _resource(items[0]) != appended_uri:
                    raise SonosError("The appended queue content changed during verification.")
                return topology, None, page

            return self._execute(context, "add_queue_item", operation, verify)

    def play_favorite(self, speaker_id: str, favorite_id: str) -> ControlResult:
        uid = normalize_id(speaker_id)
        favorite_id = self._validate_favorite_id(favorite_id)
        with self._lock:
            context = self._context(uid)
            media_uri, metadata, mode = self._media(context, None, favorite_id)
            selection = None

            def operation(mutation):
                nonlocal selection
                if mode == "stream":
                    self._play_uri_steps(context, mutation, media_uri, metadata)
                    return None
                change, revision, track_uri = self._enqueue(context, mutation, media_uri, metadata)
                if change.index is None:
                    raise SonosError("The favorite append did not establish an insertion position.")
                selection = (change.index, revision, track_uri)
                self._select_queue(context, mutation, change.index, revision)
                return change

            def verify(change):
                topology, snapshot = self._observe(context)
                playback = snapshot.state.playback
                if playback is None or playback.transport_state != "PLAYING":
                    raise SonosError("Favorite playback was not confirmed.")
                if mode == "stream":
                    if snapshot.source_uri != media_uri:
                        raise SonosError("The requested favorite stream was not confirmed.")
                    return topology, snapshot.state, None
                if selection is None:
                    raise SonosError("The favorite queue selection was not established.")
                index, revision, track_uri = selection
                page, _ = self._checked_queue(context, revision, index)
                if playback.track is None or playback.track.queue_index != index or snapshot.track_uri != track_uri:
                    raise SonosError("The requested favorite queue item was not confirmed.")
                return topology, snapshot.state, page

            return self._execute(context, "play_favorite", operation, verify)

    def play_queue_item(self, speaker_id: str, index: int, queue_revision: QueueRevision) -> ControlResult:
        uid = normalize_id(speaker_id)
        _integer(index, "index")
        revision = self._revision(queue_revision)
        with self._lock:
            context = self._context(uid)
            _, items = self._checked_queue(context, revision, index)
            track_uri = _resource(items[0])

            def operation(mutation):
                self._select_queue(context, mutation, index, revision)

            def verify(change):
                topology, snapshot = self._observe(context)
                page, _ = self._checked_queue(context, revision, index)
                playback = snapshot.state.playback
                if (
                    playback is None or playback.transport_state != "PLAYING" or playback.track is None
                    or playback.track.queue_index != index or snapshot.track_uri != track_uri
                ):
                    raise SonosError("The requested queue item and playing state were not confirmed.")
                return topology, snapshot.state, page

            return self._execute(context, "play_queue_item", operation, verify)

    @staticmethod
    def _signature(item: DidlObject) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        return (
            _resource(item) if item.resources else None, _text(getattr(item, "title", None)),
            _artist(item), _text(getattr(item, "album", None)),
        )

    def remove_queue_item(self, speaker_id: str, index: int, queue_revision: QueueRevision) -> ControlResult:
        uid = normalize_id(speaker_id)
        _integer(index, "index")
        revision = self._revision(queue_revision)
        with self._lock:
            context = self._context(uid)
            before, _ = self._checked_queue(context, revision, index)
            self._require_atomic_revision(revision)
            following = None
            if index + 1 < before.total_matches:
                _, items = self._checked_queue(context, revision, index + 1)
                following = self._signature(items[0])

            def operation(mutation):
                self._checked_queue(context, revision, index)
                self._send(context, mutation, "avTransport", "RemoveTrackRangeFromQueue", [
                    ("InstanceID", 0), ("UpdateID", revision.update_id),
                    ("StartingIndex", index + 1), ("NumberOfTracks", 1),
                ])
                return QueueChange(before.total_matches, before.total_matches - 1, index=index)

            def verify(change):
                topology = self._check_context(context)
                page, items = self._queue(context.target, uid, index, 1)
                if page.total_matches != before.total_matches - 1 or page.queue_revision == revision:
                    raise SonosError("Queue removal was not confirmed.")
                if following is not None and (not items or self._signature(items[0]) != following):
                    raise SonosError("The expected following queue item was not observed after removal.")
                if following is None and items:
                    raise SonosError("The final queue item was not removed as expected.")
                return topology, None, page

            return self._execute(context, "remove_queue_item", operation, verify)

    def move_queue_item(self, speaker_id: str, from_index: int, to_index: int, queue_revision: QueueRevision) -> ControlResult:
        uid = normalize_id(speaker_id)
        _integer(from_index, "from_index")
        _integer(to_index, "to_index")
        revision = self._revision(queue_revision)
        with self._lock:
            context = self._context(uid)
            before, items = self._checked_queue(context, revision, from_index)
            _integer(to_index, "to_index", 0, before.total_matches - 1)
            if from_index != to_index:
                self._require_atomic_revision(revision)
            expected = self._signature(items[0])

            def operation(mutation):
                self._checked_queue(context, revision, from_index)
                if from_index != to_index:
                    self._send(context, mutation, "avTransport", "ReorderTracksInQueue", [
                        ("InstanceID", 0), ("StartingIndex", from_index + 1), ("NumberOfTracks", 1),
                        ("InsertBefore", to_index + (2 if to_index > from_index else 1)),
                        ("UpdateID", revision.update_id),
                    ])
                return QueueChange(before.total_matches, before.total_matches, from_index=from_index, to_index=to_index)

            def verify(change):
                topology = self._check_context(context)
                page, items = self._queue(context.target, uid, to_index, 1)
                if page.total_matches != before.total_matches or not items or self._signature(items[0]) != expected:
                    raise SonosError("The moved item was not observed at the requested final index.")
                if from_index != to_index and page.queue_revision == revision:
                    raise SonosError("The queue revision did not change after reordering.")
                return topology, None, page

            return self._execute(context, "move_queue_item", operation, verify)

    def clear_queue(self, speaker_id: str, queue_revision: QueueRevision) -> ControlResult:
        uid = normalize_id(speaker_id)
        revision = self._revision(queue_revision)
        with self._lock:
            context = self._context(uid)
            before, _ = self._checked_queue(context, revision)
            if before.total_matches:
                self._require_atomic_revision(revision)

            def operation(mutation):
                self._checked_queue(context, revision)
                if before.total_matches:
                    self._send(context, mutation, "avTransport", "RemoveTrackRangeFromQueue", [
                        ("InstanceID", 0), ("UpdateID", revision.update_id),
                        ("StartingIndex", 1), ("NumberOfTracks", before.total_matches),
                    ])
                return QueueChange(before.total_matches, 0)

            def verify(change):
                topology = self._check_context(context)
                page, _ = self._queue(context.target, uid, 0, 1)
                if page.total_matches or before.total_matches and page.queue_revision == revision:
                    raise SonosError("An empty queue with the expected revision change was not observed.")
                return topology, None, page

            return self._execute(context, "clear_queue", operation, verify)

    def _group_change(self, speaker_id: str, target_speaker_id: Optional[str]) -> ControlResult:
        uid = normalize_id(speaker_id)
        target_id = normalize_id(target_speaker_id) if target_speaker_id is not None else None
        with self._lock:
            context = self._context(uid, room_only=True)
            source_group = context.watched_groups[0]
            room_members = {
                member for member, zone in context.topology.zones.items() if zone.room_id == uid
            }
            target = None
            if target_id is not None:
                if target_id not in context.topology.zones:
                    raise SonosError("The target is not in the same discovered Sonos household.")
                target_context = self._context(target_id)
                target = target_context.target
                target_group = target_context.watched_groups[0]
                unchanged = target_group == source_group
                if not unchanged:
                    context.watched_groups.append(target_group)
                desired = set(target_group.speaker_ids) | room_members
                expected_coordinator = target.speaker_id
            else:
                unchanged = set(source_group.speaker_ids) == room_members
                desired = room_members
                expected_coordinator = uid
            remaining = set(source_group.speaker_ids) - room_members

            def operation(mutation):
                if unchanged:
                    return
                if target is not None:
                    self._describe(target.ip, target.speaker_id)
                    self._send(context, mutation, "avTransport", "SetAVTransportURI", [
                        ("InstanceID", 0), ("CurrentURI", f"x-rincon:{target.speaker_id}"), ("CurrentURIMetaData", ""),
                    ])
                else:
                    self._send(context, mutation, "avTransport", "BecomeCoordinatorOfStandaloneGroup", [("InstanceID", 0)])

            def verify(change):
                identity = self._describe(context.requested.ip, uid)
                topology = self._topology(identity)
                actual = topology.groups[topology.zones[uid].coordinator_id]
                if unchanged:
                    if actual != source_group:
                        raise SonosError("The group changed while checking an already matching membership.")
                elif actual.coordinator_id != expected_coordinator or set(actual.speaker_ids) != desired:
                    raise SonosError("The requested room's resulting group membership was not confirmed.")
                if not unchanged and remaining and not any(set(group.speaker_ids) == remaining for group in topology.groups.values()):
                    raise SonosError("Other source-group rooms did not remain together as expected.")
                return topology, None, None

            return self._execute(
                context, "join_group" if target_id is not None else "leave_group",
                operation, verify, group_change=True,
            )

    def join_group(self, speaker_id: str, target_speaker_id: str) -> ControlResult:
        return self._group_change(speaker_id, target_speaker_id)

    def leave_group(self, speaker_id: str) -> ControlResult:
        return self._group_change(speaker_id, None)
