"""The Claude adapter's streaming output format.

It exists for one reason: the text format prints nothing until the run is nearly
over, so an idle deadline could not tell a wedged agent from a busy one. The
trade is a parsed event stream, so these tests care mostly about degrading
safely when that stream is not what we expect -- losing the model's answer to a
schema change would be a worse bug than the one being fixed.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from typing import Any, ClassVar, Dict

from helpers import CLAUDE_HELP, IsolatedCase, present

from orchestrator.execution import ExecOutcome
from orchestrator.providers import base
from orchestrator.providers.claude import (
    ClaudeProvider,
    parse_stream_json,
    parse_stream_tools,
    parse_stream_usage,
)


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
        assert tools is not None
        self.assertEqual(tools["tool_uses"], 4)
        self.assertEqual(tools["tool_uses_by_name"], {"Read": 1, "Bash": 2, "Grep": 1})
        self.assertEqual(tools["tool_output_chars"], 100 + 2 + 9 + 2)

    def test_a_result_carrying_blocks_is_measured_by_its_text(self):
        """``content`` is a string on some results and a list on others."""
        blocks = [{"type": "text", "text": "abcde"}, {"type": "text", "text": "fg"}]
        tools = parse_stream_tools(stream(tool_use("a", "Read"), tool_result("a", blocks)))
        assert tools is not None
        self.assertEqual(tools["tool_output_chars"], 7)

    def test_a_block_with_no_text_adds_nothing_rather_than_failing(self):
        blocks = [{"type": "image", "source": {"data": "...."}}, {"type": "text", "text": "ab"}]
        tools = parse_stream_tools(stream(tool_use("a", "Read"), tool_result("a", blocks)))
        assert tools is not None
        self.assertEqual(tools["tool_output_chars"], 2)

    def test_a_result_with_no_matching_call_is_still_counted(self):
        """The characters were printed back whether or not this stream showed
        the call that asked for them. Dropping them would understate the run."""
        tools = parse_stream_tools(stream(tool_use("a", "Read"), tool_result("zz", "12345")))
        assert tools is not None
        self.assertEqual(tools["tool_output_chars"], 5)

    def test_a_stream_that_used_no_tools_reports_a_measured_zero(self):
        tools = parse_stream_tools(stream(THINKING, ASSISTANT, RESULT_EVENT))
        assert tools is not None
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
        assert tools is not None
        self.assertEqual(tools["tool_uses"], 1)
        self.assertEqual(tools["tool_uses_by_name"], {"unknown": 1})

    def test_string_content_on_an_event_is_not_read_as_blocks(self):
        """Some events carry prose where others carry a list. Neither is a
        tool call, and asking a string for `.get` would be an exception."""
        tools = parse_stream_tools(stream({"type": "assistant", "message": {"content": "hello"}}))
        assert tools is not None
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
        assert usage is not None
        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.tool_uses, 1)
        self.assertEqual(usage.tool_uses_by_name, {"Bash": 1})
        self.assertEqual(usage.tool_output_chars, 2)

    def test_tool_calls_without_a_usage_report_are_still_recorded(self):
        """A stream can end without a usable `result` event. The run still
        used the tools it used, and `measured` still means tokens."""
        usage = self.usage(tool_use("a", "Read"), tool_result("a", "abc"))
        assert usage is not None
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
        assert usage is not None
        self.assertEqual(usage.input_tokens, 10)
        self.assertIsNone(usage.tool_uses)
        self.assertIsNone(usage.tool_output_chars)

    def test_no_per_name_character_breakdown_is_produced_at_all(self):
        """Nothing aggregates it, so it is not carried into `Usage`, not
        written into every run's event where it could never be read back, and
        no longer accumulated per block on the way there."""
        usage = self.usage(tool_use("a", "Bash"), tool_result("a", "3\n"))
        assert usage is not None
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
        setattr(self.provider, "_capture", lambda command, timeout=30: _Help())
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

    def with_help(self, stdout, returncode=0):
        base.clear_discovery_cache()
        completed = _Help()
        completed.stdout, completed.returncode = stdout, returncode
        setattr(self.provider, "_capture", lambda command, timeout=30: completed)

    def test_partial_messages_are_asked_for_when_the_help_lists_them(self):
        self.with_help(CLAUDE_HELP)
        for mode in (base.MODE_PLAN, base.MODE_REVIEW, base.MODE_IMPLEMENT):
            with self.subTest(mode=mode):
                command = self.provider.build_command(mode, self.resolved, self.project)
                self.assertEqual(command[command.index("--verbose") + 1], "--include-partial-messages")

    def test_partial_messages_are_only_for_the_streaming_format(self):
        self.with_help(CLAUDE_HELP)
        for output_format in ("text", "json"):
            with self.subTest(output_format=output_format):
                command = self.provider.build_command(
                    base.MODE_PLAN, self.resolved, self.project, options={"output_format": output_format}
                )
                self.assertNotIn("--include-partial-messages", command)

    def test_partial_messages_are_never_guessed(self):
        """Help without the flag, or help that cannot be read, leaves the
        command as it was before the flag existed."""
        for stdout, code in ((_Help.stdout, 0), (CLAUDE_HELP, 1)):
            with self.subTest(returncode=code):
                self.with_help(stdout, code)
                command = self.provider.build_command(base.MODE_IMPLEMENT, self.resolved, self.project)
                self.assertNotIn("--include-partial-messages", command)
                self.assertEqual(command[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        base.clear_discovery_cache()
        self.provider._capture = lambda command, timeout=30: None
        command = self.provider.build_command(base.MODE_IMPLEMENT, self.resolved, self.project)
        self.assertNotIn("--include-partial-messages", command)

    def test_an_unknown_output_format_is_rejected(self):
        problems = self.provider.validate_options({"output_format": "yaml"})
        self.assertTrue(any("output_format" in p for p in problems))

    def test_read_only_enforcement_is_unaffected_by_the_format(self):
        command = self.provider.build_command(base.MODE_REVIEW, self.resolved, self.project)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertIn("--disallowed-tools", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--restricted", command)
        self.assertEqual(self.provider.read_only_enforcement()["status"], "verified")


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
        "  --restricted                          Restricted mode: removes the built-in\n"
        "  --strict-mcp-config                   Only use MCP servers from --mcp-config,\n"
        "  --tools <tools...>                    Specify the list of available tools from\n"
    )


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "claude")
MISSING = "00000000-0000-4000-8000-000000000000"
REJECTED_EXIT = 1


def fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


class TestResumeRejected(IsolatedCase):
    """Matched to what the CLI printed for a session that does not exist."""

    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()
        self.stdout = fixture("resume-rejected.stdout")
        self.stderr = fixture("resume-rejected.stderr")

    def rejected(self, stdout=None, exit_code=REJECTED_EXIT, options=None, session=MISSING, **flags):
        outcome = ExecOutcome(exit_code, self.stdout if stdout is None else stdout, self.stderr, 0.5, **flags)
        options = {} if options is None else options
        return self.provider.resume_rejected(outcome, base.MODE_PLAN, options, session)

    def result_with(self, **changes):
        event = json.loads(self.stdout)
        event.update(changes)
        return json.dumps(event) + "\n"

    def test_the_recorded_rejection_is_one(self):
        self.assertTrue(self.rejected())

    def test_only_for_the_id_that_was_asked_for(self):
        self.assertFalse(self.rejected(session="44444444-4444-4444-8444-444444444444"))

    def test_another_error_is_not(self):
        self.assertFalse(self.rejected(fixture("resume-rejected-other-error.stdout")))
        event = json.loads(self.stdout)
        del event["errors"]
        self.assertFalse(self.rejected(json.dumps(event) + "\n"))

    def test_a_run_that_got_anywhere_is_not(self):
        self.assertFalse(self.rejected(exit_code=0))
        self.assertFalse(self.rejected(stalled=True))
        self.assertFalse(self.rejected(timed_out=True))
        self.assertFalse(self.rejected(stream(THINKING) + self.stdout))
        self.assertFalse(self.rejected(""))
        self.assertFalse(self.rejected(self.result_with(num_turns=1)))
        self.assertFalse(self.rejected(self.result_with(is_error=False)))
        self.assertFalse(self.rejected(self.result_with(subtype="success")))

    def test_other_formats_are_never_judged(self):
        for output_format in ("json", "text"):
            with self.subTest(output_format=output_format):
                self.assertFalse(self.rejected(options={"output_format": output_format}))

    def test_the_rejection_is_measured_at_zero(self):
        outcome = ExecOutcome(REJECTED_EXIT, self.stdout, self.stderr, 0.5)
        usage = self.provider.parse_usage(outcome, base.MODE_PLAN)
        assert usage is not None
        self.assertTrue(usage.measured)
        self.assertEqual((usage.input_tokens, usage.output_tokens), (0, 0))
        self.assertEqual((usage.cache_read_tokens, usage.cache_write_tokens), (0, 0))
        self.assertEqual(usage.cost_usd, 0.0)
        self.assertIsNone(usage.tool_uses)
        self.assertEqual(
            self.provider.parse_session(outcome),
            {"session_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "context_tokens": None, "init": None},
        )


class TestParseSession(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()

    def parse(self, stdout):
        return self.provider.parse_session(ExecOutcome(0, stdout, "", 1.0))

    def test_the_recorded_resumed_session(self):
        stdout = fixture("resume-write-probe.jsonl")
        session = self.parse(stdout)
        self.assertEqual(session["session_id"], "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.assertEqual(session["context_tokens"], 11336)
        expected = {"tools": ["Glob", "Grep", "Read"], "mcp_servers": [], "permission_mode": "plan"}
        self.assertEqual(session["init"], dict(expected, version="2.1.283"))
        outcome = ExecOutcome(0, stdout, "", 1.0)
        self.assertFalse(self.provider.resume_rejected(outcome, base.MODE_PLAN, {}, MISSING))
        self.assertEqual(present(parse_stream_tools(stdout))["tool_uses"], 0)
        self.assertEqual(present(parse_stream_usage(stdout)).cost_usd, 0.037491)

    def test_the_fixture_carries_nothing_from_the_machine_it_was_recorded_on(self):
        stdout = fixture("resume-write-probe.jsonl")
        # A home directory or drive path would name the user it came from.
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\|/Users/|/home/|\\\\Users\\\\", stdout))
        for word in ("pipe", "signature", "scratchpad"):
            self.assertNotIn(word, stdout)

    def test_the_result_id_wins(self):
        session = self.parse(
            stream(
                {"type": "system", "subtype": "init", "session_id": "system-id"},
                {"type": "result", "session_id": "result-id"},
            )
        )
        self.assertEqual(session["session_id"], "result-id")

    def test_init_fields_of_the_wrong_shape_are_dropped(self):
        session = self.parse(stream({"type": "system", "subtype": "init", "tools": "Read"}))
        self.assertIsNone(session["init"]["tools"])
        self.assertIsNone(self.parse(stream(THINKING))["init"])

    def test_context_is_only_what_the_cli_counted(self):
        self.assertIsNone(self.parse(stream(RESULT_EVENT))["context_tokens"])
        self.assertIsNone(self.parse(stream(ASSISTANT))["context_tokens"])
        for bad in (True, "12"):
            usage = {"input_tokens": bad, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1}
            event = {"type": "assistant", "message": {"usage": usage}}
            with self.subTest(value=bad):
                self.assertIsNone(self.parse(stream(event))["context_tokens"])


class TestPartialMessages(IsolatedCase):
    """A recorded run with ``--include-partial-messages`` that called ``Read``.

    The ``stream_event`` lines repeat what the ``assistant`` events say, so
    every reader has to give the answer it gives without them.
    """

    def setUp(self):
        super().setUp()
        self.provider = ClaudeProvider()
        self.stdout = fixture("partial-messages-tool.jsonl")
        self.events = [json.loads(line) for line in self.stdout.splitlines()]
        self.result = next(event for event in self.events if event["type"] == "result")

    def outcome(self, stdout=None, exit_code=0):
        return ExecOutcome(exit_code, self.stdout if stdout is None else stdout, "", 1.0)

    def without_partial_messages(self):
        return stream(*[event for event in self.events if event["type"] != "stream_event"])

    def test_the_fixture_streams_partial_messages(self):
        kinds = {event["event"]["type"] for event in self.events if event["type"] == "stream_event"}
        self.assertTrue({"message_start", "content_block_start", "content_block_delta"} <= kinds)

    def test_the_answer_is_the_result_event(self):
        self.assertEqual(parse_stream_json(self.stdout), (self.result["result"], ""))

    def test_usage_is_the_result_event(self):
        usage = self.provider.parse_usage(self.outcome(), base.MODE_REVIEW)
        assert usage is not None
        self.assertEqual(usage.input_tokens, self.result["usage"]["input_tokens"])
        self.assertEqual(usage.output_tokens, self.result["usage"]["output_tokens"])
        self.assertEqual(usage.cache_read_tokens, self.result["usage"]["cache_read_input_tokens"])
        self.assertEqual(usage.cache_write_tokens, self.result["usage"]["cache_creation_input_tokens"])
        self.assertEqual(usage.cost_usd, self.result["total_cost_usd"])

    def test_a_tool_is_counted_once_not_again_from_its_content_block_start(self):
        names = [
            block["name"]
            for event in self.events
            if event["type"] == "assistant"
            for block in event["message"]["content"]
            if block["type"] == "tool_use"
        ]
        started = [
            event
            for event in self.events
            if event["type"] == "stream_event"
            and event["event"].get("content_block", {}).get("type") == "tool_use"
        ]
        self.assertIn("Read", names)
        self.assertEqual(len(started), len(names))
        tools = parse_stream_tools(self.stdout)
        assert tools is not None
        self.assertEqual(tools["tool_uses"], len(names))
        self.assertEqual(tools["tool_uses_by_name"], {name: names.count(name) for name in names})
        self.assertEqual(tools, parse_stream_tools(self.without_partial_messages()))

    def test_the_session_and_context_come_from_the_assistant_and_result_events(self):
        last = [event for event in self.events if event["type"] == "assistant"][-1]["message"]["usage"]
        context = sum(
            last[key] for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        session = self.provider.parse_session(self.outcome())
        self.assertEqual(session["session_id"], self.result["session_id"])
        self.assertEqual(session["context_tokens"], context)
        self.assertEqual(session, self.provider.parse_session(self.outcome(self.without_partial_messages())))

    def test_the_fixture_carries_nothing_from_the_machine_it_was_recorded_on(self):
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\|/Users/|/home/|\\\\Users\\\\", self.stdout))
        self.assertNotIn("scratchpad", self.stdout)

    def test_the_recorded_rejection_is_still_one(self):
        stdout = fixture("resume-rejected.stdout")
        outcome = ExecOutcome(REJECTED_EXIT, stdout, fixture("resume-rejected.stderr"), 0.5)
        self.assertTrue(self.provider.resume_rejected(outcome, base.MODE_PLAN, {}, MISSING))

    def test_a_stream_with_partial_messages_is_not_a_rejection(self):
        """Even with the rejection's own result event after them: a message
        was being written, so the session existed."""
        chunks = stream(*[event for event in self.events if event["type"] == "stream_event"])
        stdout = chunks + fixture("resume-rejected.stdout")
        outcome = ExecOutcome(REJECTED_EXIT, stdout, "", 0.5)
        self.assertFalse(self.provider.resume_rejected(outcome, base.MODE_PLAN, {}, MISSING))

    def cut_after(self, count):
        """The stream up to the ``count``-th ``text_delta`` of its last message."""
        last_start = max(
            index
            for index, event in enumerate(self.events)
            if event["type"] == "stream_event" and event["event"]["type"] == "message_start"
        )
        kept, seen, written = self.events[:last_start], 0, []
        for event in self.events[last_start:]:
            delta = event.get("event", {}).get("delta", {})
            if delta.get("type") == "text_delta":
                if seen == count:
                    break
                seen += 1
                written.append(delta["text"])
            kept.append(event)
        self.assertEqual(seen, count)
        return stream(*kept), "".join(written)

    def test_a_run_killed_mid_message_keeps_what_it_had_written(self):
        stdout, written = self.cut_after(4)
        earlier = [
            block["text"]
            for event in self.events[: len(stdout.splitlines())]
            if event["type"] == "assistant"
            for block in event["message"]["content"]
            if block["type"] == "text"
        ]
        text, note = parse_stream_json(stdout)
        self.assertEqual(text, "\n".join([*earlier, written]))
        self.assertIn("unfinished message", note)
        outcome = ExecOutcome(0, stdout, "", 1.0, stalled=True)
        self.assertEqual(self.provider.postprocess(outcome, base.MODE_PLAN)[0], text)

    def test_an_unfinished_message_alone_is_kept(self):
        start = {"type": "stream_event", "event": {"type": "message_start", "message": {}}}
        chunks = [
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": delta}}
            for delta in ({"type": "text_delta", "text": "a"}, {"type": "text_delta", "text": "b"})
        ]
        text, note = parse_stream_json(stream(THINKING, start, *chunks))
        self.assertEqual(text, "ab")
        self.assertEqual(note, "no result event; reconstructed from an unfinished message")

    START: ClassVar[Dict[str, Any]] = {
        "type": "stream_event",
        "event": {"type": "message_start", "message": {}},
    }
    BOTH = "no result event; reconstructed from assistant messages and an unfinished message"

    @staticmethod
    def delta(kind, **fields):
        inner = {"type": "content_block_delta", "delta": {"type": kind, **fields}}
        return {"type": "stream_event", "event": inner}

    @staticmethod
    def assistant(*blocks):
        return {"type": "assistant", "message": {"content": list(blocks)}}

    def test_a_second_block_cut_off_follows_the_finished_first(self):
        events = [
            self.START,
            self.delta("text_delta", text="a"),
            self.assistant({"type": "text", "text": "a"}),
            self.delta("text_delta", text="b"),
        ]
        text, note = parse_stream_json(stream(*events))
        self.assertEqual(text, "a\nb")
        self.assertEqual(note, self.BOTH)

    def test_a_thinking_only_assistant_event_keeps_the_text_after_it(self):
        events = [
            self.START,
            self.delta("thinking_delta", thinking=""),
            self.assistant({"type": "thinking", "thinking": "", "signature": ""}),
            self.delta("text_delta", text="a"),
            self.delta("text_delta", text="b"),
        ]
        self.assertEqual(
            parse_stream_json(stream(*events)),
            ("ab", "no result event; reconstructed from an unfinished message"),
        )

    def test_an_empty_result_does_not_hide_the_unfinished_message_after_it(self):
        events = [
            self.START,
            self.delta("text_delta", text="a"),
            self.assistant({"type": "text", "text": "a"}),
            {"type": "result", "subtype": "success", "result": "", "is_error": False},
            self.START,
            self.delta("text_delta", text="b"),
        ]
        text, note = parse_stream_json(stream(*events))
        self.assertEqual(text, "a\nb")
        self.assertEqual(note, self.BOTH)

    def test_a_finished_message_is_not_repeated(self):
        """Without the result event, every message did get its assistant event."""
        events = [event for event in self.events if event["type"] != "result"]
        text, note = parse_stream_json(stream(*events))
        answers = [
            block["text"]
            for event in events
            if event["type"] == "assistant"
            for block in event["message"]["content"]
            if block["type"] == "text"
        ]
        self.assertEqual(text, "\n".join(answers))
        self.assertEqual(note, "no result event; reconstructed from assistant messages")


if __name__ == "__main__":
    unittest.main()
