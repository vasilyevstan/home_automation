# Sensibo climate MCP

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

`sensibo-mcp` is a separate stdio server for Sensibo Sky (`skyv2`) controllers.
It uses the official **cloud API**, not a local device API. Internet access to
`home.sensibo.com` and a Sensibo API key are required. Devices are addressed by
stable device IDs, with verified MACs retained privately; DHCP/IP changes do
not affect control. Other device models are rejected during enrollment rather
than assigned guessed capabilities.

## Credentials and explicit enrollment

Generate a key at <https://home.sensibo.com/me/api> while signed into the account
containing the intended devices. Do not paste it into chat, source files, MCP
configuration, or command arguments. If a key has been exposed, rotate it.

On macOS, store it interactively, with `-w` last:

```bash
security add-generic-password -s sensibo-mcp -a api_key -w
```

Alternatively, supply `SENSIBO_API_KEY` securely through the server's process
environment. An explicit environment value takes precedence; empty or
whitespace-containing values fail rather than falling back to Keychain.
GitHub secret `SENSIBO_API_KEY` can retain a separate encrypted copy, but a local
MCP **cannot read secret values back from GitHub**.

From a checkout, enroll the account only after confirming the expected count:

```bash
uv run --locked python -m sensibo --enroll-account --expected-devices 4
```

This is explicit account enrollment, not an HVAC command. It validates the
count, model, and unique device-ID/MAC associations before atomically writing
`~/.config/electro/sensibo/devices.json` (directory `700`, file `600`).
Previously enrolled devices are retained when absent from the account.
Conflicting identities fail rather than being reassigned. New account devices
are never silently authorized for control.

To retain the verified inventory in a GitHub repository secret:

```bash
gh secret set SENSIBO_DEVICES --repo vasilyevstan/home_automation \
  < ~/.config/electro/sensibo/devices.json
```

The local inventory remains necessary; GitHub is not a runtime secret-fetch
service. Neither credentials nor real device inventory belong in commits,
examples, fixtures, or public logs.

## Tools, readings, and control semantics

| Tool | Behavior |
| --- | --- |
| `list_sensibo_devices()` | Enrolled IDs/MACs, connectivity, and mode-specific capabilities; missing devices remain visible and new account devices are counted but not enrolled |
| `get_sensibo_device(device_id)` | Cloud-reported AC state, measured temperature/humidity, derived feels-like temperature, RSSI, timestamps, and freshness |
| `get_sensibo_measurements(device_id, days=1)` | Provider-available historical samples for 1-7 days, UTC timestamps, actual coverage, and nulls/gaps without interpolation |
| `set_sensibo_ac_state(device_id, ...)` | One explicit desired power/mode/temperature/fan/swing update after fresh identity, connectivity, capability, and policy checks |

Current account reads share a **60-second on-demand cache**. Sensor timestamps
remain the original measurement times; cache age and sensor age are different.
Samples older than ten minutes are marked stale. Unknown timestamps, offline
status, missing readings, and null values are explicit, never fabricated zeros.
Sensor temperatures and feels-like values are **Celsius**, independently of the
AC setpoint's C/F unit. Feels-like temperature is provider-derived; RSSI is
Wi-Fi signal strength in dBm. This version does not infer power consumption,
air quality, or occupancy from Sky readings.

History has a separate short cache and retains its original requested window
and retrieval time. The seven-day request bound is an application limit, not a
promise of provider retention, complete samples, or uninterrupted recording.
There is no background collector, database, subscription, or schedule.

Control accepts `on`, `mode`, `target_temperature`, `temperature_unit`,
`fan_level`, `swing`, and `horizontal_swing`. Provide temperature and its unit
together; allowed values come from that device's selected mode, not a global
list. For example, a dry mode may have no fan-level control. Unspecified
settings are not sent, and incompatible retained settings require an explicit
choice instead of a silent reset. Account/device restrictions remain enforced.

Commands are serialized in this MCP process, but other apps/controllers can
still act independently. Every explicit command can emit infrared, including
a repeated desired state. There are no toggle or bulk-control tools and no
automatic write retries. A timeout, rejected command, or unsuccessful read-back
returns a tool error explaining that the outcome may be uncertain; read the
state before deciding whether another command is needed.

A successful result distinguishes `api_acknowledged` and
`cloud_state_matches` from `physical_effect_verified=false`. Neither cloud
state nor an accepted IR command proves that the HVAC unit received it or that
its compressor is running. Never test physical switching without selecting an
authorized device and explicit safe command.

## Launching and registering Sensibo

From a checkout:

```bash
uv run --locked sensibo-mcp
```

From the published repository:

```bash
uvx --from "git+https://github.com/vasilyevstan/home_automation.git@main" sensibo-mcp
```

Add a separate entry to the existing user-level `~/.copilot/mcp-config.json`,
preserving its other servers. Pin `@main` to a published commit SHA when a
reproducible launch is required. No credentials are embedded in this entry:

```json
{
  "mcpServers": {
    "sensibo-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vasilyevstan/home_automation.git@main",
        "sensibo-mcp"
      ],
      "tools": [
        "list_sensibo_devices",
        "get_sensibo_device",
        "get_sensibo_measurements",
        "set_sensibo_ac_state"
      ],
      "timeout": 120000
    }
  }
}
```

Run deterministic, synthetic tests without a real account or physical writes:

```bash
uv run --locked python -m unittest discover -s tests -p 'test_sensibo*.py' -v
```

References: [official Sensibo API](https://support.sensibo.com/sensibo.openapi.yaml)
and [Home Assistant's Sensibo integration](https://www.home-assistant.io/integrations/sensibo/).
