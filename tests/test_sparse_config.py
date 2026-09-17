"""Saved configuration holds what the user decided, and nothing else.

A writer that seeds a file with `default_config()` freezes every default beside
the one value it was asked to change, so improving a default never reaches an
installation that ran setup before it -- the file goes on answering with the
number that was current the day it was written. That is what happened to the
low-risk thresholds raised in 0.4.2.

The tests that matter most here are the two the old writers were hiding: a
global write performed inside a project that has its own panel must not copy
that panel into the machine-wide file, and an improved default must actually
reach a file written today. The second is checked by replacing the
`default_config` *function*, because it builds a fresh dict on every call and
mutating a returned one proves nothing at all.
"""

from __future__ import annotations

import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import REPO_ROOT, IsolatedCase

from orchestrator import cli
from orchestrator import config as config_mod

PROJECT_PANEL = [
    {
        "id": "proj-only",
        "provider": "mock",
        "model": {"family": "mock-small", "version": "latest"},
        "role": "general",
    }
]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write_raw(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class TestSparseWriters(IsolatedCase):
    def global_layer(self):
        return config_mod.read_config_file(config_mod.global_config_path())

    def test_setup_defaults_overrides_nothing(self):
        code, _, _ = run_cli("config", "setup", "--defaults")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1})

    def test_setup_defaults_overrides_nothing_in_a_project_either(self):
        run_cli("config", "setup", "--defaults", "--scope", "project")
        path = os.path.join(self.project, ".dev-orchestra.yaml")
        self.assertEqual(config_mod.read_config_file(path), {"version": 1})

    def test_a_default_improved_later_reaches_a_config_written_today(self):
        """The reason this change exists.

        `default_config` is replaced as a *function*: it returns a new dict
        every call, so patching a returned value would pass whether or not the
        writer is sparse.
        """
        run_cli("config", "setup", "--defaults")
        moved = config_mod.default_config()
        moved["optimization"]["low_risk_max_files"] = 99
        with mock.patch.object(config_mod, "default_config", lambda: copy.deepcopy(moved)):
            loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(loaded.data["optimization"]["low_risk_max_files"], 99)

    def test_a_file_written_in_the_old_full_format_pins_the_old_value(self):
        """The contrast that makes the test above mean something."""
        config_mod.write_config_file(config_mod.global_config_path(), config_mod.default_config())
        moved = config_mod.default_config()
        moved["optimization"]["low_risk_max_files"] = 99
        with mock.patch.object(config_mod, "default_config", lambda: copy.deepcopy(moved)):
            loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(loaded.data["optimization"]["low_risk_max_files"], 5)

    def test_the_first_set_writes_only_that_value(self):
        code, _, _ = run_cli("config", "set", "optimization.low_risk_max_files", "7")
        self.assertEqual(code, 0)
        layer = self.global_layer()
        self.assertEqual(sorted(layer), ["optimization", "version"])
        self.assertEqual(layer["optimization"], {"low_risk_max_files": 7})

    def test_a_value_equal_to_the_default_is_still_recorded(self):
        """Typing it is deciding it; only `config prune` reads equality as
        evidence of inheritance."""
        run_cli("config", "set", "optimization.low_risk_max_files", "5")
        self.assertEqual(self.global_layer()["optimization"], {"low_risk_max_files": 5})

    def test_a_project_value_equal_to_the_default_overrules_the_global_layer(self):
        run_cli("config", "set", "--scope", "global", "implementer.model.family", "sonnet")
        run_cli("config", "set", "--scope", "project", "implementer.model.family", "opus")
        project = config_mod.read_config_file(os.path.join(self.project, ".dev-orchestra.yaml"))
        self.assertEqual(project["implementer"]["model"]["family"], "opus")
        self.assertEqual(config_mod.load(self.project).role("implementer")["model"]["family"], "opus")

    def test_adding_a_reviewer_writes_the_panel_and_nothing_else(self):
        code, _, _ = run_cli("reviewer", "add", "--provider", "codex", "--role", "security")
        self.assertEqual(code, 0)
        layer = self.global_layer()
        self.assertEqual(sorted(layer), ["reviewers", "version"])
        self.assertEqual(
            [r["id"] for r in layer["reviewers"]],
            ["claude-general", "codex-general", "codex-security"],
        )

    def test_reset_clears_the_overrides(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "implementer.model.family", "sonnet")
        code, out, _ = run_cli("config", "reset", "--scope", "global")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1})
        self.assertIn("overrides cleared", out)
        self.assertFalse(config_mod.load(self.project).used_defaults)

    def test_setup_says_what_the_file_does_not_hold(self):
        """`version: 1` looks like nothing was saved unless the command says so."""
        _, out, _ = run_cli("config", "setup", "--defaults")
        self.assertIn("It records only what you chose", out)

    def test_the_help_no_longer_advertises_the_old_reset(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"COLUMNS": "200"}):
            with redirect_stdout(out), self.assertRaises(SystemExit):
                cli.main(["config", "--help"])
        text = out.getvalue()
        self.assertIn("clear this layer's overrides", text)
        self.assertNotIn("restore recommended defaults", text)


