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


def _keep_bytecode():
    """Cache the package's compiled modules even where the host turns caching off.

    A host may run every command with ``PYTHONDONTWRITEBYTECODE`` set (Claude
    Code does), and then each run compiles every module from source again:
    about 0.3 s of a command that takes well under a second once cached. The
    flag is there to keep ``__pycache__`` out of the directories a command is
    run in, so the cache goes to a directory of its own instead: the one the
    user named with ``PYTHONPYCACHEPREFIX``, or else one under the user's cache.
    """
    if not sys.flags.dont_write_bytecode:
        return
    if not sys.pycache_prefix:
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache")
        )
        sys.pycache_prefix = os.path.join(base, "dev-orchestra", "pycache")
    sys.dont_write_bytecode = False


_keep_bytecode()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Imported last: the version check and the bytecode cache have to come first.
from orchestrator.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
