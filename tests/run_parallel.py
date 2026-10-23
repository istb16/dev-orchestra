"""Run the suite across worker processes, one test class per task.

``python -m unittest discover -s tests -t tests`` stays the canonical command;
this runs the same tests in parallel for CI and for anyone who wants that
locally. Standard library only, like the rest of the suite::

    python tests/run_parallel.py [-v] [-j N] [--timeout SECONDS] [--serial module.Class ...]
                                 [--split module.Class ...] [--timings PATH]
                                 [--write-timings PATH] [--shard I/N] [-p GLOB ...]

``-p GLOB`` (repeatable) runs only the modules whose file name matches, with
the meaning ``unittest discover -p`` gives it (``test_x.py``, not ``test_x``
or ``tests/test_x.py``); the tree is walked once whatever the number of
patterns. A pattern that matches no module stops the run. It cannot be
combined with ``--shard`` or ``--write-timings``, which are about the whole
suite.

A class named by ``--split`` (or in ``SPLIT_CLASSES``) is handed out one test
method per task instead, so one long class does not decide how long the run
takes. Only classes with no class or module fixtures belong there.

Tasks are handed out longest first, by the seconds recorded for them in
``timings.json`` next to this script (``--timings`` reads another file). A
task with no recorded time is estimated from its test count. The times only
decide the order; a stale file never changes which tests run.

``--shard I/N`` runs part I of N, for CI jobs that each take a part. The parts
are split greedily by those times, the same way in every job, and each run
checks that the N parts together hold every task exactly once. Only this
part's tests count towards "every discovered test ran"; a module that failed
to import is reported in every part. ``--shard 2/3`` run locally runs the same
tests as that CI job.

``--timeout`` is per task, counted from when a worker starts it; the first
task to run past it stops the whole run.

``--write-timings PATH`` records how long each task took, as JSON, once the
run has finished (not after a hang). Record it serially, so the times are not
inflated by tasks competing for the CPU::

    python tests/run_parallel.py -j 1 --write-timings tests/timings.json

Workers are separate processes started with ``spawn``: tests chdir and set
environment variables, which are per process, so threads would collide. Each
class's output is buffered in its worker and printed whole, so nothing
interleaves. The name does not match ``test*.py``, so discover never runs it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import io
import json
import multiprocessing
import os
import sys
import time
import traceback
import unittest
import warnings
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Dict, List, Optional, Set, Tuple

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

#: Classes handed out one test method per task, because each alone takes long
#: enough to set the length of the whole run. Every test in them makes its own
#: temp dir and none has a class or module fixture. ``--split`` adds to it; a
#: class that is also serial is not split.
SPLIT_CLASSES = (
    "test_installers.TestAntigravityInstallers",
    "test_installers.TestClaudeInstallers",
    "test_installers.TestCodexInstallers",
)

JOBS_ENV = "DEV_ORCHESTRA_TEST_JOBS"

#: Where the seconds each task took are kept; ``--write-timings`` makes it.
TIMINGS_FILE = os.path.join(TESTS_DIR, "timings.json")

#: Seconds per test for a task with no recorded time, a little over the
#: suite's mean, so a new task is placed by its size.
ESTIMATE_PER_TEST = 0.25

#: In a worker, where it tells the parent which class it has started, so the
#: timeout can run from that moment rather than from when it was queued.
_started = None


class _Buffer(io.StringIO):
    """What ``TextTestResult`` writes to: a string buffer with ``writeln``."""

    def writeln(self, text: str = "") -> None:
        self.write(text + "\n")


def _run_suite(name: str, suite: unittest.TestSuite, verbosity: int) -> Dict[str, Any]:
    buffer = _Buffer()
    # A string buffer with `writeln`, which is all TextTestResult writes through.
    result = unittest.TextTestResult(buffer, True, verbosity)  # pyright: ignore[reportArgumentType]
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


def run_class(name: str, verbosity: int) -> Dict[str, Any]:
    """One task: load ``module.Class`` (or ``module.Class.method``) by name and run it. Runs in a worker."""
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


def _shard(text: str) -> Tuple[int, int]:
    index, slash, count = text.partition("/")
    if slash and index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count):
        return int(index), int(count)
    raise argparse.ArgumentTypeError("expected I/N with 1 <= I <= N, got %r" % text)


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
        help="seconds one task may run in a worker before it is reported as hung and the run stops",
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
    parser.add_argument(
        "--split",
        action="append",
        default=[],
        metavar="MODULE.CLASS",
        help="hand out each test method of this class as its own task; repeatable",
    )
    parser.add_argument(
        "--timings",
        default=None,
        metavar="PATH",
        help="read each task's recorded seconds from PATH (default: timings.json next to this script)",
    )
    parser.add_argument(
        "--write-timings",
        default=None,
        metavar="PATH",
        help="after a finished run, write each task's seconds to PATH as JSON",
    )
    parser.add_argument(
        "--shard",
        type=_shard,
        default=None,
        metavar="I/N",
        help="run only part I of the suite split N ways by recorded time (1-based)",
    )
    parser.add_argument(
        "-p",
        "--pattern",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "run only the test modules whose file name matches GLOB, as discover -p (test_x.py);"
            " repeatable; not with --shard or --write-timings"
        ),
    )
    args = parser.parse_args(argv)
    # Shards split the whole suite, and timings written from part of it would
    # drop every other task's time.
    if args.pattern and args.shard is not None:
        parser.error("--pattern cannot be used with --shard")
    if args.pattern and args.write_timings is not None:
        parser.error("--pattern cannot be used with --write-timings")
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

    def add(self, outcome: Dict[str, Any]) -> None:
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


def _read_timings(path: str) -> Dict[str, float]:
    """``{task: seconds}`` from a file ``--write-timings`` wrote; ValueError if it is not one."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    timings: Dict[str, float] = {}
    for name, seconds in data.items():
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError("%s: not a number of seconds: %r" % (name, seconds))
        timings[name] = float(seconds)
    return timings