class TestVersionIsAlwaysWritten(IsolatedCase):
    """`version` identifies the file format rather than configuring anything,
    so it is the one key a writer supplies -- including into files that
    predate it, which the loader has always tolerated and still does."""

    def test_an_empty_file_gains_a_version(self):
        write_raw(config_mod.global_config_path(), "")
        code, _, _ = run_cli("config", "set", "optimization.low_risk_max_files", "7")
        self.assertEqual(code, 0)
        self.assertEqual(
            config_mod.read_config_file(config_mod.global_config_path()),
            {"version": 1, "optimization": {"low_risk_max_files": 7}},
        )

    def test_a_file_without_a_version_keeps_its_other_settings(self):
        write_raw(config_mod.global_config_path(), "optimization:\n  low_risk_max_lines: 10\n")
        code, _, _ = run_cli("reviewer", "add", "--provider", "codex", "--role", "security")
        self.assertEqual(code, 0)
        layer = config_mod.read_config_file(config_mod.global_config_path())
        self.assertEqual(layer["version"], 1)
        self.assertEqual(layer["optimization"], {"low_risk_max_lines": 10})
        self.assertEqual(len(layer["reviewers"]), 3)

    def test_a_version_the_file_states_is_left_for_validate_to_report(self):
        config_mod.write_config_file(config_mod.global_config_path(), {"version": 0})
        code, _, err = run_cli("config", "set", "optimization.low_risk_max_files", "7")
        self.assertEqual(code, 0)
        self.assertEqual(config_mod.read_config_file(config_mod.global_config_path())["version"], 0)
        self.assertIn("version must be 1 (got 0)", err)


