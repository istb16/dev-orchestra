"""Each adapter's activity hook: which tool uses one line of its CLI's output shows."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict, List

from helpers import IsolatedCase

from orchestrator import activity
from orchestrator.providers.agy import AgyProvider
from orchestrator.providers.claude import ClaudeProvider
from orchestrator.providers.codex import CodexProvider
from orchestrator.providers.mock import MockProvider

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(kind: str, name: str) -> List[str]:
    with open(os.path.join(FIXTURES, kind, name), encoding="utf-8") as handle:
        return handle.read().splitlines(keepends=True)


def assistant(content: List[Dict[str, Any]], usage: Any = None) -> str:
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if usage is not None:
        message["usage"] = usage
    return json.dumps({"type": "assistant", "message": message})


def tool_use(name: str, arguments: Any = None) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "tool_use", "id": "toolu_1", "name": name}
    if arguments is not None:
        block["input"] = arguments
    return block


class TestClaudeHook(unittest.TestCase):
    cwd = "/sandbox/probe138"

    def setUp(self):
        self.provider = ClaudeProvider()

    def shown(self, lines, cwd=None):
        return [self.provider.activity_of(line, cwd or self.cwd) for line in lines]

    def test_the_recorded_read_only_run(self):
        acts = self.shown(fixture("claude", "partial-messages-tool.jsonl"))
        self.assertEqual([line for act in acts for line in act.lines], ["Read probe.py", "ExitPlanMode"])
        # Line 15 holds the Read; line 35 is text only, and still updates the context.
        self.assertEqual(acts[14].lines, ["Read probe.py"])
        self.assertEqual(acts[14].context_tokens, 10076)
        self.assertEqual(acts[34].lines, [])
        self.assertEqual(acts[34].context_tokens, 11319)
        # No stream_event, user, system or result line contributes anything.
        for number, act in enumerate(acts, start=1):
            if number not in (11, 15, 35, 39, 55):
                self.assertEqual(act, activity.NOTHING, number)

    def test_the_synthetic_implement_run(self):
        acts = self.shown(fixture("claude", "implement-tools.jsonl"), "/sandbox/probe215")
        self.assertEqual(
            [line for act in acts for line in act.lines],
            ["Bash: git status", "Write hello.txt", "Edit hello.txt", "Read hello.txt"],
        )
        self.assertEqual([act.context_tokens for act in acts if act.context_tokens][-1], 13558)
        self.assertEqual(acts[8].lines, [])
        self.assertEqual(acts[8].context_tokens, 13378)

    def test_model_text_and_tool_input_never_appear(self):
        lines = fixture("claude", "partial-messages-tool.jsonl") + fixture("claude", "implement-tools.jsonl")
        shown = " ".join(line for act in self.shown(lines) for line in act.lines)
        for secret in ("No changes needed", "first line", "working tree status", "greeting", "now says"):
            self.assertNotIn(secret, shown)

    def test_several_blocks_give_one_line_each_in_order(self):
        read = tool_use("Read", {"file_path": "/sandbox/probe138/a.py"})
        act = self.provider.activity_of(assistant([read, tool_use("Glob", {"pattern": "*.md"})]), self.cwd)
        self.assertEqual(act.lines, ["Read a.py", "Glob *.md"])

    def test_text_that_mentions_tool_use_is_not_a_tool(self):
        line = assistant([{"type": "text", "text": 'a "tool_use" block with "name": "Bash"'}])
        self.assertEqual(self.provider.activity_of(line, self.cwd).lines, [])

    def test_odd_inputs_give_the_name_only(self):
        for arguments in ("a.py", ["a.py"], None):
            line = assistant([tool_use("Read", arguments)])
            self.assertEqual(self.provider.activity_of(line, self.cwd).lines, ["Read"], arguments)
        line = assistant([tool_use("SomethingNew", {"notes": "a long paragraph of the model's reasoning"})])
        self.assertEqual(self.provider.activity_of(line, self.cwd).lines, ["SomethingNew"])

    def test_the_allowlist_through_the_hook(self):
        cases = [
            (tool_use("Task", {"description": "look for keys", "prompt": "..."}), "Task"),
            (tool_use("Grep", {"pattern": "password", "path": "/sandbox/probe138/src"}), "Grep src"),
            (tool_use("Bash", {"command": "git status\nrm -rf /", "description": "x"}), "Bash: git status"),
            (tool_use("Bash", {"command": "FOO=secret python -c 'x'"}), "Bash: python"),
            (tool_use("Bash", {"command": '"/usr/bin/git" log'}), "Bash: git log"),
            (tool_use("Bash", {"command": "npm Test"}), "Bash: npm"),
            (tool_use("Bash", {"command": "echo password"}), "Bash: echo"),
            (
                tool_use("PowerShell", {"command": "$env:DB_PASSWORD='hunter2xyz'; git status"}),
                "Bash: git status",
            ),
            (tool_use("WebFetch", {"url": "https://u:p@h/x?q#f", "prompt": "x"}), "WebFetch https://h"),
            (tool_use("Glob", {"pattern": "/" + "home/u/secrets/*"}), "Glob"),
            (tool_use("mcp__github__get_issue", {"number": 1}), "github.get_issue"),
            (tool_use("Read", {"file_path": "/etc/passwd"}), "Read <outside>/passwd"),
        ]
        for block, expected in cases:
            self.assertEqual(self.provider.activity_of(assistant([block]), self.cwd).lines, [expected])

    def test_lines_that_are_not_assistant_events(self):
        torn = '{"type": "assistant"'
        for line in ("", "not json", '{"type": "user", "x": "assistant"}', '["assistant"]', torn):
            self.assertEqual(self.provider.activity_of(line, self.cwd), activity.NOTHING, line)

    def test_usage_without_all_three_counts_is_no_context(self):
        line = assistant([], {"input_tokens": 2, "cache_read_input_tokens": 5})
        self.assertIsNone(self.provider.activity_of(line, self.cwd).context_tokens)


class TestCodexHook(unittest.TestCase):
    def setUp(self):
        self.provider = CodexProvider()

    def lines(self, name, cwd):
        acts = [self.provider.activity_of(line, cwd) for line in fixture("codex", name)]
        self.assertTrue(all(act.context_tokens is None for act in acts))
        return [line for act in acts for line in act.lines]

    def test_commands_once_each_unwrapped(self):
        shown = self.lines("exec-json-tools.jsonl", "/sandbox/probe213")
        self.assertEqual(shown, ["Bash: git status", "Bash: Get-Content"])

    def test_a_file_change(self):
        shown = self.lines("exec-json-file-change.jsonl", "/sandbox/probe214")
        self.assertEqual(shown, ["Bash: Set-Content", "Edit notes/hello.txt"])
        shown = self.lines("exec-json-file-change.jsonl", "/elsewhere")
        self.assertEqual(shown, ["Bash: Set-Content", "Edit <outside>/hello.txt"])

    def test_model_text_and_output_never_appear(self):
        shown = " ".join(self.lines("exec-json-tools.jsonl", "/sandbox/probe213"))
        self.assertNotIn("README.md holds", shown)
        self.assertNotIn("nothing to commit", shown)

    def started(self, item):
        return json.dumps({"type": "item.started", "item": item})

    def test_mcp_web_search_and_the_rest(self):
        mcp = {"type": "mcp_tool_call", "server": "github", "tool": "get_issue", "arguments": {"q": 1}}
        changes = [{"path": "/sandbox/x/a.py"}, "junk", {"kind": "add"}]
        cases = [
            (mcp, ["github.get_issue"]),
            ({"type": "mcp_tool_call", "server": 3}, ["mcp"]),
            ({"type": "web_search", "query": "how to leak a key"}, ["web_search"]),
            ({"type": "command_execution", "command": ["git", "status"]}, ["Bash"]),
            ({"type": "command_execution"}, ["Bash"]),
            ({"type": "file_change", "changes": changes}, ["Edit a.py", "Edit", "Edit"]),
            ({"type": "file_change", "changes": "a.py"}, []),
            ({"type": "reasoning", "text": "thinking aloud"}, []),
            ({"type": "agent_message", "text": "item.started"}, []),
        ]
        for item, expected in cases:
            act = self.provider.activity_of(self.started(item), "/sandbox/x")
            self.assertEqual(list(act.lines), expected, item)

    def test_completed_items_and_other_lines_show_nothing(self):
        for line in fixture("codex", "exec-json-tools.jsonl"):
            if '"item.started"' not in line:
                self.assertEqual(self.provider.activity_of(line, "/sandbox/probe213"), activity.NOTHING)
        self.assertEqual(self.provider.activity_of('{"type": "item.started"', "/x"), activity.NOTHING)
        self.assertEqual(self.provider.activity_of('["item.started"]', "/x"), activity.NOTHING)


def agy_lines(name: str, raw: bool = False) -> List[str]:
    """A recording; unless ``raw``, its sandbox rewritten to ``/sandbox/agy193/``."""
    lines = fixture("agy", name)
    if raw:
        return lines
    return [line.replace("C:\\\\sandbox\\\\agy193\\\\", "/sandbox/agy193/") for line in lines]


def agy_tool(name: Any = None, parameters: Any = None, state: str = "ACTIVE") -> str:
    """One tool step; no ``tool_name`` when ``name`` is None."""
    step: Dict[str, Any] = {"step_index": 2, "state": state, "step_type": "tool"}
    if name is not None:
        step["tool_name"] = name
    if parameters is not None:
        step["tool_info"] = {"name": name, "parameters": parameters}
    return json.dumps({"event": "step_update", "step_update": step})


class TestAgyHook(unittest.TestCase):
    cwd = "/sandbox/agy193"

    def setUp(self):
        self.provider = AgyProvider()

    def shown(self, lines, cwd=None):
        return [self.provider.activity_of(line, cwd or self.cwd) for line in lines]

    def line_of(self, step_line):
        return list(self.provider.activity_of(step_line, self.cwd).lines)

    def test_the_recorded_tool_run(self):
        acts = self.shown(agy_lines("stream-json-tools.jsonl"))
        self.assertEqual([line for act in acts for line in act.lines], ["Read input.txt", "Write output.txt"])
        # Lines 5 and 8 end those steps and repeat them: nothing.
        self.assertEqual(acts[4], activity.NOTHING)
        self.assertEqual(acts[7], activity.NOTHING)

    def test_the_recorded_denied_run(self):
        acts = self.shown(agy_lines("stream-json-denied.jsonl"))
        self.assertEqual([line for act in acts for line in act.lines], ["Bash"])
        self.assertEqual(acts[4], activity.NOTHING)

    def test_model_text_and_tool_output_never_appear(self):
        lines = agy_lines("stream-json-tools.jsonl") + agy_lines("stream-json-denied.jsonl")
        shown = " ".join(line for act in self.shown(lines) for line in act.lines)
        for secret in ("reverse ord", "gamma", "4 lines, 17 bytes", "Get-Content input.txt", "Count"):
            self.assertNotIn(secret, shown)

    def test_the_parameter_mapping(self):
        replace = agy_tool("replace_file_content", {"TargetFile": "/sandbox/agy193/a.py"})
        cases = [
            (agy_tool("view_file", {"AbsolutePath": "/etc/passwd"}), ["Read <outside>/passwd"]),
            (agy_tool("run_command", {"CommandLine": "git status && rm -rf /"}), ["Bash: git status"]),
            (replace, ["replace_file_content"]),
            (agy_tool("view_file", "x"), ["Read"]),
            (agy_tool("view_file", ["/sandbox/agy193/a.py"]), ["Read"]),
        ]
        for line, expected in cases:
            self.assertEqual(self.line_of(line), expected, line)
        text = {"step_index": 5, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "tool"}
        line = json.dumps({"event": "step_update", "step_update": text})
        self.assertEqual(self.provider.activity_of(line, self.cwd), activity.NOTHING)

    def test_powershell_commands(self):
        """A quoted value holding ``; `` does not end a ``$`` assignment
        early, so no word of it shows as the program."""
        cases = {
            "$env:TOKEN='ab; hunter2 x'; npm test": "Bash: npm test",
            "$env:K='s'; npm test": "Bash: npm test",
            "$t = 'abc def'\nnpm test": "Bash",
            "& 'C:\\x\\tool.exe' --token abc": "Bash",
        }
        shown = []
        for command, expected in cases.items():
            lines = self.line_of(agy_tool("run_command", {"CommandLine": command}))
            self.assertEqual(lines, [expected], command)
            shown += lines
        lines = self.line_of(agy_tool("run_command", {"CommandLine": ".\\run.ps1 s3cr3t"}))
        self.assertEqual(lines, ["Bash: run.ps1"])
        shown += lines
        for secret in ("hunter2", "abc", "s3cr3t"):
            self.assertFalse(any(secret in line for line in shown), secret)

    def test_unmapped_names_must_be_plain(self):
        cases = [
            ("grep_search", ["grep_search"]),
            ("mcp__github__get_issue", ["github.get_issue"]),
            ("say hunter2 now", ["tool"]),
            ("x" * 65, ["tool"]),
            ("1abc", ["tool"]),
        ]
        for name, expected in cases:
            self.assertEqual(self.line_of(agy_tool(name, {"Query": "secret"})), expected, name)
        for line in (agy_tool(""), agy_tool(None)):
            self.assertEqual(self.provider.activity_of(line, self.cwd), activity.NOTHING, line)

    def test_usage_without_both_counts_is_no_context(self):
        step = {"step_index": 1, "state": "DONE", "step_type": "agent_response", "usage": {"input_tokens": 5}}
        line = json.dumps({"event": "step_update", "step_update": step})
        self.assertEqual(self.provider.activity_of(line, self.cwd), activity.NOTHING)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_windows_paths(self):
        acts = self.shown(agy_lines("stream-json-tools.jsonl", raw=True), "C:\\sandbox\\agy193")
        self.assertEqual([line for act in acts for line in act.lines], ["Read input.txt", "Write output.txt"])


class TestOtherAdapters(IsolatedCase):
    def test_agy_and_the_base_show_nothing(self):
        """agy shows nothing for a line of another CLI."""
        line = assistant([tool_use("Read", {"file_path": "/x/a.py"})])
        self.assertEqual(AgyProvider().activity_of(line, "/x"), activity.NOTHING)
        self.assertEqual(MockProvider().activity_of(line, "/x"), activity.NOTHING)

    def test_mock_reports_its_lines_before_failing(self):
        os.environ["DEV_ORCHESTRA_MOCK_ACTIVITY"] = "Read a.py|rev-a=>Read only-a.py|rev-b=>Read only-b.py"
        os.environ["DEV_ORCHESTRA_MOCK_FAIL"] = "rev-a"
        seen: List[str] = []
        with activity.recording(activity.Sink(echo=seen.append)):
            result = MockProvider().run("review for rev-a", "review", self.project)
        self.assertFalse(result.ok)
        self.assertEqual(seen, ["Read a.py", "Read only-a.py"])

    def test_mock_without_a_sink_or_the_variable_reports_nothing(self):
        os.environ["DEV_ORCHESTRA_MOCK_ACTIVITY"] = "Read a.py"
        self.assertTrue(MockProvider().run("prompt", "review", self.project).ok)
        os.environ.pop("DEV_ORCHESTRA_MOCK_ACTIVITY")
        seen: List[str] = []
        with activity.recording(activity.Sink(echo=seen.append)):
            MockProvider().run("prompt", "review", self.project)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
