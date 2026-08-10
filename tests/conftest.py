"""Offline test shim for optional runtime retry dependency in this sandbox."""
from __future__ import annotations

import sys
import types

try:
    import tenacity  # noqa: F401
except ModuleNotFoundError:
    module = types.ModuleType("tenacity")
    module.retry = lambda **_: (lambda fn: fn)
    module.retry_if_exception_type = lambda *_: None
    module.stop_after_attempt = lambda *_: None
    module.wait_exponential_jitter = lambda **_: None
    sys.modules["tenacity"] = module
