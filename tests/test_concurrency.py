"""Shared-file safety, once detached workers exist.

CI found this the hard way: a `jobs wait` failed on one platform because the
worker was rewriting the job file while the parent read it. A truncate window
produced a parse error, and returning the default turned "unreadable" into
"absent" -- so a live job looked like a missing one.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import IsolatedCase

from orchestrator import atomic_file, cli, cli_review
from orchestrator import ledger as ledger_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws


class ReaderCase(IsolatedCase):
    """Helpers for the reader tests: no test of its own."""

    def count_sleeps(self):
        sleeps = []
        patcher = mock.patch.object(ws.time, "sleep", side_effect=sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sleeps

    def patch_reader(self, reader):
        original = ws._read_shared
        ws._read_shared = reader
        self.addCleanup(setattr, ws, "_read_shared", original)

    def forbid_tolerant_read(self):
        """Fail the test if `read_text` reaches its `errors="replace"` read,
        the only plain `open` the readers make."""
        failure = AssertionError("the tolerant read was reached")
        self.enterContext(mock.patch.object(ws, "open", side_effect=failure, create=True))

    def under_a_file(self):
        """A path whose parent is a regular file."""
        parent = os.path.join(self.project, "plain.txt")
        ws.write_text(parent, "plain")
        return os.path.join(parent, "state.json")


class TestAtomicWrites(ReaderCase):
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

    def patch_replace(self, failures, error=None):
        """``atomic_file.rename_over`` raising for the first ``failures`` calls (all of
        them for None); a sharing violation unless ``error`` is given. Returns the calls."""
        real_replace = atomic_file.rename_over
        calls = []

        def replace(src, dst):
            calls.append(dst)
            if failures is None or len(calls) <= failures:
                raise error if error is not None else PermissionError(13, "sharing violation", dst)
            return real_replace(src, dst)

        self.enterContext(mock.patch.object(atomic_file, "rename_over", replace))
        self.enterContext(mock.patch.object(time, "sleep"))
        return calls

    def temp_files(self):
        return [name for name in os.listdir(self.project) if name.startswith(".tmp-")]

    def test_a_refused_rename_is_retried(self):
        path = os.path.join(self.project, "out.txt")
        calls = self.patch_replace(2)
        ws.write_text(path, "content")
        self.assertEqual(len(calls), 3)
        self.assertEqual(ws.read_text(path), "content")
        self.assertEqual(self.temp_files(), [])

    def test_a_rename_refused_every_time_writes_in_place(self):
        """The last resort reads the temporary file back in text mode, so a
        CRLF in the text comes out as LF there and only there."""
        path = os.path.join(self.project, "out.txt")
        ws.write_text(path, "old")
        calls = self.patch_replace(None)
        ws.write_text(path, "a\r\nb")
        self.assertEqual(len(calls), 25)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"a\nb")
        self.assertEqual(self.temp_files(), [])

    def test_a_failed_write_in_place_raises_the_rename_error(self):
        path = os.path.join(self.project, "out.txt")
        ws.write_text(path, "old")
        real_open = open

        def refusing_open(file, mode="r", *args, **kwargs):
            if file == os.path.abspath(path) and "w" in mode:
                raise PermissionError(13, "denied", file)
            return real_open(file, mode, *args, **kwargs)

        self.enterContext(mock.patch.object(atomic_file, "open", refusing_open, create=True))
        calls = self.patch_replace(None)
        with self.assertRaises(PermissionError) as caught:
            ws.write_text(path, "new")
        self.assertEqual(caught.exception.strerror, "sharing violation")
        self.assertEqual(len(calls), 25)
        self.assertEqual(ws.read_text(path), "old")
        self.assertEqual(self.temp_files(), [])

    def test_another_rename_error_is_raised_at_once(self):
        path = os.path.join(self.project, "out.txt")
        ws.write_text(path, "old")
        calls = self.patch_replace(None, error=OSError(28, "disk full"))
        with self.assertRaises(OSError) as caught:
            ws.write_text(path, "new")
        self.assertNotIsInstance(caught.exception, PermissionError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(ws.read_text(path), "old")
        self.assertEqual(self.temp_files(), [])

    def test_crlf_is_written_as_is(self):
        path = os.path.join(self.project, "out.txt")
        ws.write_text(path, "a\r\nb\n")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"a\r\nb\n")

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_a_rewritten_file_is_private(self):
        path = os.path.join(self.project, "out.txt")
        ws.write_text(path, "first")
        os.chmod(path, 0o644)
        ws.write_text(path, "second")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_link_is_replaced_by_a_file(self):
        target = os.path.join(self.project, "target.txt")
        ws.write_text(target, "target")
        path = os.path.join(self.project, "out.txt")
        os.symlink(target, path)
        ws.write_text(path, "new")
        self.assertFalse(os.path.islink(path))
        self.assertEqual(ws.read_text(path), "new")
        self.assertEqual(ws.read_text(target), "target")

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
        calls = []

        def refuse(target):
            calls.append(target)
            raise PermissionError(13, "access denied", target)

        self.patch_reader(refuse)
        sleeps = self.count_sleeps()
        self.assertEqual(ws.read_json(path, "fallback"), "fallback")
        self.assertGreater(len(calls), 1)
        self.assertEqual(len(calls), ws._READ_ATTEMPTS)
        self.assertEqual(len(sleeps), ws._READ_ATTEMPTS - 1)

    def isfile_always_misses(self):
        """`os.path.isfile` is a stat that reports any error as False, and on
        Windows / Python 3.14 it misses a file mid-rename (#358)."""
        patcher = mock.patch.object(ws.os.path, "isfile", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_missing_file_yields_the_default_without_retrying(self):
        sleeps = self.count_sleeps()
        started = time.monotonic()
        self.assertIsNone(ws.read_json(os.path.join(self.project, "absent.json")))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(sleeps, [])

    def test_a_directory_yields_the_default_without_retrying(self):
        sleeps = self.count_sleeps()
        self.assertEqual(ws.read_json(self.project, "fallback"), "fallback")
        self.assertEqual(sleeps, [])

    def test_a_file_deleted_between_attempts_yields_the_default(self):
        path = os.path.join(self.project, "state.json")
        ws.write_json(path, {"ledger": {}})
        original = ws._read_shared
        calls = []

        def refuse_then_vanish(target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError(13, "sharing violation", target)
            raise FileNotFoundError(2, "gone", target)

        ws._read_shared = refuse_then_vanish
        self.addCleanup(setattr, ws, "_read_shared", original)
        sleeps = self.count_sleeps()
        self.assertEqual(ws.read_json(path, "fallback"), "fallback")
        self.assertEqual(len(sleeps), 1)

    def test_a_file_isfile_misses_is_still_read(self):
        path = os.path.join(self.project, "state.json")
        ws.write_json(path, {"ledger": {}})
        self.isfile_always_misses()
        self.assertEqual(ws.read_json(path, "UNREADABLE"), {"ledger": {}})

    def test_a_read_refused_while_isfile_misses_is_retried(self):
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
        self.isfile_always_misses()
        self.assertEqual(ws.read_json(path, "UNREADABLE"), {"ledger": {}})

    def test_read_text_ignores_a_false_isfile(self):
        path = os.path.join(self.project, "report.md")
        ws.write_text(path, "# Report\n")
        self.isfile_always_misses()
        self.assertEqual(ws.read_text(path, "fallback"), "# Report\n")


class TestTextReads(ReaderCase):
    """`read_text` returns its default for "no file here" without trying the
    tolerant read, and falls back to that read for a file that is there but
    refused or undecodable."""

    def test_a_missing_file_is_the_default(self):
        self.forbid_tolerant_read()
        self.assertEqual(ws.read_text(os.path.join(self.project, "absent.md"), "d"), "d")

    def test_a_directory_is_the_default(self):
        self.forbid_tolerant_read()
        self.assertEqual(ws.read_text(self.project, "d"), "d")

    def test_a_path_under_a_file_is_the_default(self):
        path = self.under_a_file()
        self.forbid_tolerant_read()
        self.assertEqual(ws.read_text(path, "d"), "d")

    def test_a_file_vanishing_mid_read_is_the_default(self):
        path = os.path.join(self.project, "report.md")
        ws.write_text(path, "# Report\n")

        def vanish(target):
            os.unlink(target)
            raise FileNotFoundError(2, "gone", target)

        self.patch_reader(vanish)
        self.forbid_tolerant_read()
        self.assertEqual(ws.read_text(path, "d"), "d")

    def test_an_undecodable_read_falls_back_to_the_tolerant_read(self):
        path = os.path.join(self.project, "report.md")
        ws.write_text(path, "# Report\n")

        def undecodable(target):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        self.patch_reader(undecodable)
        self.assertEqual(ws.read_text(path, "d"), "# Report\n")

    def test_a_refused_read_falls_back_to_the_tolerant_read(self):
        path = os.path.join(self.project, "report.md")
        ws.write_text(path, "# Report\n")

        def refuse(target):
            raise PermissionError(13, "sharing violation", target)

        self.patch_reader(refuse)
        self.assertEqual(ws.read_text(path, "d"), "# Report\n")

    def test_a_file_deleted_before_the_fallback_is_the_default(self):
        path = os.path.join(self.project, "report.md")
        ws.write_text(path, "# Report\n")

        def refuse_and_delete(target):
            os.unlink(target)
            raise PermissionError(13, "sharing violation", target)

        self.patch_reader(refuse_and_delete)
        self.assertEqual(ws.read_text(path, "d"), "d")


class TestNonRegularFiles(ReaderCase):
    """A FIFO or device where a file was expected reads as absent. Opened
    plainly, a FIFO with no writer blocks forever and /dev/zero never ends."""

    def assert_absent_to_every_reader(self, path):
        sleeps = self.count_sleeps()
        self.forbid_tolerant_read()
        self.assertEqual(ws.read_json(path, "fallback"), "fallback")
        self.assertEqual(ws.read_text(path, "d"), "d")
        self.assertEqual(ws.read_text_strict(path), "")
        self.assertEqual(sleeps, [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX")
    def test_a_fifo_without_a_writer_is_absent(self):
        path = os.path.join(self.project, "state.json")
        os.mkfifo(path)
        self.assert_absent_to_every_reader(path)

    @unittest.skipUnless(os.path.exists("/dev/zero"), "no /dev/zero")
    def test_a_device_is_absent(self):
        self.assert_absent_to_every_reader("/dev/zero")

    @unittest.skipUnless(sys.platform == "win32", "NUL is a Windows device")
    def test_a_windows_device_is_absent(self):
        self.assert_absent_to_every_reader("NUL")


class TestOpenFailure(IsolatedCase):
    """`_open_failure` turns a Windows error into the error a reader expects;
    pure, so it runs on every platform."""

    def test_each_code_picks_its_subclass(self):
        path = os.path.join(self.tmp, "state.json")
        for code, kind in (
            (2, FileNotFoundError),
            (3, FileNotFoundError),
            (5, PermissionError),
            (32, PermissionError),
            (267, NotADirectoryError),
        ):
            with self.subTest(code=code):
                error = ws._open_failure(path, code)
                self.assertIs(type(error), kind)
                self.assertEqual(error.filename, path)

    def test_access_denied_on_a_directory_is_a_directory_error(self):
        self.assertIs(type(ws._open_failure(self.tmp, 5)), IsADirectoryError)

    def test_access_denied_on_a_file_is_refused(self):
        path = os.path.join(self.tmp, "state.json")
        with open(path, "w") as handle:
            handle.write("{}")
        self.assertIs(type(ws._open_failure(path, 5)), PermissionError)

    def test_an_unknown_code_is_a_plain_os_error(self):
        self.assertIs(type(ws._open_failure(os.path.join(self.tmp, "x"), 1)), OSError)


@unittest.skipUnless(sys.platform == "win32", "the shared open is Windows only")
class TestSharedOpenErrors(ReaderCase):
    """`_read_shared` reports why an open failed, so a reader can tell "not
    found" from "refused" without asking `os.path.isfile`."""

    def test_a_missing_file_is_file_not_found(self):
        with self.assertRaises(FileNotFoundError) as caught:
            ws._read_shared(os.path.join(self.project, "absent.json"))
        self.assertEqual(getattr(caught.exception, "winerror", None), 2)

    def test_a_missing_directory_is_file_not_found(self):
        with self.assertRaises(FileNotFoundError) as caught:
            ws._read_shared(os.path.join(self.project, "absent", "state.json"))
        self.assertEqual(getattr(caught.exception, "winerror", None), 3)

    def test_a_directory_is_a_directory_error(self):
        with self.assertRaises(IsADirectoryError):
            ws._read_shared(self.project)

    def test_a_share_none_holder_is_a_permission_error(self):
        # Windows only: ctypes.WinDLL exists nowhere else.
        import ctypes
        import ctypes.wintypes

        path = os.path.join(self.project, "state.json")
        ws.write_json(path, {"ledger": {}})
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        GENERIC_READ = 0x80000000
        OPEN_EXISTING = 3
        handle = kernel32.CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING, 0x80, None)
        self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
        self.addCleanup(kernel32.CloseHandle, handle)

        with self.assertRaises(PermissionError) as caught:
            ws._read_shared(path)
        self.assertEqual(getattr(caught.exception, "winerror", None), 32)
        with mock.patch.object(ws.time, "sleep"):
            self.assertEqual(ws.read_json(path, "fallback"), "fallback")

    def test_a_path_under_a_file_is_absent(self):
        with self.assertRaises(ws._ABSENT):
            ws._read_shared(self.under_a_file())

    def fail_open_with(self, code):
        """Make every `CreateFileW` fail with Windows error ``code``."""
        # Windows only: ctypes.set_last_error exists nowhere else.
        import ctypes

        def create_file(*args):
            ctypes.set_last_error(code)
            return ctypes.c_void_p(-1).value

        kernel32 = mock.MagicMock()
        kernel32.CreateFileW.side_effect = create_file
        self.enterContext(mock.patch.object(atomic_file, "_kernel32", return_value=kernel32))

    def test_access_denied_on_a_file_is_refused_and_retried(self):
        """Error 5 means a directory only when the path is one; on a file it
        is a refusal, which a reader retries rather than calling absent."""
        path = os.path.join(self.project, "state.json")
        ws.write_json(path, {"ledger": {}})
        self.fail_open_with(5)
        with self.assertRaises(PermissionError) as caught:
            ws._read_shared(path)
        self.assertEqual(getattr(caught.exception, "winerror", None), 5)
        sleeps = self.count_sleeps()
        self.assertEqual(ws.read_json(path, "fallback"), "fallback")
        self.assertEqual(len(sleeps), ws._READ_ATTEMPTS - 1)
        del sleeps[:]
        self.assertIsNone(ws.read_text_strict(path))
        self.assertEqual(len(sleeps), ws._READ_ATTEMPTS - 1)

    def test_access_denied_on_a_vanished_path_is_refused(self):
        """A directory gone before the isdir check is not called a directory."""
        self.fail_open_with(5)
        with self.assertRaises(PermissionError):
            ws._read_shared(os.path.join(self.project, "gone"))

    def test_error_directory_is_not_a_directory_and_absent(self):
        path = os.path.join(self.project, "state.json")
        self.fail_open_with(267)  # ERROR_DIRECTORY
        with self.assertRaises(NotADirectoryError):
            ws._read_shared(path)
        sleeps = self.count_sleeps()
        self.assertEqual(ws.read_json(path, "fallback"), "fallback")
        self.assertEqual(ws.read_text_strict(path), "")
        self.assertEqual(sleeps, [])


class TestStrictTextReads(ReaderCase):
    """`read_text_strict` never guesses: absent is "", unreadable is None."""

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
        calls = []

        def refuse(target):
            calls.append(target)
            raise PermissionError(13, "access denied", target)

        self.patch_reader(refuse)
        sleeps = self.count_sleeps()
        self.assertIsNone(ws.read_text_strict(path))
        self.assertGreater(len(calls), 1)
        self.assertEqual(len(calls), ws._READ_ATTEMPTS)
        self.assertEqual(len(sleeps), ws._READ_ATTEMPTS - 1)

    def test_a_directory_is_empty_without_retrying(self):
        sleeps = self.count_sleeps()
        self.assertEqual(ws.read_text_strict(self.project), "")
        self.assertEqual(sleeps, [])

    def test_a_path_under_a_file_is_empty_without_retrying(self):
        path = self.under_a_file()
        sleeps = self.count_sleeps()
        self.assertEqual(ws.read_text_strict(path), "")
        self.assertEqual(sleeps, [])

    def test_a_file_removed_between_attempts_is_empty(self):
        path = os.path.join(self.project, "plan.md")
        ws.write_text(path, "# Plan\n")

        def remove_and_refuse(target):
            os.unlink(target)
            raise FileNotFoundError(2, "gone", target)

        self.patch_reader(remove_and_refuse)
        self.assertEqual(ws.read_text_strict(path), "")

    def test_a_file_isfile_misses_is_still_read(self):
        path = os.path.join(self.project, "plan.md")
        ws.write_text(path, "# Plan\n")
        with mock.patch.object(ws.os.path, "isfile", return_value=False):
            self.assertEqual(ws.read_text_strict(path), "# Plan\n")

    def test_a_read_refused_while_isfile_misses_is_unreadable_not_absent(self):
        path = os.path.join(self.project, "plan.md")
        ws.write_text(path, "# Plan\n")

        calls = []

        def refuse(target):
            calls.append(target)
            raise PermissionError(13, "access denied", target)

        self.patch_reader(refuse)
        sleeps = self.count_sleeps()
        with mock.patch.object(ws.os.path, "isfile", return_value=False):
            self.assertIsNone(ws.read_text_strict(path))
        self.assertEqual(len(calls), ws._READ_ATTEMPTS)
        self.assertEqual(len(sleeps), ws._READ_ATTEMPTS - 1)


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
        waited = []
        # Name why a contender gave up, should one ever do so: how long it
        # waited, and whether a holder could not remove the lock file (a virus
        # scanner holding a file just created is one way that happens, #358).
        unlink_failures = []
        unlink = os.unlink

        def recording_unlink(target, *args, **kwargs):
            try:
                return unlink(target, *args, **kwargs)
            except OSError as exc:
                if str(target).endswith(".lock"):
                    code = getattr(exc, "winerror", None)
                    unlink_failures.append("%s(winerror=%s)" % (type(exc).__name__, code))
                raise

        def worker():
            started = time.monotonic()
            with ws.file_lock(path, timeout=1.0) as lock:
                acquired.append(lock.acquired)
                if not lock.acquired:
                    waited.append(round(time.monotonic() - started, 2))
                time.sleep(hold)

        with mock.patch.object(ws.os, "unlink", recording_unlink):
            threads = [threading.Thread(target=worker) for _ in range(contenders)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        self.assertEqual(
            acquired,
            [True] * contenders,
            "gave up after waiting %s s; lock file removals that failed: %s"
            % (waited, ", ".join(unlink_failures) or "none"),
        )

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
        failures = []
        original = ws._read_shared

        def read_and_record(target):
            # Name why an open failed, should a read ever come back unusable.
            try:
                return original(target)
            except OSError as exc:
                if target != self.workspace.state_path:
                    raise
                failures.append("%s(winerror=%s)" % (type(exc).__name__, getattr(exc, "winerror", None)))
                raise

        ws._read_shared = read_and_record
        self.addCleanup(setattr, ws, "_read_shared", original)

        def churn():
            while not stop.is_set():
                book.consume("test")

        thread = threading.Thread(target=churn, daemon=True)
        thread.start()
        try:
            # At least 1.5s and more than 20 reads. A virus scanner opening the
            # file just renamed into place can hold every open of it for a few
            # seconds (measured: 2.5s under ESET), so the count is waited for
            # rather than demanded of a fixed window; the cap still catches a
            # reader that is stuck.
            started = time.monotonic()
            reads = 0
            while time.monotonic() - started < 1.5 or (reads <= 20 and time.monotonic() - started < 15):
                state = ws.read_json(self.workspace.state_path, "UNREADABLE")
                reads += 1
                if not isinstance(state, dict) or "ledger" not in state:
                    bad.append(state if state == "UNREADABLE" else "no ledger key")
                time.sleep(0.001)
        finally:
            stop.set()
            thread.join(timeout=5)

        self.assertGreater(reads, 20)
        self.assertEqual(
            bad,
            [],
            "%d of %d reads came back unusable; open failures seen: %s"
            % (len(bad), reads, ", ".join(failures) or "none"),
        )


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
