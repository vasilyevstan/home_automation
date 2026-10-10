"""Compatibility entry point for the household-consumption MCP."""

import sys

from home_automation.household_consumption import server as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
