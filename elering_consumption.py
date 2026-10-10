"""Compatibility import for the household-consumption client."""

import sys

from home_automation.household_consumption import client as _implementation

sys.modules[__name__] = _implementation
