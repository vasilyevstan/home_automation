# Terrace heater MCP

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

`terrace-heater-mcp` is a separate **direct-LAN stdio server** for one
privately provisioned Tuya heater. It reads reported temperature/state and
provides explicit power, thermostat, native mode, and native level controls.
No runtime commands or measurements go through Tuya cloud. The initial
target is the Veltron JH-18-WiFi; mappings come from the selected device's
actual account schema and authenticated LAN response, not guessed DPS IDs.

## One-time app onboarding

`terrace-heater-setup` is a separate, interactive provisioning utility for
direct-LAN heater control. It uses a **one-time Tuya Smart app login** to
obtain the selected heater's Device ID, local key, and data-point metadata.
It does not send heater control commands or require a Tuya developer
project, Home Assistant installation, or cloud subscription.

From this checkout, run it in a **private interactive terminal**:

```bash
uv run --locked --extra heater-setup terrace-heater-setup
```

Enter the app's **User Code** at the hidden prompt. This is found under
Tuya Smart **Me > Settings > Account and Security > User Code**. Scan the
displayed QR code with the app's scanner, check the requesting application,
and approve access. The utility uses the Home Assistant-compatible public
app identity supported by Tuya's sharing SDK; it does not install or run
Home Assistant. Select the heater explicitly by its app name.

The User Code is an account-login identifier, **not** the heater's Device ID
or local key. Do not put it in GitHub secrets, command-line arguments, MCP
configuration, screenshots, logs, or source. The setup session is discarded
when the utility finishes, fails, or is cancelled.

The optional `heater-setup` dependencies are needed only for provisioning,
not for the existing MCPs or subsequent direct-LAN operations.
The preferred flow depends on Tuya granting app-authorized access to this
device's local key and data model. Denied authorization, missing keys, or
missing metadata are errors, not reasons to silently switch to cloud control.

By default, the utility writes two files under
`~/.config/electro/terrace-heater/`, with a user-only directory and files:

| File | Purpose |
| --- | --- |
| `runtime.env` | Private, shell-quoted POSIX launcher environment containing the Device ID, local key, and eventual verified profile path. **Contains a secret; never print, commit, or upload it.** |
| `onboarding.json` | Only the selected device's identity and data-point metadata, explicitly marked as awaiting LAN and mapping verification. No local key, User Code, or account tokens. |

An explicit `--output-dir /absolute/private/path` selects another location.
The utility refuses Git directories, unsafe directory permissions, symbolic
links at its destination, or overwriting existing provisioning. No other
devices' credentials are persisted. SDK response bodies and account tokens
are excluded from errors and logs.

Next, verify authenticated LAN status and generate the private
`device.json` profile without changing heater settings:

```bash
uv run --locked --extra heater-setup terrace-heater-setup --verify-lan
```

Use the same `--output-dir` if onboarding used a nondefault location.
Verification compares the Device ID/product, schema, units, conversions,
and observed data-point values, and refuses an ambiguous LAN result. It
does not overwrite an existing profile or perform a physical control test.
The [synthetic profile example](../../examples/terrace-heater.example.json) explains
the shape only: its identity, address, and verification timestamp are
invented and must never be used as evidence about a real heater.

**Provisioning is not a physical control test.** Do not guess data points,
temperature scaling, percentages, or mode meanings from another heater.
Heater identity is the Device ID; a cached IP is only a connection hint,
and a failed authenticated connection triggers bounded identity-based
rediscovery rather than control of the next available device.

## Portable runtime

The runtime needs only the `heater` extra, not the cloud SDK or QR renderer:

```bash
uv run --locked --extra heater terrace-heater-mcp \
  --env-file "$HOME/.config/electro/terrace-heater/runtime.env"
```

`--env-file` explicitly reads only the three allowed provisioning fields,
validates them, and **never executes the file as shell code**. It refuses
partial, duplicate, malformed, or mixed file/process-environment credentials.
Without that flag, inject all three environment values through the host's
private secret mechanism. No implicit `.env` lookup, Keychain dependency,
embedded Mac path, cloud session, or GitHub API access is needed.

Move private provisioning securely to the new LAN host, update
`TERRACE_HEATER_CONFIG` to its absolute profile path, and provision the local
key there without exposing it in shell history or process arguments.
Preserve file/directory permissions. Tuya discovery requires UDP 6666,
6667, and 7000; authenticated control uses TCP 6668. Container networking
must carry the LAN discovery traffic. No public port forwarding is needed.
Resetting/re-pairing the heater can rotate the key; re-provision explicitly
rather than falling back to another identity or cloud control.