class TestGlobalWritesWithAProjectLayer(IsolatedCase):
    """The global file is shared by every project on the machine, so a write to
    it must never pick up the panel of whichever project it was run in. The old
    seed hid this: it copied the effective list, and only the fact that the
    global seed already carried `reviewers` kept it from firing."""

    def setUp(self):
        super().setUp()
        self.project_path = os.path.join(self.project, ".dev-orchestra.yaml")
        config_mod.write_config_file(self.project_path, {"version": 1, "reviewers": PROJECT_PANEL})
        self.project_bytes = open(self.project_path, "rb").read()

    def global_layer(self):
        return config_mod.read_config_file(config_mod.global_config_path())

    def assertProjectFileUntouched(self):
        self.assertEqual(open(self.project_path, "rb").read(), self.project_bytes)

    def test_reviewer_add_starts_from_the_default_panel(self):
        code, _, _ = run_cli(
            "reviewer", "add", "--scope", "global", "--provider", "codex", "--role", "security"
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["id"] for r in self.global_layer()["reviewers"]],
            ["claude-general", "codex-general", "codex-security"],
        )
        self.assertProjectFileUntouched()
        self.assertEqual([r["id"] for r in config_mod.load(self.project).reviewers()], ["proj-only"])
        _, listing, _ = run_cli("reviewer", "list", "--json")
        self.assertEqual([r["id"] for r in json.loads(listing)], ["proj-only"])

    def test_reviewer_remove_removes_from_the_default_panel(self):
        code, _, _ = run_cli("reviewer", "remove", "--scope", "global", "claude-general")
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in self.global_layer()["reviewers"]], ["codex-general"])
        self.assertProjectFileUntouched()

    def test_reviewer_set_changes_the_default_panel(self):
        code, _, _ = run_cli("reviewer", "set", "--scope", "global", "codex-general", "--role", "test")
        self.assertEqual(code, 0)
        reviewers = self.global_layer()["reviewers"]
        self.assertEqual([r["id"] for r in reviewers], ["claude-general", "codex-general"])
        self.assertEqual(reviewers[1]["role"], "test")
        self.assertProjectFileUntouched()

    def test_an_indexed_set_seeds_from_the_default_panel(self):
        code, _, _ = run_cli("config", "set", "--scope", "global", "reviewers[0].role", "security")
        self.assertEqual(code, 0)
        reviewers = self.global_layer()["reviewers"]
        self.assertEqual(len(reviewers), 2)
        self.assertEqual(reviewers[0]["id"], "claude-general")
        self.assertEqual(reviewers[0]["role"], "security")
        self.assertProjectFileUntouched()

    def test_resetting_the_project_layer_hands_it_back_to_the_global_one(self):
        run_cli("config", "set", "--scope", "global", "implementer.model.family", "sonnet")
        code, out, _ = run_cli("config", "reset", "--scope", "project")
        self.assertEqual(code, 0)
        self.assertEqual(config_mod.read_config_file(self.project_path), {"version": 1})
        self.assertIn("follows the global layer", out)
        loaded = config_mod.load(self.project)
        self.assertEqual(loaded.role("implementer")["model"]["family"], "sonnet")
        self.assertEqual(len(loaded.reviewers()), 2)

    def test_resetting_the_global_layer_leaves_the_project_one_alone(self):
        run_cli("config", "set", "--scope", "global", "implementer.model.family", "sonnet")
        code, _, _ = run_cli("config", "reset", "--scope", "global")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer(), {"version": 1})
        self.assertProjectFileUntouched()
        self.assertEqual([r["id"] for r in config_mod.load(self.project).reviewers()], ["proj-only"])


class TestResetAgainstDelete(IsolatedCase):
    """The two are only equivalent while deleting the file uncovers nothing.
    `find_project_config` tries every accepted name in a directory before it
    walks up, so a second candidate beside the first is shadowed by it."""

    def setUp(self):
        super().setUp()
        self.first = os.path.join(self.project, config_mod.PROJECT_CONFIG_NAMES[0])
        self.second = os.path.join(self.project, config_mod.PROJECT_CONFIG_NAMES[1])
        for path, family in ((self.first, "opus"), (self.second, "sonnet")):
            config_mod.write_config_file(
                path,
                {
                    "version": 1,
                    "implementer": {
                        "provider": "claude",
                        "model": {"family": family, "version": "latest"},
                    },
                },
            )

    def test_reset_keeps_the_second_candidate_shadowed(self):
        run_cli("config", "reset", "--scope", "project")
        self.assertEqual(config_mod.read_config_file(self.first), {"version": 1})
        loaded = config_mod.load(self.project)
        self.assertEqual(os.path.basename(loaded.project_path), os.path.basename(self.first))
        self.assertEqual(loaded.role("implementer")["model"]["family"], "opus")

    def test_delete_uncovers_it(self):
        run_cli("config", "reset", "--scope", "project", "--delete")
        self.assertFalse(os.path.isfile(self.first))
        loaded = config_mod.load(self.project)
        self.assertEqual(os.path.basename(loaded.project_path), os.path.basename(self.second))
        self.assertEqual(loaded.role("implementer")["model"]["family"], "sonnet")


