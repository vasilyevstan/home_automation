import copy
import json
import os
import threading
import unittest
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import starlink as api

try:
    import grpc
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    from grpc_reflection.v1alpha import reflection_pb2
except ModuleNotFoundError as error:
    if not any((error.name or "").startswith(name) for name in ("grpc", "google")):
        raise
    grpc = None


ID = "ut00000000-00000000-00000001"
OTHER_ID = "ut00000000-00000000-00000002"
NOW = "2026-10-02T12:00:00+00:00"


def protocol(omit=()):
    """Synthetic schema, not a recorded or vendored Starlink descriptor."""
    file = descriptor_pb2.FileDescriptorProto(name="synthetic.proto", package="SpaceX.API.Device", syntax="proto3")
    kinds = {"float": 2, "uint64": 4, "int32": 5, "bool": 8, "str": 9, "uint32": 13}

    def message(name, fields, *, enums=None, oneof=False):
        result = file.message_type.add(name=name)
        for enum_name, values in (enums or {}).items():
            enum = result.enum_type.add(name=enum_name)
            for label, number in values.items():
                enum.value.add(name=label, number=number)
        if oneof:
            result.oneof_decl.add(name="message")
        for number, (field_name, kind) in enumerate(fields, 1):
            if f"{name}.{field_name}" in omit:
                continue
            repeated = kind.startswith("[]")
            kind = kind.removeprefix("[]")
            field = result.field.add(name=field_name, number=number, label=3 if repeated else 1)
            if kind in kinds:
                field.type = kinds[kind]
            elif enums and kind in enums:
                field.type = 14
                field.type_name = f".SpaceX.API.Device.{name}.{kind}"
            else:
                field.type = 11
                field.type_name = f".SpaceX.API.Device.{kind}"
            if oneof:
                field.oneof_index = 0
        return result

    message("Empty", [])
    message("RpcStatus", [("code", "int32")])
    message("Info", [("id", "str"), ("hardware_version", "str"), ("software_version", "str"), ("bootcount", "uint32")])
    message("State", [("uptime_s", "uint64")])
    message("Alerts", [("is_heating", "bool"), ("install_pending", "bool"), ("dbf_telem_stale", "bool")])
    message("Outage", [("cause", "Cause"), ("start_timestamp_ns", "uint64"), ("duration_ns", "uint64")],
            enums={"Cause": {"UNKNOWN": 0, "BOOTING": 1, "OBSTRUCTED": 6, "SLEEPING": 11}})
    message("Stats", [
        ("currently_obstructed", "bool"), ("fraction_obstructed", "float"), ("valid_s", "float"),
        ("avg_prolonged_obstruction_duration_s", "float"), ("avg_prolonged_obstruction_interval_s", "float"),
        ("avg_prolonged_obstruction_valid", "bool"),
    ])
    message("Status", [
        ("device_info", "Info"), ("device_state", "State"), ("alerts", "Alerts"), ("outage", "Outage"),
        ("downlink_throughput_bps", "float"), ("uplink_throughput_bps", "float"),
        ("pop_ping_latency_ms", "float"), ("pop_ping_drop_rate", "float"), ("eth_speed_mbps", "float"),
        ("has_actuators", "Actuators"), ("obstruction_stats", "Stats"),
        ("disablement_code", "Disablement"),
    ], enums={
        "Actuators": {"HAS_ACTUATORS_UNKNOWN": 0, "HAS_ACTUATORS_YES": 1, "HAS_ACTUATORS_NO": 2},
        "Disablement": {"UNKNOWN": 0, "OKAY": 1, "NO_ACTIVE_ACCOUNT": 2},
    })
    message("History", [
        ("current", "uint64"), ("pop_ping_drop_rate", "[]float"), ("pop_ping_latency_ms", "[]float"),
        ("downlink_throughput_bps", "[]float"), ("uplink_throughput_bps", "[]float"),
        ("power_in", "[]float"), ("outages", "[]Outage"),
    ])
    message("Config", [
        ("snow_melt_mode", "Snow"), ("apply_snow_melt_mode", "bool"),
        ("power_save_mode", "bool"), ("apply_power_save_mode", "bool"),
        ("power_save_start_minutes", "uint32"), ("apply_power_save_start_minutes", "bool"),
        ("power_save_duration_minutes", "uint32"), ("apply_power_save_duration_minutes", "bool"),
        ("swupdate_reboot_hour", "uint32"),
    ], enums={"Snow": {"AUTO": 0, "ALWAYS_ON": 7, "ALWAYS_OFF": 9}})
    message("ConfigEnvelope", [("dish_config", "Config")])
    message("Sleep", [("enable_power_save", "bool"), ("power_save_start_minutes", "uint32"),
                      ("power_save_duration_minutes", "uint32")])
    message("Map", [("num_rows", "uint32"), ("num_cols", "uint32"), ("snr", "[]float"),
                    ("map_reference_frame", "Frame")], enums={"Frame": {"UNKNOWN": 0, "FRAME_EARTH": 1}})
    message("Request", [
        ("get_status", "Empty"), ("get_history", "Empty"), ("dish_get_config", "Empty"),
        ("dish_get_obstruction_map", "Empty"), ("reboot", "Empty"), ("dish_power_save", "Sleep"),
        ("dish_set_config", "ConfigEnvelope"),
    ], oneof=True)
    response = message("Response", [
        ("dish_get_status", "Status"), ("dish_get_history", "History"),
        ("dish_get_config", "ConfigEnvelope"), ("dish_get_obstruction_map", "Map"),
        ("reboot", "Empty"), ("dish_power_save", "Empty"), ("dish_set_config", "Empty"),
    ], oneof=True)
    response.field.add(name="status", number=100, label=1, type=11, type_name=".SpaceX.API.Device.RpcStatus")
    service = file.service.add(name="Device")
    service.method.add(name="Handle", input_type=".SpaceX.API.Device.Request", output_type=".SpaceX.API.Device.Response")
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    types = {message.name: message_factory.GetMessageClass(
        pool.FindMessageTypeByName(f"SpaceX.API.Device.{message.name}")
    ) for message in file.message_type}
    return types, file


