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
An independent catastrophic-price controller also starts in Shadow and is not
automatically armed by installation, options, restart, or elapsed time.

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
the active limit or measured output. Read-only entries remain readable through
the guarded API, but no write tool or automatic cap is available unless the
selected entry has elevated parameter configuration and its native configuration
coordinator.

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
| Seven-day price forecast | Next available p50 all-in import estimate; attributes summarize freshness, coverage, uncertainty and retained evidence |
| Extreme price protection | Independent catastrophic-price decision, per-load proposed actions, ownership/lease evidence, alerts and promotion state |

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

### Seven-day forecast and application API

The integration also retrieves Wattcast's Estonia forecast and returns seven
complete `Europe/Tallinn` delivery days starting tomorrow. A normal week has
168 hourly predictions; a daylight-saving transition can have 167 or 169. Raw
hourly p10/p50/p90 wholesale estimates are expanded to real quarter-hour
instants and passed through the same effective-dated household tariff engine as
published prices. The resulting primary values are marginal all-in import
EUR/kWh, including applicable VAT and excluding unchanged monthly fees.

Canonical HA Nord Pool quarters always replace predictions once published. If
the provider labels an hour published but HA has no canonical price, that hour
is unavailable rather than accepted from a second source. Negative predictions
remain negative when the tariff calculation permits them. The p10-p90 range is
the provider's nominal 80% uncertainty interval, not a guaranteed bound.
Provider MAE and coverage are evidence supplied by the provider, not an
independent audit.

Use the response-returning action for the complete dashboard/application
payload:

```yaml
action: household_electricity_pricing.get_price_forecast
data:
  config_entry: YOUR_HOUSEHOLD_PRICING_ENTRY_ID
  view: current
```

`view: current` returns all quarter-hour published/p10/p50/p90 values, daily
summaries, freshness, completeness, warnings, attribution and provider accuracy.
The integration stores every distinct provider issuance for each delivery hour
and D-1 through D-7 horizon for 90 days. A migrated archive created before
version 0.11.0 retains only its last surviving issuance and is labeled as
partial evidence rather than an original forecast. Query the backward-compatible
latest-retained view with:

```yaml
action: household_electricity_pricing.get_price_forecast
data:
  config_entry: YOUR_HOUSEHOLD_PRICING_ENTRY_ID
  view: archive
  history_days: 7
  horizon_days: 1
  include_future_days: 1
```

`include_future_days` defaults to `0`, preserving the historical-only archive
view. Set it to `1` to keep tomorrow's stored D-1 forecast accessible after
canonical tomorrow prices publish. A comparison must join exact quarter-hour
timestamps and show a point only when both the retained prediction and the
canonical published household price exist; missing or partial publication is
not zero.

Use the bounded evolution view to inspect every retained revision for one
delivery date:

```yaml
action: household_electricity_pricing.get_price_forecast
data:
  config_entry: YOUR_HOUSEHOLD_PRICING_ENTRY_ID
  view: evolution
  delivery_date: "2026-10-20"
  history_days: 7
  horizon_days: 1
  include_future_days: 1
```

The response returns each quarter's chronological vintages plus the first and
latest retained values, issuance coverage, and explicit capture-quality flags.
It calls the first value original only when vintage tracking covered the full
requested D-N window and no migrated latest-only record is involved. Compare
forecast revisions with canonical household prices by exact quarter timestamp;
do not infer missing actuals or invent revisions that were overwritten before
vintage tracking began.

A future application can call the same action through HA's authenticated REST
service API instead of introducing a separate unauthenticated server. Use a
least-privilege HA credential over an existing trusted local or remote-access
path; do not embed an administrator token in a public client. The summary sensor
is recorder-safe and intentionally omits the full interval array.

