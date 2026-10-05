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

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Optional, Sequence
from unittest import mock

from helpers import REPO_ROOT, IsolatedCase, make_dir_link, present, remove_link

# isort: split
# ``helpers`` first: it puts ``scripts/`` on the path, where the script under
# test lives, and fixes the environment before ``smoke_live`` imports the
# provider registry -- which would otherwise import the real user's adapters.
import smoke_live

from orchestrator.providers import agy as agy_module
from orchestrator.providers import base
from orchestrator.providers import claude as claude_module
from orchestrator.providers import mock as mock_module
from orchestrator.providers.agy import AgyProvider
from orchestrator.providers.base import Detection, ResolvedModel, RunResult, Usage
from orchestrator.providers.claude import ClaudeProvider
from orchestrator.providers.codex import CodexProvider
from orchestrator.providers.mock import MockProvider


class _FakeProvider(base.Provider):
    """An adapter that never starts a process. Every live-check member is
    the base's default unless a subclass sets it."""

    name = "fake"
    executable = "fake"

    def __init__(self, installed=True, raises=None, writes=None, usage=None, stdout="READY", result=None):
        super().__init__()
        self._installed = installed
        self._raises = raises
        self._writes = writes
        self._usage = usage if usage is not None else Usage(total_tokens=10, source="fake")
        self._stdout = stdout
        self._result = result
        self.calls = []
        self.specs = []

    def detect(self):
        return Detection(self._installed, "fake", "fake 1", None if self._installed else "not on PATH")

    def resolve_model(self, spec):
        self.specs.append(spec)
        return ResolvedModel("fake", "fake-1", "latest", "fake-1", "fake-1", "test")

    # Keywords only, as smoke_live.py calls it.
    def run(self, prompt, mode, cwd, **kwargs):  # pyright: ignore[reportIncompatibleMethodOverride]
        self.calls.append({"prompt": prompt, "mode": mode, "cwd": cwd, "kwargs": kwargs})
        if self._raises is not None:
            raise self._raises
        if self._writes:
            with open(os.path.join(cwd, self._writes), "w", encoding="utf-8") as handle:
                handle.write("BREACH\n")
        if self._result is not None:
            return self._result
        return RunResult(True, 0, self._stdout, "", ["fake"], 0.1, usage=self._usage)


