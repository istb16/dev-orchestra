#!/usr/bin/env python3
"""Entry point for the ``dev-orchestra`` command.

Runnable directly (``python scripts/dev_orchestra.py doctor``) without
installing anything: it puts its own directory on ``sys.path`` first.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
