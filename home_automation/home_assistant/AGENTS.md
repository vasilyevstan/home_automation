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
- Native MCP prefixes tools with the API display-name slug, not only its ID.
  Keep existing API display names stable when adding tools; otherwise an
  unchanged price-tool suffix still breaks the client's existing allowlist.
- Current rates are marginal EUR/kWh, not monthly invoices. Preserve negative
  prices, net export earnings, invoice-derived qualifications, exact decimal
  values, period boundaries, and missing/unpublished data. No zero fallbacks.
- `price_forecast.py` may use the bundled SDK-free Wattcast core, but every
  predicted wholesale quantile must pass through `pricing.py` and the same
  effective-dated private tariff engine. Do not duplicate tariff logic or
  compare raw wholesale forecasts with household all-in actual prices.
- Forecast seven complete Tallinn delivery days starting tomorrow; expect
  167/168/169 hours across DST. Canonical HA Nord Pool quarters replace
  predictions when published. If canonical published data is missing, report
  unavailable rather than trusting a second published-price source.
- Retain every distinct provider issuance per delivery hour and D-1 through
  D-7 horizon for 90 days. Deduplicate exact same-issuance values and reject
  conflicting values under one issuance timestamp. Keep raw hourly quantiles
  in storage and recompute quarter-hour all-in values with the tariff effective
  at delivery time. Migrate the prior latest-only archive as explicitly partial
  evidence rather than presenting it as original forecast history.
- Keep future archived delivery hours hidden by default. The explicit bounded
  `include_future_days` archive option may expose them for publication
  comparison, but charts must join exact quarter-hour timestamps and render
  only pairs with a canonical published household price. Partial or missing
  publication is unavailable, never zero.
- Keep the backward-compatible `archive` response latest-only per delivery
  hour. Use the bounded one-delivery-day `evolution` response for chronological
  vintages. Preserve the default single-horizon contract; `all_horizons: true`
  combines D-1 through D-7 and reports the actual horizons included. Compute
  per-issuance MAE, signed bias and movement only over the profile-compatible
  quarter intersection shared by every retained issuance and the canonical
  published schedule. Keep partial coverage and migrated evidence explicit.
  Retain published schedules for 90 days with profile provenance; never insert
  zero or forecast fallbacks.
- Forecasts, p10-p90 bands and provider accuracy are advisory evidence. They
  cannot authorize heating writes, finalize bills, prove savings, or be exposed
  through an unauthenticated application endpoint. Keep the detailed payload in
  the response service; recorder-facing sensor attributes must stay compact.
- Use actual UTC quarters and Tallinn tariff days, including adjacent CET market
  days and DST. Never use Nord Pool's peak blocks as household tariff bands.
- Restrict native MCP to selected APIs/entities and specifically requested
  controls. The pricing API itself is read-only. Do not expose arbitrary service,
  register, file, administrator, installer, or protection-setting operations.
- Optional device bindings enable a separate guarded API. Reuse the existing
  HA device clients and real-command authentication, not setup write probes or
  another Modbus connection. Journal intent before a write and retain uncertain
  outcomes; no automatic retries or rollback.
- The deterministic DHW optimizer is not an LLM tool. It must use the existing
  all-in price snapshot and the same shared guarded `EnergyDevices` instance.
  Default to shadow with zero writes; never let installation, setup, status
  reads, or option saves promote it.
- Preserve the 07:00/21:00 readiness, guarded native minimum capability,
  automatic 45/50°C policy, native target range/step validation, complete
  published-price requirement, sign-safe absolute/relative price gate, and
  ownership-aware restore. Size candidate blocks from learned shallow/deep
  50°C recovery duration for the measured temperature, including reserve and
  quarter-hour rounding; use the deep allowance for 55°C storage rather than
  claiming target-specific learning. Select the absolute minimum-average
  feasible block; later wins only on an exact average tie. Reject blocks that
  cannot retain at least 45°C through the protected endpoint.
  Treat 45°C as usable and 50°C as preferred. Recover immediately below 45°C
  and keep that recovery latched through measured 50°C. At 45–49°C, accept a
  truthful usable-below-preferred outcome only when the current price is
  materially expensive relative to the anchored feasible block; otherwise
  request 50°C. Missing or incomplete prices fail safe to 50°C. Never toggle a
  native 50°C target merely to defeat thermostat hysteresis, and never mandate
  a post-readiness or final 55°C top-up.
  Suppress storage above 50°C until retention qualifies with at least three
  clean passive-loss samples across two readiness cycles. Permit 55°C only
  when both material price gates pass, the retention-derived horizon is no more
  than eight hours, and the model predicts at least 45°C through the protected
  endpoint. Hold 55°C only inside the selected economic-storage block and
  restore 50°C through exact ownership checks when it ends.
  Preserve minimum dwell for ordinary price transitions, provisional
  post-deadline evidence, separate learned heating durations, exact
  expected-previous validation, persisted pending operations and no automatic
  retry. A policy migration must return a write-capable old revision to Shadow,
  send no command, invent no ownership, leave manual/unowned 55°C untouched,
  and reconcile optimizer-owned 55°C only from fresh exact state after explicit
  reactivation.
