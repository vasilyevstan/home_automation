"""Compatibility import for the local Starlink client."""

import sys

from home_automation.starlink import client as _implementation

sys.modules[__name__] = _implementation
