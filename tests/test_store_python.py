"""Where a Microsoft Store Python really keeps dev-orchestra's files (#235).

The redirect is faked: ``config.real_location`` maps the config home into a
package folder and ``config.on_windows`` says yes, so this runs anywhere.
"""

from __future__ import annotations

import os
import unittest
from typing import Any, Dict
from unittest import mock

from helpers import IsolatedCase
from test_cli import UNLISTED_CLAUDE, pretend_claude_is_installed, pretend_claude_version, record_resume_pass

from orchestrator import config as config_mod
from orchestrator import doctor as doctor_mod

# Assembled at run time: the repository may not contain a literal profile path.
PROFILE = "C:" + r"\Users\u"
#: The interpreter from the issue, as Windows spells it.
ISSUE_EXECUTABLE = (
    PROFILE + r"\AppData\Local\Microsoft\WindowsApps"
    r"\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe"
)
STORE_PREFIX = (
    r"C:\Program Files\WindowsApps"
    r"\PythonSoftwareFoundation.Python.3.13_3.13.2032.0_x64__qbz5n2kfra8p0"
)
PYTHON_ORG = PROFILE + r"\AppData\Local\Programs\Python\Python313"


class TestStorePython(unittest.TestCase):
    def test_the_issue_interpreter_is_the_store_package(self):
        self.assertTrue(doctor_mod._is_store_path((ISSUE_EXECUTABLE, PYTHON_ORG)))
        self.assertTrue(doctor_mod._is_store_path((ISSUE_EXECUTABLE.upper(), PYTHON_ORG)))
        forward = ISSUE_EXECUTABLE.replace("\\", "/")
        self.assertTrue(doctor_mod._is_store_path((forward, PYTHON_ORG)))

    def test_a_venv_made_from_it_is_too(self):
        venv = r"C:\work\project\.venv\Scripts\python.exe"
        self.assertTrue(doctor_mod._is_store_path((venv, STORE_PREFIX)))

    def test_a_python_org_install_is_not(self):
        executable = PYTHON_ORG + r"\python.exe"
        self.assertFalse(doctor_mod._is_store_path((executable, PYTHON_ORG)))

    def test_a_python_from_another_package_counts_too(self):
        """Windows redirects every packaged app the same way, not only this one."""
        other = r"C:\Program Files\WindowsApps\Contoso.Python_1.0.0.0_x64__abc\python.exe"
        self.assertTrue(doctor_mod._is_store_path((other, PYTHON_ORG)))

    def test_empty_values_are_not(self):
        self.assertFalse(doctor_mod._is_store_path(("", None)))
        self.assertFalse(doctor_mod._is_store_path(()))

    def test_it_reads_this_interpreter(self):
        with mock.patch.object(config_mod, "on_windows", return_value=True):
            with mock.patch.object(doctor_mod.sys, "executable", ISSUE_EXECUTABLE):
                self.assertTrue(doctor_mod.store_python())
            with mock.patch.object(doctor_mod.sys, "executable", PYTHON_ORG + r"\python.exe"):
                with mock.patch.object(doctor_mod.sys, "base_prefix", STORE_PREFIX):
                    self.assertTrue(doctor_mod.store_python())
                with mock.patch.object(doctor_mod.sys, "base_prefix", PYTHON_ORG):
                    self.assertFalse(doctor_mod.store_python())

    def test_off_windows_never(self):
        with mock.patch.object(config_mod, "on_windows", return_value=False):
            with mock.patch.object(doctor_mod.sys, "executable", ISSUE_EXECUTABLE):
                with mock.patch.object(doctor_mod.sys, "base_prefix", STORE_PREFIX):
                    self.assertFalse(doctor_mod.store_python())


