from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import terrace_heater as heater


DEVICE_ID = "syntheticheater001"
LOCAL_KEY = "synthetic-key-01"
ADDRESS = "10.20.30.40"
PRODUCT = "synthetic-product"


def profile_document():
    return {
        "schema_version": 1,
        "device_id": DEVICE_ID,
        "name": "Synthetic heater",
        "product_id": PRODUCT,
        "protocol_version": "3.3",
        "address_hint": ADDRESS,
        "verified_at": "2026-10-01T12:00:00+00:00",
        "standby_settings_verified": False,
        "fields": {
            "power": {"dp": "11", "kind": "Boolean", "writable": True},
            "temperature": {
                "dp": "12", "kind": "Integer", "writable": False,
                "minimum": -20, "maximum": 80, "step": 1, "scale": 0, "unit": "C",
            },
            "setpoint": {
                "dp": "13", "kind": "Integer", "writable": True,
                "minimum": 5, "maximum": 40, "step": 1, "scale": 0, "unit": "C",
            },
            "mode": {"dp": "14", "kind": "Enum", "writable": True, "values": ["smart", "auto"]},
            "power_level": {"dp": "15", "kind": "Enum", "writable": True, "values": ["1", "2", "3", "4"]},
        },
    }


def make_profile(document=None):
    return heater.parse_profile(document or profile_document(), DEVICE_ID)


def status_dps(enabled=False):
    return {"11": enabled, "12": 17, "13": 21, "14": "smart", "15": "1"}


def onboarding_metadata():
    document = profile_document()
    result = {
        "schema_version": 1,
        "device": {"id": DEVICE_ID, "name": "Synthetic heater", "product_id": PRODUCT},
        "specifications": {"functions": [], "status": []},
        "local_model": {"productKey": PRODUCT, "dpStatusRelationDTOS": []},
    }
    for name, item in document["fields"].items():
        code, kind, writable = heater.FIELD_CODES[name]
        values = {}
        if kind == "Integer":
            values = {
                "min": item["minimum"], "max": item["maximum"], "step": item["step"],
                "scale": item["scale"], "unit": "\u00b0C",
            }
        elif kind == "Enum":
            values = {"range": item["values"]}
        serialized = json.dumps(values)
        entry = {"code": code, "type": kind, "values": serialized}
        result["specifications"]["status"].append(entry)
        if writable:
            result["specifications"]["functions"].append(dict(entry))
        result["local_model"]["dpStatusRelationDTOS"].append({
            "dpCode": code, "statusCode": code, "dpId": int(item["dp"]),
            "valueType": kind, "supportLocal": True, "valueConvert": "default",
            "statusFormat": json.dumps({code: "$"}), "valueDesc": serialized,
            "enumMappingMap": {},
        })
    return result


class FakeDevice:
    def __init__(self, dps=None, responses=None, write_error=None, apply=True):
        self.dps = dict(dps if dps is not None else status_dps())
        self.responses = list(responses or [])
        self.write_error = write_error
        self.apply = apply
        self.writes = []
        self.limits = []
        self.socket = object()
        self.closed = False
        self.read_socket_states = []

    def status(self):
        self.read_socket_states.append(self.socket is not None)
        if self.socket is None:
            self.socket = object()
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"dps": dict(self.dps), "devId": DEVICE_ID}

    def set_socketRetryLimit(self, limit):
        self.limits.append(limit)

    def set_value(self, dp, value):
        if self.limits[-1] != 0:
            raise AssertionError("Physical writes must never have automatic retries.")
        self.writes.append((dp, value))
        if self.apply:
            self.dps[dp] = value
        if self.write_error:
            raise self.write_error
        return {"dps": {dp: value}}

    def close(self):
        self.closed = True
        self.socket = None


def make_client(device=None, profile=None, factory=None, discoverer=None):
    device = device or FakeDevice()
    factory = factory or Mock(return_value=device)
    discoverer = discoverer or Mock(return_value=(ADDRESS, "3.3"))
    client = heater.HeaterClient(
        credentials_provider=lambda: heater.Credentials(DEVICE_ID, LOCAL_KEY),
        profile_provider=lambda device_id: profile or make_profile(),
        discoverer=discoverer,
        device_factory=factory,
    )
    return client, device, factory, discoverer


