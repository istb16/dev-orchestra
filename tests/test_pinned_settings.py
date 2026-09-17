"""Settings fixed at a value the built-in default has since moved off.

`config setup --defaults` writes every default into the file, so improving a
default never reaches an existing installation: the file goes on answering
with the number that was current when it was written. That is not theoretical.
0.4.2 raised the low-risk thresholds from 2 files / 50 lines to 5 / 150 so the
panel reduction could fire at all, and every configuration written before it
kept reporting the old pair -- so the release did nothing for anyone who had
already run `config setup`.

`doctor` reports them. It does not fix them: "chose 2 deliberately" and
"inherited 2 from an older default" are the same two characters on disk, and
rewriting the first silently would be worse than leaving the second to be
noticed.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase

from orchestrator import cli
from orchestrator import config as config_mod
from orchestrator import doctor as doctor_mod

pinned = config_mod.pinned_differences


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def settings(entries):
    return [entry["setting"] for entry in entries]


class TestWhatCountsAsPinned(unittest.TestCase):
    def test_a_config_that_matches_the_defaults_reports_nothing(self):
        """The common case, and the one that decides whether this is noise."""
        self.assertEqual(pinned(config_mod.default_config()), [])

    def test_the_case_this_exists_for(self):
        data = config_mod.default_config()
        data["optimization"]["low_risk_max_files"] = 2
        data["optimization"]["low_risk_max_lines"] = 50
        self.assertEqual(
            settings(pinned(data)),
            ["optimization.low_risk_max_files", "optimization.low_risk_max_lines"],
        )

    def test_the_current_default_is_reported_beside_the_pinned_value(self):
        """Without both numbers the line cannot be acted on."""
        data = config_mod.default_config()
        data["optimization"]["low_risk_max_files"] = 2
        entry = pinned(data)[0]
        self.assertEqual(entry["value"], 2)
        self.assertEqual(entry["default"], 5)

    def test_a_deliberate_choice_is_reported_too(self):
        """It cannot be told apart from an inherited one, and saying so is the
        whole design: reported, never rewritten."""
        data = config_mod.default_config()
        data["implementer"]["model"]["family"] = "sonnet"
        self.assertEqual(settings(pinned(data)), ["implementer.model.family"])

    def test_a_setting_the_defaults_do_not_mention_is_not_reported(self):
        """An option this version knows nothing about is not a stale default."""
        data = config_mod.default_config()
        data["implementer"]["options"] = {"permission_mode": "acceptEdits"}
        self.assertEqual(pinned(data), [])

    def test_reviewers_are_never_reported(self):
        """A panel is the user's own; comparing it to the default one would
        report every installation that added a reviewer."""
        data = config_mod.default_config()
        data["reviewers"] = [dict(data["reviewers"][0], id="mine")]
        self.assertEqual(pinned(data), [])

    def test_a_list_is_compared_by_length_rather_than_dumped(self):
        """`high_risk_paths` is thirty entries and nobody reads a diff of it in
        a diagnostic. This is the setting a user is most likely to narrow."""
        data = config_mod.default_config()
        data["optimization"]["high_risk_paths"] = ["*auth*"]
        entry = pinned(data)[0]
        self.assertEqual(entry["setting"], "optimization.high_risk_paths")
        self.assertEqual(entry["value"], "1 entries")

    def test_an_unchanged_list_is_quiet(self):
        data = config_mod.default_config()
        data["review"]["exclude"] = list(data["review"]["exclude"])
        self.assertEqual(pinned(data), [])

    def test_a_nested_setting_is_reported_by_its_nested_path(self):
        data = config_mod.default_config()
        data["review"]["design"]["max_iterations"] = 1
        self.assertEqual(settings(pinned(data)), ["review.design.max_iterations"])

    def test_a_config_written_before_the_design_block_existed_is_quiet(self):
        data = config_mod.default_config()
        del data["review"]["design"]
        self.assertEqual(pinned(data), [])

    def test_the_settings_are_named_by_their_full_path(self):
        """So the line can be pasted into `config set`."""
        data = config_mod.default_config()
        data["review"]["max_review_iterations"] = 5
        self.assertEqual(pinned(data)[0]["setting"], "review.max_review_iterations")


class TestThroughDoctor(IsolatedCase):
    def test_the_report_carries_them(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "optimization.low_risk_max_files", "2")
        report = doctor_mod.collect(self.project, probe_models=False)
        self.assertEqual(settings(report["config"]["pinned"]), ["optimization.low_risk_max_files"])

    def test_they_are_printed_with_both_numbers(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "optimization.low_risk_max_lines", "50")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("optimization.low_risk_max_lines", out)
        self.assertIn("50 (default 150)", out)

    def test_a_fresh_config_prints_no_section_at_all(self):
        run_cli("config", "setup", "--defaults")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertNotIn("Pinned at a value", out)

    def test_they_are_not_problems(self):
        """A pinned value is information. Reporting it as a problem would make
        `doctor --strict` fail over a configuration that is working."""
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "optimization.low_risk_max_files", "2")
        report = doctor_mod.collect(self.project, probe_models=False)
        self.assertEqual([p for p in report["problems"] if "low_risk" in p], [])

    def test_nothing_is_rewritten(self):
        run_cli("config", "setup", "--defaults")
        run_cli("config", "set", "optimization.low_risk_max_files", "2")
        run_cli("doctor", "--fast")
        loaded = config_mod.load(self.project, validate_result=False)
        self.assertEqual(loaded.optimization_settings()["low_risk_max_files"], 2)


if __name__ == "__main__":
    unittest.main()