Forecasts and their archive are advisory evidence only. They do not authorize
DHW, floor-heating or other device writes, do not replace published prices in
actual bills, and must not be described as measured savings.

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
require expected config-entry ID, serial, Pmax, configured/active limits and
explicit permission for a mode change. `SetSolarGenerationLimit` is not
advertised for a read-only Huawei entry. Daikin requests require known inactive
guard flags, native mode/range/step, and configured/effective read-back where
applicable.

Controls reuse HA's existing device clients. They do not open another inverter
connection or write a permission-test register during setup. Device
authentication/permission errors are still enforced on actual requested writes;
no password is guessed or authentication disabled. Huawei writes fail closed
unless elevated parameter configuration and the native configuration coordinator
are both usable.

The guarded writer holds Huawei's `update_lock`, which serializes against native
polling, but Huawei's own service/entity writes do not participate in that lock.
After authentication and durable prepared-audit persistence, the guarded writer
therefore performs a second exact config-entry/serial/Pmax, configured/active
limit and active-power read immediately before dispatch. Any change is treated
as an external override and no command is sent. A native write can still race in
the final interval between that optimistic read and the Modbus write, so do not
run Huawei-native limit changes concurrently with guarded control.

Intent is journaled privately in HA's
`.storage/home_energy_control.jsonl` before dispatch. Requests are serialized per
device, unchanged verified targets produce read-only no-ops, and uncertain
outcomes are reported instead of controller-level replay, inversion or rollback.
Pre-dispatch lock, authentication and read failures explicitly state that no
command was sent. If exact device read-back succeeds but the final audit record
cannot be persisted, the confirmed device state is retained and the separate
audit-persistence failure is exposed. The controller makes no claim about retry
behavior internal to the Huawei transport.

Do not expose an unrestricted service caller, register access, administrator
tools, boost/installer controls, or automatic optimization through the LLM API.
A write may take effect even if acknowledgement/read-back fails: report
uncertainty and inspect fresh state rather than replaying it. Connectivity tests
are read-only, not same-value writes, toggles, heating changes, or power-limit
changes.

## Solar curtailment

Selecting a Huawei entry creates an independent deterministic
`Solar curtailment` runtime even when no Daikin is configured. It uses the exact
current `export_earnings_eur_kwh` from the pricing coordinator. This is bounded
open-loop generation curtailment based on export economics; it is not
zero-export control and does not infer household surplus, grid direction or
export power.

Policy revision 1 defaults to `Shadow`. `Armed` is hidden until deterministic
policy replay, elevated Huawei write capability, bound config-entry/serial/Pmax,
mode-preserving cap calculation and two stable unrestricted observations are
verified. Passing those gates never selects `Armed` automatically.

An exact negative export price is a Shadow opportunity. Armed entry additionally
requires:

- export earnings at or below **-€0.010/kWh**;
- a known contiguous negative-price block with at least 30 minutes remaining;
- the existing configured and active limit to match at unrestricted 100% or
  Pmax;
- fresh inverter AC active power at or above 5,000 W on two observations at
  least 30 seconds apart, plus a full 60-second qualification dwell;
- a distinct native-step **4,000 W-equivalent** target in the existing limit
  mode. An 8 kW inverter in percentage mode therefore uses 50.0%; fixed-watt
  mode uses 4,000 W. The controller never changes limit mode.

One dispatched acquisition attempt is allowed per contiguous block. The lease
ends at the earlier of two hours or the block end. Clouds or lower PV output after
acquisition do not trigger an early restore because the cap can become
non-binding. The exact original limit is restored on non-negative, stale or
invalid prices, profile change, lease expiry, block end, mode change or graceful
unload.

Ownership is stored before and after dispatch and is bound to the pricing config
entry, inverter serial and Pmax. Startup reconciliation retains an exact target,
clears an exact original, and treats any other limit as an external/manual
override without writing. Controller state uses a private atomic JSON file with
mode 0600, no-follow opens, file and directory `fsync`, and atomic replacement;
unlike HA `Store.async_save`, serialization and disk failures propagate to the
caller before dispatch. Legacy Store state is read only for migration.

