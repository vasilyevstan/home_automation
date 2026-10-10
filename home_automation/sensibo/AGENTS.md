# Sensibo MCP maintenance

- Read [README.md](README.md). `client.py` handles the cloud API, enrollment,
  private inventory, and commands; `server.py` exposes the four MCP tools.
- Retain explicit enrollment, stable device identities, supported-model checks,
  and per-device capability/policy validation. Do not authorize newly discovered
  account devices automatically or embed keys or inventories in source.
- Preserve actual sensor timestamps, units, nulls, cache age, and stale flags.
  Acknowledged infrared/cloud state is not independently verified HVAC activity.
- Writes require an explicit selected device and desired state. Keep in-process
  serialization, fresh validation, no automatic write retries, and explicit
  uncertain outcomes. Imports and read-only checks must not emit controls.
- Preserve the existing command, enrollment entry point, Keychain service,
  private paths, and compatibility imports.
- From the repository root:
  `uv run --locked python -m unittest discover -s tests -p 'test_sensibo*.py'`.
  Use mocked account responses; never test physical switching automatically.
