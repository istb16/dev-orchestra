"""The live-CLI smoke script, checked without a live CLI.

``scripts/smoke_live.py`` is the one thing here that talks to a real CLI, so
it cannot be exercised from a suite that must pass with neither installed.
What can be checked is everything around the calls: that a provider which
cannot be reached is reported rather than crashing, that a run which raises --
the exact shape of the `TypeError` that went unnoticed for weeks -- becomes a
failed check instead of a traceback, and that the read-only verdict is read
from the filesystem rather than from what the agent said about it.

A smoke script that falls over on the first surprise tells you nothing on the
day you need it.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The script under test lives beside the package rather than inside it, and is
# imported by name. ``helpers`` would put ``scripts/`` on the path too, but
# only once it has been imported -- which is after this line either way.
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import smoke_live  # noqa: E402
from helpers import IsolatedCase  # noqa: E402

from orchestrator.providers.base import Detection, ResolvedModel, RunResult, Usage  # noqa: E402


class _FakeProvider:
    """A provider-shaped object that never starts a process."""

    name = "fake"
    executable = "fake"

    def __init__(self, installed=True, raises=None, writes=None, usage=None, stdout="READY"):
        self._installed = installed
        self._raises = raises
        self._writes = writes
        self._usage = usage if usage is not None else Usage(total_tokens=10, source="fake")
        self._stdout = stdout
        self.calls = []

    def detect(self):
        return Detection(self._installed, "fake", "fake 1", None if self._installed else "not on PATH")

    def resolve_model(self, spec):
        return ResolvedModel("fake", "fake-1", "latest", "fake-1", "fake-1", "test")

    def run(self, prompt, mode, cwd, **kwargs):
        self.calls.append({"prompt": prompt, "mode": mode, "cwd": cwd, "kwargs": kwargs})
        if self._raises is not None:
            raise self._raises
        if self._writes:
            with open(os.path.join(cwd, self._writes), "w", encoding="utf-8") as handle:
                handle.write("BREACH\n")
        return RunResult(True, 0, self._stdout, "", ["fake"], 0.1, usage=self._usage)


def verdict(checks, name):
    for check in checks:
        if check.name == name:
            return check
    raise AssertionError("no check named %r in %s" % (name, [c.name for c in checks]))


class TestTheReadOnlyVerdict(IsolatedCase):
    """Read from the filesystem, never from the agent's own account of itself."""

    def test_a_provider_that_writes_the_file_fails(self):
        provider = _FakeProvider(writes=smoke_live.WRITE_TARGET)
        check = smoke_live.check_read_only(provider, "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn(smoke_live.WRITE_TARGET, check.detail)

    def test_the_breach_file_is_cleaned_up_either_way(self):
        """It is evidence, not a deliverable, and the next check runs in the
        same directory."""
        provider = _FakeProvider(writes=smoke_live.WRITE_TARGET)
        smoke_live.check_read_only(provider, "fake", self.project)
        self.assertFalse(os.path.exists(os.path.join(self.project, smoke_live.WRITE_TARGET)))

    def test_a_provider_that_writes_nothing_passes(self):
        check = smoke_live.check_read_only(_FakeProvider(), "fake", self.project)
        self.assertTrue(check.ok)

    def test_a_polite_refusal_is_not_what_is_measured(self):
        """An agent that says "I cannot do that" and writes the file anyway
        fails, and one that says nothing and writes nothing passes."""
        talker = _FakeProvider(writes=smoke_live.WRITE_TARGET, stdout="I am read-only and cannot write.")
        self.assertFalse(smoke_live.check_read_only(talker, "fake", self.project).ok)
        silent = _FakeProvider(stdout="")
        self.assertTrue(smoke_live.check_read_only(silent, "fake", self.project).ok)

    def test_a_stale_file_does_not_condemn_the_run(self):
        """The check clears the target first, so a leftover from a previous
        provider cannot be read as this one's breach."""
        with open(os.path.join(self.project, smoke_live.WRITE_TARGET), "w", encoding="utf-8") as handle:
            handle.write("left over\n")
        self.assertTrue(smoke_live.check_read_only(_FakeProvider(), "fake", self.project).ok)

    def test_a_raising_run_is_a_failed_check_not_a_traceback(self):
        provider = _FakeProvider(raises=TypeError("unexpected keyword argument 'idle_timeout'"))
        check = smoke_live.check_read_only(provider, "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn("TypeError", check.detail)


class TestOneProvidersChecks(IsolatedCase):
    def checks(self, provider):
        original = smoke_live.get_provider
        smoke_live.get_provider = lambda name: provider
        self.addCleanup(setattr, smoke_live, "get_provider", original)
        return smoke_live.check_provider("fake", self.project)

    def test_a_missing_cli_reports_once_and_stops(self):
        """No point asking an absent CLI what it costs."""
        checks = self.checks(_FakeProvider(installed=False))
        self.assertEqual([c.name for c in checks], ["installed"])
        self.assertFalse(checks[0].ok)

    def test_a_run_that_raises_is_reported_not_propagated(self):
        """The defect this script exists for: `CodexProvider.run` did not
        accept a keyword the orchestrator always sends."""
        checks = self.checks(_FakeProvider(raises=TypeError("unexpected keyword argument")))
        answer = verdict(checks, "answers a review prompt")
        self.assertFalse(answer.ok)
        self.assertIn("TypeError", answer.detail)

    def test_unreported_usage_is_a_failure_not_a_blank(self):
        """Silence here means the CLI's accounting format moved and the
        parser did not -- which is how a cost report becomes fiction."""
        checks = self.checks(_FakeProvider(usage=Usage()))
        spent = verdict(checks, "reports what it spent")
        self.assertFalse(spent.ok)
        self.assertIn("accounting", spent.detail)

    def test_a_healthy_provider_passes_everything(self):
        checks = self.checks(_FakeProvider())
        self.assertTrue(all(c.ok for c in checks), [c.name for c in checks if not c.ok])
        self.assertEqual(
            [c.name for c in checks],
            [
                "installed",
                "resolves a model",
                "answers a review prompt",
                "reports what it spent",
                "stays read-only",
            ],
        )

    def test_the_wrong_answer_fails_even_with_a_zero_exit(self):
        checks = self.checks(_FakeProvider(stdout="Sure! Here is a poem about readiness."))
        self.assertFalse(verdict(checks, "answers a review prompt").ok)

    def test_every_call_is_made_in_review_mode(self):
        """A smoke test that ran in implement mode would be testing a mode no
        reviewer uses, and would be allowed to write."""
        provider = _FakeProvider()
        self.checks(provider)
        self.assertTrue(provider.calls)
        for call in provider.calls:
            self.assertEqual(call["mode"], "review")


class TestTheCommandLine(IsolatedCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = smoke_live.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_an_unknown_provider_is_refused_before_anything_runs(self):
        code, _, err = self.run_main("--provider", "nonexistent")
        self.assertEqual(code, 2)
        self.assertIn("nonexistent", err)

    def test_the_mock_is_not_smoke_tested(self):
        """It is always "installed" and never starts a process, so running it
        here would prove only that the stub is a stub."""
        self.assertIn("mock", smoke_live.OFFLINE)


class TestItStaysOutOfTheSuite(unittest.TestCase):
    def test_the_script_is_not_collected_by_discovery(self):
        """`unittest discover` matches `test*.py`. This script spends money
        and must never run as part of the suite."""
        self.assertFalse(os.path.basename(smoke_live.__file__).startswith("test"))

    def test_contributing_says_how_to_run_it(self):
        """A maintenance script nobody is told about is a script nobody runs."""
        with open(os.path.join(REPO_ROOT, "CONTRIBUTING.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("scripts/smoke_live.py", text)


if __name__ == "__main__":
    unittest.main()