## Tools and control semantics

| Tool | Inputs and behavior |
| --- | --- |
| `get_terrace_heater_status` | No inputs. Returns enabled state, reported temperature, setpoint, mode, level, retrieval time, warnings, and verified capability options/bounds. Never enables heating. |
| `set_terrace_heater_power` | Strict boolean `enabled`. Sets an explicit state, never toggles; verifies fresh read-back. |
| `set_terrace_heater_temperature` | Numeric `temperature_c`. Validates exact configured bounds and step; no rounding, clamping, or implicit power-on. |
| `set_terrace_heater_mode` | String `mode` from `capabilities.modes`. Uses native labels without inventing their meaning. |
| `set_terrace_heater_power_level` | String `level` from `capabilities.power_levels`. Native labels, not percentages or measured watts. |

Temperature is the **heater's reported sensor value**, not an independently
calibrated room measurement. A value returned while off may be retained;
`temperature_measured_at` is null and `temperature_freshness` is
`not_reported`. The retrieval timestamp must not be presented as sensor
measurement time. Missing readings are null, not zero. `enabled=true`
does not establish actual heating activity; `heating_active` remains null
without a verified activity measurement.

The product advertises a 5-40 C thermostat and four dimming percentages,
but control uses the device's verified bounds and native levels. The
implementation does not infer a percentage-to-level or wattage mapping.
Settings changes while off are rejected until that firmware behavior has
been separately verified; already-matching settings are read-only no-ops.
An explicit power-on may immediately begin heating at the retained setpoint.

Every operation authenticates the configured heater. Operations are serialized
within the process, and a write is sent only once on its already-authenticated
connection, with SDK write retries disabled. Read-back uses fresh authenticated
connections to avoid queued pre-command reports and allows a three-second
settling window between bounded socket reads. Only reads are repeated; the
control command is never replayed. Missing/failed acknowledgements are disclosed even when read-back
observes the requested state. An unavailable or mismatched read-back is a
tool error with an uncertain outcome, not proof that heating is off.
External app changes remain possible; never assume exclusive hardware control.

Reads are annotated read-only; controls are annotated mutating. Respect the
client's permission policy and explicit user intent. No automatic schedules,
startup commands, background polling, unattended control tests, or software
guarantee of physical shutdown is included.

## Registration and identity storage

Add a separate entry without replacing other MCPs. A published, verified
commit keeps the launcher independent of the checkout's name or location:

```json
{
  "mcpServers": {
    "terrace-heater-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--python", "3.12",
        "--from",
        "estonia-nordpool-prices[heater] @ git+https://github.com/vasilyevstan/home_automation.git@<tested-commit-sha>",
        "terrace-heater-mcp",
        "--env-file", "/absolute/private/path/runtime.env"
      ],
      "tools": [
        "get_terrace_heater_status",
        "set_terrace_heater_power",
        "set_terrace_heater_temperature",
        "set_terrace_heater_mode",
        "set_terrace_heater_power_level"
      ],
      "timeout": 120000
    }
  }
}
```

Replace `<tested-commit-sha>` with a published revision. This isolated launch
does not change the checkout's shared virtual environment and continues to
work if that checkout is moved. Only the private env-file path belongs in
client configuration, never the local key or User Code.

The confirmed **Device ID only** can be stored as the GitHub Actions secret
`TERRACE_HEATER_DEVICE_ID` using an interactive command:

```bash
gh secret set TERRACE_HEATER_DEVICE_ID --repo vasilyevstan/home_automation
```

Keep the local key in private host secret storage. A local MCP cannot read
GitHub secret values back; future deployment must inject its configuration.
The runtime host must reach the heater on its LAN. Local control also does
not prevent the heater's firmware from contacting Tuya independently.

Run the synthetic setup, runtime, and MCP tests without a real account/device:

```bash
uv run --locked --extra heater-setup python -m unittest discover \
  -s tests -p 'test_terrace_heater*.py' -v
```

Live control checks require someone present with safe heater surroundings.
Record initial settings, verify only explicitly authorized controls, restore
safe non-power settings, and finish with a fresh confirmation of heating off.
If that confirmation fails, check the physical device rather than claiming
success or replaying commands blindly.

References: [Tuya Local configuration paths](https://github.com/make-all/tuya-local#choose-your-configuration-path),
[Tuya sharing SDK](https://github.com/tuya/tuya-device-sharing-sdk), and
[app User Code and QR instructions](https://www.home-assistant.io/integrations/tuya/).
