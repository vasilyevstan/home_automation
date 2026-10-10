# Electricity-market MCP maintenance

- Read [README.md](README.md) for source, interval, VAT, and forecast semantics.
  `server.py` exposes tools, `prices.py` contains the shared market/date helpers
  and CLI, and `forecast.py` handles Wattcast.
- Keep published prices separate from forecasts. Preserve UTC joins, Tallinn
  calendar boundaries, DST completeness, negative values, and explicit missing
  or stale data. These prices exclude household-specific charges.
- The consumption and pricing integrations reuse `prices.py`; do not duplicate
  its time or market validation logic or change their behavior incidentally.
- Preserve `electro`, the four tool names, `nordpool-ee`, `nordpool-ee-mcp`, and
  the root compatibility imports. Keep stdio stdout protocol-only.
- From the repository root, run the existing `test_nordpool_ee.py`,
  `test_mcp_server.py`, and `test_wattcast_forecast.py` suites with unittest.
  Live checks are read-only; do not add device-control behavior.