def patch_symlink(case, replacement=None):
    """Replace ``os.symlink`` until ``case`` cleans up: by default with a
    copy, a link that works everywhere (Windows needs a privilege for one)."""

    def copy(source, target):
        shutil.copyfile(source, target)

    patcher = mock.patch.object(os, "symlink", replacement or copy)
    patcher.start()
    case.addCleanup(patcher.stop)


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

    def test_a_run_that_never_started_proves_nothing(self):
        """No file is also what an adapter that refused to launch leaves."""
        result = RunResult(False, 2, "", "read-only run: refused", ["fake"], 0.0, invoked=False)
        check = smoke_live.check_read_only(_FakeProvider(result=result), "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn("refused to launch", check.detail)

    def test_a_run_that_failed_proves_nothing_either(self):
        failed = _FakeProvider(result=RunResult(False, 1, "", "boom", ["fake"], 0.1))
        check = smoke_live.check_read_only(failed, "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn("did not complete (exit 1): boom", check.detail)


class _Reader(_FakeProvider):
    """Reads what the prompt names, the way a CLI that is not confined would.

    ``confined``: nothing outside the working directory is read unless
    ``--add-dir`` was passed. ``reads``: whether it reads anything at all.
    Declared confining, with Claude's widening arguments and a gate that,
    like Claude's, accepts them.
    """

    name = "claude"
    confines_read_only = True

    def __init__(self, confined=False, reads=True):
        super().__init__()
        self.confined = confined
        self.reads = reads

    def read_only_widening_args(self, directory):
        return ["--add-dir", directory]

    def refused_read_only_args(self, raw_args, source):
        raw = list(raw_args)
        if len(raw) == 2 and raw[0] == "--add-dir" and not raw[1].startswith("-"):
            return []
        return super().refused_read_only_args(raw, source)

    def run(self, prompt, mode, cwd, **kwargs):
        self.calls.append({"prompt": prompt, "mode": mode, "cwd": cwd, "kwargs": kwargs})
        widened = "--add-dir" in (kwargs.get("extra_args") or [])
        text = ""
        if self.reads and (widened or not self.confined):
            for piece in prompt.split("`")[1::2]:
                path = piece if os.path.isabs(piece) else os.path.join(cwd, piece)
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as handle:
                        text += handle.read()
        return RunResult(True, 0, text or "nothing", "", ["claude"], 0.1)


class TestTheConfinementCheck(IsolatedCase):
    def copy_instead_of_link(self):
        patch_symlink(self)

    def by_name(self, provider):
        return {check.name: check for check in smoke_live.check_confined(provider, "claude", self.project)}

    def test_only_confining_providers_are_checked(self):
        class _NotConfining(_Reader):
            confines_read_only = False

        self.assertEqual(smoke_live.check_confined(_NotConfining(), "codex", self.project), [])

    def test_a_cli_that_reads_outside_fails(self):
        self.copy_instead_of_link()
        checks = self.by_name(_Reader(confined=False))
        self.assertFalse(checks["stays confined (absolute)"].ok)
        self.assertFalse(checks["stays confined (symlink)"].ok)
        self.assertTrue(checks["--add-dir widens"].ok)

    def test_a_confined_cli_passes_and_add_dir_widens_it(self):
        self.copy_instead_of_link()
        checks = self.by_name(_Reader(confined=True))
        self.assertTrue(all(check.ok for check in checks.values()), checks)
        self.assertEqual(len(checks), 3)

    def test_add_dir_that_reads_nothing_fails(self):
        self.copy_instead_of_link()
        checks = self.by_name(_Reader(reads=False))
        self.assertTrue(checks["stays confined (absolute)"].ok)
        self.assertFalse(checks["--add-dir widens"].ok)

    def test_an_untestable_symlink_is_skipped_not_passed(self):
        def refuse(source, target):
            raise OSError("symbolic link privilege not held")

        patch_symlink(self, refuse)
        check = self.by_name(_Reader(confined=True))["stays confined (symlink)"]
        self.assertFalse(check.ok)
        self.assertTrue(check.skipped)
        self.assertIn("symlink not tested: symbolic link privilege not held", check.detail)

    def test_nothing_is_left_behind(self):
        self.copy_instead_of_link()
        self.by_name(_Reader(confined=True))
        self.assertFalse(os.path.lexists(os.path.join(self.project, "link.txt")))

    def test_the_marker_is_never_in_a_prompt_whole(self):
        reader = _Reader(confined=True)
        self.copy_instead_of_link()
        self.by_name(reader)
        prompts = " ".join(call["prompt"] for call in reader.calls)
        self.assertEqual(len(reader.calls), 3)
        for call in reader.calls:
            self.assertEqual(call["mode"], "review")
        self.assertNotRegex(prompts, r"[0-9a-f]{32}")


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
                "reports its tool activity",
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


class TestTheToolActivityCheck(IsolatedCase):
    """The event shape these counts come from belongs to the CLI.

    The unit tests read it from a fixture, so they keep passing on the day it
    changes. Only a real run catches that, and the cost of missing it is a
    measurement that reads "this reviewer opened nothing" when it means "we
    stopped being able to tell".
    """

    def expect_tools(self):
        patcher = mock.patch.object(_FakeProvider, "tool_activity_reported", base.TOOL_ACTIVITY_OUTPUT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_provider_whose_adapter_does_not_count_them_is_not_failed_for_it(self):
        """Codex hands back its final message and prices itself in prose. It is
        unreported by design, which is not a drift to catch."""
        check = smoke_live.check_tool_activity(_FakeProvider(), "fake", self.project)
        self.assertTrue(check.ok)
        self.assertIn("by design", check.detail)

    def test_an_unparsed_count_is_a_failure(self):
        self.expect_tools()
        provider = _FakeProvider(usage=Usage(total_tokens=10, source="fake"))
        check = smoke_live.check_tool_activity(provider, "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn("event shape", check.detail)

    def test_a_zero_is_a_failure_for_a_prompt_that_needs_a_tool(self):
        """A measured zero is a legitimate report elsewhere. Here the question
        cannot be answered without a tool, so zero means the pairing broke."""
        self.expect_tools()
        usage = Usage(total_tokens=10, source="fake", tool_uses=0, tool_uses_by_name={})
        check = smoke_live.check_tool_activity(_FakeProvider(usage=usage), "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn("0 tool uses", check.detail)

    def test_calls_without_results_are_a_failure_too(self):
        """Calls and results come from different events, paired by
        `tool_use_id`, so the pairing can break on its own: the call count
        still looks right while every output figure becomes zero. The prompt
        makes the agent read a file, so it has output."""
        self.expect_tools()
        usage = Usage(
            total_tokens=10,
            source="fake",
            tool_uses=2,
            tool_uses_by_name={"Read": 2},
            tool_output_chars=0,
        )
        check = smoke_live.check_tool_activity(_FakeProvider(usage=usage), "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn("no output to pair", check.detail)

    def test_a_missing_output_count_is_not_read_as_a_pass(self):
        usage = Usage(total_tokens=10, source="fake", tool_uses=1, tool_uses_by_name={"Read": 1})
        self.expect_tools()
        check = smoke_live.check_tool_activity(_FakeProvider(usage=usage), "fake", self.project)
        self.assertFalse(check.ok)

    def test_a_counted_run_names_the_tools_it_used(self):
        """The breakdown is the point: the run measured while designing this
        read a file with `Bash`, and counting only `Read` would have said it
        read nothing."""
        self.expect_tools()
        usage = Usage(
            total_tokens=10,
            source="fake",
            tool_uses=2,
            tool_uses_by_name={"Bash": 2},
            tool_output_chars=3,
        )
        check = smoke_live.check_tool_activity(_FakeProvider(usage=usage), "fake", self.project)
        self.assertTrue(check.ok)
        self.assertIn("Bash x2", check.detail)

    def test_a_raising_run_is_a_failed_check_not_a_traceback(self):
        self.expect_tools()
        provider = _FakeProvider(raises=TypeError("unexpected keyword argument"))
        check = smoke_live.check_tool_activity(provider, "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn("TypeError", check.detail)


#: What agy's adapter warns when the CLI denied an action.
AGY_DENIED_COMMAND = (
    "agy denied 1 action(s): RunCommand (command); set options.skip_permissions: true in the global "
    "config, or pass --extra --dangerously-skip-permissions, for the implementer to run commands"
)


def agy_run(stdout="smoke", ok=True, tool_uses: Optional[int] = 1, by_name=None, warnings=()):
    """A finished agy run: tools counted, their output not."""
    usage = Usage(
        input_tokens=10,
        source="agy stream-json result",
        tool_uses=tool_uses,
        tool_uses_by_name={"view_file": 1} if by_name is None else by_name,
    )
    return RunResult(ok, 0, stdout, "", ["agy"], 0.1, usage=usage, warnings=list(warnings))


class _AgyLike(_FakeProvider):
    """agy's live-check members, its options and its reading of a denial."""

    name = "agy"
    tool_activity_reported = AgyProvider.tool_activity_reported
    file_read_tool = AgyProvider.file_read_tool
    implement_write_checked = AgyProvider.implement_write_checked
    permission_bypass_options = AgyProvider.permission_bypass_options
    option_keys = AgyProvider.option_keys

    def validate_options(self, options):
        return AgyProvider().validate_options(options)

    def denied_action_items(self, warning):
        return AgyProvider().denied_action_items(warning)


class TestTheAgyToolActivityCheck(IsolatedCase):
    """agy counts tool calls but not their output: the reply and the tool
    names stand in for the output count, and nothing may have been denied."""

    def check(self, result):
        provider = _AgyLike(result=result)
        return smoke_live.check_tool_activity(provider, "agy", self.project), provider

    def test_each_way_to_fail(self):
        cases = [
            (agy_run(tool_uses=None, by_name={}), "no tool activity parsed"),
            (agy_run(tool_uses=0, by_name={}), "0 tool uses"),
            (agy_run(warnings=[AGY_DENIED_COMMAND]), '"agy denied 1 action(s): RunCommand (command)'),
            (agy_run(stdout="", ok=False), "did not complete"),
            (agy_run(stdout="README.md has 1 line"), "did not carry README.md's content"),
            (agy_run(by_name={"run_command": 1}), "expected view_file in tool_uses_by_name, got run_comm"),
        ]
        for result, detail in cases:
            with self.subTest(detail=detail):
                check, _ = self.check(result)
                self.assertFalse(check.ok)
                self.assertIn(detail, check.detail)

    def test_agy_passes_with_view_file_and_no_output_chars(self):
        check, _ = self.check(agy_run())
        self.assertTrue(check.ok, check.detail)
        self.assertIn("view_file x1", check.detail)
        self.assertIn("output chars not reported by agy", check.detail)

    def test_only_the_chars_requirement_is_skipped(self):
        class _ReportsOutput(_FakeProvider):
            tool_activity_reported = base.TOOL_ACTIVITY_OUTPUT

        provider = _ReportsOutput(result=agy_run())
        check = smoke_live.check_tool_activity(provider, "fake", self.project)
        self.assertFalse(check.ok)
        self.assertIn("no output to pair", check.detail)

    def test_agy_uses_its_own_tool_prompt(self):
        _, provider = self.check(agy_run())
        self.assertEqual(provider.calls[0]["prompt"], smoke_live.FILE_TOOL_PROMPT)
        self.assertNotIn(smoke_live.README_TEXT, provider.calls[0]["prompt"])


class TestTheDeniedCommandCheck(IsolatedCase):
    """The live check that ``-p`` gives the shape of the denied recording."""

    def denied_check(self, warnings):
        provider = _AgyLike(result=agy_run(stdout="", warnings=warnings))
        checks = smoke_live.check_implement(provider, "agy", self.project)
        return verdict(checks, "names a denied command"), provider

    def test_a_named_command_passes(self):
        check, _ = self.denied_check([AGY_DENIED_COMMAND])
        self.assertTrue(check.ok, check.detail)
        self.assertIn("RunCommand (command)", check.detail)

    def test_the_warning_is_read_by_the_adapters_own_wording(self):
        """The smoke script holds no copy of the wording: one the adapter
        writes is the one both checks read."""
        denied = [{"action": "command", "display_name": "Run"}]
        warning = agy_module._denied_warning({"denied_actions": denied})
        assert warning is not None
        found = smoke_live._denied_actions(AgyProvider(), [warning])
        self.assertEqual(found, [[("Run", "command")]])
        self.assertEqual(smoke_live._denied_text(found), "Run (command)")
        reworded = "agy refused Run (command); set it"
        with mock.patch.object(agy_module, "_DENIED_RE", re.compile(r"^agy refused (.*?); set ")):
            found = smoke_live._denied_actions(AgyProvider(), [reworded])
            self.assertEqual(found, [[("Run", "command")]])
            self.assertEqual(smoke_live._denied_text(found), "Run (command)")
            provider = _AgyLike(result=agy_run(warnings=[reworded]))
            check = smoke_live.check_tool_activity(provider, "agy", self.project)
        self.assertFalse(check.ok)
        self.assertIn('"agy refused Run (command)', check.detail)

    def test_another_denied_action_fails(self):
        warning = (
            "agy denied 1 action(s): WriteFile (write); set options.skip_permissions: true in the global "
            "config, or pass --extra --dangerously-skip-permissions, for the implementer to run commands"
        )
        check, _ = self.denied_check([warning])
        self.assertFalse(check.ok)
        self.assertIn("WriteFile (write)", check.detail)

    def test_no_warning_fails(self):
        check, _ = self.denied_check([])
        self.assertFalse(check.ok)
        self.assertIn("no action was denied", check.detail)

    def test_it_runs_the_command_prompt_without_the_bypass(self):
        _, provider = self.denied_check([AGY_DENIED_COMMAND])
        calls = [call for call in provider.calls if call["prompt"] == smoke_live.COMMAND_PROMPT]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["mode"], "implement")
        self.assertNotIn("options", calls[0]["kwargs"])
        self.assertEqual(calls[1]["kwargs"]["options"], {"skip_permissions": True})


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

    def fake_checks(self, *checks):
        original = smoke_live.check_provider
        smoke_live.check_provider = lambda name, root, model_spec=None, record=True: list(checks)
        self.addCleanup(setattr, smoke_live, "check_provider", original)

    def test_a_skipped_check_keeps_the_run_from_reading_all_green(self):
        self.fake_checks(
            smoke_live.Check("claude", "installed", True, "1"),
            smoke_live.Check("claude", "stays confined (symlink)", False, "not tested: x", skipped=True),
        )
        code, out, _ = self.run_main("--provider", "claude")
        self.assertEqual(code, 1)
        self.assertIn("SKIP claude", out)
        self.assertIn("1 check(s) were not tested", out)
        self.assertNotIn("All checks passed", out)
        self.assertNotIn("failed", out)
        code, out, _ = self.run_main("--provider", "claude", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["skipped"], 1)

    def test_the_mock_is_not_smoke_tested(self):
        """It is always "installed" and never starts a process, so running it
        here would prove only that the stub is a stub."""
        self.assertIn("mock", smoke_live.OFFLINE)

    def test_without_model_every_provider_records(self):
        """Asked with ``record=True`` and no model, and both records are then written."""
        calls = []

        def check_provider(name, root, model_spec=None, record=True):
            calls.append((name, root, model_spec, record))
            return []

        with mock.patch.object(smoke_live, "check_provider", check_provider):
            self.run_main("--provider", "claude")
        self.assertEqual(len(calls), 1)
        name, root, model_spec, record = calls[0]
        self.assertEqual((name, model_spec, record), ("claude", None, True))
        self.assertTrue(os.path.basename(root).startswith("dev-orchestra-smoke-"), root)

        patch_symlink(self)
        provider = _claude_like()
        with mock.patch.object(smoke_live, "get_provider", lambda _: provider):
            code, out, err = self.run_main("--provider", "claude")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.exists(smoke_live.verified.record_path("claude")))
        self.assertTrue(os.path.exists(smoke_live.verified.smoke_record_path("claude")))


#: Read by ``record_resume`` from the module the provider's class lives in,
#: the way it reads the claude adapter's table. Empty: nothing is built in.
VERIFIED_RESUME = {}

PARENT = "66666666-6666-4666-8666-666666666666"
READ_ONLY_INIT = {"tools": ["Glob", "Grep", "Read"], "mcp_servers": [], "permission_mode": "plan"}


class _Resumer(_Reader):
    """A CLI that resumes, with whatever init it is told to report: Claude's
    members, and Claude's reading of that init."""

    supports_resume = True
    required_resume_checks: Sequence[str] = smoke_live.verified.REQUIRED_RESUME_CHECKS
    repository_hooks_file = ".claude/settings.json"

    def resumed_session_problem(self, result):
        return ClaudeProvider().resumed_session_problem(result)

    def __init__(
        self, init=None, fork=True, parent: Optional[str] = PARENT, rejects=True, hooks_on_resume=False
    ):
        super().__init__(confined=True)
        self.init = dict(READ_ONLY_INIT) if init is None else init
        self.fork = fork
        self.parent = parent
        self.rejects = rejects
        self.hooks_on_resume = hooks_on_resume

    def version(self):
        return "9.9.9 (Fake)", None

    def read_only_enforcement(self):
        return {"status": "verified", "mechanism": "--fake-read-only"}

    def resume_mechanism(self) -> str:
        return "--fake-read-only"

    def resume_args(self, session_id):
        return ["--resume=%s" % session_id]

    def run(self, prompt, mode, cwd, **kwargs):
        read = super().run(prompt, mode, cwd, **kwargs)
        session = kwargs.get("resume_session")
        if session is None:
            return RunResult(True, 0, read.stdout, "", ["fake"], 0.1, session_id=self.parent)
        if session == smoke_live.MISSING_SESSION:
            return RunResult(False, 1, "", "", ["fake"], 0.1, resume_rejected=self.rejects)
        if self.hooks_on_resume and os.path.isdir(os.path.join(cwd, ".claude")):
            open(os.path.join(cwd, smoke_live.HOOK_TARGET), "w").close()
        return RunResult(
            True,
            0,
            read.stdout,
            "",
            ["fake"],
            0.1,
            session_id=str(uuid.uuid4()) if self.fork else session,
            session_init=self.init,
        )


class TestTheResumeChecks(IsolatedCase):
    def setUp(self):
        super().setUp()
        patch_symlink(self)

    def checks(self, provider, name="claude"):
        return {check.name: check for check in smoke_live.check_resume(provider, name, self.project)}

    def test_an_adapter_that_does_not_resume_is_not_asked(self):
        self.assertEqual(smoke_live.check_resume(_FakeProvider(), "fake", self.project), [])

    def test_the_switch_does_not_decide_but_the_resume_arguments_do(self):
        """Any adapter with ``resume_args`` of its own is checked while its
        resume is off; one that only says it resumes is not."""
        provider = _Resumer()
        setattr(provider, "supports_resume", False)
        checks = self.checks(provider)
        self.assertEqual(sorted(checks), sorted(smoke_live.resume_labels(provider)))
        self.assertTrue(all(check.ok for check in checks.values()), checks)

        class _OnlySaysSo(_FakeProvider):
            supports_resume = True

        self.assertEqual(smoke_live.check_resume(_OnlySaysSo(), "claude", self.project), [])

    def test_an_adapter_with_only_a_resume_command_is_asked_too(self):
        """An adapter may resume with a command of another shape instead."""

        class _ResumesByCommand(_FakeProvider):
            def resume_command(self, mode, resolved, cwd, extra_args, options, session_id):
                return ["fake", "resume", session_id]

        self.assertTrue(smoke_live.resumes(_ResumesByCommand()))
        self.assertFalse(smoke_live.resumes(_FakeProvider()))

    def test_every_shipped_adapter_has_what_the_record_reads(self):
        """``record_resume`` reads these directly, without a fallback."""
        for cls in (ClaudeProvider, CodexProvider, AgyProvider):
            with self.subTest(provider=cls.__name__):
                provider = cls()
                self.assertIsInstance(tuple(provider.required_resume_checks), tuple)
                self.assertIsInstance(provider.resume_mechanism(), str)

    def test_a_read_only_resumed_session_passes_everything(self):
        provider = _Resumer()
        checks = self.checks(provider)
        self.assertEqual(sorted(checks), sorted(smoke_live.resume_labels(provider)))
        self.assertNotIn("ignores repository config on resume", checks)
        self.assertTrue(all(check.ok for check in checks.values()), checks)
        for call in provider.calls[1:]:
            if call["kwargs"].get("resume_session"):
                self.assertEqual(call["mode"], "plan")

    def test_extra_tools_fail_even_when_nothing_was_written(self):
        init = dict(READ_ONLY_INIT, tools=["Bash", "Read"])
        check = self.checks(_Resumer(init=init))["resumes read-only"]
        self.assertFalse(check.ok)
        self.assertIn("outside Read/Grep/Glob: Bash", check.detail)

    def test_no_init_mcp_servers_or_another_mode_fail(self):
        for init, text in (
            ({}, "no tool list"),
            (dict(READ_ONLY_INIT, mcp_servers=[{"name": "slack"}]), "1 MCP server(s)"),
            (dict(READ_ONLY_INIT, permission_mode="default"), "permission mode was not plan"),
        ):
            with self.subTest(text=text):
                check = self.checks(_Resumer(init=init))["resumes read-only"]
                self.assertFalse(check.ok)
                self.assertIn(text, check.detail)

    def test_keeping_the_parent_id_is_not_a_fork(self):
        self.assertFalse(self.checks(_Resumer(fork=False))["forks the session"].ok)

    def test_a_missing_session_has_to_be_recognised(self):
        self.assertFalse(self.checks(_Resumer(rejects=False))["reports a missing session"].ok)

    def test_a_hook_that_runs_on_resume_fails_and_is_cleaned_up(self):
        checks = self.checks(_Resumer(hooks_on_resume=True))
        self.assertTrue(checks["ignores repository hooks"].ok)
        self.assertFalse(checks["ignores repository hooks on resume"].ok)
        for leftover in (".claude", "hook.py", smoke_live.HOOK_TARGET):
            self.assertFalse(os.path.exists(os.path.join(self.project, leftover)))

    def test_no_parent_session_skips_every_resume_check_and_records_nothing(self):
        class _NoParentRun(_Resumer):
            def run(self, prompt, mode, cwd, **kwargs):
                if kwargs.get("resume_session") is None:
                    return RunResult(False, 1, "", "rate limited", ["fake"], 0.1)
                return super().run(prompt, mode, cwd, **kwargs)

        for provider in (_Resumer(parent=None), _NoParentRun()):
            with self.subTest(provider=type(provider).__name__):
                checks = self.checks(provider)
                self.assertTrue(checks)
                self.assertTrue(all(not check.ok and check.skipped for check in checks.values()))
                self.assertIn("no parent session id", checks["resumes read-only"].detail)
                # As in a whole run, where the fresh read-only check comes first.
                fresh = smoke_live.Check("claude", "stays read-only", True, "refused to write")
                recorded = smoke_live.record_resume(provider, "claude", [fresh, *checks.values()], "fake-1")
                self.assertEqual(recorded, [])
                self.assertFalse(os.path.exists(smoke_live.verified.record_path("claude")))

    def test_confinement_is_only_asked_of_a_confining_provider(self):
        checks = self.checks(_OtherResumer(), name="other")
        self.assertNotIn("resumes confined (absolute)", checks)

    def test_no_other_provider_passes_on_the_filesystem_alone(self):
        check = self.checks(_OtherResumer(), name="other")["resumes read-only"]
        self.assertFalse(check.ok)
        self.assertIn("no reading of a resumed session's restrictions is known for other", check.detail)


class _OtherResumer(_Resumer):
    """A resumer that declares nothing for the live check: the base's defaults."""

    name = "other"
    confines_read_only = False
    repository_hooks_file = ""

    def resumed_session_problem(self, result):
        return base.Provider.resumed_session_problem(self, result)


CODEX_READ_ONLY_INIT = {"sandbox_policy": "read-only", "approval_policy": "never"}


class _CodexResumer(_Resumer):
    """A Codex-shaped resumer: its init is the sandbox policy of the fork's
    rollout, and a repository ``.codex/config.toml`` may change it. Codex's
    members, and Codex's reading of that init."""

    name = "codex"
    confines_read_only = False
    repository_hooks_file = ""
    repository_sandbox_config_file = ".codex/config.toml"

    def resumed_session_problem(self, result):
        return CodexProvider().resumed_session_problem(result)

    def __init__(self, init=None, under_config=None, writes_under_config=False):
        super().__init__(init=dict(CODEX_READ_ONLY_INIT) if init is None else init)
        self.under_config = under_config
        self.writes_under_config = writes_under_config
        self.configs_seen = []

    def run(self, prompt, mode, cwd, **kwargs):
        config = os.path.join(cwd, ".codex", "config.toml")
        result = super().run(prompt, mode, cwd, **kwargs)
        if os.path.isfile(config) and kwargs.get("resume_session"):
            with open(config, encoding="utf-8") as handle:
                self.configs_seen.append(handle.read())
            if self.writes_under_config:
                open(os.path.join(cwd, smoke_live.WRITE_TARGET), "w").close()
            if self.under_config is not None:
                result.session_init = self.under_config
        return result


class TestTheCodexResumeChecks(IsolatedCase):
    def codex_checks(self, provider):
        return {check.name: check for check in smoke_live.check_resume(provider, "codex", self.project)}

    def test_a_read_only_fork_passes_everything_asked_of_codex(self):
        provider = _CodexResumer()
        checks = self.codex_checks(provider)
        self.assertEqual(list(checks), smoke_live.resume_labels(provider))
        self.assertTrue(all(check.ok for check in checks.values()), checks)
        for label in smoke_live.RESUME_CHECKS:
            if "hooks" in label or "confined" in label:
                self.assertNotIn(label, checks)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".claude")))

    def test_a_loose_or_missing_sandbox_policy_fails(self):
        for init in ({"sandbox_policy": "workspace-write"}, {}, None):
            with self.subTest(init=init):
                provider = _CodexResumer()
                setattr(provider, "init", init)
                check = self.codex_checks(provider)["resumes read-only"]
                self.assertFalse(check.ok)
                self.assertIn("sandbox policy was not confirmed read-only", check.detail)
                self.assertFalse(os.path.exists(os.path.join(self.project, smoke_live.WRITE_TARGET)))

    def test_the_repository_config_is_written_for_the_fork_and_removed(self):
        provider = _CodexResumer()
        check = self.codex_checks(provider)["ignores repository config on resume"]
        self.assertTrue(check.ok, check.detail)
        self.assertEqual(provider.configs_seen, [smoke_live.REPO_CONFIG])
        self.assertIn('sandbox_mode = "danger-full-access"', smoke_live.REPO_CONFIG)
        self.assertIn('profile = "loose"', smoke_live.REPO_CONFIG)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".codex")))

    def test_a_fork_loosened_by_the_repository_config_fails(self):
        loosened = self.codex_checks(_CodexResumer(under_config={"sandbox_policy": "danger-full-access"}))
        self.assertFalse(loosened["ignores repository config on resume"].ok)
        self.assertTrue(loosened["resumes read-only"].ok)
        checks = self.codex_checks(_CodexResumer(writes_under_config=True))
        wrote = checks["ignores repository config on resume"]
        self.assertFalse(wrote.ok)
        self.assertIn("it wrote breach.txt", wrote.detail)
        for leftover in (".codex", smoke_live.WRITE_TARGET):
            self.assertFalse(os.path.exists(os.path.join(self.project, leftover)))

    def test_codex_is_checked_while_its_resume_is_still_off(self):
        provider = _CodexResumer()
        setattr(provider, "supports_resume", False)
        checks = self.codex_checks(provider)
        self.assertEqual(list(checks), smoke_live.resume_labels(provider))
        self.assertTrue(all(check.ok for check in checks.values()), checks)

    def test_a_missing_session_refused_before_the_cli_starts_is_reported(self):
        class _RefusesUnknown(_CodexResumer):
            def run(self, prompt, mode, cwd, **kwargs):
                if kwargs.get("resume_session") == smoke_live.MISSING_SESSION:
                    refused = "codex: the session to resume was not started in this workspace"
                    return RunResult(
                        False, 2, "", refused, ["codex"], 0.0, invoked=False, resume_rejected=True
                    )
                return super().run(prompt, mode, cwd, **kwargs)

        check = self.codex_checks(_RefusesUnknown())["reports a missing session"]
        self.assertTrue(check.ok, check.detail)
        self.assertIn("before the CLI started", check.detail)

    def test_the_repository_config_is_asked_only_of_codex(self):
        self.assertIn("ignores repository config on resume", smoke_live.resume_labels(_CodexResumer()))
        for provider in (_Resumer(), _OtherResumer()):
            self.assertNotIn("ignores repository config on resume", smoke_live.resume_labels(provider))


