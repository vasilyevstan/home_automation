#!/usr/bin/env python3
"""Compatibility entry point for the electricity-market CLI and helpers."""

import sys

from home_automation.electricity_market import prices as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
