# Home Assistant maintenance

- Read [README.md](README.md). HA's native MCP is the gateway; do not create
  another device-protocol server or make HA depend on a CLI/MCP process.
- `pricing.py` only normalizes published market inputs and adapts the existing
  engine's billable-unit quotes. Never duplicate tariff/VAT/calendar formulas.
  Canonical core files are bundled unchanged by the explicit allowlist in
  `bundle.py`; no private profile, credential, SDK, or server is bundled.
- `calculator.py` adapts the canonical engine and read-only Elering client.
  Native bill presets use published prices only. Explicit supplied future-price
  estimates must remain labeled estimates and cannot finalize actual invoices.
  Month-to-date uses its dedicated provisional/full-fixed-fee engine path;
  do not weaken `monthly` complete-month checks or zero-fill unmeasured days.
- Automatic household reads require a confined private, mode-0600 credential
  file. Snapshot one profile per calculation, retain the real metering cutoff,
  and reject historical tariff gaps before requesting household data.
- Keep HA and MCP dependency environments isolated. Preserve existing MCP tools,
  entry points, imports, and financial behavior.
- `price_api.py` and `controls.py` register explicitly selected APIs. Do not name
  an internal helper `llm.py`: HA treats that filename as a contributed platform
  and expects an `async_get_tools` hook.
- Current rates are marginal EUR/kWh, not monthly invoices. Preserve negative
  prices, net export earnings, invoice-derived qualifications, exact decimal
  values, period boundaries, and missing/unpublished data. No zero fallbacks.
- Use actual UTC quarters and Tallinn tariff days, including adjacent CET market
  days and DST. Never use Nord Pool's peak blocks as household tariff bands.
- Restrict native MCP to selected APIs/entities and specifically requested
  controls. The pricing API itself is read-only. Do not expose arbitrary service,
  register, file, administrator, installer, or protection-setting operations.
- Optional device bindings enable a separate guarded API. Reuse the existing
  HA device clients and real-command authentication, not setup write probes or
  another Modbus connection. Journal intent before a write and retain uncertain
  outcomes; no automatic retries or rollback.
- Huawei control/read transactions must hold the device's `update_lock`, shared
  with its native polling coordinator, not only the API's own per-device lock.
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