class TestIndexedListEditing(IsolatedCase):
    """Editing one entry of a list is a decision about the whole list, because
    `deep_merge` replaces a list wholesale. The entries that were not touched
    are copied from what the layer was inheriting."""

    def global_layer(self):
        return config_mod.read_config_file(config_mod.global_config_path())

    def test_an_exclude_entry_can_be_set_without_a_layer(self):
        code, _, _ = run_cli("config", "set", "review.exclude[0]", "custom")
        self.assertEqual(code, 0)
        layer = self.global_layer()
        self.assertEqual(sorted(layer), ["review", "version"])
        self.assertEqual(sorted(layer["review"]), ["exclude"])
        default = config_mod.default_config()["review"]["exclude"]
        self.assertEqual(layer["review"]["exclude"], ["custom", *default[1:]])
        self.assertEqual(config_mod.load(self.project).review_settings()["exclude"][0], "custom")

    def test_the_same_works_after_setup_defaults(self):
        run_cli("config", "setup", "--defaults")
        code, _, _ = run_cli("config", "set", "review.exclude[0]", "custom")
        self.assertEqual(code, 0)
        self.assertEqual(self.global_layer()["review"]["exclude"][0], "custom")

    def test_a_high_risk_path_can_be_set_without_a_layer(self):
        code, _, _ = run_cli("config", "set", "optimization.high_risk_paths[0]", "custom")
        self.assertEqual(code, 0)
        layer = self.global_layer()
        self.assertEqual(sorted(layer["optimization"]), ["high_risk_paths"])
        default = config_mod.default_config()["optimization"]["high_risk_paths"]
        self.assertEqual(layer["optimization"]["high_risk_paths"], ["custom", *default[1:]])

    def test_a_project_layer_inherits_the_list_from_the_global_one(self):
        run_cli("config", "set", "--scope", "global", "review.exclude", "[a, b]")
        code, _, _ = run_cli("config", "set", "--scope", "project", "review.exclude[1]", "c")
        self.assertEqual(code, 0)
        project = config_mod.read_config_file(os.path.join(self.project, ".dev-orchestra.yaml"))
        self.assertEqual(project, {"version": 1, "review": {"exclude": ["a", "c"]}})

    def test_a_scalar_where_the_list_belongs_is_refused_not_replaced(self):
        """Seeding is for a path this layer holds nothing at. A value of the
        wrong shape is the user's, and replacing it with the inherited list
        would turn `set_path`'s refusal into a silent overwrite."""
        config_mod.write_config_file(
            config_mod.global_config_path(),
            {"version": 1, "review": {"exclude": "everything"}},
            "global",
        )
        code, _, err = run_cli("config", "set", "review.exclude[0]", "custom")
        self.assertEqual(code, 2)
        self.assertIn("not a list", err)
        self.assertEqual(self.global_layer()["review"], {"exclude": "everything"})

    def test_an_unknown_path_is_still_refused(self):
        code, _, err = run_cli("config", "set", "foo.bar[0]", "x")
        self.assertEqual(code, 2)
        self.assertIn("not a list", err)
        self.assertFalse(os.path.isfile(config_mod.global_config_path()))

    def test_an_index_past_the_end_is_an_error_not_a_traceback(self):
        code, _, err = run_cli("config", "set", "review.exclude[99]", "x")
        self.assertEqual(code, 2)
        self.assertIn("review.exclude[99]: index out of range", err)
        self.assertFalse(os.path.isfile(config_mod.global_config_path()))

    def test_an_index_past_the_end_of_a_project_list_too(self):
        code, _, err = run_cli("config", "set", "--scope", "project", "review.exclude[99]", "x")
        self.assertEqual(code, 2)
        self.assertIn("index out of range", err)
        self.assertFalse(os.path.isfile(os.path.join(self.project, ".dev-orchestra.yaml")))

    def test_an_index_past_the_end_of_the_panel_too(self):
        code, _, err = run_cli("config", "set", "reviewers[99].role", "security")
        self.assertEqual(code, 2)
        self.assertIn("index out of range", err)


