# Starlink MCP maintenance

- Read [README.md](README.md). `client.py` handles optional reflected gRPC,
  telemetry, and guarded commands; `server.py` exposes the seven MCP tools.
- Preserve the optional `starlink` extra, lazy SDK loading, trusted-LAN boundary,
  bounded requests, and explicit unsupported/partial data. Do not change routes
  or use cloud account services.
- Keep throughput units, ring-buffer alignment, sample coverage, and reported
  timestamps truthful. Traffic is not a speed test; missing is not zero.
- Keep controls disabled by default. Require the configured expected identity,
  enabled action, and explicit user intent, including reboot confirmation.
  Preserve serialization, no write retries, and verified/uncertain outcomes.
- Retain all environment names, default private paths, console/tool names, and
  compatibility imports. Discovery of a write schema does not authorize it.
- From the repository root:
  `uv run --locked --extra starlink --inexact python -m unittest discover -s tests -p 'test_starlink*.py'`.
  Live acceptance must not reboot the dish or change its sleep/heating settings.
