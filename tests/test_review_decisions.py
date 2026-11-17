"""The "Decisions already made" section of the code review prompt (#385).

It carries the user's answers, the plan's Risks and Decisions sections and
the findings rejected in earlier rounds of the same review, in that order,
fenced and capped. With none of them the prompt is the one it always was.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from helpers import IsolatedCase, has_git, run_cli
from test_design_review import PLAN as DESIGN_PLAN
from test_design_review import DesignReviewCase
from test_review_instructions import capture_prompts

from orchestrator import questions as questions_mod
from orchestrator import review as review_mod
from orchestrator import workspace as ws
from orchestrator.review_common import (
    CODE_DECISIONS_PLAN_LABEL,
    CODE_DECISIONS_REJECTED_LABEL,
    CODE_DECISIONS_SECTION,
    CODE_DECISIONS_USER_LABEL,
)
from orchestrator.review_decisions import DECISIONS_CAP_CHARS, render_code_decisions
from orchestrator.review_plan import plan_section

LINEAGE = "lineage-a"
CURRENT_SHA = "c" * 64

PLAN = """# Plan

## Goal

Do the thing.

## 10. Decisions

- Keep the cache in memory.

```markdown
## Decisions

- Fenced, not a heading.
```

### Sub-point

Still part of Decisions.

## 11. Risks

- Slow on a cold start.

## Test Strategy

- Unit tests for the cache.
"""

HEAD = CODE_DECISIONS_SECTION.split("{parts}")[0].split("\n\n")[0]
#: A line only the code round's section has; a design round's own decisions
#: section shares its heading.
CODE_INTRO = "Decided before or during this change."


def reviewer(reviewer_id="r1"):
    return {"id": reviewer_id, "provider": "mock", "role": "general"}


def finding(problem, triage, file="app.py", line="3", note="", reviewer_id="r-secret"):
    item = {
        "file": file,
        "line": line,
        "problem": problem,
        "severity": "medium",
        "triage": triage,
        "reported_by": [reviewer_id],
        "reviewer": reviewer_id,
    }
    if note:
        item["triage_note"] = note
    return item


class DecisionsCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()
        self.meta = {"sha256": CURRENT_SHA, "round_id": "now"}
        self.user = ""
        patcher = mock.patch(
            "orchestrator.review_decisions.questions_mod.answered_text", side_effect=lambda state: self.user
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_plan(self, text):
        os.makedirs(os.path.dirname(self.workspace.plan_path), exist_ok=True)
        with open(self.workspace.plan_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def write_round(self, name, sha, generated_at, findings, lineage=LINEAGE):
        data = {
            "snapshot": {"sha256": sha, "round_id": name},
            "generated_at": generated_at,
            "lineage": lineage,
            "findings": findings,
        }
        if generated_at is None:
            del data["generated_at"]
        self.write_raw(name, data)

    def write_raw(self, name, data):
        ws.write_json(os.path.join(self.workspace.rounds_dir, name + ".json"), data)

    def render(self, cap=DECISIONS_CAP_CHARS):
        return render_code_decisions(self.workspace, LINEAGE, self.meta, cap)


class PlanSectionTest(unittest.TestCase):
    def test_plan_section_parsing(self):
        self.assertEqual(plan_section(PLAN, "Risks"), "- Slow on a cold start.")
        decisions = plan_section(PLAN, "Decisions")
        assert decisions is not None
        self.assertTrue(decisions.startswith("- Keep the cache in memory."))
        self.assertIn("Still part of Decisions.", decisions)
        self.assertIn("- Fenced, not a heading.", decisions)
        self.assertNotIn("Slow on a cold start", decisions)
        self.assertEqual(plan_section("# P\n\n### Decisions\n\nX.\n\n### Next\n", "decisions"), "X.")
        self.assertIsNone(plan_section(PLAN, "Compatibility"))
        # A heading inside a fence is not the section.
        self.assertIsNone(plan_section("# P\n\n```\n## Risks\n- no\n```\n", "Risks"))
        # Level 1 and level 4 headings do not count.
        self.assertIsNone(plan_section("# Risks\n\n#### Risks\n", "Risks"))

    def test_plan_section_prefix(self):
        """``prefix`` takes a heading that goes on past the name, on a word boundary only."""
        plan = "# P\n\n## Added in this revision (round 2)\n\n- Flag.\n\n## Risks\n"
        self.assertEqual(plan_section(plan, "Added in this revision", prefix=True), "- Flag.")
        self.assertIsNone(plan_section(plan, "Added in this revision"))
        self.assertIsNone(plan_section("# P\n\n## Risksome\n\nX.\n", "Risks", prefix=True))
        self.assertEqual(review_mod.added_in_revision(plan), "- Flag.")

    def test_heading_variants(self):
        for plan, expected in (
            ("# P\n\n## Risks:\n\nX.\n", "X."),
            ("# P\n\n## Risks and mitigations\n\nX.\n", None),
            ("# P\n\n## 11) Risks\n\nX.\n", "X."),
            ("# P\n\n## 11. Risks\n\nX.\n", "X."),
            ("# P\n\n## Risks ##\n\nX.\n", "X."),
            ("# P\n\n## risks\n\nX.\n", "X."),
            ("# P\n\n## Risks\n\nFirst.\n\n## Risks\n\nSecond.\n", "First."),
            ("# P\n\n## Risks\n## Test Strategy\n\nT.\n", ""),
            ("# P\r\n\r\n## Risks\r\n\r\n- A.\r\n- B.\r\n\r\n## Next\r\n", "- A.\n- B."),
        ):
            with self.subTest(plan=plan):
                self.assertEqual(plan_section(plan, "Risks"), expected)

    def test_a_level_two_match_wins_over_an_earlier_level_three(self):
        """A unit's own ``### Risks`` does not hide the plan's ``## Risks``."""
        plan = "# P\n\n## Units\n\n### Risks\n\nUnit risk.\n\n## Risks\n\nMain risk.\n"
        self.assertEqual(plan_section(plan, "Risks"), "Main risk.")
        self.assertEqual(plan_section("# P\n\n## Units\n\n### Risks\n\nUnit risk.\n", "Risks"), "Unit risk.")
        # A ``### Risks`` inside the ``## Risks`` section stays part of it.
        nested = "# P\n\n## Risks\n\nMain.\n\n### Risks\n\nSub.\n\n## Next\n"
        self.assertEqual(plan_section(nested, "Risks"), "Main.\n\n### Risks\n\nSub.")

    def test_added_in_revision_levels(self):
        """The levels the former ``ADDED_HEADING_RE`` took (2 and 3), and only those."""
        for hashes, expected in (("#", None), ("##", "- Flag."), ("###", "- Flag."), ("####", None)):
            with self.subTest(level=hashes):
                plan = "# P\n\n%s Added in this revision\n\n- Flag.\n" % hashes
                self.assertEqual(review_mod.added_in_revision(plan), expected)