class ProfileTests(unittest.TestCase):
    def test_documented_example_has_valid_synthetic_profile_shape(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "terrace-heater.example.json"
        document = json.loads(path.read_text())
        profile = heater.parse_profile(document, DEVICE_ID)
        self.assertEqual(profile.fields["power"].dp, "11")
        self.assertFalse(profile.standby_settings_verified)

    def test_credentials_are_portable_explicit_and_redacted(self):
        with patch.dict(os.environ, {
            "TERRACE_HEATER_DEVICE_ID": DEVICE_ID, "TERRACE_HEATER_LOCAL_KEY": LOCAL_KEY,
        }, clear=True):
            credentials = heater.load_credentials()
        self.assertEqual(credentials.device_id, DEVICE_ID)
        self.assertNotIn(LOCAL_KEY, repr(credentials))
        for environment in (
            {}, {"TERRACE_HEATER_DEVICE_ID": DEVICE_ID},
            {"TERRACE_HEATER_LOCAL_KEY": LOCAL_KEY},
            {"TERRACE_HEATER_DEVICE_ID": DEVICE_ID, "TERRACE_HEATER_LOCAL_KEY": ""},
        ):
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(heater.HeaterError):
                    heater.load_credentials()

    def test_profile_identity_schema_bounds_and_duplicate_fields_fail_closed(self):
        changes = [
            lambda p: p.update(device_id="anotherheater001"),
            lambda p: p.update(schema_version=True),
            lambda p: p.update(protocol_version="3.1"),
            lambda p: p.update(address_hint="203.0.113.5"),
            lambda p: p.update(verified_at="2026-10-01T12:00:00"),
            lambda p: p.update(unexpected=True),
            lambda p: p["fields"]["mode"].update(dp="11"),
            lambda p: p["fields"]["power"].update(writable=1),
            lambda p: p["fields"]["setpoint"].update(maximum=41),
            lambda p: p["fields"]["setpoint"].update(minimum=0),
            lambda p: p["fields"]["setpoint"].update(step=0),
            lambda p: p["fields"]["setpoint"].update(scale=-1),
            lambda p: p["fields"]["temperature"].update(unit="F"),
            lambda p: p["fields"]["power_level"].update(values=["1", "1"]),
            lambda p: p["fields"]["power_level"].update(values=["\x1b[31m"]),
        ]
        for change in changes:
            document = profile_document()
            change(document)
            with self.subTest(document=document):
                with self.assertRaises(heater.HeaterError):
                    heater.parse_profile(document, DEVICE_ID)
        with self.assertRaisesRegex(heater.HeaterError, "Duplicate"):
            heater.parse_json('{"a":1,"a":2}')

    def test_private_profile_requires_absolute_path_and_restricted_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile_document()))
            path.chmod(0o600)
            with patch.dict(os.environ, {"TERRACE_HEATER_CONFIG": str(path)}, clear=True):
                self.assertEqual(heater.load_profile(DEVICE_ID).device_id, DEVICE_ID)
                path.chmod(0o644)
                with self.assertRaises(heater.HeaterError):
                    heater.load_profile(DEVICE_ID)
            path.chmod(0o600)
            alias = Path(directory) / "alias.json"
            alias.symlink_to(path)
            with self.assertRaises(heater.HeaterError):
                heater.private_text(alias)
        for value in ("", "relative.json"):
            with patch.dict(os.environ, {"TERRACE_HEATER_CONFIG": value}, clear=True):
                with self.assertRaises(heater.HeaterError):
                    heater.load_profile(DEVICE_ID)

    def test_explicit_environment_file_is_parsed_as_data_and_never_as_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            text = (
                "export TERRACE_HEATER_DEVICE_ID=syntheticheater001\n"
                "export TERRACE_HEATER_LOCAL_KEY='synthetic-key-01'\n"
                "export TERRACE_HEATER_CONFIG='/private/device.json'\n"
            )
            path.write_text(text)
            path.chmod(0o600)
            self.assertEqual(heater.load_environment_file(path)["TERRACE_HEATER_LOCAL_KEY"], LOCAL_KEY)
            for invalid in (
                text + "exit 0\n",
                text + "export ANOTHER_VAR=value\n",
                text + "export TERRACE_HEATER_DEVICE_ID=anotherheater001\n",
                text.replace("'/private/device.json'", "'relative.json'"),
                text.replace("'synthetic-key-01'", "'unterminated"),
            ):
                path.write_text(invalid)
                with self.assertRaises(heater.HeaterError):
                    heater.load_environment_file(path)


