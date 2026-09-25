"""Run the suite across worker processes, one test class per task.

``python -m unittest discover -s tests -t tests`` stays the canonical command;
this runs the same tests in parallel for CI and for anyone who wants that
locally. Standard library only, like the rest of the suite::

    python tests/run_parallel.py [-v] [-j N] [--timeout SECONDS] [--serial module.Class ...]

``--timeout`` is per class, counted from when a worker starts it; the first
class to run past it stops the whole run.

Workers are separate processes started with ``spawn``: tests chdir and set
environment variables, which are per process, so threads would collide. Each
class's output is buffered in its worker and printed whole, so nothing
interleaves. The name does not match ``test*.py``, so discover never runs it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import multiprocessing
import os
import sys
import time
import traceback
import unittest
import warnings
from concurrent.futures.process import BrokenProcessPool
from typing import Dict, List, Optional

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

# Before anything from `orchestrator`, in the parent and in every worker (which
# re-imports this module); see the note at the top of helpers.py.
import helpers  # noqa: E402

#: Classes to run in this process after the pool has finished, with nothing
#: competing for the CPU -- the fallback for a class that turns out not to
#: tolerate running alongside others. ``--serial`` adds to it.
SERIAL_CLASSES = ()

JOBS_ENV = "DEV_ORCHESTRA_TEST_JOBS"

#: In a worker, where it tells the parent which class it has started, so the
#: timeout can run from that moment rather than from when it was queued.
_started = None


class _Buffer(io.StringIO):
    """What ``TextTestResult`` writes to: a string buffer with ``writeln``."""

    def writeln(self, text: str = "") -> None:
        self.write(text + "\n")


def _run_suite(name: str, suite: unittest.TestSuite, verbosity: int) -> Dict[str, object]:
    buffer = _Buffer()
    result = unittest.TextTestResult(buffer, True, verbosity)
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buffer
    started = time.monotonic()
    try:
        # What TextTestRunner does when unittest is run without -W.
        with warnings.catch_warnings():
            if not sys.warnoptions:
                warnings.simplefilter("default")
            result.startTestRun()
            try:
                suite(result)
            finally:
                result.stopTestRun()
        result.printErrors()
    finally:
        sys.stdout, sys.stderr = saved
    return {
        "name": name,
        "text": buffer.getvalue(),
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expectedFailures": len(result.expectedFailures),
        "unexpectedSuccesses": len(result.unexpectedSuccesses),
        "duration": time.monotonic() - started,
    }


def _init_worker(start_dir: str, started) -> None:
    global _started
    if start_dir not in sys.path:
        sys.path.insert(0, start_dir)
    _started = started


def run_class(name: str, verbosity: int) -> Dict[str, object]:
    """One task: load ``module.Class`` by name and run it. Runs in a worker."""
    if _started is not None:
        _started.put(name)
    return _run_suite(name, unittest.defaultTestLoader.loadTestsFromName(name), verbosity)


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _default_jobs() -> int:
    configured = os.environ.get(JOBS_ENV)
    if configured:
        return int(configured)
    return min(os.cpu_count() or 1, 4)


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the test suite in parallel, one class per task.")
    parser.add_argument("-v", "--verbose", action="store_true", help="unittest's -v, per test")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="worker processes (default: $%s, else the CPU count up to 4)" % JOBS_ENV,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="seconds one class may run in a worker before it is reported as hung and the run stops",
    )
    parser.add_argument(
        "--start-dir",
        default=TESTS_DIR,
        help="directory to discover tests in (default: the one this script is in)",
    )
    parser.add_argument(
        "--serial",
        action="append",
        default=[],
        metavar="MODULE.CLASS",
        help="run this class in this process after the pool, alone; repeatable",
    )
    args = parser.parse_args(argv)
    if args.jobs is None:
        args.jobs = _default_jobs()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    args.start_dir = os.path.abspath(args.start_dir)
    return args


class _Tally:
    def __init__(self) -> None:
        self.run = 0
        self.failures = 0
        self.errors = 0
        self.skipped = 0
        self.expected_failures = 0
        self.unexpected_successes = 0

    def add(self, outcome: Dict[str, object]) -> None:
        sys.stderr.write(str(outcome["text"]))
        sys.stderr.flush()
        self.run += int(outcome["testsRun"])
        self.failures += int(outcome["failures"])
        self.errors += int(outcome["errors"])
        self.skipped += int(outcome["skipped"])
        self.expected_failures += int(outcome["expectedFailures"])
        self.unexpected_successes += int(outcome["unexpectedSuccesses"])

    def error(self, name: str, message: str) -> None:
        """A class that produced no result at all: its worker died, or loading it did."""
        sys.stderr.write("%s\nERROR: %s\n%s\n%s\n" % ("=" * 70, name, "-" * 70, message.rstrip()))
        sys.stderr.flush()
        self.errors += 1

    def summary(self, elapsed: float) -> str:
        infos = []
        for label, count in (
            ("failures", self.failures),
            ("errors", self.errors),
            ("skipped", self.skipped),
            ("expected failures", self.expected_failures),
            ("unexpected successes", self.unexpected_successes),
        ):
            if count:
                infos.append("%s=%d" % (label, count))
        verdict = "OK" if self.ok() else "FAILED"
        if infos:
            verdict += " (%s)" % ", ".join(infos)
        return "%s\nRan %d test%s in %.3fs\n\n%s\n" % (
            "-" * 70,
            self.run,
            "" if self.run == 1 else "s",
            elapsed,
            verdict,
        )

    def ok(self) -> bool:
        return not (self.failures or self.errors or self.unexpected_successes)


def _terminate_workers(executor: concurrent.futures.ProcessPoolExecutor) -> None:
    # A hung worker never returns, and shutdown() cannot stop one.
    for process in list((getattr(executor, "_processes", None) or {}).values()):
        process.terminate()


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    verbosity = 2 if args.verbose else 1
    started = time.monotonic()

    suite = unittest.TestLoader().discover(args.start_dir, top_level_dir=args.start_dir)
    sizes: Dict[str, int] = {}
    in_parent: List[unittest.TestCase] = []
    for test in _flatten(suite):
        cls = type(test)
        if cls.__module__.startswith("unittest"):
            # A module that failed to import (or skipped itself) has no class
            # a worker could load by name; report it here and now.
            in_parent.append(test)
        else:
            name = "%s.%s" % (cls.__module__, cls.__qualname__)
            sizes[name] = sizes.get(name, 0) + 1
    expected = len(in_parent) + sum(sizes.values())

    serial = list(dict.fromkeys([*SERIAL_CLASSES, *args.serial]))
    unknown = [name for name in serial if name not in sizes]
    if unknown:
        sys.stderr.write("run_parallel: no such test class: %s\n" % ", ".join(unknown))
        return 2
    # Largest first, so the long classes do not start last; ties by name so
    # the order is the same every run.
    pooled = sorted((name for name in sizes if name not in serial), key=lambda name: (-sizes[name], name))

    tally = _Tally()
    if in_parent:
        tally.add(_run_suite("discovery", unittest.TestSuite(in_parent), verbosity))

    # One git template for every worker, handed over through the environment
    # (spawned workers inherit it) and removed here, where it was made.
    template = None
    if helpers.has_git():
        template = helpers.create_git_template()
        os.environ[helpers.GIT_TEMPLATE_ENV] = template

    context = multiprocessing.get_context("spawn")
    # Synchronous puts: a message is in the pipe before the class starts, so
    # a class that hangs straight away still gets its clock.
    started_queue = context.SimpleQueue()
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=args.jobs,
        mp_context=context,
        initializer=_init_worker,
        initargs=(args.start_dir, started_queue),
    )
    broken = False
    try:
        futures = {executor.submit(run_class, name, verbosity): name for name in pooled}
        # When each class was seen to start in a worker. A future's own
        # "running" state is set when it is queued to the pool, which can be
        # well before a worker takes it.
        running_since: Dict[str, float] = {}
        pending = set(futures)
        hung: List[str] = []
        poll = min(0.5, args.timeout)
        while pending and not hung:
            done, pending = concurrent.futures.wait(
                pending, timeout=poll, return_when=concurrent.futures.FIRST_COMPLETED
            )
            now = time.monotonic()
            while not started_queue.empty():
                running_since.setdefault(started_queue.get(), now)
            for future in sorted(done, key=futures.__getitem__):
                name = futures[future]
                try:
                    tally.add(future.result())
                except BrokenProcessPool:
                    # The pool fails every unfinished task at once, so this
                    # names the class that killed its worker and all the ones
                    # that never got to finish because of it.
                    broken = True
                    tally.error(name, "worker died before this class finished (the process pool broke)")
                except Exception:
                    tally.error(name, traceback.format_exc())
            hung = sorted(
                futures[future]
                for future in pending
                if now - running_since.get(futures[future], now) > args.timeout
            )
        if hung:
            for name in hung:
                tally.error(name, "still running after %gs in its worker (hung)" % args.timeout)
            stopped = "not finished: the run stopped because %s hung" % ", ".join(hung)
            for name in sorted(futures[future] for future in pending if futures[future] not in hung):
                tally.error(name, stopped)
            sys.stderr.write(tally.summary(time.monotonic() - started))
            sys.stderr.flush()
            _terminate_workers(executor)
            if template is not None:
                helpers.remove_tree(template)
            # Waiting for the pool would wait for the hung class.
            os._exit(1)
        executor.shutdown(wait=not broken, cancel_futures=True)

        for name in serial:
            tally.add(run_class(name, verbosity))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        if template is not None:
            os.environ.pop(helpers.GIT_TEMPLATE_ENV, None)
            helpers.remove_tree(template)

    complete = tally.run == expected
    if not complete:
        sys.stderr.write("\nrun_parallel: discover found %d tests but %d ran\n" % (expected, tally.run))
    sys.stderr.write(tally.summary(time.monotonic() - started))
    return 0 if tally.ok() and complete else 1


if __name__ == "__main__":
    sys.exit(main())
