"""Shared-file safety, once detached workers exist.

CI found this the hard way: a `jobs wait` failed on one platform because the
worker was rewriting the job file while the parent read it. A truncate window
produced a parse error, and returning the default turned "unreadable" into
"absent" -- so a live job looked like a missing one.
"""

from __future__ import annotations

import io
import os
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import IsolatedCase

from orchestrator import cli, cli_review
from orchestrator import ledger as ledger_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws


class TestAtomicWrites(IsolatedCase):
    def test_a_reader_never_sees_a_partial_file(self):
        """A poller reading while a worker rewrites must see one version or the other."""
        path = os.path.join(self.project, "big.json")
        small = {"n": 1}
        large = {"n": 2, "padding": ["x" * 200] * 200}
        ws.write_json(path, small)

        stop = threading.Event()
        write_errors = []
        unreadable = 0
        samples = 0

        def writer():
            while not stop.is_set():
                try:
                    ws.write_json(path, large)
                    ws.write_json(path, small)
                except Exception as exc:  # a concurrent reader must not break writes
                    write_errors.append(repr(exc))
                    return

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                data = ws.read_json(path, "UNREADABLE")
                samples += 1
                if data == "UNREADABLE":
                    unreadable += 1
                else:
                    self.assertIn(data.get("n"), (1, 2))
                time.sleep(0.001)  # a poller, not a spin loop
        finally:
            stop.set()
            thread.join(timeout=5)

        self.assertEqual(write_errors, [], "a reader must never make a write fail")
        self.assertGreater(samples, 20, "expected the reader to get plenty of samples")
        self.assertEqual(unreadable, 0, "read %d of %d samples as unreadable" % (unreadable, samples))

    def test_write_text_is_atomic_too(self):
        path = os.path.join(self.project, "out.txt")
        ws.write_text(path, "first")
        ws.write_text(path, "second")
        self.assertEqual(ws.read_text(path), "second")

    def test_no_temp_files_are_left_behind(self):
        path = os.path.join(self.project, "out.txt")
        for _ in range(5):
            ws.write_text(path, "content")
        leftovers = [name for name in os.listdir(self.project) if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_a_genuinely_corrupt_file_still_yields_the_default(self):
        path = os.path.join(self.project, "broken.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.assertEqual(ws.read_json(path, "fallback"), "fallback")

    def test_a_read_refused_mid_rename_is_retried(self):
        """Windows refuses an open that lands in the instant a rename replaces
        the file. That is as passing as a torn read, not a missing file."""
        path = os.path.join(self.project, "state.json")
        ws.write_json(path, {"ledger": {}})
        original = ws._read_shared
        refusals = [2]

        def refuse_then_read(target):
            if refusals[0]:
                refusals[0] -= 1
                raise PermissionError(13, "sharing violation", target)
            return original(target)

        ws._read_shared = refuse_then_read
        self.addCleanup(setattr, ws, "_read_shared", original)
        self.assertEqual(ws.read_json(path, "UNREADABLE"), {"ledger": {}})

    def test_a_file_that_stays_refused_still_yields_the_default(self):
        path = os.path.join(self.project, "state.json")
        ws.write_json(path, {"ledger": {}})
        original = ws._read_shared

        def refuse(target):
            raise PermissionError(13, "access denied", target)

        ws._read_shared = refuse
        self.addCleanup(setattr, ws, "_read_shared", original)
        self.assertEqual(ws.read_json(path, "fallback"), "fallback")

    def test_a_missing_file_yields_the_default_without_retrying(self):
        started = time.monotonic()
        self.assertIsNone(ws.read_json(os.path.join(self.project, "absent.json")))
        self.assertLess(time.monotonic() - started, 0.5)


class TestStrictTextReads(IsolatedCase):
    """`read_text_strict` never guesses: absent is "", unreadable is None."""

    def patch_reader(self, reader):
        original = ws._read_shared
        ws._read_shared = reader
        self.addCleanup(setattr, ws, "_read_shared", original)

    def test_an_absent_file_is_empty(self):
        self.assertEqual(ws.read_text_strict(os.path.join(self.project, "absent.md")), "")

    def test_a_valid_file_is_its_text(self):
        path = os.path.join(self.project, "plan.md")
        ws.write_text(path, "# Plan\n")
        self.assertEqual(ws.read_text_strict(path), "# Plan\n")

    def test_invalid_utf8_is_unreadable_where_the_tolerant_read_replaces(self):
        path = os.path.join(self.project, "plan.md")
        with open(path, "wb") as handle:
            handle.write(b"# Plan \xff\n")
        self.assertIsNone(ws.read_text_strict(path))
        self.assertIn("�", ws.read_text(path))

    def test_a_read_refused_once_is_retried(self):
        path = os.path.join(self.project, "plan.md")
        ws.write_text(path, "# Plan\n")
        original = ws._read_shared
        calls = []

        def refuse_once(target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError(13, "sharing violation", target)
            return original(target)

        self.patch_reader(refuse_once)
        self.assertEqual(ws.read_text_strict(path), "# Plan\n")
        self.assertEqual(len(calls), 2)

    def test_a_file_that_stays_refused_is_unreadable(self):
        path = os.path.join(self.project, "plan.md")
        ws.write_text(path, "# Plan\n")

        def refuse(target):
            raise PermissionError(13, "access denied", target)

        self.patch_reader(refuse)
        self.assertIsNone(ws.read_text_strict(path))

    def test_a_file_removed_between_attempts_is_empty(self):
        path = os.path.join(self.project, "plan.md")
        ws.write_text(path, "# Plan\n")

        def remove_and_refuse(target):
            os.unlink(target)
            raise FileNotFoundError(2, "gone", target)

        self.patch_reader(remove_and_refuse)
        self.assertEqual(ws.read_text_strict(path), "")


class TestFileLock(IsolatedCase):
    def test_the_lock_is_exclusive(self):
        path = os.path.join(self.project, "state.json")
        with ws.file_lock(path) as first:
            self.assertTrue(first.acquired)
            with ws.file_lock(path, timeout=0.2) as second:
                self.assertFalse(second.acquired)

    def test_the_lock_is_released_on_exit(self):
        path = os.path.join(self.project, "state.json")
        with ws.file_lock(path):
            pass
        with ws.file_lock(path, timeout=0.2) as again:
            self.assertTrue(again.acquired)

    def test_the_lock_is_released_even_when_the_body_raises(self):
        path = os.path.join(self.project, "state.json")
        with self.assertRaises(RuntimeError):
            with ws.file_lock(path):
                raise RuntimeError("boom")
        with ws.file_lock(path, timeout=0.2) as again:
            self.assertTrue(again.acquired)

    def test_a_stale_lock_is_broken_rather_than_waited_out(self):
        path = os.path.join(self.project, "state.json")
        lock_file = os.path.abspath(path) + ".lock"
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
        with open(lock_file, "w", encoding="utf-8") as handle:
            handle.write("99999")
        old = time.time() - 600
        os.utime(lock_file, (old, old))
        with ws.file_lock(path, timeout=0.5, stale_after=30) as lock:
            self.assertTrue(lock.acquired)

    def test_a_queue_of_holders_does_not_run_the_deadline_down(self):
        """The bound is there to survive a holder that died. A queue moving
        along is not that, and treating it as that made the guarantee depend on
        how many writers were ahead: measured at twelve contenders holding for
        half a second each with a five-second bound, two gave up and clobbered
        each other. Every visible change of hands pushes the deadline out.
        """
        path = os.path.join(self.project, "state.json")
        acquired = []
        # 2.4s of serialised work against a 1s bound, so the queue outlasts the
        # bound more than twice over. The slack between one hold and the bound
        # is what a slow runner has to absorb a stall in: at 0.2s held against
        # 0.5s it was 0.3s, and a Windows runner in CI lost six of twelve
        # contenders to it while the lock itself changed hands every 0.2s.
        hold = 0.1
        contenders = 24

        def worker():
            with ws.file_lock(path, timeout=1.0) as lock:
                acquired.append(lock.acquired)
                time.sleep(hold)

        threads = [threading.Thread(target=worker) for _ in range(contenders)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(acquired, [True] * contenders)

    def test_reading_the_holder_does_not_stop_it_releasing(self):
        """Waiters read the lock file to tell a queue from a corpse, and a
        plain `open` on Windows does not grant delete sharing -- so the read
        stopped the holder unlinking it, and a poll meant to detect progress
        prevented it. Ten of twelve contenders proceeded unlocked.
        """
        path = os.path.join(self.project, "state.json")
        with ws.file_lock(path) as first:
            self.assertTrue(first.acquired)
            lock_file = os.path.abspath(path) + ".lock"

            def reader():
                for _ in range(200):
                    ws.file_lock(path)._holder()

            watcher = threading.Thread(target=reader)
            watcher.start()
            time.sleep(0.05)
        watcher.join(timeout=30)
        self.assertFalse(os.path.exists(lock_file))

    def test_a_lock_that_never_moves_is_still_given_up_on(self):
        """The extension must not turn into waiting for ever. A holder alive
        enough to keep the file fresh and stuck enough never to release it is
        what `max_wait` is for."""
        path = os.path.join(self.project, "state.json")
        lock_file = os.path.abspath(path) + ".lock"
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
        with open(lock_file, "w", encoding="utf-8") as handle:
            handle.write("1:neverreleases")
        started = time.monotonic()
        with ws.file_lock(path, timeout=0.2, stale_after=600, max_wait=1.0) as lock:
            self.assertFalse(lock.acquired)
        self.assertLess(time.monotonic() - started, 30)

    def test_failing_to_lock_does_not_raise(self):
        """Proceeding unlocked beats deadlocking the whole pipeline."""
        path = os.path.join(self.project, "state.json")
        with ws.file_lock(path):
            with ws.file_lock(path, timeout=0.1) as blocked:
                self.assertFalse(blocked.acquired)  # and no exception


class TestConcurrentLedgerUpdates(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()

    def book(self, **overrides):
        settings = dict(ledger_mod.DEFAULT_BUDGETS)
        settings.update(overrides)
        return ledger_mod.Ledger(self.workspace, settings)

    def test_concurrent_consumes_are_not_lost(self):
        """Without the lock, read-modify-write drops one of the two edits."""
        attempts = 12
        errors = []

        def consume():
            try:
                self.book(test=attempts * 2, total_delegated_runs=attempts * 4).consume("test")
            except Exception as exc:  # collected and reported after the join
                errors.append(exc)

        threads = [threading.Thread(target=consume) for _ in range(attempts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        used = self.book().load()["attempts"]["test"]
        self.assertEqual(used, attempts)

    def test_concurrent_in_flight_entries_all_survive(self):
        book = self.book()
        tokens = []
        lock = threading.Lock()

        def begin():
            token = book.begin("implementer", deadline=60)
            with lock:
                tokens.append(token)

        threads = [threading.Thread(target=begin) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(set(tokens)), 8, "tokens must be unique")
        self.assertEqual(len(book.in_flight()), 8, "no entry may be lost")

    def test_concurrent_charges_are_all_added(self):
        """The same read-modify-write hazard, on the runtime accumulator."""
        book = self.book()
        tokens = [book.begin("implementer", deadline=60) for _ in range(8)]
        errors = []

        def end(token):
            try:
                self.book().end(token, "ok", charged_seconds=1)
            except Exception as exc:  # collected and reported after the join
                errors.append(exc)

        threads = [threading.Thread(target=end, args=(token,)) for token in tokens]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(book.load()["runtime_seconds"], 8)
        self.assertEqual(book.in_flight(), {})

    def test_concurrent_ends_all_leave_their_event_behind(self):
        """The other half of the same write: `end` records the charge and its
        event under one hold of the lock. Appending afterwards read the whole
        state back after releasing it, so an event another process had
        committed in between vanished along with its charge."""
        book = self.book()
        tokens = [book.begin("implementer", deadline=60) for _ in range(8)]
        errors = []

        def end(token):
            try:
                self.book().end(token, "ok", charged_seconds=1)
            except Exception as exc:  # collected and reported after the join
                errors.append(exc)

        threads = [threading.Thread(target=end, args=(token,)) for token in tokens]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        events = self.workspace.read_state().get("events") or []
        ended = [event for event in events if event.get("stage") == "implementer"]
        self.assertEqual(len(ended), 8, "no event may be lost")

    def test_in_flight_tokens_do_not_collide_when_started_together(self):
        book = self.book()
        tokens = {book.begin("test", deadline=1) for _ in range(20)}
        self.assertEqual(len(tokens), 20)

    def test_the_state_file_is_always_readable_while_being_churned(self):
        """Read it the way the code does -- that is the guarantee we ship."""
        book = self.book(test=500, total_delegated_runs=900)
        book.consume("test")  # make sure the file exists before reading it
        stop = threading.Event()
        bad = []

        def churn():
            while not stop.is_set():
                book.consume("test")

        thread = threading.Thread(target=churn, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 1.5
            reads = 0
            while time.monotonic() < deadline:
                state = ws.read_json(self.workspace.state_path, "UNREADABLE")
                reads += 1
                if not isinstance(state, dict) or "ledger" not in state:
                    bad.append(state if state == "UNREADABLE" else "no ledger key")
                time.sleep(0.001)
        finally:
            stop.set()
            thread.join(timeout=5)

        self.assertGreater(reads, 20)
        self.assertEqual(bad, [], "%d of %d reads came back unusable" % (len(bad), reads))


class TestConcurrentTriage(IsolatedCase):
    """#262: triage calls run side by side each read the report, set their own
    decision and wrote it back, and every one printed success while the last
    write dropped the others."""

    FINDINGS = 8

    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()
        self.findings = findings = [
            {
                "reviewer": "r1",
                "severity": "high",
                "file": "file%d.py" % index,
                "line": "1",
                "category": "correctness",
                "problem": "problem number %d" % index,
                "impact": "",
                "evidence": "",
                "recommended_fix": "",
            }
            for index in range(self.FINDINGS)
        ]
        data = review_mod.build_consolidation(self.workspace, [], findings)
        review_mod.save_consolidation(self.workspace, data)

    def test_concurrent_triage_keeps_every_decision(self):
        real = review_mod.set_triage

        def slow(*args, **kwargs):
            # Widens the window between the read and the write, so the race
            # the lock closes is not left to the scheduler.
            time.sleep(0.05)
            return real(*args, **kwargs)

        codes = []
        errors = []

        def triage(finding_id):
            try:
                args = cli.build_parser().parse_args(["review", "triage", finding_id, "--status", "accepted"])
                codes.append(cli_review.cmd_review_triage(args))
            except Exception as exc:  # collected and reported after the join
                errors.append(exc)

        ids = ["F%d" % index for index in range(1, self.FINDINGS + 1)]
        threads = [threading.Thread(target=triage, args=(finding_id,)) for finding_id in ids]
        with mock.patch.object(review_mod, "set_triage", slow), redirect_stdout(io.StringIO()):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(codes, [0] * self.FINDINGS)
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertEqual(sorted(f["id"] for f in review_mod.accepted_findings(data)), sorted(ids))

    def test_the_review_commands_save_the_report_only_under_its_lock(self):
        """A round read the last report's triage, then built and saved a new
        one; a triage landing in between was lost unless the read and the
        save share the lock. Every save goes through ``update_consolidation``,
        never a bare ``save_consolidation`` a later caller could take
        unlocked."""
        from orchestrator import review_consolidation

        findings = [dict(f) for f in self.findings]
        inside = threading.local()
        saves = []
        real_update = review_consolidation.update_consolidation
        real_save = review_consolidation.save_consolidation

        def update(*args, **kwargs):
            outer = getattr(inside, "held", False)
            inside.held = True
            try:
                return real_update(*args, **kwargs)
            finally:
                inside.held = outer

        def save(*args, **kwargs):
            saves.append(getattr(inside, "held", False))
            return real_save(*args, **kwargs)

        commands = (
            (["review", "consolidate"], cli_review.cmd_review_consolidate),
            (["review", "triage", "F1", "--status", "accepted"], cli_review.cmd_review_triage),
        )
        with (
            mock.patch.object(review_mod, "read_reports", lambda *args, **kwargs: (findings, [])),
            mock.patch.object(review_consolidation, "update_consolidation", update),
            mock.patch.object(review_mod, "update_consolidation", update),
            mock.patch.object(review_consolidation, "save_consolidation", save),
            mock.patch.object(review_mod, "save_consolidation", save),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            for argv, command in commands:
                before = len(saves)
                self.assertEqual(command(cli.build_parser().parse_args(argv)), 0, argv)
                self.assertGreater(len(saves), before, argv)
        self.assertTrue(all(saves), saves)
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertEqual([f["id"] for f in review_mod.accepted_findings(data)], ["F1"])

    def test_a_triage_made_while_a_round_is_built_survives_it(self):
        """``review consolidate`` is held after it has read the last report
        and built the new one, but before it saves; ``review triage`` starts
        then. The triage must wait for the save and land on the new report,
        not be written over by a report built from what was read before it."""
        findings = [dict(f) for f in self.findings]
        real_build = review_mod.build_consolidation
        built = threading.Event()
        triage_started = threading.Event()
        results = {}

        def build(*args, **kwargs):
            data = real_build(*args, **kwargs)
            built.set()
            self.assertTrue(triage_started.wait(timeout=30))
            # Long enough for an unlocked triage to read, decide and save.
            time.sleep(0.5)
            return data

        def run(name, argv, command):
            try:
                results[name] = command(cli.build_parser().parse_args(argv))
            except Exception as exc:  # reported after the join
                results[name] = exc

        consolidate = threading.Thread(
            target=run, args=("consolidate", ["review", "consolidate"], cli_review.cmd_review_consolidate)
        )
        triage = threading.Thread(
            target=run,
            args=("triage", ["review", "triage", "F1", "--status", "accepted"], cli_review.cmd_review_triage),
        )
        with (
            mock.patch.object(review_mod, "read_reports", lambda *args, **kwargs: (findings, [])),
            mock.patch.object(review_mod, "build_consolidation", build),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            consolidate.start()
            self.assertTrue(built.wait(timeout=30))
            triage.start()
            triage_started.set()
            consolidate.join(timeout=60)
            triage.join(timeout=60)
        self.assertFalse(consolidate.is_alive() or triage.is_alive())
        self.assertEqual(results, {"consolidate": 0, "triage": 0})
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertEqual([f["id"] for f in review_mod.accepted_findings(data)], ["F1"])

    def test_no_writer_can_save_over_a_report_it_did_not_read_under_the_lock(self):
        """The lock is taken where the report is owned, so a caller cannot
        forget it: an update waits for one in progress."""
        from orchestrator import review_consolidation

        order = []
        inside = threading.Event()

        def first(data):
            inside.set()
            time.sleep(0.3)
            order.append("first")
            return review_mod.set_triage(data, "F1", "accepted")

        def second(data):
            order.append("second")
            return review_mod.set_triage(data, "F2", "rejected")

        one = threading.Thread(target=review_consolidation.update_consolidation, args=(self.workspace, first))
        one.start()
        self.assertTrue(inside.wait(timeout=30))
        review_consolidation.update_consolidation(self.workspace, second)
        one.join(timeout=30)
        self.assertFalse(one.is_alive())
        self.assertEqual(order, ["first", "second"])
        data = ws.read_json(self.workspace.consolidated_json_path, {})
        self.assertEqual({f["id"]: f["triage"] for f in data["findings"]}["F1"], "accepted")
        self.assertEqual({f["id"]: f["triage"] for f in data["findings"]}["F2"], "rejected")


if __name__ == "__main__":
    unittest.main()
