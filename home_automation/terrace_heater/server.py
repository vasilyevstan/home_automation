"""Separate local-only MCP for one privately provisioned terrace heater."""

import argparse
import os
from pathlib import Path
import sys
from typing import Annotated, Union

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StrictFloat, StrictInt, StrictStr

from .client import (
    HeaterClient, HeaterControlResult, HeaterError, HeaterStatus, _sdk,
    load_environment_file,
)


mcp = MCPServer(
    "terrace-heater-mcp",
    instructions=(
        "Read and explicitly control only the privately configured terrace heater "
        "over its LAN. Identify it by its Device ID, never an old IP. Reads and "
        "server startup never enable heating. Report the heater's temperature "
        "with its availability and unknown sensor freshness; an off-state value "
        "may be retained, not a current room measurement. Enabled is not proof "
        "of active heating. Native power levels are not percentages or measured "
        "watts. Use only verified modes/temperature limits. Never change settings "
        "or power without an explicit user request, infer a schedule, toggle, or "
        "test a heater unattended. A failed command can have an uncertain physical "
        "outcome; do not claim it is off without fresh confirmation. Credentials "
        "and account onboarding are never tool arguments or outputs."
    ),
)
_client = HeaterClient()
_READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False,
    idempotent_hint=True, open_world_hint=True,
)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True,
    idempotent_hint=True, open_world_hint=True,
)


@mcp.tool(title="Read terrace heater state and reported temperature", annotations=_READ)
def get_terrace_heater_status() -> HeaterStatus:
    """Read one heater's state, setpoint, native mode/level, and verified capabilities.

    Never enables heating. Missing readings are not zero. Retrieval time is not
    measurement time; the device may retain a temperature reading while off.
    """
    try:
        return _client.get_status()
    except HeaterError as error:
        raise ToolError(str(error)) from None


def _set(name: str, value) -> HeaterControlResult:
    try:
        return _client.set_value(name, value)
    except HeaterError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Explicitly switch terrace heater on or off", annotations=_WRITE)
def set_terrace_heater_power(enabled: Annotated[bool, Field(strict=True)]) -> HeaterControlResult:
    """Set an explicitly requested power state, never toggle, and verify read-back.

    Turning on can immediately produce heat. Do not use for unattended tests.
    Repeating an already-matching state sends no command. An uncertain outcome
    is an error, not a confirmed shutdown; check the device before retrying.
    """
    return _set("power", enabled)


@mcp.tool(title="Set terrace heater thermostat target", annotations=_WRITE)
def set_terrace_heater_temperature(temperature_c: Union[StrictFloat, StrictInt]) -> HeaterControlResult:
    """Set a verified Celsius thermostat target without implicitly enabling heating.

    Respect reported bounds and step; values are never rounded or clamped.
    A change while off is rejected unless standby setting behavior was verified.
    """
    return _set("setpoint", temperature_c)


@mcp.tool(title="Set terrace heater native heating mode", annotations=_WRITE)
def set_terrace_heater_mode(mode: StrictStr) -> HeaterControlResult:
    """Select an exact native mode from status.capabilities.modes.

    Do not infer the meaning of native labels or substitute invented eco/manual
    modes. Does not implicitly enable heating; verifies the resulting setting.
    """
    return _set("mode", mode)


@mcp.tool(title="Set terrace heater native power level", annotations=_WRITE)
def set_terrace_heater_power_level(level: StrictStr) -> HeaterControlResult:
    """Select a native level from status.capabilities.power_levels, e.g. "1".

    Levels are device labels, not a verified percentage-to-watt conversion.
    Does not implicitly enable heating; verifies the resulting setting.
    """
    return _set("power_level", level)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path,
        help="Explicit private provisioning file, parsed as data rather than executed by a shell.",
    )
    args = parser.parse_args(argv)
    try:
        if args.env_file is not None:
            if any(name in os.environ for name in (
                "TERRACE_HEATER_DEVICE_ID", "TERRACE_HEATER_LOCAL_KEY", "TERRACE_HEATER_CONFIG",
            )):
                raise HeaterError("Use either --env-file or the heater environment variables, not a mixture of both.")
            os.environ.update(load_environment_file(args.env_file))
        _sdk()
    except HeaterError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