class FakeDish:
    def __init__(self, omit=()):
        self.types, self.descriptor = protocol(omit)
        self.status = self.types["Status"](
            device_info={"id": ID, "hardware_version": "synthetic-hw", "software_version": "synthetic-fw", "bootcount": 3},
            device_state={"uptime_s": 1000}, alerts={}, has_actuators=2,
            obstruction_stats={"valid_s": 900}, downlink_throughput_bps=8_000_000,
            uplink_throughput_bps=1_000_000, pop_ping_latency_ms=25, eth_speed_mbps=1000,
        )
        self.config = self.types["ConfigEnvelope"](dish_config={
            "snow_melt_mode": 0, "apply_snow_melt_mode": True,
            "power_save_mode": False, "apply_power_save_mode": True,
            "power_save_start_minutes": 120, "apply_power_save_start_minutes": True,
            "power_save_duration_minutes": 90, "apply_power_save_duration_minutes": True,
            "swupdate_reboot_hour": 4,
        })
        self.history = self.types["History"](
            current=6, pop_ping_drop_rate=[0, 0, 0, 0], pop_ping_latency_ms=[24, 25, 22, 23],
            downlink_throughput_bps=[4e6, 5e6, 2e6, 3e6],
            uplink_throughput_bps=[4e5, 5e5, 2e5, 3e5], power_in=[44, 45, 42, 43],
        )
        self.obstruction_map = self.types["Map"](num_rows=2, num_cols=2, snr=[0, 0, -1, 1], map_reference_frame=1)
        self.calls = []
        self.connections = 0
        self.fail = {}
        self.write_failure = None
        self.apply_writes = True
        self.wrong_after_write = False

    def connect(self, target):
        self.connections += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request_field(self, name):
        return self.types["Request"].DESCRIPTOR.fields_by_name.get(name)

    def call(self, name, values=None, **kwargs):
        self.calls.append((name, copy.deepcopy(values)))
        if name in self.fail:
            raise self.fail[name]
        reads = {
            "get_status": self.status, "get_history": self.history,
            "dish_get_config": self.config, "dish_get_obstruction_map": self.obstruction_map,
        }
        if name in reads:
            return copy.deepcopy(reads[name])
        if self.apply_writes:
            if name == "dish_set_config":
                enum = self.types["Config"].DESCRIPTOR.fields_by_name["snow_melt_mode"].enum_type
                self.config.dish_config.snow_melt_mode = enum.values_by_name[values["dish_config"]["snow_melt_mode"]].number
            elif name == "dish_power_save":
                self.config.dish_config.power_save_mode = values["enable_power_save"]
                self.config.dish_config.power_save_start_minutes = values["power_save_start_minutes"]
                self.config.dish_config.power_save_duration_minutes = values["power_save_duration_minutes"]
            elif name == "reboot":
                self.status.device_state.uptime_s = 2
                self.status.device_info.bootcount += 1
            if self.wrong_after_write:
                self.status.device_info.id = OTHER_ID
        if self.write_failure:
            raise self.write_failure
        return None

    @property
    def writes(self):
        return [call for call in self.calls if call[0] in api._ACTIONS.values()]

    def client(self, actions=()):
        return api.StarlinkClient(
            lambda: api.Settings(expected_device_id=ID, allowed_actions=frozenset(actions)),
            self.connect, lambda: NOW,
        )


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_read_only_and_require_no_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(api.load_settings(), api.Settings())

    def test_strict_target_identity_and_action_configuration(self):
        for values in (
            {"STARLINK_TARGET": "example.com:9200"}, {"STARLINK_TARGET": "8.8.8.8:9200"},
            {"STARLINK_TARGET": "127.0.0.1:9200"}, {"STARLINK_TARGET": "192.168.100.1:0"},
            {"STARLINK_TARGET": "192.168.100.1:65536"}, {"STARLINK_TARGET": "192.168.100.1"},
            {"STARLINK_EXPECTED_DEVICE_ID": "dish"}, {"STARLINK_ALLOWED_ACTIONS": "reboot"},
            {"STARLINK_ALLOWED_ACTIONS": "reset", "STARLINK_EXPECTED_DEVICE_ID": ID},
            {"STARLINK_ALLOWED_ACTIONS": "reboot,", "STARLINK_EXPECTED_DEVICE_ID": ID},
        ):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(api.StarlinkError):
                    api.load_settings()
        with patch.dict(os.environ, {
            "STARLINK_TARGET": "10.0.0.2:9200", "STARLINK_EXPECTED_DEVICE_ID": ID,
            "STARLINK_ALLOWED_ACTIONS": "reboot,sleep,snow_melt",
        }, clear=True):
            self.assertEqual(api.load_settings().allowed_actions, frozenset(api._ACTIONS))

    def test_disabled_controls_fail_without_connection(self):
        connect = MagicMock()
        client = api.StarlinkClient(lambda: api.Settings(), connect)
        for call in (lambda: client.reboot(True), lambda: client.set_sleep(False),
                     lambda: client.set_snow_melt("always_off")):
            with self.assertRaisesRegex(api.StarlinkError, "disabled"):
                call()
        connect.assert_not_called()

    def test_invalid_inputs_fail_before_connecting(self):
        connect = MagicMock()
        client = api.StarlinkClient(connection_factory=connect)
        for call in (
            lambda: client.get_history(True), lambda: client.get_history(901),
            lambda: client.get_history(0), lambda: client.get_history(1, "true"),
            lambda: client.reboot(1), lambda: client.reboot(False),
            lambda: client.set_sleep(1), lambda: client.set_sleep(True),
            lambda: client.set_sleep(True, True, 2), lambda: client.set_sleep(True, 1440, 60),
            lambda: client.set_sleep(True, 0, 0), lambda: client.set_sleep(False, 0, 1),
            lambda: client.set_snow_melt("off"),
        ):
            with self.subTest(call=call), self.assertRaises(api.StarlinkError):
                call()
        connect.assert_not_called()