class DoctorCase(IsolatedCase):
    """A config file, a user adapter that loads and one that fails, and a
    resume record: one of everything doctor names a path for."""

    store = False

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(doctor_mod, "store_python", lambda: self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def populate(self):
        config_mod.write_config_file(config_mod.global_config_path(), {"version": 1}, "global")
        self.adapter = self.write_user_provider("mycli")
        self.broken = self.write_user_provider("broken", "raise RuntimeError('boom')\n")
        self.load_user_providers()
        pretend_claude_is_installed(self)
        pretend_claude_version(self, UNLISTED_CLAUDE)
        record_resume_pass(self.project)

    def collect(self) -> Dict[str, Any]:
        return doctor_mod.collect(self.project)

    def assert_no_real_keys(self, report):
        self.assertNotIn("store_python", report["platform"])
        self.assertNotIn("global_real", report["config"])
        self.assertNotIn("directory_real", report["user_providers"])
        for item in report["user_providers"]["loaded"] + report["user_providers"]["errors"]:
            self.assertNotIn("path_real", item)
        for entry in report["providers"].values():
            self.assertNotIn("record_real", entry.get("resume_support") or {})
        for note in report["notes"]:
            self.assertNotIn("stored at", note)
            self.assertNotIn("really at", note)


class TestNothingRedirected(DoctorCase):
    def test_no_key_and_no_note(self):
        self.populate()
        # Pinned rather than left to the machine: under a Store Python the
        # temp folder this test writes to can itself be redirected.
        with mock.patch.object(config_mod, "on_windows", return_value=True):
            with mock.patch.object(config_mod, "real_location", lambda path: path):
                report = self.collect()
        self.assertEqual(report["providers"]["claude"]["resume_support"]["source"], "record")
        self.assertEqual(len(report["user_providers"]["loaded"]), 1)
        self.assertEqual(len(report["user_providers"]["errors"]), 1)
        self.assert_no_real_keys(report)
        text = doctor_mod.render(report)
        self.assertNotIn("stored at", text)
        self.assertNotIn("Microsoft Store", text)


class TestRedirected(DoctorCase):
    store = True

    def setUp(self):
        super().setUp()
        self.real = self.redirect_config_home()
        self.home = config_mod.global_config_dir()

    def moved(self, path):
        self.assertTrue(path.startswith(self.home), path)
        return self.real + path[len(self.home) :]

    def test_every_path_says_where_it_really_is(self):
        self.populate()
        report = self.collect()
        path = config_mod.global_config_path()
        record = report["providers"]["claude"]["resume_support"]
        self.assertIs(report["platform"]["store_python"], True)
        self.assertEqual(report["config"]["global_real"], self.moved(path))
        user = report["user_providers"]
        self.assertEqual(user["directory_real"], os.path.join(self.real, "providers"))
        self.assertEqual(user["loaded"][0]["path_real"], self.moved(self.adapter))
        self.assertEqual(user["errors"][0]["path_real"], self.moved(self.broken))
        self.assertEqual(record["record_real"], self.moved(record["record"]))

        text = doctor_mod.render(report)
        self.assertIn("/ Python %s (Microsoft Store package)\n" % report["platform"]["python"], text)
        self.assertIn("  Global: %s (stored at %s)\n" % (path, self.moved(path)), text)
        self.assertIn(
            "  Directory: %s, merged with this Python's private copy at %s"
            " (the private copy wins for a name in both)\n"
            % (config_mod.user_providers_dir(), user["directory_real"]),
            text,
        )
        self.assertIn("<- %s (stored at %s)\n" % (self.adapter, self.moved(self.adapter)), text)
        self.assertIn("  Failed:   %s (stored at %s) -- " % (self.broken, self.moved(self.broken)), text)
        self.assertIn("record: %s (stored at %s))" % (record["record"], record["record_real"]), text)

    def test_the_note_says_what_happened_and_how_to_fix_it(self):
        self.populate()
        notes = [note for note in self.collect()["notes"] if "Microsoft Store" in note]
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertIn("%s is really at %s." % (self.home, self.real), note)
        self.assertIn("inspect %s to see the adapter code" % os.path.join(self.real, "providers"), note)
        self.assertIn("set DEV_ORCHESTRA_HOME to a trusted folder you control", note)
        self.assertIn("outside %USERPROFILE%\\AppData and outside any project checkout", note)
        self.assertIn("copy only config.yaml from %s" % self.real, note)
        self.assertIn("regenerate the verified\\ records", note)

    def test_the_note_is_not_a_problem(self):
        report = self.collect()
        self.assertTrue(any("is really at" in note for note in report["notes"]))
        self.assertFalse(any("really at" in problem for problem in report["problems"]))

    def test_an_explicit_config_file_is_not_to_be_copied_from_the_folder(self):
        os.environ["DEV_ORCHESTRA_CONFIG"] = os.path.join(self.tmp, "elsewhere.yaml")
        notes = [note for note in self.collect()["notes"] if "Microsoft Store" in note]
        self.assertEqual(len(notes), 1)
        self.assertNotIn("config.yaml", notes[0])
        self.assertIn("outside any project checkout. Review providers\\*.py", notes[0])

    def test_another_python_gets_the_short_note(self):
        self.store = False
        report = self.collect()
        self.assertNotIn("store_python", report["platform"])
        self.assertIn("%s is stored at %s; " % (self.home, self.real), "\n".join(report["notes"]))
        text = doctor_mod.render(report)
        self.assertNotIn("Microsoft Store", text)
        # A link moves the directory; only the Store Python merges a private copy.
        directory = config_mod.user_providers_dir()
        shown = "  Directory: %s (stored at %s) (not present; nothing imported)\n" % (
            directory,
            os.path.join(self.real, "providers"),
        )
        self.assertIn(shown, text)
        self.assertNotIn("private copy", text)

    def test_a_missing_config_file_is_not_reported_but_its_folder_is(self):
        report = self.collect()
        self.assertNotIn("global_real", report["config"])
        self.assertIn("  Global: not found\n", doctor_mod.render(report))
        said = "%s is really at %s." % (self.home, self.real)
        self.assertTrue(any(said in note for note in report["notes"]))

    def test_a_parse_error_keeps_the_note_and_the_key(self):
        path = config_mod.global_config_path()
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("version: 1\nnot a key and a value\n")
        report = self.collect()
        self.assertEqual(report["config"]["status"], "error")
        self.assertEqual(report["config"]["global"], path)
        self.assertEqual(report["config"]["global_real"], self.moved(path))
        self.assertTrue(any("is really at" in note for note in report["notes"]))
        text = doctor_mod.render(report)
        self.assertIn("Microsoft Store package", text)
        self.assertIn("  Global: %s (stored at %s)\n" % (path, self.moved(path)), text)

    def test_an_error_in_the_project_file_still_names_the_global_one(self):
        """``global_real`` is where the named file is, whichever file failed."""
        path = config_mod.global_config_path()
        config_mod.write_config_file(path, {"version": 1}, "global")
        project = os.path.join(self.project, ".dev-orchestra.yaml")
        with open(project, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("version: 1\nnot a key and a value\n")
        report = self.collect()
        self.assertEqual(report["config"]["status"], "error")
        self.assertEqual(report["config"]["project_override"], project)
        self.assertEqual(report["config"]["global"], path)
        self.assertEqual(report["config"]["global_real"], self.moved(path))
        text = doctor_mod.render(report)
        self.assertIn("  Project override: %s\n" % project, text)

    def test_an_error_without_a_global_file_has_no_key(self):
        project = os.path.join(self.project, ".dev-orchestra.yaml")
        with open(project, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("version: 1\nnot a key and a value\n")
        report = self.collect()
        self.assertEqual(report["config"]["status"], "error")
        self.assertEqual(report["config"]["global"], "not found")
        self.assertNotIn("global_real", report["config"])
        self.assertIn("  Global: not found\n", doctor_mod.render(report))

    def test_a_home_outside_appdata_that_is_not_redirected_has_no_note(self):
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.tmp, "elsewhere")
        report = self.collect()
        self.assertFalse(any("really at" in note for note in report["notes"]))
        self.assertNotIn("directory_real", report["user_providers"])

    def test_a_home_in_appdata_that_is_not_redirected_has_no_note(self):
        """The outcome follows the real location, not how the path is spelled."""
        with mock.patch.object(config_mod, "real_location", lambda path: path):
            report = self.collect()
        self.assertFalse(any("really at" in note for note in report["notes"]))

    def test_a_home_spelled_in_another_case_is_still_reported(self):
        os.environ["DEV_ORCHESTRA_HOME"] = self.home.upper()
        report = self.collect()
        self.assertTrue(any("is really at" in note for note in report["notes"]))


class TestRenderers(unittest.TestCase):
    """The renderers only read the ``*_real`` keys; nothing is looked up."""

    def test_config_lines(self):
        info = {"global": "G", "project_override": "none"}
        self.assertIn("  Global: G", doctor_mod._config_lines(info))
        self.assertIn("  Global: G (stored at R)", doctor_mod._config_lines(dict(info, global_real="R")))
        failed = {"status": "error", "error": "x", "global": "G", "global_real": "R"}
        self.assertIn("  Global: G (stored at R)", doctor_mod._config_lines(failed))

    def test_user_provider_lines(self):
        info = {
            "directory": "D",
            "enabled": True,
            "present": True,
            "loaded": [{"name": "mycli", "path": "D/m.py"}],
            "errors": [{"path": "D/b.py", "error": "boom"}],
        }
        plain = doctor_mod._user_provider_lines(info)
        self.assertIn("  Directory: D", plain)
        self.assertIn("  Imported: mycli  <- D/m.py", plain)
        self.assertIn("  Failed:   D/b.py -- boom", plain)
        moved = dict(
            info,
            directory_real="R",
            loaded=[{"name": "mycli", "path": "D/m.py", "path_real": "R/m.py"}],
            errors=[{"path": "D/b.py", "error": "boom", "path_real": "R/b.py"}],
        )
        lines = doctor_mod._user_provider_lines(moved, store=True)
        merged = "merged with this Python's private copy at R (the private copy wins for a name in both)"
        self.assertIn("  Directory: D, %s" % merged, lines)
        self.assertIn("  Imported: mycli  <- D/m.py (stored at R/m.py)", lines)
        self.assertIn("  Failed:   D/b.py (stored at R/b.py) -- boom", lines)
        linked = doctor_mod._user_provider_lines(moved)
        self.assertIn("  Directory: D (stored at R)", linked)
        self.assertFalse(any("private copy" in line for line in linked))
        absent = doctor_mod._user_provider_lines({"directory": "D", "directory_real": "R", "present": False})
        self.assertIn("  Directory: D (stored at R) (not present; nothing imported)", absent)
        disabled = doctor_mod._user_provider_lines(
            {"directory": "D", "directory_real": "R", "enabled": False}
        )
        self.assertIn("  Directory: D (stored at R)", disabled)

    def test_resume_line(self):
        support = {
            "status": "verified",
            "version": "1.0",
            "source": "record",
            "record": "P",
            "verified_at": "2026-10-01",
        }
        self.assertTrue(doctor_mod._resume_line("claude", support).endswith("(record: P)"))
        moved = doctor_mod._resume_line("claude", dict(support, record_real="R"))
        self.assertTrue(moved.endswith("(record: P (stored at R))"))
        trusted = dict(support, status="trusted", newer_than="0.9", record_real="R")
        self.assertIn("record: P (stored at R)", doctor_mod._resume_line("claude", trusted))

    def test_environment_line(self):
        report: Dict[str, Any] = {"platform": {"system": "Windows", "release": "11", "python": "3.13.1"}}
        self.assertIn("Environment: Windows 11 / Python 3.13.1", doctor_mod._environment_lines(report))
        report["platform"]["store_python"] = True
        self.assertIn(
            "Environment: Windows 11 / Python 3.13.1 (Microsoft Store package)",
            doctor_mod._environment_lines(report),
        )
