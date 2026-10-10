import io
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from mcp import Client

import govee as api
import govee_mcp as server
from nordpool_ee import UTC


DEVICE_ID = "AA:BB:CC:DD:EE:01"
IP = "192.168.50.20"
NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def device():
    return api.GoveeDevice(
        DEVICE_ID,
        IP,
        "H6072",
        "1.00.01",
        "1.02.03",
        "1.00.00",
        "1.03.05",
        NOW.isoformat(),
    )


def state(on=True):
    return api.DeviceState(
        DEVICE_ID,
        IP,
        "H6072",
        on,
        60,
        api.RGBColor(10, 20, 30),
        None,
        "rgb",
        NOW.isoformat(),
        [],
        [],
    )


def result(control="power", requested=None):
    requested = requested or api.RequestedControl(on=True)
    return api.ControlResult(
        DEVICE_ID,
        control,
        requested,
        state(False),
        True,
        None,
        True,
        state(True),
        ["Fresh status confirmed the request."],
    )


class GoveeProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = MagicMock(spec=api.GoveeClient)
        self.backend.list_devices = AsyncMock(
            return_value=api.DeviceInventory(
                "govee_lan", NOW.isoformat(), True, [device()], []
            )
        )
        self.backend.get_device = AsyncMock(return_value=state())
        self.backend.set_power = AsyncMock(return_value=result())
        self.backend.set_brightness = AsyncMock(
            return_value=result(
                "brightness", api.RequestedControl(brightness=75)
            )
        )
        self.backend.set_color = AsyncMock(
            return_value=result(
                "color", api.RequestedControl(color=api.RGBColor(1, 2, 3))
            )
        )
        self.backend.set_color_temperature = AsyncMock(
            return_value=result(
                "color_temperature",
                api.RequestedControl(color_temperature_kelvin=3500),
            )
        )

    async def test_exact_tools_schemas_annotations_and_no_raw_targets(self):
        async with Client(server.mcp, raise_exceptions=True) as client:
            listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        self.assertEqual(
            set(tools),
            {
                "list_govee_devices",
                "get_govee_device",
                "set_govee_power",
                "set_govee_brightness",
                "set_govee_color",
                "set_govee_color_temperature",
            },
        )
        self.assertEqual(
            set(tools["list_govee_devices"].input_schema["properties"]),
            {"refresh"},
        )
        self.assertEqual(
            set(tools["get_govee_device"].input_schema["properties"]),
            {"device_id"},
        )
        self.assertEqual(
            set(tools["set_govee_power"].input_schema["properties"]),
            {"device_id", "on"},
        )
        self.assertEqual(
            set(tools["set_govee_brightness"].input_schema["properties"]),
            {"device_id", "brightness"},
        )
        self.assertEqual(
            set(tools["set_govee_color"].input_schema["properties"]),
            {"device_id", "red", "green", "blue"},
        )
        self.assertEqual(
            set(tools["set_govee_color_temperature"].input_schema["properties"]),
            {"device_id", "kelvin"},
        )
        for name, tool in tools.items():
            read_only = name in {"list_govee_devices", "get_govee_device"}
            self.assertEqual(tool.annotations.read_only_hint, read_only)
            self.assertEqual(tool.annotations.destructive_hint, not read_only)
            self.assertTrue(tool.annotations.idempotent_hint)
            self.assertTrue(tool.annotations.open_world_hint)
            schema = json.dumps(tool.input_schema).lower()
            for forbidden in ("ip_address", "raw_payload", "api_key", "toggle", "scene"):
                self.assertNotIn(forbidden, schema)

    async def test_read_and_control_results_are_structured_and_forwarded(self):
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                inventory = await client.call_tool("list_govee_devices", {})
                cached = await client.call_tool(
                    "list_govee_devices", {"refresh": False}
                )
                reading = await client.call_tool(
                    "get_govee_device", {"device_id": DEVICE_ID}
                )
                power = await client.call_tool(
                    "set_govee_power", {"device_id": DEVICE_ID, "on": True}
                )
                brightness = await client.call_tool(
                    "set_govee_brightness",
                    {"device_id": DEVICE_ID, "brightness": 75},
                )
                color = await client.call_tool(
                    "set_govee_color",
                    {
                        "device_id": DEVICE_ID,
                        "red": 1,
                        "green": 2,
                        "blue": 3,
                    },
                )
                temperature = await client.call_tool(
                    "set_govee_color_temperature",
                    {"device_id": DEVICE_ID, "kelvin": 3500},
                )
        for response in (
            inventory,
            cached,
            reading,
            power,
            brightness,
            color,
            temperature,
        ):
            self.assertFalse(response.is_error)
            self.assertEqual(
                json.loads(response.content[0].text), response.structured_content
            )
        self.assertEqual(inventory.structured_content["devices"][0]["device_id"], DEVICE_ID)
        self.assertEqual(reading.structured_content["active_color_mode"], "rgb")
        self.assertTrue(power.structured_content["verified"])
        self.assertEqual(
            brightness.structured_content["requested"]["brightness"], 75
        )
        self.assertEqual(color.structured_content["requested"]["color"]["red"], 1)
        self.assertEqual(
            temperature.structured_content["requested"][
                "color_temperature_kelvin"
            ],
            3500,
        )
        self.assertEqual(
            [call.args for call in self.backend.list_devices.await_args_list],
            [(True,), (False,)],
        )
        self.backend.get_device.assert_awaited_once_with(DEVICE_ID)
        self.backend.set_power.assert_awaited_once_with(DEVICE_ID, True)
        self.backend.set_brightness.assert_awaited_once_with(DEVICE_ID, 75)
        self.backend.set_color.assert_awaited_once_with(DEVICE_ID, 1, 2, 3)
        self.backend.set_color_temperature.assert_awaited_once_with(DEVICE_ID, 3500)

    async def test_mutations_require_strict_explicit_values(self):
        cases = (
            ("set_govee_power", {"device_id": DEVICE_ID, "on": "true"}),
            ("set_govee_power", {"device_id": DEVICE_ID, "on": 1}),
            (
                "set_govee_brightness",
                {"device_id": DEVICE_ID, "brightness": "75"},
            ),
            ("set_govee_brightness", {"device_id": DEVICE_ID, "brightness": 0}),
            (
                "set_govee_color",
                {"device_id": DEVICE_ID, "red": True, "green": 2, "blue": 3},
            ),
            (
                "set_govee_color",
                {"device_id": DEVICE_ID, "red": 0, "green": 0, "blue": 256},
            ),
            (
                "set_govee_color_temperature",
                {"device_id": DEVICE_ID, "kelvin": 1999},
            ),
            (
                "set_govee_color_temperature",
                {"device_id": DEVICE_ID, "kelvin": "3500"},
            ),
        )
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, arguments in cases:
                    with self.subTest(name=name, arguments=arguments):
                        response = await client.call_tool(name, arguments)
                        self.assertTrue(response.is_error)
        self.backend.set_power.assert_not_awaited()
        self.backend.set_brightness.assert_not_awaited()
        self.backend.set_color.assert_not_awaited()
        self.backend.set_color_temperature.assert_not_awaited()

    async def test_expected_and_uncertain_failures_are_tool_errors(self):
        for method in (
            self.backend.list_devices,
            self.backend.get_device,
            self.backend.set_power,
            self.backend.set_brightness,
            self.backend.set_color,
            self.backend.set_color_temperature,
        ):
            method.side_effect = api.GoveeError(
                "The command may have taken effect; inspect fresh state."
            )
        calls = (
            ("list_govee_devices", {}),
            ("get_govee_device", {"device_id": DEVICE_ID}),
            ("set_govee_power", {"device_id": DEVICE_ID, "on": True}),
            (
                "set_govee_brightness",
                {"device_id": DEVICE_ID, "brightness": 75},
            ),
            (
                "set_govee_color",
                {"device_id": DEVICE_ID, "red": 1, "green": 2, "blue": 3},
            ),
            (
                "set_govee_color_temperature",
                {"device_id": DEVICE_ID, "kelvin": 3500},
            ),
        )
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, arguments in calls:
                    response = await client.call_tool(name, arguments)
                    self.assertTrue(response.is_error)
                    self.assertIsNone(response.structured_content)
                    self.assertIn("may have taken effect", response.content[0].text)


class StartupTests(unittest.TestCase):
    def test_startup_runs_stdio_without_touching_client(self):
        with patch.object(server.mcp, "run") as run, patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output, patch("sys.stderr", new_callable=io.StringIO) as errors:
            server.main()
        run.assert_called_once_with(transport="stdio")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
