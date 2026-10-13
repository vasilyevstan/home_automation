# Household electricity pricing MCP

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

`electrisity-price` is a separate **read-only stdio server**. It uses a private
JSON tariff profile, published market prices and either supplied readings or
the existing Elering customer client. It does not alter the other MCPs,
control appliances, use forecasts, learn rates automatically or run a hosted
service.

## Private configuration

Start with [the synthetic example](../../examples/pricing-profile.example.json),
outside the repository:

```bash
mkdir -p ~/.config/electro/pricing
chmod 700 ~/.config/electro/pricing
cp -n examples/pricing-profile.example.json ~/.config/electro/pricing/profile.json
chmod 600 ~/.config/electro/pricing/profile.json
```

**Replace the invented example rates, tax fractions and dates before using
the profile for real costs.** There is no built-in household tariff or
silent fallback. Keep contracts, invoices, profile history and real replay
fixtures private; only synthetic examples belong in this repository.

```bash
ELECTRICITY_PRICING_PROFILE="$HOME/.config/electro/pricing/profile.json" \
  uv run electrisity-price-mcp
```

The profile specifies EUR, Europe/Tallinn, a version, finite coverage dates,
an explicit rounding policy, source evidence and effective-dated rules.
`from` is inclusive; `until` is exclusive and may be null on an individual
indefinite rule. The overall profile requires a finite coverage end.
Unknown fields, missing sources, rule gaps/overlaps and unsupported structures
are errors. Rate fields are **VAT-exclusive decimal strings**; `vat_rate` is
a fraction, not a percentage. Gross rates are not accepted as a separate
input basis or silently converted.

| Component kind | Meaning of its configured `rate` |
| --- | --- |
| `spot` | EUR/kWh adjustment added to the matching published EUR/MWh price divided by 1000. Import is a cost; export proceeds are negated into customer-cost convention. Use a negative adjustment for a buyback deduction. |
| `unit` | EUR/kWh charge on the selected billable import/export direction, such as excise or balancing fees. |
| `banded` | Net EUR/kWh `band_rates`: `day` and `night`, optionally with both `weekday_peak` and `rest_peak`. |
| `monthly` | Net EUR per calendar month for one stable component/obligation ID. No energy direction. |

The supported network calendar uses Tallinn working weekdays 07:00-22:00 for
day pricing; weekends, statutory holidays and other hours are night.
Configured winter peaks apply November-March: working days 09:00-12:00 and
16:00-20:00; rest days 16:00-20:00. The holiday calendar follows the current
Estonian holiday act, including Good Friday, Easter Sunday and Pentecost;
Easter Monday is not a statutory holiday.

Effective-dated netting rules choose either `gross` or `quarter_hour_net`.
The latter bills `max(import - export, 0)` and `max(export - import, 0)`
**within each quarter**, never across a whole day or month.

`tax_treatment` distinguishes `taxable` from `outside_vat`. Outside-VAT
amounts require a zero VAT fraction but are not labeled zero-rated taxable
sales. Export balancing can therefore be taxable even when the energy credit
is outside VAT. Negative spot prices and negative export proceeds are retained.

## Tools

