"""Compatibility import for the local Govee client."""

import sys

from home_automation.govee import client as _implementation

sys.modules[__name__] = _implementation
