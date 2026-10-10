"""Compatibility entry point for the Sensibo MCP."""

import sys

from home_automation.sensibo import server as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