class _OwnChecks(_Resumer):
    """An adapter that names its own required checks and resume mechanism."""

    required_resume_checks = ("resumes read-only", "forks the session")

    def resume_mechanism(self):
        return "--fork-read-only"


class TestTheResumeRecord(IsolatedCase):
    def checks(self, **overrides):
        names = [*smoke_live.verified.REQUIRED_RESUME_CHECKS, "resumes confined (symlink)"]
        checks = []
        for label in names:
            state = overrides.get(label, "ok")
            checks.append(smoke_live.Check("claude", label, state == "ok", "", skipped=state == "skip"))
        return checks

    def record(self, checks):
        os.chdir(self.project)
        return smoke_live.record_resume(_Resumer(), "claude", checks, "fake-1")

    def test_a_pass_is_recorded_and_the_table_entry_printed(self):
        lines = self.record(self.checks(**{"resumes confined (symlink)": "skip"}))
        assert lines is not None
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].ok)
        self.assertEqual(present(lines[0].record)["version"], "9.9.9 (Fake)")
        self.assertTrue(present(lines[0].record)["new"])
        self.assertIn("VERIFIED_RESUME", lines[0].notes[0])
        found = smoke_live.verified.lookup("claude", "9.9.9 (Fake)", "--fake-read-only", self.project)
        self.assertEqual(found["status"], "passed")

    def test_the_message_names_the_record(self):
        with mock.patch.object(smoke_live.config_mod, "on_windows", return_value=False):
            lines = self.record(self.checks())
        path = smoke_live.verified.record_path("claude")
        self.assertEqual(lines[0].detail, "recorded 9.9.9 (Fake) in %s" % path)
        self.assertEqual(present(lines[0].record)["path"], path)

    def test_the_message_says_where_a_store_python_really_keeps_it(self):
        real = self.redirect_config_home()
        lines = self.record(self.checks())
        path = smoke_live.verified.record_path("claude")
        moved = os.path.join(real, "verified", "claude-resume.json")
        self.assertEqual(lines[0].detail, "recorded 9.9.9 (Fake) in %s (stored at %s)" % (path, moved))
        self.assertEqual(present(lines[0].record)["path"], path)

    def test_a_failure_is_recorded(self):
        lines = self.record(self.checks(**{"ignores repository hooks on resume": "fail"}))
        self.assertFalse(lines[0].ok)
        self.assertIn("recorded as failed", lines[0].detail)
        found = smoke_live.verified.lookup("claude", "9.9.9 (Fake)", "--fake-read-only", self.project)
        self.assertEqual(found["status"], "failed")

    def test_a_skipped_required_check_records_nothing(self):
        self.assertEqual(self.record(self.checks(**{"resumes read-only": "skip"})), [])
        self.assertFalse(os.path.exists(smoke_live.verified.record_path("claude")))

    def test_the_adapters_own_checks_and_mechanism_are_recorded(self):
        os.chdir(self.project)
        provider = _OwnChecks()
        checks = [smoke_live.Check("codex", label, True, "") for label in provider.required_resume_checks]
        lines = smoke_live.record_resume(provider, "codex", checks, "fake-1")
        self.assertTrue(lines[0].ok, lines[0].detail)

        def status(mechanism):
            found = smoke_live.verified.lookup(
                "codex", "9.9.9 (Fake)", mechanism, self.project, provider.required_resume_checks
            )
            return found["status"]

        self.assertEqual(status("--fork-read-only"), "passed")
        self.assertEqual(status("--fake-read-only"), "absent")

    def test_an_adapter_that_requires_no_check_records_nothing(self):
        os.chdir(self.project)
        provider = _OwnChecks()
        setattr(provider, "required_resume_checks", ())
        checks = [smoke_live.Check("codex", "resumes read-only", False, "it wrote breach.txt")]
        self.assertEqual(smoke_live.record_resume(provider, "codex", checks, "fake-1"), [])
        self.assertFalse(os.path.exists(smoke_live.verified.record_path("codex")))

    def test_a_required_check_that_is_never_asked_is_named(self):
        """The base's required set, on an adapter that declares no confinement
        and no hooks file: no run could write a pass, and the run says why."""
        os.chdir(self.project)
        provider = _OtherResumer()
        asked = ["stays read-only", *smoke_live.resume_labels(provider)]
        checks = [smoke_live.Check("other", label, True, "") for label in asked]
        lines = smoke_live.record_resume(provider, "other", checks, "fake-1")
        self.assertEqual([line.name for line in lines], ["resume verified"])
        self.assertFalse(lines[0].ok)
        self.assertFalse(lines[0].skipped)
        self.assertEqual(
            lines[0].detail,
            "required_resume_checks names resumes confined (absolute), ignores repository hooks, ignores "
            "repository hooks on resume, which the live check does not ask of this adapter; nothing recorded",
        )
        self.assertFalse(os.path.exists(smoke_live.verified.record_path("other")))

    def test_a_failure_is_still_recorded_when_a_required_check_is_never_asked(self):
        os.chdir(self.project)
        provider = _OtherResumer()
        checks = [smoke_live.Check("other", "stays read-only", True, "")]
        checks.append(smoke_live.Check("other", "resumes read-only", False, "it wrote breach.txt"))
        lines = smoke_live.record_resume(provider, "other", checks, "fake-1")
        self.assertEqual([line.name for line in lines], ["resume recorded as failed"])
        found = smoke_live.verified.lookup("other", "9.9.9 (Fake)", "--fake-read-only", self.project)
        self.assertEqual(found["status"], "failed")

    def test_a_record_inside_the_checkout_is_refused(self):
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        lines = self.record(self.checks())
        self.assertFalse(lines[0].ok)
        self.assertIn("nothing recorded", lines[0].detail)
        self.assertFalse(os.path.exists(smoke_live.verified.record_path("claude")))

    def test_a_record_inside_the_checkout_is_refused_from_a_subdirectory(self):
        self.init_git_repo()
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        subdirectory = os.path.join(self.project, "src")
        os.makedirs(subdirectory)
        os.chdir(subdirectory)
        lines = smoke_live.record_resume(_Resumer(), "claude", self.checks(), "fake-1")
        self.assertFalse(lines[0].ok)
        self.assertIn("nothing recorded", lines[0].detail)
        self.assertFalse(os.path.exists(smoke_live.verified.record_path("claude")))


