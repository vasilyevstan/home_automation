# Home Assistant: native MCP and household electricity prices

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

Home Assistant owns the direct device integrations and the price calculations.
Copilot connects to HA's built-in **Model Context Protocol Server**, not a second
device gateway. This is different from HA's MCP Client integration, which calls
external MCPs.

```text
Copilot -> native HA MCP -> approved HA entities and APIs
                            -> local Huawei Solar integration
                            -> local Daikin Altherma integration
                            -> Nord Pool + household price integration
```

The pricing API is read-only and calculated, targeted and tested against
**Home Assistant 2026.10.1**. Optional, explicitly selected device bindings enable
a separate guarded control API. Its device connectors are separate community
integrations and must be checked against the installed HA version and hardware.
It does not install them or change device settings during setup. There is no
LLM-driven optimizer. An explicitly configured deterministic DHW optimizer is
separate from the guarded MCP tools and always starts in zero-write shadow mode.

## Native MCP connection

Enable **Model Context Protocol Server** in HA and use the URL shown there.
The host port is installation-specific; do not assume port 8123.

In the server's options, turn off automatic exposure of all LLM APIs. Select
**Assist** and, after installing the price integration, **Household electricity
prices (read only)**. Expose only the intended entities. Do not enable a broad
administrator API simply to control an inverter or heat pump.

Copilot CLI supports the native Streamable HTTP transport:

```sh
copilot mcp add --transport http \
  --tools assist__homeassistant__GetLiveContext,household-electricity-prices-read-only__GetHouseholdElectricityPrices \
  home-assistant http://homeassistant.local/api/mcp
```

HA prefixes exported MCP tool names; discover the actual names before setting
the allowlist. This is an example read-only allowlist, not automatic authorization. Authenticate
using the supported HA/client OAuth flow, then inspect the actual exposed tools
before enabling additional operations. A configured URL or successful HTTP
response alone is not an authenticated MCP connection.

Keep tokens in private credential storage, not chat, Git, examples, or command
arguments. An administrator setup token, when needed for installation, can be
stored interactively on macOS with `-w` last:

```sh
security add-generic-password -s home-assistant -a access_token -w
```

Do not expose HA or device ports to the internet for local CLI access. Use a
trusted LAN or an existing secure remote-access mechanism.

## Direct device integrations

### Huawei solar

