import copy
import io
import json
import threading
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import sonos as api

try:
    from soco.data_structures import (
        DidlAudioBroadcast, DidlFavorite, DidlMusicTrack, DidlPlaylistContainer,
        DidlResource, to_didl_string,
    )
except ModuleNotFoundError as error:
    if error.name != "soco":
        raise
    SOCO_AVAILABLE = False
else:
    SOCO_AVAILABLE = True


FIRST = "RINCON_02000000000101400"
SECOND = "RINCON_02000000000201400"
THIRD = "RINCON_02000000000301400"
IP = "192.168.50.2"
OTHER_IP = "192.168.50.3"
NEW_IP = "192.168.50.4"
NOW = datetime(2026, 1, 1, 12, tzinfo=api.UTC)
READS = {
    "GetZoneGroupState", "GetVolume", "GetMute", "GetTransportInfo",
    "GetMediaInfo", "GetPositionInfo", "GetCurrentTransportActions", "Browse",
}


def description(uid=FIRST, name="Example room"):
    return {
        "UDN": "uuid:" + uid, "roomName": name, "modelName": "Sonos One",
        "modelNumber": "S18", "softwareVersion": "synthetic-fw",
        "hardwareVersion": "synthetic-hw",
    }


def track(number):
    return DidlMusicTrack(
        item_id=f"Q:0/{number}", parent_id="Q:0", title=f"Track {number}",
        creator="Synthetic artist", album="Synthetic album",
        resources=[DidlResource(
            uri=f"https://example.test/track-{number}.mp3",
            protocol_info="http-get:*:audio/mpeg:*",
        )],
    )


def favorite(number=1, *, stream=False, container=False):
    cls = DidlAudioBroadcast if stream else DidlPlaylistContainer if container else DidlMusicTrack
    reference = cls(
        item_id=f"media:{number}", parent_id="media", title=f"Favorite {number}",
        resources=[DidlResource(
            uri=f"x-sonosapi-stream:station-{number}" if stream else f"https://example.test/favorite-{number}.mp3",
            protocol_info="x-rincon-playlist:*:*:*" if container else "http-get:*:audio/mpeg:*",
        )],
    )
    item = DidlFavorite(item_id=f"FV:2/{number}", parent_id="FV:2", title=f"Favorite {number}")
    item.reference = reference
    return item


