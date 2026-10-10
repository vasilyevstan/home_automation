"""Separate stdio MCP for local Govee lights and explicit desired-state controls."""

import sys
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StrictStr

from .client import ControlResult, DeviceInventory, DeviceState, GoveeClient, GoveeError


mcp = MCPServer(
    "govee-mcp",
    instructions=(
        "Discover local Govee devices with LAN Control enabled and identify every "
        "target by the exact device ID returned by discovery, never a display name "
        "or old IP address. Read fresh status before deciding on a physical change. "
        "An empty discovery result does not prove devices are absent or off; check "
        "model support, Govee Home LAN Control, client isolation, multicast, firewall, "
        "and local-network permissions. Only perform the exact desired-state change "
        "requested by the user. Never toggle, send raw commands, control multiple "
        "devices, test a light unsolicited, or use undocumented scenes/DIY effects. "
        "Brightness and color changes require the light to be on so they cannot "
        "implicitly energize an off device. A write is sent at most once and must be "
        "confirmed by fresh status. An uncertain outcome may already have taken "
        "effect: read current state and never blindly repeat the tool call."
    ),
)
_client = GoveeClient()
_READ = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)


async def _call(method, *args):
    try:
        return await method(*args)
    except GoveeError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Discover local Govee devices by exact ID", annotations=_READ)
async def list_govee_devices(
    refresh: Annotated[bool, Field(strict=True)] = True,
) -> DeviceInventory:
    """Discover LAN-enabled devices or return this process's cached inventory.

    Discovery and cached listing never issue a light-control command. A device
    not seen in a bounded refresh is not assumed off or removed from Govee Home.
    """
    return await _call(_client.list_devices, refresh)


@mcp.tool(title="Read one Govee device's current light state", annotations=_READ)
async def get_govee_device(device_id: StrictStr) -> DeviceState:
    """Rediscover one exact device ID and read power, brightness, and color state.

    Null status fields are unavailable, not zero. A zero protocol color
    temperature denotes RGB mode and is returned as null with that mode labeled.
    """
    return await _call(_client.get_device, device_id)


@mcp.tool(title="Explicitly set one Govee device's power", annotations=_WRITE)
async def set_govee_power(
    device_id: StrictStr, on: Annotated[bool, Field(strict=True)]
) -> ControlResult:
    """Set one exact device on or off and verify the resulting status.

    This can illuminate or darken a physical space. A matching state is a no-op;
    an unconfirmed command is an error and is never automatically retried.
    """
    return await _call(_client.set_power, device_id, on)


@mcp.tool(title="Set one powered-on Govee device's brightness", annotations=_WRITE)
async def set_govee_brightness(
    device_id: StrictStr,
    brightness: Annotated[int, Field(strict=True, ge=1, le=100)],
) -> ControlResult:
    """Set explicit brightness from 1 through 100 and verify fresh status.

    The device must already be on. Use set_govee_power explicitly first when the
    user requests illumination; this tool never implicitly powers on a light.
    """
    return await _call(_client.set_brightness, device_id, brightness)


@mcp.tool(title="Set one powered-on Govee device's RGB color", annotations=_WRITE)
async def set_govee_color(
    device_id: StrictStr,
    red: Annotated[int, Field(strict=True, ge=0, le=255)],
    green: Annotated[int, Field(strict=True, ge=0, le=255)],
    blue: Annotated[int, Field(strict=True, ge=0, le=255)],
) -> ControlResult:
    """Set exact RGB channels, disable color-temperature mode, and verify status.

    The device must already be on. This does not select a scene, DIY effect,
    segment, animation, or group.
    """
    return await _call(_client.set_color, device_id, red, green, blue)


@mcp.tool(
    title="Set one powered-on Govee device's color temperature",
    annotations=_WRITE,
)
async def set_govee_color_temperature(
    device_id: StrictStr,
    kelvin: Annotated[int, Field(strict=True, ge=2000, le=9000)],
) -> ControlResult:
    """Set explicit color temperature from 2000 through 9000 K and verify status.

    The device must already be on. Device-specific limits can still reject or
    quantize a value; a read-back mismatch is reported as an uncertain outcome.
    """
    return await _call(_client.set_color_temperature, device_id, kelvin)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
