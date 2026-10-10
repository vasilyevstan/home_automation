# Govee MCP maintenance

- Read [README.md](README.md). `client.py` implements the standard-library LAN
  transport and verified desired states; `server.py` exposes the six tools.
- Preserve LAN-only operation, lazy discovery, bounded requests, serialized
  response-port access, and process-local inventory. Do not add a cloud fallback.
- Require exact discovered device IDs with matching UDP-source/advertised
  addresses and nonconflicting identity/SKU. Never authorize by a name or old IP.
- Preserve required power state, optional unavailable fields, RGB-mode null
  color temperature, strict command types, and native device limitations.
- Brightness/color changes must not implicitly energize an off light. Keep
  no-op checks, one mutation attempt, fresh read-back, and explicit uncertainty.
- Preserve all command/tool names and compatibility imports. Do not place real
  identities, packet captures, or device state in public tests or documentation.
- From the repository root:
  `uv run --locked python -m unittest discover -s tests -p 'test_govee*.py'`.
  Use injected UDP transport; live acceptance is discovery and reads only.