class FakeTransport:
    def __init__(self):
        self.descriptions = {IP: description(), OTHER_IP: description(SECOND)}
        self.addresses = [IP, OTHER_IP]
        self.groups = [[FIRST], [SECOND]]
        self.bonds = {}
        self.hidden = set()
        self.bridges = set()
        self.discovery_calls = 0
        self.description_calls = []
        self.calls = []
        self.faults = {}
        self.hook = None
        self.after_write = None
        self.volumes = {FIRST: 20, SECOND: 30}
        self.mutes = {FIRST: False, SECOND: False}
        self.queues = {FIRST: [track(i) for i in (1, 2, 3)], SECOND: []}
        self.updates = {FIRST: 7, SECOND: 7}
        self.positions = {FIRST: 0, SECOND: 0}
        self.states = {FIRST: "STOPPED", SECOND: "STOPPED"}
        self.sources = {uid: f"x-rincon-queue:{uid}#0" for uid in (FIRST, SECOND)}
        self.actions = "Set, Stop, Pause, Play, Next, Previous, X_DLNA_SeekTrackNr"
        self.favorites = [favorite(), favorite(2, stream=True), favorite(3, container=True)]

    def discover(self):
        self.discovery_calls += 1
        return list(self.addresses)

    def describe(self, ip):
        self.description_calls.append(ip)
        if ip not in self.descriptions:
            raise api.SonosError("Synthetic unreachable device.")
        result = self.descriptions[ip]
        if isinstance(result, Exception):
            raise result
        return dict(result)

    def uid(self, ip):
        return self.descriptions[ip]["UDN"][5:]

    def topology(self):
        root = ET.Element("ZoneGroups")
        for members in self.groups:
            group = ET.SubElement(root, "ZoneGroup", Coordinator=members[0], ID=members[0] + ":0")
            for uid in members:
                host = next(ip for ip, data in self.descriptions.items() if isinstance(data, dict) and data["UDN"] == "uuid:" + uid)
                values = {
                    "UUID": uid, "Location": f"http://{host}:1400/xml/device_description.xml",
                    "ZoneName": self.descriptions[host]["roomName"],
                    "Invisible": "1" if uid in self.hidden else "0",
                    "IsZoneBridge": "1" if uid in self.bridges else "0",
                }
                secondaries = [child for child, primary in self.bonds.items() if primary == uid]
                if secondaries:
                    values["ChannelMapSet"] = ";".join([uid + ":LF"] + [child + ":RF" for child in secondaries])
                ET.SubElement(group, "ZoneGroupMember", values)
        return ET.tostring(root, encoding="unicode")

    def browse(self, uid, args):
        object_id = args["ObjectID"]
        if object_id == "Q:0":
            all_items = [copy.deepcopy(item) for item in self.queues[uid]]
            for index, item in enumerate(all_items):
                item.item_id = f"Q:0/{index + 1}"
        elif object_id == "FV:2":
            all_items = self.favorites
        else:
            all_items = [item for item in self.favorites if item.item_id == object_id]
        start, limit = args["StartingIndex"], args["RequestedCount"]
        items = all_items[start:start + limit]
        return {
            "NumberReturned": str(len(items)), "TotalMatches": str(len(all_items)),
            "UpdateID": str(self.updates[uid]), "Result": to_didl_string(*items),
        }

    def call(self, ip, service, action, arguments):
        args = dict(arguments)
        self.calls.append((ip, service, action, args))
        if self.hook is not None:
            self.hook(ip, action, args)
        fault = self.faults.get((ip, action))
        if fault is not None:
            raise fault
        uid = self.uid(ip)
        if action == "GetZoneGroupState":
            return {"ZoneGroupState": self.topology()}
        if action == "GetVolume":
            return {"CurrentVolume": str(self.volumes[uid])}
        if action == "GetMute":
            return {"CurrentMute": str(int(self.mutes[uid]))}
        if action == "GetTransportInfo":
            return {"CurrentTransportState": self.states[uid], "CurrentTransportStatus": "OK", "CurrentSpeed": "1"}
        if action == "GetMediaInfo":
            return {"CurrentURI": self.sources[uid]}
        if action == "GetPositionInfo":
            index = self.positions[uid]
            item = self.queues[uid][index] if index < len(self.queues[uid]) else None
            return {
                "Track": str(index + 1) if item else "0", "TrackDuration": "0:03:00",
                "RelTime": "0:00:12", "TrackURI": item.resources[0].uri if item else self.sources[uid],
                "TrackMetaData": to_didl_string(item) if item else "",
            }
        if action == "GetCurrentTransportActions":
            return {"Actions": self.actions}
        if action == "Browse":
            return self.browse(uid, args)
        result = self.write(uid, action, args)
        if self.after_write is not None:
            self.after_write(uid, action, args)
        return result

    def write(self, uid, action, args):
        result = {}
        if action == "SetVolume":
            self.volumes[uid] = args["DesiredVolume"]
        elif action == "SetMute":
            self.mutes[uid] = bool(args["DesiredMute"])
        elif action in ("Play", "Pause", "Stop"):
            self.states[uid] = {"Play": "PLAYING", "Pause": "PAUSED_PLAYBACK", "Stop": "STOPPED"}[action]
        elif action in ("Next", "Previous"):
            step = 1 if action == "Next" else -1
            self.positions[uid] = (self.positions[uid] + step) % len(self.queues[uid])
        elif action == "SetAVTransportURI":
            uri = args["CurrentURI"]
            if uri.startswith("x-rincon:"):
                target = uri.split(":", 1)[1]
                self.groups = [[member for member in group if member != uid] for group in self.groups]
                self.groups = [group for group in self.groups if group]
                next(group for group in self.groups if target in group).append(uid)
            else:
                self.sources[uid] = uri
        elif action == "BecomeCoordinatorOfStandaloneGroup":
            self.groups = [[member for member in group if member != uid] for group in self.groups]
            self.groups = [group for group in self.groups if group] + [[uid]]
        elif action == "Seek":
            self.positions[uid] = int(args["Target"]) - 1
        elif action == "AddURIToQueue":
            start = len(self.queues[uid])
            count = 2 if "object.container" in args["EnqueuedURIMetaData"] else 1
            for offset in range(count):
                item = track(start + offset + 4)
                item.resources[0].uri = (
                    f"https://example.test/container-member-{offset}.mp3"
                    if count > 1 else args["EnqueuedURI"]
                )
                self.queues[uid].append(item)
            self.updates[uid] += 1
            result = {
                "FirstTrackNumberEnqueued": str(start + 1), "NumTracksAdded": str(count),
                "NewQueueLength": str(len(self.queues[uid])),
            }
        elif action in ("RemoveTrackRangeFromQueue", "ReorderTracksInQueue"):
            if args["UpdateID"] != self.updates[uid]:
                raise api._UPnPError(1028)
            start = args["StartingIndex"] - 1
            count = args["NumberOfTracks"]
            if action == "RemoveTrackRangeFromQueue":
                del self.queues[uid][start:start + count]
            else:
                insert = args["InsertBefore"] - 1
                moved = self.queues[uid][start:start + count]
                del self.queues[uid][start:start + count]
                if insert > start:
                    insert -= count
                self.queues[uid][insert:insert] = moved
            self.updates[uid] += 1
            result = {"NewUpdateID": str(self.updates[uid])}
        else:
            raise AssertionError(f"Unexpected synthetic action: {action}")
        return result

    @property
    def writes(self):
        return [call for call in self.calls if call[2] not in READS]


