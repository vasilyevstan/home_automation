import asyncio
import io
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from mcp import Client

import sonos as api
import sonos_mcp as server


UID = "RINCON_02000000000101400"
NOW = "2026-01-01T12:00:00+00:00"
READS = {"list_sonos_speakers", "get_sonos_speaker", "list_sonos_favorites", "get_sonos_queue"}
WRITES = {
    "set_sonos_playback", "set_sonos_volume", "set_sonos_mute", "play_sonos_uri",
    "play_sonos_favorite", "join_sonos_group", "leave_sonos_group",
    "add_sonos_queue_item", "remove_sonos_queue_item", "move_sonos_queue_item",
    "clear_sonos_queue", "play_sonos_queue_item",
}


def state():
    return api.SpeakerState(
        "local_sonos", NOW,
        api.SpeakerIdentity(UID, "192.168.50.2", "Synthetic room", "Sonos One", "S18", "synthetic-fw", "synthetic-hw", NOW),
        api.Zone(UID, "192.168.50.2", "Synthetic room", UID, UID, True, False, None),
        api.SonosGroup(UID, [UID], [UID]),
        api.RenderingState(UID, 20, False, []),
        api.PlaybackState(UID, "STOPPED", "OK", None, None, ["Play"], []), [],
    )


def queue():
    return api.QueuePage(UID, UID, NOW, api.QueueRevision(UID, 7), 0, 50, 0, 0, None, [])