The manifest orders setup after Huawei when that integration is present. A
transient not-loaded/setup/retry state defers persisted-ownership reconciliation
without latching uncertainty; after Huawei is loaded, an actual reconciliation
read failure faults normally. A persisted pending phase or any post-dispatch
failure latches an uncertain fault and requires the explicit resume button.
Pre-dispatch failures may requalify. Manual/external limit changes cancel
ownership and suppress re-entry for the rest of that block.

At a quarter boundary, the coordinator still expires the old current price
immediately. During its bounded refresh transition, however, an exact owned cap
is retained only while the persisted block end, pricing profile and lease remain
valid. Refreshed data then either continues ownership or requests exact-original
restoration; rollover alone neither restores nor consumes/suppresses the block.

If device read-back exactly confirms the target or original but final controller
state or verified-audit persistence fails, the sensor reports a separate
`persistence_fault`. It keeps the in-memory physical classification exact
(`owned` target or cleared ownership after original restoration) rather than
calling that confirmed outcome uncertain.

The status sensor exposes exact prices, block bounds, active-power age, Pmax,
output and dwell gates, configured/active/target caps, ownership/original/lease,
operation and audit results, external override and commissioning gates. Its
value fields are limited to cost per avoided exported kWh and elapsed eligible
or owned minutes. Without fresh signed grid power it never reports curtailed
kWh, zero-export performance, or estimated/realized euro savings.

## Domestic-hot-water price optimizer

Selecting a Daikin entry also creates a deterministic HA-native DHW optimizer.
It consumes the coordinator's published all-in `import_eur_kwh` quarters; it
does not call MCP, duplicate tariff rules, use wholesale-only prices, substitute
forecasts, or depend on Copilot or another process. Missing coverage through the
next readiness deadline disables price shifting and holds the normal target.

The default policy is:

| Setting | Value |
| --- | --- |
| Readiness deadlines | 07:00 and 21:00 Europe/Tallinn |
| Soft-standby target | 30°C |
| Comfort threshold / safe coast target | 45°C |
| Normal target | 50°C |
| Cheap-period / final-readiness charge target | 55°C |
| Maximum 30°C lease | 4 hours |
| Price gate | Current all-in price at least €0.05/kWh and 20% above the selected cheap-window average |
| Initial heating allowance | 90 minutes |
| Preheat coast horizon | 8 hours |

For each readiness cycle, the optimizer anchors one cheapest contiguous
all-in-price block instead of selecting a new trailing block every quarter.
It compares that block with the highest-cost feasible block of the same
duration. The 55°C target is proposed only when that preheat opportunity passes
the same €0.05/kWh and 20% gates; flat or low-value price shapes stay at 50°C
outside the final readiness allowance.
This is a price opportunity per kWh, not a measured saving, because whole-Daikin
electrical energy is unavailable.

The bounded readiness pilot is deliberately separate from price shifting.
Outside the final conservative heating allowance it holds 50°C and cannot use
45°C coast or 30°C soft standby. Inside that allowance, it requests 55°C only
while measured tank temperature remains below 50°C, then immediately restores
the 50°C target. The higher target is a temporary request to overcome native
target-50 hysteresis; it does not change the measured readiness requirement.

Comfort or missed-deadline recovery is latched until the measured tank reaches
50°C. Reaching 50°C after an anchored cheap window starts is also persisted as
completion for that readiness plan, including completion during post-window
recovery. A later 49°C reading therefore does not incorrectly re-enter recovery
for a plan that already succeeded; comfort and readiness guards still apply.
Ordinary 45/50°C cost-driven transitions use a 30-minute minimum dwell, while
comfort, readiness, incomplete-data and lease-expiry exits bypass it. Each
policy revision that changes scheduling values or qualification semantics clears
prior evidence so changed Shadow behavior must earn a new observation period.
Revision 5 adds the final-readiness 55→50°C behavior and therefore clears
revision-4 day, cycle, readiness and write evidence. If a policy upgrade finds
a persisted write-capable mode, it pauses and faults for explicit review rather
than continuing under evidence earned by the previous policy.

