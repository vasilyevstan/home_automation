"""Compatibility import for the local Tapo client."""

import sys

from home_automation.tapo import client as _implementation

sys.modules[__name__] = _implementation