class ReadTests(unittest.TestCase):
    def test_read_is_passive_and_off_temperature_is_not_claimed_fresh(self):
        client, device, _, discovery = make_client()
        state = client.get_status()
        self.assertFalse(state.enabled)
        self.assertEqual(state.temperature_c, 17)
        self.assertEqual(state.target_temperature_c, 21)
        self.assertIsNone(state.temperature_measured_at)
        self.assertEqual(state.temperature_freshness, "not_reported")
        self.assertIsNone(state.heating_active)
        self.assertEqual(state.capabilities.power_levels, ["1", "2", "3", "4"])
        self.assertEqual(state.capabilities.power_level_unit, "native_level")
        self.assertIn("retained", " ".join(state.warnings))
        self.assertEqual(device.writes, [])
        self.assertTrue(device.closed)
        discovery.assert_not_called()

    def test_missing_temperature_is_not_zero_and_zero_is_valid(self):
        for value, expected in ((None, None), (0, 0.0), (-5, -5.0)):
            dps = status_dps()
            dps["12"] = value
            state = heater.build_status(make_profile(), dps, ADDRESS)
            self.assertEqual(state.temperature_c, expected)
            self.assertEqual(state.temperature_status, "unavailable" if value is None else "available")

    def test_malformed_telemetry_fails_without_guessing(self):
        for dp, value in (("11", None), ("11", 1), ("12", False), ("12", 81), ("14", "eco"), ("15", "5")):
            dps = status_dps()
            dps[dp] = value
            client, device, _, _ = make_client(FakeDevice(dps))
            with self.assertRaises(heater.HeaterError):
                client.get_status()
            self.assertEqual(device.writes, [])
            self.assertTrue(device.closed)

    def test_failed_cached_address_rediscovery_is_identity_bound(self):
        old = FakeDevice(responses=[{"Error": LOCAL_KEY, "Err": "914", "Payload": LOCAL_KEY}])
        new = FakeDevice()
        factory = Mock(side_effect=[old, new])
        discover = Mock(return_value=("10.20.30.99", "3.3"))
        client, _, _, _ = make_client(factory=factory, discoverer=discover)
        state = client.get_status()
        self.assertEqual(state.ip, "10.20.30.99")
        discover.assert_called_once_with(DEVICE_ID, PRODUCT)
        self.assertTrue(old.closed)
        self.assertTrue(new.closed)
        self.assertEqual(factory.call_args_list[0].args[2], ADDRESS)
        self.assertEqual(factory.call_args_list[1].args[2], "10.20.30.99")

    def test_wrong_authenticated_identity_and_changed_protocol_never_write(self):
        old = FakeDevice(responses=[{"dps": status_dps(), "devId": "anotherheater001"}])
        discover = Mock(return_value=("10.20.30.99", "3.5"))
        client, _, _, _ = make_client(old, discoverer=discover)
        with self.assertRaisesRegex(heater.HeaterError, "protocol changed"):
            client.set_value("power", True)
        self.assertEqual(old.writes, [])

    def test_transport_payloads_and_credentials_are_not_in_errors(self):
        factory = Mock(side_effect=[
            FakeDevice(responses=[{"Error": LOCAL_KEY, "Err": "914"}]),
            FakeDevice(responses=[OSError(LOCAL_KEY)]),
        ])
        client, _, _, _ = make_client(factory=factory)
        with self.assertRaises(heater.HeaterError) as caught:
            client.get_status()
        self.assertNotIn(LOCAL_KEY, str(caught.exception))


