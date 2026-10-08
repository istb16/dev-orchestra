"""The execution layer: it must always return, and never leave the tree running.

These tests spawn real child processes -- Python itself, never a provider CLI --
because the failure modes being covered (a killed parent's grandchild holding a
pipe, a wedged child producing no output) cannot be faked with a stub.
"""

from __future__ import annotations

import errno
import io
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from typing import Any, List, Optional, Tuple
from unittest import mock

from helpers import SCRIPTS_DIR, IsolatedCase

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

#: Writes its pid to ``argv[1]``, then sleeps for 90s holding the stdout and
#: stderr it inherited.
LINGERING_GRANDCHILD = (
    "import os, sys, time\n"
    "with open(sys.argv[1] + '.tmp', 'w') as f: f.write(str(os.getpid()))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "time.sleep(90)\n"
)

#: Leaves LINGERING_GRANDCHILD running with its stdout and stderr, then exits
#: 0: a CLI that started a dev server in the background and finished.
LINGERING_PARENT = (
    "import os, subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]], stdin=subprocess.DEVNULL,"
    " stdout=sys.stdout, stderr=sys.stderr)\n"
    "deadline = time.monotonic() + 30\n"
    "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline: time.sleep(0.05)\n"
    "sys.stdout.write('parent done\\n'); sys.stdout.flush()\n"
) % LINGERING_GRANDCHILD

#: Writes its pid to ``argv[1]``, stays quiet for 1.5s, then writes to the
#: stdout it inherited without end: a dev server's log.
FLOODING_GRANDCHILD = (
    "import os, sys, time\n"
    "with open(sys.argv[1] + '.tmp', 'w') as f: f.write(str(os.getpid()))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "time.sleep(1.5)\n"
    "for _ in range(9000):\n"
    "    sys.stdout.write('x' * 999 + '\\n'); sys.stdout.flush(); time.sleep(0.01)\n"
)

#: LINGERING_PARENT, with FLOODING_GRANDCHILD left behind.
FLOODING_PARENT = LINGERING_PARENT.replace(repr(LINGERING_GRANDCHILD), repr(FLOODING_GRANDCHILD))

#: Starts ``argv[1]`` (code, given ``argv[2]``) detached and returns at once,
#: so the started process is not our child: the shape of a detached worker.
LAUNCHER = (
    "import os, subprocess, sys\n"
    "kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'"
    " else {'start_new_session': True}\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],"
    " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)\n"
)

#: Ignores SIGTERM, then writes its pid to ``argv[1]`` and sleeps: a member of
#: a group that outlives its leader's SIGTERM.
STUBBORN_CHILD = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "with open(sys.argv[1] + '.tmp', 'w') as f: f.write(str(os.getpid()))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "while True: time.sleep(0.05)\n"
)

#: Starts STUBBORN_CHILD in this process's own group, then writes "<own pid> <child pid>"
#: to ``argv[1]`` and sleeps. It keeps the default SIGTERM action.
LEADER = (
    "import os, subprocess, sys, time\n"
    "child_file = sys.argv[1] + '.child'\n"
    "subprocess.Popen([sys.executable, '-c', %r, child_file],"
    " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "deadline = time.monotonic() + 30\n"
    "while not os.path.exists(child_file) and time.monotonic() < deadline: time.sleep(0.05)\n"
    "with open(child_file) as f: child = f.read()\n"
    "with open(sys.argv[1] + '.tmp', 'w') as f: f.write('%%d %%s' %% (os.getpid(), child))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "while True: time.sleep(0.05)\n"
) % STUBBORN_CHILD

#: A detached worker as far as signals go: it installs the worker's SIGTERM
#: handler and runs ``argv[2]`` (code, given ``argv[3]``) as its CLI.
FAKE_WORKER = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from orchestrator import execution\n"
    "execution.end_children_on_sigterm()\n"
    "execution.execute([sys.executable, '-c', sys.argv[2], sys.argv[3]], cwd=sys.argv[4], timeout=60)\n"
)

#: A worker between CLIs: it installs the handler, writes its pid to
#: ``argv[2]`` and runs nothing.
IDLE_WORKER = (
    "import os, sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from orchestrator import execution\n"
    "execution.end_children_on_sigterm()\n"
    "with open(sys.argv[2] + '.tmp', 'w') as f: f.write(str(os.getpid()))\n"
    "os.replace(sys.argv[2] + '.tmp', sys.argv[2])\n"
    "while True: time.sleep(0.05)\n"
)


def python_code(code):
    return [sys.executable, "-c", code]


def read_pids(case: unittest.TestCase, path: str) -> Tuple[int, ...]:
    """The pids a test script wrote to ``path``, once it has written them."""
    deadline = time.monotonic() + 30
    while not os.path.exists(path):
        case.assertLess(time.monotonic(), deadline, "the process never wrote its pids")
        time.sleep(0.05)
    with open(path) as f:
        return tuple(int(part) for part in f.read().split())


def end_pid_in(path: str) -> None:
    """Safety net: end the process whose pid a test script wrote to ``path``."""
    try:
        with open(path) as f:
            pid = int(f.read())
    except (OSError, ValueError):
        return
    if execution.pid_alive(pid):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass


