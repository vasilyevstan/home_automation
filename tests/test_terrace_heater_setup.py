import io
import importlib.util
import json
import logging
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import terrace_heater_setup as setup
from test_terrace_heater import (
    ADDRESS, make_client, onboarding_metadata,
)


HAS_SETUP_SDK = importlib.util.find_spec("tuya_sharing") is not None
if HAS_SETUP_SDK:
    from requests.exceptions import RequestException
    from tuya_sharing.customerapi import CustomerApi
    from tuya_sharing.customerlogging import logger as sdk_logger
    from tuya_sharing.user import LoginControl


DEVICE_ID = "syntheticheater001"
LOCAL_KEY = "synthetic-key-01"
USER_CODE = "TestCode"
ACCESS_TOKEN = "sentinel-access-token"
REFRESH_TOKEN = "sentinel-refresh-token"


def login_info(**changes):
    info = {
        "uid": "synthetic-account",
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "t": int(time.time() * 1000),
        "expire_time": 3600,
        "endpoint": "https://apigw.tuyaeu.com",
    }
    info.update(changes)
    return info


def chosen_device():
    return setup.DeviceChoice(DEVICE_ID, "Synthetic terrace heater", "synthetic-product", LOCAL_KEY)


def metadata():
    return {
        "verification": "requires_lan_and_mapping_verification",
        "device": {"id": DEVICE_ID, "name": "Synthetic terrace heater"},
    }


class SetupValidationTests(unittest.TestCase):
    def test_device_repr_hides_key_and_missing_key_is_explicit(self):
        self.assertNotIn(LOCAL_KEY, repr(chosen_device()))
        for value in (None, "", "short", "x" * 15 + "\n"):
            with self.subTest(value=value):
                device = setup.DeviceChoice(DEVICE_ID, "Heater", None, value)
                with self.assertRaisesRegex(setup.SetupError, "usable local key"):
                    setup._validate_key(device)

    def test_auth_endpoint_must_be_https_at_an_expected_provider(self):
        self.assertEqual(setup._endpoint("https://apigw.tuyaeu.com/"), "https://apigw.tuyaeu.com")
        for value in (
            "http://apigw.tuyaeu.com", "https://tuyaeu.com.attacker.invalid",
            "https://not-tuyaeu.com", "https://user:secret@apigw.tuyaeu.com",
            "https://apigw.tuyaeu.com:444", "https://apigw.tuyaeu.com/private",
            "https://apigw.tuyaeu.com?token=private", None,
        ):
            with self.subTest(value=value):
                with self.assertRaises(setup.SetupError):
                    setup._endpoint(value)

    def test_invalid_device_identifiers_and_failed_results_are_not_defaults(self):
        for value in (None, {}, {"id": "../private"}, {"id": True}):
            with self.assertRaises(setup.SetupError):
                setup._device(value)
        with self.assertRaises(setup.SetupError) as caught:
            setup._result({"success": False, "msg": ACCESS_TOKEN}, "device listing")
        self.assertNotIn(ACCESS_TOKEN, str(caught.exception))

    def test_non_interactive_setup_rejects_input_before_authentication(self):
        with patch("sys.stdin.isatty", return_value=False), patch.object(
            setup, "TuyaAppSession",
        ) as session, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(setup.main([]), 2)
        session.assert_not_called()