class InputTests(unittest.TestCase):
    def test_ids_and_private_addresses(self):
        self.assertEqual(api.normalize_id(FIRST.lower()), FIRST)
        for value in ("Example room", IP, "", None, True):
            with self.subTest(value=value), self.assertRaises(api.SonosError):
                api.normalize_id(value)
        for value in ("127.0.0.1", "8.8.8.8", "169.254.1.1", "224.0.0.1", "::1", None):
            with self.subTest(value=value), self.assertRaises(api.SonosError):
                api._ip(value)

    def test_urls_and_redaction(self):
        good = "https://example.test/song.mp3?token=synthetic-secret"
        self.assertEqual(api.validate_uri(good), good)
        self.assertEqual(api._redact_uri(good), "https://example.test/song.mp3?redacted")
        self.assertEqual(api._redact_uri("https://user:synthetic-secret@example.test/a"), "https://example.test/a")
        for value in ("file:///etc/passwd", "x-rincon:" + FIRST, "http://user:password@example.test/a", "https://example.test:99999/a", "https://example.test/a\n", "https:///missing", True):
            with self.subTest(value=value), self.assertRaises(api.SonosError):
                api.validate_uri(value)

    def test_xml_limits_and_entities(self):
        for value in ('<!DOCTYPE x [<!ENTITY test "value">]><x>&test;</x>', b"\xff", "<broken>", "x" * (api.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(value=str(value)[:30]), self.assertRaises(api.SonosError):
                api._xml(value)

    def test_missing_optional_runtime_is_actionable(self):
        with patch("sonos.importlib.util.find_spec", return_value=None), self.assertRaisesRegex(api.SonosError, "--extra sonos"):
            api.ensure_runtime()


@unittest.skipUnless(SOCO_AVAILABLE, "Install the optional sonos extra for SDK/transport tests.")
class ReadTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.now = NOW
        self.client = api.SonosClient(self.transport, lambda: self.now, lambda delay: None)

    def test_read_inventory_details_queue_and_favorites_never_write(self):
        inventory = self.client.list_speakers()
        self.assertEqual(len(inventory.speakers), 2)
        self.assertTrue(all(item.availability == "seen" for item in inventory.speakers))
        state = self.client.get_speaker(FIRST)
        self.assertEqual(state.rendering.volume, 20)
        self.assertFalse(state.rendering.muted)
        self.assertEqual(state.playback.transport_state, "STOPPED")
        self.assertEqual(state.playback.track.position_seconds, 12)
        self.assertEqual(state.playback.track.duration_seconds, 180)
        queue = self.client.get_queue(FIRST, 1, 1)
        self.assertEqual(queue.queue_revision, api.QueueRevision(FIRST, 7))
        self.assertEqual(queue.items[0].index, 1)
        self.assertEqual(queue.next_start, 2)
        favorites = self.client.list_favorites(FIRST)
        self.assertEqual([item.playback_mode for item in favorites.items], ["queue", "stream", "queue"])
        self.assertEqual(self.transport.writes, [])

    def test_unseen_and_cached_metadata_is_explicit(self):
        self.client.list_speakers()
        self.transport.addresses = []
        self.now += timedelta(seconds=40)
        unseen = self.client.list_speakers()
        self.assertTrue(all(item.availability == "not_seen" and item.cache_age_seconds == 40 for item in unseen.speakers))
        calls = (self.transport.discovery_calls, len(self.transport.calls), len(self.transport.description_calls))
        cached = self.client.list_speakers(False)
        self.assertFalse(cached.discovery_performed)
        self.assertTrue(all(item.availability == "not_checked" for item in cached.speakers))
        self.assertEqual(calls, (self.transport.discovery_calls, len(self.transport.calls), len(self.transport.description_calls)))

    def test_ip_reassignment_rediscovery_uses_fresh_udn(self):
        self.client.list_speakers()
        self.transport.descriptions[NEW_IP] = self.transport.descriptions.pop(IP)
        self.transport.addresses = [NEW_IP, OTHER_IP]
        state = self.client.get_speaker(FIRST)
        self.assertEqual(state.identity.ip, NEW_IP)
        self.assertEqual(self.transport.discovery_calls, 2)
        self.assertEqual(self.transport.writes, [])

    def test_identity_conflict_is_not_silently_merged(self):
        self.transport.descriptions[OTHER_IP] = description(FIRST)
        self.transport.groups = [[FIRST]]
        with self.assertRaises(api.SonosError):
            self.client.list_speakers()

    def test_group_state_routes_transport_but_not_rendering(self):
        self.transport.groups = [[FIRST, SECOND]]
        state = self.client.get_speaker(SECOND)
        self.assertEqual(state.playback.coordinator_id, FIRST)
        self.assertEqual(state.rendering.room_id, SECOND)
        self.assertEqual(state.rendering.volume, 30)
        self.assertEqual(self.client.get_queue(SECOND).coordinator_id, FIRST)

    def test_bonded_secondary_is_visible_as_physical_metadata(self):
        self.transport.groups = [[FIRST, SECOND]]
        self.transport.bonds = {SECOND: FIRST}
        self.transport.hidden = {SECOND}
        state = self.client.get_speaker(SECOND)
        self.assertEqual(state.identity.speaker_id, SECOND)
        self.assertEqual(state.topology.room_id, FIRST)
        self.assertEqual(state.topology.channel, "RF")
        self.assertEqual(state.rendering.room_id, FIRST)
        with self.assertRaisesRegex(api.SonosError, "visible room"):
            self.client._context(SECOND)

    def test_missing_optional_rendering_is_not_zero(self):
        self.transport.faults[(IP, "GetVolume")] = api._UPnPError(401)
        state = self.client.get_speaker(FIRST)
        self.assertIsNone(state.rendering.volume)
        self.assertTrue(state.rendering.warnings)
        self.assertFalse(state.rendering.muted)

    def test_artist_metadata_without_creator_is_preserved(self):
        self.transport.queues[FIRST][0] = DidlMusicTrack(
            item_id="Q:0/1", parent_id="Q:0", title="Artist-only metadata",
            artist="Synthetic performer", resources=[DidlResource(
                uri="https://example.test/a.mp3", protocol_info="http-get:*:audio/mpeg:*",
            )],
        )
        self.assertEqual(self.client.get_queue(FIRST).items[0].artist, "Synthetic performer")
        self.assertEqual(self.client.get_speaker(FIRST).playback.track.artist, "Synthetic performer")

    def test_bad_pagination_fails_before_discovery(self):
        for start, limit in ((-1, 1), (True, 1), (0, 0), (0, 101), (0, "50")):
            with self.subTest(start=start, limit=limit), self.assertRaises(api.SonosError):
                self.client.get_queue(FIRST, start, limit)
        self.assertEqual(self.transport.discovery_calls, 0)


class Response(io.BytesIO):
    def __init__(self, content, status=200):
        super().__init__(content)
        self.code = status


@unittest.skipUnless(SOCO_AVAILABLE, "Install the optional sonos extra for SDK/transport tests.")
class TransportTests(unittest.TestCase):
    def test_fresh_description_reads_udn_not_soco_uid_cache(self):
        device = ET.Element("root", xmlns=api.DEVICE_NS)
        child = ET.SubElement(device, "device")
        for name, value in {**description(), "deviceType": "urn:schemas-upnp-org:device:ZonePlayer:1"}.items():
            ET.SubElement(child, name).text = value
        opener = Mock()
        opener.open.return_value = Response(ET.tostring(device))
        result = api.SonosTransport(opener).describe(IP)
        self.assertEqual(result["UDN"], "uuid:" + FIRST)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], api.REQUEST_TIMEOUT)

    def test_soap_write_is_sent_once_and_parsed_without_sdk_http(self):
        payload = f'<s:Envelope xmlns:s="{api.SOAP_NS}"><s:Body><u:SetVolumeResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1"/></s:Body></s:Envelope>'.encode()
        opener = Mock()
        opener.open.return_value = Response(payload)
        with patch("soco.services.requests.post") as sdk_post:
            result = api.SonosTransport(opener).call(IP, "renderingControl", "SetVolume", [
                ("InstanceID", 0), ("Channel", "Master"), ("DesiredVolume", 5),
            ])
        self.assertEqual(result, {})
        opener.open.assert_called_once()
        sdk_post.assert_not_called()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f"http://{IP}:1400/MediaRenderer/RenderingControl/Control")
        self.assertIn(b"<DesiredVolume>5</DesiredVolume>", request.data)

    def test_failed_transport_redacts_exception_and_never_retries(self):
        for error in (URLError("synthetic-private-token"), TimeoutError("synthetic-private-token")):
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(error=type(error)), self.assertRaises(api.SonosError) as caught:
                api.SonosTransport(opener).call(IP, "avTransport", "Next", [("InstanceID", 0)])
            self.assertNotIn("synthetic-private-token", str(caught.exception))
            opener.open.assert_called_once()

    def test_redirect_and_oversize_are_rejected(self):
        for response in (Response(b"", 307), Response(b"x" * (api.MAX_RESPONSE_BYTES + 1))):
            opener = Mock()
            opener.open.return_value = response
            with self.assertRaises(api.SonosError):
                api.SonosTransport(opener).describe(IP)
            opener.open.assert_called_once()
        handler = api._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 307, "", {}, "https://example.test"))


