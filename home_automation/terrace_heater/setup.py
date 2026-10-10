"""One-time Tuya app authorization; never sends heater control commands."""

import argparse
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from .client import (
    FIELD_CODES,
    HeaterClient,
    HeaterError,
    discover_device,
    load_environment_file,
    parse_json,
    parse_profile,
    private_text,
    validate_credentials,
)


# Public app identifiers used by Tuya's sharing SDK and Tuya Local, not secrets.
APP_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
APP_SCHEMA = "haauthorize"
PROVISION_FILES = ("onboarding.json", "runtime.env")
DEVICE_ID_PATTERN = re.compile(r"[A-Za-z0-9]{10,64}")
TUYA_DOMAINS = (
    "iotbing.com", "tuya.com", "tuyaeu.com", "tuyaus.com",
    "tuyacn.com", "tuyaind.com",
)


class SetupError(Exception):
    """An expected setup failure whose message is safe to display."""


@dataclass(frozen=True)
class DeviceChoice:
    device_id: str
    name: str
    product_id: Optional[str]
    local_key: Optional[str] = field(repr=False)


def _result(response: object, operation: str) -> object:
    if not isinstance(response, dict) or response.get("success") is not True:
        raise SetupError(
            "Tuya did not authorize {}. Check the app authorization and retry; "
            "no device control was attempted.".format(operation)
        )
    return response.get("result")


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _device(item: object) -> DeviceChoice:
    if not isinstance(item, dict):
        raise SetupError("Tuya returned an invalid device list.")
    device_id = item.get("id")
    if not isinstance(device_id, str) or not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise SetupError("Tuya returned an invalid device identifier.")
    name = item.get("name")
    product_id = item.get("product_id")
    local_key = item.get("local_key")
    return DeviceChoice(
        device_id,
        name if _text(name) else "(unnamed device)",
        product_id if isinstance(product_id, str) else None,
        local_key if isinstance(local_key, str) else None,
    )


def _validate_key(device: DeviceChoice) -> str:
    key = device.local_key
    if (
        not isinstance(key, str) or len(key) != 16 or not key.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in key)
    ):
        raise SetupError(
            "Tuya did not provide a usable local key for the selected device. "
            "Direct LAN access cannot be configured without it."
        )
    return key


def _endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise SetupError("Tuya did not return a supported authentication endpoint.")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        valid = (
            parts.scheme == "https" and parts.port in (None, 443)
            and parts.username is None and parts.password is None
            and parts.path in ("", "/") and not parts.query and not parts.fragment
            and any(host == domain or host.endswith("." + domain) for domain in TUYA_DOMAINS)
        )
    except ValueError:
        valid = False
    if not valid:
        raise SetupError("Tuya returned an unsupported authentication endpoint; credentials were not forwarded.")
    return value.rstrip("/")


