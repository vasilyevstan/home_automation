"""Compatibility entry point for the Sonos MCP."""

import sys

from home_automation.sonos import server as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
