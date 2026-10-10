# Starlink MCP

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

`starlink-mcp` is a separate **local stdio server** for a Starlink dish.
It works on macOS and Linux with Python 3.10+ and an optional runtime:

```bash
uv sync --locked --extra starlink
uv run --locked --extra starlink starlink-mcp
```

The dish must be reachable at `192.168.100.1:9200`. When using your own router
with Starlink in bypass mode, a LAN-to-dish route may be necessary; the server
does not modify routing. No cloud login or Starlink account credentials are
required. The local gRPC interface is plaintext and does not authenticate
device identity cryptographically: use a trusted LAN and **never expose port
9200 to the internet**. Startup validates configuration without contacting
the dish.

SpaceX documents an [official local diagnostics subset](https://github.com/SpaceExplorationTechnologies/enterprise-api/tree/main/device-api).
The richer status/history/configuration/control messages used here are
firmware-dependent extensions outside that subset. Protocol reflection
discovers the current schema on every connection. A schema-listed action
is not proof that a particular firmware permits execution. Unsupported,
denied, unavailable, and malformed responses are explicit errors or labeled
partial results, not fabricated data.

## Tools and throughput

| Tool | Inputs and behavior |
| --- | --- |
| `get_starlink_status` | No arguments. Identity, firmware, health/outage state, alerts, uptime, current download/upload throughput, latency, loss, and heating state where supported. |
| `get_starlink_capabilities` | No arguments. Current sleep/snow-melt configuration, read-request schema presence, and separate local enablement and execution evidence for controls. Never tests a write. |
| `get_starlink_history` | `samples`: strict integer 1..900, default 900; `include_samples`: strict boolean, default false. Both throughput directions with average/peak/minimum, latency/loss/power summaries, optional ordered samples, and retained outage records. |
| `get_starlink_obstructions` | No arguments. Device-reported obstruction fraction, collection validity, prolonged-obstruction statistics, and compact map metadata; no image or raw grid. |
| `reboot_starlink` | Required `confirm_disruption=true`. Explicitly reboot once, when locally enabled. Non-idempotent; interrupts internet and may apply a pending firmware update. |
| `set_starlink_sleep_schedule` | Required boolean `enabled`; enabling also requires `start_minutes_utc` (0..1439) and `duration_minutes` (1..1440). Disabling omits both and preserves the stored schedule. |
| `set_starlink_snow_melt_mode` | Required `mode`: `auto`, `always_on`, or `always_off`. Changes only snow-melt mode, not other configuration. |

Status contains **both `download` and `upload`**, each with raw `bps` and
decimal `mbps` (`bps / 1_000_000`), availability, and an unavailable reason.
For example, a synthetic `8_000_000 bps` is `8 Mbps`, not `8 MB/s`.
These are **aggregate traffic rates through the dish**, not a speed test,
maximum subscription speed, spare bandwidth, Wi-Fi speed, or per-client use.
A zero rate can simply mean idle traffic. No active speed test is run.

History exposes `download.bps` / `download.mbps` and the equivalent upload
summaries, including valid/missing sample counts, average, minimum, and peak.
The dish normally retains roughly 15 minutes of nominal one-second samples;
the actual returned coverage is reported. Counters reset on reboot. The
server orders the ring, never pads missing samples, and rejects ambiguous
alignment rather than combining different-length metric buffers. Summaries
exclude unavailable/non-finite readings, not real zero traffic. `complete`
is false when fewer samples were returned or a selected metric has gaps.

`retrieved_at` is collection time, not a device measurement timestamp.
Optional samples use counters and `seconds_before_latest`; there are no
invented precise wall-clock times. Retained outages may extend beyond the
selected window; raw start timestamps are labeled in nanoseconds and duration
in seconds, without assuming an undocumented epoch. Latency is in ms,
packet loss is a ratio from 0 to 1, and history power is reported in W.
Status deliberately does not mislabel a history power sample as instantaneous
power. There is no persistent history, background polling, or energy billing.

## Explicit control opt-in

Configuration belongs in the local process environment, not tool arguments:

| Variable | Meaning |
| --- | --- |
| `STARLINK_TARGET` | Private LAN IPv4 address and port; default `192.168.100.1:9200`. |
| `STARLINK_EXPECTED_DEVICE_ID` | Exact terminal ID obtained from a read; required for controls. When configured, all reads also check it. Keep real identities out of public examples and commits. |
| `STARLINK_ALLOWED_ACTIONS` | Comma-separated subset of `reboot,sleep,snow_melt`; empty by default. Unknown entries or enabling controls without the expected ID are errors. |

The default installation is read-only even though control schemas are listed.
Each write requires local opt-in, the freshly observed expected dish, and an
explicit user request. A matching desired-state setting sends no command.
Mutations are serialized within each server process; gRPC retries are disabled.
There is no generic raw RPC, toggle, bulk action, automatic schedule, stow,
factory reset, GPS setting, router control, or firmware-install tool.

Sleep start is **minutes after UTC midnight**, not local time. It does not
automatically move with daylight-saving changes, and sleep can immediately
interrupt internet. Snow-melt `always_on` may increase power use; configured
mode is not proof of active heating or measured watts.

Results distinguish command sent, acknowledgement, and verified state.
Configuration changes require fresh read-back. Reboot confirmation requires
a subsequently observed restart of the same dish using uptime/boot evidence.
Reboot read-back is bounded; a slow restart can therefore produce an
unconfirmed-outcome error even when the command was accepted. A disconnect
alone is not proof of reboot. Lost acknowledgement can be resolved by a fresh
read, but an unconfirmed outcome is an MCP error: **read status before any
retry; never automatically resend**. A firmware rejection is not bypassed.

## Persistent registration and validation

Install from a verified commit in a persistent local environment, then add
only the new entry to `~/.copilot/mcp-config.json`. Use the installation's
absolute executable path, independent of the current checkout or session:

```json
{
  "mcpServers": {
    "starlink-mcp": {
      "type": "stdio",
      "command": "/absolute/path/to/persistent/environment/bin/starlink-mcp",
      "args": [],
      "env": {"STARLINK_ALLOWED_ACTIONS": ""},
      "tools": [
        "get_starlink_status", "get_starlink_capabilities",
        "get_starlink_history", "get_starlink_obstructions",
        "reboot_starlink", "set_starlink_sleep_schedule",
        "set_starlink_snow_melt_mode"
      ],
      "timeout": 120000
    }
  }
}
```

Preserve other servers and do not broaden tool permissions. User-level
registration makes the MCP available to other sessions; existing sessions
may need a configuration reload. Every stdio client launches its own process,
so no background daemon or listening HTTP port is needed. For a later Linux
move, install the same commit with the extra, update the executable path,
and recheck the LAN route and identity. Do not copy macOS paths.

```bash
uv run --locked --extra starlink python -m unittest discover -s tests -p 'test_starlink*.py' -v
```

Tests use synthetic protobuf schemas and mocked transport/control operations.
They cover exact Mbps conversion, both traffic directions, history ordering
and coverage, protocol defaults versus absent fields, strict inputs, opt-in
and identity checks, narrow writes, no-op setters, single-attempt mutations,
and confirmed/uncertain outcomes. Live acceptance and initial registration
are **read-only**; successful telemetry is not proof that controls work.
Keep private device captures and identities out of source, PRs, and CI logs.