def kill_group(pgid: int) -> None:
    """Safety net: SIGKILL a whole group, never a bare pid."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


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

    def test_the_prompt_reaches_stdin_byte_for_byte(self):
        # A diff of a CRLF file must not grow a second \r on Windows (#270).
        prompt = "a\nb\r\nc\néあ\n"
        outcome = execution.execute(
            python_code("import sys; print(sys.stdin.buffer.read().hex())"),
            cwd=self.project,
            prompt=prompt,
            timeout=60,
        )
        self.assertTrue(outcome.ok, outcome.stderr)
        self.assertEqual(bytes.fromhex(outcome.stdout.strip()), prompt.encode("utf-8"))

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

    def test_output_without_a_newline_counts_as_output(self):
        # A dot every 0.3s and never a newline: alive, not stalled (#273).
        dots = (
            "import sys, time\n"
            "for _ in range(8):\n"
            "    sys.stdout.write('.'); sys.stdout.flush(); time.sleep(0.3)\n"
        )
        outcome = execution.execute(python_code(dots), cwd=self.project, timeout=60, idle_timeout=1)
        self.assertFalse(outcome.stalled, outcome.stderr)
        self.assertTrue(outcome.ok, outcome.stderr)
        self.assertEqual(outcome.stdout, "." * 8)

    def test_a_quick_command_is_unaffected_by_a_short_idle_deadline(self):
        outcome = execution.execute(
            python_code("print('fast')"), cwd=self.project, timeout=60, idle_timeout=1
        )
        self.assertTrue(outcome.ok)


#: Three stdout lines, the last without a newline, with stderr between them.
MIXED_OUTPUT = (
    "import sys\n"
    "sys.stdout.write('one\\n'); sys.stdout.flush()\n"
    "sys.stderr.write('err one\\n'); sys.stderr.flush()\n"
    "sys.stdout.write('two\\n'); sys.stdout.flush()\n"
    "sys.stderr.write('err two\\n'); sys.stderr.flush()\n"
    "sys.stdout.write('three')\n"
)

#: A line every 0.2s for about two seconds.
STEADY_OUTPUT = "import sys, time\nfor i in range(10):\n    print(i, flush=True); time.sleep(0.2)\n"


class TestOnLine(IsolatedCase):
    def test_only_stdout_lines_reach_the_callback(self):
        seen: List[str] = []
        outcome = execution.execute(
            python_code(MIXED_OUTPUT), cwd=self.project, timeout=60, on_line=seen.append
        )
        self.assertTrue(outcome.ok, outcome.stderr)
        self.assertEqual([line.rstrip("\r\n") for line in seen], ["one", "two", "three"])
        self.assertIn("err two", outcome.stderr)

    def test_a_callback_that_raises_changes_nothing(self):
        def broken(line: str) -> None:
            raise RuntimeError("sink broke")

        outcome = execution.execute(python_code(MIXED_OUTPUT), cwd=self.project, timeout=60, on_line=broken)
        self.assertTrue(outcome.ok, outcome.stderr)
        self.assertEqual(outcome.stdout.splitlines(), ["one", "two", "three"])

    def test_the_callback_runs_after_the_line_is_recorded_and_outside_the_lock(self):
        observed: List[Tuple[float, int]] = []

        def check(line: str) -> None:
            # Takes the drain's lock: called under it, this would never return.
            observed.append((drain.last_seen(), len(drain.text())))

        drain = execution._Drain(check)
        before = drain.last_seen()
        thread = threading.Thread(target=drain.pump, args=(io.BytesIO(b"a\nb\n"),), daemon=True)
        thread.start()
        thread.join(timeout=10)
        drain.finish(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(observed), 2, "the callback deadlocked on the drain's lock")
        # The callback runs on its own thread, so by the time it sees "a" the
        # reader may already have recorded "b" too.
        self.assertIn(observed[0][1], (2, 4))
        self.assertEqual(observed[1][1], 4)
        self.assertTrue(all(seen >= before for seen, _ in observed))

    def test_a_callback_slower_than_the_idle_deadline_does_not_stall_a_healthy_run(self):
        # The reader keeps reading, and so keeps the idle clock fresh, while
        # the callback is still on the first line. Were the callback called
        # from the reader, nothing would be read for 1.5s and a run whose
        # child prints every 0.2s would be killed as stalled at 1s.
        slow = threading.Event()

        def on_line(line: str) -> None:
            if not slow.is_set():
                slow.set()
                time.sleep(1.5)

        outcome = execution.execute(
            python_code(STEADY_OUTPUT), cwd=self.project, timeout=60, idle_timeout=1, on_line=on_line
        )
        self.assertTrue(slow.is_set())
        self.assertFalse(outcome.stalled, outcome.stderr)
        self.assertTrue(outcome.ok, outcome.stderr)
        self.assertEqual(len(outcome.stdout.splitlines()), 10)

    def test_every_line_reaches_a_callback_that_keeps_up(self):
        seen: List[str] = []
        outcome = execution.execute(
            python_code(STEADY_OUTPUT), cwd=self.project, timeout=60, on_line=seen.append
        )
        self.assertTrue(outcome.ok, outcome.stderr)
        # execute() returns only once the callback has caught up.
        self.assertEqual([line.strip() for line in seen], [str(i) for i in range(10)])

    def test_a_full_queue_drops_lines_and_never_blocks_the_reader(self):
        release = threading.Event()
        seen: List[str] = []

        def stuck(line: str) -> None:
            release.wait(10)
            seen.append(line)

        with mock.patch.object(execution, "_LINE_QUEUE_SIZE", 2):
            drain = execution._Drain(stuck)
        text = "".join("%d\n" % i for i in range(20))
        thread = threading.Thread(target=drain.pump, args=(io.BytesIO(text.encode()),), daemon=True)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "the reader waited on the callback")
        self.assertEqual(drain.text(), text)
        self.assertGreater(drain.dropped, 0)
        release.set()
        drain.finish(10)
        self.assertEqual(len(seen) + drain.dropped, 20)

    def test_a_callback_still_behind_at_the_end_is_abandoned(self):
        calls: List[str] = []

        def slow(line: str) -> None:
            calls.append(line)
            time.sleep(0.5)

        drain = execution._Drain(slow)
        drain.pump(io.BytesIO(b"a\nb\nc\nd\n"))
        drain.finish(0.1)
        time.sleep(1.5)
        # The line in hand when it was abandoned may finish; no later one starts.
        self.assertLessEqual(len(calls), 2)

    def test_lines_split_across_reads_reach_the_callback_whole(self):
        # A line, a CRLF and a character may each arrive in pieces.
        pieces = [b"on", b"e\r", b"\ntw", "o あ".encode()[:-1], "あ".encode()[-1:] + b"\nthr", b"ee"]

        class Pieces(io.BytesIO):
            def read1(self, size: Optional[int] = -1) -> bytes:
                return pieces.pop(0) if pieces else b""

        seen: List[str] = []
        drain = execution._Drain(seen.append)
        drain.pump(Pieces())
        drain.finish(10)
        self.assertEqual(seen, ["one\n", "two あ\n", "three"])
        self.assertEqual(drain.text(), "one\ntwo あ\nthree")

    def test_a_silent_child_still_stalls(self):
        seen: List[str] = []
        outcome = execution.execute(
            python_code(SILENT_HANG), cwd=self.project, timeout=120, idle_timeout=1, on_line=seen.append
        )
        self.assertTrue(outcome.stalled)
        self.assertEqual(seen, [])


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

    def test_a_grandchild_left_holding_the_output_after_a_clean_exit_does_not_block_the_return(self):
        """The CLI exits 0, but what it left running still holds the pipes:
        closing them would wait on the readers for as long as it lives."""
        pid_file = os.path.join(self.project, "grandchild.pid")
        self.addCleanup(end_pid_in, pid_file)
        started = time.monotonic()
        outcome = execution.execute([*python_code(LINGERING_PARENT), pid_file], cwd=self.project, timeout=120)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 45, "returned only after the grandchild exited")
        self.assertEqual(outcome.exit_code, 0, outcome.stderr)
        self.assertFalse(outcome.timed_out)
        self.assertIn("parent done", outcome.stdout)
        (grandchild,) = read_pids(self, pid_file)
        if execution.IS_WINDOWS:
            # Its parent is gone, so taskkill /T has no tree to start from.
            self.assertTrue(outcome.orphans_possible)
            self.assertIn("may still be running", outcome.stderr)
        else:
            self.assertFalse(outcome.orphans_possible, outcome.stderr)
            self.assertIn("it was stopped", outcome.stderr)
            deadline = time.monotonic() + 10
            while execution.pid_alive(grandchild) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(execution.pid_alive(grandchild))

    def test_what_a_leftover_writes_after_the_cli_exited_is_not_kept(self):
        self.assertNotEqual(FLOODING_PARENT, LINGERING_PARENT)
        pid_file = os.path.join(self.project, "grandchild.pid")
        self.addCleanup(end_pid_in, pid_file)
        outcome = execution.execute([*python_code(FLOODING_PARENT), pid_file], cwd=self.project, timeout=120)
        self.assertEqual(outcome.exit_code, 0, outcome.stderr)
        self.assertEqual(outcome.stdout, "parent done\n")
        self.assertRegex(outcome.stderr, r"warning: [0-9]+ characters it wrote after the CLI exited")

    def test_what_the_cli_wrote_last_is_kept_when_a_leftover_is_cut_off(self):
        pid_file = os.path.join(self.project, "grandchild.pid")
        self.addCleanup(end_pid_in, pid_file)
        last_words = LINGERING_PARENT + "sys.stdout.write('z' * 300000); sys.stdout.flush()\n"
        outcome = execution.execute([*python_code(last_words), pid_file], cwd=self.project, timeout=120)
        self.assertEqual(outcome.exit_code, 0, outcome.stderr)
        self.assertEqual(outcome.stdout, "parent done\n" + "z" * 300000)

    def test_a_detached_drain_keeps_reading_and_keeps_nothing(self):
        read_end, write_end = os.pipe()
        reader, writer = os.fdopen(read_end, "rb"), os.fdopen(write_end, "wb")
        self.addCleanup(writer.close)
        seen: List[str] = []
        drain = execution._Drain(seen.append)
        thread = threading.Thread(target=drain.pump, args=(reader,), daemon=True)
        thread.start()

        def wait_for(text: str) -> None:
            deadline = time.monotonic() + 10
            while drain.text() != text:
                self.assertLess(time.monotonic(), deadline, drain.text())
                time.sleep(0.01)

        writer.write(b"keep\n")
        writer.flush()
        wait_for("keep\n")
        mark = drain.mark()
        writer.write(b"drop\n")
        writer.flush()
        wait_for("keep\ndrop\n")
        self.assertEqual(drain.detach(mark), 5)
        self.assertEqual(drain.text(), "keep\n")
        # Far more than a pipe buffer: the writer would block were it not read.
        flood = threading.Thread(target=lambda: (writer.write(b"y" * 4_000_000), writer.flush()), daemon=True)
        flood.start()
        flood.join(timeout=30)
        self.assertFalse(flood.is_alive(), "the writer blocked on a pipe nobody read")
        writer.close()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(drain.text(), "keep\n")
        self.assertEqual(drain.chunks, ["keep\n"])
        drain.finish(5)
        self.assertNotIn("y", "".join(seen))

    def test_a_pipe_still_in_use_is_not_closed(self):
        class Pipe:
            closed = False

            def __init__(self) -> None:
                self.close = mock.Mock()

        class Thread:
            def __init__(self, alive: bool) -> None:
                self.alive = alive

            def is_alive(self) -> bool:
                return self.alive

        proc: Any = mock.Mock(stdout=Pipe(), stderr=Pipe(), stdin=Pipe())
        threads: Any = [Thread(True), Thread(False), Thread(False)]
        execution._close_pipes(proc, threads)
        proc.stdout.close.assert_not_called()
        proc.stderr.close.assert_called_once_with()
        proc.stdin.close.assert_called_once_with()

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
        pids_file = os.path.join(self.project, "pids.txt")
        worker = (
            "import os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', %r])\n"
            "with open(sys.argv[1] + '.tmp', 'w') as f: f.write('%%d %%d' %% (os.getpid(), child.pid))\n"
            "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "while True: time.sleep(0.05)\n"
        ) % SILENT_HANG
        subprocess.run([*python_code(LAUNCHER), worker, pids_file], check=True, timeout=30)
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


class _Exited(Exception):
    """Raised by a fake ``os._exit``, so the test survives it."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class TestGroupEnd(IsolatedCase):
    """The POSIX group logic, driven by fakes so that it runs on every OS."""

    def as_posix(self, killpg) -> None:
        self.enterContext(mock.patch.object(execution, "IS_WINDOWS", False))
        self.enterContext(mock.patch.object(execution, "_reap", lambda pid: None))
        self.enterContext(mock.patch.object(execution.os, "killpg", killpg, create=True))
        self.enterContext(mock.patch.object(execution.signal, "SIGKILL", 9, create=True))

    def test_group_alive_trusts_only_esrch(self):
        cases = [
            (OSError(errno.ESRCH, "no such group"), False),
            (None, True),
            (OSError(errno.EPERM, "only zombies left"), True),
            (OSError(errno.EINVAL, "odd"), True),
        ]
        for error, alive in cases:

            def killpg(pgid, sig, error=error):
                if error is not None:
                    raise error

            with mock.patch.object(execution.os, "killpg", killpg, create=True):
                self.assertEqual(execution._group_alive(4242), alive, repr(error))

    def test_a_group_that_outlives_sigkill_is_not_reported_gone(self):
        sent = []
        kills = []
        self.as_posix(lambda pgid, sig: sent.append(sig) if sig else None)
        self.assertFalse(execution._end_tree(4242, 0.2, lambda timeout: True, lambda: kills.append(1)))
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(kills, [])

    def test_an_empty_group_is_seen_even_when_the_leader_used_the_whole_window(self):
        sent = []

        def killpg(pgid, sig):
            if sig == 0:
                raise ProcessLookupError
            sent.append(sig)

        def exited(timeout):
            time.sleep(timeout)
            return True

        self.as_posix(killpg)
        self.assertTrue(execution._end_tree(4242, 0.2, exited, lambda: None))
        self.assertEqual(sent, [signal.SIGTERM])

    def test_no_sigkill_to_a_group_that_emptied_after_its_leader_left(self):
        """Alive for the whole SIGTERM step, gone by the time SIGKILL is due:
        the emptied id may be someone else's by then."""
        sent = []

        def killpg(pgid, sig):
            if sig == 0:
                raise ProcessLookupError
            sent.append(sig)

        self.as_posix(killpg)
        self.enterContext(mock.patch.object(execution, "_group_ends", lambda pgid, deadline: False))
        self.assertTrue(execution._end_tree(4242, 0.2, lambda timeout: True, lambda: None))
        self.assertEqual(sent, [signal.SIGTERM])

    def test_a_group_we_may_not_signal_falls_back_to_kill_and_is_not_gone(self):
        """EPERM, e.g. a group that is someone else's by now: nothing more is
        sent to it, and it is never reported gone."""
        sent = []
        kills = []

        def killpg(pgid, sig):
            if sig == 0:
                return
            sent.append(sig)
            raise PermissionError(errno.EPERM, "not ours")

        self.as_posix(killpg)
        self.assertFalse(execution._end_tree(4242, 0.2, lambda timeout: bool(kills), lambda: kills.append(1)))
        self.assertEqual(sent, [signal.SIGTERM])
        self.assertEqual(kills, [1])

    def test_a_leader_that_never_exits_is_killed_and_not_reported_gone(self):
        sent = []
        kills = []
        self.as_posix(lambda pgid, sig: sent.append(sig) if sig else None)
        self.assertFalse(execution._end_tree(4242, 0.2, lambda timeout: False, lambda: kills.append(1)))
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(kills, [1])

    def test_a_killed_leader_whose_group_survives_is_not_reported_gone(self):
        kills = []

        def exited(timeout):
            # Only the wait after kill() sees the leader go.
            return bool(kills) and timeout > 0

        self.as_posix(lambda pgid, sig: None)
        self.assertFalse(execution._end_tree(4242, 0.2, exited, lambda: kills.append(1)))
        self.assertEqual(kills, [1])

    def test_group_ends_gives_up_at_the_deadline(self):
        probes = []
        self.as_posix(lambda pgid, sig: probes.append(sig))
        self.assertFalse(execution._group_ends(4242, time.monotonic() + 0.3))
        self.assertGreater(len(probes), 1)
        # A deadline already past still probes once.
        del probes[:]
        self.assertFalse(execution._group_ends(4242, time.monotonic() - 1))
        self.assertEqual(probes, [0])

    def test_end_tree_refuses_pid_0_and_1(self):
        sent = []
        kills = []
        self.as_posix(lambda pgid, sig: sent.append((pgid, sig)))
        for pid in (0, 1):
            self.assertFalse(execution._end_tree(pid, 0.2, lambda timeout: True, lambda: kills.append(1)))
        self.assertEqual(sent, [])
        self.assertEqual(kills, [])

    def test_kill_tree_leader_gone_between_checks(self):
        """It exited between the liveness check and the group lookup: the
        group is reported on, never signalled."""

        def gone(pid):
            raise ProcessLookupError

        sent = []
        # What the group probe raises; None while the group is still there.
        probe: List[Optional[BaseException]] = [None]

        def killpg(pgid, sig):
            if sig != 0:
                sent.append(("killpg", sig))
            elif probe[0] is not None:
                raise probe[0]

        def kill(pid, sig):
            sent.append(("kill", sig))

        self.as_posix(killpg)
        self.enterContext(mock.patch.object(execution, "pid_alive", lambda pid: True))
        self.enterContext(mock.patch.object(execution.os, "getpgid", gone, create=True))
        self.enterContext(mock.patch.object(execution.os, "kill", kill))
        for error, expected in ((None, False), (ProcessLookupError(), True)):
            probe[0] = error
            self.assertEqual(execution.kill_tree(4242, grace=0.2), expected, repr(error))
        self.assertEqual(sent, [])


