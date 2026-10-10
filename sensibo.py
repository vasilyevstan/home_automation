"""Compatibility import and enrollment entry point for Sensibo."""

import sys

from home_automation.sensibo import client as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
