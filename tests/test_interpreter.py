"""Which Python the CLI is started with.

Reported from real use: `python` is not a name every machine has. Recent Linux
distributions and Homebrew install the interpreter as `python3` only, so
anything that hardcodes `python` -- a wrapper, an installer's pointer block, a
skill telling an agent what to type -- is a "command not found" there.

The wrappers in `bin/` resolve the name themselves. Everything that *writes* or
*prints* an invocation -- an installer's pointer block, the skill, the agent
manifest -- has to agree with them, which is what these tests hold.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from helpers import REPO_ROOT

from orchestrator import miniyaml

POSIX_WRAPPER = os.path.join(REPO_ROOT, "bin", "dev-orchestra")
SH = shutil.which("sh")

#: POSIX first tries the name that exists where only one of them does.
#: Windows is the other way round: a `python3` there is usually the Store's app
#: execution alias, which opens the Microsoft Store rather than running Python.
POSIX_ORDER = ("python3", "python")
WINDOWS_ORDER = ("python", "py", "python3")


def read(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


class TestTheWrappers(unittest.TestCase):
    def test_the_posix_wrapper_prefers_python3(self):
        self.assertIn("for candidate in %s; do" % " ".join(POSIX_ORDER), read("bin/dev-orchestra"))

    def test_the_windows_wrapper_avoids_the_store_alias_first(self):
        listed = ", ".join("'%s'" % name for name in WINDOWS_ORDER)
        self.assertIn("foreach ($candidate in @(%s))" % listed, read("bin/dev-orchestra.ps1"))

    def test_a_machine_with_no_interpreter_is_told_so(self):
        """Rather than a shell error naming a command nobody typed."""
        for relative in ("bin/dev-orchestra", "bin/dev-orchestra.ps1"):
            text = read(relative)
            self.assertIn("Python 3.9+ is required but was not found on PATH", text)
            self.assertIn("127", text)


@unittest.skipIf(os.name == "nt", "the POSIX wrapper needs a POSIX shell")
@unittest.skipUnless(SH, "sh is required")
class TestThePosixWrapperPicks(unittest.TestCase):
    """The selection itself, with stub interpreters that say who they are."""

    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix="devorchestra-interp-")
        self.addCleanup(shutil.rmtree, self.bin, True)
        # The wrapper runs with *only* this directory on PATH: prepending it to
        # the real one proves nothing, because the system `python3` is then
        # still found in the case where there is supposed to be none. So the
        # one external command the wrapper needs has to live here too.
        dirname = shutil.which("dirname")
        if not dirname:
            self.skipTest("dirname is required")
        shutil.copy(dirname, os.path.join(self.bin, "dirname"))

    def stub(self, name):
        """An interpreter that does nothing but say which name ran it."""
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nprintf '%s' " + name + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def run_wrapper(self):
        result = subprocess.run(
            [SH, POSIX_WRAPPER, "--version"],
            env={"PATH": self.bin},
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or result.stderr.strip()

    def test_a_machine_with_neither_says_so_and_exits_127(self):
        result = subprocess.run(
            [SH, POSIX_WRAPPER, "--version"],
            env={"PATH": self.bin},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 127)
        self.assertIn("Python 3.9+ is required", result.stderr)

    def test_python3_wins_when_both_are_there(self):
        self.stub("python")
        self.stub("python3")
        self.assertEqual(self.run_wrapper(), "python3")

    def test_python_is_used_when_python3_is_not_there(self):
        """A machine where `python` is the only name -- which is what every
        Windows install and every older distribution looks like."""
        self.stub("python")
        self.assertEqual(self.run_wrapper(), "python")


class TestWhatWeTellOtherPeopleToType(unittest.TestCase):
    """An installer's pointer block and the skill are read by an agent, which
    will type exactly what they say."""

    def test_the_posix_installer_resolves_the_interpreter(self):
        script = read("install/install.sh")
        self.assertIn("for candidate in python3 python; do", script)
        self.assertIn('"$python_cmd"', script)
        self.assertNotIn("    printf '    python %s/scripts", script)

    def test_the_windows_installer_resolves_the_interpreter(self):
        script = read("install/install.ps1")
        for name in WINDOWS_ORDER:
            self.assertIn("'%s'" % name, script)
        self.assertIn("$PythonCmd $root/scripts/dev_orchestra.py", script)

    def test_the_skill_names_the_fallback(self):
        """It hands the agent one literal command line; the agent has no other
        way to learn that the name differs on its machine."""
        self.assertIn("python3", read("skills/dev-orchestra/SKILL.md"))

    def test_the_agent_manifest_lists_the_candidates(self):
        """The manifest is one list for every host, so it cannot encode both
        orders -- it leads with the POSIX one and names the wrappers, which do
        know which platform they are on."""
        entrypoint = miniyaml.loads(read("agents/openai.yaml"))["entrypoint"]
        self.assertEqual(entrypoint["command_candidates"][:2], list(POSIX_ORDER))
        self.assertIn("py", entrypoint["command_candidates"])
        self.assertTrue(entrypoint["wrappers"]["posix"].endswith("bin/dev-orchestra"))

    def test_both_readmes_mention_it(self):
        for relative in ("README.md", "README.ja.md"):
            self.assertIn("python3", read(relative), relative)


if __name__ == "__main__":
    unittest.main()