@unittest.skipUnless(SOCO_AVAILABLE, "Install the optional sonos extra for control tests.")
class ControlTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.client = api.SonosClient(self.transport, lambda: NOW, lambda delay: None)

    def revision(self, uid=FIRST):
        return self.client.get_queue(uid).queue_revision

    def test_volume_and_mute_only_change_the_selected_room_in_a_group(self):
        self.transport.groups = [[FIRST, SECOND]]
        volume = self.client.set_volume(SECOND, 9)
        muted = self.client.set_mute(SECOND, True)
        self.assertEqual(self.transport.volumes, {FIRST: 20, SECOND: 9})
        self.assertEqual(self.transport.mutes, {FIRST: False, SECOND: True})
        for result in (volume, muted):
            self.assertTrue(result.verified)
            self.assertEqual(result.execution_speaker_id, SECOND)
            self.assertEqual(result.affected_speaker_ids, [SECOND])
        self.assertEqual([call[0] for call in self.transport.writes], [OTHER_IP, OTHER_IP])

    def test_matching_volume_mute_transport_and_group_states_are_noops(self):
        results = [
            self.client.set_volume(FIRST, 20), self.client.set_mute(FIRST, False),
            self.client.set_playback(FIRST, "stop"), self.client.leave_group(FIRST),
            self.client.join_group(FIRST, FIRST),
        ]
        self.assertTrue(all(result.verified and not result.command_sent and result.acknowledged is None for result in results))
        self.assertEqual(self.transport.writes, [])

    def test_invalid_inputs_do_not_discover_or_write(self):
        invalid = [
            lambda: self.client.set_volume(FIRST, True),
            lambda: self.client.set_volume(FIRST, 101),
            lambda: self.client.set_volume(FIRST, "20"),
            lambda: self.client.set_mute(FIRST, 1),
            lambda: self.client.set_playback(FIRST, "toggle"),
            lambda: self.client.play_uri(FIRST, "x-rincon:" + SECOND),
            lambda: self.client.add_queue_item(FIRST),
            lambda: self.client.add_queue_item(FIRST, "https://example.test/a", "FV:2/1"),
            lambda: self.client.play_favorite(FIRST, "SQ:1"),
            lambda: self.client.play_queue_item(FIRST, True, api.QueueRevision(FIRST, 7)),
            lambda: self.client.clear_queue(FIRST, api.QueueRevision(FIRST, True)),
            lambda: self.client.clear_queue(FIRST, {"coordinator_id": FIRST, "update_id": 7}),
        ]
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(api.SonosError):
                operation()
        self.assertEqual(self.transport.discovery_calls, 0)
        self.assertEqual(self.transport.writes, [])

    def test_playback_uses_coordinator_and_available_actions(self):
        self.transport.groups = [[FIRST, SECOND]]
        result = self.client.set_playback(SECOND, "play")
        self.assertEqual(result.execution_speaker_id, FIRST)
        self.assertEqual(result.affected_speaker_ids, [FIRST, SECOND])
        self.assertEqual(self.transport.writes[-1][0], IP)
        self.assertEqual(result.state.playback.transport_state, "PLAYING")
        self.client.set_playback(SECOND, "pause")
        self.client.set_playback(SECOND, "next")
        self.assertEqual(self.transport.positions[FIRST], 1)
        self.client.set_playback(SECOND, "previous")
        self.assertEqual(self.transport.positions[FIRST], 0)
        self.transport.actions = "Play, Stop"
        count = len(self.transport.writes)
        with self.assertRaisesRegex(api.SonosError, "not currently available"):
            self.client.set_playback(FIRST, "next")
        self.assertEqual(len(self.transport.writes), count)

    def test_uncertain_write_is_never_repeated_even_if_it_took_effect(self):
        def lost_ack(uid, action, args):
            if action == "SetVolume":
                raise api.SonosError("Synthetic acknowledgement timeout.")

        self.transport.after_write = lost_ack
        with self.assertRaisesRegex(api.SonosError, "may have.*taken effect"):
            self.client.set_volume(FIRST, 8)
        self.assertEqual(self.transport.volumes[FIRST], 8)
        self.assertEqual(len(self.transport.writes), 1)

    def test_verification_failure_retries_only_reads(self):
        def unchanged(uid, action, args):
            self.transport.volumes[uid] = 20

        self.transport.after_write = unchanged
        with self.assertRaisesRegex(api.SonosError, "verification failed"):
            self.client.set_volume(FIRST, 8)
        self.assertEqual(len(self.transport.writes), 1)
        self.assertGreaterEqual(sum(call[2] == "GetVolume" for call in self.transport.calls), api.VERIFY_ATTEMPTS)

    def test_group_change_before_dispatch_never_retargets_a_write(self):
        def change_group(ip, action, args):
            if action == "GetCurrentTransportActions":
                self.transport.groups = [[FIRST, SECOND]]

        self.transport.hook = change_group
        with self.assertRaisesRegex(api.SonosError, "group membership or coordinator changed"):
            self.client.set_playback(SECOND, "play")
        self.assertEqual(self.transport.writes, [])

    def test_uri_playback_does_not_clear_queue_and_redacts_query_tokens(self):
        uri = "https://example.test/song.mp3?token=synthetic-private-token"
        result = self.client.play_uri(SECOND, uri)
        self.assertEqual(result.completed_steps, ["SetAVTransportURI", "Play"])
        self.assertEqual(len(self.transport.queues[FIRST]), 3)
        self.assertNotIn("synthetic-private-token", json.dumps(api._redact_uri(uri)))
        self.assertEqual(result.state.playback.source_uri, "https://example.test/song.mp3?redacted")

    def test_partial_source_change_does_not_start_a_different_group(self):
        def regroup(uid, action, args):
            if action == "SetAVTransportURI":
                self.transport.groups = [[FIRST, SECOND]]

        self.transport.after_write = regroup
        with self.assertRaisesRegex(api.SonosError, "partially or fully"):
            self.client.play_uri(SECOND, "https://example.test/song.mp3")
        self.assertEqual([call[2] for call in self.transport.writes], ["SetAVTransportURI"])

    def test_append_does_not_start_playback(self):
        uri = "https://example.test/song.mp3"
        result = self.client.add_queue_item(FIRST, uri=uri)
        self.assertEqual(result.queue_change.index, 3)
        self.assertEqual(result.queue_change.tracks_added, 1)
        self.assertEqual(result.queue.total_matches, 4)
        self.assertEqual(self.transport.states[FIRST], "STOPPED")
        self.assertEqual([call[2] for call in self.transport.writes], ["AddURIToQueue"])
        self.assertEqual(self.transport.queues[FIRST][-1].resources[0].uri, uri)

    def test_favorite_stream_and_container_use_reference_metadata(self):
        stream = self.client.play_favorite(SECOND, "FV:2/2")
        self.assertEqual(stream.completed_steps, ["SetAVTransportURI", "Play"])
        self.assertFalse(self.transport.queues[SECOND])
        metadata = self.transport.writes[0][3]["CurrentURIMetaData"]
        self.assertIn("audioBroadcast", metadata)
        self.assertNotIn("resMD", metadata)
        container = self.client.play_favorite(FIRST, "FV:2/3")
        self.assertEqual(container.completed_steps, ["AddURIToQueue", "SetAVTransportURI", "Seek", "Play"])
        self.assertEqual(container.queue_change.tracks_added, 2)
        self.assertEqual(container.queue_change.index, 3)
        self.assertEqual(container.state.playback.track.queue_index, 3)
        self.assertEqual(container.queue.total_matches, 5)
        self.assertEqual([item.title for item in self.transport.queues[FIRST][:3]], ["Track 1", "Track 2", "Track 3"])

    def test_stream_favorites_cannot_be_appended_and_missing_favorites_fail(self):
        with self.assertRaisesRegex(api.SonosError, "stream"):
            self.client.add_queue_item(FIRST, favorite_id="FV:2/2")
        with self.assertRaisesRegex(api.SonosError, "not found"):
            self.client.play_favorite(FIRST, "FV:2/999")
        self.assertEqual(self.transport.writes, [])

    def test_malformed_favorite_is_listed_with_reason_but_not_played(self):
        bad = DidlFavorite(item_id="FV:2/4", parent_id="FV:2", title="Unsupported")
        self.transport.favorites.append(bad)
        listed = self.client.list_favorites(FIRST)
        self.assertEqual(listed.items[-1].playback_mode, "unsupported")
        self.assertIsNotNone(listed.items[-1].reason)
        with self.assertRaises(api.SonosError):
            self.client.play_favorite(FIRST, "FV:2/4")
        self.assertEqual(self.transport.writes, [])

    def test_queue_selection_uses_explicit_source_seek_play_and_zero_based_index(self):
        result = self.client.play_queue_item(FIRST, 2, self.revision())
        self.assertEqual(result.state.playback.track.queue_index, 2)
        self.assertEqual([call[2] for call in self.transport.writes], ["SetAVTransportURI", "Seek", "Play"])
        self.assertEqual(self.transport.writes[1][3]["Target"], 3)

    def test_queue_change_after_source_selection_stops_before_seek_or_play(self):
        revision = self.revision()

        def change_queue(uid, action, args):
            if action == "SetAVTransportURI":
                self.transport.updates[uid] += 1

        self.transport.after_write = change_queue
        with self.assertRaisesRegex(api.SonosError, "partially or fully"):
            self.client.play_queue_item(FIRST, 1, revision)
        self.assertEqual([call[2] for call in self.transport.writes], ["SetAVTransportURI"])

    def test_source_change_after_seek_never_starts_other_audio(self):
        revision = self.revision()

        def change_source(uid, action, args):
            if action == "Seek":
                self.transport.sources[uid] = "https://example.test/other-audio.mp3"

        self.transport.after_write = change_source
        with self.assertRaisesRegex(api.SonosError, "queue source was not confirmed"):
            self.client.play_queue_item(FIRST, 1, revision)
        self.assertEqual([call[2] for call in self.transport.writes], ["SetAVTransportURI", "Seek"])
        self.assertEqual(self.transport.states[FIRST], "STOPPED")

    def test_stale_and_other_coordinator_revisions_fail_without_writes(self):
        revision = self.revision()
        self.transport.updates[FIRST] += 1
        operations = [
            lambda: self.client.remove_queue_item(FIRST, 0, revision),
            lambda: self.client.move_queue_item(FIRST, 0, 2, revision),
            lambda: self.client.clear_queue(FIRST, revision),
            lambda: self.client.play_queue_item(FIRST, 0, revision),
            lambda: self.client.clear_queue(SECOND, revision),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaisesRegex(api.SonosError, "revision or coordinator changed"):
                operation()
        self.assertEqual(self.transport.writes, [])

    def test_remove_each_index_has_exact_result_and_native_revision_guard(self):
        for index in range(3):
            with self.subTest(index=index):
                self.setUp()
                result = self.client.remove_queue_item(FIRST, index, self.revision())
                expected = [f"Track {i + 1}" for i in range(3) if i != index]
                self.assertEqual([item.title for item in self.transport.queues[FIRST]], expected)
                self.assertEqual(result.queue.total_matches, 2)
                command = self.transport.writes[0]
                self.assertEqual(command[2], "RemoveTrackRangeFromQueue")
                self.assertEqual(command[3]["StartingIndex"], index + 1)
                self.assertEqual(command[3]["UpdateID"], 7)

    def test_move_all_index_pairs_has_exact_order_and_noop_behavior(self):
        for source in range(3):
            for destination in range(3):
                with self.subTest(source=source, destination=destination):
                    self.setUp()
                    expected = ["Track 1", "Track 2", "Track 3"]
                    expected.insert(destination, expected.pop(source))
                    result = self.client.move_queue_item(FIRST, source, destination, self.revision())
                    self.assertEqual([item.title for item in self.transport.queues[FIRST]], expected)
                    self.assertEqual(result.command_sent, source != destination)
                    self.assertEqual(result.queue_change.to_index, destination)
                    if source != destination:
                        self.assertEqual(self.transport.writes[0][3]["UpdateID"], 7)

    def test_clear_uses_guarded_range_and_empty_is_noop(self):
        cleared = self.client.clear_queue(FIRST, self.revision())
        self.assertEqual(cleared.queue.total_matches, 0)
        self.assertEqual(self.transport.writes[0][2], "RemoveTrackRangeFromQueue")
        self.assertEqual(self.transport.writes[0][3], {"InstanceID": 0, "UpdateID": 7, "StartingIndex": 1, "NumberOfTracks": 3})
        count = len(self.transport.writes)
        empty = self.client.clear_queue(SECOND, self.revision(SECOND))
        self.assertFalse(empty.command_sent)
        self.assertEqual(len(self.transport.writes), count)

    def test_zero_native_revision_is_not_used_as_a_destructive_guard(self):
        self.transport.updates[FIRST] = 0
        self.transport.updates[SECOND] = 0
        revision = self.revision()
        for operation in (
            lambda: self.client.remove_queue_item(FIRST, 0, revision),
            lambda: self.client.move_queue_item(FIRST, 0, 2, revision),
            lambda: self.client.clear_queue(FIRST, revision),
        ):
            with self.assertRaisesRegex(api.SonosError, "UpdateID 0"):
                operation()
        self.assertEqual(self.transport.writes, [])
        self.assertFalse(self.client.move_queue_item(FIRST, 0, 0, revision).command_sent)
        self.assertFalse(self.client.clear_queue(SECOND, self.revision(SECOND)).command_sent)

    def test_atomic_queue_conflict_is_not_retried_or_applied(self):
        revision = self.revision()

        def race(ip, action, args):
            if action == "RemoveTrackRangeFromQueue":
                self.transport.updates[FIRST] += 1

        self.transport.hook = race
        with self.assertRaisesRegex(api.SonosError, "No command was automatically retried"):
            self.client.remove_queue_item(FIRST, 0, revision)
        self.assertEqual(len(self.transport.queues[FIRST]), 3)
        self.assertEqual(len(self.transport.writes), 1)

    def test_join_leave_and_coordinator_departure_preserve_other_rooms(self):
        joined = self.client.join_group(FIRST, SECOND)
        self.assertEqual(joined.after_groups[0].coordinator_id, SECOND)
        self.assertEqual(joined.affected_speaker_ids, [FIRST, SECOND])
        left = self.client.leave_group(SECOND)
        self.assertEqual({tuple(group.speaker_ids) for group in left.after_groups}, {(FIRST,), (SECOND,)})
        self.assertEqual([call[2] for call in self.transport.writes], ["SetAVTransportURI", "BecomeCoordinatorOfStandaloneGroup"])

    def test_hidden_secondary_cannot_be_regrouped(self):
        self.transport.groups = [[FIRST, SECOND]]
        self.transport.bonds = {SECOND: FIRST}
        self.transport.hidden = {SECOND}
        with self.assertRaisesRegex(api.SonosError, "visible room"):
            self.client.leave_group(SECOND)
        with self.assertRaisesRegex(api.SonosError, "visible room"):
            self.client.join_group(FIRST, SECOND)
        self.assertEqual(self.transport.writes, [])

    def test_client_serializes_mutations_through_verification(self):
        entered, release = threading.Event(), threading.Event()
        results, failures = [], []

        def hold(uid, action, args):
            if action == "SetVolume" and args["DesiredVolume"] == 8:
                entered.set()
                if not release.wait(2):
                    raise api.SonosError("Synthetic test synchronization timeout.")

        def run(value):
            try:
                results.append(self.client.set_volume(FIRST, value))
            except api.SonosError as error:
                failures.append(error)

        self.transport.after_write = hold
        first = threading.Thread(target=run, args=(8,))
        second = threading.Thread(target=run, args=(9,))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            second.start()
            self.assertEqual(len(self.transport.writes), 1)
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual([call[3]["DesiredVolume"] for call in self.transport.writes], [8, 9])


if __name__ == "__main__":
    unittest.main()