The [native Home Assistant integration](../home_assistant/README.md#native-bill-and-estimate-actions)
also reuses this engine without the MCP SDK. Its additional month-to-date path
is explicitly provisional and includes full known monthly fees; the original
MCP's `report`/`monthly` behavior and published-price-only contract remain unchanged.

| Tool | Inputs and behavior |
| --- | --- |
| `quote_electricity_interval` | Offset-aware `start`/`end` for exactly one aligned 15-minute interval, `consumption_kwh` and/or `export_kwh`, and `quantity_basis` (`raw` by default, or `billable`). Returns variable costs before VAT, VAT and after VAT; monthly fees are excluded. |
| `calculate_electricity_period` | `start_date`, optional inclusive `end_date`, optional `intervals`, `quantity_basis`, `mode` (`report` or `monthly`) and optional `include_daily`. Supports complete days, weeks, months and multi-month/year ranges. |
| `estimate_electricity_scenario` | `mode="load"` for constant kW or uniformly distributed hypothetical kWh over a run, or `mode="comparison"` for supplied before/after quarter-hour grid profiles. Returns the incremental variable cost, including network, statutory fees and VAT, but not unchanged monthly fees. |

Quantities are interval **kWh**, not power. They are multiplied by their
matching market price without another duration factor. Decimal strings are
preferred; numeric readings from the consumption MCP are also accepted at
their supplied precision. No measured hourly/monthly quantity is distributed
into invented quarters. Only the scenario tool's explicit hypothetical load
mode allocates energy by a declared constant-power/uniform-energy assumption.
Timestamps require UTC offsets and may not lose sub-microsecond precision.

An interval quote can select just one explicitly billable direction. Raw
quotes require both directions whenever netting applies. Period calculations
require both directions for every interval. Missing/null is never zero.
Already-netted billable inputs are not netted again, and cannot contain two
positive directions in one netting interval.

For example, a supplied-quantity quote uses:

```json
{
  "start": "2026-10-01T22:00:00+03:00",
  "end": "2026-10-01T22:15:00+03:00",
  "consumption_kwh": "2",
  "export_kwh": "0",
  "quantity_basis": "raw"
}
```

To calculate a month from the authorized household automatically:

```json
{
  "start_date": "2026-09-01",
  "end_date": "2026-09-30",
  "mode": "monthly",
  "include_daily": true
}
```

Omitting `intervals` selects automatic retrieval. It reuses the existing
Keychain/environment credentials and single-household checks, requests
quarter-hour readings and bounds upstream requests rather than issuing one
request per quoted interval. The separate consumption MCP retains its own
31-day limit; the pricing server composes bounded requests for longer ranges.
Automatic pricing requires completed past calendar days. Today's unfinished
day, missing readings, ambiguous households, unknown tariffs and unpublished
market prices produce explicit tool errors with no partial total.

Alternatively, supply `intervals` containing the consumption MCP's
`start`, `end`, `consumption_kwh` and `export_kwh` fields. An empty list is
not an automatic-retrieval request. Caller-provided quantities are identified
as such, including hypothetical future quantities when the market prices
have actually been published.

## Appliance estimates and before/after scenarios

For a **hypothetical all-grid load**, call `estimate_electricity_scenario`
with an offset-aware start, positive duration in minutes, and exactly one of
`power_kw` or `energy_kwh`:

```json
{
  "mode": "load",
  "start": "2026-08-03T10:07:30+03:00",
  "duration_minutes": "60",
  "power_kw": "2"
}
```

The example represents a constant 2 kW load for one hour, or 2 kWh.
Alternatively, replace `power_kw` with `"energy_kwh": "2"` to explicitly
spread that hypothetical energy evenly across the run. These are scenario
assumptions, **not measurements or inferred appliance consumption**.
All energy in this mode is additional **billable grid import**; it does not
assume the rest of the household consumes zero. Omit `quantity_basis` and
both interval profiles in load mode. Zero power/energy is valid; zero or
negative duration is not.

Partial first/last market quarters are allocated by real elapsed overlap.
UTC arithmetic preserves duration through daylight-saving changes, while
network bands and displayed times use Tallinn time. Derived energy uses at
most 24 significant digits and 18 decimal places; cumulative allocation
preserves the represented total without accumulating rounding drift.
Durations must resolve to whole microseconds and stay within profile coverage.

For **solar/battery-aware pricing**, supply complete before-and-after grid
profiles instead. The following synthetic example reduces export and
increases import:

```json
{
  "mode": "comparison",
  "quantity_basis": "raw",
  "baseline_intervals": [
    {
      "start": "2026-08-03T10:00:00+03:00",
      "end": "2026-08-03T10:15:00+03:00",
      "consumption_kwh": "0",
      "export_kwh": "2"
    }
  ],
  "scenario_intervals": [
    {
      "start": "2026-08-03T10:00:00+03:00",
      "end": "2026-08-03T10:15:00+03:00",
      "consumption_kwh": "1",
      "export_kwh": "0"
    }
  ]
}
```

Comparison mode takes no load-mode start, duration, power or energy argument.
Both profiles need known import **and** export in every aligned 15-minute
interval over the same window; their bounds define the comparison period.
`quantity_basis` defaults to `raw` and applies to both sides. Historical
gross-flow or quarter-hour netting rules are applied separately to each
profile; explicitly billable inputs are not netted again.

The tool prices **scenario minus baseline**, including lost export credits,
changes in import/export balancing, transmission, statutory fees, excise
and each component's VAT treatment. A negative result is a saving, not an
error. It does not infer solar output, battery state, dispatch or efficiency.
Recharge energy, battery losses and later export changes count only when
represented in the supplied comparison window. Data outside that window is
not silently assumed free or included. Use the existing consumption MCP to
obtain actual baseline readings when appropriate; this tool never retrieves
or substitutes household readings automatically.

In both modes, top-level `amounts`, `lines` and `vat_groups` describe the
**unrounded incremental cost**, not the whole household's bill. `baseline`
(null for a load estimate) and `scenario` show the separately priced variable
costs and quantities. `start`/`end` describe the requested run or comparison;
`accounting_start`/`accounting_end` describe the containing full quarters.
`energy_allocation` records the active overlap and kWh in each load quarter;
`energy_kwh` is null in comparison mode because grid differences do not
establish appliance energy use.

Unchanged monthly fees are excluded. Prices and the profile are shared
across both sides from one request snapshot, with source, version/hash and
invoice-derived qualifications retained. Unpublished prices, missing rates,
incomplete/mismatched profiles or mixed input modes fail explicitly.
There are no forecasts, appliance controls or silent missing-as-zero defaults.
An estimate is not an exact difference between two cent-rounded monthly
invoices; use the existing monthly calculation for complete energy bills.

## Reporting versus invoice arithmetic

`report` returns **unrounded** variable amounts plus calendar-day allocation
of fixed fees within each covered month. This is suitable for days, weeks
and partial months; it is not a standalone supplier bill.

`monthly` requires complete calendar months. It sums unrounded interval
contributions into monthly component lines, rounds those lines, and calculates
and rounds VAT separately per month, contract section and VAT rate. An
annual total sums these finalized monthly results; it does not reround one
annual taxable base.

Each monthly fee is charged once per stable component ID, even if unchanged
financial rules have different source versions. A fixed-fee amount or tax
change inside a billing month is rejected in monthly mode because actual
partial-contract proration is not configured; an explicitly allocated report
remains available. Calendar-day report allocation must not be mistaken for
a supplier's contractual partial-period billing rule.

`include_daily=true` adds analytical daily allocations even in monthly mode.
**Summing rounded displayed days or interval quotes is not an exact monthly
invoice reconstruction.** For composition, supply the underlying interval
readings instead.

Results include exact decimal-string amounts, separate taxable net and
outside-VAT amounts, component lines, VAT groups, raw/billable quantities,
monthly summaries and optional daily allocations. Finalized monthly amounts
have two decimals. Every result includes profile version/hash, source IDs and
evidence basis, input provenance and warnings. Rates based on invoices stay
labeled `invoice_derived`; listing reference invoice months never implies
that a different input or an unbilled period has been invoice-verified.
Complete metering coverage also does not establish final settlement.

`ROUND_HALF_UP` and `ROUND_HALF_EVEN` are explicit profile choices. Preserve
the supplier's demonstrated rounding stages and document any unproven tie
policy instead of silently using the Decimal default.

Totals represent energy costs and credits, not an account ledger. Interest,
previous balances, payments and amount due are excluded. Reconcile such items
separately when comparing a complete invoice.

## Updating rates and retaining evidence

Document any accepted contract-versus-invoice discrepancy alongside the
private profile, including the written rule, working rule, checked periods
and unresolved qualifications. Preserve repeatable private comparisons of
line amounts, quantities and section VAT. If a future invoice differs, inspect
those differences before changing the profile; do not fit rates automatically.

Append effective-dated changes, retain prior amounts and sources, increment
the profile version and rerun historical fixtures plus the new case. The MCP
loads one immutable profile snapshot per call, so edits take effect on the
next call without a restart or mixed old/new rates. Its hash is provenance,
not a billing-line or monthly-fee occurrence ID.

## Register the pricing MCP

Add an entry without replacing existing servers:

```json
{
  "mcpServers": {
    "electrisity-price": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vasilyevstan/home_automation.git@<tested-commit-sha>",
        "electrisity-price-mcp"
      ],
      "env": {
        "ELECTRICITY_PRICING_PROFILE": "~/.config/electro/pricing/profile.json"
      },
      "tools": [
        "quote_electricity_interval",
        "calculate_electricity_period",
        "estimate_electricity_scenario"
      ],
      "timeout": 600000
    }
  }
}
```

Only the profile path belongs in MCP configuration, never its rates, household
identifiers or credentials. This is still a local process: GitHub publishes the
generic executable code but does not host the private profile or household API.
