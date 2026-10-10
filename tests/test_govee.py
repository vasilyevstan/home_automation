import asyncio
import json
import unittest
from collections import defaultdict
from datetime import datetime

import govee as api
from nordpool_ee import UTC


DEVICE_ID = "AA:BB:CC:DD:EE:01"
OTHER_ID = "AA:BB:CC:DD:EE:02"
IP = "192.168.50.20"
OTHER_IP = "192.168.50.21"
NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def packet(command, data, ip=IP, port=api.CONTROL_PORT):
    return api.Datagram(
        json.dumps({"msg": {"cmd": command, "data": data}}).encode(),
        ip,
        port,
    )


def scan(device_id=DEVICE_ID, ip=IP, sku="H6072"):
    return packet(
        "scan",
        {
            "ip": ip,
            "device": device_id,
            "sku": sku,
            "bleVersionHard": "1.00.01",
            "bleVersionSoft": "1.02.03",
            "wifiVersionHard": "1.00.00",
            "wifiVersionSoft": "1.03.05",
        },
        ip,
    )


def status(
    *,
    on=True,
    brightness=60,
    color=(10, 20, 30),
    color_temperature=0,
    ip=IP,
):
    return packet(
        "devStatus",
        {
            "onOff": 1 if on else 0,
            "brightness": brightness,
            "color": {"r": color[0], "g": color[1], "b": color[2]},
            "colorTemInKelvin": color_temperature,
        },
        ip,
    )


def command(payload):
    return json.loads(payload.decode())["msg"]


class FakeTransport:
    def __init__(self):
        self.queues = defaultdict(list)
        self.exchange_calls = []
        self.send_calls = []
        self.send_error = None

    def queue(self, command_name, *responses):
        self.queues[command_name].append(list(responses))

    async def exchange(self, payload, targets, timeout):
        message = command(payload)
        self.exchange_calls.append((message, tuple(targets), timeout))
        queue = self.queues[message["cmd"]]
        if not queue:
            raise AssertionError(f"No queued response for {message['cmd']}")
        response = queue.pop(0)
        if len(response) == 1 and isinstance(response[0], Exception):
            raise response[0]
        return response

    async def send(self, payload, target):
        self.send_calls.append((command(payload), target))
        if self.send_error is not None:
            raise self.send_error


class IdentityAndDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def client(self, transport):
        return api.GoveeClient(transport, lambda: NOW)

    def test_exact_device_id_validation(self):
        self.assertEqual(api.normalize_device_id(DEVICE_ID), DEVICE_ID)
        for value in (
            "",
            " bedroom ",
            "bedroom light",
            "../device",
            "x" * 129,
            None,
            123,
        ):
            with self.subTest(value=value), self.assertRaises(api.GoveeError):
                api.normalize_device_id(value)

    async def test_discovery_parses_identity_and_cache_without_control(self):
        transport = FakeTransport()
        transport.queue(
            "scan",
            scan(),
            packet("devStatus", {"onOff": 1}),
            api.Datagram(b"{broken", OTHER_IP, api.RESPONSE_PORT),
        )
        client = self.client(transport)

        inventory = await client.list_devices()
        cached = await client.list_devices(False)

        self.assertTrue(inventory.discovery_performed)
        self.assertEqual(len(inventory.devices), 1)
        self.assertEqual(inventory.devices[0].device_id, DEVICE_ID)
        self.assertEqual(inventory.devices[0].ip, IP)
        self.assertEqual(inventory.devices[0].sku, "H6072")
        self.assertEqual(inventory.devices[0].last_seen, NOW.isoformat())
        self.assertIn("Ignored 1 invalid", inventory.warnings[0])
        self.assertFalse(cached.discovery_performed)
        self.assertEqual(cached.devices, inventory.devices)
        self.assertEqual(len(transport.exchange_calls), 1)
        self.assertEqual(transport.send_calls, [])
        self.assertEqual(
            transport.exchange_calls[0][1],
            (
                (api.MULTICAST_GROUP, api.DISCOVERY_PORT),
                (api.BROADCAST_ADDRESS, api.DISCOVERY_PORT),
            ),
        )

    async def test_empty_and_spoofed_discovery_are_warnings_not_false_state(self):
        transport = FakeTransport()
        advertised_other = scan(ip=OTHER_IP)
        transport.queue(
            "scan",
            api.Datagram(advertised_other.payload, IP, advertised_other.source_port),
        )
        inventory = await self.client(transport).list_devices()
        self.assertEqual(inventory.devices, [])
        self.assertTrue(any("No Govee devices responded" in item for item in inventory.warnings))
        self.assertTrue(any("Ignored 1 invalid" in item for item in inventory.warnings))

    async def test_conflicting_id_or_address_fails_without_cache_update(self):
        for responses in (
            (scan(), scan(ip=OTHER_IP)),
            (scan(), scan(device_id=OTHER_ID)),
        ):
            with self.subTest(responses=responses):
                transport = FakeTransport()
                transport.queue("scan", *responses)
                with self.assertRaisesRegex(api.GoveeError, "conflicting"):
                    await self.client(transport).list_devices()

    async def test_invalid_refresh_type_and_empty_cache_are_explicit(self):
        client = self.client(FakeTransport())
        cached = await client.list_devices(False)
        self.assertEqual(cached.devices, [])
        self.assertTrue(any("No devices are cached" in item for item in cached.warnings))
        for value in ("false", 0, None):
            with self.subTest(value=value), self.assertRaises(api.GoveeError):
                await client.list_devices(value)


