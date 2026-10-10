# Terrace-heater MCP maintenance

- Read [README.md](README.md). `setup.py` performs explicit one-time app
  provisioning; `client.py` owns verified local identity/mappings and runtime;
  `server.py` exposes the five tools and explicit private env-file option.
- Keep `heater` separate from `heater-setup` dependencies. Runtime is direct
  LAN, not cloud control. Never execute an env file, expose its local key, or
  commit private provisioning, account identifiers, or real mappings.
- Require verified device identity, authenticated observations, documented
  bounds/steps, and native mode/level labels. Never guess DPS IDs, unit scaling,
  heater activity, or a native-level-to-wattage mapping.
- Preserve off-state restrictions, explicit power intent, serialization,
  single-attempt commands, fresh bounded read-back, and uncertain-outcome errors.
  A status request or server startup must never enable heating.
- Preserve portable private-path configuration, existing commands/tools, and
  compatibility imports. Do not tie runtime installation to the checkout path.
- From the repository root:
  `uv run --locked --extra heater-setup --inexact python -m unittest discover -s tests -p 'test_terrace_heater*.py'`.
  Automated tests use mocks; physical tests require separate explicit safe intent.