@unittest.skipIf(grpc is None, "Install the optional starlink extra for protobuf/transport tests.")
class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.dish = FakeDish()
        self.client = self.dish.client()

    def test_status_download_upload_units_identity_and_no_write(self):
        state = self.client.get_status()
        self.assertEqual((state.download.bps, state.download.mbps), (8_000_000, 8))
        self.assertEqual((state.upload.bps, state.upload.mbps), (1_000_000, 1))
        self.assertEqual(state.latency.unit, "ms")
        self.assertEqual(state.packet_loss.value, 0)
        self.assertEqual(state.connection_state, "connected")
        self.assertIsNone(state.measurement_time)
        self.assertIsNone(state.power.value)
        self.assertFalse(state.heating)
        self.assertFalse(state.has_actuators)
        self.assertEqual(self.dish.writes, [])

    def test_idle_zero_proto_defaults_differ_from_missing_schema(self):
        self.dish.status.ClearField("downlink_throughput_bps")
        state = self.client.get_status()
        self.assertEqual(state.download.mbps, 0)
        self.assertEqual(state.download.status, "available")
        types, _ = protocol(omit=("Status.downlink_throughput_bps",))
        raw = types["Status"]()
        raw.ParseFromString(self.dish.status.SerializeToString())
        state = api.status_data(raw, NOW)
        self.assertEqual(state.download.status, "unsupported")
        self.assertIsNone(state.download.mbps)

    def test_nonfinite_invalid_values_and_unknown_enum_never_become_zero(self):
        self.dish.status.downlink_throughput_bps = float("nan")
        self.dish.status.uplink_throughput_bps = -5
        self.dish.status.pop_ping_drop_rate = 1.5
        self.dish.status.has_actuators = 42
        self.dish.status.alerts.dbf_telem_stale = True
        state = self.client.get_status()
        self.assertIsNone(state.download.bps)
        self.assertIsNone(state.upload.bps)
        self.assertIsNone(state.packet_loss.value)
        self.assertIsNone(state.has_actuators)
        self.assertIn("stale telemetry", " ".join(state.warnings))
        json.dumps(asdict(state), allow_nan=False)

    def test_outages_are_not_connected_and_missing_identity_is_an_error(self):
        self.dish.status.disablement_code = 2
        self.assertEqual(self.client.get_status().connection_state, "disabled")
        self.assertEqual(self.client.get_status().disablement_reason, "NO_ACTIVE_ACCOUNT")
        self.dish.status.outage.cause = 11
        self.assertEqual(self.client.get_status().connection_state, "SLEEPING")
        self.dish.status.device_info.id = OTHER_ID
        with self.assertRaisesRegex(api.StarlinkError, "identity"):
            self.client.get_status()
        self.dish.status.ClearField("device_info")
        with self.assertRaisesRegex(api.StarlinkError, "identity"):
            self.client.get_status()

    def test_missing_parent_messages_and_unsuccessful_ping_are_unavailable(self):
        self.dish.status.ClearField("device_state")
        self.dish.status.pop_ping_drop_rate = 1
        self.dish.status.ClearField("alerts")
        state = self.client.get_status()
        self.assertEqual(state.uptime_seconds.status, "unavailable")
        self.assertIsNone(state.latency.value)
        self.assertFalse(state.alerts_available)
        self.assertIsNone(state.heating)

    def test_history_ring_order_average_peak_and_both_units(self):
        history = self.client.get_history(4, True)
        self.assertEqual([point.counter for point in history.samples], [3, 4, 5, 6])
        self.assertEqual([point.seconds_before_latest for point in history.samples], [3, 2, 1, 0])
        self.assertEqual([point.download.mbps for point in history.samples], [2, 3, 4, 5])
        self.assertEqual(history.download.mbps.average, 3.5)
        self.assertEqual(history.download.bps.peak, 5_000_000)
        self.assertAlmostEqual(history.upload.mbps.average, 0.35)
        self.assertEqual(history.power.average, 43.5)
        self.assertTrue(history.complete)
        self.assertIsNone(self.client.get_history(2).samples)

    def test_partial_startup_reset_and_empty_history_are_not_padded(self):
        for current, expected in ((2, 2), (0, 0), (1, 1)):
            self.dish.history.current = current
            history = self.client.get_history(4, True)
            self.assertEqual(history.returned_samples, expected)
            self.assertEqual(len(history.samples), expected)
            self.assertFalse(history.complete)
            self.assertEqual(history.available_samples, expected)
        self.dish.history.ClearField("pop_ping_drop_rate")
        history = self.client.get_history(4)
        self.assertEqual(history.returned_samples, 0)
        self.assertIsNone(history.download.mbps.average)

    def test_mismatched_missing_and_invalid_history_do_not_invent_alignment(self):
        self.dish.history.uplink_throughput_bps.pop()
        self.dish.history.downlink_throughput_bps[0] = float("inf")
        self.dish.history.pop_ping_drop_rate[1] = 1
        history = self.client.get_history(4, True)
        self.assertFalse(history.complete)
        self.assertEqual(history.upload.mbps.valid_samples, 0)
        self.assertEqual(history.download.mbps.valid_samples, 3)
        self.assertAlmostEqual(history.download.mbps.average, 10 / 3)
        self.assertEqual(history.latency.valid_samples, 3)
        self.assertIn("alignment is unknown", " ".join(history.warnings))
        json.dumps(asdict(history), allow_nan=False)
        types, _ = protocol(omit=("History.power_in",))
        raw = types["History"]()
        raw.ParseFromString(self.dish.history.SerializeToString())
        result = api.history_data(raw, self.client.get_status().device, NOW, 4, False)
        self.assertEqual(result.power.status, "unsupported")

    def test_outage_units_and_missing_primary_history_schema(self):
        self.dish.history.outages.add(cause=6, start_timestamp_ns=10, duration_ns=1_500_000_000)
        outage = self.client.get_history().retained_outages[0]
        self.assertEqual(outage.cause, "OBSTRUCTED")
        self.assertEqual(outage.duration_seconds.value, 1.5)
        types, _ = protocol(omit=("History.current",))
        with self.assertRaisesRegex(api.StarlinkError, "counter"):
            api.history_data(types["History"](), self.client.get_status().device, NOW, 4, False)

    def test_capabilities_never_test_controls_and_unknown_config_is_explicit(self):
        result = self.client.get_capabilities()
        self.assertTrue(all(c.schema_present for c in result.controls))
        self.assertTrue(all(c.execution_support == "unverified" and not c.locally_enabled for c in result.controls))
        self.assertEqual(result.configuration.snow_melt_mode, "auto")
        self.assertEqual(result.configuration.sleep_start_minutes_utc, 120)
        self.dish.config.dish_config.snow_melt_mode = 51
        self.dish.config.dish_config.apply_power_save_mode = False
        result = self.client.get_capabilities()
        self.assertIsNone(result.configuration.snow_melt_mode)
        self.assertIsNone(result.configuration.sleep_enabled)
        self.assertEqual(self.dish.writes, [])

    def test_partial_capabilities_include_read_failure(self):
        self.dish.fail["dish_get_config"] = api.RpcFailure("PERMISSION_DENIED")
        result = self.client.get_capabilities()
        self.assertIsNone(result.configuration)
        self.assertIn("PERMISSION_DENIED", " ".join(result.warnings))
        self.assertEqual(self.dish.writes, [])

    def test_obstructions_validity_and_compact_map(self):
        result = self.client.get_obstructions()
        self.assertEqual(result.fraction_obstructed.value, 0)
        self.assertEqual((result.map_rows, result.map_columns), (2, 2))
        self.assertIsNone(result.average_prolonged_interval.value)
        self.assertNotIn("snr", asdict(result))
        self.dish.fail["dish_get_obstruction_map"] = api.RpcFailure("UNIMPLEMENTED")
        result = self.client.get_obstructions()
        self.assertIsNone(result.map_rows)
        self.assertIn("UNIMPLEMENTED", " ".join(result.warnings))
        self.assertEqual(self.dish.writes, [])

    def test_obstruction_collection_and_malformed_maps_are_explicit(self):
        self.dish.status.obstruction_stats.valid_s = 0
        self.assertIsNone(self.client.get_obstructions().fraction_obstructed.value)
        self.dish.obstruction_map.snr.pop()
        with self.assertRaisesRegex(api.StarlinkError, "incomplete obstruction"):
            self.client.get_obstructions()