@unittest.skipUnless(HAS_SETUP_SDK, "Install the heater-setup extra for SDK adapter tests.")
class AppSessionTests(unittest.TestCase):
    def _authorized(self, session):
        session.request_qr(USER_CODE)
        session.confirm_login()

    def test_qr_login_is_bounded_and_session_data_is_discarded(self):
        with patch.object(LoginControl, "qr_code", return_value={
            "success": True, "result": {"qrcode": "synthetic-qr-token"},
        }) as qr, patch.object(LoginControl, "login_result", return_value=(True, login_info())) as login:
            previous = sdk_logger.disabled
            with setup.TuyaAppSession() as session:
                self._authorized(session)
                api = session._api
                self.assertEqual(api.session.max_redirects, 0)
                self.assertTrue(sdk_logger.disabled)
            self.assertEqual(api.token_info.access_token, "")
            self.assertEqual(api.token_info.refresh_token, "")
            self.assertEqual(api.user_code, "")
            self.assertEqual(session._user_code, "")
            self.assertEqual(session._qr_token, "")
            self.assertEqual(sdk_logger.disabled, previous)
        qr.assert_called_once_with(setup.APP_CLIENT_ID, setup.APP_SCHEMA, USER_CODE)
        login.assert_called_once()

    def test_authorization_errors_do_not_render_provider_payloads(self):
        for result in (
            (False, {"msg": ACCESS_TOKEN}),
            (True, login_info(expire_time=0)),
            (True, login_info(endpoint="https://untrusted.invalid")),
            (True, login_info(access_token="")),
        ):
            with self.subTest(result=result[0]), patch.object(LoginControl, "qr_code", return_value={
                "success": True, "result": {"qrcode": "synthetic-qr-token"},
            }), patch.object(LoginControl, "login_result", return_value=result):
                with setup.TuyaAppSession() as session:
                    session.request_qr(USER_CODE)
                    with self.assertRaises(setup.SetupError) as caught:
                        session.confirm_login()
                self.assertNotIn(ACCESS_TOKEN, str(caught.exception))
                self.assertNotIn(REFRESH_TOKEN, str(caught.exception))

    def test_transport_errors_and_sdk_logs_are_redacted(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)

        def fail(*args):
            sdk_logger.error("private %s %s", USER_CODE, ACCESS_TOKEN)
            raise RequestException(REFRESH_TOKEN)

        try:
            with patch.object(LoginControl, "qr_code", side_effect=fail):
                with setup.TuyaAppSession() as session:
                    with self.assertRaises(setup.SetupError) as caught:
                        session.request_qr(USER_CODE)
        finally:
            logging.getLogger().removeHandler(handler)
        rendered = output.getvalue() + str(caught.exception)
        for secret in (USER_CODE, ACCESS_TOKEN, REFRESH_TOKEN):
            self.assertNotIn(secret, rendered)

    def test_only_selected_device_schema_is_requested_and_saved_without_keys(self):
        responses = [
            {"success": True, "result": [{"ownerId": 1, "name": "Private home"}]},
            {"success": True, "result": [{
                "id": DEVICE_ID, "name": "Heater", "local_key": LOCAL_KEY,
                "product_id": "synthetic-product",
            }]},
            {"success": True, "result": {
                "functions": [{"code": "switch", "type": "Boolean", "values": "{}"}],
                "status": [{"code": "temp_current", "type": "Integer", "values": "{}"}],
                "unexpected_secret": ACCESS_TOKEN,
            }},
            {"success": True, "result": {
                "productKey": "synthetic-product",
                "dpStatusRelationDTOS": [{
                    "dpId": 1, "dpCode": "switch", "supportLocal": True,
                    "valueType": "bool", "unexpected_secret": REFRESH_TOKEN,
                }],
                "unexpected_secret": LOCAL_KEY,
            }},
        ]
        with patch.object(LoginControl, "qr_code", return_value={
            "success": True, "result": {"qrcode": "synthetic-qr-token"},
        }), patch.object(LoginControl, "login_result", return_value=(True, login_info())), patch.object(
            CustomerApi, "get", side_effect=responses,
        ) as get:
            with setup.TuyaAppSession() as session:
                self._authorized(session)
                devices = session.list_devices()
                data = session.selected_metadata(devices[0])
        self.assertEqual(data["device"]["id"], DEVICE_ID)
        self.assertEqual(data["verification"], "requires_lan_and_mapping_verification")
        self.assertEqual(get.call_count, 4)
        self.assertIn(DEVICE_ID, get.call_args_list[-1].args[0])
        for secret in (LOCAL_KEY, USER_CODE, ACCESS_TOKEN, REFRESH_TOKEN):
            self.assertNotIn(secret, json.dumps(data))

    def test_cli_private_prompt_completes_without_rendering_secrets(self):
        output = io.StringIO()
        errors = io.StringIO()
        output.isatty = lambda: True
        errors.isatty = lambda: True
        with tempfile.TemporaryDirectory() as directory, patch(
            "sys.stdin.isatty", return_value=True,
        ), patch("sys.stdout", output), patch("sys.stderr", errors), patch.object(
            setup.getpass, "getpass", return_value=USER_CODE,
        ) as prompt, patch.object(
            LoginControl, "qr_code",
            return_value={"success": True, "result": {"qrcode": "synthetic-qr-token"}},
        ), patch.object(
            LoginControl, "login_result", return_value=(True, login_info()),
        ), patch.object(
            setup.TuyaAppSession, "list_devices", return_value=[chosen_device()],
        ), patch.object(
            setup.TuyaAppSession, "selected_metadata", return_value=metadata(),
        ), patch.object(setup, "render_qr") as qr, patch(
            "builtins.input", side_effect=["", "1"],
        ):
            private = Path(directory) / "heater"
            self.assertEqual(setup.main(["--output-dir", str(private)]), 0)
            self.assertTrue((private / "runtime.env").is_file())
            self.assertTrue((private / "onboarding.json").is_file())
            self.assertFalse((private / "device.json").exists())
        prompt.assert_called_once_with("Tuya User Code (hidden): ")
        qr.assert_called_once_with("synthetic-qr-token")
        rendered = output.getvalue() + errors.getvalue()
        for secret in (USER_CODE, ACCESS_TOKEN, REFRESH_TOKEN, LOCAL_KEY):
            self.assertNotIn(secret, rendered)
        self.assertIn("still required", rendered)

    def test_cli_cancellation_clears_session_and_writes_no_credentials(self):
        output = io.StringIO()
        errors = io.StringIO()
        output.isatty = lambda: True
        errors.isatty = lambda: True
        session = setup.TuyaAppSession()
        with tempfile.TemporaryDirectory() as directory, patch(
            "sys.stdin.isatty", return_value=True,
        ), patch("sys.stdout", output), patch("sys.stderr", errors), patch.object(
            setup.getpass, "getpass", return_value=USER_CODE,
        ), patch.object(
            setup, "TuyaAppSession", return_value=session,
        ), patch.object(
            LoginControl, "qr_code",
            return_value={"success": True, "result": {"qrcode": "synthetic-qr-token"}},
        ), patch.object(setup, "render_qr"), patch(
            "builtins.input", side_effect=KeyboardInterrupt,
        ):
            private = Path(directory) / "heater"
            self.assertEqual(setup.main(["--output-dir", str(private)]), 130)
            self.assertEqual(list(private.iterdir()), [])
        self.assertEqual(session._user_code, "")
        self.assertEqual(session._qr_token, "")