- Never select 30°C automatically or expose it as an optimizer mode. Preserve
  it only as an explicit guarded native target. If the unchanged 30°C option is
  below the selected device's native target range, persist the device's actual
  minimum instead. Native DHW standby/off remains out of scope unless a later
  task supplies new evidence and policy.
- Preserve the evidence-gated DHW promotion path, but permit a separately
  recorded owner-approved early activation when the owner explicitly requests
  full control. Do not synthesize qualification days, cycles, checks or writes.
  Early activation changes authority only; every comfort/readiness exit, exact
  expected-previous check, manufacturer guard, ownership rule and
  uncertain-command latch remains mandatory.
- Manual/manufacturer changes, powerful/hygiene-like operation, safety flags,
  circuit standby, weather-dependent control, and configured/effective target
  mismatch suspend optimizer ownership. An uncertain write latches fault and
  needs an explicit fresh-read resume; do not replay or invert it.
- Repeat the relevant manufacturer checks immediately before every Daikin
  dispatch and bind automatic requests to the exact expected previous target or
  circuit state. Reject a queued stale decision instead of overwriting a newer
  device or user change. Extreme Price Protection has priority while faulted,
  while it owns a protected load, while Armed lacks a current canonical price,
  or while a published hard-stop quarter is active.
- Keep the floor-heating optimizer separate from DHW counters and default it to
  zero-write Shadow. The native LWT offset range is -10°C to +10°C, but
  capability is not permission: revision 7 selects a 4/6/8/10°C candidate from
  exact €0.05/20%, €0.08/30%, €0.12/40% and €0.18/50% price-spread tiers, then
  clamps it by coldest-room headroom and projected leaving-water temperature.
  Never project useful LWT below 25°C. At or below 20°C do not acquire a
  negative overlay; through 20.5/21/22°C cap setback to 2/4/6°C. Cheap-block
  preheat may use the selected tier at or below 20°C, is capped to 6°C through
  20.5°C and 4°C through 21°C, and is suppressed above 21°C because valve/flow
  telemetry is unavailable. Plan only from complete Tallinn delivery days with
  DST-aware 92/96/100-quarter boundaries; preserve a validated anchored pair
  until its peak ends. Require complete candidate-day all-in prices, the
  comfort and hydraulic guards, useful heating conditions and DHW heating
  priority. Treat only `recovery`, `readiness_charge` and `economic_storage` as
  unambiguous DHW heating intent; `readiness_preferred` and
  `readiness_usable` do not prove compressor demand. Reset floor evidence when
  this numerical policy changes and require
  five clean revision-7 cycles before evidence-gated promotion. A separately
  recorded, explicit owner approval may activate the pilot earlier without
  marking that evidence passed.
- In active floor mode, accept -2°C and 0°C as the initial native schedule
  baselines and capture one fresh baseline before the first offset
  action, keep the owned adaptive target stable across evaluations without
  mid-window escalation, and restore the exact baseline only while current
  state still matches ownership. After a comfort, LWT, HVAC or compressor exit,
  suppress reacquisition through the end of that same price window. Bind every
  dispatch to exact expected previous state and native read-back. An accepted
  native schedule transition while an overlay is owned releases ownership,
  preserves the new baseline, contaminates that candidate and holds Active
  without reapplication until the current price window ends. Unexpected or
  unstable changes return to Shadow. Persist a pending operation
  before dispatch; while one is unresolved, block every new dispatch. Any
  post-dispatch error faults and requires fresh-state reconciliation without
  automatic retry, inversion or rollback. Revision-6 state migrates
  command-free to Shadow, preserving pending/owned state for review while
  resetting numerical-policy evidence and anchored plans.
- Use only explicitly configured, fresh occupied-room temperature entities for
  floor comfort decisions. Require every configured source and use the coldest
  room. The Daikin `IndoorTemperature` reading is from an unheated garage and
  is diagnostic only; never fall back to it when a room source is missing.
  Continue using a climate entity's measured temperature for comfort while its
  HVAC is active, but yield price shifting and exclude that period from floor
  thermal-response evidence. A setpoint change while the HVAC remains off does
  not by itself invalidate the temperature.