class TestStopOnSigterm(IsolatedCase):
    """A detached worker's SIGTERM handler ends the CLI it runs before exiting."""

    def setUp(self) -> None:
        super().setUp()
        self.ended: List[Tuple[int, float]] = []
        self.notes: List[str] = []
        self.group_ends = True
        execution._stop.reset()
        self.addCleanup(execution._stop.reset)
        self.enterContext(mock.patch.object(execution, "_stop_group", self.record_end))
        self.enterContext(mock.patch.object(execution, "_note", self.notes.append))
        self.enterContext(mock.patch.object(execution.os, "_exit", self.fake_exit))

    def record_end(self, pgid, grace):
        self.ended.append((pgid, grace))
        return self.group_ends

    @staticmethod
    def fake_exit(code):
        raise _Exited(code)

    @staticmethod
    def running_cli(pid: int = 4242) -> mock.Mock:
        return mock.Mock(pid=pid, returncode=None)

    def assert_left_clean(self) -> None:
        self.assertEqual(execution._stop.active, [])
        self.assertIs(execution._stop.spawning, False)

    def test_sigterm_ends_the_active_cli_then_exits(self):
        execution._stop.active.append(self.running_cli())
        with self.assertRaises(_Exited) as caught:
            execution._on_sigterm(signal.SIGTERM, None)
        self.assertEqual(self.ended, [(4242, execution._STOP_GRACE_SECONDS)])
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(self.notes, [])

    def test_a_cli_group_that_cannot_be_ended_is_noted_before_exiting(self):
        self.group_ends = False
        execution._stop.active.append(self.running_cli())
        with self.assertRaises(_Exited) as caught:
            execution._on_sigterm(signal.SIGTERM, None)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("pid 4242", self.notes[0])
        self.assertIn("may still be running", self.notes[0])

    def test_a_reaped_cli_is_not_signalled(self):
        """Its pid may be someone else's by now."""
        execution._stop.active.append(mock.Mock(pid=4242, returncode=0))
        with self.assertRaises(_Exited):
            execution._on_sigterm(signal.SIGTERM, None)
        self.assertEqual(self.ended, [])

    def test_sigterm_during_spawn_is_honoured_after_registration(self):
        fake = self.running_cli()

        def popen(*args, **kwargs):
            execution._on_sigterm(signal.SIGTERM, None)
            # Deferred: the CLI is not registered yet.
            self.assertEqual(self.ended, [])
            return fake

        with mock.patch.object(execution.subprocess, "Popen", popen), self.assertRaises(_Exited):
            execution.execute(["cli"], cwd=self.project, timeout=5)
        self.assertEqual(self.ended, [(4242, execution._STOP_GRACE_SECONDS)])
        # The real os._exit never returns, so the CLI is still registered here
        # only because the fake one raised; the flag is what must be reset.
        self.assertEqual(execution._stop.active, [fake])
        self.assertIs(execution._stop.spawning, False)

    def test_sigterm_during_a_failed_spawn_is_honoured(self):
        def popen(*args, **kwargs):
            execution._on_sigterm(signal.SIGTERM, None)
            raise OSError("no such file")

        with mock.patch.object(execution.subprocess, "Popen", popen), self.assertRaises(_Exited) as caught:
            execution.execute(["cli"], cwd=self.project, timeout=5)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(self.ended, [])
        self.assert_left_clean()

    def test_a_spawn_error_other_than_oserror_is_not_turned_into_a_stop(self):
        """Only the finally runs: the pending stop is not honoured on the way out."""

        def popen(*args, **kwargs):
            execution._on_sigterm(signal.SIGTERM, None)
            raise ValueError("bad argument")

        with mock.patch.object(execution.subprocess, "Popen", popen), self.assertRaises(ValueError):
            execution.execute(["cli"], cwd=self.project, timeout=5)
        self.assertEqual(self.ended, [])
        self.assert_left_clean()

    def test_the_cli_is_registered_while_spawning_and_honoured_after(self):
        """A SIGTERM between Popen and the registration is still deferred."""
        seen: List[Tuple[str, bool]] = []

        class Recording(list):
            def append(self, item):
                seen.append(("append", execution._stop.spawning))
                super().append(item)

        def honour():
            seen.append(("honour", execution._stop.spawning))
            raise _Exited(0)

        with (
            mock.patch.object(execution._stop, "active", Recording()),
            mock.patch.object(execution, "_honour_stop", honour),
            mock.patch.object(execution.subprocess, "Popen", lambda *a, **k: self.running_cli()),
            self.assertRaises(_Exited),
        ):
            execution.execute(["cli"], cwd=self.project, timeout=5)
        self.assertEqual(seen, [("append", True), ("honour", False)])

    def test_a_failed_spawn_with_no_sigterm_leaves_nothing_behind(self):
        with mock.patch.object(execution.subprocess, "Popen", mock.Mock(side_effect=OSError("nope"))):
            outcome = execution.execute(["cli"], cwd=self.project, timeout=5)
        self.assertEqual(outcome.exit_code, execution.EXIT_SPAWN_FAILED)
        self.assert_left_clean()

    def test_execute_unregisters_its_cli_when_it_returns(self):
        outcome = execution.execute(python_code("print('hi')"), cwd=self.project, timeout=30)
        self.assertEqual(outcome.exit_code, 0)
        self.assert_left_clean()

    def test_execute_unregisters_its_cli_when_it_raises(self):
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            self.addCleanup(proc.wait, 30)
            self.addCleanup(execution.terminate_tree, proc, 2)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    self.addCleanup(pipe.close)
            return proc

        with (
            mock.patch.object(execution.subprocess, "Popen", popen),
            mock.patch.object(execution, "_Drain", mock.Mock(side_effect=RuntimeError("boom"))),
            self.assertRaises(RuntimeError),
        ):
            execution.execute(python_code(SILENT_HANG), cwd=self.project, timeout=60)
        self.assert_left_clean()

    def test_sigterm_with_no_cli_exits_at_once(self):
        with self.assertRaises(_Exited):
            execution._on_sigterm(signal.SIGTERM, None)
        self.assertEqual(self.ended, [])

    def test_nothing_is_installed_on_windows(self):
        with (
            mock.patch.object(execution, "IS_WINDOWS", True),
            mock.patch.object(execution.signal, "signal") as install,
        ):
            execution.end_children_on_sigterm()
        install.assert_not_called()

    def test_the_handler_is_installed_for_sigterm_on_posix(self):
        with (
            mock.patch.object(execution, "IS_WINDOWS", False),
            mock.patch.object(execution.signal, "SIGTERM", 15),
            mock.patch.object(execution.signal, "signal") as install,
        ):
            execution.end_children_on_sigterm()
        install.assert_called_once_with(15, execution._on_sigterm)

    def test_the_note_path_is_kept_for_the_handler(self):
        note = os.path.join(self.project, "a-1.stop")
        with (
            mock.patch.object(execution, "IS_WINDOWS", False),
            mock.patch.object(execution._stop, "note_path", None),
            mock.patch.object(execution.signal, "signal"),
        ):
            execution.end_children_on_sigterm(note)
            self.assertEqual(execution._stop.note_path, note)