class TestTheLiveCheckRecord(IsolatedCase):
    """Which version each run checked, for doctor. Names only, never a detail."""

    def run_provider(self, provider):
        original = smoke_live.get_provider
        smoke_live.get_provider = lambda name: provider
        self.addCleanup(setattr, smoke_live, "get_provider", original)
        os.chdir(self.project)
        return smoke_live.check_provider("fake", self.project)

    def test_a_failure_and_a_skip_are_recorded_by_name(self):
        checks = [
            smoke_live.Check("fake", "installed", True, "fake 1"),
            smoke_live.Check("fake", "reports what it spent", False, "secret=abc"),
            smoke_live.Check("fake", "stays confined (symlink)", False, "not tested", skipped=True),
            smoke_live.Check("other", "stays read-only", False, "not this provider"),
        ]
        os.chdir(self.project)
        self.assertEqual(smoke_live.record_smoke("fake", "fake 1", checks), [])
        entry = smoke_live.verified.smoke_status("fake", "fake 1", self.project)["entry"]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["failed"], ["reports what it spent"])
        self.assertEqual(entry["skipped"], ["stays confined (symlink)"])
        with open(smoke_live.verified.smoke_record_path("fake"), encoding="utf-8") as handle:
            self.assertNotIn("secret", handle.read())

    def test_a_provider_run_records_its_version(self):
        checks = self.run_provider(_FakeProvider(usage=Usage()))
        self.assertNotIn("live check recorded", [c.name for c in checks])
        found = smoke_live.verified.smoke_status("fake", "fake 1", self.project)
        self.assertEqual(found["status"], "failed")
        self.assertEqual(found["entry"]["failed"], ["reports what it spent"])

    def test_a_run_that_stops_early_is_still_recorded(self):
        self.run_provider(_FakeProvider(raises=TypeError("unexpected keyword argument")))
        found = smoke_live.verified.smoke_status("fake", "fake 1", self.project)
        self.assertEqual(found["entry"]["failed"], ["answers a review prompt"])

    def test_a_missing_cli_records_nothing(self):
        self.run_provider(_FakeProvider(installed=False))
        self.assertFalse(os.path.exists(smoke_live.verified.smoke_record_path("fake")))

    def test_a_record_inside_the_checkout_is_a_failed_check(self):
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        checks = self.run_provider(_FakeProvider())
        line = verdict(checks, "live check recorded")
        self.assertFalse(line.ok)
        self.assertIn("nothing recorded", line.detail)
        self.assertFalse(os.path.exists(smoke_live.verified.smoke_record_path("fake")))

    def test_a_record_inside_the_checkout_is_refused_from_a_subdirectory(self):
        """Judged against the checkout, as doctor judges it, not the current directory."""
        self.init_git_repo()
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.project, ".ai", "home")
        subdirectory = os.path.join(self.project, "src")
        os.makedirs(subdirectory)
        os.chdir(subdirectory)
        checks = [smoke_live.Check("fake", "installed", True, "fake 1")]
        lines = smoke_live.record_smoke("fake", "fake 1", checks)
        self.assertEqual([c.name for c in lines], ["live check recorded"])
        self.assertIn("nothing recorded", lines[0].detail)
        self.assertFalse(os.path.exists(smoke_live.verified.smoke_record_path("fake")))

    def test_a_write_that_fails_is_a_failed_check_not_a_crash(self):
        """The tokens are spent by then: the run still reports."""
        error = PermissionError(13, "Access is denied", "C:/cfg/secret=abc")
        with mock.patch.object(smoke_live.verified.ws, "write_json", side_effect=error):
            checks = self.run_provider(_FakeProvider(usage=Usage()))
        line = verdict(checks, "live check recorded")
        self.assertFalse(line.ok)
        self.assertIn("PermissionError", line.detail)
        self.assertIn("Access is denied", line.detail)
        self.assertIn("reports what it spent", [c.name for c in checks])


#: The lines ``check_provider`` adds once the checks are done.
RECORD_LINES = ("live check recorded", "resume verified", "resume recorded as failed")