@unittest.skipIf(grpc is None, "Install the optional starlink extra for control tests.")
class ControlTests(unittest.TestCase):
    def setUp(self):
        self.dish = FakeDish()
        self.client = self.dish.client(api._ACTIONS)

    def test_snow_melt_uses_symbolic_enum_narrow_update_and_fresh_read(self):
        result = self.client.set_snow_melt("always_on")
        self.assertTrue(result.verified)
        self.assertTrue(result.acknowledged)
        self.assertEqual(result.observed, {"snow_melt_mode": "always_on"})
        self.assertEqual(self.dish.writes, [("dish_set_config", {
            "dish_config": {"snow_melt_mode": "ALWAYS_ON", "apply_snow_melt_mode": True},
        })])
        self.assertEqual(self.dish.config.dish_config.snow_melt_mode, 7)
        self.assertEqual(self.dish.config.dish_config.swupdate_reboot_hour, 4)
        self.assertEqual(self.dish.connections, 2)
        self.assertEqual(self.client.get_capabilities().controls[2].execution_support, "accepted")

    def test_noop_and_missing_schema_identity_or_config_send_nothing(self):
        result = self.client.set_snow_melt("auto")
        self.assertFalse(result.command_sent)
        self.assertIsNone(result.acknowledged)
        self.dish.config.dish_config.apply_snow_melt_mode = False
        with self.assertRaises(api.Unsupported):
            self.client.set_snow_melt("always_on")
        self.dish.status.device_info.id = OTHER_ID
        with self.assertRaisesRegex(api.StarlinkError, "identity"):
            self.client.reboot(True)
        self.assertEqual(self.dish.writes, [])
        self.dish.status.device_info.id = ID
        with patch.object(self.dish, "request_field", return_value=None):
            with self.assertRaises(api.Unsupported):
                self.client.reboot(True)
        self.assertEqual(self.dish.writes, [])

    def test_sleep_exact_utc_values_and_disable_preserve_other_settings(self):
        self.client.set_sleep(True, 1430, 120)
        self.assertEqual(self.dish.writes[-1], ("dish_power_save", {
            "enable_power_save": True, "power_save_start_minutes": 1430, "power_save_duration_minutes": 120,
        }))
        self.client.set_sleep(False)
        self.assertEqual(self.dish.writes[-1], ("dish_power_save", {
            "enable_power_save": False, "power_save_start_minutes": 1430, "power_save_duration_minutes": 120,
        }))
        self.assertEqual(self.dish.config.dish_config.snow_melt_mode, 0)
        self.assertFalse(self.client.set_sleep(False).command_sent)

    def test_lost_acknowledgement_is_read_back_never_retried(self):
        self.dish.write_failure = api.RpcFailure("DEADLINE_EXCEEDED")
        result = self.client.set_snow_melt("always_off")
        self.assertTrue(result.verified)
        self.assertIsNone(result.acknowledged)
        self.assertEqual(len(self.dish.writes), 1)
        self.assertIn("Acknowledgement was lost", " ".join(result.warnings))

    def test_unconfirmed_write_wrong_identity_and_rejected_write_are_errors(self):
        self.dish.write_failure = api.RpcFailure("UNAVAILABLE")
        self.dish.apply_writes = False
        with self.assertRaisesRegex(api.StarlinkError, "may have taken effect"):
            self.client.set_snow_melt("always_on")
        self.assertEqual(len(self.dish.writes), 1)
        self.dish.apply_writes = True
        self.dish.wrong_after_write = True
        with self.assertRaisesRegex(api.StarlinkError, "outcome unconfirmed"):
            self.client.set_snow_melt("always_on")
        self.assertEqual(len(self.dish.writes), 2)
        self.dish.status.device_info.id = ID
        self.dish.wrong_after_write = False
        self.dish.write_failure = api.RpcFailure("PERMISSION_DENIED")
        self.dish.apply_writes = False
        with self.assertRaisesRegex(api.StarlinkError, "PERMISSION_DENIED"):
            self.client.set_snow_melt("always_off")
        self.assertEqual(len(self.dish.writes), 3)
        self.assertEqual(self.client.get_capabilities().controls[2].execution_support, "denied")

    def test_reboot_is_single_attempt_and_requires_observed_restart(self):
        self.dish.status.alerts.install_pending = True
        self.dish.write_failure = api.RpcFailure("UNAVAILABLE")
        result = self.client.reboot(True)
        self.assertTrue(result.verified)
        self.assertIsNone(result.acknowledged)
        self.assertEqual(len(self.dish.writes), 1)
        self.assertEqual(result.observed["boot_count"], 4)
        self.assertIn("pending software", " ".join(result.warnings))

    def test_reboot_acceptance_and_disconnect_are_not_confirmed_restarts(self):
        self.dish.apply_writes = False
        with patch("starlink.READBACK_SECONDS", 0):
            with self.assertRaisesRegex(api.StarlinkError, "restart unconfirmed"):
                self.client.reboot(True)
        self.assertEqual(len(self.dish.writes), 1)

    def test_control_evidence_does_not_survive_firmware_change(self):
        self.client.set_snow_melt("always_on")
        self.dish.status.device_info.software_version = "new-synthetic-firmware"
        self.assertEqual(self.client.get_capabilities().controls[2].execution_support, "unverified")

    def test_concurrent_mutations_are_serialized_and_second_is_noop(self):
        results, errors = [], []
        def set_mode():
            try:
                results.append(self.client.set_snow_melt("always_on"))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=set_mode) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(result.command_sent for result in results), 1)
        self.assertEqual(len(self.dish.writes), 1)


