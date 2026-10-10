"""Compatibility import for the household electricity-pricing engine."""

import sys

from home_automation.electricity_pricing import engine as _implementation

sys.modules[__name__] = _implementation