class _LiveLike:
    """Answers every probe smoke_live.py sends as a healthy CLI would.

    Mixed into a built-in adapter, so it carries that adapter's own members
    and readings; only discovery and the run itself are replaced.
    """

    version_text = "9.9.9 (Fake)"
    enforcement = "verified"
    read_tool = "Read"
    resumed_init: Optional[dict] = None
    #: Set to make a resumed session write the file it is asked to.
    breaches = False

    def __init__(self):
        super().__init__()
        self.calls = []
        self.specs = []

    def detect(self):
        return Detection(True, "fake", self.version_text, None)

    def version(self):
        return self.version_text, None

    def resolve_model(self, spec):
        self.specs.append(spec)
        return ResolvedModel("fake", "fake-1", "latest", "fake-1", "fake-1", "test")

    def read_only_enforcement(self):
        return {"status": self.enforcement, "mechanism": "--fake-read-only", "detail": "fake"}

    def run(self, prompt, mode, cwd, **kwargs):
        self.calls.append({"prompt": prompt, "mode": mode, "cwd": cwd, "kwargs": kwargs})
        session = kwargs.get("resume_session")
        if session == smoke_live.MISSING_SESSION:
            return RunResult(False, 1, "", "", ["fake"], 0.1, resume_rejected=True)
        usage = Usage(
            total_tokens=10,
            source="fake",
            tool_uses=1,
            tool_uses_by_name={self.read_tool: 1},
            tool_output_chars=6,
        )
        ok, stdout, warnings = True, "READY", []
        writes = self.enforcement == "unenforced" or bool(session and self.breaches)
        if prompt == smoke_live.WRITE_PROMPT and writes:
            open(os.path.join(cwd, smoke_live.WRITE_TARGET), "w").close()
        elif prompt == smoke_live.IMPLEMENT_PROMPT:
            open(os.path.join(cwd, smoke_live.IMPLEMENT_TARGET), "w").close()
        elif prompt == smoke_live.COMMAND_PROMPT:
            if (kwargs.get("options") or {}).get("skip_permissions"):
                stdout = "true"
            else:
                denied = [{"action": "command", "display_name": "RunCommand"}]
                ok, stdout = False, ""
                warning = agy_module._denied_warning({"denied_actions": denied})
                warnings = [warning] if warning else []
        elif "README.md" in prompt:
            stdout = "smoke"
        elif "--add-dir" in (kwargs.get("extra_args") or []):
            for piece in prompt.split("`")[1::2]:
                if os.path.isfile(piece):
                    with open(piece, encoding="utf-8") as handle:
                        stdout = handle.read()
        return RunResult(
            ok,
            0,
            stdout,
            "",
            ["fake"],
            0.1,
            usage=usage,
            warnings=warnings,
            session_id=str(uuid.uuid4()) if session else PARENT,
            session_init=self.resumed_init if session else None,
        )


def _live_like(base_cls, **attributes):
    return type("_%sLike" % base_cls.__name__, (_LiveLike, base_cls), attributes)


def _claude_like():
    return _live_like(ClaudeProvider, resumed_init=dict(READ_ONLY_INIT))()


def _codex_like():
    return _live_like(CodexProvider, resumed_init=dict(CODEX_READ_ONLY_INIT))()


def _agy_like():
    return _live_like(AgyProvider, enforcement="unenforced", read_tool="view_file")()


class TestTheCheckSequences(IsolatedCase):
    """Which checks each built-in adapter is asked, in order: pinned from the
    script as it was before it stopped naming CLIs."""

    def setUp(self):
        super().setUp()
        patch_symlink(self)

    def labels(self, name, provider):
        with mock.patch.object(smoke_live, "get_provider", lambda _: provider):
            checks = smoke_live.check_provider(name, self.project)
        labels = []
        for check in checks:
            if check.name in RECORD_LINES:
                break
            labels.append(check.name)
        return labels

    def test_claude(self):
        self.assertEqual(
            self.labels("claude", _claude_like()),
            [
                "installed",
                "resolves a model",
                "answers a review prompt",
                "reports what it spent",
                "reports its tool activity",
                "stays read-only",
                "stays confined (absolute)",
                "stays confined (symlink)",
                "--add-dir widens",
                "resumes read-only",
                "forks the session",
                "reports a missing session",
                "resumes confined (absolute)",
                "resumes confined (symlink)",
                "ignores repository hooks",
                "ignores repository hooks on resume",
            ],
        )

    def test_codex(self):
        self.assertEqual(
            self.labels("codex", _codex_like()),
            [
                "installed",
                "resolves a model",
                "answers a review prompt",
                "reports what it spent",
                "reports its tool activity",
                "stays read-only",
                "resumes read-only",
                "forks the session",
                "reports a missing session",
                "ignores repository config on resume",
            ],
        )

    def test_agy(self):
        self.assertEqual(
            self.labels("agy", _agy_like()),
            [
                "installed",
                "resolves a model",
                "answers a review prompt",
                "reports what it spent",
                "reports its tool activity",
                "read-only status matches reality",
                "writes a file in implement mode",
                "names a denied command",
                "runs a command with skip_permissions",
            ],
        )

    def test_every_check_of_a_healthy_cli_passes(self):
        for name, factory in (("claude", _claude_like), ("codex", _codex_like), ("agy", _agy_like)):
            with self.subTest(provider=name):
                with mock.patch.object(smoke_live, "get_provider", lambda _, f=factory: f()):
                    checks = smoke_live.check_provider(name, self.project)
                failed = [(c.name, c.detail) for c in checks if not c.ok and c.name not in RECORD_LINES]
                self.assertEqual(failed, [])


class TestTheRunHelper(IsolatedCase):
    def test_a_run_that_raises_is_no_result_and_the_exception(self):
        provider = _FakeProvider(raises=TypeError("unexpected keyword argument"))
        self.assertEqual(
            smoke_live._run(provider, "p", base.MODE_REVIEW, self.project),
            (None, "TypeError: unexpected keyword argument"),
        )

    def test_a_refused_launch_cannot_be_judged(self):
        refused = RunResult(False, 2, "", "read-only run: refused", ["fake"], 0.0, invoked=False)
        result, why = smoke_live._run(_FakeProvider(result=refused), "p", base.MODE_REVIEW, self.project)
        self.assertIs(result, refused)
        self.assertIn("refused to launch", why or "")

    def test_a_finished_run_can_be_judged(self):
        result, why = smoke_live._run(_FakeProvider(), "p", base.MODE_REVIEW, self.project)
        self.assertTrue(result.ok)
        self.assertIsNone(why)

    def test_the_model_is_passed_only_when_given(self):
        provider = _FakeProvider()
        smoke_live._run(provider, "p", base.MODE_REVIEW, self.project)
        smoke_live._run(provider, "p", base.MODE_REVIEW, self.project, model_spec={"family": "m"})
        self.assertNotIn("model_spec", provider.calls[0]["kwargs"])
        self.assertEqual(provider.calls[1]["kwargs"]["model_spec"], {"family": "m"})
        self.assertEqual(provider.calls[1]["kwargs"]["timeout"], smoke_live.TIMEOUT)


class _HookedAndFailing(_Resumer):
    """Runs the repository's hooks, and then fails."""

    def run(self, prompt, mode, cwd, **kwargs):
        self.calls.append({"prompt": prompt, "mode": mode, "cwd": cwd, "kwargs": kwargs})
        if os.path.isdir(os.path.join(cwd, ".claude")):
            open(os.path.join(cwd, smoke_live.HOOK_TARGET), "w").close()
        return RunResult(False, 1, "", "boom", ["fake"], 0.1)


class TestNotOkResultsKeepTheirDetails(IsolatedCase):
    """A run that did not complete is still read for what it left behind,
    in the order each check read it before the run helper existed."""

    def failed(self, **fields):
        return RunResult(False, 1, "", "boom", ["fake"], 0.1, **fields)

    def test_a_denied_command_on_an_empty_answer_passes(self):
        provider = _AgyLike(result=self.failed(warnings=[AGY_DENIED_COMMAND]))
        checks = smoke_live.check_implement(provider, "agy", self.project)
        check = verdict(checks, "names a denied command")
        self.assertTrue(check.ok, check.detail)
        self.assertEqual(check.detail, "denied RunCommand (command)")

    def test_a_breach_by_a_run_that_failed_is_still_a_breach(self):
        provider = _FakeProvider(writes=smoke_live.WRITE_TARGET, result=self.failed())
        check = smoke_live.check_read_only(provider, "fake", self.project)
        self.assertEqual(check.detail, "it wrote breach.txt in review mode")
        self.assertEqual(check.observed["read_only"], "wrote")

    def test_a_file_written_by_a_run_that_failed_is_still_written(self):
        provider = _AgyLike(writes=smoke_live.IMPLEMENT_TARGET, result=self.failed())
        checks = smoke_live.check_implement(provider, "agy", self.project)
        check = verdict(checks, "writes a file in implement mode")
        self.assertTrue(check.ok, check.detail)
        self.assertEqual(check.detail, "wrote implement.txt")

    def test_unparsed_tool_activity_is_named_before_the_failure(self):
        class _ReportsOutput(_FakeProvider):
            tool_activity_reported = base.TOOL_ACTIVITY_OUTPUT

        check = smoke_live.check_tool_activity(_ReportsOutput(result=self.failed()), "fake", self.project)
        self.assertEqual(check.detail, "no tool activity parsed -- the event shape may have changed")

    def test_a_hook_that_ran_is_named_before_the_failure(self):
        checks = smoke_live._check_hooks(_HookedAndFailing(), "claude", self.project, PARENT)
        self.assertEqual(checks[0].detail, "a command hook from .claude/settings.json ran")


class TestEveryRunCarriesTheModel(IsolatedCase):
    def setUp(self):
        super().setUp()
        patch_symlink(self)

    def test_every_run_of_every_check(self):
        spec = {"family": "m"}
        runs = (("claude", _claude_like, 13), ("agy", _agy_like, 6), ("codex", _codex_like, 6))
        for name, factory, count in runs:
            with self.subTest(provider=name):
                provider = factory()
                with mock.patch.object(smoke_live, "get_provider", lambda _, p=provider: p):
                    smoke_live.check_provider(name, self.project, model_spec=spec, record=False)
                self.assertEqual(provider.specs, [spec])
                self.assertEqual(len(provider.calls), count)
                for call in provider.calls:
                    self.assertEqual(call["kwargs"].get("model_spec"), spec, call["prompt"])
                sessions = [call["kwargs"].get("resume_session") for call in provider.calls]
                if name != "agy":
                    self.assertIn(PARENT, sessions)
                    self.assertIn(smoke_live.MISSING_SESSION, sessions)


class TestTheModelDetail(IsolatedCase):
    def resolved(self, model_spec):
        provider = _FakeProvider()
        with mock.patch.object(smoke_live, "get_provider", lambda _: provider):
            checks = smoke_live.check_provider("fake", self.project, model_spec=model_spec, record=False)
        return provider, verdict(checks, "resolves a model")

    def test_a_named_model_is_what_is_resolved_and_shown(self):
        provider, check = self.resolved({"family": "m"})
        self.assertEqual(provider.specs, [{"family": "m"}])
        self.assertEqual(check.detail, "fake-1 (--model m)")

    def test_without_one_the_cli_default_is_resolved(self):
        provider, check = self.resolved(None)
        self.assertEqual(provider.specs, [None])
        self.assertEqual(check.detail, "fake-1")


