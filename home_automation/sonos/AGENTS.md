# Sonos MCP maintenance

- Read [README.md](README.md). `client.py` implements optional SoCo transport,
  identity, groups, media, and queue handling; `server.py` exposes the tools.
- Preserve lazy optional dependencies and zero discovery/control at import or
  startup. Use discovered exact UIDs and freshly checked device identities,
  never room names or a stale IP cache as authorization.
- Preserve room-versus-group effects, bonded-speaker restrictions, coordinator
  checks, zero-based indexes, and the coordinator-bound native queue revision.
  Do not bypass revision checks or clear/replace queues implicitly.
- Writes are explicit, serialized, and single-attempt. Preserve bounded
  read-back, partial-step evidence, cancellation behavior, and uncertain errors.
  Successful device state is not proof that sound was physically heard.
- Keep media metadata and real device captures private; redact display URIs.
  Keep existing tool/command names, optional extra, and compatibility imports.
- From the repository root:
  `uv run --locked --extra sonos --inexact python -m unittest discover -s tests -p 'test_sonos*.py'`.
  Read-only acceptance must not start playback, change volume, or alter groups.
