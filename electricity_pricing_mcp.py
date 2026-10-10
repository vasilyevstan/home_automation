"""Compatibility entry point for the household electricity-pricing MCP."""

import sys

from home_automation.electricity_pricing import server as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
