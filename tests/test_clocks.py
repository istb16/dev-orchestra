"""The awake clock and the sleep it lets a run's charge leave out.

No test here sleeps a machine. A fake awake clock stands in for one that
stopped while the machine was suspended; the one test of the real clock only
checks that it runs at the rate ``time.monotonic()`` does while awake.
"""

from __future__ import annotations

import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import IsolatedCase

from orchestrator import clocks


class FakeAwake:
    """Reads ``start`` on the first call and ``start + awake`` after that."""

    def __init__(self, awake: float, start: float = 1000.0) -> None:
        self.values = [start, start + awake]

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def raising() -> float:
    raise OSError("clock gone")


class TestStopwatch(IsolatedCase):
    def read(self, awake_clock, duration: float) -> float:
        watch = clocks.Stopwatch(awake_clock)
        watch.start()
        return watch.read(duration)

    def test_an_awake_clock_lagging_the_duration_is_the_sleep(self):
        self.assertAlmostEqual(self.read(FakeAwake(awake=300.0), 600.0), 300.0)

    def test_a_lag_under_a_second_is_not_a_sleep(self):
        self.assertEqual(self.read(FakeAwake(awake=599.5), 600.0), 0.0)

    def test_an_awake_clock_ahead_of_the_duration_is_no_sleep(self):
        self.assertEqual(self.read(FakeAwake(awake=601.0), 600.0), 0.0)

    def test_an_awake_clock_that_went_backwards_gives_no_sleep(self):
        # Read as all sleep, it would charge the run nothing: the budget would
        # fail open on a clock it cannot trust.
        self.assertEqual(self.read(FakeAwake(awake=-50.0), 600.0), 0.0)

    def test_a_clock_raising_at_start_gives_no_sleep(self):
        self.assertEqual(self.read(raising, 600.0), 0.0)

    def test_a_clock_raising_at_read_gives_no_sleep(self):
        calls = []

        def fails_later() -> float:
            calls.append(1)
            if len(calls) > 1:
                raise OSError("clock gone")
            return 1000.0

        self.assertEqual(self.read(fails_later, 600.0), 0.0)

    def test_no_awake_clock_gives_no_sleep(self):
        with mock.patch.object(clocks, "awake_clock", return_value=None):
            self.assertEqual(self.read(None, 600.0), 0.0)


class TestAwakeClock(IsolatedCase):
    def test_there_is_one_on_windows_and_none_elsewhere(self):
        if sys.platform == "win32":
            self.assertIsNotNone(clocks.awake_clock())
        else:
            self.assertIsNone(clocks.awake_clock())

    @unittest.skipUnless(sys.platform == "win32", "the awake clock exists on Windows only")
    def test_it_runs_at_the_rate_of_the_monotonic_clock(self):
        # Catches a wrong unit or declaration: either would undercharge every
        # Windows run without anything else noticing.
        awake = clocks.awake_clock()
        assert awake is not None
        awake_started, started = awake(), time.monotonic()
        time.sleep(0.2)
        awake_delta, delta = awake() - awake_started, time.monotonic() - started
        self.assertAlmostEqual(awake_delta, delta, delta=0.1)


class TestSuspendedOfAnOutcome(IsolatedCase):
    """What the base charges from an ``execute()`` outcome, or a stand-in for one."""

    def of(self, **fields):
        from orchestrator.providers import base

        return base._suspended_of(SimpleNamespace(**fields))

    def test_a_stand_in_without_the_field_measured_no_sleep(self):
        self.assertEqual(self.of(duration=600.0), 0.0)

    def test_a_value_that_is_not_finite_is_no_sleep(self):
        for value in (float("nan"), float("inf"), "abc"):
            self.assertEqual(self.of(duration=600.0, suspended=value), 0.0)

    def test_the_sleep_is_kept_within_the_duration(self):
        self.assertEqual(self.of(duration=600.0, suspended=900.0), 600.0)
        self.assertEqual(self.of(duration=600.0, suspended=-5.0), 0.0)
        self.assertEqual(self.of(duration=600.0, suspended=120.0), 120.0)


if __name__ == "__main__":
    unittest.main()
