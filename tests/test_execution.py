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
from unittest import mock

from helpers import IsolatedCase

from orchestrator import clocks, execution

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

    def test_kill_tree_by_pid_returns_once_the_process_is_gone(self):
        proc = subprocess.Popen(python_code(SILENT_HANG), **execution.spawn_kwargs())
        try:
            self.assertTrue(execution.kill_tree(proc.pid))
            self.assertFalse(execution.pid_alive(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)

    def test_kill_tree_stops_a_detached_worker_and_its_child(self):
        """The real shape: the worker is not our child, so nothing here can
        reap it, and its own child is reached only through the tree."""
        import os

        pids_file = os.path.join(self.project, "pids.txt")
        launcher = (
            "import os, subprocess, sys\n"
            "kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'"
            " else {'start_new_session': True}\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],"
            " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)\n"
        )
        worker = (
            "import os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', %r])\n"
            "with open(sys.argv[1] + '.tmp', 'w') as f: f.write('%%d %%d' %% (os.getpid(), child.pid))\n"
            "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "while True: time.sleep(0.05)\n"
        ) % SILENT_HANG
        subprocess.run([*python_code(launcher), worker, pids_file], check=True, timeout=30)
        deadline = time.monotonic() + 30
        while not os.path.exists(pids_file):
            self.assertLess(time.monotonic(), deadline, "the worker never started")
            time.sleep(0.05)
        with open(pids_file) as f:
            worker_pid, grandchild_pid = (int(part) for part in f.read().split())
        try:
            self.assertTrue(execution.kill_tree(worker_pid))
            self.assertFalse(execution.pid_alive(worker_pid))
            deadline = time.monotonic() + 10
            while execution.pid_alive(grandchild_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(execution.pid_alive(grandchild_pid))
        finally:
            for pid in (worker_pid, grandchild_pid):  # pragma: no cover - safety net
                if execution.pid_alive(pid):
                    try:
                        os.kill(pid, 9 if os.name != "nt" else 15)
                    except OSError:
                        pass

    def test_kill_tree_on_an_already_dead_pid_is_fine(self):
        proc = subprocess.Popen(python_code("pass"), **execution.spawn_kwargs())
        proc.wait(timeout=30)
        self.assertTrue(execution.kill_tree(proc.pid))

    @unittest.skipIf(execution.IS_WINDOWS, "process groups are POSIX")
    def test_kill_tree_leaves_a_pid_that_does_not_lead_its_group_alone(self):
        """A recorded pid that is not a group leader is not a worker we spawned,
        most likely a reused pid: nothing is signalled and it is not reported gone."""
        sent = []
        with (
            mock.patch.object(execution, "pid_alive", lambda pid: True),
            mock.patch.object(execution.os, "getpgid", lambda pid: pid + 1),
            mock.patch.object(execution.os, "killpg", lambda pgid, sig: sent.append(("killpg", sig))),
            mock.patch.object(execution.os, "kill", lambda pid, sig: sent.append(("kill", sig))),
        ):
            self.assertFalse(execution.kill_tree(4242, grace=0.2))
        self.assertEqual(sent, [])

    def test_the_last_resort_is_not_used_once_the_tree_is_gone(self):
        """After taskkill, or a group that is no longer there, the bare pid may
        already belong to someone else."""

        def gone(*args, **kwargs):
            raise ProcessLookupError

        kills = []
        with (
            mock.patch.object(execution.subprocess, "run", lambda *args, **kwargs: None),
            mock.patch.object(execution.os, "getpgid", gone, create=True),
            mock.patch.object(execution.os, "killpg", gone, create=True),
        ):
            self.assertTrue(execution._end_tree(4242, 0.2, lambda timeout: True, lambda: kills.append(1)))
        self.assertEqual(kills, [])


class TestPidLiveness(IsolatedCase):
    def test_our_own_pid_is_alive(self):
        import os

        self.assertTrue(execution.pid_alive(os.getpid()))

    def test_a_finished_child_is_not_alive(self):
        proc = subprocess.Popen(python_code("pass"))
        proc.wait(timeout=30)
        # Holding ``proc`` is what makes this deterministic rather than a race
        # with pid reuse: on Windows its handle keeps the process object, and
        # its pid, from being handed to anyone else. That same handle is why
        # this used to answer True -- an openable process is not a running one.
        self.assertFalse(execution.pid_alive(proc.pid))

    def test_a_killed_child_is_not_alive(self):
        """The shape that mattered: something killed it, nothing reaped it.

        `clear_stalls` asks this about a worker that was cancelled or killed,
        and while it answered True the stage was never buried -- so a cancelled
        run stayed in flight until it passed 1.5x its deadline, three quarters
        of an hour later.
        """
        # Its own process group, like every other terminate_tree test here:
        # on POSIX terminate_tree signals the whole group, and a child sharing
        # the runner's group kills the test run itself.
        proc = subprocess.Popen(python_code(SILENT_HANG), **execution._spawn_kwargs())
        try:
            self.assertTrue(execution.pid_alive(proc.pid))
            execution.terminate_tree(proc)
            proc.wait(timeout=30)
            self.assertFalse(execution.pid_alive(proc.pid))
        finally:
            if proc.poll() is None:  # pragma: no cover - safety net
                proc.kill()

    def test_an_impossible_pid_is_not_alive(self):
        self.assertFalse(execution.pid_alive(-1))
        self.assertFalse(execution.pid_alive(0))


class TestOutcomeReporting(IsolatedCase):
    def test_to_dict_carries_the_stall_signals(self):
        outcome = execution.execute(python_code(SILENT_HANG), cwd=self.project, timeout=60, idle_timeout=1)
        payload = outcome.to_dict()
        self.assertTrue(payload["stalled"])
        idle = payload["idle_for_seconds"]
        assert isinstance(idle, (int, float))
        self.assertGreaterEqual(idle, 1)
        self.assertIn("orphans_possible", payload)


class TestSuspendedTime(IsolatedCase):
    def test_a_quick_command_spent_no_time_asleep(self):
        outcome = execution.execute(python_code("print('hi')"), cwd=self.project, timeout=60)
        self.assertEqual(outcome.suspended, 0.0)
        self.assertEqual(outcome.to_dict()["suspended_seconds"], 0.0)

    def test_the_time_the_awake_clock_missed_is_reported_as_asleep(self):
        # An awake clock that missed 1.2s of the run while the monotonic one
        # ran on: the shape of a Windows machine sleeping through part of it.
        reads = []

        def awake() -> float:
            reads.append(1)
            return time.monotonic() if len(reads) == 1 else time.monotonic() - 1.2

        with mock.patch.object(clocks, "awake_clock", return_value=awake):
            outcome = execution.execute(
                python_code("import time; time.sleep(1.5)"), cwd=self.project, timeout=60
            )
        self.assertTrue(outcome.ok)
        self.assertGreaterEqual(outcome.duration, 1.5)
        self.assertAlmostEqual(outcome.suspended, 1.2, delta=0.05)
        self.assertEqual(outcome.to_dict()["suspended_seconds"], round(outcome.suspended, 2))


if __name__ == "__main__":
    unittest.main()
