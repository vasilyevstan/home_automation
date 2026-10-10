# Tapo MCP maintenance

- Read [README.md](README.md). `client.py` owns local discovery, private
  inventory, credentials, and relay operations; `server.py` exposes three tools.
- Keep `python-kasa` optional and Python 3.11+ specific to this MCP. Do not
  import its SDK or contact plugs at package import or server startup.
- Target verified MACs, not names or cached IPs. Preserve bounded rediscovery,
  retained unavailable/unsupported devices, and authenticated identity checks.
- Preserve null-versus-zero measurements, W-versus-kWh units, private inventory
  permissions, and paired environment/Keychain credential handling.
- Relay changes are explicit desired states, never toggles. Preserve no-op
  handling, serialization, single-attempt writes, fresh read-back, and errors
  for unconfirmed outcomes. Never broaden permissions as part of a refactor.
- From the repository root:
  `uv run --locked --extra tapo --inexact python -m unittest discover -s tests -p 'test_tapo*.py'`.
  Automated tests must mock physical switching and use synthetic identities.
