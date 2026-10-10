"""Compatibility entry point for the Tapo MCP."""

import sys

from home_automation.tapo import server as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
