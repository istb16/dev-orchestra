"""Every adapter must answer the call the orchestrator actually makes.

An adapter overrides ``_launch`` (it used to override ``run``, which is now
the read-only gate every adapter shares) to do something the base class
cannot: Codex
writes its final message to a file and reads it back, rather than having its
log scraped. An override like that has to repeat the base signature, and a
repeated signature drifts.

It drifted. ``idle_timeout`` was added to ``Provider.run`` and not to
``CodexProvider.run``, so every real call raised ``TypeError`` -- and
``options`` was accepted but never passed on, so a configured ``sandbox``
was silently replaced by the default. Neither was caught, because the suite
reviews with ``MockProvider``, which overrides ``run`` outright and therefore
exercises no adapter's signature but its own.

So these tests do not test a provider. They test the *seam* between the
orchestrator and every provider registered, present and future, and they do
it without starting a process.
"""

from __future__ import annotations

import inspect
import os
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple

from helpers import CLAUDE_HELP, IsolatedCase, usage_dict

from orchestrator import execution, providers, verified
from orchestrator.providers import base


class _Completed:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


#: Exactly what `run`/`review run` pass. Written out rather than generated
#: from the base signature, so a keyword the orchestrator sends and an
#: adapter forgets cannot both disappear from the test at once.
CALL = {
    "model_spec": {"family": "default", "version": "latest"},
    "timeout": 30,
    "extra_args": [],
    "env": {},
    "options": {},
    "idle_timeout": 5.0,
}


class _Outcome:
    """A finished child process, without there having been one."""

    def __init__(self):
        self.exit_code = 0
        self.stdout = "done"
        self.stderr = ""
        self.duration = 0.1
        self.timed_out = False
        self.stalled = False
        self.idle_for = 0.0
        self.orphans_possible = False

    @property
    def ok(self) -> bool:
        return True


class TestEveryAdapterTakesTheWholeCall(IsolatedCase):
    def adapters(self):
        return [providers.get_provider(name) for name in providers.available_providers()]

    def test_every_run_accepts_every_keyword_the_cli_sends(self):
        """The TypeError this file exists for. A not-installed CLI returns
        early inside the base method, so nothing is spawned -- but the call
        has to get that far, which is the whole point."""
        for provider in self.adapters():
            provider.which = lambda: None
            with self.subTest(provider=provider.name):
                result = provider.run("prompt", base.MODE_REVIEW, self.project, **CALL)
                self.assertIsNotNone(result)

    def test_every_run_signature_covers_the_base_signature(self):
        """Caught statically as well, because an adapter can be added by
        someone who never runs it through the orchestrator."""
        expected = set(inspect.signature(base.Provider.run).parameters)
        for provider in self.adapters():
            with self.subTest(provider=provider.name):
                actual = set(inspect.signature(type(provider).run).parameters)
                self.assertEqual(expected - actual, set(), "%s.run is missing keywords" % provider.name)

    def test_every_launch_signature_covers_the_base_signature(self):
        """``_launch`` is where adapters override now, so it drifts the same way."""
        expected = set(inspect.signature(base.Provider._launch).parameters)
        for provider in self.adapters():
            with self.subTest(provider=provider.name):
                actual = set(inspect.signature(type(provider)._launch).parameters)
                self.assertEqual(expected - actual, set(), "%s._launch is missing keywords" % provider.name)

    def test_no_adapter_overrides_the_gate(self):
        """``run`` holds the read-only raw-argument gate; an adapter that
        overrides it runs without one."""
        for provider in self.adapters():
            with self.subTest(provider=provider.name):
                self.assertIs(type(provider).run, base.Provider.run)

    def test_a_not_installed_cli_is_reported_not_raised(self):
        for provider in self.adapters():
            if provider.name == "mock":
                continue  # the mock is always installed; that is its job
            provider.which = lambda: None
            with self.subTest(provider=provider.name):
                result = provider.run("prompt", base.MODE_REVIEW, self.project, **CALL)
                self.assertFalse(result.ok)
                self.assertFalse(result.invoked)


class TestUserAdaptersTakeTheWholeCall(TestEveryAdapterTakesTheWholeCall):
    """The same seam for an adapter from the user's config directory -- the
    documented minimal example, so a change to ``run`` that would break
    every adapter written from it fails here first. Loaded explicitly: the
    suite never reads the real user's directory."""

    def setUp(self):
        super().setUp()
        self.write_user_provider("mycli")
        providers.load_user_providers()

    def adapters(self):
        return [providers.get_provider("mycli")]


