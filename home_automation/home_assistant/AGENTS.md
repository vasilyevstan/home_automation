# Home Assistant maintenance

- Read [README.md](README.md). HA's native MCP is the gateway; do not create
  another device-protocol server or make HA depend on a CLI/MCP process.
- `pricing.py` only normalizes published market inputs and adapts the existing
  engine's billable-unit quotes. Never duplicate tariff/VAT/calendar formulas.
  Canonical core files are bundled unchanged by the explicit allowlist in
  `bundle.py`; no private profile, credential, SDK, or server is bundled.
- Keep HA and MCP dependency environments isolated. Preserve existing MCP tools,
  entry points, imports, and financial behavior.
- Current rates are marginal EUR/kWh, not monthly invoices. Preserve negative
  prices, net export earnings, invoice-derived qualifications, exact decimal
  values, period boundaries, and missing/unpublished data. No zero fallbacks.
- Use actual UTC quarters and Tallinn tariff days, including adjacent CET market
  days and DST. Never use Nord Pool's peak blocks as household tariff bands.
- Restrict native MCP to selected APIs/entities and specifically requested
  controls. The pricing API itself is read-only. Do not expose arbitrary service,
  register, file, administrator, installer, or protection-setting operations.
- HA must be the sole Huawei Modbus client during handover. Keep the old local
  solar MCP disconnected as rollback; cloud history is independent. Generation
  caps, export limits, and household load are not interchangeable.
- Daikin LWT offset is not room temperature. Preserve native mode/range/guard
  checks and obtain fresh read-back. Never toggle a device or perform a
  same-value write as an installation/connectivity check.
- Run the root `test_home_assistant_pricing.py` suite and the isolated real-HA
  test command in the README. Do not install HA dependencies into the shared
  MCP environment. Use synthetic data; keep live captures and private profiles
  out of commits and public logs.
