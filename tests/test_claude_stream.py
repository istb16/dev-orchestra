"""The Claude adapter's streaming output format.

It exists for one reason: the text format prints nothing until the run is nearly
over, so an idle deadline could not tell a wedged agent from a busy one. The
trade is a parsed event stream, so these tests care mostly about degrading
safely when that stream is not what we expect -- losing the model's answer to a
schema change would be a worse bug than the one being fixed.
"""

from __future__ import annotations

import json
import unittest

from helpers import IsolatedCase

from orchestrator.execution import ExecOutcome
from orchestrator.providers import base
from orchestrator.providers.claude import ClaudeProvider, parse_stream_json, parse_stream_tools


def stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


RESULT_EVENT = {"type": "result", "subtype": "success", "result": "the final answer", "is_error": False}
THINKING = {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 12}
ASSISTANT = {
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "partial thoughts"}]},
}


class TestStreamParsing(IsolatedCase):
    def test_the_result_event_is_the_answer(self):
        text, note = parse_stream_json(stream(THINKING, ASSISTANT, RESULT_EVENT))
        self.assertEqual(text, "the final answer")
        self.assertEqual(note, "")

    def test_the_last_result_event_wins(self):
        first = dict(RESULT_EVENT, result="stale")
        text, _ = parse_stream_json(stream(first, RESULT_EVENT))
        self.assertEqual(text, "the final answer")

    def test_an_error_result_is_flagged_but_still_returned(self):
        text, note = parse_stream_json(stream(dict(RESULT_EVENT, is_error=True)))
        self.assertEqual(text, "the final answer")
        self.assertIn("is_error", note)

    def test_assistant_text_is_the_fallback_when_there_is_no_result(self):
        text, note = parse_stream_json(stream(THINKING, ASSISTANT))
        self.assertEqual(text, "partial thoughts")
        self.assertIn("reconstructed", note)

    def test_several_assistant_blocks_are_joined(self):
        second = {"type": "assistant", "message": {"content": [{"type": "text", "text": "and more"}]}}
        text, _ = parse_stream_json(stream(ASSISTANT, second))
        self.assertEqual(text, "partial thoughts\nand more")

    def test_an_empty_result_falls_back_rather_than_returning_nothing(self):
        text, _ = parse_stream_json(stream(ASSISTANT, dict(RESULT_EVENT, result="   ")))
        self.assertEqual(text, "partial thoughts")

    def test_plain_text_output_is_left_alone(self):
        """A non-stream response must never be discarded as unparseable."""
        self.assertEqual(parse_stream_json("just some prose\n"), (None, ""))

    def test_unknown_event_types_are_ignored_not_fatal(self):
        text, _ = parse_stream_json(stream({"type": "something_new", "payload": 1}, RESULT_EVENT))
        self.assertEqual(text, "the final answer")

    def test_malformed_lines_are_skipped(self):
        raw = "{not json\n" + json.dumps(RESULT_EVENT) + "\n"
        text, _ = parse_stream_json(raw)
        self.assertEqual(text, "the final answer")

    def test_a_stream_with_no_usable_content_is_reported_as_such(self):
        self.assertEqual(parse_stream_json(stream(THINKING)), (None, ""))

    def test_empty_output(self):
        self.assertEqual(parse_stream_json(""), (None, ""))


class TestPostprocess(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()

    def test_a_stream_is_reduced_to_its_answer(self):
        outcome = ExecOutcome(0, stream(THINKING, RESULT_EVENT), "", 1.0)
        stdout, _ = self.provider.postprocess(outcome, base.MODE_PLAN)
        self.assertEqual(stdout, "the final answer")

    def test_raw_output_survives_when_it_is_not_a_stream(self):
        outcome = ExecOutcome(0, "plain answer\n", "some warning", 1.0)
        stdout, stderr = self.provider.postprocess(outcome, base.MODE_PLAN)
        self.assertEqual(stdout, "plain answer\n")
        self.assertEqual(stderr, "some warning")

    def test_a_fallback_note_reaches_stderr(self):
        outcome = ExecOutcome(0, stream(ASSISTANT), "", 1.0)
        stdout, stderr = self.provider.postprocess(outcome, base.MODE_PLAN)
        self.assertEqual(stdout, "partial thoughts")
        self.assertIn("reconstructed", stderr)


def tool_use(identifier, name):
    block = {"type": "tool_use", "id": identifier, "name": name}
    return {"type": "assistant", "message": {"content": [block]}}


def tool_result(identifier, content):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": identifier, "content": content}]},
    }