class TestUserAdaptersPassTheGate(IsolatedCase):
    """The documented template only implements ``build_command``, which
    forwards raw arguments untouched. The gate still holds, because it is in
    the ``run`` the template inherits."""

    def setUp(self):
        super().setUp()
        self.write_user_provider("mycli")
        providers.load_user_providers()
        self.seen = {}
        original = execution.execute
        self.addCleanup(setattr, execution, "execute", original)

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.seen["command"] = list(command)
            return _Outcome()

        execution.execute = execute
        self.provider = providers.get_provider("mycli")
        self.provider.which = lambda: "mycli"
        self.provider.version = lambda: ("mycli 1.2.0", None)

    def test_an_unspecified_adapter_still_runs(self):
        self.assertEqual(self.provider.read_only_enforcement()["status"], "unspecified")
        result = self.provider.run("prompt", base.MODE_REVIEW, self.project)
        self.assertTrue(result.ok)
        self.assertIn("--read-only", self.seen["command"])

    def test_raw_arguments_are_refused_in_review(self):
        result = self.provider.run("prompt", base.MODE_REVIEW, self.project, options={"args": ["--yolo"]})
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertNotIn("command", self.seen)

    def test_raw_arguments_still_reach_implement(self):
        self.provider.run("prompt", base.MODE_IMPLEMENT, self.project, options={"args": ["--yolo"]})
        self.assertIn("--yolo", self.seen["command"])


class TestOptionsReachTheCommand(IsolatedCase):
    """An override that accepts ``options`` and does not forward them turns a
    configured policy into a default, with nothing said."""

    def setUp(self):
        super().setUp()
        self.seen = {}
        self.original = execution.execute
        self.addCleanup(self.restore)

    def restore(self):
        execution.execute = self.original

    def capture(self, provider):
        """Run the adapter with the child process replaced by a recording."""

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.seen["command"] = list(command)
            self.seen["idle_timeout"] = idle_timeout
            return _Outcome()

        execution.execute = execute
        provider.which = lambda: provider.executable
        provider.version = lambda: ("test 1", None)
        provider.configured_model = lambda: "gpt-example-1"
        return provider

    def test_codex_passes_a_configured_sandbox_through_run(self):
        """`build_command` has always honoured `sandbox`; the bug was that
        `run` never handed it over. Tested through `run`, not `build_command`,
        because that is where the value was being dropped."""
        provider = self.capture(providers.get_provider("codex"))
        provider.run(
            "prompt",
            base.MODE_IMPLEMENT,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            options={"sandbox": "danger-full-access"},
            idle_timeout=7.0,
        )
        command = self.seen["command"]
        self.assertEqual(command[command.index("-s") + 1], "danger-full-access")

    def test_codex_passes_extra_args_from_options_through_run(self):
        provider = self.capture(providers.get_provider("codex"))
        provider.run(
            "prompt",
            base.MODE_IMPLEMENT,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            options={"args": ["--flag-from-config"]},
        )
        self.assertIn("--flag-from-config", self.seen["command"])

    def test_codex_refuses_options_args_in_review_through_run(self):
        """The gate sees the caller's arguments before `-o` is added, so the
        refusal is of the config's flag and no child is started."""
        provider = self.capture(providers.get_provider("codex"))
        result = provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            options={"args": ["--flag-from-config"]},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertNotIn("command", self.seen)
        self.assertEqual(result.command, ["codex"])

    def test_codex_refuses_a_loosening_extra_arg_without_its_value(self):
        provider = self.capture(providers.get_provider("codex"))
        result = provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            extra_args=["-sdanger-full-access"],
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("'-s'", result.stderr)
        self.assertNotIn("danger-full-access", result.stderr)
        self.assertNotIn("command", self.seen)

    def test_mock_refuses_options_args_in_review(self):
        """The mock overrides `_launch`, so it passes through the gate too --
        the path `review run` takes in the suite."""
        provider = providers.get_provider("mock")
        result = provider.run("prompt", base.MODE_REVIEW, self.project, options={"args": ["x"]})
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertIn("a bare value", result.stderr)

    def test_mock_still_runs_with_no_raw_arguments(self):
        result = providers.get_provider("mock").run("prompt", base.MODE_REVIEW, self.project)
        self.assertTrue(result.ok)

    def test_codex_still_captures_its_final_message_to_a_file(self):
        """The reason the override exists in the first place."""
        provider = self.capture(providers.get_provider("codex"))
        provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
        )
        self.assertIn("-o", self.seen["command"])

    def test_codex_asks_for_no_idle_deadline_whatever_is_requested(self):
        """Forwarding the keyword is not the same as honouring it. Codex does
        not stream progress, so an idle deadline would kill a working run;
        the adapter has to keep answering None."""
        provider = self.capture(providers.get_provider("codex"))
        provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            idle_timeout=5.0,
        )
        self.assertIsNone(self.seen["idle_timeout"])

    def test_claude_does_honour_the_idle_deadline(self):
        """The other half of the same rule: Claude streams, so the deadline
        is real there. Without this the test above would pass on an adapter
        that ignored the keyword everywhere."""
        provider = self.capture(providers.get_provider("claude"))
        # A CLI that advertises its read-only flags; otherwise the run is
        # refused before anything is spawned.
        setattr(provider, "_capture", lambda command, timeout=30: _Completed(CLAUDE_HELP))
        provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "sonnet", "version": "latest"},
            idle_timeout=5.0,
        )
        self.assertEqual(self.seen["idle_timeout"], 5.0)


