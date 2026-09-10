"""Shared-file safety, once detached workers exist.

CI found this the hard way: a `jobs wait` failed on one platform because the
worker was rewriting the job file while the parent read it. A truncate window
produced a parse error, and returning the default turned "unreadable" into
"absent" -- so a live job looked like a missing one.
"""

from __future__ import annotations

import os
import threading
import time
import unittest

from helpers import IsolatedCase

from orchestrator import ledger as ledger_mod
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

    def test_a_missing_file_yields_the_default_without_retrying(self):
        started = time.monotonic()
        self.assertIsNone(ws.read_json(os.path.join(self.project, "absent.json")))
        self.assertLess(time.monotonic() - started, 0.5)


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

    def test_failing_to_lock_does_not_raise(self):
        """Proceeding unlocked beats deadlocking the whole pipeline."""
        path = os.path.join(self.project, "state.json")
        with ws.file_lock(path):
            with ws.file_lock(path, timeout=0.1) as blocked:
                self.assertFalse(blocked.acquired)  # and no exception


class TestConcurrentLedgerUpdates(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = ws.Workspace(self.project).ensure()

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


if __name__ == "__main__":
    unittest.main()
