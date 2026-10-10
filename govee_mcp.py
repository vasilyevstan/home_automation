"""Compatibility entry point for the Govee MCP."""

import sys

from home_automation.govee import server as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