- Do not infer household surplus from inverter generation, infer appliance kWh
  from aggregate import, or claim floor-heating savings without whole-unit
  energy. Fresh signed net-grid power remains required for surplus-following,
  zero-export, solar-heating or savings claims. The only exception is the
  bounded open-loop Huawei generation-cap controller below, which acts from
  exact negative export economics and must not claim measured export response.
- Record DHW and floor price opportunities in dedicated compact Recorder
  sensors as EUR per shifted kWh, deduplicated by anchored plan. Never label the
  spread itself as saved euros. Estimated or realized euros require measured
  electrical energy attributable to that exact action; confirmed writes alone
  are insufficient. Keep growing operational history in private storage/events;
  expose only a small bounded recent-decision list in Recorder attributes.
- Keep extreme-price protection independent from both heating optimizers. It
  consumes canonical published household all-in quarters only: forecasts may
  warn but cannot authorize device control. Preserve the €1.50/kWh hard stop,
  €2/kWh alert severity, €0.50/kWh-for-two-hours warning, two-hour maximum
  lease, DHW below-45°C/recovery/readiness-charge exits and 20.5°C
  coldest-room floor restore guard. Do not replace anchored DHW scheduling with
  a generic readiness lead-time exit; latch an observed DHW exit across
  protection ownership until the circuit is restored.
- Start Extreme Price Protection before DHW and floor runtimes so persisted
  breaker ownership is reconciled before ordinary heating evaluation. Stop the
  ordinary runtimes before the breaker during cleanup.
- Extreme-price installation and option saves default to Shadow. Do not expose
  or accept Armed until observation is qualified by seven live Shadow days or
  the deterministic seven-day commissioning replay, and alert delivery plus
  controlled Daikin/climate exercises are explicitly verified. A natural
  €1.50/kWh event is not a promotion prerequisite, and passing gates must never
  select Armed automatically.
- Keep commissioning on the production guarded writer. Daikin exercises require
  fresh safe state, DHW at or above 46°C, no active readiness lead-time window,
  room headroom, and exact standby/on read-back. Do not let the Shadow
  optimizer's broader recovery latch replace those commissioning-specific
  checks. PyAltherma advertises circuit `Power` as an allowed-value list, not a
  numeric-style `settable` profile; validate `on`/`standby` membership before a
  write. Climate commissioning may briefly prepare one allowlisted unit only
  from fresh off state with at least 1°C no-cooling target headroom. Configure
  its exact target, fan and swing settings while it remains off, explicitly
  activate the saved HVAC mode, exercise exact off/restore, and return the unit
  to off. Keep the bounded stabilization dwell between commissioning power
  transitions. Journal every dispatched step and the underlying failure cause;
  any uncertain result faults without retry or rollback.
- Treat every breaker write as a separate non-atomic operation. Persist exact
  per-load ownership, prior state, block identity and lease; restore only a
  load still matching breaker-owned standby/off state. Manual or external
  changes cancel ownership. Uncertain stop/restore outcomes fault without
  automatic retry, inversion or rollback.
- Climate protection is limited to the explicit allowlist. Do not emit no-op IR
  commands to units already off. Snapshot mode, target/unit, fan, vertical and
  horizontal swing before stopping an active unit, validate current
  capabilities before restore, and state clearly that HA/Sensibo cloud
  read-back does not prove physical HVAC power or IR reception.
- An active climate stop/recovery contaminates floor thermal-response evidence.
  Never present Daikin circuit standby as whole-heat-pump zero power, present
  the multi-load shutdown as atomic, or convert an avoided high-price quarter
  into saved kWh/euros without attributable energy measurement.
- Huawei control/read transactions must hold the device's `update_lock`, shared
  with its native polling coordinator, not only the API's own per-device lock.
  Native Huawei service/entity writes do not share that lock: after
  authentication and prepared-audit persistence, repeat the exact
  identity/Pmax/configured/active-limit read immediately before dispatch and
  reject any change. Active power must be fresh for policy qualification but is
  expected to move between reads and must not be treated as immutable device
  state. Document that the residual read-to-write race remains.
- Huawei writes fail closed unless the selected entry has elevated parameter
  configuration and a usable native configuration coordinator. Keep the read
  tool for read-only entries but do not advertise `SolarLimitWrite`. Every
  expected request and audit record must bind config-entry ID, serial and Pmax;
  reject any mismatch. Include one fresh AC active-power read with native cap
  capabilities.