class ParseStampTest(unittest.TestCase):
    def test_parse_stamp(self):
        utc = timezone.utc
        for value, expected in (
            ("2026-01-01T00:00:00", datetime(2026, 1, 1, tzinfo=utc)),
            ("2026-01-01T00:00:00Z", datetime(2026, 1, 1, tzinfo=utc)),
            ("2026-01-01T09:00:00+09:00", datetime(2026, 1, 1, tzinfo=utc)),
            ("2026-01-01T00:00:00.500000+00:00", datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=utc)),
            ("", None),
            (None, None),
            (5, None),
            ("yesterday", None),
            ("0001-01-01T00:00:00+05:00", None),
            ("9999-12-31T23:59:59-05:00", None),
        ):
            with self.subTest(value=value):
                stamp = ws.parse_stamp(value)
                self.assertEqual(stamp, expected)
                if stamp is not None:
                    self.assertEqual(stamp.utcoffset(), timedelta(0))

    def test_edges_that_still_fit(self):
        """Only a stamp that leaves the range once moved to UTC is refused."""
        utc = timezone.utc
        self.assertEqual(ws.parse_stamp("0001-01-01T05:00:00+05:00"), datetime(1, 1, 1, tzinfo=utc))
        last = datetime(9999, 12, 31, 23, 59, 59, tzinfo=utc)
        self.assertEqual(ws.parse_stamp("9999-12-31T18:59:59-05:00"), last)