Deadline evidence distinguishes a measured success, measured readiness later
consumed by a rapid draw, a rapid-draw-contaminated check that cannot prove
delivery, a genuine miss, and unavailable/overridden evidence. Measured success
and measured-then-consumed outcomes count as delivered. Contaminated and
unavailable checks are excluded rather than silently passed or failed; a genuine
miss blocks promotion.

The options flow validates every target against the selected Daikin's native
range and step. On the verified BRP069A62 installation that range is 30–55°C in
1°C steps; 60°C is rejected rather than clamped.

The runtime mode select provides:

- `shadow`: default; calculate and record the same 55/45/50°C decisions the safe
  pilot would make, but send no device commands.
- `readiness_pilot`: permit guarded 55→50°C target changes only inside the
  final readiness allowance; hold 50°C at all other times.
- `safe_target_shift`: after at least seven combined Shadow/readiness-pilot
  observation days, five usable heat cycles, ten delivered usable readiness
  checks with no genuine miss, and two confirmed readiness-pilot writes, permit
  guarded 55°C preheat, 45°C coast, and latched 50°C recovery targets.
- `soft_standby`: after a validated seven-day safe pilot and ten confirmed
  safe-pilot writes, permit the 30°C target under a persisted four-hour lease.
- `paused`: issue no automatic targets.

The evidence gates remain the normal promotion path. A separate
`Activate full hot water control` button records an explicit owner-approved
early activation timestamp and selects `soft_standby` without fabricating
qualification days, cycles or checks. All comfort/readiness exits, target
ownership, exact expected-previous validation, manufacturer guards, leases and
fault latches remain unchanged. This override is intentionally auditable in
the status attributes.

The 30°C target is soft standby, not active cooling and not the comfort floor.
It leaves the DHW circuit enabled but relies on HA to restore 50°C at the 45°C
threshold, lease expiry, readiness lead time, incomplete data, or a failed price
gate. A complete HA outage can leave that low target in place until HA returns,
so the mode is never enabled automatically and its residual risk must remain
visible in the UI.

One shared `EnergyDevices` instance serializes optimizer and explicit MCP writes.
The controller yields to circuit standby, powerful/hygiene-like operation,
weather-dependent control, active/unknown safety flags, configured/effective
target mismatch, external target changes, and Extreme Price Protection priority.
Before every automatic target transition, the writer repeats the relevant
manufacturer-state checks and requires the exact fresh target to match the
controller's expected previous value. It only modifies a target it owns. An
uncertain write latches the optimizer in fault, creates a persistent notification,
and requires a fresh read plus explicit resume; there is no automatic retry,
inversion, or rollback.

The integration exposes an optimizer status sensor with current/proposed
targets, decision reason, selected price window, readiness deadline, price-gate
evidence, lease, learning counters and recent decisions; a mode select; and
explicit resume and return-to-normal buttons. Return-to-normal pauses scheduling
before requesting an ownership-aware 50°C restore. Whole-unit electrical energy
is unavailable, so reported price opportunity is not measured kWh or savings.
Recorder-facing status attributes expose only the four most recent decisions;
24 internal events still feed the compact recovery and override counters.

The dedicated `Hot water price opportunity` sensor is Recorder-safe and keeps
the current anchored plan's highest-cost comparable-block average minus its
selected cheap-block average in EUR per shifted kWh. Its compact attributes
identify the plan, windows, qualification gate, observed target completion and
cumulative qualified-plan count. Shadow values are counterfactual. In any later
write-capable mode, confirmed commands still do not establish euro savings:
`measured_shifted_energy_kwh`, `estimated_savings_eur` and
`realized_savings_eur` remain unavailable until attributable whole-Daikin
electrical energy exists.

## Floor-heating optimizer

