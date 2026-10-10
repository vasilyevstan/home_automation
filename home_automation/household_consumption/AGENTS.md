# Household-consumption MCP maintenance

- Read [README.md](README.md). `client.py` owns credentials, OAuth, bounded
  requests, and measurement parsing; `server.py` owns the two read-only tools.
- Preserve the single-authorized-household check, 31-day request limit,
  five-second request spacing, bounded retries, and Keychain/environment rules.
  Never print credentials, household identities, or private response bodies.
- Preserve interval kWh, raw import/export, Tallinn dates, DST, pending
  intervals, and independent completeness for each direction. Missing is not
  zero; grid export is not total solar output, and readings are not live power.
- The pricing MCP reuses this client. Keep its automatic quarter-hour retrieval
  and the legacy imports/command compatible; do not start another MCP process.
- From the repository root:
  `uv run --locked python -m unittest discover -s tests -p 'test_elering_consumption*.py'`.
  Use synthetic public fixtures; keep authorized live captures private.
