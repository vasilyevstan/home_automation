"""Compatibility import for the local Sonos client."""

import sys

from home_automation.sonos import client as _implementation

sys.modules[__name__] = _implementation