def _task_times(sizes: Dict[str, int], recorded: Dict[str, float]) -> Dict[str, float]:
    """Each task's recorded seconds, or an estimate from its test count."""
    return {
        name: recorded[name] if name in recorded else ESTIMATE_PER_TEST * count
        for name, count in sizes.items()
    }


def _longest_first(times: Dict[str, float]) -> List[str]:
    # Ties by name, so the order is the same every run.
    return sorted(times, key=lambda name: (-times[name], name))


def _partition(times: Dict[str, float], count: int) -> List[List[str]]:
    """Split the tasks into ``count`` shards of about equal time.

    Longest first, each to the shard with the least time so far (the lower
    index on a tie). It depends only on ``times``, so every job computes the
    same split and each part keeps the longest-first order.
    """
    parts: List[List[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for name in _longest_first(times):
        lightest = min(range(count), key=lambda index: (loads[index], index))
        parts[lightest].append(name)
        loads[lightest] += times[name]
    return parts


def _covers(parts: List[List[str]], times: Dict[str, float]) -> bool:
    """Whether the parts hold every task exactly once, and nothing else."""
    names = [name for part in parts for name in part]
    return len(names) == len(set(names)) and set(names) == set(times)


def _write_timings(path: str, timings: Dict[str, float]) -> None:
    rounded = {name: round(seconds, 2) for name, seconds in timings.items()}
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(rounded, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _terminate_workers(executor: concurrent.futures.ProcessPoolExecutor) -> None:
    # A hung worker never returns, and shutdown() cannot stop one.
    for process in list((getattr(executor, "_processes", None) or {}).values()):
        process.terminate()


class _AnyPattern(unittest.TestLoader):
    """A loader that takes a module matching any of ``patterns``, in one walk of the tree.

    ``_match_path`` is the hook unittest leaves for another matching rule; it
    also records which patterns matched, so one that matched nothing can be named.
    """

    def __init__(self, patterns: List[str]) -> None:
        super().__init__()
        self.patterns = patterns
        self.matched: Set[str] = set()

    def _match_path(self, path: str, full_path: str, pattern: str) -> bool:
        hits = [each for each in self.patterns if fnmatch.fnmatch(path, each)]
        self.matched.update(hits)
        return bool(hits)


def _discover_patterns(start_dir: str, patterns: List[str]) -> Tuple[unittest.TestSuite, List[str]]:
    """The tests of the modules any pattern matches, and the patterns that matched no module."""
    loader = _AnyPattern(patterns)
    suite = loader.discover(start_dir, pattern=patterns[0], top_level_dir=start_dir)
    return suite, [each for each in dict.fromkeys(patterns) if each not in loader.matched]


def _pattern_hint(pattern: str) -> str:
    """A reminder of the form, for a pattern that looks like a module name or a path."""
    if pattern.endswith(".py") and "/" not in pattern and "\\" not in pattern:
        return ""
    return " (patterns match file names, such as test_x.py)"


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    verbosity = 2 if args.verbose else 1
    started = time.monotonic()

    if args.pattern:
        suite, unmatched = _discover_patterns(args.start_dir, args.pattern)
        for pattern in unmatched:
            hint = _pattern_hint(pattern)
            sys.stderr.write("run_parallel: no test module matches %s%s\n" % (pattern, hint))
        if unmatched:
            return 2
    else:
        suite = unittest.TestLoader().discover(args.start_dir, top_level_dir=args.start_dir)
    classes: Dict[str, List[str]] = {}
    in_parent: List[unittest.TestCase] = []
    for test in _flatten(suite):
        cls = type(test)
        if cls.__module__.startswith("unittest"):
            # A module that failed to import (or skipped itself) has no class
            # a worker could load by name; report it here and now.
            in_parent.append(test)
        else:
            name = "%s.%s" % (cls.__module__, cls.__qualname__)
            classes.setdefault(name, []).append(test.id())

    # A default missing from what was discovered (a -p selection, or a tree
    # that is not this repository's) is skipped, as SPLIT_CLASSES is below;
    # a class named on the command line must be there.
    defaults = [name for name in SERIAL_CLASSES if name in classes]
    serial = list(dict.fromkeys([*defaults, *args.serial]))
    unknown = [name for name in dict.fromkeys([*serial, *args.split]) if name not in classes]
    if unknown:
        sys.stderr.write("run_parallel: no such test class: %s\n" % ", ".join(unknown))
        return 2
    # The defaults may be missing from a tree that is not this repository's.
    split = {name for name in [*SPLIT_CLASSES, *args.split] if name in classes and name not in serial}

    # Each task, a class or one method of a split class, with its test count.
    # A serial class is a task too: it is in one shard like any other.
    sizes: Dict[str, int] = {}
    for name, ids in classes.items():
        if name in split:
            for test_id in ids:
                sizes[test_id] = sizes.get(test_id, 0) + 1
        else:
            sizes[name] = len(ids)

    # The default file may be missing from a tree that is not this
    # repository's; one named on the command line must be there.
    timings_path = args.timings if args.timings is not None else TIMINGS_FILE
    recorded: Dict[str, float] = {}
    if args.timings is not None or os.path.exists(timings_path):
        try:
            recorded = _read_timings(timings_path)
        except (OSError, ValueError) as error:
            sys.stderr.write("run_parallel: cannot read timings from %s: %s\n" % (timings_path, error))
            return 2
    times = _task_times(sizes, recorded)

    mine = set(times)
    if args.shard is not None:
        index, count = args.shard
        parts = _partition(times, count)
        if not _covers(parts, times):
            sys.stderr.write("run_parallel: the %d shards do not cover every task exactly once\n" % count)
            return 2
        mine = set(parts[index - 1])
        sys.stderr.write(
            "run_parallel: shard %d/%d: %d of %d tasks, %d of %d tests, %d without a recorded time\n"
            % (
                index,
                count,
                len(mine),
                len(times),
                len(in_parent) + sum(sizes[name] for name in mine),
                len(in_parent) + sum(sizes.values()),
                sum(1 for name in mine if name not in recorded),
            )
        )
        sys.stderr.flush()
    # Import failures are in every shard, so no shard can lose one.
    expected = len(in_parent) + sum(sizes[name] for name in mine)
    serial = [name for name in serial if name in mine]
    # Longest first, so the long tasks do not start last.
    pooled = [name for name in _longest_first(times) if name in mine and name not in serial]

    tally = _Tally()
    # Seconds per task that produced a result, for --write-timings.
    timings: Dict[str, float] = {}
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
                    outcome = future.result()
                    timings[name] = outcome["duration"]
                    tally.add(outcome)
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
            outcome = run_class(name, verbosity)
            timings[name] = outcome["duration"]
            tally.add(outcome)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        if template is not None:
            os.environ.pop(helpers.GIT_TEMPLATE_ENV, None)
            helpers.remove_tree(template)

    complete = tally.run == expected
    if not complete:
        sys.stderr.write("\nrun_parallel: discover found %d tests but %d ran\n" % (expected, tally.run))
    if args.write_timings is not None:
        _write_timings(args.write_timings, timings)
    sys.stderr.write(tally.summary(time.monotonic() - started))
    return 0 if tally.ok() and complete else 1


if __name__ == "__main__":
    sys.exit(main())