class TestTheModelFlag(IsolatedCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = smoke_live.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def spy(self):
        calls = []

        def check_provider(name, root, model_spec=None, record=True):
            calls.append((name, model_spec, record))
            return []

        patcher = mock.patch.object(smoke_live, "check_provider", check_provider)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def cli(self, provider):
        patcher = mock.patch.object(smoke_live, "get_provider", lambda _: provider)
        patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, *argv):
        calls = self.spy()
        code, _, err = self.run_main(*argv)
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        return err

    def test_a_malformed_value_is_refused(self):
        for value in ("agy", "=x", "agy=", "agy=a b", "agy=-x", "agy=--add-dir", "agy=" + "m" * 101):
            with self.subTest(value=value):
                self.assertIn(smoke_live.MODEL_SYNTAX, self.refused("--model", value))

    def test_a_slash_in_the_model_is_accepted(self):
        calls = self.spy()
        self.cli(_FakeProvider())
        code, _, err = self.run_main("--provider", "agy", "--model", "agy=org/model-1")
        self.assertEqual(code, 0, err)
        self.assertNotIn(smoke_live.MODEL_SYNTAX, err)
        self.assertEqual(calls, [("agy", {"family": "org/model-1"}, False)])

    def test_an_unknown_provider_is_refused(self):
        self.assertIn("unknown provider(s): nope", self.refused("--model", "nope=x"))

    def test_a_provider_this_run_does_not_check_is_refused(self):
        err = self.refused("--provider", "claude", "--model", "agy=x")
        self.assertIn("--model names agy, which this run does not check; add --provider agy", err)

    def test_an_offline_provider_is_not_in_the_default_set(self):
        err = self.refused("--model", "mock=x")
        self.assertIn("--model names mock, which this run does not check; add --provider mock", err)

    def test_the_same_provider_twice_is_refused(self):
        err = self.refused("--model", "agy=x", "--model", "agy=y")
        self.assertIn("--model names agy more than once", err)

    def test_a_model_the_adapter_cannot_resolve_is_refused(self):
        family = "mock=%s" % mock_module.UNRESOLVABLE_FAMILY
        self.assertIn("cannot be resolved", self.refused("--provider", "mock", "--model", family))

    def test_a_cli_that_cannot_be_checked_is_refused(self):
        class _Unreadable(_FakeProvider):
            def detect(self):
                raise RuntimeError("boom")

        self.cli(_Unreadable())
        err = self.refused("--provider", "agy", "--model", "agy=x")
        self.assertIn("--model agy=x: could not check the CLI: boom", err)

    def test_the_default_set_keeps_recording_every_other_provider(self):
        calls = self.spy()
        self.cli(_FakeProvider())
        names = ["agy", "claude", "codex", "mock"]
        with mock.patch.object(smoke_live, "available_providers", lambda: names):
            code, _, err = self.run_main("--model", "agy=x")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            calls, [("agy", {"family": "x"}, False), ("claude", None, True), ("codex", None, True)]
        )

    def test_a_cli_that_is_not_installed_uses_no_model(self):
        class _Absent(_FakeProvider):
            def resolve_model(self, spec):
                raise AssertionError("a model was resolved for a CLI that is not installed")

        self.cli(_Absent(installed=False))
        code, out, _ = self.run_main("--provider", "agy", "--model", "agy=x")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL\s+agy\s+installed")
        self.assertIn("note: agy: --model agy=x was not used: the CLI is not installed", out)


class TestRecordsUnderModel(IsolatedCase):
    def setUp(self):
        super().setUp()
        patch_symlink(self)

    def run_main(self, providers, *argv):
        out, err = io.StringIO(), io.StringIO()
        names = sorted(providers)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(smoke_live, "get_provider", lambda name: providers[name]))
            stack.enter_context(mock.patch.object(smoke_live, "available_providers", lambda: names))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = smoke_live.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_breach_under_a_model_is_still_recorded_as_failed(self):
        provider = _claude_like()
        provider.breaches = True
        argv = ("--provider", "claude", "--model", "claude=x")
        code, out, _ = self.run_main({"claude": provider}, *argv)
        self.assertEqual(code, 1)
        self.assertIn("resume recorded as failed", out)
        data, _ = smoke_live.verified.read("claude", self.project)
        self.assertIn("9.9.9 (Fake)", present(data)["failed"])
        self.assertFalse(os.path.exists(smoke_live.verified.smoke_record_path("claude")))

    def test_a_pass_under_a_model_writes_nothing(self):
        paths = [smoke_live.verified.record_path("claude"), smoke_live.verified.smoke_record_path("claude")]
        for path in paths:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b'{"versions": {}}\n')
        argv = ("--provider", "claude", "--model", "claude=x")
        code, out, err = self.run_main({"claude": _claude_like()}, *argv)
        self.assertEqual(code, 0, out + err)
        for path in paths:
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b'{"versions": {}}\n')
        self.assertIn("note: claude: no live-check record and no resume pass written", out)
        self.assertNotIn("resume verified", out)
        self.assertNotIn("VERIFIED_RESUME", out)

    def test_the_json_output_says_the_model_was_overridden(self):
        note = "note: claude: no live-check record and no resume pass written -- --model claude=x"
        for breaches, code in ((False, 0), (True, 1)):
            with self.subTest(breaches=breaches):
                provider = _claude_like()
                provider.breaches = breaches
                argv = ("--json", "--provider", "claude", "--model", "claude=x")
                got, out, err = self.run_main({"claude": provider}, *argv)
                self.assertEqual(got, code, out + err)
                payload = json.loads(out)
                self.assertIsNone(payload["record"])
                self.assertTrue(any(n.startswith(note) for n in payload["notes"]), payload["notes"])
                names = [check["check"] for check in payload["checks"]]
                self.assertEqual("resume recorded as failed" in names, breaches)

    def test_a_provider_without_a_model_records_beside_one_with(self):
        providers = {"claude": _claude_like(), "agy": _agy_like()}
        argv = ("--provider", "claude", "--provider", "agy", "--model", "agy=x")
        code, out, err = self.run_main(providers, *argv)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.exists(smoke_live.verified.smoke_record_path("claude")))
        self.assertTrue(os.path.exists(smoke_live.verified.record_path("claude")))
        self.assertFalse(os.path.exists(smoke_live.verified.smoke_record_path("agy")))
        self.assertFalse(os.path.exists(smoke_live.verified.record_path("agy")))


#: Every live-check member at its default, as ``members`` reports it.
LIVE_CHECK_DEFAULTS = {
    "confines_read_only": False,
    "read_only_widening_args": [],
    "repository_hooks_file": "",
    "repository_sandbox_config_file": "",
    "tool_activity_reported": "none",
    "file_read_tool": "",
    "denied_action_items": False,
    "implement_write_checked": False,
    "permission_bypass_options": None,
    "resumed_session_problem": False,
}


def members(provider):
    """``provider``'s live-check members; for a method, whether it is its own."""
    cls = type(provider)
    return {
        "confines_read_only": provider.confines_read_only,
        "read_only_widening_args": provider.read_only_widening_args("/d"),
        "repository_hooks_file": provider.repository_hooks_file,
        "repository_sandbox_config_file": provider.repository_sandbox_config_file,
        "tool_activity_reported": provider.tool_activity_reported,
        "file_read_tool": provider.file_read_tool,
        "denied_action_items": cls.denied_action_items is not base.Provider.denied_action_items,
        "implement_write_checked": provider.implement_write_checked,
        "permission_bypass_options": provider.permission_bypass_options,
        "resumed_session_problem": cls.resumed_session_problem is not base.Provider.resumed_session_problem,
    }


class TestBuiltInLiveCheckAttributes(unittest.TestCase):
    def test_every_built_in_declares_what_it_was_checked_for(self):
        expected = {
            ClaudeProvider: dict(
                LIVE_CHECK_DEFAULTS,
                confines_read_only=True,
                read_only_widening_args=["--add-dir", "/d"],
                repository_hooks_file=".claude/settings.json",
                tool_activity_reported="calls and output",
                resumed_session_problem=True,
            ),
            CodexProvider: dict(
                LIVE_CHECK_DEFAULTS,
                repository_sandbox_config_file=".codex/config.toml",
                resumed_session_problem=True,
            ),
            AgyProvider: dict(
                LIVE_CHECK_DEFAULTS,
                tool_activity_reported="calls",
                file_read_tool="view_file",
                denied_action_items=True,
                implement_write_checked=True,
                permission_bypass_options={"skip_permissions": True},
            ),
            MockProvider: LIVE_CHECK_DEFAULTS,
        }
        for cls, values in expected.items():
            with self.subTest(provider=cls.__name__):
                self.assertEqual(members(cls()), values)

    def test_the_resume_checks_each_is_asked(self):
        self.assertEqual(
            smoke_live.resume_labels(ClaudeProvider()),
            [
                "resumes read-only",
                "forks the session",
                "reports a missing session",
                "resumes confined (absolute)",
                "resumes confined (symlink)",
                "ignores repository hooks",
                "ignores repository hooks on resume",
            ],
        )
        self.assertEqual(
            smoke_live.resume_labels(CodexProvider()),
            [
                "resumes read-only",
                "forks the session",
                "reports a missing session",
                "ignores repository config on resume",
            ],
        )

    def test_the_resumed_tools_do_not_follow_the_read_only_tools(self):
        self.assertEqual(claude_module._RESUMED_TOOLS, frozenset({"Read", "Grep", "Glob"}))
        init = {"tools": ["Bash", "Read"], "mcp_servers": [], "permission_mode": "plan"}
        result = RunResult(True, 0, "", "", [], 0.0, session_init=init)
        with mock.patch.object(claude_module, "_READ_ONLY_TOOLS", "Read,Grep,Glob,Bash"):
            problem = ClaudeProvider().resumed_session_problem(result)
        self.assertEqual(problem, "the resumed session was offered tools outside Read/Grep/Glob: Bash")


class _ReadsWithATool(_FakeProvider):
    tool_activity_reported = base.TOOL_ACTIVITY_OUTPUT
    file_read_tool = "view_file"


def tool_run(stdout="smoke", by_name=None, chars=6):
    """A finished run that read README.md with one tool, and printed ``chars``."""
    usage = Usage(
        total_tokens=10,
        source="fake",
        tool_uses=1,
        tool_uses_by_name={"view_file": 1} if by_name is None else by_name,
        tool_output_chars=chars,
    )
    return RunResult(True, 0, stdout, "", ["fake"], 0.1, usage=usage)


