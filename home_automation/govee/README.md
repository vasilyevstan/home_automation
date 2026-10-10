# Local Govee MCP

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

`govee-mcp` is a separate **LAN-only stdio server** for Govee devices that
support the documented Govee LAN API. It uses only Python's standard library:
there is no optional package, Govee account, developer key, cloud fallback,
BLE connection, background daemon, or listening HTTP service.

## Setup and LAN discovery

In Govee Home, open each supported device's settings and enable **LAN Control**.
The device and MCP host must share a LAN where multicast/broadcast and direct
UDP are permitted. Guest isolation, VLAN boundaries, VPN routing, host
firewalls, and macOS local-network permissions can prevent discovery.

Govee LAN traffic uses:

| UDP port | Direction | Purpose |
| --- | --- | --- |
| `4001` | MCP to multicast/broadcast | Device discovery |
| `4002` | Device to MCP | Discovery and status responses |
| `4003` | MCP to one device | Status requests and explicit controls |

Not every Govee model supports LAN Control. If a documented model does not show
the switch, Govee recommends power-cycling it, waiting for the setting to
appear, and contacting Govee support if it remains unavailable. The server
does not treat zero responders as proof that no Govee devices exist or that
they are off, and it never silently switches to cloud control.

Run from this checkout without pruning already-installed optional extras:

```bash
uv run --locked --inexact govee-mcp
```

Or launch the published source directly:

```bash
uvx --python 3.12 \
  --from "git+https://github.com/vasilyevstan/home_automation.git@main" \
  govee-mcp
```

For an immutable installation, replace `@main` with a verified commit SHA.
Importing or starting the server does not scan or control the LAN; discovery
begins only when a read tool is called.

## Tools and identity

| Tool | Inputs and behavior |
| --- | --- |
| `list_govee_devices` | Optional strict boolean `refresh` (default `true`). Discover LAN-enabled devices by exact device ID and SKU. `false` returns only this server process's in-memory cache. |
| `get_govee_device` | Required exact `device_id`. Rediscover its current address, then read power, brightness, RGB, and color-temperature state. |
| `set_govee_power` | Required exact `device_id` and strict boolean `on`. Set an explicit desired power state and verify fresh status. |
| `set_govee_brightness` | Required exact `device_id` and integer `brightness` from 1 through 100. The device must already be on. |
| `set_govee_color` | Required exact `device_id` and integer `red`, `green`, and `blue` channels from 0 through 255. The device must already be on. |
| `set_govee_color_temperature` | Required exact `device_id` and integer `kelvin` from 2000 through 9000. The device must already be on. |

Use only the exact `device_id` returned by discovery. Display names and IP
addresses are not accepted as targets: names can be duplicated, and DHCP can
assign an old address to another device. Every status or control operation
performs fresh bounded discovery and requires the response's advertised IP to
match its UDP source. Conflicting IDs, addresses, or SKUs fail explicitly.

The inventory is process-local and intentionally not persisted. `refresh=false`
does not prove current reachability. Real device IDs, addresses, SKUs,
firmware versions, names, status captures, and packet traces must stay out of
public source, fixtures, PR descriptions, and CI logs.

## State and control semantics

`get_govee_device` reports observation time, power, brightness, RGB, active
color mode, and color temperature. A protocol color-temperature value of zero
means RGB mode; it is returned as `color_temperature_kelvin=null`, not as a
physical zero-kelvin reading. Omitted or malformed optional fields are null
with an `unavailable` reason and warning, never invented as zero. Power state
is required for a valid reading.

Brightness/color controls require the light to be on. This avoids a color or
brightness command implicitly energizing an off device; use
`set_govee_power(..., on=true)` first only when the user explicitly requests
illumination. Setting RGB disables color-temperature mode, while setting a
color temperature selects that mode. Device-specific limits or quantization
can still reject a nominally documented value.

All writes are explicit desired-state operations, not toggles. The client:

1. rediscovers the exact ID and reads fresh state;
2. sends no command when the requested state already matches;
3. sends at most one mutation, with no automatic command retry; and
4. reads fresh status and requires the requested postcondition.

Govee's UDP protocol does not provide a reliable command acknowledgement.
`verified=true` therefore means a separate status response matched the
requested state. If dispatch, read-back, or comparison is uncertain, the tool
returns an MCP error saying the command may already have taken effect. Read
current state before deciding on another action; blindly replaying a command
can change a device twice around an external app or physical control.

There are no raw-packet, toggle, group, schedule, scene, DIY effect, segment,
animation, firmware, cloud, or bulk-control tools. Discovery and status reads
never intentionally send a light-control command.

## Persistent registration and validation

Install the server in a dedicated environment so synchronizing this base-only
integration does not prune optional dependencies used by other MCPs:

```bash
UV_PROJECT_ENVIRONMENT="$HOME/.local/share/electro/govee-mcp" \
  uv sync --locked --no-editable \
  --reinstall-package estonia-nordpool-prices
"$HOME/.local/share/electro/govee-mcp/bin/govee-mcp"
```

After read-only discovery succeeds, add only the new entry to the existing
private `~/.copilot/mcp-config.json`. Use an absolute path; `~` is not expanded
in the command field, and existing server entries must be preserved:

```json
{
  "mcpServers": {
    "govee-mcp": {
      "type": "stdio",
      "command": "/absolute/path/to/home/.local/share/electro/govee-mcp/bin/govee-mcp",
      "args": [],
      "tools": [
        "list_govee_devices",
        "get_govee_device",
        "set_govee_power",
        "set_govee_brightness",
        "set_govee_color",
        "set_govee_color_temperature"
      ],
      "timeout": 120000
    }
  }
}
```

Existing Copilot sessions may need to reload their MCP configuration.
Registration does not authorize unsolicited physical control tests.

```bash
uv run --locked python -m unittest \
  tests.test_govee tests.test_govee_mcp
```

Tests use synthetic IDs and an injected UDP transport. They cover spoofed and
conflicting discovery responses, status semantics, strict inputs, read-only
behavior, exact payloads, no-op setters, serialized access to response port
4002, single-attempt mutations, and confirmed or uncertain outcomes. Live
acceptance is read-only unless a specific device and desired state are
separately authorized.
