# Home automation

Nine independent local stdio MCP servers for electricity data, household
costs, and explicitly selected device controls, plus integration with Home
Assistant's native HTTP MCP server. The local servers share one installable
Python distribution, not a hosted service or a background automation daemon.

Repository: <https://github.com/vasilyevstan/home_automation>

## MCP folders

Each folder contains its implementation, a `README.md` with setup/tool
documentation, and an `AGENTS.md` with maintenance and safety guidance.

| MCP registration | Folder and README | Purpose | Optional runtime extra |
| --- | --- | --- | --- |
| `electro` | [electricity_market](home_automation/electricity_market/README.md) | Published Estonia prices, standalone CLI, and separate experimental forecast | None |
| `elering-kodala-consumption` | [household_consumption](home_automation/household_consumption/README.md) | Authorized household grid import/export | None |
| `electrisity-price` | [electricity_pricing](home_automation/electricity_pricing/README.md) | Effective-dated costs, bills, and incremental scenarios | None |
| `sensibo-mcp` | [sensibo](home_automation/sensibo/README.md) | Enrolled Sensibo Sky climate devices | None |
| `tp-tapo-mcp` | [tapo](home_automation/tapo/README.md) | Local MAC-addressed sockets and measurements | `tapo` |
| `starlink-mcp` | [starlink](home_automation/starlink/README.md) | Local dish telemetry and opt-in controls | `starlink` |
| `sonos-mcp` | [sonos](home_automation/sonos/README.md) | Local speakers, playback, groups, and queues | `sonos` |
| `govee-mcp` | [govee](home_automation/govee/README.md) | LAN-enabled lights | None |
| `terrace-heater-mcp` | [terrace_heater](home_automation/terrace_heater/README.md) | Privately provisioned direct-LAN heater | `heater` |
| `home-assistant` (native remote) | [home_assistant](home_automation/home_assistant/README.md) | Direct HA device integrations and SDK-free household price sensors | Runs in HA; no extra MCP gateway |

The heater's one-time provisioning utility additionally uses `heater-setup`.
Tests and synthetic examples remain in the shared `tests/` and `examples/`
directories. The pricing server reuses the market and household clients
directly rather than launching other MCP processes.

The HA pricing integration bundles the same pure financial engine without its
MCP dependencies. It calculates inside HA; it does not call the pricing MCP or
require the checkout/CLI to stay running. See its guide for installation,
private profiles, scoped MCP access, and direct-device handover.

The distribution name **`estonia-nordpool-prices`**, all existing console
commands, MCP registration names, and tool names are retained for compatibility.
Root-level Python files are small compatibility entry points: imports such as
`electricity_pricing` and `nordpool_ee` still refer to the real implementation
modules, including their state and exception classes. Put implementation changes
in the relevant `home_automation/` folder, not those compatibility files.

## Requirements

- Python 3.10 or newer
- Python 3.11 or newer and the optional `tapo` extra for `tp-tapo-mcp` only
- The optional `starlink` extra and a trusted LAN route to the dish for
  `starlink-mcp`
- The optional `sonos` extra and trusted LAN access to Sonos speakers for
  `sonos-mcp`; no Sonos cloud credentials are required
- Trusted LAN access to supported Govee devices with **LAN Control** enabled
  in Govee Home for `govee-mcp`; no Govee cloud credentials are required
- The optional `heater` extra and verified private provisioning for the heater
- Internet access to `dashboard.elering.ee` for published electricity prices
- Internet access to `wattcast.eu` for the optional forecast tool (no API key)
- Elering customer API credentials and access to `estfeed.elering.ee` and
  `kc.elering.ee` for household consumption and automatic household pricing;
  pricing supplied readings or individual quantities needs no household credentials