SESSION = "22222222-2222-4222-8222-222222222222"
PARSED_SESSION = "11111111-1111-4111-8111-111111111111"


class _BareAdapter(base.Provider):
    """An adapter with nothing but a command, the way one is written."""

    name = "bare"
    executable = "bare"

    def which(self):
        return "bare"

    def version(self):
        return "bare 1", None

    def _resolve_latest(self, family):
        return base.ResolvedModel(self.name, family, "latest", None, "default", "cli-default")

    def build_command(self, mode, resolved, cwd, extra_args=(), options=None):
        return ["bare", mode, *extra_args]


class _TodaysLaunch(_BareAdapter):
    """``_launch`` overridden with the signature it had before resuming."""

    # The older signature is the point: a user adapter written before resuming.
    def _launch(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        prompt,
        mode,
        cwd,
        model_spec=None,
        timeout=1800,
        extra_args=(),
        env=None,
        options=None,
        idle_timeout=None,
    ):
        return super()._launch(
            prompt,
            mode,
            cwd,
            model_spec=model_spec,
            timeout=timeout,
            extra_args=extra_args,
            env=env,
            options=options,
            idle_timeout=idle_timeout,
        )


class _ResumingAdapter(_BareAdapter):
    name = "resuming"
    supports_resume = True

    def __init__(self):
        super().__init__()
        self.rejected_calls = []
        self.parse_calls = 0
        self.launch_kwargs = []
        self.raise_in_rejected = False
        self.raise_in_parse = False
        self.raise_in_postprocess = False

    def resume_args(self, session_id):
        return ["--resume=%s" % session_id]

    def resume_rejected(self, outcome, mode, options, session_id):
        self.rejected_calls.append({"options": options, "session_id": session_id})
        if self.raise_in_rejected:
            raise RuntimeError("rejected reader broke")
        return outcome.stderr.startswith("No conversation") and session_id == SESSION

    def parse_session(self, outcome):
        self.parse_calls += 1
        if self.raise_in_parse:
            raise RuntimeError("session reader broke")
        return {"session_id": PARSED_SESSION, "context_tokens": 123, "init": {"tools": ["Read"]}}

    def postprocess(self, outcome, mode):
        if self.raise_in_postprocess:
            raise RuntimeError("answer reader broke")
        return super().postprocess(outcome, mode)

    # Takes whatever the base passes, to record it; the base names its keywords.
    def _launch(self, prompt, mode, cwd, **kwargs):  # pyright: ignore[reportIncompatibleMethodOverride]
        self.launch_kwargs.append(dict(kwargs))
        return super()._launch(prompt, mode, cwd, **kwargs)


class _RejectedOutcome(_Outcome):
    def __init__(self):
        super().__init__()
        self.exit_code = 1
        self.stdout = ""
        self.stderr = "No conversation found with session ID: %s" % SESSION

    @property
    def ok(self) -> bool:
        return False


