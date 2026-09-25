"""The parallel test runner CI depends on, run against suites made to break it.

A runner that under-reports is worse than a slow one: CI turns green and
nobody looks. So each case here writes a small tests directory with one way
of failing in it, runs ``run_parallel.py`` on it in a subprocess (the timeout
path ends in ``os._exit``, which would take this process with it) and checks
the exit code and the summary line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_parallel.py")

PASSING = """\
import unittest


class TestPasses(unittest.TestCase):
    def test_one(self):
        pass

    def test_two(self):
        pass
"""

FAILING = """\
import unittest


class TestFails(unittest.TestCase):
    def test_fails(self):
        self.fail("on purpose")
"""

BROKEN_IMPORT = """\
import unittest

raise ImportError("on purpose")
"""

KILLS_ITS_WORKER = """\
import os
import unittest


class TestKillsItsWorker(unittest.TestCase):
    def test_exits(self):
        os._exit(3)
"""

HANGS = """\
import time
import unittest


class TestHangs(unittest.TestCase):
    def test_sleeps(self):
        time.sleep(120)
"""

# One test in the parent, which discovers, and two in a worker, which runs it:
# the count check has to notice the difference even though everything passes.
FEWER_IN_A_WORKER = """\
import multiprocessing
import unittest


class TestShrinks(unittest.TestCase):
    def test_always(self):
        pass

    if multiprocessing.parent_process() is None:

        def test_only_where_discovered(self):
            pass
"""


def slow(name: str, seconds: float) -> str:
    return textwrap.dedent(
        """\
        import time
        import unittest


        class %s(unittest.TestCase):
            def test_sleeps(self):
                time.sleep(%r)
        """
        % (name, seconds)
    )


class RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.start_dir = tempfile.mkdtemp(prefix="devorchestra-runner-")
        self.addCleanup(shutil.rmtree, self.start_dir, ignore_errors=True)

    def module(self, name: str, source: str) -> None:
        path = os.path.join(self.start_dir, name + ".py")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(source)

    def run_runner(self, *argv: str):
        completed = subprocess.run(
            [sys.executable, RUNNER, "--start-dir", self.start_dir, "-j", "2", *argv],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return completed.returncode, completed.stderr

    def assertSummary(self, stderr: str, ran: int, verdict: str) -> None:
        self.assertIn("\nRan %d test%s in " % (ran, "" if ran == 1 else "s"), stderr)
        self.assertIn("\n%s\n" % verdict, stderr, stderr)


class TestAPassingSuite(RunnerCase):
    def test_passes_with_every_test_counted(self):
        self.module("test_passing", PASSING)
        self.module("test_also_passing", PASSING.replace("TestPasses", "TestAlsoPasses"))
        code, stderr = self.run_runner()
        self.assertEqual(code, 0, stderr)
        self.assertSummary(stderr, 4, "OK")


class TestAFailingClass(RunnerCase):
    def test_fails_the_run(self):
        self.module("test_passing", PASSING)
        self.module("test_failing", FAILING)
        code, stderr = self.run_runner()
        self.assertEqual(code, 1, stderr)
        self.assertSummary(stderr, 3, "FAILED (failures=1)")
        self.assertIn("on purpose", stderr)


class TestAModuleThatDoesNotImport(RunnerCase):
    def test_is_an_error_not_a_silent_skip(self):
        self.module("test_passing", PASSING)
        self.module("test_broken", BROKEN_IMPORT)
        code, stderr = self.run_runner()
        self.assertEqual(code, 1, stderr)
        self.assertSummary(stderr, 3, "FAILED (errors=1)")
        self.assertIn("test_broken", stderr)


class TestAKilledWorker(RunnerCase):
    def test_is_reported_by_class(self):
        self.module("test_killer", KILLS_ITS_WORKER)
        code, stderr = self.run_runner("-j", "1")
        self.assertEqual(code, 1, stderr)
        self.assertIn("ERROR: test_killer.TestKillsItsWorker", stderr)
        self.assertIn("the process pool broke", stderr)
        self.assertIn("\nFAILED (errors=", stderr)


class TestTheCountCheck(RunnerCase):
    def test_fewer_tests_than_discovered_fails_an_otherwise_green_run(self):
        self.module("test_shrinks", FEWER_IN_A_WORKER)
        code, stderr = self.run_runner()
        self.assertEqual(code, 1, stderr)
        self.assertIn("discover found 2 tests but 1 ran", stderr)


class TestTheTimeout(RunnerCase):
    def test_a_hung_class_is_named_and_stops_the_run(self):
        self.module("test_passing", PASSING)
        self.module("test_hangs", HANGS)
        code, stderr = self.run_runner("--timeout", "3")
        self.assertEqual(code, 1, stderr)
        self.assertIn("ERROR: test_hangs.TestHangs", stderr)
        self.assertIn("still running after 3s in its worker (hung)", stderr)
        self.assertNotIn("ERROR: test_passing.TestPasses", stderr)

    def test_counts_per_class_not_for_the_whole_run(self):
        """One worker, two classes of 2s each: the run takes over 3s, and
        neither class does."""
        self.module("test_slow_a", slow("TestSlowA", 2))
        self.module("test_slow_b", slow("TestSlowB", 2))
        code, stderr = self.run_runner("-j", "1", "--timeout", "3")
        self.assertEqual(code, 0, stderr)
        self.assertSummary(stderr, 2, "OK")


class TestSerialClasses(RunnerCase):
    def test_a_named_class_runs_and_is_counted(self):
        self.module("test_passing", PASSING)
        self.module("test_failing", FAILING)
        code, stderr = self.run_runner("--serial", "test_failing.TestFails")
        self.assertEqual(code, 1, stderr)
        self.assertSummary(stderr, 3, "FAILED (failures=1)")

    def test_an_unknown_class_is_refused(self):
        self.module("test_passing", PASSING)
        code, stderr = self.run_runner("--serial", "test_passing.TestNoSuchClass")
        self.assertEqual(code, 2, stderr)
        self.assertIn("no such test class: test_passing.TestNoSuchClass", stderr)


if __name__ == "__main__":
    unittest.main()
