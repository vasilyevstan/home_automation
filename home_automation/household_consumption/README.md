# Household consumption MCP

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

`elering-kodala-consumption` is a **separate, read-only MCP server** using the
[Elering customer portal](https://estfeed.elering.ee/), not the public price
API. It does not change the four price/forecast tools or the standalone CLI.

Run from a checkout:

```bash
uv run elering-kodala-consumption-mcp
```

Or from GitHub:

```bash
uvx --from "git+https://github.com/vasilyevstan/home_automation.git@main" \
  elering-kodala-consumption-mcp
```

It exposes only these two tools, annotated read-only:

| Tool | Inputs and result |
| --- | --- |
| `list_household_metering_points` | Optional `start_date` and inclusive `end_date`; defaults to the latest 31 Tallinn calendar days including today. Returns authorized electricity EICs and access periods, not profile data. |
| `get_household_consumption` | Required `start_date`, optional inclusive `end_date` (one day by default), and `resolution`: `hourly` (default) or `15_minutes`. Returns interval import/export and daily/monthly summaries. |

Dates must use `YYYY-MM-DD`, must not be in the future, and each call covers at
most **31 calendar days**. Query successive ranges for longer history.
Metering-point discovery is cached in memory for five minutes for the same
date window; measurements are fetched on demand, without a disk cache.
The data tool requires exactly one authorized electricity metering point in
the requested period. It does not guess between multiple points or aggregate
different households.

## Energy quantities and completeness

Values are **kWh**, not kW, prices or bills. `consumption_kwh` means grid
import; `export_kwh` is the provider's `productionKwh` reading at the grid
connection, not necessarily all solar generation. VAT does not apply to
energy quantities.

The result includes offset-aware Estonia timestamps, requested bounds,
retrieval time, the latest returned reading within that window, intervals,
and separate consumption/export summaries. kWh values are summed with Decimal
arithmetic, not averaged or duration-weighted a second time. DST days have
23/24/25 hours or 92/96/100 quarter-hours.

Each direction's summary reports `total_kwh`, `known_intervals`,
`missing_elapsed_intervals`, `complete`, and `elapsed_complete`. Nulls and
missing intervals are **not zero**; a numeric zero remains zero. Totals cover
known readings and are explicitly partial when readings are absent. An
entirely missing direction has a null total. An entirely unavailable window
returns an MCP error, not a zero-consumption result.

Only completed accounting intervals are returned and summed. Today's running
interval and future intervals within today are listed as `pending_intervals`,
not missing elapsed data. A current-day result is therefore not a final
full-day total. `complete` requires the entire requested period;
`elapsed_complete` refers only to intervals that have finished.

Daily and monthly summaries retain their actual start/end bounds.
`covers_full_calendar_period=false` identifies a requested slice of a month,
not the full month's consumption. Coverage is not a guarantee of settlement:
the supported API schema has no finality flag, and readings can arrive late
or be revised. Historical readings are not labeled stale just because the
requested period is old. This is **not real-time or per-appliance telemetry**.

## Credentials

Create customer API credentials through Elering's portal and authorize only the
intended household. Do not put credentials in source, issue comments, tool
arguments, `.env` files committed to Git, or MCP configuration.

On macOS, store the pair in the login Keychain using these interactive commands.
Keep `-w` as the final argument so `security` prompts instead of putting the
credential value in command history or process arguments:

```bash
security add-generic-password \
  -s elering-kodala-consumption -a client_id -w
security add-generic-password \
  -s elering-kodala-consumption -a client_secret -w
```

The server reads these entries without printing them. Missing, empty, locked
or inaccessible Keychain entries produce explicit errors. Do not use
`security`'s unrestricted `-A` access option.

Alternatively, provide **both** `ELERING_CLIENT_ID` and `ELERING_CLIENT_SECRET`
through the process environment using your own secure credential mechanism.
An explicit environment pair takes precedence over Keychain; a partial or
empty pair fails instead of mixing credential sources. Non-macOS hosts need
the environment pair.

GitHub Actions secrets with these same names can store credentials for
separately authorized workflows, but **a local MCP cannot read their values
back from GitHub**. The Keychain/environment credentials are a separate runtime
source. This repository does not add a workflow that uses household secrets.

OAuth uses the official `elering-sso` client-credentials endpoint. Access tokens
are held only in memory and refreshed before expiry. Requests are paced at
least five seconds apart in this process; rate-limit retries are bounded and
respect Retry-After. Other applications/sessions sharing the same key can
still exhaust its limit. Unexpected redirects are rejected, and private
response bodies and tokens are excluded from errors.

## Register the additional MCP

Add this entry to your existing user-level MCP configuration; do not replace
other server entries. The Mac uses Keychain, so this example contains no
credentials:

```json
{
  "mcpServers": {
    "elering-kodala-consumption": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vasilyevstan/home_automation.git@main",
        "elering-kodala-consumption-mcp"
      ],
      "tools": [
        "list_household_metering_points",
        "get_household_consumption"
      ],
      "timeout": 120000
    }
  }
}
```

Replace `@main` with a tested commit SHA for an immutable installation.
Keep household EICs, readings, credentials and private API captures out of
public fixtures, documentation and CI logs. Deterministic tests use synthetic
data; live account checks belong on the authorized local machine.