class RenderTest(DecisionsCase):
    def test_empty_is_unchanged(self):
        self.assertEqual(self.render(), "")
        plain = review_mod.build_review_prompt(reviewer(), self.workspace, "diff body").text
        empty = review_mod.build_review_prompt(reviewer(), self.workspace, "diff body", decisions="").text
        self.assertEqual(empty, plain)
        self.assertNotIn("Decisions already made", plain)

    def test_none_decisions_contribute_nothing(self):
        self.write_plan("# Plan\n\n## Decisions\n\nNone.\n\n## Risks\n\n- None\n")
        self.assertEqual(self.render(), "")

    def test_parts_in_order(self):
        self.user = "## Q1\n\nUse SQLite.\n"
        self.write_plan(PLAN)
        rejected = finding("Cache is unbounded", "rejected", note="by design")
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", [rejected])
        text = self.render()
        self.assertTrue(text.startswith(HEAD))
        positions = [
            text.index("Use SQLite."),
            text.index("Slow on a cold start."),
            text.index("Keep the cache in memory."),
            text.index("Cache is unbounded"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Unit tests for the cache", text)
        self.assertNotIn("Do the thing", text)
        self.assertIn("plan.md#Risks, plan.md#Decisions", text)
        self.assertNotIn("was cut", text)

    def test_rejected_scope(self):
        self.write_round(
            "r1",
            "a" * 64,
            "2026-01-01T00:00:00Z",
            [
                finding("Earlier rejection kept", "rejected", note="intended"),
                finding("Later accepted", "rejected", file="b.py"),
                finding("Accepted one", "accepted", file="c.py"),
            ],
        )
        self.write_round(
            "r2", "b" * 64, "2026-01-02T00:00:00Z", [finding("Later accepted", "accepted", file="b.py")]
        )
        other = [finding("Other lineage", "rejected")]
        self.write_round("other", "d" * 64, "2026-01-01T00:00:00Z", other, lineage="lineage-b")
        same = [finding("Current snapshot", "rejected")]
        self.write_round("same", CURRENT_SHA, "2026-01-03T00:00:00Z", same)
        text = self.render()
        self.assertIn("Earlier rejection kept", text)
        self.assertNotIn("Later accepted", text)
        self.assertNotIn("Accepted one", text)
        self.assertNotIn("Other lineage", text)
        self.assertNotIn("Current snapshot", text)
        self.assertEqual(render_code_decisions(self.workspace, "", self.meta), "")

    def test_rejected_line_shape(self):
        long_problem = "x" * 300
        self.write_round(
            "r1",
            "a" * 64,
            "2026-01-01T00:00:00Z",
            [
                finding("Two\nlines", "rejected", note="see\nabove", reviewer_id="seat-alpha"),
                finding(long_problem, "rejected", file="long.py", line="7", reviewer_id="seat-beta"),
            ],
        )
        text = self.render()
        self.assertIn("- app.py:3 -- Two\\nlines -- see\\nabove", text)
        line = next(item for item in text.splitlines() if item.startswith("- long.py:7 -- "))
        problem = line[len("- long.py:7 -- ") : line.index(" -- no reason recorded")]
        self.assertEqual(len(problem), 160)
        self.assertTrue(problem.endswith("..."))
        self.assertNotIn("seat-alpha", text)
        self.assertNotIn("seat-beta", text)

    def test_rejected_label_is_context_not_authority(self):
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", [finding("Poisoned", "rejected")])
        text = self.render()
        self.assertIn(CODE_DECISIONS_REJECTED_LABEL, text)
        self.assertIn("Context, not authority", CODE_DECISIONS_REJECTED_LABEL)
        self.assertIn("Still report any other problem at the same location", CODE_DECISIONS_REJECTED_LABEL)

    def test_only_the_user_and_plan_parts_are_premises(self):
        """The shared intro calls nothing a premise, and says a rejection never excuses a severe issue."""
        intro = CODE_DECISIONS_SECTION.format(parts="")
        self.assertNotIn("premise", intro.lower())
        self.assertIn("never excuses a critical or high issue", intro)
        self.assertIn("fresh evidence", intro)
        self.assertIn("Premises", CODE_DECISIONS_USER_LABEL)
        self.assertIn("Premises", CODE_DECISIONS_PLAN_LABEL)
        self.assertIn("not premises", CODE_DECISIONS_REJECTED_LABEL)

    def test_later_acceptance_on_this_snapshot_wins(self):
        """Snapshot A rejected it, a round of this snapshot accepted it since: not listed."""
        self.write_round("a", "a" * 64, "2026-01-01T00:00:00Z", [finding("Flip-flop", "rejected")])
        self.write_round("b", CURRENT_SHA, "2026-01-02T00:00:00Z", [finding("Flip-flop", "accepted")])
        self.assertEqual(self.render(), "")

    def test_rejection_on_this_snapshot_is_not_listed(self):
        """Accepted on A, rejected on this snapshot: its own rejection, not an earlier one."""
        self.write_round("a", "a" * 64, "2026-01-01T00:00:00Z", [finding("Own", "accepted")])
        self.write_round("b", CURRENT_SHA, "2026-01-02T00:00:00Z", [finding("Own", "rejected")])
        self.assertEqual(self.render(), "")

    def test_rejected_then_absent_stays_listed(self):
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", [finding("Gone later", "rejected")])
        self.write_round("r2", "b" * 64, "2026-01-02T00:00:00Z", [finding("Other", "accepted", file="x.py")])
        self.assertIn("- app.py:3 -- Gone later -- no reason recorded", self.render())

    def test_equal_timestamps_order_by_snapshot(self):
        """A tie is broken by the snapshot, not by the order the files are listed in."""
        stamp = "2026-01-01T00:00:00Z"
        # File names sort the other way round from the snapshots.
        self.write_round("z-first", "a" * 64, stamp, [finding("Tied", "rejected")])
        self.write_round("a-second", "b" * 64, stamp, [finding("Tied", "accepted")])
        self.assertEqual(self.render(), "")
        self.write_round("z-first", "a" * 64, stamp, [finding("Tied", "accepted")])
        self.write_round("a-second", "b" * 64, stamp, [finding("Tied", "rejected")])
        first = self.render()
        self.assertIn("Tied", first)
        self.assertEqual(self.render(), first)

    def test_equal_timestamp_and_snapshot_order_by_round_id(self):
        """The last tie is broken by the round id, not by the order the files are listed in."""
        stamp = "2026-01-01T00:00:00Z"

        def write(file_name, round_id, triage):
            data = {
                "snapshot": {"sha256": "a" * 64, "round_id": round_id},
                "generated_at": stamp,
                "lineage": LINEAGE,
                "findings": [finding("Tied", triage)],
            }
            self.write_raw(file_name, data)

        # File names sort the other way round from the round ids.
        write("a-file", "round-b", "accepted")
        write("z-file", "round-a", "rejected")
        self.assertEqual(self.render(), "")
        write("a-file", "round-b", "rejected")
        write("z-file", "round-a", "accepted")
        self.assertIn("Tied", self.render())

    def test_timestamps_in_other_formats_order_by_time(self):
        """An offset or fractional seconds orders by the moment, not by the string."""
        # 08:00 at +09:00 is 23:00 UTC the day before: earlier, though it sorts later as text.
        self.write_round("utc", "a" * 64, "2026-01-01T00:00:00Z", [finding("Offset", "rejected")])
        self.write_round("tokyo", "b" * 64, "2026-01-01T08:00:00+09:00", [finding("Offset", "accepted")])
        self.assertIn("Offset", self.render())
        # Half a second later, though "." sorts before "Z" as text.
        self.write_round("utc", "a" * 64, "2026-01-01T00:00:00Z", [finding("Offset", "accepted")])
        later = "2026-01-01T00:00:00.500000+00:00"
        self.write_round("tokyo", "b" * 64, later, [finding("Offset", "rejected")])
        self.assertIn("Offset", self.render())

    def test_acceptance_under_a_merged_wording_supersedes_the_rejection(self):
        """A rejected report later merged into a longer, accepted wording is accepted, both ways round."""
        short, longer = "Short wording", "Short wording, now said at length"
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", [finding(short, "rejected", note="noise")])
        merged = dict(finding(longer, "accepted"), merged_reports=[{"problem": short}])
        self.write_round("r2", "b" * 64, "2026-01-02T00:00:00Z", [merged])
        self.assertEqual(self.render(), "")
        # The other way round: rejected under the merged wording, then accepted in the short one.
        merged = dict(finding(longer, "rejected", note="noise"), merged_reports=[{"problem": short}])
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", [merged])
        self.write_round("r2", "b" * 64, "2026-01-02T00:00:00Z", [finding(short, "accepted")])
        self.assertEqual(self.render(), "")

    def test_a_rejected_merged_finding_is_listed_once(self):
        merged = dict(finding("Longer wording", "rejected", note="noise"))
        merged["merged_reports"] = [{"problem": "Short"}]
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", [merged])
        self.assertEqual(self.render().count("Longer wording"), 1)

    def test_out_of_range_timestamp_sorts_with_missing(self):
        """A stamp that overflows once moved to UTC is unreadable, not a crash of the round."""
        self.write_round("later", "b" * 64, "2026-01-02T00:00:00Z", [finding("Edge", "rejected")])
        self.write_round("edge", "a" * 64, "0001-01-01T00:00:00+05:00", [finding("Edge", "accepted")])
        self.assertIn("Edge", self.render())

    def test_unreadable_timestamp_sorts_with_missing(self):
        self.write_round("later", "b" * 64, "2026-01-02T00:00:00Z", [finding("Garbled", "rejected")])
        self.write_round("garbled", "a" * 64, "yesterday", [finding("Garbled", "accepted")])
        self.assertIn("Garbled", self.render())

    def test_missing_timestamp_sorts_first(self):
        self.write_round("later", "b" * 64, "2026-01-02T00:00:00Z", [finding("Stamped", "rejected")])
        self.write_round("unstamped", "a" * 64, None, [finding("Stamped", "accepted")])
        self.assertIn("Stamped", self.render())

    def test_missing_file_and_line(self):
        item = finding("Nowhere", "rejected", note="ok")
        del item["file"], item["line"]
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", [item])
        self.assertIn("- ?:n/a -- Nowhere -- ok", self.render())

    def test_malformed_rounds_are_skipped(self):
        self.write_raw("bad-snapshot", {"snapshot": "oops", "lineage": LINEAGE, "findings": []})
        self.write_raw("bad-list", {"snapshot": ["x"], "lineage": LINEAGE})
        for name, findings in (("str", "oops"), ("dict", {"file": "app.py"}), ("none", None)):
            self.write_round(name, name[0] * 64, "2026-01-01T00:00:00Z", findings)
        self.write_round("mixed", "m" * 64, "2026-01-01T00:00:00Z", ["text", 3, finding("Kept", "rejected")])
        text = self.render()
        self.assertIn("- app.py:3 -- Kept -- no reason recorded", text)
        self.assertEqual(text.count("\n- "), 1)

    def test_none_like_plan_bodies(self):
        """Only a body that is "None" alone adds nothing; anything more is carried."""
        for body, carried in (
            ("None", False),
            ("- none.", False),
            ("**None**", False),
            ("N/A", True),
            ("None. (see below)", True),
        ):
            with self.subTest(body=body):
                self.write_plan("# Plan\n\n## Risks\n\n%s\n" % body)
                text = self.render()
                if carried:
                    self.assertIn(body, text)
                else:
                    self.assertEqual(text, "")

    def test_cap_with_a_fence_in_the_answers(self):
        """A body holding its own fence gets a longer one, and the cap still holds wherever it falls."""
        lines = ["Answer %d." % index for index in range(30)]
        self.user = "\n".join([*lines[:15], "```python", "x = 1", "```", *lines[15:]])
        self.write_plan(PLAN)
        decisions = self.workspace.relative(self.workspace.decisions_path)
        full = self.render(100_000)
        self.assertIn(CODE_DECISIONS_USER_LABEL + "\n\n````markdown\n", full)
        for cap in range(len(CODE_DECISIONS_SECTION.format(parts="")), len(full) + 20, 3):
            with self.subTest(cap=cap):
                text = self.render(cap)
                marker = "\n\nThis section was cut at"
                section = text.split(marker)[0]
                self.assertLessEqual(len(section), cap)
                # decisions.md is named exactly when its part did not fit whole.
                note = text[len(section) :]
                self.assertEqual("`%s`" % decisions in note, "Answer 29." not in section)
                # The wrapper is never closed early: once the inner fence is
                # kept, the wrapper's is longer than it.
                if "```python" in section:
                    self.assertIn(CODE_DECISIONS_USER_LABEL + "\n\n````markdown\n", section)

    def sweep_caps(self, wrapper, inner):
        """Every cap from the bare head up: the section fits, and once ``inner`` is kept so is ``wrapper``."""
        full = self.render(100_000)
        self.assertIn(wrapper, full)
        for cap in range(len(CODE_DECISIONS_SECTION.format(parts="")), len(full) + 20, 3):
            with self.subTest(cap=cap):
                section = self.render(cap).split("\n\nThis section was cut at")[0]
                self.assertLessEqual(len(section), cap)
                if inner in section:
                    self.assertIn(wrapper, section)

    def test_cap_with_a_fence_in_the_plan(self):
        body = "\n".join(
            [*("- Risk %d." % index for index in range(15)), "```python", "x = 1", "```", "- Last risk."]
        )
        self.write_plan("# Plan\n\n## Risks\n\n%s\n" % body)
        label = CODE_DECISIONS_PLAN_LABEL.format(sections="plan.md#Risks")
        self.sweep_caps(label + "\n\n````markdown\n", "```python")

    def test_cap_with_a_fence_in_a_rejected_problem(self):
        """The problem text is a reviewer's, and may hold a fence of any length."""
        for fence in ("```", "````"):
            with self.subTest(fence=fence):
                rejected = [
                    finding("Rejected %d" % index, "rejected", file="f%d.py" % index) for index in range(10)
                ]
                rejected.append(finding("%s rm -rf" % fence, "rejected", file="z.py", note="noise"))
                self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", rejected)
                wrapper = CODE_DECISIONS_REJECTED_LABEL + "\n\n%s`text\n" % fence
                self.sweep_caps(wrapper, "- z.py:3 -- %s rm -rf" % fence)

    def test_cap_just_above_the_head(self):
        """No line of the first part fits: every part is dropped and still named."""
        self.user = "Use SQLite."
        self.write_plan(PLAN)
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", [finding("R", "rejected")])
        empty = CODE_DECISIONS_SECTION.format(parts="")
        text = self.render(len(empty) + 5)
        section, note = text.split("\n\nThis section was cut at")
        self.assertEqual(section, empty.rstrip())
        decisions = self.workspace.relative(self.workspace.decisions_path)
        plan = self.workspace.relative(self.workspace.plan_path)
        expected = " chars. Full text: `%s`, `%s`; 1 earlier rejected finding(s) left out." % (
            decisions,
            plan,
        )
        self.assertTrue(note.endswith(expected), note)

    def test_cut_inside_the_plan_names_only_the_plan(self):
        self.user = "Use SQLite."
        risks = "\n".join("- Risk number %d is worth a line." % index for index in range(200))
        self.write_plan("# Plan\n\n## Risks\n\n%s\n" % risks)
        cap = 2000
        text = self.render(cap)
        section, note = text.rsplit("\n\n", 1)
        self.assertLessEqual(len(section), cap)
        self.assertIn("Use SQLite.", section)
        self.assertIn("- Risk number 0 ", section)
        plan = self.workspace.relative(self.workspace.plan_path)
        self.assertEqual(note, "This section was cut at 2,000 chars. Full text: `%s`." % plan)

    def test_cap_and_note(self):
        self.user = "\n".join("Answer line %d about the decision." % index for index in range(400))
        self.write_plan(PLAN)
        self.write_round(
            "r1",
            "a" * 64,
            "2026-01-01T00:00:00Z",
            [finding("Rejected %d" % index, "rejected", file="f%d.py" % index) for index in range(5)],
        )
        cap = 2000
        text = self.render(cap)
        section, note = text.rsplit("\n\n", 1)
        self.assertLessEqual(len(section), cap)
        self.assertTrue(note.startswith("This section was cut at 2,000 chars."))
        decisions = self.workspace.relative(self.workspace.decisions_path)
        plan = self.workspace.relative(self.workspace.plan_path)
        expected = "Full text: `%s`, `%s`; 5 earlier rejected finding(s) left out." % (decisions, plan)
        self.assertIn(expected, note)
        self.assertIn("Answer line 0 ", section)
        self.assertNotIn("Slow on a cold start", section)
        # Cut on a line boundary: every line kept is whole.
        self.assertNotIn("Answer line 399", section)
        for item in section.splitlines():
            if item.startswith("Answer line"):
                self.assertTrue(item.endswith("about the decision."))

    def test_cap_names_only_cut_sources(self):
        self.user = "Use SQLite."
        filler = "y" * 100
        many = [finding("Rejected %d %s" % (n, filler), "rejected", file="f%d.py" % n) for n in range(60)]
        self.write_round("r1", "a" * 64, "2026-01-01T00:00:00Z", many)
        text = self.render()
        section, note = text.rsplit("\n\n", 1)
        self.assertLessEqual(len(section), DECISIONS_CAP_CHARS)
        self.assertIn("Use SQLite.", section)
        self.assertNotIn("Full text", note)
        self.assertRegex(
            note, r"^This section was cut at 6,000 chars\. \d+ earlier rejected finding\(s\) left out\.$"
        )
        shown = sum(1 for item in section.splitlines() if item.startswith("- f"))
        self.assertGreater(shown, 0)
        self.assertIn(" %d earlier" % (60 - shown), note)

    def test_position_in_prompt(self):
        self.write_plan(PLAN)
        ws.write_json(
            self.workspace.snapshot_meta_path,
            {"sha256": CURRENT_SHA, "units": {"u1": {"resolved_files": ["app.py"]}}},
        )
        decisions = self.render()
        with mock.patch(
            "orchestrator.review_fanout.context_mod.render_surrounding", return_value="SURROUNDING NOTE"
        ):
            built = review_mod.build_review_prompt(
                reviewer(), self.workspace, "diff body", decisions=decisions
            )
        text = built.text
        units = text.index("separate work units")
        surrounding = text.index("SURROUNDING NOTE")
        section = text.index("## Decisions already made")
        output = text.index("\n## Output\n")
        self.assertLess(units, surrounding)
        self.assertLess(surrounding, section)
        self.assertLess(section, output)
        self.assertIn("\n\n" + decisions + "\n\n## Output", text)


class RealStateTest(IsolatedCase):
    """The user part read through the real state and ``decisions_text``, nothing patched."""

    def setUp(self):
        super().setUp()
        self.workspace = self.cli_workspace()
        self.meta = {"sha256": CURRENT_SHA}

    def test_no_questions_no_section(self):
        self.assertEqual(render_code_decisions(self.workspace, LINEAGE, self.meta), "")

    def test_option_answer_is_kept_and_open_one_left_out(self):
        asked = [
            {
                "topic": "Storage",
                "question": "Which store?",
                "options": [{"key": "A", "text": "SQLite"}, {"key": "B", "text": "Files"}],
                "recommended": None,
            },
            {"topic": "Retries", "question": "How many retries?", "options": [], "recommended": None},
        ]
        ids = questions_mod.record(self.workspace, asked, "orchestrator")["ids"]
        questions_mod.answer(self.workspace, [ids[0]], "option", option="A")
        text = render_code_decisions(self.workspace, LINEAGE, self.meta)
        self.assertIn("## %s: Storage" % ids[0], text)
        self.assertIn("- Answer: A", text)
        self.assertNotIn("Retries", text)

    def test_answers_and_open_questions(self):
        asked = [
            {"topic": "Storage", "question": "Which store?", "options": [], "recommended": None},
            {"topic": "Retries", "question": "How many retries?", "options": [], "recommended": None},
        ]
        ids = questions_mod.record(self.workspace, asked, "orchestrator")["ids"]
        questions_mod.answer(self.workspace, [ids[0]], "text", text="Use SQLite, not files")
        text = render_code_decisions(self.workspace, LINEAGE, self.meta)
        self.assertIn(CODE_DECISIONS_USER_LABEL + "\n\n```markdown\n", text)
        self.assertIn("## %s: Storage" % ids[0], text)
        self.assertIn("- Answer: Use SQLite, not files", text)
        # An open question is not a decision: it is left out, not given as a premise.
        self.assertNotIn("Retries", text)
        self.assertNotIn("not answered yet", text)
        self.assertNotIn("was cut", text)

    def test_only_open_questions_no_section(self):
        asked = [{"topic": "Retries", "question": "How many retries?", "options": [], "recommended": None}]
        questions_mod.record(self.workspace, asked, "orchestrator")
        self.assertEqual(render_code_decisions(self.workspace, LINEAGE, self.meta), "")


@unittest.skipUnless(has_git(), "git is required")
class FanoutWiringTest(IsolatedCase):
    """``run_reviews`` hands the section to every code seat, and a ``prompt_for`` never sees it."""

    SECTION = "## Decisions already made\n\nWIRING-MARKER"

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.write("app.py", "x = 2\n")
        self.workspace = self.cli_workspace()
        review_mod.create_snapshot(self.workspace)
        self.prompts = capture_prompts(self)

    def test_code_seats_get_the_section(self):
        options = review_mod.FanoutOptions(parallel=False, decisions=self.SECTION)
        runs = review_mod.run_reviews([reviewer("r1"), reviewer("r2")], self.workspace, options)
        self.assertEqual([run.status for run in runs], ["ok", "ok"])
        self.assertEqual(len(self.prompts), 2)
        for prompt in self.prompts:
            self.assertIn("\n\n" + self.SECTION + "\n\n## Output", prompt)

    def test_prompt_for_ignores_it(self):
        def built(seat):
            return review_mod.BuiltPrompt("OWN PROMPT for %s" % seat["id"], "inline", 10)

        options = review_mod.FanoutOptions(parallel=False, decisions=self.SECTION, prompt_for=built)
        review_mod.run_reviews([reviewer("r1")], self.workspace, options)
        self.assertEqual(self.prompts, ["OWN PROMPT for r1"])


@unittest.skipUnless(has_git(), "git is required")
class ReviewRunWiringTest(IsolatedCase):
    """``review run`` builds the section once and every code seat's prompt ends with it."""

    def setUp(self):
        super().setUp()
        self.init_git_repo()
        self.write("app.py", "x = 1\n")
        self.commit_all("init")
        self.write("app.py", "x = 2\n")
        run_cli("config", "setup", "--defaults")
        for seat in ("claude-general", "codex-general", "claude-security", "claude-test"):
            run_cli("reviewer", "remove", seat)
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m1", "--role", "general")
        run_cli("reviewer", "add", "--provider", "mock", "--id", "m2", "--role", "general")
        self.workspace = self.cli_workspace()
        self.prompts = capture_prompts(self)

    def test_code_round_carries_the_plan(self):
        ws.write_text(self.workspace.plan_path, "# Plan\n\n## Decisions\n\n- CODE-PLAN-DECISION\n")
        run_cli("review", "snapshot")
        # --high-risk keeps the whole panel, which a change this small would shrink to one seat.
        _, out, err = run_cli("review", "run", "--high-risk")
        self.assertIn("2 successful, 0 failed", out, err)
        self.assertEqual(len(self.prompts), 2)
        for prompt in self.prompts:
            self.assertIn(CODE_INTRO, prompt)
            self.assertIn(CODE_DECISIONS_PLAN_LABEL.format(sections="plan.md#Decisions"), prompt)
            self.assertIn("- CODE-PLAN-DECISION", prompt)
            self.assertLess(prompt.index(CODE_INTRO), prompt.index("\n## Output\n"))

    def test_a_rejection_reaches_the_next_round(self):
        """Consolidated, triaged and archived for real: the next round's prompt lists the rejection."""
        problem = "INTEGRATION-PROBLEM the value is hard-coded"
        self.set_env(
            "DEV_ORCHESTRA_MOCK_RESPONSE",
            "## Finding\n- Severity: medium\n- File: app.py\n- Line: 1\n- Category: correctness\n"
            "- Problem: %s\n- Impact: none\n- Evidence: x = 2\n- Fix: read it from config\n" % problem,
        )
        run_cli("review", "snapshot")
        _, out, err = run_cli("review", "run", "--high-risk")
        self.assertIn("2 successful, 0 failed", out, err)
        live = ws.read_json(self.workspace.consolidated_json_path, {})
        (entry,) = live["findings"]
        self.assertEqual(entry["reported_by"], ["m1", "m2"])
        code, _, err = run_cli("review", "triage", "F1", "--status", "rejected", "--note", "INTEGRATION-NOTE")
        self.assertEqual(code, 0, err)
        self.write("app.py", "x = 3\n")
        run_cli("review", "snapshot")
        del self.prompts[:]
        _, out, err = run_cli("review", "run", "--high-risk")
        self.assertIn("2 successful, 0 failed", out, err)
        self.assertEqual(len(self.prompts), 2)
        expected = "- app.py:1 -- %s -- INTEGRATION-NOTE" % problem
        for prompt in self.prompts:
            self.assertIn(CODE_DECISIONS_REJECTED_LABEL, prompt)
            (line,) = [item for item in prompt.splitlines() if item.startswith("- app.py:1 -- ")]
            self.assertEqual(line, expected)
            self.assertNotIn("m1", line)
            self.assertNotIn("m2", line)

    def test_answered_questions_reach_the_prompt(self):
        asked = [
            {"topic": "Storage", "question": "Which store?", "options": [], "recommended": None},
            {"topic": "Retries", "question": "How many retries?", "options": [], "recommended": None},
        ]
        ids = questions_mod.record(self.workspace, asked, "orchestrator")["ids"]
        questions_mod.answer(self.workspace, [ids[0]], "text", text="RUN-ANSWER use SQLite")
        run_cli("review", "snapshot")
        _, out, err = run_cli("review", "run", "--high-risk")
        self.assertIn("2 successful, 0 failed", out, err)
        for prompt in self.prompts:
            self.assertIn(CODE_DECISIONS_USER_LABEL, prompt)
            self.assertIn("- Answer: RUN-ANSWER use SQLite", prompt)
            self.assertNotIn("How many retries?", prompt)

    def test_code_round_without_any_has_none(self):
        run_cli("review", "snapshot")
        _, out, err = run_cli("review", "run")
        self.assertIn("1 successful, 0 failed", out, err)
        (prompt,) = self.prompts
        self.assertNotIn("Decisions already made", prompt)


class DesignRunWiringTest(DesignReviewCase):
    """A design round never gets the code round's section, whatever there is to put in it."""

    def setUp(self):
        super().setUp()
        self.prompts = capture_prompts(self)

    def test_design_round_has_no_code_section(self):
        self.write_plan(DESIGN_PLAN + "\n## Risks\n\n- DESIGN-PLAN-RISK\n")
        self.write_request()
        with mock.patch(
            "orchestrator.cli_review.render_code_decisions", return_value="## Decisions already made\n\nLEAK"
        ):
            _, out, err = run_cli("review", "run", "--design")
        self.assertIn("2 successful, 0 failed", out, err)
        self.assertEqual(len(self.prompts), 2)
        for prompt in self.prompts:
            self.assertNotIn(CODE_INTRO, prompt)
            self.assertNotIn("LEAK", prompt)
            self.assertNotIn("From the plan (", prompt)


if __name__ == "__main__":
    unittest.main()
