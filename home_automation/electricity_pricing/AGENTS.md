# Electricity-pricing MCP maintenance

- Read [README.md](README.md). `engine.py` contains pure Decimal calculations;
  `server.py` connects the private profile, shared market client, and shared
  household client. Preserve the existing `electrisity-price` spelling.
- Match each interval's kWh with its actual UTC market price; do not multiply
  kWh by duration again, redistribute measured quantities, or use forecasts
  as published prices. Preserve raw/billable and effective-dated netting rules.
- Preserve negative proceeds, outside-VAT credits, taxable balancing, component
  line rounding, section VAT, and one fixed-fee occurrence per obligation/month.
  Analytical reports and incremental estimates are not finalized invoices.
- Snapshot profiles once per call. Missing rates, gaps, unknown flows, and
  conflicting inputs are errors. Retain evidence qualifications; never fit
  rates silently or publish real profiles, invoices, or replay fixtures.
- Keep the three tools and root compatibility imports unchanged unless the
  requested change explicitly needs an API revision. Reuse shared clients.
- From the repository root:
  `uv run --locked python -m unittest discover -s tests -p 'test_electricity_pricing*.py'`.
  Relevant private invoice replays remain outside the repository and public logs.
