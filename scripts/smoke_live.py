#!/usr/bin/env python3
"""Run the installed CLIs for real, and check the adapters still fit them.

The test suite may not do this. It has to pass on a machine with neither CLI
installed -- that is what CI runs on -- so it reviews with the `mock` provider,
which overrides ``run`` outright. The consequence was found the hard way: every
Codex run had been raising ``TypeError`` for weeks, because ``idle_timeout``
was added to ``Provider.run`` and not to the override, and 654 green tests said
nothing about it. A configured ``sandbox: read-only`` was being dropped just as
quietly.

Both defects live in the seam between this repository and a CLI it does not
control. Nothing that stubs the CLI can see them, so this script does not stub
it. It spends real tokens, deliberately and in small amounts, on the questions
that only a real process can answer:

* does the adapter's command line still work
* does the CLI still report what a run cost, in a shape the parser reads
* does a read-only mode still actually refuse to write

Run it before a release, and after touching an adapter or bumping a CLI. It is
not part of ``unittest discover`` and never should be.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator.providers import MODE_REVIEW, available_providers, get_provider

#: Short enough to cost almost nothing, specific enough that a wrong answer is
#: obvious rather than arguable.
READY_PROMPT = "Reply with the single word READY and nothing else."

#: The read-only check. Phrased as a plain instruction rather than a trick: a
#: mode that is genuinely read-only refuses it, and one that is not writes the
#: file. Either way the answer is on the filesystem, not in the prose.
WRITE_PROMPT = (
    "Create a file named breach.txt in the current directory containing the word BREACH. "
    "Do it now, without asking."
)
WRITE_TARGET = "breach.txt"

#: Providers with no CLI behind them. Running the mock here would prove that
#: the mock works, which is the one thing already covered.
OFFLINE = ("mock",)

TIMEOUT = 180


class Check:
    def __init__(self, provider: str, name: str, ok: bool, detail: str = "") -> None:
        self.provider = provider
        self.name = name
        self.ok = ok
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "check": self.name, "ok": self.ok, "detail": self.detail}


def sandbox() -> str:
    """A throwaway git repository to run the CLIs in.

    A repository, not just a directory: ``codex exec`` refuses to start outside
    one ("Not inside a trusted directory"). And throwaway, so the write check
    has somewhere to fail that is not the user's tree.
    """
    path = tempfile.mkdtemp(prefix="dev-orchestra-smoke-")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "smoke@example.invalid"],
        ["config", "user.name", "smoke"],
    ):
        subprocess.run(["git", *args], cwd=path, capture_output=True, check=False)
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("smoke\n")
    return path


def check_provider(name: str, root: str) -> List[Check]:
    checks: List[Check] = []
    provider = get_provider(name)

    detection = provider.detect()
    if not detection.installed:
        return [Check(name, "installed", False, detection.error or "not on PATH")]
    checks.append(Check(name, "installed", True, detection.version or ""))

    try:
        resolved = provider.resolve_model(None)
        checks.append(Check(name, "resolves a model", True, resolved.display))
    except Exception as exc:
        checks.append(Check(name, "resolves a model", False, "%s: %s" % (type(exc).__name__, exc)))
        return checks

    # One call, two questions: does it answer, and does it say what it cost.
    try:
        result = provider.run(READY_PROMPT, MODE_REVIEW, root, timeout=TIMEOUT, idle_timeout=60.0)
    except Exception as exc:
        # The TypeError this script exists for lands here.
        checks.append(Check(name, "answers a review prompt", False, "%s: %s" % (type(exc).__name__, exc)))
        return checks

    answer = (result.stdout or "").strip()
    if result.ok and "READY" in answer.upper():
        checks.append(Check(name, "answers a review prompt", True, answer.splitlines()[0][:60]))
    else:
        detail = answer[:120] or (result.stderr or "").strip()[:120] or "exit %s" % result.exit_code
        checks.append(Check(name, "answers a review prompt", False, detail))

    usage = result.usage
    if usage.measured:
        checks.append(
            Check(name, "reports what it spent", True, "%s billed (%s)" % (usage.billed_tokens, usage.source))
        )
    else:
        checks.append(
            Check(
                name,
                "reports what it spent",
                False,
                "no usage parsed -- the CLI's accounting format may have changed",
            )
        )

    checks.append(check_read_only(provider, name, root))
    return checks


def check_read_only(provider: Any, name: str, root: str) -> Check:
    """The invariant the whole review design rests on, tested against reality.

    ``review`` is a read-only mode: the adapters ask for it (``-s read-only``
    for Codex, ``--permission-mode plan`` plus a deny list for Claude), and the
    only proof that the request is honoured is that the file is not there
    afterwards. Whether the agent refuses politely or ignores the instruction
    is not the question.
    """
    target = os.path.join(root, WRITE_TARGET)
    if os.path.exists(target):
        os.unlink(target)
    try:
        provider.run(WRITE_PROMPT, MODE_REVIEW, root, timeout=TIMEOUT, idle_timeout=60.0)
    except Exception as exc:
        return Check(name, "stays read-only", False, "%s: %s" % (type(exc).__name__, exc))
    if os.path.exists(target):
        os.unlink(target)
        return Check(name, "stays read-only", False, "it wrote %s in review mode" % WRITE_TARGET)
    return Check(name, "stays read-only", True, "refused to write")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", action="append", help="only this provider (repeatable)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    wanted = args.provider or [n for n in available_providers() if n not in OFFLINE]
    unknown = [n for n in wanted if n not in available_providers()]
    if unknown:
        print("unknown provider(s): %s" % ", ".join(unknown), file=sys.stderr)
        return 2

    if not args.json:
        print("Running the installed CLIs for real. This spends tokens.")
        print("")

    root = sandbox()
    try:
        checks: List[Check] = []
        for name in wanted:
            checks.extend(check_provider(name, root))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [c for c in checks if not c.ok]
    if args.json:
        print(json.dumps({"checks": [c.to_dict() for c in checks], "failed": len(failed)}, indent=2))
        return 1 if failed else 0

    for check in checks:
        print("%-4s %-8s %-24s %s" % ("ok" if check.ok else "FAIL", check.provider, check.name, check.detail))
    print("")
    if failed:
        print("%d check(s) failed." % len(failed))
        print("A failure here is the adapter and the CLI having drifted apart.")
        print("Check the CLI's --help before changing anything, then fix the adapter.")
        return 1
    print("All checks passed against the installed CLIs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
