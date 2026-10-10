"""Compatibility entry point for private terrace-heater provisioning."""

import sys

from home_automation.terrace_heater import setup as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