class TuyaAppSession:
    def __init__(self) -> None:
        try:
            from cryptography.exceptions import InvalidTag
            from requests.exceptions import RequestException
            from tuya_sharing.customerapi import CustomerApi, CustomerTokenInfo
            from tuya_sharing.customerlogging import logger
            from tuya_sharing.exceptions import ApiRequestException
            from tuya_sharing.user import LoginControl
        except ImportError:
            raise SetupError(
                "Install setup dependencies with: uv sync --extra heater-setup"
            ) from None
        self._api_type = CustomerApi
        self._token_type = CustomerTokenInfo
        self._request_errors = (RequestException, ApiRequestException, InvalidTag, ValueError, OSError)
        self._logger = logger
        self._was_disabled = logger.disabled
        logger.disabled = True
        self._login = LoginControl()
        self._login.session.max_redirects = 0
        self._api = None
        self._user_code = ""
        self._qr_token = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._api is not None:
            self._api.session.close()
            self._api.token_info.access_token = ""
            self._api.token_info.refresh_token = ""
            self._api.user_code = ""
            self._api = None
        self._login.session.close()
        self._user_code = ""
        self._qr_token = ""
        self._logger.disabled = self._was_disabled

    def request_qr(self, user_code: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9]{4,64}", user_code):
            raise SetupError("Enter the User Code from the app's Account and Security page.")
        try:
            response = self._login.qr_code(APP_CLIENT_ID, APP_SCHEMA, user_code)
        except self._request_errors:
            raise SetupError("Could not request Tuya app authorization; check connectivity and retry.") from None
        result = _result(response, "QR authorization")
        token = result.get("qrcode") if isinstance(result, dict) else None
        if (
            not isinstance(token, str) or not token or len(token) > 4096
            or any(ord(character) < 33 or ord(character) == 127 for character in token)
        ):
            raise SetupError("Tuya did not return a usable QR authorization token.")
        self._user_code = user_code
        self._qr_token = token
        return token

    def confirm_login(self) -> None:
        if not self._user_code or not self._qr_token:
            raise SetupError("Request and scan the QR code before completing authorization.")
        try:
            success, info = self._login.login_result(
                self._qr_token, APP_CLIENT_ID, self._user_code,
            )
        except self._request_errors:
            raise SetupError("Could not complete Tuya app authorization; check connectivity and retry.") from None
        if success is not True or not isinstance(info, dict):
            raise SetupError("Tuya app authorization was denied, incomplete, or expired. Restart setup and approve the QR code.")
        for name in ("uid", "access_token", "refresh_token"):
            if not _text(info.get(name)):
                raise SetupError("Tuya returned incomplete app authorization data.")
        if type(info.get("t")) is not int or type(info.get("expire_time")) is not int:
            raise SetupError("Tuya returned invalid authorization expiry data.")
        if info["expire_time"] <= 0 or info["t"] + info["expire_time"] * 1000 <= (time.time() + 60) * 1000:
            raise SetupError("Tuya app authorization has expired. Restart setup.")
        endpoint = _endpoint(info.get("endpoint"))
        self._api = self._api_type(
            self._token_type(info), APP_CLIENT_ID, self._user_code, endpoint, None,
        )
        self._api.session.max_redirects = 0
        self._qr_token = ""

    def _get(self, path: str, operation: str, params=None) -> object:
        if self._api is None:
            raise SetupError("Complete app authorization before requesting device data.")
        if self._api.token_info.expire_time <= (time.time() + 60) * 1000:
            raise SetupError("Tuya app authorization has expired. Restart setup.")
        try:
            response = self._api.get(path, params)
        except self._request_errors:
            raise SetupError("Tuya {} request failed; check authorization and connectivity.".format(operation)) from None
        return _result(response, operation)

    def list_devices(self) -> List[DeviceChoice]:
        homes = self._get("/v1.0/m/life/users/homes", "home listing")
        if not isinstance(homes, list):
            raise SetupError("Tuya returned an invalid home list.")
        devices: Dict[str, DeviceChoice] = {}
        for home in homes:
            if not isinstance(home, dict) or type(home.get("ownerId")) not in (str, int):
                raise SetupError("Tuya returned an invalid home identifier.")
            rows = self._get(
                "/v1.0/m/life/ha/home/devices", "device listing",
                {"homeId": str(home["ownerId"])},
            )
            if not isinstance(rows, list):
                raise SetupError("Tuya returned an invalid device list.")
            for row in rows:
                device = _device(row)
                previous = devices.get(device.device_id)
                if previous is not None and previous.local_key != device.local_key:
                    raise SetupError("Tuya returned conflicting data for one device; no device was selected.")
                devices[device.device_id] = device
        if not devices:
            raise SetupError("No devices were returned. Check that the User Code belongs to the app account containing the heater.")
        return sorted(devices.values(), key=lambda device: (device.name, device.device_id))

    def selected_metadata(self, device: DeviceChoice) -> dict:
        _validate_key(device)
        specifications = self._get(
            "/v1.1/m/life/{}/specifications".format(device.device_id),
            "selected-device specifications",
        )
        model = self._get(
            "/v1.0/m/life/devices/{}/status".format(device.device_id),
            "selected-device local model",
        )
        if not isinstance(specifications, dict) or not isinstance(model, dict):
            raise SetupError("Tuya returned an invalid selected-device data model.")
        functions = _select_fields(
            specifications.get("functions"), ("code", "type", "values"),
        )
        status = _select_fields(
            specifications.get("status"), ("code", "type", "values"),
        )
        relations = _select_fields(
            model.get("dpStatusRelationDTOS"),
            (
                "dpId", "dpCode", "statusCode", "supportLocal", "valueType",
                "valueDesc", "valueConvert", "statusFormat", "enumMappingMap",
            ),
        )
        if not relations or not functions or not status:
            raise SetupError("Tuya did not expose a complete local data model. Heater controls cannot yet be configured.")
        return {
            "schema_version": 1,
            "verification": "requires_lan_and_mapping_verification",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "device": {
                "id": device.device_id, "name": device.name,
                "product_id": device.product_id,
            },
            "specifications": {"functions": functions, "status": status},
            "local_model": {
                "productKey": model.get("productKey"),
                "dpStatusRelationDTOS": relations,
            },
        }