class TestResumingThroughTheBase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.seen = {}
        self.outcome = _Outcome()
        original = execution.execute
        self.addCleanup(setattr, execution, "execute", original)

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.seen["command"] = list(command)
            return self.outcome

        execution.execute = execute

    def test_the_default_says_unsupported_or_unspecified(self):
        self.assertEqual(base.Provider().resume_support(self.project)["status"], "unsupported")

        class Declares(base.Provider):
            supports_resume = True

        self.assertEqual(Declares().resume_support(self.project)["status"], "unspecified")

    def test_an_adapter_that_names_no_table_gets_the_two_key_report(self):
        unspecified = {
            "status": "unspecified",
            "detail": "this adapter does not report whether a resumed session stays read-only",
        }
        self.assertEqual(
            base.Provider().resume_support(self.project),
            {"status": "unsupported", "detail": "base does not resume sessions"},
        )

        class Declares(base.Provider):
            supports_resume = True

        self.assertEqual(Declares().resume_support(self.project), unspecified)

        class DeclaresHooks(base.Provider):
            supports_resume = True
            resume_flags = ("--x",)

            def resume_help_text(self) -> Optional[str]:
                raise AssertionError("the help was read")

            def version(self) -> "tuple[Optional[str], Optional[str]]":
                raise AssertionError("the version was read")

        self.assertEqual(DeclaresHooks().resume_support(self.project), unspecified)

    def test_a_table_with_no_flags_to_look_for_is_unverified(self):
        class NoFlags(base.Provider):
            name = "noflags"
            supports_resume = True

            def verified_resume(self) -> Optional[Dict[str, Dict[str, Any]]]:
                return {}

            def resume_help_text(self) -> Optional[str]:
                raise AssertionError("the help was read")

            def version(self) -> "tuple[Optional[str], Optional[str]]":
                raise AssertionError("the version was read")

        report = NoFlags().resume_support(self.project)
        keys = ["status", "detail", "version", "source", "record", "verified_at", "newer_than", "missing"]
        self.assertEqual(list(report), keys)
        self.assertEqual(
            report,
            {
                "status": "unverified",
                "detail": (
                    "this adapter names no resume flags to look for in its help, so resuming is unverified"
                ),
                "version": None,
                "source": None,
                "record": verified.record_path("noflags"),
                "verified_at": None,
                "newer_than": None,
                "missing": [],
            },
        )

    def bare_table_adapter(
        self, help_text: Optional[str] = "--x", version_output: Any = ("1.0.0", None), **attrs: Any
    ):
        """A plain base.Provider subclass with a table and flags, and none of
        the base's resume defaults overridden except what ``attrs`` sets."""

        class BareTable(base.Provider):
            name = "baretable"
            supports_resume = True
            resume_flags = ("--x",)

            def verified_resume(self) -> Optional[Dict[str, Dict[str, Any]]]:
                return {}

            def resume_help_text(self) -> Optional[str]:
                return help_text

            def version(self) -> "tuple[Optional[str], Optional[str]]":
                return version_output

        for key, value in attrs.items():
            setattr(BareTable, key, value)
        return BareTable()

    def unverified(self, detail, **fields):
        report = {
            "status": "unverified",
            "detail": detail,
            "version": None,
            "source": None,
            "record": verified.record_path("baretable"),
            "verified_at": None,
            "newer_than": None,
            "missing": [],
        }
        report.update(fields)
        return report

    def test_the_base_matcher_advertises_nothing(self):
        report = self.bare_table_adapter().resume_support(self.project)
        expected = self.unverified("--x not advertised", status="unsupported", missing=["--x"])
        self.assertEqual(report, expected)
        self.assertEqual(list(report), list(expected))

    def test_the_base_wording_for_unread_help_and_version(self):
        report = self.bare_table_adapter(help_text=None).resume_support(self.project)
        self.assertEqual(
            report,
            self.unverified("could not read the help that lists the resume flags, so resuming is unverified"),
        )
        advertised: Dict[str, Any] = {"resume_advertises": lambda self, help_text, flag: True}
        for version in ((None, "boom"), ("", None)):
            with self.subTest(version=version):
                provider = self.bare_table_adapter(version_output=version, **advertised)
                report = provider.resume_support(self.project)
                self.assertEqual(report, self.unverified("could not read the CLI's --version"))

    def test_an_adapters_missing_flags_wording_cannot_break_the_report(self):
        cases = (
            ("fork flags missing", "fork flags missing"),
            ("100% required: %s", "100% required: --x, --y"),
            ("%s missing (%d)", "--x, --y missing (%d)"),
        )
        for wording, detail in cases:
            with self.subTest(wording=wording):
                provider = self.bare_table_adapter(resume_flags=("--x", "--y"), resume_flags_missing=wording)
                report = provider.resume_support(self.project)
                expected = self.unverified(detail, status="unsupported", missing=["--x", "--y"])
                self.assertEqual(report, expected)

    def test_codex_and_mock_say_what_they_do(self):
        codex = providers.get_provider("codex")
        self.assertTrue(codex.supports_resume)
        self.assertNotEqual(codex.resume_support(self.project)["status"], "unspecified")
        self.assertEqual(providers.get_provider("mock").resume_support(self.project)["status"], "verified")

    def test_a_launch_with_todays_signature_still_runs_fresh(self):
        provider = _TodaysLaunch()
        for mode in (base.MODE_PLAN, base.MODE_REVIEW):
            with self.subTest(mode=mode):
                result = provider.run("prompt", mode, self.project, **CALL)
                self.assertTrue(result.ok)
        with self.assertRaises(TypeError):
            provider.run("prompt", base.MODE_PLAN, self.project, resume_session=SESSION, **CALL)

    def test_a_build_command_only_adapter_still_runs_fresh(self):
        result = _BareAdapter().run("prompt", base.MODE_PLAN, self.project, **CALL)
        self.assertTrue(result.ok)
        self.assertEqual(self.seen["command"], ["bare", base.MODE_PLAN])

    def test_command_line_refuses_to_resume_on_an_adapter_that_cannot(self):
        provider = _BareAdapter()
        resolved = provider.resolve_model(None)
        with self.assertRaises(NotImplementedError):
            provider.command_line(base.MODE_PLAN, resolved, self.project, resume_session=SESSION)

    def test_only_a_read_only_run_may_resume(self):
        with self.assertRaises(ValueError):
            _ResumingAdapter().run("prompt", base.MODE_IMPLEMENT, self.project, resume_session=SESSION)

    def test_a_rejected_resume_is_reported(self):
        provider = _ResumingAdapter()
        self.outcome = _RejectedOutcome()
        result = provider.run("prompt", base.MODE_PLAN, self.project, resume_session=SESSION, **CALL)
        self.assertFalse(result.ok)
        self.assertTrue(result.resume_rejected)
        self.assertEqual(result.session_id, PARSED_SESSION)
        self.assertEqual(result.context_tokens, 123)
        self.assertEqual(result.session_init, {"tools": ["Read"]})
        self.assertEqual(self.seen["command"][-1], "--resume=%s" % SESSION)
        # The checked copy taken before around_launch, equal to what was passed.
        self.assertEqual(provider.rejected_calls[0]["options"], CALL["options"])
        self.assertEqual(provider.rejected_calls[0]["session_id"], SESSION)

    def test_a_rejected_resume_survives_an_unreadable_answer(self):
        provider = _ResumingAdapter()
        provider.raise_in_postprocess = True
        self.outcome = _RejectedOutcome()
        result = provider.run("prompt", base.MODE_PLAN, self.project, resume_session=SESSION, **CALL)
        self.assertFalse(result.ok)
        self.assertFalse(result.usage.measured)
        self.assertTrue(result.resume_rejected)
        self.assertEqual(result.session_id, PARSED_SESSION)
        self.assertEqual(result.session_init, {"tools": ["Read"]})

    def test_a_fresh_run_is_not_asked_about_rejection(self):
        provider = _ResumingAdapter()
        result = provider.run("prompt", base.MODE_PLAN, self.project, **CALL)
        self.assertTrue(result.ok)
        self.assertEqual(provider.rejected_calls, [])
        self.assertFalse(any(token.startswith("--resume") for token in self.seen["command"]))
        self.assertEqual(provider.parse_calls, 1)
        self.assertEqual(result.session_id, PARSED_SESSION)
        self.assertNotIn("resume_session", provider.launch_kwargs[0])

    def test_a_broken_rejection_reader_is_not_a_rejection(self):
        provider = _ResumingAdapter()
        provider.raise_in_rejected = True
        self.outcome = _RejectedOutcome()
        result = provider.run("prompt", base.MODE_PLAN, self.project, resume_session=SESSION, **CALL)
        self.assertFalse(result.resume_rejected)
        self.assertTrue(result.stderr.startswith("RuntimeError: rejected reader broke"))

    def test_a_broken_session_reader_leaves_the_fields_empty(self):
        provider = _ResumingAdapter()
        provider.raise_in_parse = True
        result = provider.run("prompt", base.MODE_PLAN, self.project, **CALL)
        self.assertIsNone(result.session_id)
        self.assertIsNone(result.context_tokens)
        self.assertIsNone(result.session_init)
        self.assertIn("RuntimeError: session reader broke", result.stderr)

    def test_a_raw_resume_argument_is_still_refused(self):
        provider = _ResumingAdapter()
        result = provider.run(
            "prompt",
            base.MODE_PLAN,
            self.project,
            options={"args": ["--resume=33333333-3333-4333-8333-333333333333"]},
            resume_session=SESSION,
        )
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.invoked)
        self.assertNotIn("command", self.seen)

    def test_the_session_fields_are_serialised(self):
        result = _ResumingAdapter().run("prompt", base.MODE_PLAN, self.project, **CALL)
        data = result.to_dict()
        for key in ("session_id", "context_tokens", "session_init", "resume_rejected"):
            self.assertIn(key, data)


