"""What every part of running a child CLI reads: how often to poll, and
whether this is Windows.

The lowest of the ``execution_*`` modules.
"""

from __future__ import annotations

import sys

_POLL_SECONDS = 0.2

IS_WINDOWS = sys.platform.startswith("win")