class PrivateProvisioningTests(unittest.TestCase):
    def test_private_files_are_restricted_and_environment_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            output = setup.prepare_directory(Path(directory) / "heater")
            setup.save_provisioning(output, chosen_device(), metadata())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for name in setup.PROVISION_FILES:
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
            capture = (output / "onboarding.json").read_text()
            self.assertNotIn(LOCAL_KEY, capture)
            self.assertNotIn(USER_CODE, capture)
            result = subprocess.run(
                [
                    "/bin/sh", "-c",
                    '. "$1"; test "$TERRACE_HEATER_DEVICE_ID" = "$2" && '
                    'test "$TERRACE_HEATER_LOCAL_KEY" = "$3" && '
                    'test "$TERRACE_HEATER_CONFIG" = "$4"',
                    "test-private-environment", str(output / "runtime.env"),
                    DEVICE_ID, LOCAL_KEY, str(output / "device.json"),
                ],
                check=False, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((output / "device.json").exists())

    def test_existing_files_symlinks_and_git_directories_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = setup.prepare_directory(root / "private")
            setup.save_provisioning(private, chosen_device(), metadata())
            before = (private / "runtime.env").read_bytes()
            with self.assertRaises(setup.SetupError):
                setup.prepare_directory(private)
            with self.assertRaises(setup.SetupError):
                setup.save_provisioning(private, chosen_device(), metadata())
            self.assertEqual((private / "runtime.env").read_bytes(), before)
            alias = root / "alias"
            alias.symlink_to(private, target_is_directory=True)
            with self.assertRaises(setup.SetupError):
                setup.prepare_directory(alias)
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(setup.SetupError, "outside a Git"):
                setup.prepare_directory(repo / "private")

    def test_identity_mismatch_or_nonfinite_metadata_never_writes_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            output = setup.prepare_directory(Path(directory) / "heater")
            invalid = metadata()
            invalid["device"]["id"] = "anotherdevice001"
            with self.assertRaises(setup.SetupError):
                setup.save_provisioning(output, chosen_device(), invalid)
            invalid = metadata()
            invalid["value"] = float("nan")
            with self.assertRaises(setup.SetupError):
                setup.save_provisioning(output, chosen_device(), invalid)
            self.assertEqual(list(output.iterdir()), [])


class MetadataVerificationTests(unittest.TestCase):
    def test_model_conversion_uses_reported_dps_ranges_and_native_enums(self):
        metadata = onboarding_metadata()
        document = setup.profile_from_metadata(metadata, DEVICE_ID, ADDRESS, "3.3")
        self.assertEqual(document["fields"]["power"]["dp"], "11")
        self.assertEqual(document["fields"]["setpoint"]["minimum"], 5)
        self.assertEqual(document["fields"]["setpoint"]["maximum"], 40)
        self.assertEqual(document["fields"]["power_level"]["values"], ["1", "2", "3", "4"])
        self.assertFalse(document["standby_settings_verified"])

    def test_conflicting_model_or_nonidentity_conversions_fail(self):
        mutations = [
            lambda m: m["device"].update(id="anotherheater001"),
            lambda m: m["local_model"].update(productKey="wrong-product"),
            lambda m: m["local_model"]["dpStatusRelationDTOS"][0].update(valueConvert="unknown"),
            lambda m: m["local_model"]["dpStatusRelationDTOS"][0].update(supportLocal=False),
            lambda m: m["local_model"]["dpStatusRelationDTOS"][0].update(statusFormat='{"switch":"!$"}'),
            lambda m: m["local_model"]["dpStatusRelationDTOS"][1].update(valueDesc='{"unit":"F"}'),
            lambda m: m["local_model"]["dpStatusRelationDTOS"][-1].update(
                enumMappingMap={"1": {"code": "level", "value": "4"}},
            ),
        ]
        for mutate in mutations:
            metadata = onboarding_metadata()
            mutate(metadata)
            with self.assertRaises(setup.SetupError):
                setup.profile_from_metadata(metadata, DEVICE_ID, ADDRESS, "3.3")

    def test_lan_verification_writes_profile_only_after_read_only_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            output = setup.prepare_directory(Path(directory) / "heater")
            setup.save_provisioning(output, chosen_device(), onboarding_metadata())
            implementation, device, _, _ = make_client()
            with patch.object(setup, "discover_device", return_value=(ADDRESS, "3.3")), patch.object(
                setup, "HeaterClient", return_value=implementation,
            ):
                state = setup.verify_private_device(output)
            self.assertFalse(state.enabled)
            self.assertEqual(device.writes, [])
            target = output / "device.json"
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            document = json.loads(target.read_text())
            self.assertEqual(document["device_id"], DEVICE_ID)
            self.assertFalse(document["standby_settings_verified"])
            self.assertNotIn(LOCAL_KEY, target.read_text())
            with self.assertRaises(setup.SetupError):
                setup.verify_private_device(output)

    def test_failed_authentication_never_creates_verified_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            output = setup.prepare_directory(Path(directory) / "heater")
            setup.save_provisioning(output, chosen_device(), onboarding_metadata())
            with patch.object(setup, "discover_device", return_value=(ADDRESS, "3.3")), patch.object(
                setup.HeaterClient, "get_status",
                side_effect=setup.HeaterError("Read-only authentication failed."),
            ):
                with self.assertRaises(setup.SetupError):
                    setup.verify_private_device(output)
            self.assertFalse((output / "device.json").exists())


if __name__ == "__main__":
    unittest.main()