def _select_fields(rows: object, fields) -> list:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SetupError("Tuya returned an invalid selected-device data model.")
    return [{name: row[name] for name in fields if name in row} for row in rows]


def prepare_directory(value: Path) -> Path:
    path = value.expanduser().absolute()
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise SetupError("Provisioning must be stored outside a Git repository.")
    try:
        if path.is_symlink():
            raise SetupError("The private output directory must not be a symbolic link.")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if os.name == "posix" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise SetupError("The private output directory must be owned by this user with mode 700.")
        if any((path / name).exists() or (path / name).is_symlink() for name in PROVISION_FILES):
            raise SetupError("Provisioning files already exist. Choose a new private --output-dir; existing credentials will not be overwritten.")
    except OSError:
        raise SetupError("Could not prepare the private output directory; check its path and permissions.") from None
    return path


def save_provisioning(path: Path, device: DeviceChoice, metadata: dict) -> None:
    key = _validate_key(device)
    if metadata.get("device", {}).get("id") != device.device_id:
        raise SetupError("Selected device and metadata identity do not match.")
    try:
        capture = json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError):
        raise SetupError("Tuya returned metadata that cannot be safely saved.") from None
    environment = "# Private POSIX launcher environment; never commit or print this file.\n"
    for name, value in (
        ("TERRACE_HEATER_DEVICE_ID", device.device_id),
        ("TERRACE_HEATER_LOCAL_KEY", key),
        ("TERRACE_HEATER_CONFIG", str(path / "device.json")),
    ):
        environment += "export {}={}\n".format(name, shlex.quote(value))
    created = []
    try:
        with ExitStack() as stack:
            streams = []
            for name in PROVISION_FILES:
                target = path / name
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags, 0o600)
                created.append(target)
                streams.append(stack.enter_context(os.fdopen(descriptor, "w", encoding="utf-8")))
            for stream, content in zip(streams, (capture, environment)):
                stream.write(content)
    except (OSError, KeyboardInterrupt) as error:
        for target in created:
            try:
                target.unlink()
            except OSError:
                raise SetupError("Provisioning failed and an incomplete private file could not be removed. Check the output directory.") from None
        if isinstance(error, KeyboardInterrupt):
            raise
        raise SetupError("Could not save private provisioning; no existing files were overwritten.") from None