class TestShowingOneLayer(IsolatedCase):
    def test_a_layer_is_shown_as_it_is_on_disk(self):
        run_cli("config", "set", "implementer.model.family", "sonnet")
        _, out, _ = run_cli("config", "show", "--scope", "global")
        self.assertIn("implementer:", out)
        self.assertIn("Everything not listed is inherited", out)
        self.assertNotIn("(empty layer)", out)

    def test_a_layer_that_overrides_nothing_says_so(self):
        run_cli("config", "setup", "--defaults")
        _, out, _ = run_cli("config", "show", "--scope", "global")
        self.assertIn("Nothing overridden", out)

    def test_a_missing_layer_says_so(self):
        _, out, _ = run_cli("config", "show", "--scope", "global")
        self.assertIn("nothing overridden", out)
        self.assertNotIn("orchestrator", out)

    def test_an_empty_file_is_not_reported_as_missing(self):
        """An empty file reads as `{}` too, and calling that "no file" would
        contradict the `Source:` line printed directly above it."""
        write_raw(config_mod.global_config_path(), "")
        _, out, _ = run_cli("config", "show", "--scope", "global")
        self.assertNotIn("No file at", out)
        self.assertIn("Nothing overridden", out)

    def test_scoped_json_is_the_file_and_only_the_file(self):
        _, out, _ = run_cli("config", "show", "--scope", "global", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["config"], {})
        self.assertIn("not created yet", payload["source"])

        run_cli("config", "setup", "--defaults")
        _, out, _ = run_cli("config", "show", "--scope", "global", "--json")
        self.assertEqual(json.loads(out)["config"], {"version": 1})

    def test_a_missing_project_layer_is_empty_too(self):
        _, out, _ = run_cli("config", "show", "--scope", "project", "--json")
        self.assertEqual(json.loads(out)["config"], {})

    def test_a_version_the_file_does_not_hold_is_not_reported(self):
        """`config show` reports the file; it is writers that supply a version."""
        write_raw(config_mod.global_config_path(), "optimization:\n  low_risk_max_lines: 10\n")
        _, out, _ = run_cli("config", "show", "--scope", "global", "--json")
        self.assertNotIn("version", json.loads(out)["config"])


class TestSetupPassesTheLayerBase(IsolatedCase):
    """What the wizard offers has to be what the layer would inherit, or
    pressing enter through a project setup overrules the global layer with a
    built-in default nobody chose."""

    def captured_base(self, *argv):
        seen = {}

        def fake(prompter, existing=None, base=None):
            seen["base"] = base
            return {"version": 1}, True

        with mock.patch.object(cli.wizard_mod, "run", fake):
            code, _, _ = run_cli(*argv)
        self.assertEqual(code, 0)
        return seen["base"]

    def test_a_project_setup_is_offered_the_global_choice(self):
        run_cli("config", "set", "--scope", "global", "implementer.model.family", "sonnet")
        base = self.captured_base("config", "setup", "--scope", "project", "--force")
        self.assertEqual(base["implementer"]["model"]["family"], "sonnet")

    def test_a_global_setup_is_offered_the_built_in_defaults(self):
        run_cli("config", "set", "--scope", "global", "implementer.model.family", "sonnet")
        base = self.captured_base("config", "setup", "--scope", "global", "--force")
        self.assertEqual(base["implementer"]["model"]["family"], "opus")