class ControlTests(unittest.TestCase):
    def test_explicit_on_off_mode_level_and_temperature_are_read_back(self):
        for name, value, dp in (
            ("power", False, "11"), ("setpoint", 22.0, "13"),
            ("mode", "auto", "14"), ("power_level", "4", "15"),
        ):
            with self.subTest(name=name):
                device = FakeDevice(status_dps(True))
                client, _, _, _ = make_client(device)
                result = client.set_value(name, value)
                self.assertTrue(result.command_sent)
                self.assertTrue(result.verified)
                self.assertTrue(result.acknowledged)
                self.assertEqual(device.writes, [(dp, int(value) if name == "setpoint" else value)])
                self.assertEqual(device.limits, [0, 1])
                self.assertTrue(device.closed)
        client, device, _, _ = make_client()
        result = client.set_value("power", True)
        self.assertTrue(result.state.enabled)
        self.assertEqual(device.writes, [("11", True)])

    def test_matching_state_is_a_noop_even_while_off(self):
        for name, value in (("power", False), ("setpoint", 21), ("mode", "smart"), ("power_level", "1")):
            client, device, _, _ = make_client()
            result = client.set_value(name, value)
            self.assertFalse(result.command_sent)
            self.assertIsNone(result.acknowledged)
            self.assertTrue(result.verified)
            self.assertEqual(device.writes, [])

    def test_invalid_requests_do_not_open_network_connections(self):
        for name, value in (
            ("power", 1), ("power", "on"), ("setpoint", True), ("setpoint", "20"),
            ("setpoint", float("nan")), ("setpoint", float("inf")),
            ("setpoint", 4), ("setpoint", 41), ("setpoint", 20.5),
            ("mode", "eco"), ("power_level", "25%"), ("power_level", 1),
            ("power_level", "5"), ("temperature", 20), ("arbitrary_dp", True),
        ):
            with self.subTest(name=name, value=value):
                client, device, factory, discovery = make_client()
                with self.assertRaises(heater.HeaterError):
                    client.set_value(name, value)
                factory.assert_not_called()
                discovery.assert_not_called()
                self.assertEqual(device.writes, [])

    def test_scaled_range_and_step_are_respected_without_rounding(self):
        document = profile_document()
        document["fields"]["setpoint"].update(minimum=50, maximum=400, scale=1, step=5)
        profile = make_profile(document)
        dps = status_dps(True)
        dps["13"] = 210
        client, device, _, _ = make_client(FakeDevice(dps), profile)
        self.assertEqual(client.set_value("setpoint", 21.5).state.target_temperature_c, 21.5)
        self.assertEqual(device.writes, [("13", 215)])
        client, _, factory, _ = make_client(profile=profile)
        with self.assertRaises(heater.HeaterError):
            client.set_value("setpoint", 21.2)
        factory.assert_not_called()

    def test_off_settings_are_rejected_without_implicit_on(self):
        for name, value in (("setpoint", 22), ("mode", "auto"), ("power_level", "2")):
            client, device, _, _ = make_client()
            with self.assertRaisesRegex(heater.HeaterError, "while off"):
                client.set_value(name, value)
            self.assertEqual(device.writes, [])
        document = profile_document()
        document["standby_settings_verified"] = True
        client, device, _, _ = make_client(profile=make_profile(document))
        result = client.set_value("setpoint", 22)
        self.assertFalse(result.state.enabled)
        self.assertEqual(device.writes, [("13", 22)])

    def test_missing_ack_is_explicit_even_when_readback_confirms_state(self):
        client, device, _, _ = make_client(FakeDevice(write_error=TimeoutError(LOCAL_KEY)))
        result = client.set_value("power", True)
        self.assertTrue(result.verified)
        self.assertFalse(result.acknowledged)
        self.assertIn("acknowledgement", " ".join(result.state.warnings))
        self.assertNotIn(LOCAL_KEY, repr(result))
        self.assertEqual(device.writes, [("11", True)])

    def test_mismatched_or_unavailable_readback_is_not_success_and_not_replayed(self):
        cases = [
            FakeDevice(apply=False),
            FakeDevice(responses=[
                {"dps": status_dps()}, {"Error": LOCAL_KEY, "Err": "914"},
            ]),
        ]
        for device in cases:
            client, _, _, discovery = make_client(device)
            with patch.object(heater, "READBACK_TIMEOUT_SECONDS", 0), self.assertRaises(heater.HeaterError):
                client.set_value("power", True)
            self.assertEqual(device.writes, [("11", True)])
            self.assertEqual(device.limits, [0, 1])
            self.assertTrue(device.closed)
            discovery.assert_not_called()

    def test_delayed_readback_uses_fresh_connections_without_replaying_the_write(self):
        for name, requested, initially_on, dp in (
            ("power", True, False, "11"),
            ("power", False, True, "11"),
            ("setpoint", 22, True, "13"),
        ):
            before = status_dps(initially_on)
            after = dict(before)
            after[dp] = requested
            device = FakeDevice(responses=[
                {"dps": before}, {"dps": before}, {"dps": after},
            ])
            client, _, _, discovery = make_client(device)
            with patch.object(heater.time, "sleep") as sleep:
                result = client.set_value(name, requested)
            self.assertTrue(result.verified)
            self.assertEqual(device.writes, [(dp, requested)])
            self.assertEqual(device.read_socket_states, [True, False, False])
            sleep.assert_called_once_with(heater.READBACK_INTERVAL_SECONDS)
            discovery.assert_not_called()

    def test_readback_settling_has_a_deadline_and_never_retries_control(self):
        client, device, _, _ = make_client(FakeDevice(apply=False))
        with patch.object(
            heater.time, "monotonic", side_effect=[10, 10, 13],
        ), patch.object(heater.time, "sleep") as sleep:
            with self.assertRaisesRegex(heater.HeaterError, "read-back window"):
                client.set_value("power", True)
        self.assertEqual(device.writes, [("11", True)])
        self.assertEqual(device.read_socket_states, [True, False, False])
        sleep.assert_called_once_with(heater.READBACK_INTERVAL_SECONDS)

    def test_settings_that_change_power_fail_explicitly_without_another_write(self):
        before = status_dps(True)
        after = dict(before, **{"11": False, "13": 22})
        device = FakeDevice(responses=[{"dps": before}, {"dps": after}])
        client, _, _, _ = make_client(device)
        with self.assertRaisesRegex(heater.HeaterError, "unexpectedly changed"):
            client.set_value("setpoint", 22)
        self.assertEqual(device.writes, [("13", 22)])

    def test_in_process_operations_are_serialized(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        class SlowDevice(FakeDevice):
            def status(self):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.01)
                with lock:
                    active -= 1
                return super().status()

        client, _, _, _ = make_client(factory=lambda *args: SlowDevice())
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: client.get_status(), range(6)))
        self.assertEqual(maximum, 1)