class TestStopNote(IsolatedCase):
    """What the handler could not end reaches the note file, since a worker's
    stderr goes nowhere."""

    def test_each_line_is_appended_to_the_note_file(self):
        note = os.path.join(self.project, "a-1.stop")
        with (
            mock.patch.object(execution._stop, "note_path", note),
            mock.patch.object(execution.os, "write", wraps=os.write) as wrote,
        ):
            execution._note("first\n")
            execution._note("second\n")
        with open(note, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "first\nsecond\n")
        # And to stderr, for a worker run by hand.
        self.assertIn(mock.call(2, b"first\n"), wrote.call_args_list)

    def test_no_note_file_outside_a_worker(self):
        with (
            mock.patch.object(execution._stop, "note_path", None),
            mock.patch.object(execution.os, "open") as opened,
        ):
            execution._note("x\n")
        opened.assert_not_called()

    def test_an_unwritable_note_file_is_ignored(self):
        missing = os.path.join(self.project, "no-such-dir", "a-1.stop")
        with mock.patch.object(execution._stop, "note_path", missing):
            execution._note("x\n")
        self.assertFalse(os.path.exists(missing))


class TestStopGroup(IsolatedCase):
    """The handler's own group kill, driven by fakes so that it runs on every OS."""

    def setUp(self) -> None:
        super().setUp()
        self.sent: List[int] = []
        self.reaped: List[int] = []
        self.enterContext(mock.patch.object(execution, "_reap", self.reaped.append))
        self.enterContext(mock.patch.object(execution.signal, "SIGKILL", 9, create=True))

    def killpg(self, alive_after: Optional[int] = None, error: Optional[BaseException] = None):
        """A group that empties after signal ``alive_after`` (never if None),
        or whose first real signal raises ``error``."""

        def killpg(pgid, sig):
            if sig == 0:
                if alive_after is not None and alive_after in self.sent:
                    raise ProcessLookupError
                return
            if error is not None:
                raise error
            self.sent.append(sig)

        self.enterContext(mock.patch.object(execution.os, "killpg", killpg, create=True))

    def test_a_group_that_ends_on_sigterm_gets_no_sigkill(self):
        self.killpg(alive_after=signal.SIGTERM)
        self.assertTrue(execution._stop_group(4242, 0.2))
        self.assertEqual(self.sent, [signal.SIGTERM])
        # The leader is our child: an unreaped one keeps the group present.
        self.assertIn(4242, self.reaped)

    def test_a_group_that_ignores_sigterm_gets_sigkill(self):
        self.killpg(alive_after=9)
        self.assertTrue(execution._stop_group(4242, 0.2))
        self.assertEqual(self.sent, [signal.SIGTERM, 9])

    def test_a_group_that_never_empties_is_not_reported_gone(self):
        self.killpg()
        self.assertFalse(execution._stop_group(4242, 0.2))
        self.assertEqual(self.sent, [signal.SIGTERM, 9])

    def test_a_group_already_gone_is_gone(self):
        self.killpg(error=ProcessLookupError())
        self.assertTrue(execution._stop_group(4242, 0.2))

    def test_a_group_we_may_not_signal_is_not_reported_gone(self):
        self.killpg(error=PermissionError(errno.EPERM, "not ours"))
        self.assertFalse(execution._stop_group(4242, 0.2))

    def test_pid_0_and_1_are_refused(self):
        self.killpg()
        for pid in (0, 1):
            self.assertFalse(execution._stop_group(pid, 0.2))
        self.assertEqual(self.sent, [])

    def test_it_never_waits_through_popen(self):
        """The handler may have interrupted the main thread inside proc.wait."""
        proc = mock.Mock(pid=4242, returncode=None)
        self.killpg(alive_after=signal.SIGTERM)
        with (
            mock.patch.object(execution._stop, "active", [proc]),
            mock.patch.object(execution.os, "_exit", TestStopOnSigterm.fake_exit),
            self.assertRaises(_Exited),
        ):
            execution._stop_now()
        proc.wait.assert_not_called()
        proc.poll.assert_not_called()
        self.assertEqual(self.sent, [signal.SIGTERM])