def control():
    return api.ControlResult(
        "set_volume", UID, UID, [UID], True, True, True, ["SetVolume"],
        [state().group], [state().group], state(), None, None, [],
    )


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = Mock(spec=api.SonosClient)
        self.backend.list_speakers.return_value = api.SpeakerInventory("local_sonos", NOW, True, NOW, [], [], [])
        self.backend.get_speaker.return_value = state()
        self.backend.list_favorites.return_value = api.FavoritesPage(UID, NOW, 0, 50, 0, 0, None, [], [])
        self.backend.get_queue.return_value = queue()
        for name in (
            "set_playback", "set_volume", "set_mute", "play_uri", "play_favorite",
            "join_group", "leave_group", "add_queue_item", "remove_queue_item",
            "move_queue_item", "clear_queue", "play_queue_item",
        ):
            getattr(self.backend, name).return_value = control()

    async def test_exact_tools_schemas_and_truthful_annotations(self):
        async with Client(server.mcp, raise_exceptions=True) as client:
            listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        self.assertEqual(set(tools), READS | WRITES)
        for name, tool in tools.items():
            self.assertEqual(tool.annotations.read_only_hint, name in READS)
            self.assertEqual(tool.annotations.destructive_hint, name in WRITES)
            self.assertEqual(tool.annotations.idempotent_hint, name in READS | {"set_sonos_volume", "set_sonos_mute"})
            self.assertTrue(tool.annotations.open_world_hint)
            self.assertNotIn("password", json.dumps(tool.input_schema))
        volume = tools["set_sonos_volume"].input_schema
        self.assertEqual(set(volume["required"]), {"speaker_id", "volume"})
        self.assertEqual(volume["properties"]["volume"]["minimum"], 0)
        self.assertEqual(volume["properties"]["volume"]["maximum"], 100)
        move = tools["move_sonos_queue_item"].input_schema
        self.assertEqual(set(move["required"]), {"speaker_id", "from_index", "to_index", "queue_revision"})
        revision = move["$defs"]["QueueRevision"]
        self.assertEqual(set(revision["required"]), {"coordinator_id", "update_id"})
        self.assertEqual(revision["properties"]["update_id"]["minimum"], 0)
        self.assertEqual(tools["get_sonos_queue"].input_schema["properties"]["limit"]["maximum"], 100)

    async def test_all_tools_delegate_and_text_matches_structured_output(self):
        revision = {"coordinator_id": UID, "update_id": 7}
        calls = [
            ("list_sonos_speakers", {}, "list_speakers", (True,)),
            ("get_sonos_speaker", {"speaker_id": UID}, "get_speaker", (UID,)),
            ("list_sonos_favorites", {"speaker_id": UID}, "list_favorites", (UID, 0, 50)),
            ("get_sonos_queue", {"speaker_id": UID}, "get_queue", (UID, 0, 50)),
            ("set_sonos_playback", {"speaker_id": UID, "action": "pause"}, "set_playback", (UID, "pause")),
            ("set_sonos_volume", {"speaker_id": UID, "volume": 8}, "set_volume", (UID, 8)),
            ("set_sonos_mute", {"speaker_id": UID, "muted": True}, "set_mute", (UID, True)),
            ("play_sonos_uri", {"speaker_id": UID, "uri": "https://example.test/a.mp3"}, "play_uri", (UID, "https://example.test/a.mp3")),
            ("play_sonos_favorite", {"speaker_id": UID, "favorite_id": "FV:2/1"}, "play_favorite", (UID, "FV:2/1")),
            ("join_sonos_group", {"speaker_id": UID, "target_speaker_id": UID}, "join_group", (UID, UID)),
            ("leave_sonos_group", {"speaker_id": UID}, "leave_group", (UID,)),
            ("add_sonos_queue_item", {"speaker_id": UID, "favorite_id": "FV:2/1"}, "add_queue_item", (UID, None, "FV:2/1")),
            ("remove_sonos_queue_item", {"speaker_id": UID, "index": 1, "queue_revision": revision}, "remove_queue_item", (UID, 1, api.QueueRevision(UID, 7))),
            ("move_sonos_queue_item", {"speaker_id": UID, "from_index": 1, "to_index": 2, "queue_revision": revision}, "move_queue_item", (UID, 1, 2, api.QueueRevision(UID, 7))),
            ("clear_sonos_queue", {"speaker_id": UID, "queue_revision": revision}, "clear_queue", (UID, api.QueueRevision(UID, 7))),
            ("play_sonos_queue_item", {"speaker_id": UID, "index": 1, "queue_revision": revision}, "play_queue_item", (UID, 1, api.QueueRevision(UID, 7))),
        ]
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, arguments, method, expected in calls:
                    with self.subTest(tool=name):
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(result.is_error)
                        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                        getattr(self.backend, method).assert_called_once_with(*expected)

    async def test_invalid_schema_inputs_never_reach_the_backend(self):
        cases = [
            ("list_sonos_speakers", {"refresh": "false"}),
            ("get_sonos_speaker", {"speaker_id": 123}),
            ("get_sonos_queue", {"speaker_id": UID, "start": True}),
            ("get_sonos_queue", {"speaker_id": UID, "limit": 101}),
            ("set_sonos_playback", {"speaker_id": UID, "action": "toggle"}),
            ("set_sonos_volume", {"speaker_id": UID}),
            ("set_sonos_volume", {"speaker_id": UID, "volume": True}),
            ("set_sonos_volume", {"speaker_id": UID, "volume": "10"}),
            ("set_sonos_volume", {"speaker_id": UID, "volume": 101}),
            ("set_sonos_mute", {"speaker_id": UID, "muted": 1}),
            ("set_sonos_mute", {"speaker_id": UID, "muted": "false"}),
            ("move_sonos_queue_item", {"speaker_id": UID, "from_index": 0, "to_index": 1}),
            ("clear_sonos_queue", {"speaker_id": UID, "queue_revision": {"coordinator_id": UID, "update_id": True}}),
            ("clear_sonos_queue", {"speaker_id": UID, "queue_revision": {"coordinator_id": UID, "update_id": "7"}}),
            ("clear_sonos_queue", {"speaker_id": UID, "queue_revision": {"coordinator_id": UID, "update_id": -1}}),
            ("clear_sonos_queue", {"speaker_id": UID, "queue_revision": {"update_id": 7}}),
        ]
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, arguments in cases:
                    with self.subTest(tool=name, arguments=arguments):
                        result = await client.call_tool(name, arguments)
                        self.assertTrue(result.is_error)
        self.assertEqual(self.backend.mock_calls, [])

    async def test_expected_and_uncertain_failures_are_tool_errors(self):
        self.backend.get_speaker.side_effect = api.SonosError("Synthetic unavailable speaker.")
        self.backend.set_volume.side_effect = api.SonosError("The command may have taken effect; read state before another action.")
        self.backend.clear_queue.side_effect = api.SonosError("The queue revision or coordinator changed.")
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, arguments in (
                    ("get_sonos_speaker", {"speaker_id": UID}),
                    ("set_sonos_volume", {"speaker_id": UID, "volume": 8}),
                    ("clear_sonos_queue", {"speaker_id": UID, "queue_revision": {"coordinator_id": UID, "update_id": 7}}),
                ):
                    result = await client.call_tool(name, arguments)
                    self.assertTrue(result.is_error)
                    self.assertIsNone(result.structured_content)

    async def test_cancellation_does_not_release_the_worker_owned_write_lock(self):
        from test_sonos import FakeTransport, NOW as CLOCK, SOCO_AVAILABLE

        if not SOCO_AVAILABLE:
            self.skipTest("Install the sonos extra for worker cancellation coverage.")
        transport = FakeTransport()
        backend = api.SonosClient(transport, lambda: CLOCK, lambda delay: None)
        entered, release = threading.Event(), threading.Event()

        def hold(uid, action, args):
            if action == "SetVolume" and args["DesiredVolume"] == 8:
                entered.set()
                if not release.wait(3):
                    raise api.SonosError("Synthetic cancellation test timeout.")

        transport.after_write = hold
        first = asyncio.create_task(server._call(backend.set_volume, UID, 8))
        second = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(server._call(backend.set_volume, UID, 9))
            await asyncio.sleep(0.02)
            self.assertEqual(len(transport.writes), 1)
        finally:
            release.set()
            if second is not None:
                result = await second
                self.assertTrue(result.verified)
            elif not first.done():
                await first
        self.assertEqual([call[3]["DesiredVolume"] for call in transport.writes], [8, 9])


class StartupTests(unittest.TestCase):
    def test_missing_optional_runtime_fails_on_stderr(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sonos_mcp.ensure_runtime", side_effect=api.SonosError("Install the sonos extra.")), patch.object(
            server.mcp, "run"
        ) as run, patch("sys.stdout", output), patch("sys.stderr", errors), self.assertRaises(SystemExit) as caught:
            server.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Install the sonos extra", errors.getvalue())
        run.assert_not_called()

    def test_import_and_startup_do_not_discover_or_load_sdk_at_import(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys, sonos_mcp; assert 'soco' not in sys.modules"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        with patch("sonos_mcp.ensure_runtime"), patch.object(server.mcp, "run") as run, patch.object(
            server._client.transport, "discover"
        ) as discover:
            server.main()
        run.assert_called_once_with(transport="stdio")
        discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