@unittest.skipUnless(importlib.util.find_spec("tinytuya"), "Install heater extra for SDK adapter tests.")
class SdkTests(unittest.TestCase):
    def test_discovery_is_read_only_and_checks_unique_identity_and_product(self):
        from tinytuya import scanner
        valid = {
            ADDRESS: {"gwId": DEVICE_ID, "productKey": PRODUCT, "version": "3.3"},
            "10.20.30.41": {"gwId": "anotherheater001", "productKey": PRODUCT, "version": "3.3"},
        }
        with patch.object(scanner, "devices", return_value=valid) as scan:
            self.assertEqual(heater.discover_device(DEVICE_ID, PRODUCT), (ADDRESS, "3.3"))
        self.assertFalse(scan.call_args.kwargs["poll"])
        self.assertFalse(scan.call_args.kwargs["forcescan"])
        self.assertNotIn(LOCAL_KEY, repr(scan.call_args))
        for result in (
            {},
            {**valid, "10.20.30.42": valid[ADDRESS]},
            {ADDRESS: dict(valid[ADDRESS], productKey="wrong-product")},
            {"8.8.8.8": valid[ADDRESS]},
        ):
            with patch.object(scanner, "devices", return_value=result):
                with self.assertRaises(heater.HeaterError):
                    heater.discover_device(DEVICE_ID, PRODUCT)

    def test_factory_uses_one_connection_attempt_persistence_and_no_truncated_retry(self):
        import tinytuya
        with patch.object(tinytuya, "Device") as factory:
            result = heater._device_factory(
                make_profile(), heater.Credentials(DEVICE_ID, LOCAL_KEY), ADDRESS,
            )
        self.assertEqual(factory.call_args.kwargs["connection_retry_limit"], 1)
        self.assertEqual(factory.call_args.kwargs["connection_timeout"], heater.SOCKET_TIMEOUT)
        self.assertTrue(factory.call_args.kwargs["persist"])
        result.set_retry.assert_called_once_with(False)

    def test_real_sdk_does_not_retransmit_a_timed_out_write(self):
        device = heater._device_factory(
            make_profile(), heater.Credentials(DEVICE_ID, LOCAL_KEY), ADDRESS,
        )
        sock = Mock()
        device.socket = sock
        device.set_socketRetryLimit(0)
        with patch.object(device, "_receive", side_effect=TimeoutError):
            result = device.set_value("11", True)
        sock.sendall.assert_called_once()
        self.assertEqual(result["Err"], "902")
        device.close()


if __name__ == "__main__":
    unittest.main()
