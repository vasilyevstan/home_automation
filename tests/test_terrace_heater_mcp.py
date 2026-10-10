import io
import json
import os
import unittest
from unittest.mock import patch

from mcp import Client

import terrace_heater as heater
import terrace_heater_mcp as server
from test_terrace_heater import FakeDevice, LOCAL_KEY, make_client, status_dps


class HeaterProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_exact_tools_and_read_only_status_never_sends_control(self):
        client_impl, device, _, _ = make_client()
        with patch.object(server, "_client", client_impl):
            async with Client(server.mcp, raise_exceptions=True) as client:
                listing = await client.list_tools()
                result = await client.call_tool("get_terrace_heater_status", {})
        tools = {tool.name: tool for tool in listing.tools}
        self.assertEqual(set(tools), {
            "get_terrace_heater_status", "set_terrace_heater_power",
            "set_terrace_heater_temperature", "set_terrace_heater_mode",
            "set_terrace_heater_power_level",
        })
        for name, tool in tools.items():
            self.assertEqual(tool.annotations.read_only_hint, name == "get_terrace_heater_status")
            self.assertEqual(tool.annotations.destructive_hint, name != "get_terrace_heater_status")
            self.assertTrue(tool.annotations.idempotent_hint)
            self.assertNotIn("local_key", tool.input_schema.get("properties", {}))
            self.assertNotIn("ip", tool.input_schema.get("properties", {}))
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertFalse(result.structured_content["enabled"])
        self.assertIsNone(result.structured_content["temperature_measured_at"])
        self.assertEqual(device.writes, [])
        self.assertNotIn(LOCAL_KEY, str(result))

    async def test_controls_have_typed_results_and_exact_native_inputs(self):
        for tool, arguments, expected in (
            ("set_terrace_heater_power", {"enabled": False}, ("11", False)),
            ("set_terrace_heater_temperature", {"temperature_c": 22}, ("13", 22)),
            ("set_terrace_heater_mode", {"mode": "auto"}, ("14", "auto")),
            ("set_terrace_heater_power_level", {"level": "2"}, ("15", "2")),
        ):
            device = FakeDevice(status_dps(True))
            implementation, _, _, _ = make_client(device)
            with patch.object(server, "_client", implementation):
                async with Client(server.mcp, raise_exceptions=True) as client:
                    result = await client.call_tool(tool, arguments)
            self.assertFalse(result.is_error)
            self.assertTrue(result.structured_content["verified"])
            self.assertTrue(result.structured_content["command_sent"])
            self.assertEqual(device.writes, [expected])
            self.assertEqual(json.loads(result.content[0].text), result.structured_content)

    async def test_invalid_input_never_connects_or_writes(self):
        implementation, device, factory, discovery = make_client()
        with patch.object(server, "_client", implementation):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for tool, args in (
                    ("set_terrace_heater_power", {"enabled": "on"}),
                    ("set_terrace_heater_power", {"enabled": 1}),
                    ("set_terrace_heater_temperature", {"temperature_c": True}),
                    ("set_terrace_heater_temperature", {"temperature_c": "22"}),
                    ("set_terrace_heater_temperature", {"temperature_c": 41}),
                    ("set_terrace_heater_temperature", {"temperature_c": 20.5}),
                    ("set_terrace_heater_mode", {"mode": "eco"}),
                    ("set_terrace_heater_power_level", {"level": 1}),
                    ("set_terrace_heater_power_level", {"level": "25%"}),
                ):
                    result = await client.call_tool(tool, args)
                    self.assertTrue(result.is_error)
                    self.assertIsNone(result.structured_content)
        factory.assert_not_called()
        discovery.assert_not_called()
        self.assertEqual(device.writes, [])

    async def test_expected_offline_and_uncertain_control_failures_are_tool_errors(self):
        with patch.object(server._client, "get_status", side_effect=heater.HeaterError("Heater authentication failed.")):
            async with Client(server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool("get_terrace_heater_status", {})
        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertIn("authentication failed", result.content[0].text)
        implementation, device, _, _ = make_client(FakeDevice(apply=False))
        with patch.object(server, "_client", implementation), patch.object(
            heater, "READBACK_TIMEOUT_SECONDS", 0,
        ):
            async with Client(server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool("set_terrace_heater_power", {"enabled": True})
        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertEqual(device.writes, [("11", True)])


class HeaterStartupTests(unittest.TestCase):
    def test_explicit_provisioning_is_loaded_without_startup_device_commands(self):
        environment = {
            "TERRACE_HEATER_DEVICE_ID": "syntheticheater001",
            "TERRACE_HEATER_LOCAL_KEY": LOCAL_KEY,
            "TERRACE_HEATER_CONFIG": "/private/device.json",
        }
        with patch.dict(os.environ, {}, clear=True), patch.object(
            server, "load_environment_file", return_value=environment,
        ) as load, patch.object(server, "_sdk"), patch.object(
            server.mcp, "run",
        ) as run, patch.object(server, "_client") as implementation:
            server.main(["--env-file", "/private/runtime.env"])
            self.assertEqual(os.environ["TERRACE_HEATER_LOCAL_KEY"], LOCAL_KEY)
            self.assertEqual(str(load.call_args.args[0]), "/private/runtime.env")
        run.assert_called_once_with(transport="stdio")
        implementation.get_status.assert_not_called()
        implementation.set_value.assert_not_called()

    def test_mixed_sources_are_rejected_before_reading_files_or_starting_server(self):
        with patch.dict(os.environ, {
            "TERRACE_HEATER_LOCAL_KEY": LOCAL_KEY,
        }, clear=True), patch.object(server, "load_environment_file") as load, patch.object(
            server.mcp, "run",
        ) as run, patch("sys.stderr", new_callable=io.StringIO) as errors:
            with self.assertRaises(SystemExit) as caught:
                server.main(["--env-file", "/private/runtime.env"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("not a mixture", errors.getvalue())
        self.assertNotIn(LOCAL_KEY, errors.getvalue())
        load.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