class _HookedAdapter(_ResumingAdapter):
    """Every hook the base calls after the run, recorded, and each made to
    fail or to answer None on request."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.unenforced = False
        self.warnings: Any = ["a run warning"]
        self.session: Any = {"session_id": PARSED_SESSION}
        self.raise_in_usage = False

    def read_only_enforcement(self) -> Dict[str, Any]:
        if not self.unenforced:
            return super().read_only_enforcement()
        return {"status": "unenforced", "mechanism": "none", "detail": "it writes"}

    def resume_rejected(self, outcome, mode, options, session_id):
        self.calls.append("resume_rejected")
        return super().resume_rejected(outcome, mode, options, session_id)

    def parse_session(self, outcome):
        self.calls.append("parse_session")
        if self.raise_in_parse:
            raise RuntimeError("session reader broke")
        return self.session

    def run_warnings(self, outcome, mode):
        self.calls.append("run_warnings")
        return self.warnings

    def postprocess(self, outcome, mode):
        self.calls.append("postprocess")
        if self.raise_in_postprocess:
            raise ValueError("boom")
        return outcome.stdout, outcome.stderr

    def parse_usage(self, outcome, mode):
        self.calls.append("parse_usage")
        if self.raise_in_usage:
            raise ValueError("boom")
        return None


UNENFORCED = "read-only is NOT enforced by resuming -- it writes"
#: What a plan run with every note says above its stderr, in order.
NOTES = [UNENFORCED, "a run warning", "RuntimeError: session reader broke"]


class TestWhatTheBaseMakesOfTheHooks(IsolatedCase):
    """The order the hooks run in, and what a failing one leaves behind."""

    def setUp(self):
        super().setUp()
        self.outcome: Any = _Outcome()
        self.outcome.stderr = "err"
        self.addCleanup(setattr, execution, "execute", execution.execute)
        execution.execute = lambda command, cwd, **kwargs: self.outcome
        self.provider = _HookedAdapter()

    def run_plan(self, **kwargs):
        return self.provider.run("prompt", base.MODE_PLAN, self.project, **kwargs)

    def test_the_hooks_run_in_order(self):
        self.outcome = _RejectedOutcome()
        self.run_plan(resume_session=SESSION)
        expected = ["resume_rejected", "parse_session", "run_warnings", "postprocess", "parse_usage"]
        self.assertEqual(self.provider.calls, expected)

    def test_an_unreadable_answer_names_every_failure_in_order(self):
        self.provider.unenforced = True
        self.provider.raise_in_parse = True
        self.provider.raise_in_postprocess = True
        result = self.run_plan()
        self.assertEqual(result.stderr, "\n".join([*NOTES, "ValueError: boom\nerr"]))
        self.assertEqual(result.warnings, [UNENFORCED, "a run warning"])
        self.assertIsNone(result.usage.prompt_chars)
        self.assertEqual(self.provider.calls, ["parse_session", "run_warnings", "postprocess"])

    def test_an_unreadable_invoice_is_named_below_the_notes(self):
        self.provider.unenforced = True
        self.provider.raise_in_parse = True
        self.provider.raise_in_usage = True
        result = self.run_plan()
        self.assertEqual(result.stderr, "\n".join([*NOTES, "ValueError: boom\nerr"]))
        self.assertEqual(result.usage.prompt_chars, len("prompt"))

    def test_run_warnings_that_fail_part_way_keep_what_came_before(self):
        def warnings():
            yield "first"
            raise RuntimeError("late")

        self.provider.warnings = warnings()
        result = self.run_plan()
        self.assertEqual(result.warnings, ["first"])
        self.assertEqual(result.stderr, "first\nRuntimeError: late\nerr")

    def test_a_failing_rejection_reader_is_named_above_stderr(self):
        self.provider.raise_in_rejected = True
        self.provider.warnings = []
        self.outcome = _RejectedOutcome()
        result = self.run_plan(resume_session=SESSION)
        self.assertFalse(result.resume_rejected)
        expected = "RuntimeError: rejected reader broke\nNo conversation found with session ID: " + SESSION
        self.assertEqual(result.stderr, expected)

    def test_hooks_that_answer_none(self):
        self.provider.session = None
        self.provider.warnings = None
        result = self.run_plan()
        session = (result.session_id, result.context_tokens, result.session_init)
        self.assertEqual(session, (None, None, None))
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.stderr, "err")


def _measured_outcome():
    """An outcome with no field left at its default."""
    return execution.ExecOutcome(
        3,
        "out",
        "err",
        5.0,
        timed_out=True,
        stalled=True,
        idle_for=4.0,
        orphans_possible=True,
        suspended=2.0,
    )


class TestBothResultsCarryTheMeasurement(IsolatedCase):
    """The result of a run whose answer could not be read, and of one whose
    answer could, field by field."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, execution, "execute", execution.execute)
        execution.execute = lambda command, cwd, **kwargs: _measured_outcome()

    def expected(self, prompt_chars):
        return {
            "ok": False,
            "exit_code": 3,
            "timed_out": True,
            "stalled": True,
            "idle_for_seconds": 4.0,
            "orphans_possible": True,
            "duration_seconds": 5.0,
            "suspended_seconds": 2.0,
            "command": ["bare", base.MODE_IMPLEMENT],
            "model": {
                "provider": "bare",
                "family": "",
                "version_policy": "latest",
                "argument": None,
                "display": "default",
                "source": "cli-default",
                "note": "",
            },
            "invoked": True,
            "usage": usage_dict(prompt_chars=prompt_chars),
            "session_id": None,
            "context_tokens": None,
            "session_init": None,
            "resume_rejected": False,
            "warnings": [],
        }

    def test_an_unreadable_answer(self):
        class Unreadable(_BareAdapter):
            def postprocess(self, outcome, mode):
                raise ValueError("boom")

        ran = Unreadable().run("prompt", base.MODE_IMPLEMENT, self.project)
        self.assertEqual(ran.to_dict(), self.expected(None))
        self.assertEqual((ran.stdout, ran.stderr, ran.suspended), ("out", "ValueError: boom\nerr", 2.0))

    def test_a_readable_answer(self):
        result = _BareAdapter().run("prompt", base.MODE_IMPLEMENT, self.project)
        self.assertEqual(result.to_dict(), self.expected(len("prompt")))
        self.assertEqual((result.stdout, result.stderr, result.suspended), ("out", "err", 2.0))


