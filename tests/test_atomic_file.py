"""The shared write through a sibling temporary file and one rename."""

from __future__ import annotations

import os
import sys
import time
import unittest
from typing import List, Tuple
from unittest import mock

from helpers import IsolatedCase

from orchestrator import atomic_file


class AtomicFileCase(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.directory = os.path.join(self.tmp, "files")
        self.path = os.path.join(self.directory, "out.txt")

    def read_bytes(self, path: str = "") -> bytes:
        with open(path or self.path, "rb") as handle:
            return handle.read()

    def write_bytes(self, content: bytes) -> None:
        os.makedirs(self.directory, exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(content)

    def others(self) -> List[str]:
        """What the directory holds besides the file written."""
        return sorted(name for name in os.listdir(self.directory) if name != os.path.basename(self.path))

    def recording_replace(self) -> List[Tuple[str, str]]:
        """Patches ``rename_over`` to record its arguments and then rename; returns the record."""
        real_replace = atomic_file.rename_over
        calls: List[Tuple[str, str]] = []

        def replace(src, dst):
            calls.append((src, dst))
            return real_replace(src, dst)

        self.enterContext(mock.patch.object(atomic_file, "rename_over", replace))
        return calls

    def temporary_with(self, content: bytes) -> str:
        """A file beside the target holding ``content``, as ``write`` would make."""
        os.makedirs(self.directory, exist_ok=True)
        temporary = os.path.join(self.directory, "temporary")
        with open(temporary, "wb") as handle:
            handle.write(content)
        return temporary


class TestWrite(AtomicFileCase):
    def test_writes_bytes_and_makes_the_directory(self):
        atomic_file.write(self.path, b"content")
        self.assertEqual(self.read_bytes(), b"content")
        self.assertEqual(self.others(), [])

    def test_a_bare_name_is_written_in_the_current_directory(self):
        os.makedirs(self.directory)
        previous = os.getcwd()
        os.chdir(self.directory)
        try:
            atomic_file.write("out.txt", b"content")
        finally:
            os.chdir(previous)
        self.assertEqual(self.read_bytes(), b"content")
        self.assertEqual(self.others(), [])

    def test_a_str_is_utf8_with_its_newlines_as_they_are(self):
        atomic_file.write(self.path, "café\r\nb\n")
        self.assertEqual(self.read_bytes(), "café\r\nb\n".encode("utf-8"))

    def test_the_temporary_file_is_named_after_the_file_by_default(self):
        calls = self.recording_replace()
        atomic_file.write(self.path, b"content")
        ((temporary, target),) = calls
        self.assertEqual(target, self.path)
        self.assertEqual(os.path.dirname(temporary), self.directory)
        name = os.path.basename(temporary)
        self.assertTrue(name.startswith(".out.txt.") and name.endswith(".tmp"), name)

    def test_an_explicit_prefix_and_suffix(self):
        calls = self.recording_replace()
        atomic_file.write(self.path, b"content", prefix=".tmp-", suffix=".part")
        name = os.path.basename(calls[0][0])
        self.assertTrue(name.startswith(".tmp-") and name.endswith(".part"), name)

    def test_durable_flushes_to_disk_and_the_default_does_not(self):
        with mock.patch.object(os, "fsync", wraps=os.fsync) as fsync:
            atomic_file.write(self.path, b"plain")
            fsync.assert_not_called()
            atomic_file.write(self.path, b"durable", durable=True)
            fsync.assert_called_once()
        self.assertEqual(self.read_bytes(), b"durable")

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_keep_mode_copies_the_replaced_files_mode(self):
        self.write_bytes(b"old")
        os.chmod(self.path, 0o640)
        atomic_file.write(self.path, b"kept", keep_mode=True)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)
        atomic_file.write(self.path, b"private")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_keep_mode_with_no_file_to_replace(self):
        atomic_file.write(self.path, b"new", keep_mode=True)
        self.assertEqual(self.read_bytes(), b"new")
        self.assertEqual(self.others(), [])
        if os.name != "nt":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_follow_link_replaces_the_target(self):
        target = os.path.join(self.tmp, "elsewhere", "real.txt")
        os.makedirs(os.path.dirname(target))
        with open(target, "wb") as handle:
            handle.write(b"old")
        os.makedirs(self.directory)
        try:
            os.symlink(target, self.path)
        except (OSError, NotImplementedError):
            self.skipTest("cannot make a file symlink here")
        atomic_file.write(self.path, b"new", follow_link=True)
        self.assertTrue(os.path.islink(self.path))
        self.assertEqual(self.read_bytes(target), b"new")
        self.assertEqual(sorted(os.listdir(os.path.dirname(target))), ["real.txt"])
        atomic_file.write(self.path, b"plain")
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual(self.read_bytes(target), b"new")

    def test_follow_link_creates_a_missing_target(self):
        """A dangling link's target is made, its directory too, and the link stays."""
        target = os.path.join(self.tmp, "elsewhere", "real.txt")
        os.makedirs(self.directory)
        try:
            os.symlink(target, self.path)
        except (OSError, NotImplementedError):
            self.skipTest("cannot make a file symlink here")
        atomic_file.write(self.path, b"new", follow_link=True)
        self.assertTrue(os.path.islink(self.path))
        self.assertEqual(self.read_bytes(target), b"new")
        self.assertEqual(self.read_bytes(), b"new")
        self.assertEqual(sorted(os.listdir(os.path.dirname(target))), ["real.txt"])
        self.assertEqual(self.others(), [])

    def test_expected_bytes_that_match_are_replaced(self):
        self.write_bytes(b"old")
        atomic_file.write(self.path, b"new", expected=b"old")
        self.assertEqual(self.read_bytes(), b"new")

    def test_expected_bytes_that_changed_raise_raced(self):
        self.write_bytes(b"changed")
        with self.assertRaises(atomic_file.Raced):
            atomic_file.write(self.path, b"new", expected=b"old")
        self.assertEqual(self.read_bytes(), b"changed")
        self.assertEqual(self.others(), [])

    def test_expected_bytes_of_a_file_since_removed_raise_raced(self):
        with self.assertRaises(atomic_file.Raced):
            atomic_file.write(self.path, b"new", expected=b"old")
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.others(), [])

    def test_expected_none_means_no_file(self):
        atomic_file.write(self.path, b"created", expected=None)
        self.assertEqual(self.read_bytes(), b"created")
        with self.assertRaises(atomic_file.Raced):
            atomic_file.write(self.path, b"again", expected=None)
        self.assertEqual(self.read_bytes(), b"created")

    def test_a_given_replace_moves_the_temporary_file(self):
        calls: List[Tuple[str, str]] = []

        def replace(temporary: str, target: str) -> None:
            calls.append((temporary, target))
            os.replace(temporary, target)

        atomic_file.write(self.path, b"content", replace=replace)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], self.path)
        self.assertEqual(self.read_bytes(), b"content")

    def test_any_exception_removes_the_temporary_file(self):
        self.write_bytes(b"old")

        def interrupted(temporary: str, target: str) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            atomic_file.write(self.path, b"new", replace=interrupted)
        with self.assertRaises(UnicodeEncodeError):
            atomic_file.write(self.path, "\ud800")
        self.assertEqual(self.read_bytes(), b"old")
        self.assertEqual(self.others(), [])

    def test_a_failed_flush_to_disk_removes_the_temporary_file(self):
        self.write_bytes(b"old")
        with mock.patch.object(os, "fsync", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                atomic_file.write(self.path, b"new", durable=True)
        self.assertEqual(self.read_bytes(), b"old")
        self.assertEqual(self.others(), [])


class TestReplaceWithRetry(AtomicFileCase):
    def test_no_attempts_goes_straight_to_writing_in_place(self):
        self.write_bytes(b"old")
        temporary = os.path.join(self.directory, "temporary")
        with open(temporary, "wb") as handle:
            handle.write(b"new")
        calls = self.recording_replace()
        atomic_file.replace_with_retry(temporary, self.path, attempts=0)
        self.assertEqual(calls, [])
        self.assertEqual(self.read_bytes(), b"new")
        self.assertEqual(self.others(), [])

    def test_no_attempts_and_a_failed_write_in_place_return_quietly(self):
        """With no rename tried there is no error to raise, so nothing is; the
        target keeps its content and the temporary file is left to the caller."""
        self.write_bytes(b"old")
        temporary = os.path.join(self.directory, "temporary")
        with open(temporary, "wb") as handle:
            handle.write(b"new")
        with mock.patch.object(atomic_file, "open", side_effect=PermissionError(13, "denied"), create=True):
            atomic_file.replace_with_retry(temporary, self.path, attempts=0)
        self.assertEqual(self.read_bytes(), b"old")
        self.assertEqual(self.others(), ["temporary"])


class TestReplaceWithRetryWithoutFallback(AtomicFileCase):
    def test_a_refused_rename_is_raised_and_the_target_is_left_alone(self):
        self.write_bytes(b"old")
        temporary = os.path.join(self.directory, "temporary")
        with open(temporary, "wb") as handle:
            handle.write(b"new")
        with mock.patch.object(
            atomic_file, "rename_over", side_effect=PermissionError(13, "denied")
        ) as replace:
            with mock.patch("time.sleep"):
                with self.assertRaises(PermissionError):
                    atomic_file.replace_with_retry(temporary, self.path, attempts=3, fallback=False)
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(self.read_bytes(), b"old")
        self.assertEqual(self.others(), ["temporary"])


class TestRenameOver(AtomicFileCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(atomic_file, "_NO_POSIX_RENAME", set()))
        os.makedirs(self.directory)

    def on(self, windows: bool, renamed: object = None) -> Tuple[mock.MagicMock, mock.MagicMock]:
        """Pretend to run on Windows or not; returns the mocked handle rename and
        ``os.replace``. ``renamed`` is the handle rename's return or side effect."""
        self.enterContext(mock.patch.object(atomic_file, "_IS_WINDOWS", windows))
        if isinstance(renamed, list):
            by_handle = mock.MagicMock(side_effect=renamed)
        else:
            by_handle = mock.MagicMock(return_value=renamed)
        self.enterContext(mock.patch.object(atomic_file, "_rename_by_handle", by_handle))
        replace = self.enterContext(mock.patch.object(os, "replace"))
        return by_handle, replace

    def test_posix_uses_os_replace(self):
        by_handle, replace = self.on(windows=False)
        atomic_file.rename_over("temporary", self.path)
        replace.assert_called_once_with("temporary", self.path)
        by_handle.assert_not_called()

    def test_windows_takes_the_handle_rename(self):
        by_handle, replace = self.on(windows=True, renamed=True)
        atomic_file.rename_over("temporary", self.path)
        by_handle.assert_called_once_with("temporary", self.path)
        replace.assert_not_called()

    def test_unsupported_falls_back_and_is_remembered(self):
        by_handle, replace = self.on(windows=True, renamed=False)
        atomic_file.rename_over("temporary", self.path)
        atomic_file.rename_over("temporary", self.path)
        self.assertEqual(by_handle.call_count, 1)
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(atomic_file._NO_POSIX_RENAME, {os.stat(self.directory).st_dev})

    def test_only_the_refusing_volume_is_remembered(self):
        """A folder on another volume -- a mounted stick, a junction to a
        share -- does not mark the drive it sits under as unsupported."""
        other = os.path.join(self.tmp, "mounted", "out.txt")
        volumes = {self.path: 1, other: 2}
        self.enterContext(mock.patch.object(atomic_file, "_volume_of", side_effect=volumes.get))
        by_handle, replace = self.on(windows=True, renamed=[False, True, True])
        atomic_file.rename_over("temporary", other)
        atomic_file.rename_over("temporary", self.path)
        atomic_file.rename_over("temporary", self.path)
        self.assertEqual(by_handle.call_count, 3)
        replace.assert_called_once_with("temporary", other)
        self.assertEqual(atomic_file._NO_POSIX_RENAME, {2})

    def test_an_unknown_volume_is_not_remembered(self):
        self.enterContext(mock.patch.object(atomic_file, "_volume_of", return_value=None))
        by_handle, replace = self.on(windows=True, renamed=False)
        atomic_file.rename_over("temporary", self.path)
        atomic_file.rename_over("temporary", self.path)
        self.assertEqual(by_handle.call_count, 2)
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(atomic_file._NO_POSIX_RENAME, set())

    def test_a_sharing_violation_reaches_the_retry(self):
        refused = PermissionError(13, "sharing violation")
        by_handle, replace = self.on(windows=True, renamed=[refused, refused, True])
        with mock.patch("time.sleep") as sleep:
            atomic_file.replace_with_retry("temporary", self.path, fallback=False)
        self.assertEqual(by_handle.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        replace.assert_not_called()
        # A refusal is momentary, not "unsupported": the volume keeps the handle rename.
        self.assertEqual(atomic_file._NO_POSIX_RENAME, set())


@unittest.skipUnless(sys.platform == "win32", "the handle rename is Windows only")
class TestRenameByHandle(AtomicFileCase):
    def test_the_handle_rename_replaces(self):
        """A real call: a layout mistake in FILE_RENAME_INFO comes back as
        unsupported and would silently fall back, so False fails here."""
        self.write_bytes(b"old")
        temporary = self.temporary_with(b"new")
        self.assertIs(atomic_file._rename_by_handle(temporary, self.path), True)
        self.assertEqual(self.read_bytes(), b"new")
        self.assertEqual(self.others(), [])

    def test_the_handle_rename_creates_a_missing_target(self):
        temporary = self.temporary_with(b"new")
        self.assertIs(atomic_file._rename_by_handle(temporary, self.path), True)
        self.assertEqual(self.read_bytes(), b"new")
        self.assertEqual(self.others(), [])

    def test_a_name_past_the_basic_plane(self):
        """A character past U+FFFF takes two UTF-16 units; a FileNameLength
        counted in characters would come back as unsupported here."""
        self.path = os.path.join(self.directory, "out-\U0001f600.txt")
        temporary = self.temporary_with(b"new")
        self.assertIs(atomic_file._rename_by_handle(temporary, self.path), True)
        self.assertEqual(self.read_bytes(), b"new")
        self.assertEqual(self.others(), [])

    def test_a_missing_temporary_file_is_file_not_found(self):
        os.makedirs(self.directory)
        with self.assertRaises(FileNotFoundError):
            atomic_file._rename_by_handle(os.path.join(self.directory, "temporary"), self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_a_failed_open_closes_nothing(self):
        # Windows only: ctypes.set_last_error exists nowhere else.
        import ctypes

        def missing(*args):
            ctypes.set_last_error(2)
            return ctypes.c_void_p(-1).value

        kernel32 = mock.MagicMock()
        kernel32.CreateFileW.side_effect = missing
        with mock.patch.object(atomic_file, "_kernel32", return_value=kernel32):
            with self.assertRaises(FileNotFoundError) as caught:
                atomic_file._rename_by_handle("temporary", self.path)
        self.assertEqual(getattr(caught.exception, "winerror", None), 2)
        kernel32.SetFileInformationByHandle.assert_not_called()
        kernel32.CloseHandle.assert_not_called()

    def test_a_delete_sharing_reader_does_not_block(self):
        """Called directly, not through ``replace_with_retry``. Antivirus can
        hold the fresh temporary file for a moment, so a refusal is tried
        again a few times; a reader that blocked the rename would refuse
        every time and still fail the test."""
        # Windows only: msvcrt exists nowhere else.
        import ctypes
        import msvcrt

        self.write_bytes(b"old")
        temporary = self.temporary_with(b"new")
        GENERIC_READ = 0x80000000
        FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004  # read | write | delete
        OPEN_EXISTING = 3
        handle = atomic_file._kernel32().CreateFileW(
            self.path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING, 0x80, None
        )
        self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
        with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb") as held:
            renamed = None
            for attempt in range(10):
                try:
                    renamed = atomic_file._rename_by_handle(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.5)
            self.assertIs(renamed, True)
            self.assertEqual(held.read(), b"old")
        self.assertEqual(self.read_bytes(), b"new")
        self.assertEqual(self.others(), [])

    def test_a_plain_reader_is_a_permission_error(self):
        self.write_bytes(b"old")
        temporary = self.temporary_with(b"new")
        with open(self.path, "rb"):
            with self.assertRaises(PermissionError):
                atomic_file._rename_by_handle(temporary, self.path)
        self.assertEqual(self.read_bytes(), b"old")

    def test_the_error_code_maps_to_the_fallback(self):
        # Windows only: ctypes.set_last_error exists nowhere else.
        import ctypes

        for code in (1, 50, 87):
            with self.subTest(code=code):

                def refuse(*args, code=code):
                    ctypes.set_last_error(code)
                    return 0

                kernel32 = mock.MagicMock()
                kernel32.CreateFileW.return_value = 1234
                kernel32.SetFileInformationByHandle.side_effect = refuse
                with mock.patch.object(atomic_file, "_kernel32", return_value=kernel32):
                    self.assertIs(atomic_file._rename_by_handle("temporary", self.path), False)
                kernel32.CloseHandle.assert_called_once_with(1234)

    def test_another_error_code_is_raised(self):
        # Windows only: ctypes.set_last_error exists nowhere else.
        import ctypes

        def refuse(*args):
            ctypes.set_last_error(32)
            return 0

        kernel32 = mock.MagicMock()
        kernel32.CreateFileW.return_value = 1234
        kernel32.SetFileInformationByHandle.side_effect = refuse
        with mock.patch.object(atomic_file, "_kernel32", return_value=kernel32):
            with self.assertRaises(PermissionError) as caught:
                atomic_file._rename_by_handle("temporary", self.path)
        self.assertEqual(getattr(caught.exception, "winerror", None), 32)
        kernel32.CloseHandle.assert_called_once_with(1234)


class TestCurrentBytes(AtomicFileCase):
    def test_the_bytes_or_none(self):
        self.assertIsNone(atomic_file.current_bytes(self.path))
        self.write_bytes(b"a\r\n")
        self.assertEqual(atomic_file.current_bytes(self.path), b"a\r\n")


if __name__ == "__main__":
    unittest.main()
