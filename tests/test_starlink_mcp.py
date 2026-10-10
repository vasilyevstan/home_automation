import io
import json
import unittest
from unittest.mock import MagicMock, patch

from mcp import Client

import starlink as api
import starlink_mcp as server


ID = "ut00000000-00000000-00000001"
NOW = "2026-10-02T12:00:00+00:00"
READS = {"get_starlink_status", "get_starlink_capabilities", "get_starlink_history", "get_starlink_obstructions"}
WRITES = {"reboot_starlink", "set_starlink_sleep_schedule", "set_starlink_snow_melt_mode"}


def status():
    return api.StarlinkStatus(
        api.SOURCE, NOW, api.Identity(ID, "synthetic-hw", "synthetic-fw"), None, "connected", None,
        api.Reading(1000, "s", "available"), 2,
        api.Throughput(8_000_000, 8, "available"), api.Throughput(500_000, 0.5, "available"),
        api.Reading(25, "ms", "available"), api.Reading(0, "ratio", "available"),
        api.Reading(1000, "Mbps", "available"),
        api.Reading(None, "W", "unavailable", "Only available from history."),
        False, False, [], True, [],
    )


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = MagicMock(spec=api.StarlinkClient)
        self.backend.get_status.return_value = status()
        self.backend.get_capabilities.return_value = api.Capabilities(
            api.SOURCE, NOW, status().device, None, {"get_status": True},
            [api.ControlCapability("reboot", True, False, "unverified", "Disabled by default.")], [],
        )
        self.backend.get_obstructions.return_value = api.Obstructions(
            api.SOURCE, NOW, status().device, False, api.Reading(0, "ratio", "available"),
            api.Reading(900, "s", "available"), api.Reading(None, "s", "unavailable"),
            api.Reading(None, "s", "unavailable"), 2, 2, "FRAME_EARTH", [],
        )
        summary = api.Summary("bps", "available", 3, 0, 2_000_000, 1_000_000, 3_000_000)
        rate = api.ThroughputSummary(summary, api.Summary("Mbps", "available", 3, 0, 2, 1, 3))
        self.backend.get_history.return_value = api.History(
            api.SOURCE, NOW, status().device, 3, 3, 900, 1, 1000, True, rate, rate,
            api.Summary("ms", "available", 3, 0, 25, 24, 26),
            api.Summary("ratio", "available", 3, 0, 0, 0, 0),
            api.Summary("W", "available", 3, 0, 30, 28, 32), None, [], [],
        )
        result = api.ControlResult(
            api.SOURCE, NOW, status().device, "snow_melt", {"snow_melt_mode": "auto"},
            {"snow_melt_mode": "always_off"}, {"snow_melt_mode": "auto"}, True, True, True, [],
        )
        self.backend.set_snow_melt.return_value = result
        self.backend.set_sleep.return_value = result
        self.backend.reboot.return_value = result

    async def test_exact_tools_schemas_and_annotations(self):
        async with Client(server.mcp, raise_exceptions=True) as client:
            listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        self.assertEqual(set(tools), READS | WRITES)
        for name, tool in tools.items():
            self.assertEqual(tool.annotations.read_only_hint, name in READS)
            self.assertEqual(tool.annotations.destructive_hint, name in WRITES)
            self.assertEqual(tool.annotations.idempotent_hint, name != "reboot_starlink")
            self.assertTrue(tool.annotations.open_world_hint)
            for forbidden in ("target", "device_id", "password", "token"):
                self.assertNotIn(forbidden, tool.input_schema["properties"])
        schema = tools["get_starlink_history"].input_schema["properties"]
        self.assertEqual((schema["samples"]["minimum"], schema["samples"]["maximum"]), (1, 900))
        self.assertEqual(tools["reboot_starlink"].input_schema["required"], ["confirm_disruption"])
        self.assertEqual(tools["set_starlink_sleep_schedule"].input_schema["required"], ["enabled"])

    async def test_reads_and_control_results_are_typed_and_text_matches(self):
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, args in (
                    ("get_starlink_status", {}), ("get_starlink_capabilities", {}),
                    ("get_starlink_history", {"samples": 3}), ("get_starlink_obstructions", {}),
                    ("set_starlink_snow_melt_mode", {"mode": "auto"}),
                    ("set_starlink_sleep_schedule", {"enabled": False}),
                    ("reboot_starlink", {"confirm_disruption": True}),
                ):
                    with self.subTest(name=name):
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.is_error)
                        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                        if name == "get_starlink_status":
                            self.assertEqual(result.structured_content["download"]["mbps"], 8)
                            self.assertEqual(result.structured_content["upload"]["mbps"], 0.5)
        self.backend.get_history.assert_called_once_with(3, False)
        self.backend.set_sleep.assert_called_once_with(False, None, None)

    async def test_strict_boolean_enum_bounds_and_required_inputs(self):
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, arguments in (
                    ("reboot_starlink", {}), ("reboot_starlink", {"confirm_disruption": 1}),
                    ("reboot_starlink", {"confirm_disruption": "true"}),
                    ("set_starlink_sleep_schedule", {}), ("set_starlink_sleep_schedule", {"enabled": "false"}),
                    ("set_starlink_sleep_schedule", {"enabled": True, "start_minutes_utc": 1440}),
                    ("set_starlink_sleep_schedule", {"enabled": True, "duration_minutes": 0}),
                    ("set_starlink_snow_melt_mode", {"mode": "off"}),
                    ("get_starlink_history", {"samples": True}), ("get_starlink_history", {"samples": 901}),
                    ("get_starlink_history", {"include_samples": "false"}),
                ):
                    with self.subTest(name=name, arguments=arguments):
                        result = await client.call_tool(name, arguments)
                        self.assertTrue(result.is_error)
        self.assertEqual(self.backend.mock_calls, [])

    async def test_expected_failures_disabled_and_uncertain_writes_are_errors(self):
        for method in ("get_status", "get_capabilities", "get_history", "get_obstructions",
                       "reboot", "set_sleep", "set_snow_melt"):
            getattr(self.backend, method).side_effect = api.StarlinkError(
                "Command may have taken effect; outcome unconfirmed."
            )
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, args in (
                    ("get_starlink_status", {}), ("get_starlink_capabilities", {}),
                    ("get_starlink_history", {}), ("get_starlink_obstructions", {}),
                    ("set_starlink_snow_melt_mode", {"mode": "auto"}),
                    ("set_starlink_sleep_schedule", {"enabled": False}),
                    ("reboot_starlink", {"confirm_disruption": True}),
                ):
                    result = await client.call_tool(name, args)
                    self.assertTrue(result.is_error)
                    self.assertIsNone(result.structured_content)
                    self.assertIn("unconfirmed", result.content[0].text)


class StartupTests(unittest.TestCase):
    def test_missing_dependencies_fail_on_stderr_without_running(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("starlink_mcp.ensure_runtime", side_effect=api.StarlinkError("Install the Starlink runtime.")), patch.object(
            server.mcp, "run"
        ) as run, patch("sys.stdout", output), patch("sys.stderr", errors):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Install", errors.getvalue())
        run.assert_not_called()

    def test_startup_validates_configuration_without_contacting_dish(self):
        with patch("starlink_mcp.ensure_runtime"), patch("starlink_mcp.load_settings"), patch.object(
            server.mcp, "run"
        ) as run, patch("starlink.Connection.__enter__") as connect:
            server.main()
        run.assert_called_once_with(transport="stdio")
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