@unittest.skipIf(grpc is None, "Install the optional starlink extra for transport tests.")
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.types, self.descriptor = protocol()
        self.channel = MagicMock()
        self.handle = self.channel.unary_unary.return_value
        self.reflection = reflection_pb2.ServerReflectionResponse()
        self.reflection.file_descriptor_response.file_descriptor_proto.append(self.descriptor.SerializeToString())

    def connect(self):
        patches = [
            patch("grpc.insecure_channel", return_value=self.channel),
            patch("grpc.channel_ready_future"),
            patch("grpc_reflection.v1alpha.reflection_pb2_grpc.ServerReflectionStub"),
        ]
        mocks = [self.enterContext(context) for context in patches]
        mocks[2].return_value.ServerReflectionInfo.return_value = iter([self.reflection])
        connection = api.Connection(api.TARGET)
        self.enterContext(connection)
        return connection, mocks

    def test_reflection_deadlines_and_transport_retry_disabled(self):
        connection, mocks = self.connect()
        self.handle.return_value = self.types["Response"](dish_get_status={"device_info": {"id": ID}})
        response = connection.call("get_status")
        self.assertEqual(response.device_info.id, ID)
        self.assertIn(("grpc.enable_retries", 0), mocks[0].call_args.kwargs["options"])
        mocks[1].return_value.result.assert_called_once_with(timeout=api.RPC_TIMEOUT)
        self.assertEqual(mocks[2].return_value.ServerReflectionInfo.call_args.kwargs["timeout"], api.RPC_TIMEOUT)
        self.assertEqual(self.handle.call_args.kwargs["timeout"], api.RPC_TIMEOUT)
        self.assertEqual(self.handle.call_args.args[0].WhichOneof("message"), "get_status")
        connection.close()
        self.channel.close.assert_called()

    def test_wrong_empty_and_device_error_response_are_rejected_for_reads(self):
        connection, _ = self.connect()
        for response in (
            self.types["Response"](), self.types["Response"](dish_get_history={}),
            self.types["Response"](status={"code": 7}),
        ):
            self.handle.return_value = response
            with self.assertRaises(api.StarlinkError):
                connection.call("get_status")
        self.assertEqual(self.handle.call_count, 3)

    def test_empty_control_ack_and_enum_mapping_do_not_retry(self):
        connection, _ = self.connect()
        self.handle.return_value = self.types["Response"]()
        self.assertIsNone(connection.call("dish_set_config", {"dish_config": {
            "snow_melt_mode": "ALWAYS_OFF", "apply_snow_melt_mode": True,
        }}))
        self.assertEqual(self.handle.call_args.args[0].dish_set_config.dish_config.snow_melt_mode, 9)
        self.assertEqual(self.handle.call_count, 1)
        with self.assertRaises(api.Unsupported):
            connection.call("factory_reset")
        self.assertEqual(self.handle.call_count, 1)

    def test_rpc_errors_redact_details_and_have_one_attempt(self):
        class Error(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE
            def details(self):
                return "synthetic-secret"
        connection, _ = self.connect()
        self.handle.side_effect = Error()
        with self.assertRaisesRegex(api.RpcFailure, "UNAVAILABLE") as caught:
            connection.call("reboot")
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(self.handle.call_count, 1)

    def test_malformed_reflection_and_missing_dependencies_close_channel(self):
        for blob in (b"\x80", descriptor_pb2.FileDescriptorProto(
            name="missing.proto", dependency=["absent.proto"]
        ).SerializeToString()):
            with self.subTest(blob=blob):
                self.reflection.file_descriptor_response.file_descriptor_proto[:] = [blob]
                with self.assertRaises(api.StarlinkError):
                    self.connect()
                self.channel.close.assert_called()
                self.handle.assert_not_called()

    def test_no_reflection_service_and_connection_timeout_are_errors(self):
        self.reflection.ClearField("file_descriptor_response")
        self.reflection.error_response.error_code = 12
        with self.assertRaises(api.Unsupported):
            self.connect()
        self.channel.close.assert_called()
        with patch("grpc.insecure_channel", return_value=self.channel), patch(
            "grpc.channel_ready_future"
        ) as ready:
            ready.return_value.result.side_effect = grpc.FutureTimeoutError()
            with self.assertRaisesRegex(api.StarlinkError, "connection timed out"):
                with api.Connection(api.TARGET):
                    self.fail("A timed-out connection should not open.")
        self.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