Use the community [Huawei Solar integration](https://github.com/wlcrs/huawei_solar).
It speaks Modbus locally. Reuse privately verified host, port, unit ID, and
identity rather than guessing defaults. SUN2000/SDongle installations commonly
use unit 1, while a direct inverter access-point connection may differ.

Huawei permits only one Modbus connection at a time. Record current configured
and active cap modes/values, disconnect the old local MCP client, and then enable
HA polling. Do not run both against the inverter. On rollback, close HA's
connection before reconnecting the old client. FusionSolar history retrieval is
independent and can remain available.

Advanced parameter configuration exposes percentage and fixed-watt derating
entities on supported devices; some are disabled by default. Scope percentage
operations to the verified 0..100 policy and watt limits to the device's actual
Pmax. Preserve mode-aware checks and read-back; a stored limit is not necessarily
the active limit or measured output.

Grid-feed-in limiting is a different feature and may require a compatible power
meter. PV generation alone is not household demand or grid export. Do not infer
live load from delayed Elering readings or invent zero readings when a meter is
absent. No firmware changes, password guessing, or commissioning is automated.

### Daikin Altherma

For BRP069A61/A62 LAN adapters, evaluate the
[local Altherma integration](https://github.com/tadasdanielius/daikin_altherma),
not the ordinary Daikin AC Wi-Fi integration. The local adapter uses WebSocket
`/mca`; Onecta cloud access is not required for that path.

Validate the community integration on the actual HA version before retiring a
working client. Use a stable, verified device-specific network binding. In LWT
control mode, heating adjustment is a **leaving-water-temperature offset**, not
an absolute room-temperature target.

The [v1.5.3 compatibility patch](compat/daikin-altherma-1.5.3-asyncio.patch)
replaces the removed `async_timeout` dependency with `asyncio.timeout` and uses
`async with` in both configuration paths. Apply it to that exact upstream release;
it changes no device operations or tariff logic.

Keep manufacturer control loops, protection, and hygiene settings intact.
Do not copy a generic on/off thermostat example onto the heat pump. Only expose
requested native operations after checking current mode, availability, supported
range/step, and guard flags. An enabled circuit does not prove that the compressor
is running; setting a target does not prove water has reached that temperature.

## Build the SDK-free price integration

The existing financial engine remains the single implementation. The builder
copies an explicit list of unchanged standard-library core files into the HA
artifact and records their hashes. It excludes MCP SDKs, server modules, and
private configuration. This avoids conflicting with HA's own MCP SDK version.

From a source checkout:

```sh
uv run --locked --inexact python -m home_automation.home_assistant.bundle \
  --output /tmp/household_electricity_pricing.zip
```

The output must not already exist. The ZIP is reproducible for the same source
files and contains `custom_components/household_electricity_pricing/`, its source
manifest, and the source license.

After backing up the relevant HA configuration, install that directory into the
HA configuration directory using the supported file-access mechanism for that
installation. Do not install the entire `estonia-nordpool-prices` distribution
inside HA or override its dependency pins.

## Private configuration

Configure HA's native **Nord Pool** integration for **EE** and **EUR** first.
The pricing integration consumes its published `get_prices_for_date` action,
whose prices are EUR/MWh; it does not mistake them for the sensor's EUR/kWh units.

Deploy a validated private tariff profile, for example:

```text
<HA configuration directory>/private/pricing/profile.json
```

Keep the directory and file private. Use the canonical profile's exact bytes,
version, and hash rather than maintaining separate hand-entered rates in HA
templates. The [synthetic example](../../examples/pricing-profile.example.json)
documents the schema only; its invented rates are not a usable household tariff.

Restart/reload HA as required for custom-integration discovery, then add
**Household electricity pricing** in **Settings > Devices & services**. Select
the existing EE/EUR Nord Pool entry and enter the profile's path relative to the
HA configuration directory. Absolute paths and paths escaping that directory
are rejected. Use the integration's reconfigure flow to change the selected
entry/path. Profile contents are reloaded on each calculation refresh.

No default real tariff is supplied. Missing, invalid, or expired profiles are
errors, not a reason to assume free energy or extrapolate rates.

For automatic household billing, optionally deploy a separate private JSON file:

```json
{
  "client_id": "YOUR_ELERING_CLIENT_ID",
  "client_secret": "YOUR_ELERING_CLIENT_SECRET"
}
```

Select its configuration-relative path, for example
`private/elering/credentials.json`, in the integration's configure/reconfigure
form. Keep its directory private and set the file to mode `0600`. Paths escaping
the configuration directory, group/other-readable files, partial credentials
and duplicate JSON fields are rejected. Credentials are used only by the existing
read-only Elering client; they are never action arguments, results, or bundle
contents. Omitting this configuration preserves all price sensors, quotes and
caller-supplied calculations; automatic metering requests fail explicitly.

## Sensors, price windows, and MCP queries

The calculated integration exposes:

| Sensor | Meaning |
| --- | --- |
| Spot price excluding VAT | Published market energy price in EUR/kWh |
| All-in import price | Variable cost of one additional billable imported kWh, including applicable VAT |
| Net export earnings | Net earnings for one billable exported kWh after export deductions and taxable balancing fees; negative means exporting costs money |

Rates exclude unchanged monthly fixed fees. They preserve unrounded Decimal
calculations; suggested display precision does not round the billing engine.
State attributes retain exact values, current period bounds, profile version/hash,
evidence basis, and incomplete-day reasons. Invoice-derived assumptions stay
qualified; this integration does not silently replace them with a rounded
supplier quotation.

Current values expire at the quarter boundary before refreshing, so a delayed or
failed update does not leave the previous quarter looking current. Complete
published market days are cached; incomplete future data is retried on a bounded
cadence. Missing current prices make the rate sensors unavailable, never zero.

The `household_electricity_pricing.get_prices` action returns today's or tomorrow's
Tallinn price window:

```yaml
action: household_electricity_pricing.get_prices
data:
  config_entry: YOUR_HOUSEHOLD_PRICING_ENTRY_ID
  period: tomorrow
```

The response includes `complete`, `expected_intervals`,
`missing_market_intervals`, `unavailable_reason`, and the known priced intervals.
Never present an incomplete window as a complete next-day schedule. Profile
coverage and market coverage are separate. No forecast is substituted.

The separately selectable **Household electricity prices (read only)**
LLM API exposes `GetHouseholdElectricityPrices`,
`QuoteHouseholdElectricityInterval`, `CalculateHouseholdElectricityPeriod` and
`EstimateHouseholdElectricityScenario` through HA's native MCP. The existing
price-window tool still accepts only `period: today` or `period: tomorrow`.
The API has no device controls, per-call file arguments or arbitrary service
access. Existing clients with explicit tool allowlists must add the three new
calculator names without removing their existing tools.
The API's existing display name is intentionally retained: HA uses it to form
the native MCP tool namespace, so renaming it would break existing allowlists.

## Native bill and estimate actions

**Developer tools > Actions** exposes the same SDK-free financial engine through
three response-returning actions:

| Action | Purpose |
| --- | --- |
| `household_electricity_pricing.quote_electricity_interval` | One supplied 15-minute import/export interval; monthly fees excluded |
| `household_electricity_pricing.calculate_electricity_period` | Actual household bill presets or custom dates/length, with automatic Elering or complete supplied energy |
| `household_electricity_pricing.estimate_electricity_scenario` | Additional hypothetical load or before/after grid-profile variable cost; unchanged monthly fees excluded |

For the current bill:

```yaml
action: household_electricity_pricing.calculate_electricity_period
data:
  config_entry: YOUR_HOUSEHOLD_PRICING_ENTRY_ID
  period: month_to_date
```

`month_to_date` prices the contiguous complete delivery days currently available,
starting on the first of the Tallinn calendar month. It includes **full known
monthly fixed charges** and applies the engine's component-line/section-VAT
rounding to the observed totals. The response is explicitly a `provisional_bill`,
not a finalized full-month invoice; today's unfinished day is excluded, and
`coverage` identifies the requested/actual cutoff and pending completed days.
Holes inside the included days remain errors. Known fixed-fee changes anywhere
in that month must be resolved, not silently prorated. Optional daily breakdowns
still allocate fixed fees by day and must not be summed to reconstruct this bill.

`previous_month` selects the last complete calendar month; `last_12_months`
selects the last 12 **complete** calendar months. Both default to the existing
monthly invoice arithmetic. Every requested date needs sourced tariff history:
if the private profile does not cover the older months, the action fails before
retrieving household data. It does not extrapolate a current margin into history.

Use `period: custom` with inclusive `start_date`/`end_date`, or with a positive
whole-number `days`. A supplied start plus `days` counts forward; `days` alone
selects that many latest completed calendar days. Do not combine presets with
custom dates/length, or `end_date` with `days`. `mode: report` uses unrounded,
calendar-day-allocated fees; `mode: monthly` still requires whole calendar months.
All energy is real 15-minute **kWh**, not redistributed hourly/monthly totals.

Published prices are always the default, fetched through the canonical public
market-data helper in bounded 31-day batches. There is **no automatic forecast
provider or fallback**. For a future period, supply complete energy intervals,
`price_basis: estimate` and complete `price_intervals` objects containing
offset-aware `start`, `end` and net `price_eur_mwh`. Use report mode for such
planning periods; estimates cannot finalize actual monthly bills or use automatic
future metering. The result and nested scenario prices retain explicit estimated
provenance, profile qualifications and unrounded amounts.

The native extension does not change the original pricing MCP's tool signatures
or its published-price-only billing behavior.

Calculations join actual UTC instants and use Tallinn tariff rules, accounting
for adjacent CET market days and 92/96/100-quarter days. Nord Pool's generic
peak/off-peak blocks are not the household network tariff calendar.

HA Energy Dashboard totals remain analytical estimates, not guaranteed supplier
invoices. Physical raw flows, quarter-hour netting, settled metering, and invoice
line/section-VAT rounding still matter. Keep the existing Elering-based billing
and private invoice-replay tools for reconciliation.

For historical storage, HA's official InfluxDB integration runs alongside
Recorder. Use an explicit entity allowlist, a dedicated database/bucket, scoped
writer/reader credentials, persistent storage, and backups. Do not turn high-cardinality
timestamps or profile hashes into tags or automatically expire data without a
selected retention policy. Enabling export does not backfill older readings.

On HA OS, the maintained community **InfluxDB v1** app is a supported local
option and uses API version 1. It is not interchangeable with v2/v3 configuration.
Disable unneeded host-port mappings, retain database authentication, and use
HA's authenticated ingress for administration. Native app backups include its
database storage; verify backup contents and recovery credentials without
restoring over a live database just to test connectivity.

InfluxDB's HA exporter coerces numeric-looking attributes. Use
`profile_version_str`, `profile_sha256_str`, `period_start_str`, and
`period_end_str` for their original text, not the generated numeric counterparts.
`exact_price_text_str` retains the complete decimal rate plus `EUR/kWh`; the
ordinary numeric `value` field is useful for charts, not invoice finalization.

## Controls and ownership

The pricing API never controls equipment. When a verified Huawei and/or Daikin
config entry is explicitly selected in the integration's optional device fields,
HA also offers **Home energy devices (guarded)** as a separate LLM API. It is not
automatically selected by an MCP server whose all-APIs option is off.

That API provides `GetSolarGenerationLimit`, `SetSolarGenerationLimit`,
`GetDaikinStatus`, and/or `ControlDaikin` for the selected devices only. Add only the discovered, prefixed
tool names to the client's allowlist after selecting the API. Solar requests
require expected configured/active limits and explicit permission for a mode
change. Daikin requests require known inactive guard flags, native mode/range/step,
and configured/effective read-back where applicable.

Controls reuse HA's existing device clients. They do not open another inverter
connection or write a permission-test register during setup. Device
authentication/permission errors are still enforced on actual requested writes;
no password is guessed or authentication disabled. Broad Huawei parameter UI
configuration can remain off while these narrowly scoped MCP operations are
enabled separately.

Intent is journaled privately in HA's
`.storage/home_energy_control.jsonl` before dispatch. Requests are serialized per
device, unchanged verified targets produce read-only no-ops, and uncertain
outcomes are reported instead of automatically replayed or rolled back.

Do not expose an unrestricted service caller, register access, administrator
tools, boost/installer controls, or automatic optimization through the LLM API.
A write may take effect even if acknowledgement/read-back fails: report
uncertainty and inspect fresh state rather than replaying it. Connectivity tests
are read-only, not same-value writes, toggles, heating changes, or power-limit
changes.

## Domestic-hot-water price optimizer

Selecting a Daikin entry also creates a deterministic HA-native DHW optimizer.
It consumes the coordinator's published all-in `import_eur_kwh` quarters; it
does not call MCP, duplicate tariff rules, use wholesale-only prices, substitute
forecasts, or depend on Copilot or another process. Missing coverage through the
next readiness deadline fails safe to the normal target.

The default policy is:

| Setting | Value |
| --- | --- |
| Readiness deadlines | 07:00 and 21:00 Europe/Tallinn |
| Soft-standby target | 30°C |
| Comfort threshold / safe coast target | 45°C |
| Normal target | 50°C |
| Cheap-period preheat target | 55°C |
| Maximum 30°C lease | 4 hours |
| Price gate | Current all-in price at least €0.05/kWh and 20% above the selected cheap-window average |
| Initial heating allowance | 90 minutes |
| Preheat coast horizon | 8 hours |

For each readiness cycle, the optimizer anchors one cheapest contiguous
all-in-price block instead of selecting a new trailing block every quarter.
It compares that block with the highest-cost feasible block of the same
duration. The 55°C target is proposed only when that preheat opportunity passes
the same €0.05/kWh and 20% gates; flat or low-value price shapes stay at 50°C.
This is a price opportunity per kWh, not a measured saving, because whole-Daikin
electrical energy is unavailable.

Comfort or missed-deadline recovery is latched until the measured tank reaches
50°C. Ordinary 45/50°C cost-driven transitions use a 30-minute minimum dwell,
while comfort, readiness, incomplete-data and lease-expiry exits bypass it.
Each policy revision clears prior qualification evidence so changed Shadow
behavior must earn a new observation period.

The options flow validates every target against the selected Daikin's native
range and step. On the verified BRP069A62 installation that range is 30–55°C in
1°C steps; 60°C is rejected rather than clamped.

The runtime mode select provides:

- `shadow`: default; calculate and record the same 55/45/50°C decisions the safe
  pilot would make, but send no device commands.
- `safe_target_shift`: after at least seven observed days, five usable heat
  cycles and ten successful readiness checks with no failed check, permit
  guarded 55°C preheat, 45°C coast, and latched 50°C recovery targets.
- `soft_standby`: after a validated seven-day safe pilot and ten confirmed
  writes, permit the 30°C target under a persisted four-hour lease.
- `paused`: issue no automatic targets.

The 30°C target is soft standby, not active cooling and not the comfort floor.
It leaves the DHW circuit enabled but relies on HA to restore 50°C at the 45°C
threshold, lease expiry, readiness lead time, incomplete data, or a failed price
gate. A complete HA outage can leave that low target in place until HA returns,
so the mode is never enabled automatically and its residual risk must remain
visible in the UI.

One shared `EnergyDevices` instance serializes optimizer and explicit MCP writes.
The controller yields to circuit standby, powerful/hygiene-like operation,
weather-dependent control, active/unknown safety flags, configured/effective
target mismatch, and external target changes. It only modifies a target it owns.
An uncertain write latches the optimizer in fault, creates a persistent
notification, and requires a fresh read plus explicit resume; there is no
automatic retry, inversion, or rollback.

The integration exposes an optimizer status sensor with current/proposed
targets, decision reason, selected price window, readiness deadline, price-gate
evidence, lease, learning counters and recent decisions; a mode select; and
explicit resume and return-to-normal buttons. Return-to-normal pauses scheduling
before requesting an ownership-aware 50°C restore. Whole-unit electrical energy
is unavailable, so reported price opportunity is not measured kWh or savings.

## Validation

The source adapter and bundle tests run in the existing project environment:

```sh
uv run --locked --inexact python -m unittest discover \
  -s tests -p 'test_home_assistant_pricing*.py' -v
```

Run the actual HA API tests separately so its dependencies cannot replace those
used by existing MCPs:

```sh
uv run --no-project --isolated --python 3.14 \
  --with-requirements home_automation/home_assistant/tests/requirements.txt \
  python home_automation/home_assistant/tests/test_component.py -v

uv run --no-project --isolated --python 3.14 \
  --with-requirements home_automation/home_assistant/tests/requirements.txt \
  python home_automation/home_assistant/tests/test_controls.py -v

uv run --no-project --isolated --python 3.14 \
  --with-requirements home_automation/home_assistant/tests/requirements.txt \
  python home_automation/home_assistant/tests/test_dhw_optimizer.py -v
```

Tests build and load the real bundle, use synthetic rates and mocked market
requests, and exercise HA's actual coordinator, sensor, service, config-flow,
and LLM API types. They do not contact or actuate household devices.

Live installation still requires authenticated HA access, the private profile,
and verification on that host. Software tests are not proof of a completed
deployment or physical device-control testing.