class _Changing(_BareAdapter):
    """An adapter whose ``around_launch`` changes the run it was handed."""

    def __init__(self, **changes: Any):
        super().__init__()
        self.changes = changes

    def around_launch(self, launch, proceed):
        return proceed(launch._replace(**self.changes))


class TestAroundLaunchCannotUndoTheGate(IsolatedCase):
    """``run`` gates the caller's arguments before ``around_launch`` sees
    them, so what it gated may not change on the way to the CLI."""

    def setUp(self):
        super().setUp()
        self.seen = {}
        self.addCleanup(setattr, execution, "execute", execution.execute)

        def execute(command, cwd, prompt="", timeout=None, idle_timeout=None, env=None):
            self.seen["command"] = list(command)
            return _Outcome()

        execution.execute = execute

    def test_a_gated_field_may_not_change(self):
        cases: List[Tuple[str, Dict[str, Any]]] = [
            ("extra_args", {"extra_args": ["--yolo"]}),
            ("mode", {"mode": base.MODE_IMPLEMENT}),
            ("resume_session", {"resume_session": SESSION}),
            ("options", {"options": {"args": ["--yolo"]}}),
        ]
        for field, changes in cases:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "^around_launch may not change %s$" % field):
                    _Changing(**changes).run("prompt", base.MODE_REVIEW, self.project, options={})
                self.assertNotIn("command", self.seen)

    def test_a_gated_field_may_not_change_in_place(self):
        class Appending(_BareAdapter):
            def __init__(self, change):
                super().__init__()
                self.change = change

            def around_launch(self, launch, proceed):
                self.change(launch)
                return proceed(launch)

        cases: List[Tuple[str, Callable[[Any], None]]] = [
            ("extra_args", lambda launch: launch.extra_args.append("--yolo")),
            ("options", lambda launch: launch.options["args"].append("--yolo")),
        ]
        for field, change in cases:
            with self.subTest(field=field):
                extra_args: List[str] = []
                options: Dict[str, Any] = {"args": []}
                with self.assertRaisesRegex(ValueError, "^around_launch may not change %s$" % field):
                    Appending(change).run(
                        "prompt", base.MODE_REVIEW, self.project, extra_args=extra_args, options=options
                    )
                self.assertNotIn("command", self.seen)

    def test_the_adapters_own_arguments_follow_the_callers(self):
        provider = _Changing(own_args=("--own", "value"))
        result = provider.run("prompt", base.MODE_IMPLEMENT, self.project, extra_args=["--x"])
        self.assertTrue(result.ok)
        self.assertEqual(self.seen["command"], ["bare", base.MODE_IMPLEMENT, "--x", "--own", "value"])
        self.assertEqual(result.command, self.seen["command"])