Selecting a Daikin entry also creates a separate floor-heating optimizer.
Configure one or more occupied-room `sensor` or `climate` temperature entities
in the integration options. Every configured source must be available and no
more than 30 minutes old; the evaluator uses the coldest room as its
conservative comfort input. The Daikin `IndoorTemperature` value comes from an
unheated garage in this installation and is exposed only as diagnostic context.
There is no fallback from a missing room source to that garage reading.

The separate `Floor heating room temperature` sensor publishes the resulting
coldest fresh occupied-room value and its source details. The optimizer
defaults to `shadow`, remains separate from DHW counters and records the same
counterfactual LWT-offset decisions as before. `active` mode may be selected
after the evidence gate passes, or by the explicit
`Activate floor heating price shift` owner-approval button. Owner approval is
recorded without relabeling the evidence gate as passed.

Every live offset action captures the fresh native offset as its baseline,
binds the command to the exact expected previous value, and requires native
read-back. Re-evaluation keeps the one intended overlay instead of ratcheting
another 4°C from the already-owned value. Leaving the action window, changing
mode, hitting a safety/comfort exit, or unloading restores the exact baseline
only while the current value still matches optimizer ownership. External
changes cancel ownership and return the controller to Shadow. A post-dispatch
error persists the pending operation, latches a fault, and requires fresh-state
reconciliation through `Resume floor heating after review`; it is never
retried or inverted automatically.

Configured climate entities remain valid comfort measurements when their
setpoint changes while the unit is off. If any configured room HVAC is enabled
for heating, cooling, drying or fan operation, the floor optimizer still uses its
measured temperature for the 20°C comfort guard but otherwise yields the
price-shift proposal. The period is marked as externally influenced and is not
eligible as floor thermal-response evidence. Candidate-cycle evidence is
credited only after the paired peak ends without room-HVAC, comfort, override,
missing-input or DHW-priority contamination.

The verified Daikin surface supports a native **-10°C to +10°C** weather-curve
offset. That capability is not a safe optimization envelope by itself. Floor
policy revision 5 evaluates one meaningful **4°C relative overlay**, bounded to
an initial **-6°C to +4°C** envelope:

| Fresh native offset | Cheap-block proposal | Paired peak proposal |
| ---: | ---: | ---: |
| -2°C | +2°C | -6°C |
| 0°C | +4°C | -4°C |

Other fresh native offsets use the same `native ± 4°C` rule and clamp at the
revision-5 bounds. The old separate +1°C aggressive candidate is retained only
as a compatibility field in historical decision data; it is no longer the
policy.

The evaluator requires complete canonical all-in coverage through the end of
the current Tallinn delivery day. It may also use a later local day only when
that whole 92/96/100-quarter day is published. Cheap and peak windows stay on
the same complete local day, and an already anchored pair remains valid until
its peak ends even if later delivery-day prices are not yet published. Missing
quarters are never forecast or zero-filled. Within that evidence it pairs a
two-hour cheap block with a later two-hour expensive block no more than eight
hours away, and requires the same €0.05/kWh and 20% materiality gates as the
DHW policy. It yields to safety/override states, non-LWT control, circuit
standby, missing temperatures and offsets outside the revision-5 envelope. At
or below the 20°C indoor guard it removes a negative offset without lowering an
already warmer native setting. At or above 21°C it records `preheat_limited` and
preserves the native offset instead of adding heat.

DHW comfort, readiness and preheat states have compressor priority. A
conflicting floor preheat candidate is recorded as deferred rather than assumed
to run concurrently. Fresh directional net-grid power and whole-heat-pump
electrical energy are unavailable, so solar-driven floor-heating dispatch
remains disabled and the sensor reports price opportunity rather than measured
kWh or euro savings. The separate bounded inverter-curtailment controller does
not make the floor evaluator solar-aware.
The evidence gate remains ineligible until at least 14 observed days and five
clean revision-5 candidate cycles. The explicit owner-approved path can select
active mode earlier, but it does not claim that thermal-lag evidence passed.
The numerical -6°C to +4°C envelope and 4°C overlay are unchanged from revision
4; revision 5 adds guarded ownership and write/restore behavior.

