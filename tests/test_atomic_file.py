"""The shared write through a sibling temporary file and one rename."""

from __future__ import annotations

import os
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
        """Patches ``os.replace`` to record its arguments and then rename; returns the record."""
        real_replace = os.replace
        calls: List[Tuple[str, str]] = []

        def replace(src, dst):
            calls.append((src, dst))
            return real_replace(src, dst)

        self.enterContext(mock.patch.object(os, "replace", replace))
        return calls


class TestWrite(AtomicFileCase):
    def test_writes_bytes_and_makes_the_directory(self):
        atomic_file.write(self.path, b"content")
        self.assertEqual(self.read_bytes(), b"content")
        self.assertEqual(self.others(), [])

    def test_a_bare_name_is_written_in_the_current_directory(self):
        os.makedirs(self.directory)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.directory)
        atomic_file.write("out.txt", b"content")
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

    def test_a_patched_os_replace_is_the_one_called(self):
        calls = self.recording_replace()
        atomic_file.write(self.path, b"content")
        self.assertEqual(len(calls), 1)

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


class TestCurrentBytes(AtomicFileCase):
    def test_the_bytes_or_none(self):
        self.assertIsNone(atomic_file.current_bytes(self.path))
        self.write_bytes(b"a\r\n")
        self.assertEqual(atomic_file.current_bytes(self.path), b"a\r\n")


if __name__ == "__main__":
    unittest.main()