- Sensibo cloud access and explicit enrollment for `sensibo-mcp`
- [`uv`](https://docs.astral.sh/uv/)

## Setup

Clone the repository and install its locked dependencies:

```bash
git clone https://github.com/vasilyevstan/home_automation.git
cd home_automation
uv sync --locked --inexact
```

Run checkout commands from this repository root. Choose the optional extra
listed for the MCP you need; for example:

```bash
uv run --locked --extra tapo --inexact tp-tapo-mcp
```

The standalone electricity-price CLI is still `uv run nordpool-ee`.
Each MCP README includes its own launch and registration instructions.
`--inexact` preserves other extras already installed in a shared development
environment. Persistent installations should use a published Git commit or
their own non-editable environment outside the checkout.

Keep credentials, real tariffs, household readings, device inventories, and
invoice or device captures outside the public repository. GitHub publishes
the generic code; repository secrets do not make a local MCP a hosted service.

## Repository and local-folder rename

The GitHub repository is `vasilyevstan/home_automation`. An existing checkout
may still be named `electro`; its directory name is not part of the MCP API.
The remote can be updated without moving the checkout:

```bash
git remote set-url origin https://github.com/vasilyevstan/home_automation.git
```

For registrations that must survive a later checkout rename, use a
Git-commit-pinned `uvx --from` launch or a non-editable installation in a
separate persistent environment. Do not point those registrations at
`uv run --directory /path/to/the/checkout`. Preserve their tool allowlists and
private environment settings when updating the executable source.

After moving a development checkout, run `uv sync --locked --inexact` from its
new root to refresh the editable installation and generated entry points.
Keep any needed `--extra` selections. Independently installed MCPs do not need
the checkout to be present.

Existing `~/.config/electro/`, `~/.local/share/electro/`, environment variable
names, and Keychain services are intentionally unchanged. Do not move private
configuration or re-provision devices merely because the repository was renamed.

## Validation

Run the deterministic test suite without live network access:

```bash
uv run --locked --inexact python -m unittest discover -s tests -v
```

Check syntax:

```bash
uv run --locked --inexact python -m compileall -q home_automation *.py
```

Run the CLI for a live next-day source check:

```bash
uv run nordpool-ee
```

The deterministic suite also covers forecast schema/transport/cache errors,
quantile and VAT arithmetic, partial/stale data, missing accuracy, DST windows,
and MCP text/structured output agreement. Live integration checks must compare
the same forecast issuance, distinguish published from predicted hours, and
report actual coverage and freshness. Faithful reproduction of provider
metrics does not independently validate future predictive accuracy.

Household tests cover credential loading and redaction, OAuth expiry/retries,
rate limits, private upstream errors, date/DST boundaries, null-versus-zero
readings, independent import/export completeness and MCP output. Live checks
should compare the same authorized meter/date window and resolution to the
source, without publishing private response data or claiming independent
verification of the physical meter.

Pricing tests cover synthetic profile validation, interval weighting, raw/net
flows, holiday/time-band and DST boundaries, negative prices, source status,
rounding stages/ties, fixed-fee identity, daily allocations, annual/monthly
composition, bounded market batches and both MCP input modes. Real invoice
replays belong outside the public repository and CI logs.

Run the optional Tapo tests, and then the complete suite with the extra:

```bash
uv run --locked --extra tapo python -m unittest discover -s tests -p 'test_tapo*.py' -v
uv run --locked --extra tapo python -m unittest discover -s tests -v
```

Tapo tests use synthetic identities and mocked network/relay operations. They
cover IP reassignment, unsupported discovery, retained inventory, credential
redaction, null versus zero, units, strict boolean commands, serialized
mutations, and confirmed/uncertain read-back. Without the extra, the
library-dependent tests are explicitly skipped; base startup/credential and
MCP schema tests still run. Automated tests never require real Tapo or GitHub
secrets and must not switch household loads.

For the complete optional-runtime suite:

```bash
uv run --locked --all-extras --inexact python -m unittest discover -s tests -v
```

`test_package_layout.py` checks packaged console commands, legacy module
identity, per-MCP documentation, and lazy imports. Each MCP's maintenance file
lists its focused tests. Device tests use synthetic identities and mocked
controls; a repository refactor or installation check does not authorize
physical switching, heating, playback, regrouping, or dish reconfiguration.

## License

The source code is available under the [MIT License](LICENSE). The license does
not cover Nord Pool, Elering or Wattcast data, trademarks, or third-party content.
