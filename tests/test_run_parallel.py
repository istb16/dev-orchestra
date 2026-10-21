"""The parallel test runner CI depends on, run against suites made to break it.

A runner that under-reports is worse than a slow one: CI turns green and
nobody looks. So each case here writes a small tests directory with one way
of failing in it, runs ``run_parallel.py`` on it in a subprocess (the timeout
path ends in ``os._exit``, which would take this process with it) and checks
the exit code and the summary line.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import run_parallel

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


class TestSplitClasses(RunnerCase):
    def test_a_split_class_runs_every_method_and_counts_them(self):
        self.module("test_passing", PASSING)
        code, stderr = self.run_runner("-v", "--split", "test_passing.TestPasses")
        self.assertEqual(code, 0, stderr)
        self.assertSummary(stderr, 2, "OK")
        self.assertIn("test_one (test_passing.TestPasses", stderr)
        self.assertIn("test_two (test_passing.TestPasses", stderr)

    def test_a_failing_method_fails_the_run_and_is_named(self):
        self.module("test_passing", PASSING)
        self.module("test_failing", FAILING)
        code, stderr = self.run_runner("--split", "test_failing.TestFails")
        self.assertEqual(code, 1, stderr)
        self.assertSummary(stderr, 3, "FAILED (failures=1)")
        self.assertIn("FAIL: test_fails (test_failing.TestFails", stderr)

    def test_a_killed_worker_names_the_method(self):
        self.module("test_killer", KILLS_ITS_WORKER)
        code, stderr = self.run_runner("-j", "1", "--split", "test_killer.TestKillsItsWorker")
        self.assertEqual(code, 1, stderr)
        self.assertIn("ERROR: test_killer.TestKillsItsWorker.test_exits\n", stderr)
        self.assertIn("the process pool broke", stderr)

    def test_a_hung_method_is_named(self):
        self.module("test_passing", PASSING)
        self.module("test_hangs", HANGS)
        code, stderr = self.run_runner("--timeout", "3", "--split", "test_hangs.TestHangs")
        self.assertEqual(code, 1, stderr)
        self.assertIn("ERROR: test_hangs.TestHangs.test_sleeps\n", stderr)
        self.assertIn("still running after 3s in its worker (hung)", stderr)

    def test_a_class_that_is_also_serial_still_runs_once(self):
        self.module("test_passing", PASSING)
        self.module("test_failing", FAILING)
        code, stderr = self.run_runner(
            "--serial", "test_failing.TestFails", "--split", "test_failing.TestFails"
        )
        self.assertEqual(code, 1, stderr)
        self.assertSummary(stderr, 3, "FAILED (failures=1)")

    def test_an_unknown_class_is_refused(self):
        self.module("test_passing", PASSING)
        code, stderr = self.run_runner("--split", "test_passing.TestNoSuchClass")
        self.assertEqual(code, 2, stderr)
        self.assertIn("no such test class: test_passing.TestNoSuchClass", stderr)


class TestWriteTimings(RunnerCase):
    def setUp(self) -> None:
        super().setUp()
        output_dir = tempfile.mkdtemp(prefix="devorchestra-timings-")
        self.addCleanup(shutil.rmtree, output_dir, ignore_errors=True)
        self.path = os.path.join(output_dir, "timings.json")

    def test_writes_one_key_per_task(self):
        self.module("test_passing", PASSING)
        self.module("test_failing", FAILING)
        self.module("test_slow", slow("TestSlow", 0.2))
        code, stderr = self.run_runner(
            "--split",
            "test_passing.TestPasses",
            "--serial",
            "test_slow.TestSlow",
            "--write-timings",
            self.path,
        )
        self.assertEqual(code, 1, stderr)
        with open(self.path, encoding="utf-8") as handle:
            timings = json.load(handle)
        self.assertEqual(
            sorted(timings),
            [
                "test_failing.TestFails",
                "test_passing.TestPasses.test_one",
                "test_passing.TestPasses.test_two",
                "test_slow.TestSlow",
            ],
        )
        for seconds in timings.values():
            self.assertIsInstance(seconds, (int, float))
            self.assertEqual(seconds, round(seconds, 2))
        self.assertGreaterEqual(timings["test_slow.TestSlow"], 0.2)

    def test_a_hung_run_writes_nothing(self):
        self.module("test_hangs", HANGS)
        code, stderr = self.run_runner("--timeout", "3", "--write-timings", self.path)
        self.assertEqual(code, 1, stderr)
        self.assertFalse(os.path.exists(self.path))


class TestTheOrder(unittest.TestCase):
    """In process: which task is handed out first."""

    def test_a_recorded_time_outweighs_a_test_count(self):
        times = run_parallel._task_times({"a.Many": 10, "b.Few": 1}, {"b.Few": 30.0})
        self.assertEqual(run_parallel._longest_first(times), ["b.Few", "a.Many"])

    def test_a_task_without_a_recorded_time_is_estimated_from_its_test_count(self):
        times = run_parallel._task_times({"a.Known": 4, "b.New": 4}, {"a.Known": 0.5})
        self.assertEqual(times, {"a.Known": 0.5, "b.New": 4 * run_parallel.ESTIMATE_PER_TEST})

    def test_times_for_tasks_that_are_gone_are_ignored(self):
        times = run_parallel._task_times({"a.Here": 2}, {"a.Gone": 99.0})
        self.assertEqual(list(times), ["a.Here"])

    def test_ties_go_by_name(self):
        times = {"c.C": 1.0, "a.A": 1.0, "b.B": 2.0}
        self.assertEqual(run_parallel._longest_first(times), ["b.B", "a.A", "c.C"])


class TestTimingsFile(RunnerCase):
    def setUp(self) -> None:
        super().setUp()
        output_dir = tempfile.mkdtemp(prefix="devorchestra-timings-")
        self.addCleanup(shutil.rmtree, output_dir, ignore_errors=True)
        self.path = os.path.join(output_dir, "timings.json")

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def test_a_stale_file_still_runs_every_test(self):
        self.module("test_passing", PASSING)
        self.module("test_also_passing", PASSING.replace("TestPasses", "TestAlsoPasses"))
        self.write(json.dumps({"test_passing.TestPasses": 5.0, "test_gone.TestGone": 50.0}))
        code, stderr = self.run_runner("--timings", self.path)
        self.assertEqual(code, 0, stderr)
        self.assertSummary(stderr, 4, "OK")

    def test_a_named_file_that_is_missing_is_refused(self):
        self.module("test_passing", PASSING)
        code, stderr = self.run_runner("--timings", self.path)
        self.assertEqual(code, 2, stderr)
        self.assertIn("cannot read timings from", stderr)

    def test_a_file_that_is_not_timings_is_refused(self):
        self.module("test_passing", PASSING)
        for text in ("not json", "[1, 2]", '{"test_passing.TestPasses": "slow"}'):
            with self.subTest(text=text):
                self.write(text)
                code, stderr = self.run_runner("--timings", self.path)
                self.assertEqual(code, 2, stderr)
                self.assertIn("cannot read timings from", stderr)


def ran_lines(stderr: str):
    """The ``-v`` line of each test that ran, e.g. ``test_one (m.C.test_one) ... ok``."""
    return {line for line in stderr.splitlines() if " ... " in line}


LPT_TIMES = {"a.A": 5.0, "b.B": 4.0, "c.C": 3.0, "d.D": 3.0, "e.E": 2.0}


class TestThePartition(unittest.TestCase):
    """In process: how the tasks are split into shards."""

    def test_each_task_goes_to_the_lightest_shard_longest_first(self):
        parts = run_parallel._partition(LPT_TIMES, 2)
        self.assertEqual(parts, [["a.A", "d.D"], ["b.B", "c.C", "e.E"]])
        self.assertEqual([sum(LPT_TIMES[name] for name in part) for part in parts], [8.0, 9.0])

    def test_the_parts_cover_every_task_once(self):
        for count in (1, 2, 3, 7):
            with self.subTest(count=count):
                parts = run_parallel._partition(LPT_TIMES, count)
                self.assertEqual(len(parts), count)
                self.assertTrue(run_parallel._covers(parts, LPT_TIMES))
                self.assertEqual(sorted(name for part in parts for name in part), sorted(LPT_TIMES))

    def test_the_same_times_give_the_same_parts(self):
        reordered = dict(reversed(list(LPT_TIMES.items())))
        self.assertEqual(run_parallel._partition(LPT_TIMES, 3), run_parallel._partition(reordered, 3))

    def test_a_task_twice_or_missing_is_not_a_cover(self):
        self.assertFalse(run_parallel._covers([["a.A", "b.B"], ["a.A"]], {"a.A": 1.0, "b.B": 1.0}))
        self.assertFalse(run_parallel._covers([["a.A"], []], {"a.A": 1.0, "b.B": 1.0}))
        self.assertFalse(run_parallel._covers([["a.A", "c.C"], ["b.B"]], {"a.A": 1.0, "b.B": 1.0}))


class TestShards(RunnerCase):
    def three_modules(self) -> None:
        self.module("test_passing", PASSING)
        self.module("test_also_passing", PASSING.replace("TestPasses", "TestAlsoPasses"))
        self.module("test_third", PASSING.replace("TestPasses", "TestThird"))

    def test_the_shards_run_every_test_once_between_them(self):
        self.three_modules()
        code, everything = self.run_runner("-v")
        self.assertEqual(code, 0, everything)
        self.assertSummary(everything, 6, "OK")
        first_code, first = self.run_runner("-v", "--shard", "1/2")
        second_code, second = self.run_runner("-v", "--shard", "2/2")
        self.assertEqual(first_code, 0, first)
        self.assertEqual(second_code, 0, second)
        self.assertEqual(ran_lines(first) & ran_lines(second), set())
        self.assertEqual(ran_lines(first) | ran_lines(second), ran_lines(everything))
        self.assertEqual(len(ran_lines(everything)), 6)
        ran = [len(ran_lines(stderr)) for stderr in (first, second)]
        self.assertSummary(first, ran[0], "OK")
        self.assertSummary(second, ran[1], "OK")
        self.assertEqual(sum(ran), 6)

    def test_a_shard_says_what_it_holds(self):
        self.three_modules()
        code, stderr = self.run_runner("--shard", "2/2")
        self.assertEqual(code, 0, stderr)
        self.assertIn(
            "run_parallel: shard 2/2: 1 of 3 tasks, 2 of 6 tests, 1 without a recorded time\n", stderr
        )

    def test_a_heavy_class_has_a_shard_to_itself(self):
        self.three_modules()
        self.module("test_heavy", PASSING.replace("TestPasses", "TestHeavy"))
        timings_dir = tempfile.mkdtemp(prefix="devorchestra-timings-")
        self.addCleanup(shutil.rmtree, timings_dir, ignore_errors=True)
        path = os.path.join(timings_dir, "timings.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"test_heavy.TestHeavy": 100.0}, handle)
        code, stderr = self.run_runner("-v", "--timings", path, "--shard", "1/2")
        self.assertEqual(code, 0, stderr)
        self.assertSummary(stderr, 2, "OK")
        self.assertEqual({"test_heavy.TestHeavy" in line for line in ran_lines(stderr)}, {True})
        code, stderr = self.run_runner("-v", "--timings", path, "--shard", "2/2")
        self.assertEqual(code, 0, stderr)
        self.assertSummary(stderr, 6, "OK")
        self.assertNotIn("test_heavy.TestHeavy", stderr)

    def test_the_count_check_is_against_this_shards_tests(self):
        self.module("test_passing", PASSING)
        self.module("test_shrinks", FEWER_IN_A_WORKER)
        outcomes = [self.run_runner("--shard", "%d/2" % index) for index in (1, 2)]
        codes = sorted(code for code, _ in outcomes)
        self.assertEqual(codes, [0, 1], outcomes)
        for code, stderr in outcomes:
            if code == 1:
                self.assertIn("discover found 2 tests but 1 ran", stderr)
            else:
                self.assertSummary(stderr, 2, "OK")

    def test_an_import_failure_is_reported_in_every_shard(self):
        self.module("test_passing", PASSING)
        self.module("test_also_passing", PASSING.replace("TestPasses", "TestAlsoPasses"))
        self.module("test_broken", BROKEN_IMPORT)
        for index in (1, 2):
            with self.subTest(shard=index):
                code, stderr = self.run_runner("--shard", "%d/2" % index)
                self.assertEqual(code, 1, stderr)
                self.assertSummary(stderr, 3, "FAILED (errors=1)")
                self.assertIn("test_broken", stderr)

    def test_a_serial_class_runs_in_one_shard_only(self):
        self.module("test_passing", PASSING)
        self.module("test_failing", FAILING)
        outcomes = [
            self.run_runner("--serial", "test_failing.TestFails", "--shard", "%d/2" % index)
            for index in (1, 2)
        ]
        self.assertEqual(sorted(code for code, _ in outcomes), [0, 1], outcomes)
        self.assertEqual(sum(stderr.count("FAIL: test_fails") for _, stderr in outcomes), 1)

    def test_a_shard_that_is_not_i_of_n_is_refused(self):
        self.module("test_passing", PASSING)
        for shard in ("0/2", "3/2", "x/y", "1", "1/0"):
            with self.subTest(shard=shard):
                code, stderr = self.run_runner("--shard", shard)
                self.assertEqual(code, 2, stderr)
                self.assertIn("--shard", stderr)


if __name__ == "__main__":
    unittest.main()