class TestToolActivity(IsolatedCase):
    """What the agent did with its tools, counted from the same stream.

    Two things this must not do. It must not count only ``Read``: review mode
    denies `Edit,Write,NotebookEdit` and nothing else, so `Bash`, `Grep` and
    `Glob` are all ways to read a file -- and the run measured before this was
    designed read `CONTRIBUTING.md` with `wc -l`. And it must not present the
    character count as how much source was read, which is why the tests below
    fix only what it is: the characters the tools printed back.
    """

    def test_every_tool_is_counted_and_broken_down_by_name(self):
        events = [
            tool_use("a", "Read"),
            tool_result("a", "x" * 100),
            tool_use("b", "Bash"),
            tool_result("b", "3\n"),
            tool_use("c", "Grep"),
            tool_result("c", "app.py:1\n"),
            tool_use("d", "Bash"),
            tool_result("d", "ok"),
            RESULT_EVENT,
        ]
        tools = parse_stream_tools(stream(*events))
        self.assertEqual(tools["tool_uses"], 4)
        self.assertEqual(tools["tool_uses_by_name"], {"Read": 1, "Bash": 2, "Grep": 1})
        self.assertEqual(tools["tool_output_chars"], 100 + 2 + 9 + 2)
        self.assertEqual(tools["tool_output_chars_by_name"]["Bash"], 4)

    def test_a_result_carrying_blocks_is_measured_by_its_text(self):
        """``content`` is a string on some results and a list on others."""
        blocks = [{"type": "text", "text": "abcde"}, {"type": "text", "text": "fg"}]
        tools = parse_stream_tools(stream(tool_use("a", "Read"), tool_result("a", blocks)))
        self.assertEqual(tools["tool_output_chars"], 7)
        self.assertEqual(tools["tool_output_chars_by_name"], {"Read": 7})

    def test_a_block_with_no_text_adds_nothing_rather_than_failing(self):
        blocks = [{"type": "image", "source": {"data": "...."}}, {"type": "text", "text": "ab"}]
        tools = parse_stream_tools(stream(tool_use("a", "Read"), tool_result("a", blocks)))
        self.assertEqual(tools["tool_output_chars"], 2)

    def test_a_result_with_no_matching_call_is_counted_as_unknown(self):
        """Dropping it would make the breakdown sum to less than the total."""
        tools = parse_stream_tools(stream(tool_use("a", "Read"), tool_result("zz", "12345")))
        self.assertEqual(tools["tool_output_chars"], 5)
        self.assertEqual(tools["tool_output_chars_by_name"], {"unknown": 5})
        self.assertEqual(sum(tools["tool_output_chars_by_name"].values()), tools["tool_output_chars"])

    def test_a_stream_that_used_no_tools_reports_a_measured_zero(self):
        tools = parse_stream_tools(stream(THINKING, ASSISTANT, RESULT_EVENT))
        self.assertEqual(tools["tool_uses"], 0)
        self.assertEqual(tools["tool_output_chars"], 0)
        self.assertEqual(tools["tool_uses_by_name"], {})

    def test_output_that_is_not_a_stream_reports_nothing_at_all(self):
        """Unreported and zero are different facts, and this is the first."""
        self.assertIsNone(parse_stream_tools("just some prose\n"))
        self.assertIsNone(parse_stream_tools(""))

    def test_a_lone_result_object_is_not_a_stream_that_used_no_tools(self):
        """`output_format: json` is a documented option, and it prints one
        compact `result` object: parseable, and no stream. Counting it as a
        measured zero would claim the reviewer opened nothing for a format that
        never emits a tool event."""
        self.assertIsNone(parse_stream_tools(json.dumps(RESULT_EVENT) + "\n"))
        self.assertIsNone(parse_stream_tools("{}\n"))

    def test_a_tool_use_without_a_name_is_still_counted(self):
        unnamed = {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}
        tools = parse_stream_tools(stream(unnamed))
        self.assertEqual(tools["tool_uses"], 1)
        self.assertEqual(tools["tool_uses_by_name"], {"unknown": 1})

    def test_string_content_on_an_event_is_not_read_as_blocks(self):
        """Some events carry prose where others carry a list. Neither is a
        tool call, and asking a string for `.get` would be an exception."""
        tools = parse_stream_tools(stream({"type": "assistant", "message": {"content": "hello"}}))
        self.assertEqual(tools["tool_uses"], 0)