class TestToolActivityValues(IsolatedCase):
    def check(self, provider, name="fake"):
        return smoke_live.check_tool_activity(provider, name, self.project)

    def test_an_unknown_value_fails_without_a_run(self):
        class _Unknown(_FakeProvider):
            tool_activity_reported = "calls+output"

        provider = _Unknown()
        check = self.check(provider)
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, "unknown tool_activity_reported 'calls+output'")
        self.assertEqual(provider.calls, [])

    def test_a_file_tool_is_not_asked_of_an_adapter_that_reports_none(self):
        class _NoneWithATool(_FakeProvider):
            file_read_tool = "view_file"

        provider = _NoneWithATool()
        check = self.check(provider)
        self.assertTrue(check.ok)
        self.assertIn("by design", check.detail)
        self.assertEqual(provider.calls, [])

    def test_calls_and_output_with_a_file_tool(self):
        cases = [
            (tool_run(stdout="1"), False, "the reply did not carry README.md's content"),
            (tool_run(chars=0), False, "no output to pair"),
            (tool_run(by_name={"Read": 1}), False, "expected view_file in tool_uses_by_name, got Read x1"),
            (tool_run(), True, "1 use(s) [view_file x1], 6 observed output chars"),
        ]
        for result, ok, detail in cases:
            with self.subTest(detail=detail):
                provider = _ReadsWithATool(result=result)
                check = self.check(provider)
                self.assertEqual(check.ok, ok, check.detail)
                self.assertIn(detail, check.detail)
                self.assertEqual(provider.calls[0]["prompt"], smoke_live.FILE_TOOL_PROMPT)

    def test_calls_only_with_a_file_tool_needs_the_reply_too(self):
        check = self.check(_AgyLike(result=agy_run(stdout="1")), "agy")
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, "the reply did not carry README.md's content")


class _CustomHooks(_Resumer):
    """Declares its hooks at ``cfg/hooks.json``, and notes on each run
    whether the file was there."""

    repository_hooks_file = "cfg/hooks.json"

    def __init__(self, outcome="ok"):
        super().__init__()
        self.outcome = outcome
        self.seen = []

    def run(self, prompt, mode, cwd, **kwargs):
        self.calls.append({"prompt": prompt, "mode": mode, "cwd": cwd, "kwargs": kwargs})
        self.seen.append(os.path.isfile(os.path.join(cwd, "cfg", "hooks.json")))
        if self.outcome == "raises":
            raise RuntimeError("boom")
        if self.outcome == "fails":
            return RunResult(False, 1, "", "boom", ["fake"], 0.1)
        return RunResult(True, 0, "READY", "", ["fake"], 0.1)


def _hooks_at(relative):
    return type("_HooksAt", (_Resumer,), {"repository_hooks_file": relative})()