class TestLaunchReachesTheStart(IsolatedCase):
    """Every field of the run reaches the base's start as ``_launch`` was
    called, except what a built-in ``around_launch`` changes on purpose."""

    def setUp(self):
        super().setUp()
        self.received = []

        def start(provider, called, launch):
            self.received.append((called, launch))
            return base.RunResult(True, 0, "", "", [provider.executable], 0.0)

        self.addCleanup(setattr, base.Provider, "_start", base.Provider._start)
        setattr(base.Provider, "_start", start)

    def launched(self, name):
        provider = providers.get_provider(name)
        provider.which = lambda: provider.executable
        provider.version = lambda: ("test 1", None)
        provider._launch(
            "the prompt",
            base.MODE_IMPLEMENT,
            self.project,
            model_spec={"family": "default"},
            timeout=99,
            extra_args=["--x"],
            env={"A": "1"},
            options={"args": ["--y"]},
            idle_timeout=7.0,
            resume_session=None,
            command_kwargs={"marker": 1},
        )
        self.assertEqual(len(self.received), 1)
        return self.received.pop()

    def test_claude_and_mycli_change_nothing(self):
        self.write_user_provider("mycli")
        providers.load_user_providers()
        for name in ("claude", "mycli"):
            with self.subTest(provider=name):
                called, launch = self.launched(name)
                self.assertEqual(launch, called)

    def test_codex_adds_only_its_answer_file(self):
        called, launch = self.launched("codex")
        self.assertEqual(launch._replace(own_args=()), called)
        self.assertEqual(launch.own_args[0], "-o")
        self.assertTrue(os.path.basename(launch.own_args[1]).startswith("codex-last-"))
        self.assertEqual(len(launch.own_args), 2)

    def test_agy_changes_only_the_prompt_and_names_its_file(self):
        called, launch = self.launched("agy")
        self.assertEqual(launch._replace(prompt="the prompt", command_kwargs={"marker": 1}), called)
        self.assertEqual(launch.prompt, "")
        kwargs = dict(launch.command_kwargs or {})
        self.assertTrue(str(kwargs.pop("prompt_file")).startswith("agy-prompt-%d-" % os.getpid()))
        self.assertEqual(kwargs, {"marker": 1})


if __name__ == "__main__":
    unittest.main()