class TestToolActivityReachesUsage(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()

    def usage(self, *events):
        return self.provider.parse_usage(ExecOutcome(0, stream(*events), "", 1.0), base.MODE_REVIEW)

    def test_the_counts_ride_along_with_the_token_report(self):
        result = dict(RESULT_EVENT, usage={"input_tokens": 10, "output_tokens": 2})
        usage = self.usage(tool_use("a", "Bash"), tool_result("a", "3\n"), result)
        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.tool_uses, 1)
        self.assertEqual(usage.tool_uses_by_name, {"Bash": 1})
        self.assertEqual(usage.tool_output_chars, 2)

    def test_tool_calls_without_a_usage_report_are_still_recorded(self):
        """A stream can end without a usable `result` event. The run still
        used the tools it used, and `measured` still means tokens."""
        usage = self.usage(tool_use("a", "Read"), tool_result("a", "abc"))
        self.assertEqual(usage.tool_uses, 1)
        self.assertFalse(usage.measured)
        self.assertIsNone(usage.billed_tokens)

    def test_the_json_output_format_reports_its_tokens_and_no_tool_counts(self):
        """One compact `result` object is a priced run whose tool activity was
        never streamed. The invoice is readable; the counts are not, and must
        not arrive as zeroes."""
        result = dict(RESULT_EVENT, usage={"input_tokens": 10}, total_cost_usd=0.01)
        outcome = ExecOutcome(0, json.dumps(result) + "\n", "", 1.0)
        usage = self.provider.parse_usage(outcome, base.MODE_REVIEW)
        self.assertEqual(usage.input_tokens, 10)
        self.assertIsNone(usage.tool_uses)
        self.assertIsNone(usage.tool_output_chars)

    def test_the_per_name_character_breakdown_stays_in_the_provider(self):
        """Nothing aggregates it, so it is not carried into `Usage` and not
        written into every run's event where it could never be read back."""
        usage = self.usage(tool_use("a", "Bash"), tool_result("a", "3\n"))
        self.assertNotIn("tool_output_chars_by_name", usage.to_dict())
        self.assertFalse(hasattr(usage, "tool_output_chars_by_name"))

    def test_a_non_stream_run_leaves_all_three_unreported(self):
        """The adapter reports nothing, and the `Usage` the caller falls back
        to leaves the three fields None rather than at a measured zero."""
        usage = self.provider.parse_usage(ExecOutcome(0, "plain prose\n", "", 1.0), base.MODE_REVIEW)
        self.assertIsNone(usage)
        blank = base.Usage()
        fields = (blank.tool_uses, blank.tool_uses_by_name, blank.tool_output_chars)
        self.assertEqual(fields, (None, None, None))


class TestCommandShape(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()
        self.provider._capture = lambda command, timeout=30: _Help()
        self.resolved = self.provider.resolve_model({"family": "opus"})

    def test_streaming_is_the_default_and_needs_verbose(self):
        command = self.provider.build_command(base.MODE_PLAN, self.resolved, self.project)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", command)

    def test_a_role_can_opt_back_out_to_text(self):
        command = self.provider.build_command(
            base.MODE_PLAN, self.resolved, self.project, options={"output_format": "text"}
        )
        self.assertEqual(command[command.index("--output-format") + 1], "text")
        self.assertNotIn("--verbose", command)

    def test_an_unknown_output_format_is_rejected(self):
        problems = self.provider.validate_options({"output_format": "yaml"})
        self.assertTrue(any("output_format" in p for p in problems))

    def test_read_only_enforcement_is_unaffected_by_the_format(self):
        command = self.provider.build_command(base.MODE_REVIEW, self.resolved, self.project)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertIn("--disallowed-tools", command)


class TestIdleDeadlineApplicability(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()

    def test_streaming_gets_an_idle_deadline(self):
        self.assertEqual(self.provider.idle_timeout({}, 300), 300)

    def test_the_text_format_gets_none(self):
        """Text output is silent until the end, so a deadline would be wrong."""
        self.assertIsNone(self.provider.idle_timeout({"output_format": "text"}, 300))

    def test_a_role_can_set_its_own(self):
        self.assertEqual(self.provider.idle_timeout({"idle_timeout": 60}, 300), 60)

    def test_a_provider_that_does_not_stream_never_gets_one(self):
        class Quiet(base.Provider):
            streams_progress = False

        self.assertIsNone(Quiet().idle_timeout({}, 300))


class _Help:
    returncode = 0
    stderr = ""
    stdout = (
        "  --model <model>                       Provide an alias for the latest\n"
        "                                        model (e.g. 'fable', 'opus', or\n"
        "                                        'sonnet').\n"
        '  --permission-mode <mode>              (choices: "acceptEdits", "plan",\n'
        '                                        "bypassPermissions")\n'
    )


if __name__ == "__main__":
    unittest.main()
