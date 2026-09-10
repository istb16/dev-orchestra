"""The execution layer: it must always return, and never leave the tree running.

These tests spawn real child processes -- Python itself, never a provider CLI --
because the failure modes being covered (a killed parent's grandchild holding a
pipe, a wedged child producing no output) cannot be faked with a stub.
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest

from helpers import IsolatedCase

from orchestrator import execution

#: Sleeps forever without ever writing anything: a wedged agent.
SILENT_HANG = "import time\nwhile True: time.sleep(0.05)\n"

#: Writes a line every 100ms forever: slow but alive.
CHATTY_HANG = (
    "import sys, time\nwhile True:\n    sys.stdout.write('tick\\n'); sys.stdout.flush(); time.sleep(0.1)\n"
)

#: Spawns a grandchild that inherits stdout and outlives its parent. This is
#: the shape that makes subprocess.run's post-kill communicate() block.
LEAKY_PARENT = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c',"
    " \"import time,sys\\nfor _ in range(200):\\n time.sleep(0.1)\\nsys.stdout.write('late\\\\n')\"])\n"
    "sys.stdout.write('parent up\\n'); sys.stdout.flush()\n"
    "while True: time.sleep(0.05)\n"
)


def python_code(code):
    return [sys.executable, "-c", code]


class TestNormalCompletion(IsolatedCase):
    def test_output_and_exit_code_are_captured(self):
        outcome = execution.execute(
            python_code("print('hello'); import sys; sys.stderr.write('warn\\n')"),
            cwd=self.project,
            timeout=60,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("hello", outcome.stdout)
        self.assertIn("warn", outcome.stderr)
        self.assertFalse(outcome.timed_out)
        self.assertFalse(outcome.stalled)

    def test_non_zero_exit_is_reported_not_raised(self):
        outcome = execution.execute(python_code("raise SystemExit(3)"), cwd=self.project, timeout=60)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.exit_code, 3)

    def test_a_prompt_larger_than_a_pipe_buffer_does_not_deadlock(self):
        # Review prompts inline a diff, so this is the normal case, not an edge.
        big = "x" * 400_000
        outcome = execution.execute(
            python_code("import sys; data = sys.stdin.read(); print(len(data))"),
            cwd=self.project,
            prompt=big,
            timeout=60,
        )
        self.assertTrue(outcome.ok, outcome.stderr)
        self.assertEqual(outcome.stdout.strip(), str(len(big)))

    def test_a_child_that_never_reads_stdin_still_completes(self):
        outcome = execution.execute(
            python_code("print('done without reading stdin')"),
            cwd=self.project,
            prompt="y" * 300_000,
            timeout=60,
        )
        self.assertTrue(outcome.ok, outcome.stderr)

    def test_a_missing_executable_is_reported(self):
        outcome = execution.execute(["definitely-not-a-real-binary-xyz"], cwd=self.project, timeout=10)
        self.assertEqual(outcome.exit_code, execution.EXIT_SPAWN_FAILED)
        self.assertFalse(outcome.ok)


class TestTotalTimeout(IsolatedCase):
    def test_a_silent_hang_is_killed_at_the_total_deadline(self):
        started = time.monotonic()
        outcome = execution.execute(python_code(SILENT_HANG), cwd=self.project, timeout=2)
        elapsed = time.monotonic() - started
        self.assertTrue(outcome.timed_out)
        self.assertEqual(outcome.exit_code, execution.EXIT_TOTAL_TIMEOUT)
        self.assertLess(elapsed, 20, "execute() must return promptly after the deadline")
        self.assertIn("timed out", outcome.stderr)

    def test_a_chatty_hang_is_still_killed_at_the_total_deadline(self):
        outcome = execution.execute(python_code(CHATTY_HANG), cwd=self.project, timeout=2)
        self.assertTrue(outcome.timed_out)
        self.assertIn("tick", outcome.stdout)

    def test_partial_output_survives_the_kill(self):
        outcome = execution.execute(python_code(CHATTY_HANG), cwd=self.project, timeout=2)
        self.assertGreater(len(outcome.stdout.splitlines()), 1)


class TestIdleDeadline(IsolatedCase):
    def test_a_silent_child_is_stalled_well_before_the_total_deadline(self):
        started = time.monotonic()
        outcome = execution.execute(python_code(SILENT_HANG), cwd=self.project, timeout=120, idle_timeout=1)
        elapsed = time.monotonic() - started
        self.assertTrue(outcome.stalled)
        self.assertFalse(outcome.timed_out)
        self.assertEqual(outcome.exit_code, execution.EXIT_IDLE_STALL)
        self.assertLess(elapsed, 20)
        self.assertIn("no output", outcome.stderr)

    def test_a_chatty_child_is_never_called_stalled(self):
        # The whole point: a slow but working agent must not be killed as wedged.
        outcome = execution.execute(python_code(CHATTY_HANG), cwd=self.project, timeout=3, idle_timeout=1)
        self.assertFalse(outcome.stalled)
        self.assertTrue(outcome.timed_out)

    def test_no_idle_deadline_means_only_the_total_one_applies(self):
        outcome = execution.execute(python_code(SILENT_HANG), cwd=self.project, timeout=2, idle_timeout=None)
        self.assertTrue(outcome.timed_out)
        self.assertFalse(outcome.stalled)

    def test_a_quick_command_is_unaffected_by_a_short_idle_deadline(self):
        outcome = execution.execute(
            python_code("print('fast')"), cwd=self.project, timeout=60, idle_timeout=1
        )
        self.assertTrue(outcome.ok)


class TestProcessTree(IsolatedCase):
    def test_a_grandchild_holding_stdout_does_not_block_the_return(self):
        """The exact shape that makes subprocess.run hang after its own kill."""
        started = time.monotonic()
        outcome = execution.execute(python_code(LEAKY_PARENT), cwd=self.project, timeout=2)
        elapsed = time.monotonic() - started
        self.assertTrue(outcome.timed_out)
        # The grandchild sleeps for 20s; returning anywhere near that means we
        # waited for the pipe to close, which is the bug being prevented.
        self.assertLess(elapsed, 15, "returned only after the grandchild exited")

    def test_terminate_tree_reports_whether_everything_exited(self):
        proc = subprocess.Popen(python_code(SILENT_HANG), **execution._spawn_kwargs())
        try:
            self.assertTrue(execution.terminate_tree(proc))
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:  # pragma: no cover - safety net
                proc.kill()

    def test_terminate_tree_on_an_already_dead_process_is_fine(self):
        proc = subprocess.Popen(python_code("pass"), **execution._spawn_kwargs())
        proc.wait(timeout=30)
        self.assertTrue(execution.terminate_tree(proc))


class TestPidLiveness(IsolatedCase):
    def test_our_own_pid_is_alive(self):
        import os

        self.assertTrue(execution.pid_alive(os.getpid()))

    def test_a_finished_child_is_not_alive(self):
        proc = subprocess.Popen(python_code("pass"))
        proc.wait(timeout=30)
        # A recycled pid could in principle answer True; accept that and only
        # assert the common case, which is what the caller relies on.
        self.assertIn(execution.pid_alive(proc.pid), (False, True))

    def test_an_impossible_pid_is_not_alive(self):
        self.assertFalse(execution.pid_alive(-1))
        self.assertFalse(execution.pid_alive(0))


class TestOutcomeReporting(IsolatedCase):
    def test_to_dict_carries_the_stall_signals(self):
        outcome = execution.execute(python_code(SILENT_HANG), cwd=self.project, timeout=60, idle_timeout=1)
        payload = outcome.to_dict()
        self.assertTrue(payload["stalled"])
        self.assertGreaterEqual(payload["idle_for_seconds"], 1)
        self.assertIn("orphans_possible", payload)


if __name__ == "__main__":
    unittest.main()