The dedicated floor opportunity sensor changes its valuation status from
`counterfactual` to `live_action_unattributed` in active mode. Confirmed offset
writes and owned-action time are recorded, but measured shifted energy,
estimated savings and realized savings remain unavailable without attributable
whole-heat-pump electrical energy.

The dedicated `Floor heating price opportunity` sensor records the paired peak
average minus cheap-block average in EUR per shifted kWh. Its compact attributes
identify the anchored pair, candidate status, contamination state and
qualified-plan count, while Recorder supplies the historical series. It does
not turn aggregate household import into heat-pump energy. Estimated or realized
euros remain unavailable until electrical energy can be attributed to the
paired floor-heating action. The main floor status sensor exposes only four
recent decisions to stay below Recorder's attribute-size limit.

## Extreme price protection

Selecting a Daikin entry also creates a separate catastrophic-price controller.
It does not change either optimizer mode and always has priority over their
ordinary writes while faulted, owner-active or inside a hard-stop block.
The controller uses only canonical published household all-in
`import_eur_kwh` quarters; forecasts may remain advisory but never authorize a
shutdown. Missing current publication is unavailable, not zero and not a reason
to substitute a forecast.

The fixed revision-2 policy is:

| Setting | Value |
| --- | --- |
| Hard-stop boundary | €1.50/kWh canonical household all-in |
| Additional critical alert | €2.00/kWh |
| Extended-price warning | At least €0.50/kWh for 120 contiguous minutes |
| Maximum automatic standby lease | 120 minutes |
| DHW comfort exit | Tank at or below 45°C |
| DHW readiness exit | Existing 07:00/21:00 readiness lead time |
| Floor restore guard | Coldest fresh occupied room at or below 20.5°C |
| Notifications | HA persistent notification plus `notify.mobile_app_bananastan` |

Exact contiguous 15-minute blocks are identified by threshold, UTC start and
UTC end. The €2 boundary changes alert severity only; it does not introduce a
second control algorithm. The controller records one independent action for
each covered load:

- Daikin domestic-hot-water circuit: native standby/on.
- Daikin space-heating circuit: native standby/on.
- Explicitly allowlisted climate entities: off and exact prior-state restore.

Configure the climate allowlist in integration options. For upgrades where the
new option has not yet been saved, climate entities already configured as floor
room sources are used as the migration fallback. The allowlist is explicit and
no other climate entity can be controlled.

Climate units already off produce no service call and no infrared command. For
an active unit, the breaker snapshots the cloud-reported HVAC mode, target
temperature and unit, fan mode, vertical swing, horizontal swing and entity
identity before requesting `climate.turn_off`. Restoration occurs only while
the same allowlisted entity remains in breaker-owned cloud-reported off state,
and only after current capabilities still support every saved value. A manual
or external state change cancels ownership rather than being overwritten.
Sensibo/HA read-back confirms cloud state after an IR request; it does not
independently prove physical AC power, flap position, compressor operation or
IR reception.

Daikin and climate writes are serialized, journaled as prepared/verified/
uncertain, and confirmed separately. The combined shutdown is never atomic.
No-op commands are suppressed. An uncertain stop or restore faults the
controller, raises a critical alert and is never retried, inverted or rolled
back automatically. Resume after review first requires fresh Daikin and climate
state. Every Daikin stop, restore and commissioning transition also binds the
exact expected previous `on` or `standby` state so a queued stale decision
cannot overwrite a newer device or user change.

Extreme Price Protection starts before both heating optimizers and has explicit
arbitration priority. DHW and floor optimization suspend while the breaker is
faulted, owns any protected load, lacks the current canonical price in Armed
mode, or is inside a published €1.50/kWh block. This startup order reconciles
persisted catastrophic-price ownership before ordinary heating evaluates.

