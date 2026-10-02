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
import json
import os
import shutil
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from typing import Optional, Sequence

from helpers import REPO_ROOT, IsolatedCase, present

# isort: split
# ``helpers`` first: it puts ``scripts/`` on the path, where the script under
# test lives, and fixes the environment before ``smoke_live`` imports the
# provider registry -- which would otherwise import the real user's adapters.
import smoke_live

from orchestrator.providers.base import Detection, ResolvedModel, RunResult, Usage


class _FakeProvider:
    """A provider-shaped object that never starts a process."""

    name = "fake"
    executable = "fake"

    def __init__(self, installed=True, raises=None, writes=None, usage=None, stdout="READY", result=None):
        self._installed = installed
        self._raises = raises
        self._writes = writes
        self._usage = usage if usage is not None else Usage(total_tokens=10, source="fake")
        self._stdout = stdout
        self._result = result
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
        if self._result is not None:
            return self._result
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
    """

    name = "claude"

    def __init__(self, confined=False, reads=True):
        super().__init__()
        self.confined = confined
        self.reads = reads

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
        """A symlink that works everywhere: Windows needs a privilege for one."""
        import shutil

        original = smoke_live.os.symlink
        smoke_live.os.symlink = lambda source, target: shutil.copyfile(source, target)
        self.addCleanup(setattr, smoke_live.os, "symlink", original)

    def by_name(self, provider):
        return {check.name: check for check in smoke_live.check_confined(provider, "claude", self.project)}

    def test_only_confining_providers_are_checked(self):
        self.assertEqual(smoke_live.check_confined(_Reader(), "codex", self.project), [])

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

        original = smoke_live.os.symlink
        smoke_live.os.symlink = refuse
        self.addCleanup(setattr, smoke_live.os, "symlink", original)
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

    def expect_tools(self, providers=("fake",)):
        original = smoke_live.REPORTS_TOOLS
        smoke_live.REPORTS_TOOLS = providers
        self.addCleanup(setattr, smoke_live, "REPORTS_TOOLS", original)

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
        smoke_live.check_provider = lambda name, root: list(checks)
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


#: Read by ``record_resume`` from the module the provider's class lives in,
#: the way it reads the claude adapter's table. Empty: nothing is built in.
VERIFIED_RESUME = {}

PARENT = "66666666-6666-4666-8666-666666666666"
READ_ONLY_INIT = {"tools": ["Glob", "Grep", "Read"], "mcp_servers": [], "permission_mode": "plan"}


class _Resumer(_Reader):
    """A CLI that resumes, with whatever init it is told to report."""

    supports_resume = True
    required_resume_checks: Sequence[str] = smoke_live.verified.REQUIRED_RESUME_CHECKS

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
        original = smoke_live.os.symlink
        smoke_live.os.symlink = lambda source, target: shutil.copyfile(source, target)
        self.addCleanup(setattr, smoke_live.os, "symlink", original)

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
        self.assertEqual(sorted(checks), sorted(smoke_live.resume_labels("claude")))
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
        from orchestrator.providers.agy import AgyProvider
        from orchestrator.providers.claude import ClaudeProvider
        from orchestrator.providers.codex import CodexProvider

        for cls in (ClaudeProvider, CodexProvider, AgyProvider):
            with self.subTest(provider=cls.__name__):
                provider = cls()
                self.assertIsInstance(tuple(provider.required_resume_checks), tuple)
                self.assertIsInstance(provider.resume_mechanism(), str)

    def test_a_read_only_resumed_session_passes_everything(self):
        provider = _Resumer()
        checks = self.checks(provider)
        self.assertEqual(sorted(checks), sorted(smoke_live.resume_labels("claude")))
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
                recorded = smoke_live.record_resume(provider, "claude", list(checks.values()), "fake-1")
                self.assertEqual(recorded, [])
                self.assertFalse(os.path.exists(smoke_live.verified.record_path("claude")))

    def test_confinement_is_only_asked_of_a_confining_provider(self):
        checks = self.checks(_Resumer(), name="other")
        self.assertNotIn("resumes confined (absolute)", checks)

    def test_no_other_provider_passes_on_the_filesystem_alone(self):
        check = self.checks(_Resumer(), name="other")["resumes read-only"]
        self.assertFalse(check.ok)
        self.assertIn("no reading of a resumed session's restrictions is known for other", check.detail)


CODEX_READ_ONLY_INIT = {"sandbox_policy": "read-only", "approval_policy": "never"}


class _CodexResumer(_Resumer):
    """A Codex-shaped resumer: its init is the sandbox policy of the fork's
    rollout, and a repository ``.codex/config.toml`` may change it."""

    name = "codex"

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
        self.assertEqual(list(checks), smoke_live.resume_labels("codex"))
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
        self.assertEqual(list(checks), smoke_live.resume_labels("codex"))
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
        self.assertIn("ignores repository config on resume", smoke_live.resume_labels("codex"))
        for name in ("claude", "other"):
            self.assertNotIn("ignores repository config on resume", smoke_live.resume_labels(name))


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
        from unittest import mock

        error = PermissionError(13, "Access is denied", "C:/cfg/secret=abc")
        with mock.patch.object(smoke_live.verified.ws, "write_json", side_effect=error):
            checks = self.run_provider(_FakeProvider(usage=Usage()))
        line = verdict(checks, "live check recorded")
        self.assertFalse(line.ok)
        self.assertIn("PermissionError", line.detail)
        self.assertIn("Access is denied", line.detail)
        self.assertIn("reports what it spent", [c.name for c in checks])


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