def render_qr(token: str) -> None:
    try:
        import qrcode
    except ImportError:
        raise SetupError("Install setup dependencies with: uv sync --extra heater-setup") from None
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_Q)
    qr.add_data("tuyaSmart--qrLogin?token=" + token)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def profile_from_metadata(metadata: object, device_id: str, address: str, protocol: str) -> dict:
    if (
        not isinstance(metadata, dict) or metadata.get("schema_version") != 1
        or not isinstance(metadata.get("device"), dict)
        or metadata["device"].get("id") != device_id
        or not isinstance(metadata.get("specifications"), dict)
        or not isinstance(metadata.get("local_model"), dict)
    ):
        raise SetupError("Selected-device metadata is invalid or does not match the private Device ID.")
    identity = metadata["device"]
    if metadata["local_model"].get("productKey") != identity.get("product_id"):
        raise SetupError("Account and LAN data-model product identities do not match.")

    def indexed(rows, key):
        if not isinstance(rows, list):
            raise SetupError("Selected-device metadata has incomplete function mappings.")
        result = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get(key), str) or row[key] in result:
                raise SetupError("Selected-device metadata has invalid or duplicate function mappings.")
            result[row[key]] = row
        return result

    functions = indexed(metadata["specifications"].get("functions"), "code")
    statuses = indexed(metadata["specifications"].get("status"), "code")
    relations = indexed(metadata["local_model"].get("dpStatusRelationDTOS"), "dpCode")
    fields = {}
    for name, (code, kind, writable) in FIELD_CODES.items():
        relation = relations.get(code)
        status = statuses.get(code)
        function = functions.get(code)
        if relation is None or status is None or (writable and function is None):
            if name == "power":
                raise SetupError("The heater has no verified power-switch mapping.")
            continue
        if (
            relation.get("supportLocal") is not True
            or relation.get("valueConvert") != "default"
            or relation.get("statusCode") != code or relation.get("valueType") != kind
            or status.get("type") != kind or (writable and function.get("type") != kind)
            or type(relation.get("dpId")) is not int
        ):
            raise SetupError("A heater capability requires an unsupported local conversion or type.")
        try:
            values = parse_json(relation["valueDesc"])
            status_values = parse_json(status["values"])
            function_values = parse_json(function["values"]) if writable else values
            status_format = parse_json(relation["statusFormat"])
        except (HeaterError, KeyError):
            raise SetupError("A heater capability has invalid or missing value metadata.") from None
        if not isinstance(values, dict) or values != status_values or values != function_values or status_format != {code: "$"}:
            raise SetupError("Local and account capability metadata disagree; manual verification is required.")
        item = {"dp": str(relation["dpId"]), "kind": kind, "writable": writable}
        if kind == "Integer":
            if values.get("unit") not in ("C", "\u00b0C"):
                raise SetupError("Only verified Celsius temperature data points are supported.")
            try:
                item.update({
                    "minimum": values["min"], "maximum": values["max"],
                    "step": values["step"], "scale": values["scale"], "unit": "C",
                })
            except KeyError:
                raise SetupError("Temperature metadata lacks a range, step, or scale.") from None
        elif kind == "Enum":
            options = values.get("range")
            mapping = relation.get("enumMappingMap")
            if not isinstance(options, list) or not isinstance(mapping, dict):
                raise SetupError("Heater enum metadata is incomplete.")
            for option in options:
                if not isinstance(option, str):
                    raise SetupError("Heater enum metadata has an unsupported value.")
                if mapping and mapping.get(option) != {"code": code, "value": option}:
                    raise SetupError("Heater enum conversion is not an identity mapping; verify it before control.")
            item["values"] = options
        fields[name] = item
    document = {
        "schema_version": 1,
        "device_id": device_id,
        "name": identity.get("name"),
        "product_id": identity.get("product_id"),
        "protocol_version": protocol,
        "address_hint": address,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "standby_settings_verified": False,
        "fields": fields,
    }
    try:
        parse_profile(document, device_id)
    except HeaterError as error:
        raise SetupError(str(error)) from None
    return document