class StatusTests(unittest.IsolatedAsyncioTestCase):
    def client(self, transport):
        return api.GoveeClient(transport, lambda: NOW)

    async def test_status_resolves_fresh_id_and_preserves_rgb_mode_semantics(self):
        transport = FakeTransport()
        transport.queue("scan", scan())
        transport.queue(
            "devStatus",
            packet("turn", {"value": 1}),
            status(color=(1, 2, 3), color_temperature=0),
        )
        result = await self.client(transport).get_device(DEVICE_ID)
        self.assertTrue(result.is_on)
        self.assertEqual(result.brightness, 60)
        self.assertEqual(result.color, api.RGBColor(1, 2, 3))
        self.assertIsNone(result.color_temperature_kelvin)
        self.assertEqual(result.active_color_mode, "rgb")
        self.assertEqual(result.unavailable, [])
        self.assertEqual([call[0]["cmd"] for call in transport.exchange_calls], ["scan", "devStatus"])
        self.assertEqual(transport.send_calls, [])

    async def test_missing_optional_fields_are_null_with_reasons(self):
        transport = FakeTransport()
        transport.queue("scan", scan())
        transport.queue(
            "devStatus",
            packet("devStatus", {"onOff": 1, "colorTemInKelvin": 0}),
        )
        result = await self.client(transport).get_device(DEVICE_ID)
        self.assertIsNone(result.brightness)
        self.assertIsNone(result.color)
        self.assertEqual(
            {item.field for item in result.unavailable},
            {"brightness", "color"},
        )
        self.assertTrue(any("null values are not zero" in item for item in result.warnings))

    async def test_temperature_mode_and_invalid_core_status(self):
        transport = FakeTransport()
        transport.queue("scan", scan())
        transport.queue(
            "devStatus",
            status(color=(0, 0, 0), color_temperature=4200),
        )
        result = await self.client(transport).get_device(DEVICE_ID)
        self.assertEqual(result.active_color_mode, "color_temperature")
        self.assertEqual(result.color_temperature_kelvin, 4200)

        invalid = FakeTransport()
        invalid.queue("scan", scan())
        invalid.queue("devStatus", packet("devStatus", {"onOff": "on"}, ip=IP))
        with self.assertRaisesRegex(api.GoveeError, "no valid status"):
            await self.client(invalid).get_device(DEVICE_ID)

    async def test_wrong_source_and_timeout_do_not_impersonate_target(self):
        transport = FakeTransport()
        transport.queue("scan", scan())
        transport.queue("devStatus", status(ip=OTHER_IP))
        with self.assertRaisesRegex(api.GoveeError, "did not answer"):
            await self.client(transport).get_device(DEVICE_ID)

    async def test_unknown_exact_id_never_sends_status(self):
        transport = FakeTransport()
        transport.queue("scan", scan())
        with self.assertRaisesRegex(api.GoveeError, "not found"):
            await self.client(transport).get_device(OTHER_ID)
        self.assertEqual([call[0]["cmd"] for call in transport.exchange_calls], ["scan"])


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def client(self, transport):
        return api.GoveeClient(transport, lambda: NOW)

    def queue_control(self, transport, before, after=None):
        transport.queue("scan", scan())
        transport.queue("devStatus", before)
        if after is not None:
            transport.queue("devStatus", after)

    async def test_matching_power_is_a_verified_noop(self):
        transport = FakeTransport()
        self.queue_control(transport, status(on=True))
        result = await self.client(transport).set_power(DEVICE_ID, True)
        self.assertFalse(result.command_sent)
        self.assertIsNone(result.acknowledged)
        self.assertTrue(result.verified)
        self.assertEqual(transport.send_calls, [])

    async def test_power_sends_once_and_verifies_fresh_status(self):
        transport = FakeTransport()
        self.queue_control(transport, status(on=False), status(on=True))
        result = await self.client(transport).set_power(DEVICE_ID, True)
        self.assertTrue(result.command_sent)
        self.assertTrue(result.verified)
        self.assertIsNone(result.acknowledged)
        self.assertEqual(len(transport.send_calls), 1)
        message, target = transport.send_calls[0]
        self.assertEqual(message, {"cmd": "turn", "data": {"value": 1}})
        self.assertEqual(target, (IP, api.CONTROL_PORT))
        self.assertEqual(result.before_state.is_on, False)
        self.assertEqual(result.state.is_on, True)

    async def test_non_power_controls_require_an_on_device(self):
        calls = (
            ("brightness", lambda client: client.set_brightness(DEVICE_ID, 50)),
            ("color", lambda client: client.set_color(DEVICE_ID, 1, 2, 3)),
            (
                "temperature",
                lambda client: client.set_color_temperature(DEVICE_ID, 3500),
            ),
        )
        for name, invoke in calls:
            with self.subTest(control=name):
                transport = FakeTransport()
                self.queue_control(transport, status(on=False))
                with self.assertRaisesRegex(api.GoveeError, "is off"):
                    await invoke(self.client(transport))
                self.assertEqual(transport.send_calls, [])

    async def test_brightness_color_and_temperature_payloads_and_verification(self):
        cases = (
            (
                lambda client: client.set_brightness(DEVICE_ID, 75),
                status(brightness=20),
                status(brightness=75),
                {"cmd": "brightness", "data": {"value": 75}},
            ),
            (
                lambda client: client.set_color(DEVICE_ID, 4, 5, 6),
                status(color=(1, 2, 3)),
                status(color=(4, 5, 6)),
                {
                    "cmd": "colorwc",
                    "data": {
                        "color": {"r": 4, "g": 5, "b": 6},
                        "colorTemInKelvin": 0,
                    },
                },
            ),
            (
                lambda client: client.set_color_temperature(DEVICE_ID, 3600),
                status(color_temperature=4200),
                status(color=(0, 0, 0), color_temperature=3600),
                {
                    "cmd": "colorwc",
                    "data": {
                        "color": {"r": 0, "g": 0, "b": 0},
                        "colorTemInKelvin": 3600,
                    },
                },
            ),
        )
        for invoke, before, after, expected in cases:
            with self.subTest(expected=expected):
                transport = FakeTransport()
                self.queue_control(transport, before, after)
                result = await invoke(self.client(transport))
                self.assertTrue(result.verified)
                self.assertEqual(len(transport.send_calls), 1)
                self.assertEqual(transport.send_calls[0][0], expected)

    async def test_invalid_control_values_never_discover_or_send(self):
        client = self.client(FakeTransport())
        calls = (
            lambda: client.set_power(DEVICE_ID, 1),
            lambda: client.set_brightness(DEVICE_ID, 0),
            lambda: client.set_brightness(DEVICE_ID, True),
            lambda: client.set_color(DEVICE_ID, -1, 0, 0),
            lambda: client.set_color(DEVICE_ID, 0, 256, 0),
            lambda: client.set_color_temperature(DEVICE_ID, 1999),
            lambda: client.set_color_temperature(DEVICE_ID, 9001),
        )
        for invoke in calls:
            with self.subTest(invoke=invoke), self.assertRaises(api.GoveeError):
                await invoke()

    async def test_unavailable_capability_never_sends(self):
        transport = FakeTransport()
        transport.queue("scan", scan())
        transport.queue(
            "devStatus",
            packet(
                "devStatus",
                {
                    "onOff": 1,
                    "color": {"r": 1, "g": 2, "b": 3},
                    "colorTemInKelvin": 0,
                },
            ),
        )
        with self.assertRaisesRegex(api.GoveeError, "brightness status"):
            await self.client(transport).set_brightness(DEVICE_ID, 50)
        self.assertEqual(transport.send_calls, [])

    async def test_dispatch_and_readback_failures_are_uncertain_and_not_retried(self):
        transport = FakeTransport()
        self.queue_control(transport, status(on=False))
        transport.send_error = api.GoveeError("synthetic private transport detail")
        with self.assertRaisesRegex(api.GoveeError, "may have taken effect") as caught:
            await self.client(transport).set_power(DEVICE_ID, True)
        self.assertNotIn("synthetic", str(caught.exception))
        self.assertEqual(len(transport.send_calls), 1)

        timeout = FakeTransport()
        self.queue_control(timeout, status(on=False))
        timeout.queue("devStatus")
        with self.assertRaisesRegex(api.GoveeError, "could not be verified"):
            await self.client(timeout).set_power(DEVICE_ID, True)
        self.assertEqual(len(timeout.send_calls), 1)

        mismatch = FakeTransport()
        self.queue_control(mismatch, status(on=False), status(on=False))
        with self.assertRaisesRegex(api.GoveeError, "outcome is uncertain"):
            await self.client(mismatch).set_power(DEVICE_ID, True)
        self.assertEqual(len(mismatch.send_calls), 1)

    async def test_calls_are_serialized_around_shared_response_port(self):
        class SerialTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.maximum = 0

            async def exchange(self, payload, targets, timeout):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await asyncio.sleep(0)
                try:
                    return await super().exchange(payload, targets, timeout)
                finally:
                    self.active -= 1

        transport = SerialTransport()
        for _ in range(2):
            transport.queue("scan", scan())
            transport.queue("devStatus", status())
        client = self.client(transport)
        await asyncio.gather(
            client.get_device(DEVICE_ID),
            client.get_device(DEVICE_ID),
        )
        self.assertEqual(transport.maximum, 1)


if __name__ == "__main__":
    unittest.main()