class TestFixturePaths(IsolatedCase):
    def hooks(self, provider):
        return smoke_live._check_hooks(provider, "claude", self.project, PARENT)

    def test_a_path_outside_the_repository_is_refused_unwritten(self):
        for relative in ("/x", "\\x", "C:x", "C:\\x", "../x", "a/../../x", "a\\..\\..\\x", *NOT_A_FILE):
            with self.subTest(relative=relative):
                provider = _hooks_at(relative)
                before = sorted(os.listdir(self.project))
                checks = self.hooks(provider)
                expected = "repository_hooks_file %r is not a path inside the repository" % relative
                self.assertEqual([c.detail for c in checks], [expected, expected])
                self.assertFalse(any(c.ok for c in checks))
                self.assertEqual(provider.calls, [])
                self.assertEqual(sorted(os.listdir(self.project)), before)
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))

    def test_a_file_already_there_is_not_overwritten(self):
        path = self.write(".claude/settings.json", "keep\n")
        provider = _Resumer()
        checks = self.hooks(provider)
        self.assertFalse(any(c.ok for c in checks))
        self.assertIn("'.claude/settings.json' is already in the repository", checks[0].detail)
        self.assertEqual(provider.calls, [])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep\n")

    def test_a_path_through_a_link_out_of_the_repository_is_refused(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        link = os.path.join(self.project, "link")
        try:
            make_dir_link(link, outside)
            linked = True
        except (OSError, subprocess.CalledProcessError):
            # No link here: resolve one the way the platform would have.
            os.makedirs(link)
            linked = False
            real = os.path.realpath
            under = os.path.normcase(os.path.join(real(self.project), "link"))

            def resolve(path, *args, **kwargs):
                resolved = real(path, *args, **kwargs)
                if os.path.normcase(resolved).startswith(under):
                    return outside + resolved[len(under) :]
                return resolved

            patcher = mock.patch.object(smoke_live.os.path, "realpath", resolve)
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            provider = _hooks_at("link/hooks.json")
            checks = self.hooks(provider)
            expected = "repository_hooks_file 'link/hooks.json' is not a path inside the repository"
            self.assertEqual(checks[0].detail, expected)
            self.assertEqual(provider.calls, [])
            self.assertEqual(os.listdir(outside), [])
        finally:
            if linked:
                remove_link(link)

    def test_a_custom_path_is_written_there_and_removed_whatever_the_run_did(self):
        for outcome in ("ok", "raises", "fails"):
            with self.subTest(outcome=outcome):
                provider = _CustomHooks(outcome)
                checks = self.hooks(provider)
                self.assertEqual(len(checks), 2)
                self.assertEqual(provider.seen, [True, True])
                self.assertFalse(os.path.exists(os.path.join(self.project, "cfg")))
                self.assertFalse(os.path.exists(os.path.join(self.project, "hook.py")))

    def test_a_directory_that_was_already_there_is_kept(self):
        other = self.write(".claude/other.json", "keep\n")
        checks = self.hooks(_Resumer())
        self.assertTrue(all(check.ok for check in checks), [c.detail for c in checks])
        self.assertFalse(os.path.exists(os.path.join(self.project, ".claude", "settings.json")))
        with open(other, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep\n")


#: Values of a path member that name no file at all.
NOT_A_FILE = ("", ".", "./", None)


class _ConfigRun(_CodexResumer):
    """A Codex-shaped resumer whose resumed run notes whether the repository
    config was there, and then answers, raises or fails."""

    def __init__(self, outcome="ok", relative: Any = ".codex/config.toml"):
        super().__init__()
        self.outcome = outcome
        setattr(self, "repository_sandbox_config_file", relative)
        self.seen = []

    def run(self, prompt, mode, cwd, **kwargs):
        self.seen.append(os.path.isfile(os.path.join(cwd, ".codex", "config.toml")))
        if self.outcome == "raises":
            self.calls.append({"prompt": prompt, "mode": mode, "cwd": cwd, "kwargs": kwargs})
            raise RuntimeError("boom")
        if self.outcome == "fails":
            self.calls.append({"prompt": prompt, "mode": mode, "cwd": cwd, "kwargs": kwargs})
            return RunResult(False, 1, "", "boom", ["fake"], 0.1)
        return super().run(prompt, mode, cwd, **kwargs)


class TestTheRepositoryConfigFixture(IsolatedCase):
    """``repository_sandbox_config_file`` is the member whose fixture loosens
    the sandbox: it must never be written outside the repository, over a
    file that is there, or left behind."""

    def config(self, provider):
        return smoke_live._check_repository_config(provider, "codex", self.project, PARENT)

    def test_a_path_that_is_not_inside_the_repository_is_refused_unwritten(self):
        for relative in ("../x", "/x", "C:x", "a/../../x", *NOT_A_FILE):
            with self.subTest(relative=relative):
                provider = _ConfigRun(relative=relative)
                before = sorted(os.listdir(self.project))
                check = self.config(provider)
                self.assertFalse(check.ok)
                expected = "repository_sandbox_config_file %r is not a path inside the repository" % relative
                self.assertEqual(check.detail, expected)
                self.assertEqual(provider.calls, [])
                self.assertEqual(sorted(os.listdir(self.project)), before)
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))

    def test_a_config_already_there_is_not_overwritten(self):
        path = self.write(".codex/config.toml", "keep\n")
        provider = _ConfigRun()
        check = self.config(provider)
        self.assertFalse(check.ok)
        expected = "repository_sandbox_config_file '.codex/config.toml' is already in the repository"
        self.assertIn(expected, check.detail)
        self.assertEqual(provider.calls, [])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep\n")

    def test_the_config_is_removed_whatever_the_run_did(self):
        for outcome, detail in (
            ("raises", "RuntimeError: boom"),
            ("fails", "the run did not complete (exit 1): boom"),
        ):
            with self.subTest(outcome=outcome):
                provider = _ConfigRun(outcome)
                check = self.config(provider)
                self.assertFalse(check.ok)
                self.assertEqual(check.detail, detail)
                self.assertEqual(provider.seen, [True])
                self.assertFalse(os.path.exists(os.path.join(self.project, ".codex")))

    def test_a_directory_that_was_already_there_is_kept(self):
        other = self.write(".codex/other.toml", "keep\n")
        check = self.config(_ConfigRun())
        self.assertTrue(check.ok, check.detail)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".codex", "config.toml")))
        with open(other, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep\n")


class TestTheWideningGate(IsolatedCase):
    def test_widening_arguments_the_gate_refuses_are_not_run(self):
        class _RefusedWidening(_Reader):
            def read_only_widening_args(self, directory):
                return ["--bogus", directory]

        patch_symlink(self)
        provider = _RefusedWidening(confined=True)
        checks = {check.name: check for check in smoke_live.check_confined(provider, "claude", self.project)}
        check = checks["--add-dir widens"]
        self.assertFalse(check.ok)
        prefix = "the adapter's read-only gate refuses its widening arguments: read-only run: '--bogus'"
        self.assertTrue(check.detail.startswith(prefix), check.detail)
        self.assertNotIn("dev-orchestra-smoke-outside-", check.detail)
        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(any(call["kwargs"].get("extra_args") for call in provider.calls))

    def test_claudes_own_widening_passes_its_gate(self):
        provider = ClaudeProvider()
        args = provider.read_only_widening_args(os.path.join(self.tmp, "outside"))
        self.assertEqual(provider.read_only_arg_problems(base.MODE_REVIEW, extra_args=args), [])


def _accepts_add_dir(provider, raw_args, source):
    """A read-only gate that, like Claude's, accepts ``--add-dir <path>``."""
    raw = list(raw_args)
    if len(raw) == 2 and raw[0] == "--add-dir":
        return []
    return base.Provider.refused_read_only_args(provider, raw, source)


def _shell_denied(provider, warning):
    """A reading of a denial: any warning that says one was."""
    return [("Shell", "command")] if "denied" in warning else None


class TestUserAdaptersInTheLiveCheck(IsolatedCase):
    """The documented ``mycli`` adapter, as written and with each member set."""

    def setUp(self):
        super().setUp()
        patch_symlink(self)
        self.write_user_provider("mycli")
        self.load_user_providers()
        from orchestrator import providers

        self.mycli = type(providers.get_provider("mycli"))

    def opted(self, result=None, **declared):
        """A subclass of the loaded adapter with ``declared`` members, whose
        runs are recorded and answered with ``result``."""
        calls = []
        answer = result or RunResult(True, 0, "nothing", "", ["mycli"], 0.1)

        def run(provider, prompt, mode, cwd, **kwargs):
            calls.append({"prompt": prompt, "mode": mode, "kwargs": kwargs})
            return answer

        cls = type("_Opted", (self.mycli,), {"run": run, **declared})
        return cls(), calls

    def test_one_that_declares_nothing_is_asked_nothing_new(self):
        provider = self.mycli()
        self.assertEqual(members(provider), LIVE_CHECK_DEFAULTS)
        with mock.patch.object(self.mycli, "run", side_effect=AssertionError("ran")):
            check = smoke_live.check_tool_activity(provider, "mycli", self.project)
            self.assertEqual(smoke_live.check_confined(provider, "mycli", self.project), [])
            self.assertEqual(smoke_live.check_implement(provider, "mycli", self.project), [])
        self.assertTrue(check.ok)
        self.assertIn("by design", check.detail)
        self.assertEqual(
            smoke_live.resume_labels(provider),
            ["resumes read-only", "forks the session", "reports a missing session"],
        )

    def test_confining_with_widening_arguments(self):
        provider, calls = self.opted(
            confines_read_only=True,
            read_only_widening_args=lambda provider, directory: ["--add-dir", directory],
            refused_read_only_args=_accepts_add_dir,
        )
        checks = smoke_live.check_confined(provider, "mycli", self.project)
        self.assertEqual(
            [check.name for check in checks],
            ["stays confined (absolute)", "stays confined (symlink)", "--add-dir widens"],
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1]["kwargs"]["extra_args"][0], "--add-dir")

    def test_confining_without_widening_arguments(self):
        provider, calls = self.opted(confines_read_only=True)
        checks = {check.name: check for check in smoke_live.check_confined(provider, "mycli", self.project)}
        widens = checks["--add-dir widens"]
        self.assertFalse(widens.ok)
        self.assertEqual(widens.detail, "the adapter declares no read-only widening arguments")
        self.assertEqual(len(calls), 2)

    def test_the_implement_write_alone(self):
        provider, _ = self.opted(implement_write_checked=True)
        checks = smoke_live.check_implement(provider, "mycli", self.project)
        self.assertEqual([check.name for check in checks], ["writes a file in implement mode"])

    def test_a_bypass_with_its_denials(self):
        denied = RunResult(True, 0, "", "", ["mycli"], 0.1, warnings=["mycli denied Shell (command)"])
        provider, _ = self.opted(
            result=denied,
            permission_bypass_options={"args": ["--yes"]},
            denied_action_items=lambda provider, warning: [("Shell", "command")],
        )
        check = verdict(smoke_live.check_implement(provider, "mycli", self.project), "names a denied command")
        self.assertTrue(check.ok, check.detail)
        self.assertEqual(check.detail, "denied Shell (command)")

    def test_a_bypass_without_denials(self):
        provider, calls = self.opted(permission_bypass_options={"args": ["--yes"]})
        checks = {check.name: check for check in smoke_live.check_implement(provider, "mycli", self.project)}
        self.assertEqual(
            checks["names a denied command"].detail,
            "the adapter declares no denied_action_items, so a denied command cannot be read",
        )
        self.assertIn("runs a command with skip_permissions", checks)
        self.assertEqual([call["kwargs"].get("options") for call in calls], [{"args": ["--yes"]}])

    def test_denials_without_a_bypass(self):
        provider, calls = self.opted(denied_action_items=lambda provider, warning: [("Shell", "command")])
        self.assertEqual(smoke_live.check_implement(provider, "mycli", self.project), [])
        self.assertEqual(calls, [])

    def test_a_bypass_of_options_the_adapter_does_not_take(self):
        provider, calls = self.opted(
            permission_bypass_options={"bogus": True},
            denied_action_items=lambda provider, warning: [("Shell", "command")],
        )
        checks = smoke_live.check_implement(provider, "mycli", self.project)
        check = verdict(checks, "runs a command with skip_permissions")
        self.assertFalse(check.ok)
        expected = "permission_bypass_options names bogus, not options this adapter takes"
        self.assertEqual(check.detail, expected)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("options", calls[0]["kwargs"])

    def bypassed(self, warning, **declared):
        answer = RunResult(True, 0, "true", "", ["mycli"], 0.1, warnings=[warning])
        provider, _ = self.opted(result=answer, permission_bypass_options={"args": ["--yes"]}, **declared)
        return verdict(smoke_live.check_implement(provider, "mycli", self.project), BYPASS)

    def test_the_bypass_reads_a_denial_as_the_adapter_does(self):
        def refused(provider, warning):
            return [("Shell", "command")] if warning.startswith("mycli refused ") else None

        passed_through = self.bypassed("permission denied: /tmp/x", denied_action_items=refused)
        self.assertTrue(passed_through.ok, passed_through.detail)
        denial = self.bypassed("mycli refused Shell (command)", denied_action_items=refused)
        self.assertFalse(denial.ok)
        self.assertEqual(denial.detail, "mycli refused Shell (command)")

    def test_the_bypass_of_an_adapter_with_no_reading_looks_for_the_word(self):
        check = self.bypassed("permission denied: /tmp/x")
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, "permission denied: /tmp/x")
        self.assertTrue(self.bypassed("mycli refused Shell (command)").ok)

    def counted(self, ok=True):
        usage = Usage(total_tokens=1, source="mycli", tool_uses=1, tool_uses_by_name={"x": 1})
        warnings = ["mycli denied Shell (command)"]
        return RunResult(ok, 0 if ok else 1, "1", "boom", ["mycli"], 0.1, usage=usage, warnings=warnings)

    def test_calls_only_without_denials_cannot_see_one(self):
        provider, calls = self.opted(result=self.counted(), tool_activity_reported=base.TOOL_ACTIVITY_CALLS)
        check = smoke_live.check_tool_activity(provider, "mycli", self.project)
        self.assertTrue(check.ok, check.detail)
        self.assertEqual(check.detail, "1 use(s) [x x1]; output chars not reported by mycli")
        self.assertEqual(calls[0]["prompt"], smoke_live.TOOL_PROMPT)
        failed = self.counted(ok=False)
        provider, _ = self.opted(result=failed, tool_activity_reported=base.TOOL_ACTIVITY_CALLS)
        check = smoke_live.check_tool_activity(provider, "mycli", self.project)
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, "the run did not complete (exit 1): boom")

    def test_calls_only_with_denials_fails_on_one(self):
        provider, _ = self.opted(
            result=self.counted(),
            tool_activity_reported=base.TOOL_ACTIVITY_CALLS,
            denied_action_items=_shell_denied,
        )
        check = smoke_live.check_tool_activity(provider, "mycli", self.project)
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, '"mycli denied Shell (command)"')

    def test_resuming_without_a_reading_of_the_session(self):
        def run(provider, prompt, mode, cwd, **kwargs):
            session = kwargs.get("resume_session")
            if session == smoke_live.MISSING_SESSION:
                return RunResult(False, 1, "", "", ["mycli"], 0.1, resume_rejected=True)
            return RunResult(True, 0, "READY", "", ["mycli"], 0.1, session_id=str(uuid.uuid4()))

        provider, _ = self.opted(run=run, resume_args=lambda provider, session_id: ["--resume", session_id])
        checks = {check.name: check for check in smoke_live.check_resume(provider, "mycli", self.project)}
        check = checks["resumes read-only"]
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, "no reading of a resumed session's restrictions is known for mycli")
        self.assertFalse(os.path.exists(os.path.join(self.project, smoke_live.WRITE_TARGET)))


BYPASS = "runs a command with skip_permissions"


class TestTheBypassOptions(IsolatedCase):
    def bypass_check(self, provider):
        return verdict(smoke_live.check_implement(provider, "agy", self.project), BYPASS)

    def test_a_bypass_that_cannot_go_on_a_run_fails_without_one(self):
        for bypass, detail in (
            (
                ["skip_permissions"],
                "permission_bypass_options names ['skip_permissions'], not options this adapter takes",
            ),
            (
                {"skip_permissions": "x"},
                "permission_bypass_options is refused by the adapter: "
                "options.skip_permissions must be true or false",
            ),
        ):
            with self.subTest(bypass=bypass):
                provider = type("_Bypass", (_AgyLike,), {"permission_bypass_options": bypass})()
                check = self.bypass_check(provider)
                self.assertFalse(check.ok)
                self.assertEqual(check.detail, detail)
                runs = [call for call in provider.calls if call["prompt"] == smoke_live.COMMAND_PROMPT]
                self.assertEqual(len(runs), 1)
                self.assertNotIn("options", runs[0]["kwargs"])

    def test_the_declared_bypass_is_never_changed(self):
        """It is a class attribute shared by every instance: what validation
        or a run does to the options it is handed stays with them."""

        class _Mutates(_AgyLike):
            def validate_options(self, options):
                if isinstance(options, dict):
                    options["validated"] = True
                    options.pop("skip_permissions", None)
                return []

            def run(self, prompt, mode, cwd, **kwargs):
                options = kwargs.get("options")
                if isinstance(options, dict):
                    options["skip_permissions"] = False
                    options["ran"] = True
                return super().run(prompt, mode, cwd, **kwargs)

        provider = _Mutates(stdout="true")
        self.bypass_check(provider)
        self.assertEqual(AgyProvider.permission_bypass_options, {"skip_permissions": True})
        self.assertEqual(_Mutates.permission_bypass_options, {"skip_permissions": True})
        self.assertIs(_Mutates.permission_bypass_options, AgyProvider.permission_bypass_options)
        runs = [call for call in provider.calls if "options" in call["kwargs"]]
        self.assertEqual(len(runs), 1)


class TestTheOfflineProviders(unittest.TestCase):
    def test_doctor_and_the_script_share_one_list(self):
        from orchestrator import doctor

        self.assertIs(doctor.OFFLINE, smoke_live.OFFLINE)


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