@unittest.skipIf(execution.IS_WINDOWS, "process groups are POSIX")
class TestGroupKill(IsolatedCase):
    """Real groups whose member ignores SIGTERM, so it outlives its leader."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # A member killed after its leader is reaped by whoever adopts it. If
        # nobody does, its group never reads gone, and these tests prove nothing.
        probe = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', %r], stdin=subprocess.DEVNULL,"
            " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print('up', flush=True)\n"
            "while True: time.sleep(0.05)\n"
        ) % SILENT_HANG
        proc = subprocess.Popen(
            python_code(probe), stdout=subprocess.PIPE, text=True, **execution.spawn_kwargs()
        )
        assert proc.stdout is not None
        try:
            proc.stdout.readline()
        finally:
            kill_group(proc.pid)
            proc.wait(timeout=30)
            proc.stdout.close()
        deadline = time.monotonic() + 3
        while True:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            if time.monotonic() >= deadline:
                raise unittest.SkipTest("orphans are not reaped here")
            time.sleep(0.05)

    def assert_group_gone(self, pgid: int) -> None:
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)

    def end_on_cleanup(self, proc: subprocess.Popen) -> None:
        def end() -> None:
            kill_group(proc.pid)
            proc.kill()
            proc.wait(timeout=30)

        self.addCleanup(end)

    def start_leader(self) -> Tuple[subprocess.Popen, int]:
        pids_file = os.path.join(self.project, "pids.txt")
        proc = subprocess.Popen([*python_code(LEADER), pids_file], **execution.spawn_kwargs())
        self.end_on_cleanup(proc)
        leader_pid, child_pid = read_pids(self, pids_file)
        self.assertEqual(leader_pid, proc.pid)
        self.assertEqual(os.getpgid(child_pid), proc.pid)
        return proc, child_pid

    def record_signals(self) -> List[int]:
        """The signals sent through os.killpg and os.kill, still delivered."""
        sent: List[int] = []
        real_killpg, real_kill = os.killpg, os.kill

        def killpg(pgid, sig):
            if sig:
                sent.append(sig)
            real_killpg(pgid, sig)

        def kill(pid, sig):
            if sig:
                sent.append(sig)
            real_kill(pid, sig)

        self.enterContext(mock.patch.object(execution.os, "killpg", killpg))
        self.enterContext(mock.patch.object(execution.os, "kill", kill))
        return sent

    def test_terminate_tree_kills_a_child_that_ignores_sigterm(self):
        proc, _ = self.start_leader()
        self.assertTrue(execution.terminate_tree(proc, grace=2))
        self.assertIsNotNone(proc.poll())
        self.assert_group_gone(proc.pid)

    def test_kill_tree_kills_a_child_that_ignores_sigterm(self):
        proc, _ = self.start_leader()
        self.assertTrue(execution.kill_tree(proc.pid, grace=2))
        self.assert_group_gone(proc.pid)

    def test_kill_tree_on_a_detached_worker_whose_child_ignores_sigterm(self):
        pids_file = os.path.join(self.project, "pids.txt")
        subprocess.run([*python_code(LAUNCHER), LEADER, pids_file], check=True, timeout=30)
        worker_pid, child_pid = read_pids(self, pids_file)
        self.addCleanup(kill_group, worker_pid)
        self.assertEqual(os.getpgid(child_pid), worker_pid)
        self.assertTrue(execution.kill_tree(worker_pid, grace=2))
        self.assert_group_gone(worker_pid)

    def test_a_clean_group_gets_no_sigkill(self):
        proc = subprocess.Popen(python_code(SILENT_HANG), **execution.spawn_kwargs())
        self.end_on_cleanup(proc)
        sent = self.record_signals()
        self.assertTrue(execution.terminate_tree(proc, grace=4))
        self.assertEqual(sent, [signal.SIGTERM])

        proc = subprocess.Popen(python_code(SILENT_HANG), **execution.spawn_kwargs())
        self.end_on_cleanup(proc)
        del sent[:]
        self.assertTrue(execution.kill_tree(proc.pid, grace=4))
        self.assertEqual(sent, [signal.SIGTERM])

    def test_execute_timeout_kills_a_child_that_ignores_sigterm(self):
        pids_file = os.path.join(self.project, "pids.txt")
        real_monotonic = time.monotonic

        def clock() -> float:
            # The deadline is reached once the leader has written both pids,
            # however long a loaded machine took to get there.
            return real_monotonic() + (1000 if os.path.exists(pids_file) else 0)

        with mock.patch.object(execution.time, "monotonic", clock):
            outcome = execution.execute([*python_code(LEADER), pids_file], cwd=self.project, timeout=60)
        leader_pid, _ = read_pids(self, pids_file)
        self.addCleanup(kill_group, leader_pid)
        self.assertTrue(outcome.timed_out)
        self.assertFalse(outcome.orphans_possible, outcome.stderr)
        self.assert_group_gone(leader_pid)

    def test_execute_reports_orphans_when_the_group_survives(self):
        real = execution.terminate_tree
        with (
            mock.patch.object(execution, "_group_alive", lambda pgid: True),
            mock.patch.object(execution, "terminate_tree", lambda proc, grace=0.0: real(proc, 0.4)),
        ):
            outcome = execution.execute(python_code(SILENT_HANG), cwd=self.project, timeout=1)
        self.assertTrue(outcome.timed_out)
        self.assertTrue(outcome.orphans_possible)
        self.assertIn("check for orphans", outcome.stderr)

    def test_cancelled_worker_ends_its_cli_tree(self):
        """The CLI runs in a session of its own, so only the worker's handler
        can reach it: without it, the CLI and its child would both run on."""
        pids_file = os.path.join(self.project, "pids.txt")
        worker = subprocess.Popen(
            [*python_code(FAKE_WORKER), SCRIPTS_DIR, LEADER, pids_file, self.project],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **execution.spawn_kwargs(),
        )
        self.end_on_cleanup(worker)
        cli_pid, _ = read_pids(self, pids_file)
        self.addCleanup(kill_group, cli_pid)
        self.assertTrue(execution.kill_tree(worker.pid))
        deadline = time.monotonic() + 5
        while execution._group_alive(cli_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assert_group_gone(cli_pid)

    def test_a_worker_exits_through_its_handler_within_its_own_budget(self):
        """The CLI's child ignores SIGTERM, so the handler has to escalate;
        it still exits with SIGTERM's status, not killed by anyone else."""
        pids_file = os.path.join(self.project, "pids.txt")
        worker = subprocess.Popen(
            [*python_code(FAKE_WORKER), SCRIPTS_DIR, LEADER, pids_file, self.project],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **execution.spawn_kwargs(),
        )
        self.end_on_cleanup(worker)
        cli_pid, _ = read_pids(self, pids_file)
        self.addCleanup(kill_group, cli_pid)
        os.kill(worker.pid, signal.SIGTERM)
        # Bounded: a handler that hangs fails here instead of being SIGKILLed.
        self.assertEqual(worker.wait(timeout=30), 128 + signal.SIGTERM)
        deadline = time.monotonic() + 5
        while execution._group_alive(cli_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assert_group_gone(cli_pid)

    def test_a_worker_with_no_cli_running_exits_on_sigterm(self):
        """Between CLIs: nothing to end, but the request is still honoured."""
        ready_file = os.path.join(self.project, "ready.txt")
        worker = subprocess.Popen(
            [*python_code(IDLE_WORKER), SCRIPTS_DIR, ready_file],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **execution.spawn_kwargs(),
        )
        self.end_on_cleanup(worker)
        self.assertEqual(read_pids(self, ready_file), (worker.pid,))
        os.kill(worker.pid, signal.SIGTERM)
        self.assertEqual(worker.wait(timeout=30), 128 + signal.SIGTERM)


class TestPidLiveness(IsolatedCase):
    def test_our_own_pid_is_alive(self):
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


class _Polled:
    """A child that ``_verdict`` asks only for its exit code."""

    def __init__(self, code: Optional[int]):
        self.code = code

    def poll(self) -> Optional[int]:
        return self.code


ORPHANS = "\nwarning: the process group did not exit after being killed; check for orphans\n"


class TestVerdict(unittest.TestCase):
    """The exit code ``execute`` reports, and what it puts around stderr."""

    def verdict(self, code, watched, orphans=False):
        proc: Any = _Polled(code)
        return execution._verdict(proc, watched, "err", 7.4, 2.0, orphans)

    def test_a_timed_out_run(self):
        verdict = self.verdict(-9, execution._Watched(True, False, 0))
        self.assertEqual(verdict, (execution.EXIT_TOTAL_TIMEOUT, "timed out after 7s\nerr"))

    def test_a_stalled_run(self):
        verdict = self.verdict(-9, execution._Watched(False, True, 3.0))
        expected = "no output for 3s (idle limit 2s); treated as stalled\nerr"
        self.assertEqual(verdict, (execution.EXIT_IDLE_STALL, expected))

    def test_a_stall_reported_over_a_timeout(self):
        verdict = self.verdict(-9, execution._Watched(True, True, 3.0))
        self.assertEqual(verdict[0], execution.EXIT_IDLE_STALL)
        self.assertTrue(verdict[1].startswith("no output for 3s"))

    def test_orphans_follow_the_prefix(self):
        cases = [
            (execution._Watched(True, False, 0), execution.EXIT_TOTAL_TIMEOUT, "timed out after 7s\nerr"),
            (
                execution._Watched(False, True, 3.0),
                execution.EXIT_IDLE_STALL,
                "no output for 3s (idle limit 2s); treated as stalled\nerr",
            ),
            (execution._Watched(False, False, 0), 1, "err"),
        ]
        for watched, code, text in cases:
            with self.subTest(watched=watched):
                self.assertEqual(self.verdict(1, watched, orphans=True), (code, text + ORPHANS))

    def test_a_lingering_process_is_named_whether_or_not_it_was_stopped(self):
        watched = execution._Watched(False, False, 0)
        proc: Any = _Polled(0)
        code, stopped = execution._verdict(proc, watched, "err", 7.4, None, False, lingering=True)
        self.assertEqual(code, 0)
        self.assertTrue(stopped.startswith("err\nwarning: the CLI exited"), stopped)
        self.assertIn("it was stopped", stopped)
        _, running = execution._verdict(proc, watched, "err", 7.4, None, True, lingering=True)
        self.assertIn("may still be running; check for orphans", running)
        self.assertNotIn("after being killed", running)

    def test_a_finished_run_keeps_its_code_and_stderr(self):
        self.assertEqual(self.verdict(3, execution._Watched(False, False, 0)), (3, "err"))

    def test_a_child_with_no_exit_code_failed_to_start(self):
        verdict = self.verdict(None, execution._Watched(False, False, 0))
        self.assertEqual(verdict, (execution.EXIT_SPAWN_FAILED, "err"))


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