- Separate pre-dispatch failures from post-dispatch uncertainty. Lock,
  authentication, expected-state and read failures must say no command was sent.
  Once dispatch begins, do not replay, invert or roll back at controller level.
  Do not claim the underlying transport has no internal retries. Preserve an
  exact verified device outcome when only final JSONL audit persistence fails,
  and expose that audit failure separately.
- Keep solar curtailment independent from Daikin and available for solar-only
  configs. Policy revision 3 is Shadow-only: expose only Off and Shadow, reject
  Armed, keep production authority disabled, and hard-gate acquisition inside
  the writer-facing method. Stored Armed state migrates command-free to Shadow.
  Never let replay, commissioning, permissions or daylight observations enable
  production writes.
- A negative `export_earnings_eur_kwh` is a counterfactual opportunity. At
  night record a price-only point with `below_horizon` and no watt proposal.
  A qualified daylight proposal requires a known contiguous negative block with
  at least 30 minutes remaining, matching unrestricted configured/active 100%
  or Pmax, `sun.sun` above the horizon, fresh AC active power of at least
  5,000 W on two observations and the full 60-second dwell. Observe the
  selected Huawei entry's active-power entity directly and retain the
  30-second timer as a backstop. Preserve the current percentage/watt mode and
  calculate only a native-step 4,000 W-equivalent Shadow proposal.
- Persist versioned solar state before and after dispatch through an
  acknowledged private atomic file write; HA Store alone is not a durable-write
  acknowledgement because ordinary write/serialization failures may be
  swallowed. Bind ownership to
  pricing entry, inverter serial and Pmax, with a maximum two-hour/block-end
  lease and one dispatched acquisition attempt per contiguous block. Low PV after
  acquisition does not restore. Restore the exact original on non-negative,
  stale/invalid or profile-changed prices, lease/block end, mode change or
  graceful unload, but only while exact expected target and ownership remain.
- On solar startup, exact target retains ownership for reconciliation, exact
  original clears it, and any other limit is an external/manual override with
  no write. Persisted pending phases and post-dispatch failures latch uncertain
  fault until explicit resume. Defer reconciliation while the selected Huawei
  entry is transiently not loaded or setting up; only a read failure after load
  faults. Pre-dispatch failures may requalify. Manual or external changes cancel
  ownership and suppress the rest of that block.
- During the bounded coordinator quarter-refresh transition, retain an exact
  owned cap only under its persisted block/profile/lease. Rollover alone must
  not restore, suppress, or consume the block; refreshed data decides whether
  ownership continues.
- Solar sensors may report the exact negative-export cost opportunity per kWh
  and elapsed eligible/owned minutes. Without fresh signed grid power they must
  never report curtailed kWh, zero-export performance, or estimated/realized
  euro savings.
  Keep the dedicated solar opportunity sensor Recorder-safe and label Armed
  values as live but unattributed, never realized savings.
- Keep configured, active, historical owned request/restore, and qualified
  Shadow-proposal solar-limit history in dedicated Recorder-safe
  equivalent-watt sensors. The request must be unavailable during ordinary
  Shadow operation, and raw target capability must never be presented as a
  proposal. Preserve native percent/watt mode in attributes. Keep solar
  relevance Recorder-safe and report `shadow_ready` only for the conjunction
  of above-horizon daylight, fresh ≥5 kW output, unrestricted native limits,
  two samples and the full dwell.
- Reconstruct rolling-24-hour activity episodes only from verified actual-state
  transitions. Close an interval at the next transition on its track; keep
  optimizer, native-schedule, manual/external and protection sources distinct.
  Keep compact target/offset/limit actions separate from persistent
  circuit/protected-load status so long enabled intervals cannot consume or
  obscure action evidence. Expose total, retained and truncated counts for
  each bounded collection.
  Simulations, counterfactual proposals, no-command outcomes and review-required
  records remain point events. Clip boundary-spanning intervals while retaining
  their original transition timestamp, and keep Recorder payloads bounded.
- HA must be the sole Huawei Modbus client during handover. Keep the old local
  solar MCP disconnected as rollback; cloud history is independent. Generation
  caps, export limits, and household load are not interchangeable.
- Daikin LWT offset is not room temperature. Preserve native mode/range/guard
  checks and obtain fresh read-back. Never toggle a device or perform a
  same-value write as an installation/connectivity check.
- Run the root `test_home_assistant_pricing.py` suite and the isolated real-HA
  test commands in the README, including `test_dhw_optimizer.py` and
  `test_floor_heating_shadow.py`, `test_extreme_price_protection.py`, and
  `test_solar_curtailment.py` and `test_price_forecast.py`. Do not install HA
  dependencies into the shared MCP environment. Use synthetic data; keep live
  captures and private profiles out of commits and public logs.
