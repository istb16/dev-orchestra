"""Routing one stage to a different model without redefining the stage.

A tier is the same role doing the same job with a different model behind it:
a one-line fix handed to something cheap, a gnarly design handed to something
expensive, an independent second opinion handed to another vendor entirely.
That is a per-role override rather than a second role, because maintaining two
definitions of what the implementer *is* would be the expensive part.

Who picks is the orchestrator, by name, on the command line. Nothing here
guesses a tier from the size of a diff -- the caller is the only thing that
knows how hard the task is, and a wrong guess spends either the money or the
quality that tiers exist to control.

So the tests are mostly about the two ways a silent tier goes wrong: one that
was ignored (work quietly runs on the default model) and one that inherited
something it should not have (work quietly runs on a model nobody named).
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git

from orchestrator import cli
from orchestrator import config as config_mod
from orchestrator import wizard as wizard_mod


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def configured(**tiers):
    data = config_mod.default_config()
    data["implementer"]["model_tiers"] = tiers
    return data


def loaded(**tiers):
    return config_mod.LoadedConfig(configured(**tiers), None, None, True)


# --------------------------------------------------------------------------- merging


class TestApplyingATier(unittest.TestCase):
    def test_a_tier_changes_the_model_and_nothing_else(self):
        spec = loaded(light={"model": {"family": "sonnet", "version": "latest"}}).role("implementer", "light")
        self.assertEqual(spec["model"], {"family": "sonnet", "version": "latest"})
        self.assertEqual(spec["provider"], "claude")

    def test_no_tier_is_the_role_as_configured(self):
        base = loaded(light={"model": {"family": "sonnet"}}).role("implementer")
        self.assertEqual(base["model"]["family"], "opus")

    def test_the_tier_list_is_not_part_of_the_spec_handed_to_a_provider(self):
        """`model_tiers` is configuration about the role, not a field of it."""
        spec = loaded(light={"model": {"family": "sonnet"}}).role("implementer", "light")
        self.assertNotIn("model_tiers", spec)

    def test_a_model_is_replaced_whole_rather_than_merged(self):
        """A deep merge would leave a base `pinned` + `id` in place under a
        tier that named only a family, and run a model nobody asked for."""
        data = configured(light={"model": {"family": "sonnet", "version": "latest"}})
        data["implementer"]["model"] = {"family": "opus", "version": "pinned", "id": "opus-fixed"}
        spec = config_mod.LoadedConfig(data, None, None, True).role("implementer", "light")
        self.assertEqual(spec["model"], {"family": "sonnet", "version": "latest"})
        self.assertNotIn("id", spec["model"])

    def test_switching_provider_drops_the_old_provider_s_model(self):
        """`opus` means nothing to Codex. With no model named, the new
        provider picks its own default, which is what asking for a provider
        and no model means."""
        spec = loaded(other={"provider": "codex"}).role("implementer", "other")
        self.assertEqual(spec["provider"], "codex")
        self.assertNotIn("model", spec)

    def test_switching_provider_drops_the_old_provider_s_options(self):
        """`permission_mode` is a Claude idea; handing it to Codex is a
        configuration error that nobody wrote."""
        data = configured(other={"provider": "codex"})
        data["implementer"]["options"] = {"permission_mode": "bypassPermissions"}
        spec = config_mod.LoadedConfig(data, None, None, True).role("implementer", "other")
        self.assertNotIn("options", spec)

    def test_switching_provider_keeps_what_the_tier_itself_names(self):
        spec = loaded(
            other={
                "provider": "codex",
                "model": {"family": "recommended-coding"},
                "options": {"sandbox": "read-only"},
            }
        ).role("implementer", "other")
        self.assertEqual(spec["provider"], "codex")
        self.assertEqual(spec["model"]["family"], "recommended-coding")
        self.assertEqual(spec["options"], {"sandbox": "read-only"})

    def test_staying_on_the_same_provider_keeps_its_options(self):
        data = configured(light={"model": {"family": "sonnet"}})
        data["implementer"]["options"] = {"permission_mode": "bypassPermissions"}
        spec = config_mod.LoadedConfig(data, None, None, True).role("implementer", "light")
        self.assertEqual(spec["options"], {"permission_mode": "bypassPermissions"})

    def test_applying_a_tier_does_not_edit_the_configuration(self):
        """The next call must see the role, not the last tier used."""
        config = loaded(light={"model": {"family": "sonnet"}})
        config.role("implementer", "light")
        self.assertEqual(config.role("implementer")["model"]["family"], "opus")


class TestAskingForATierThatIsNotThere(unittest.TestCase):
    """The failure that has to be loud. A tier silently ignored runs the work
    on the default model and reports nothing -- the expensive one when you
    asked for cheap, or the cheap one when you asked for care."""

    def test_an_unknown_tier_is_an_error_not_a_fallback(self):
        with self.assertRaises(config_mod.ConfigError) as caught:
            loaded(light={"model": {"family": "sonnet"}}).role("implementer", "heavy")
        self.assertIn("heavy", str(caught.exception))

    def test_the_error_lists_the_tiers_that_do_exist(self):
        with self.assertRaises(config_mod.ConfigError) as caught:
            loaded(light={"model": {"family": "sonnet"}}, heavy={"provider": "codex"}).role(
                "implementer", "nope"
            )
        self.assertIn("heavy, light", str(caught.exception))

    def test_a_role_with_no_tiers_at_all_says_so(self):
        config = config_mod.LoadedConfig(config_mod.default_config(), None, None, True)
        with self.assertRaises(config_mod.ConfigError) as caught:
            config.role("implementer", "light")
        self.assertIn("none configured", str(caught.exception))


# --------------------------------------------------------------------------- validation


class TestValidation(unittest.TestCase):
    """A tier is only ever used by name, so a broken one fails at the moment
    somebody routes work to it. It is checked as a whole merged role here."""

    def problems(self, **tiers):
        return config_mod.validate(configured(**tiers))

    def test_a_sound_tier_validates(self):
        self.assertEqual(self.problems(light={"model": {"family": "sonnet", "version": "latest"}}), [])

    def test_an_unknown_provider_in_a_tier_is_caught(self):
        problems = self.problems(bad={"provider": "nonexistent"})
        self.assertTrue(any("model_tiers.bad" in p and "nonexistent" in p for p in problems), problems)

    def test_a_pinned_tier_without_an_id_is_caught(self):
        problems = self.problems(bad={"model": {"family": "opus", "version": "pinned"}})
        self.assertTrue(any("model_tiers.bad" in p for p in problems), problems)

    def test_an_invalid_option_in_a_tier_is_caught(self):
        problems = self.problems(bad={"provider": "codex", "options": {"sandbox": "nope"}})
        self.assertTrue(any("model_tiers.bad" in p for p in problems), problems)

    def test_a_tier_that_sets_nothing_is_not_a_tier(self):
        self.assertTrue(any("sets nothing" in p for p in self.problems(empty={})))

    def test_a_tier_cannot_override_something_that_is_not_the_model(self):
        """`role`, `id`, a budget -- none of those are what a tier is for, and
        silently ignoring them would look like they worked."""
        problems = self.problems(bad={"role": "security"})
        self.assertTrue(any("only provider, model and options" in p for p in problems), problems)

    def test_the_tier_map_must_be_a_mapping(self):
        data = configured()
        data["implementer"]["model_tiers"] = ["light"]
        self.assertTrue(any("model_tiers" in p for p in config_mod.validate(data)))

    def test_a_tier_name_has_to_be_usable_on_a_command_line(self):
        self.assertTrue(any("usable tier name" in p for p in self.problems(**{"Not A Tier": {}})))

    def test_the_defaults_ship_without_tiers_and_still_validate(self):
        """Optional means optional: nothing about an existing config changes."""
        self.assertNotIn("model_tiers", config_mod.default_config()["implementer"])
        self.assertEqual(config_mod.validate(config_mod.default_config()), [])


# --------------------------------------------------------------------------- surface


class TestItIsVisible(unittest.TestCase):
    def test_config_show_lists_every_tier_with_the_model_it_would_use(self):
        """A tier nobody can see is a tier nobody uses."""
        summary = wizard_mod.render_summary(
            configured(light={"model": {"family": "sonnet", "version": "latest"}})
        )
        self.assertIn("--tier light", summary)
        self.assertIn("claude / sonnet / latest", summary)

    def test_a_malformed_tier_does_not_break_the_summary(self):
        """`config show` is where someone goes to find out what is wrong."""
        summary = wizard_mod.render_summary(configured(broken="not a mapping"))
        self.assertIn("--tier broken", summary)


@unittest.skipUnless(has_git(), "git is required")
class TestRunningOnATier(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        run_cli("config", "setup", "--defaults")
        data = config_mod.load(self.project).data
        data["implementer"]["provider"] = "mock"
        data["implementer"]["model"] = {"family": "mock-large", "version": "latest"}
        data["implementer"]["model_tiers"] = {
            "light": {"model": {"family": "mock-small", "version": "latest"}},
            "broken": {"model": {"family": "unresolvable", "version": "latest"}},
        }
        config_mod.write_config_file(config_mod.global_config_path(), data)

    def command(self, *argv):
        code, out, err = run_cli("run", "implementer", "--print-command", "--prompt", "hi", *argv)
        self.assertEqual(code, 0, err)
        return out

    def test_the_default_model_runs_without_a_tier(self):
        self.assertIn("mock-large", self.command())

    def test_the_tier_model_runs_with_one(self):
        self.assertIn("mock-small", self.command("--tier", "light"))

    def test_an_unknown_tier_refuses_instead_of_running_the_default(self):
        code, _, err = run_cli("run", "implementer", "--prompt", "hi", "--tier", "heavy")
        self.assertEqual(code, 2)
        self.assertIn("heavy", err)

    def test_a_tier_whose_model_cannot_be_resolved_fails_as_a_model_failure(self):
        code, _, err = run_cli("run", "implementer", "--prompt", "hi", "--tier", "broken")
        self.assertEqual(code, 2)
        self.assertIn("unresolvable", err)

    def test_a_reviewer_cannot_be_tiered(self):
        """The panel is already one model per reviewer; that is the routing."""
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        code, _, err = run_cli("run", "m1", "--prompt", "hi", "--tier", "light")
        self.assertEqual(code, 2)
        self.assertIn("not to a reviewer", err)

    def test_the_run_says_which_tier_it_used(self):
        """Otherwise a report of what a workflow cost cannot be read against
        what it ran."""
        _, _, err = run_cli("run", "implementer", "--prompt", "hi", "--tier", "light")
        self.assertIn("light", err)
        self.assertIn("mock-small", err)

    def test_the_tier_is_recorded_against_what_it_spent(self):
        """The question a tier exists to raise: did the cheaper one cost less."""
        run_cli("run", "implementer", "--prompt", "hi", "--tier", "light")
        run_cli("run", "implementer", "--prompt", "hi")
        report = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertIn("implementer:light", report["by_label"])
        self.assertEqual(report["by_stage"]["implementer"]["runs"], 2)

    def test_a_detached_run_keeps_the_tier_it_was_asked_for(self):
        """`--detach` used not to forward it, so the worker quietly ran the
        default model -- the more expensive of the two, and unlabelled."""
        _, out, _ = run_cli("run", "implementer", "--prompt", "hi", "--tier", "light", "--detach", "--json")
        _, waited, _ = run_cli("jobs", "wait", json.loads(out)["id"], "--timeout", "60", "--json")
        self.assertEqual(json.loads(waited)["status"], "succeeded")
        report = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertIn("implementer:light", report["by_label"])

    def test_a_detached_run_records_a_tier_it_could_not_resolve(self):
        """The path forwarding `--tier` opened: the worker is now the one that
        fails to resolve, and its stderr goes nowhere. Left unrecorded the job
        reads `abandoned`, which is what a killed worker is called."""
        _, out, _ = run_cli("run", "implementer", "--prompt", "hi", "--tier", "broken", "--detach", "--json")
        _, waited, _ = run_cli("jobs", "wait", json.loads(out)["id"], "--timeout", "60", "--json")
        finished = json.loads(waited)
        self.assertEqual(finished["status"], "failed")
        self.assertIn("unresolvable", finished["error"])

    def test_an_untiered_run_is_not_labelled(self):
        run_cli("run", "implementer", "--prompt", "hi")
        report = json.loads(run_cli("tokens", "show", "--json")[1])
        self.assertEqual(report["by_label"], {})

    def test_the_tier_appears_in_the_run_state(self):
        run_cli("run", "implementer", "--prompt", "hi", "--tier", "light")
        from orchestrator import workspace as ws

        events = ws.read_json(self.cli_workspace().state_path, {}).get("events") or []
        tiers = [e.get("tier") for e in events if e.get("stage") == "implementer"]
        self.assertIn("light", tiers)


if __name__ == "__main__":
    unittest.main()
