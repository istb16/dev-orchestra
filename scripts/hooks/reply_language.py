#!/usr/bin/env python3
"""Claude Code plugin hook: keep the orchestrator replying in ``language.reply``.

Run by ``hooks/run`` as ``python -I reply_language.py <prompt|session-start|stop>``
with the hook's JSON on stdin. The logic is ``orchestrator.reply_language``.

A hook must never break the session it runs in, so this fails open: any
error at all, including one importing the package, prints nothing and exits 0.
"""

import os
import sys


def main(argv: list) -> int:
    try:
        # -I leaves the script's own directory off sys.path; the package is
        # one level up, next to dev_orchestra.py.
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from orchestrator import reply_language

        event = argv[1] if len(argv) > 1 else ""
        output = reply_language.run(event, sys.stdin.buffer.read(), os.environ)
        if output:
            sys.stdout.write(output)
            sys.stdout.flush()
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
