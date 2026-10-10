# Local Tapo socket MCP

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

`tp-tapo-mcp` is a separate **local stdio server** for single-outlet Tapo plugs.
It uses the community-maintained
[`python-kasa`](https://python-kasa.readthedocs.io/en/stable/) client, not a cloud
relay service. The existing price, forecast, consumption, and pricing servers
do not require the new dependency or change their behavior.

## Installation and tools

Use Python 3.11+ for this server; the checkout already selects Python 3.12.
The base package and existing entry points continue to support Python 3.10.

```bash
uv sync --locked --extra tapo
uv run --locked --extra tapo tp-tapo-mcp
```

Without a checkout, install the published server and its optional extra:

```bash
uvx --python 3.12 \
  --from "estonia-nordpool-prices[tapo] @ git+https://github.com/vasilyevstan/home_automation.git@main" \
  tp-tapo-mcp
```

For an immutable installation, replace `@main` with `@<commit-sha>`.

Keep `--extra tapo` when launching with `uv run`. Starting this MCP without the
extra or on Python 3.10 fails with an explicit setup error instead of silently
omitting its functionality. Startup itself neither contacts nor switches plugs.

| Tool | Inputs and behavior |
| --- | --- |
| `list_tapo_plugs` | Optional boolean `refresh` (default `true`). Discover local plugs and merge them with known MACs. `false` returns cached inventory only. No credentials or relay commands are used. |
| `get_tapo_plug` | Required `mac`. Authenticate the exact plug, verify its MAC, and read its relay state, electrical measurements, and supported operating sensors. |
| `set_tapo_plug_power` | Required `mac` and explicit boolean `on`. Set the desired state and verify it with a fresh authenticated read. This tool changes a physical load. |

MACs use six hexadecimal octets separated by colons or hyphens; output uses
uppercase colon notation, for example the **synthetic** `02:00:00:00:00:01`.
Names and old IPs are not accepted as control targets. Duplicate/conflicting
identities fail explicitly. If a cached address has changed, the client makes
one bounded rediscovery and rechecks the authenticated MAC rather than sending
a command to the new occupant of an old address.

There are no toggle, bulk-control, scheduling, firmware-update, generic
feature-setting, or price-based automation tools. Only request a power change
for a specific socket and a load that is safe to interrupt. Discovery and
reading never issue a relay command, including when checking capabilities.

## Local credentials and compatibility

Discovery works without credentials. Authenticated readings and control need
the TP-Link account that owns the plugs, available **locally**:

```bash
security add-generic-password -s tp-tapo-mcp -a username -w
security add-generic-password -s tp-tapo-mcp -a password -w
```

Run these in your own terminal. Keep `-w` last so the command prompts rather
than including the credential value in command history or process arguments.
Use the account's TP-Link ID/email as `username`. Do not use unrestricted
Keychain access (`-A`). Missing, locked, empty, or inaccessible entries produce
explicit errors. Keychain credentials are read when a data/control tool runs.

Alternatively, supply both `TAPO_USERNAME` and `TAPO_PASSWORD` to the MCP process
using a secure environment mechanism. An explicit environment pair takes
precedence over Keychain; an incomplete or empty pair fails instead of mixing
sources. Non-macOS hosts need the environment pair. Setting variables in an
unrelated terminal does not update an already-running MCP process.

**GitHub Actions secrets are not a runtime credential source for a local MCP.**
GitHub exposes their names and metadata, not their saved values. Keeping
GitHub copies does not populate Keychain or the MCP process environment.
Do not paste credentials into conversations or add them to tool arguments,
MCP configuration, source, the device registry, or logs. No secret-export
workflow is provided.

Discovery uses local UDP broadcasts on ports 9999 and 20002. The host and
plugs need suitable LAN connectivity; guest isolation, VLAN boundaries,
firewalls, and host local-network permissions can prevent discovery.
An unplugged socket cannot respond or be switched back on over the network.

Some firmware advertises TPAP, which the selected client does not support.
Such devices remain visible in inventory as `unsupported_protocol`, not as
an empty network. TP-Link's optional
[Third-Party Compatibility setting](https://www.tp-link.com/us/support/faq/4416/)
may expose a compatible protocol. The server does not change this setting or
assume that enabling it guarantees access: rediscovery and a successful
authenticated read are still required. Do not install an unreviewed protocol
fork or downgrade firmware to work around an error.

## Readings and power-change outcomes

Both `get_tapo_plug` and a verified power-setting result include consumption
when on. `measurements` distinguishes:

| Field | Unit and meaning |
| --- | --- |
| `power` | W, measured instantaneous power |
| `energy_today` | kWh, the device's current local-day counter |
| `energy_this_month` | kWh, the device's current local-month counter |
| `energy_total` | kWh since reboot, only if that capability is exposed |
| `voltage` | V, when supported |
| `current` | A, when supported |

Each measurement has `value`, `unit`, `status`, `reason`, and `period`.
`available` may legitimately contain zero; `unsupported` and `unavailable`
contain null, never an invented zero. Availability is detected from the device,
not assumed from a model name. Operating sensors are returned separately;
private network/account identifiers and writable features are not dumped.

An on relay does not imply a positive load. Turning a socket off does not
reset its daily/monthly energy counters. Power in W is not energy in kWh;
there is no background sampling, historical database, or cost calculation.
Counter boundaries follow the device's clock. The response preserves its
reported time and collection time rather than assuming the host timezone.

Power setting is a desired-state operation, not a toggle. If the relay is
already in the requested state, no command is sent and `acknowledged` is null.
Otherwise, the client sends one command with automatic write retries disabled.
A new authenticated connection reads back the state and consumption, avoiding
the library's cached energy-update interval.

`verified=true` means the requested state was observed at read-back, not that
it will remain unchanged or that a connected appliance is drawing power.
A lost acknowledgement may be resolved by read-back, with an explicit warning.
If the outcome cannot be confirmed, the tool returns an MCP error saying the
command may have taken effect; inspect state rather than blindly repeating it.
External applications and manual buttons can still change a plug's state.

## Private inventory and registration

Known devices persist in `~/.config/electro/tapo/devices.json`, keyed by MAC.
The directory/file use permissions 700/600; writes replace the file atomically.
The registry stores only identity, last-known addresses/labels, and capability
metadata, not credentials or measurement history. Invalid or conflicting
registry data is an error, not a reason to reset the inventory.

Devices not seen during a refresh remain listed with their last-known metadata.
`not_seen` does not prove an off relay or physical disconnection.
`capabilities_verified_at` labels the age of cached capabilities; listing does
not reauthenticate devices. Keep real inventories, MACs, readings, and private
device captures out of public source, examples, PRs, and CI output.

After read-only local verification, add only the new entry to the existing
user-level MCP configuration. This example uses the published source; pin a
verified commit for an immutable installation. Do not replace existing server
entries or add credentials to the configuration:

```json
{
  "mcpServers": {
    "tp-tapo-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--python", "3.12",
        "--from", "estonia-nordpool-prices[tapo] @ git+https://github.com/vasilyevstan/home_automation.git@main",
        "tp-tapo-mcp"
      ],
      "tools": [
        "list_tapo_plugs",
        "get_tapo_plug",
        "set_tapo_plug_power"
      ],
      "timeout": 300000
    }
  }
}
```

Use a specifically approved safe test MAC/load for any later physical on/off
check, record its original state, and verify restoration. Mocked tests and
successful discovery are not proof of authenticated readings or relay control.