class TestPrune(IsolatedCase):
    def write_old_format(self, **changes):
        data = config_mod.default_config()
        data["optimization"]["low_risk_max_files"] = 2
        data.update(changes)
        config_mod.write_config_file(config_mod.global_config_path(), data)
        return config_mod.global_config_path()

    def test_it_drops_what_the_layer_only_inherited(self):
        path = self.write_old_format()
        before = config_mod.load(self.project).data
        code, out, _ = run_cli("config", "prune")
        self.assertEqual(code, 0)
        self.assertEqual(
            config_mod.read_config_file(path),
            {"version": 1, "optimization": {"low_risk_max_files": 2}},
        )
        self.assertEqual(config_mod.load(self.project).data, before)
        self.assertIn("reviewers", out)
        self.assertIn("Dropped", out)

    def test_a_dry_run_writes_nothing(self):
        path = self.write_old_format()
        before = open(path, "rb").read()
        code, out, _ = run_cli("config", "prune", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(open(path, "rb").read(), before)

    def test_settings_the_defaults_do_not_mention_are_kept(self):
        path = self.write_old_format()
        run_cli("config", "set", "implementer.options.permission_mode", "bypassPermissions")
        run_cli("config", "prune")
        layer = config_mod.read_config_file(path)
        self.assertEqual(layer["implementer"], {"options": {"permission_mode": "bypassPermissions"}})

    def test_a_project_layer_is_compared_against_the_global_one(self):
        """Comparing it against the built-in defaults would drop a value placed
        there precisely to cancel the global layer."""
        run_cli("config", "set", "--scope", "global", "implementer.model.family", "sonnet")
        config_mod.write_config_file(
            os.path.join(self.project, ".dev-orchestra.yaml"),
            {
                "version": 1,
                "implementer": {"provider": "claude", "model": {"family": "opus", "version": "latest"}},
            },
        )
        code, _, _ = run_cli("config", "prune", "--scope", "project")
        self.assertEqual(code, 0)
        layer = config_mod.read_config_file(os.path.join(self.project, ".dev-orchestra.yaml"))
        self.assertEqual(layer, {"version": 1, "implementer": {"model": {"family": "opus"}}})

    def test_a_file_that_is_not_there_is_an_error(self):
        code, _, err = run_cli("config", "prune")
        self.assertEqual(code, 2)
        self.assertIn("no global configuration file", err)

    def test_an_old_file_without_a_version_gains_one(self):
        data = config_mod.default_config()
        del data["version"]
        config_mod.write_config_file(config_mod.global_config_path(), data)
        run_cli("config", "prune")
        self.assertEqual(config_mod.read_config_file(config_mod.global_config_path()), {"version": 1})

    def test_a_versionless_file_with_nothing_to_drop_gains_one_too(self):
        """Dropping values is not the only thing pruning does. Returning on an
        empty `dropped` left the normalisation undone for exactly the files
        that had nothing redundant in them."""
        write_raw(config_mod.global_config_path(), "optimization:\n  low_risk_max_files: 7\n")
        code, out, _ = run_cli("config", "prune")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to drop", out)
        self.assertEqual(
            config_mod.read_config_file(config_mod.global_config_path()),
            {"version": 1, "optimization": {"low_risk_max_files": 7}},
        )

    def test_a_dry_run_writes_no_version_either(self):
        write_raw(config_mod.global_config_path(), "optimization:\n  low_risk_max_files: 7\n")
        before = open(config_mod.global_config_path(), "rb").read()
        code, out, _ = run_cli("config", "prune", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(open(config_mod.global_config_path(), "rb").read(), before)

    def test_a_file_already_normalised_is_left_byte_for_byte(self):
        config_mod.write_config_file(
            config_mod.global_config_path(),
            {"version": 1, "optimization": {"low_risk_max_files": 7}},
            "global",
        )
        before = open(config_mod.global_config_path(), "rb").read()
        code, out, _ = run_cli("config", "prune")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to drop", out)
        self.assertEqual(open(config_mod.global_config_path(), "rb").read(), before)


class TestTheFileHeader(IsolatedCase):
    """The header names what the file inherits, and a project file inherits the
    global layer rather than the built-in defaults. It is also the file that
    gets committed and read by the whole team, so it is the one that must not
    describe itself wrongly."""

    def read_text(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_a_project_file_says_it_follows_the_global_layer(self):
        run_cli("config", "set", "--scope", "project", "implementer.model.family", "sonnet")
        text = self.read_text(os.path.join(self.project, ".dev-orchestra.yaml"))
        self.assertIn("follows the global layer", text)
        self.assertNotIn("built-in defaults", text)

    def test_a_global_file_still_says_the_built_in_defaults(self):
        run_cli("config", "set", "--scope", "global", "implementer.model.family", "sonnet")
        self.assertIn("follows the built-in defaults", self.read_text(config_mod.global_config_path()))


class TestTheCliReference(unittest.TestCase):
    def test_it_no_longer_claims_setup_writes_every_default(self):
        """`config setup --defaults` overrides nothing now, and the doctor
        section carried the old sentence one paragraph below its replacement."""
        path = os.path.join(REPO_ROOT, "references", "cli.md")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("writes every default into the file", text)


if __name__ == "__main__":
    unittest.main()
