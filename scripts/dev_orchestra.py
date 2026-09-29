#!/usr/bin/env python3
"""Entry point for the ``dev-orchestra`` command.

Runnable directly (``python scripts/dev_orchestra.py doctor``) without
installing anything: it puts its own directory on ``sys.path`` first.
"""

import os
import sys

# Checked before anything else is imported, and written so an older
# interpreter can still read this far: the package may use syntax an older one
# does not have, and failing on that would name a line of code, not the problem.
if sys.version_info < (3, 11):
    sys.stderr.write(
        "dev-orchestra needs Python 3.11 or later; this is Python %d.%d (%s)\n"
        % (sys.version_info[0], sys.version_info[1], sys.executable)
    )
    raise SystemExit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
