"""Compatibility import for the electricity-market forecast client."""

import sys

from home_automation.electricity_market import forecast as _implementation

sys.modules[__name__] = _implementation
