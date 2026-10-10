"""Compatibility import for the local terrace-heater client."""

import sys

from home_automation.terrace_heater import client as _implementation

sys.modules[__name__] = _implementation