Per-load ownership stores the prior state, published block identity and lease.
Only a circuit confirmed as breaker-owned standby is re-enabled. Only a climate
unit that was originally active and confirmed breaker-owned off is restored.
Restoration priority is DHW, floor heating, then climate units. Block end,
lease expiry, comfort/readiness exit, stale/missing price, mode change and
restart with an expired lease all request ownership-aware restoration. A load
that exits early is suppressed for the remainder of the same published block,
so a two-hour lease or comfort restore cannot immediately re-trigger.

If any room climate unit was active when protected, the stop/recovery interval
is marked externally influenced in Floor Shadow and cannot count as clean
thermal-response evidence. Standby requests do not prove zero whole-heat-pump
consumption, and the controller reports no avoided kWh or realized euro saving
without attributable electrical energy.

The mode select initially exposes only:

- `Off`: no warnings or new shutdown proposals; future owned restores remain
  ownership-aware.
- `Shadow (no writes)`: default; evaluate exact published blocks, proposed
  per-load actions, warnings, comfort exits and partial-shutdown evidence while
  sending no Daikin or climate command.

`Armed` exists in the persisted policy but is hidden and rejected until all
promotion gates are recorded. The observation gate can be satisfied by either
seven live Shadow days or a deterministic seven-day commissioning replay; a
naturally occurring €1.50/kWh quarter is not required. Alert delivery, both
Daikin circuit standby/on exercises, and one exact climate off/restore exercise
must also succeed through the production guarded writer.

Four one-time commissioning buttons are exposed only in healthy Shadow state:

- Verify the deterministic boundary, warning, comfort, unavailable-input,
  scheduled-block and ownership-restore replay.
- Send a persistent and mobile test alert and require the HA notify service to
  accept it.
- Exercise DHW and floor circuits one at a time, only with fresh safe state,
  DHW at or above 46°C, no active readiness lead-time window, room-temperature
  headroom and exact standby/on read-back. The Shadow optimizer's broader 50°C
  recovery latch still protects production decisions but does not replace these
  stricter commissioning-specific preconditions. Validate native circuit power
  writes against PyAltherma's advertised `["on", "standby"]` operation list.
- Exercise the first allowlisted climate entity by briefly activating its saved
  cooling state only when the saved target has at least 1°C of no-cooling
  headroom. The writer prepares the exact target, fan and swing settings while
  the unit remains off, explicitly activates the saved HVAC mode, then runs the
  exact off/restore path and returns it to its original off state. A bounded
  10-second stabilization dwell separates each commissioning power transition
  so the Sensibo cloud path is not driven by back-to-back on/off requests.

Every commissioning write is journaled. An uncertain outcome faults without
retry or rollback, just like a production breaker command. The private journal
also records the underlying service/read-back error for review. Passing every
gate only exposes `Armed`; it never selects the mode automatically.

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
  python home_automation/home_assistant/tests/test_solar_curtailment.py -v

uv run --no-project --isolated --python 3.14 \
  --with-requirements home_automation/home_assistant/tests/requirements.txt \
  python home_automation/home_assistant/tests/test_dhw_optimizer.py -v

uv run --no-project --isolated --python 3.14 \
  --with-requirements home_automation/home_assistant/tests/requirements.txt \
  python home_automation/home_assistant/tests/test_floor_heating_shadow.py -v

uv run --no-project --isolated --python 3.14 \
  --with-requirements home_automation/home_assistant/tests/requirements.txt \
  python home_automation/home_assistant/tests/test_extreme_price_protection.py -v

uv run --no-project --isolated --python 3.14 \
  --with-requirements home_automation/home_assistant/tests/requirements.txt \
  python home_automation/home_assistant/tests/test_price_forecast.py -v
```

Tests build and load the real bundle, use synthetic rates and mocked market
requests, and exercise HA's actual coordinator, sensor, service, config-flow,
and LLM API types. They do not contact or actuate household devices.

Live installation still requires authenticated HA access, the private profile,
and verification on that host. Software tests are not proof of a completed
deployment or physical device-control testing.
