"""Compatibility entry point for the terrace-heater MCP."""

import sys

from home_automation.terrace_heater import server as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
