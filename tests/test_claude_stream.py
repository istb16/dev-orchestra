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
from orchestrator.providers.claude import ClaudeProvider, parse_stream_json


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
