"""Separate local stdio MCP for Starlink telemetry and opt-in controls."""

import sys
from typing import Annotated, Literal, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import (
    Capabilities, ControlResult, History, Obstructions, StarlinkClient,
    StarlinkError, StarlinkStatus, ensure_runtime, load_settings,
)


mcp = MCPServer(
    "starlink-mcp",
    instructions=(
        "Read Starlink dish telemetry over its trusted LAN. Report download and upload "
        "throughput in Mbps: these are aggregate traffic rates, not a speed test, maximum "
        "bandwidth, or per-client traffic. Zero traffic can be normal. Preserve unavailable "
        "readings, history coverage, units, and relative timestamps. Schema presence does "
        "not prove permission to execute a control. Reboot, sleep, and snow-melt controls "
        "are individually disabled unless locally enabled, and require an exact configured "
        "dish identity and explicit user request. Reboot/sleep interrupt internet; reboot "
        "may apply a pending update and snow melting may increase power use. Never test "
        "controls, infer automation, retry an unconfirmed command, change GPS/router settings, "
        "or expose raw debug data. Read status first after an uncertain outcome."
    ),
)
_client = StarlinkClient()
_READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
)
_SET = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
)
_REBOOT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
)
StrictBool = Annotated[bool, Field(strict=True)]
SleepStart = Annotated[int, Field(strict=True, ge=0, le=1439)]
SleepDuration = Annotated[int, Field(strict=True, ge=1, le=1440)]


@mcp.tool(title="Read Starlink status and download/upload throughput", annotations=_READ)
def get_starlink_status() -> StarlinkStatus:
    """Read fresh dish health and aggregate download/upload rates in bps and Mbps.

    Latency is in ms, loss is a ratio, and throughput is current traffic rather
    than available bandwidth. No active speed test or control command is sent.
    """
    try:
        return _client.get_status()
    except StarlinkError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Read Starlink configuration and control capabilities", annotations=_READ)
def get_starlink_capabilities() -> Capabilities:
    """Read configuration and separate schema presence, local opt-in, and write evidence.

    Never executes a control to test support. Unknown support is not permission.
    """
    try:
        return _client.get_capabilities()
    except StarlinkError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Read recent Starlink throughput and quality history", annotations=_READ)
def get_starlink_history(
    samples: Annotated[int, Field(strict=True, ge=1, le=900)] = 900,
    include_samples: StrictBool = False,
) -> History:
    """Summarize up to 900 nominal one-second samples, optionally returning each sample.

    Includes download/upload averages and peaks, latency, packet loss, power,
    and actual coverage. This is a reboot-sensitive device ring, not an archive.
    Relative sample offsets are not exact wall-clock measurement times.
    """
    try:
        return _client.get_history(samples, include_samples)
    except StarlinkError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Read Starlink obstruction summary", annotations=_READ)
def get_starlink_obstructions() -> Obstructions:
    """Read device-reported obstruction statistics and compact map metadata.

    Missing or not-yet-valid statistics are not zero. No map reset is performed.
    """
    try:
        return _client.get_obstructions()
    except StarlinkError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Explicitly reboot Starlink (interrupts internet)", annotations=_REBOOT)
def reboot_starlink(confirm_disruption: StrictBool) -> ControlResult:
    """Reboot once only when explicitly requested and locally enabled.

    Requires confirm_disruption=true. May apply a pending firmware update.
    Acknowledgement or disconnect alone does not verify a reboot. If uncertain,
    check status later; never automatically repeat the command.
    """
    try:
        return _client.reboot(confirm_disruption)
    except StarlinkError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Explicitly set Starlink's UTC sleep schedule", annotations=_SET)
def set_starlink_sleep_schedule(
    enabled: StrictBool,
    start_minutes_utc: Optional[SleepStart] = None,
    duration_minutes: Optional[SleepDuration] = None,
) -> ControlResult:
    """Set an explicitly requested daily sleep schedule, which can interrupt internet.

    Enabling requires start 0..1439 minutes after UTC midnight and duration
    1..1440 minutes. Disabling omits both, preserving the existing schedule.
    The start does not track local daylight-saving time. Verify by fresh read.
    """
    try:
        return _client.set_sleep(enabled, start_minutes_utc, duration_minutes)
    except StarlinkError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Explicitly set Starlink snow-melt mode", annotations=_SET)
def set_starlink_snow_melt_mode(mode: Literal["auto", "always_on", "always_off"]) -> ControlResult:
    """Set only snow-melt mode and verify it, when explicitly requested and enabled.

    Forced heating may increase power use; the setting is not measured power
    or proof that heating is active. Do not use for unattended control tests.
    """
    try:
        return _client.set_snow_melt(mode)
    except StarlinkError as error:
        raise ToolError(str(error)) from None


def main() -> None:
    try:
        ensure_runtime()
        load_settings()
    except StarlinkError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
