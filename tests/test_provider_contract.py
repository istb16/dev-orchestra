"""Every adapter must answer the call the orchestrator actually makes.

An adapter overrides ``run`` to do something the base class cannot: Codex
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
import unittest

from helpers import IsolatedCase

from orchestrator import execution, providers
from orchestrator.providers import base

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
    def ok(self):
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

    def test_a_not_installed_cli_is_reported_not_raised(self):
        for provider in self.adapters():
            if provider.name == "mock":
                continue  # the mock is always installed; that is its job
            provider.which = lambda: None
            with self.subTest(provider=provider.name):
                result = provider.run("prompt", base.MODE_REVIEW, self.project, **CALL)
                self.assertFalse(result.ok)
                self.assertFalse(result.invoked)


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
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "recommended-coding", "version": "latest"},
            options={"args": ["--flag-from-config"]},
        )
        self.assertIn("--flag-from-config", self.seen["command"])

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
        provider.run(
            "prompt",
            base.MODE_REVIEW,
            self.project,
            model_spec={"family": "sonnet", "version": "latest"},
            idle_timeout=5.0,
        )
        self.assertEqual(self.seen["idle_timeout"], 5.0)


if __name__ == "__main__":
    unittest.main()