def verify_private_device(directory: Path):
    directory = directory.expanduser().absolute()
    target = directory / "device.json"
    if target.exists() or target.is_symlink():
        raise SetupError("A verified device.json already exists; it will not be overwritten.")
    try:
        environment = load_environment_file(directory / "runtime.env")
        credentials = validate_credentials(
            environment.get("TERRACE_HEATER_DEVICE_ID"),
            environment.get("TERRACE_HEATER_LOCAL_KEY"),
        )
        if environment.get("TERRACE_HEATER_CONFIG") != str(target):
            raise SetupError("The private environment points to a different device profile.")
        metadata = parse_json(private_text(directory / "onboarding.json"))
        if not isinstance(metadata, dict) or not isinstance(metadata.get("device"), dict) or metadata["device"].get("id") != credentials.device_id:
            raise SetupError("Provisioned metadata and credentials identify different devices.")
        address, protocol = discover_device(credentials.device_id, metadata["device"].get("product_id"))
        document = profile_from_metadata(metadata, credentials.device_id, address, protocol)
        profile = parse_profile(document, credentials.device_id)
        client = HeaterClient(
            credentials_provider=lambda: credentials,
            profile_provider=lambda device_id: profile,
        )
        state = client.get_status()
    except HeaterError as error:
        raise SetupError(str(error)) from None
    try:
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError:
        raise SetupError("Could not create the private verified profile; no existing file was overwritten.") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n")
    except (OSError, ValueError):
        try:
            target.unlink()
        except OSError:
            raise SetupError("Profile write failed and the incomplete file could not be removed; check the private directory.") from None
        raise SetupError("The verified profile could not be saved.") from None
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path.home() / ".config" / "electro" / "terrace-heater",
        help="Private directory outside Git; existing provisioning is never overwritten.",
    )
    parser.add_argument(
        "--verify-lan", action="store_true",
        help="Use existing private provisioning to verify LAN status and create device.json; never changes heater settings.",
    )
    args = parser.parse_args(argv)
    try:
        if args.verify_lan:
            state = verify_private_device(args.output_dir)
            print("Authenticated LAN status verified and private device.json created.")
            print("Heater enabled: {}. Native mode: {}. Native power level: {}.".format(
                state.enabled, state.mode, state.power_level,
            ))
            print("Reported temperature: {} C; measurement freshness is not reported.".format(state.temperature_c))
            print("No heater control commands were sent. Settings changes while off remain disabled.")
            return 0
        if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
            raise SetupError("Run setup in a private interactive terminal; User Codes are not accepted through arguments or redirected input.")
        output = prepare_directory(args.output_dir)
        print("One-time Tuya app authorization using the Home Assistant-compatible SDK flow.")
        print("No Home Assistant installation is needed. No heater control commands will be sent.")
        print("Check the requesting application before approving the QR code in Tuya Smart.")
        with TuyaAppSession() as session:
            code = getpass.getpass("Tuya User Code (hidden): ")
            token = session.request_qr(code)
            code = ""
            render_qr(token)
            token = ""
            input("Scan and approve this QR code in Tuya Smart, then press Enter (Ctrl+C cancels): ")
            session.confirm_login()
            devices = session.list_devices()
            for index, device in enumerate(devices, 1):
                print("{}: {} (Device ID ending {})".format(
                    index, json.dumps(device.name, ensure_ascii=True), device.device_id[-8:],
                ))
            choice = input("Select the heater by number (no default): ")
            if not choice.isascii() or not choice.isdecimal() or not 1 <= int(choice) <= len(devices):
                raise SetupError("No valid device was selected; no provisioning files were written.")
            device = devices[int(choice) - 1]
            metadata = session.selected_metadata(device)
            save_provisioning(output, device, metadata)
        print("Saved private credentials and selected-device metadata to {}.".format(output))
        print("LAN verification is still required: run this command with --verify-lan to create device.json without changing heater settings.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled. No heater control commands were sent.", file=sys.stderr)
        return 130
    except SetupError as error:
        print("Setup error: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
