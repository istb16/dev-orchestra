"""The reply-language hooks in the user's Claude Code settings (#254).

Every test runs against a temporary ``CLAUDE_CONFIG_DIR`` and config home
that ``IsolatedCase`` sets up; nothing here reads or writes the real
``~/.claude`` or the real dev-orchestra config directory.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional
from unittest import mock

from helpers import REPO_ROOT, IsolatedCase, make_dir_link

from orchestrator import claude_hooks as ch
from orchestrator import cli, cli_hooks, doctor, hosts
from orchestrator import config as config_mod
from orchestrator.execution import DELEGATED_ENV

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "reply_language")
SESSION = "5f0c3a52-0000-4000-8000-000000000256"
#: The environment of a command run from Claude Code's Bash tool.
IN_CLAUDE_CODE = {ch.SESSION_ENV: SESSION}


def run_cli(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def slashes(path: str) -> str:
    return path.replace("\\", "/")


def english_reply() -> str:
    with open(os.path.join(FIXTURES, "en_reply.md"), encoding="utf-8") as handle:
        return handle.read()


class HooksCase(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = os.path.join(self.claude_dir, "settings.json")

    def write_settings(self, data: Any) -> bytes:
        """``data`` as JSON, or as written when it is bytes; returns the bytes."""
        raw = data if isinstance(data, bytes) else (json.dumps(data, indent=4) + "\n").encode("utf-8")
        os.makedirs(self.claude_dir, exist_ok=True)
        with open(self.settings, "wb") as handle:
            handle.write(raw)
        return raw

    def read_bytes(self, path: Optional[str] = None) -> bytes:
        with open(path or self.settings, "rb") as handle:
            return handle.read()

    def read_settings(self) -> Dict[str, Any]:
        return json.loads(self.read_bytes().decode("utf-8"))

    def ours(self, event: str) -> List[Dict[str, Any]]:
        groups = self.read_settings()["hooks"][event]
        return [hook for group in groups for hook in group["hooks"] if ch.is_ours(hook)]

    def desired(self) -> Dict[str, Dict[str, Any]]:
        return ch.desired_entries(ch.python_command(), slashes(ch.relay_path()))

    def set_global_language(self, tag: str, *flags: str) -> str:
        code, out, err = run_cli("config", "set", "language.reply", tag, "--scope", "global", *flags)
        self.assertEqual(code, 0, err)
        return out


class TestSettingsEditing(HooksCase):
    def test_install_into_missing_file_creates_it(self):
        change = ch.install(REPO_ROOT)
        self.assertTrue(change.changed)
        self.assertIsNone(change.backup)
        expected = {event: [group] for event, group in self.desired().items()}
        self.assertEqual(self.read_settings(), {"hooks": expected})
        self.assertEqual(len(change.added), 3)
        self.assertTrue(os.path.isfile(ch.relay_path()))
        with open(ch.record_path(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), ch.desired_record(REPO_ROOT))

    def test_install_preserves_other_keys_and_user_hooks(self):
        user_stop = {"hooks": [{"type": "command", "command": "notify-send done"}]}
        user_tool = {"matcher": "Bash", "hooks": [{"type": "command", "command": "audit"}]}
        self.write_settings(
            {"model": "opus", "hooks": {"Stop": [user_stop], "PreToolUse": [user_tool]}, "env": {"A": "1"}}
        )
        ch.install(REPO_ROOT)
        data = self.read_settings()
        self.assertEqual(list(data), ["model", "hooks", "env"])
        self.assertEqual(list(data["hooks"]), ["Stop", "PreToolUse", "UserPromptSubmit", "SessionStart"])
        self.assertEqual(data["hooks"]["Stop"], [user_stop, self.desired()["Stop"]])
        self.assertEqual(data["hooks"]["PreToolUse"], [user_tool])
        self.assertEqual(data["env"], {"A": "1"})

    def test_install_is_idempotent_no_write(self):
        self.write_settings({"theme": "dark"})
        ch.install(REPO_ROOT)
        written = self.read_bytes()
        os.remove(self.settings + ch.BACKUP_SUFFIX)
        change = ch.install(REPO_ROOT)
        self.assertFalse(change.changed)
        self.assertEqual((change.added, change.removed, change.relay_files), ([], [], []))
        self.assertEqual(self.read_bytes(), written)
        self.assertFalse(os.path.exists(self.settings + ch.BACKUP_SUFFIX))
        code, out, _ = run_cli("hooks", "install")
        self.assertEqual(code, 0)
        self.assertIn("Claude Code hooks already installed in %s" % self.settings, out)

    def test_install_replaces_stale_entries_from_old_home(self):
        ch.install(REPO_ROOT)
        old_relay = slashes(ch.relay_path())
        os.environ["DEV_ORCHESTRA_HOME"] = os.path.join(self.tmp, "moved")
        change = ch.install(REPO_ROOT)
        self.assertTrue(change.changed)
        for event in ("UserPromptSubmit", "SessionStart", "Stop"):
            hooks = self.ours(event)
            self.assertEqual(len(hooks), 1, event)
            self.assertEqual(hooks[0]["args"][1], slashes(ch.relay_path()))
            self.assertEqual(len(self.read_settings()["hooks"][event]), 1, event)
        self.assertTrue(all(old_relay in entry for entry in change.removed), change.removed)
        self.assertEqual(len(change.removed), 3)

    def test_uninstall_removes_only_ours_and_empty_containers(self):
        user_hook = {"type": "command", "command": "notify-send done"}
        old_relay = "C:\\Old\\Hooks\\Dev_Orchestra_Hook.py"
        stale = {"type": "command", "command": "py", "args": ["-I", old_relay, "stop"]}
        self.write_settings(
            {"x": 1, "hooks": {"Stop": [{"hooks": [user_hook, stale]}], "Other": [{"hooks": []}]}}
        )
        ch.install(REPO_ROOT)
        change = ch.uninstall()
        self.assertTrue(change.changed)
        expected = {"x": 1, "hooks": {"Stop": [{"hooks": [user_hook]}], "Other": [{"hooks": []}]}}
        self.assertEqual(self.read_settings(), expected)
        self.assertFalse(os.path.lexists(ch.relay_path()))
        self.assertFalse(os.path.lexists(ch.record_path()))
        self.assertFalse(os.path.lexists(ch.hooks_dir()))
        self.write_settings({"x": 1})
        ch.install(REPO_ROOT)
        ch.uninstall()
        self.assertEqual(self.read_settings(), {"x": 1})
        change = ch.uninstall()
        self.assertFalse(change.changed)

    def test_refuses_unparseable_settings_and_writes_nothing(self):
        raw = self.write_settings(b"{not json")
        with self.assertRaises(ch.HooksError) as caught:
            ch.install(REPO_ROOT)
        self.assertIn(self.settings, str(caught.exception))
        self.assertIn("is not valid JSON", str(caught.exception))
        self.assertEqual(self.read_bytes(), raw)
        self.assertFalse(os.path.lexists(ch.relay_path()))
        self.assertEqual(os.listdir(self.claude_dir), ["settings.json"])
        code, _, err = run_cli("hooks", "install")
        self.assertEqual(code, 2)
        self.assertIn("it was not changed", err)
        self.assertEqual(run_cli("hooks", "uninstall")[0], 2)

    def test_refuses_non_object_hooks_or_event(self):
        for label, data, problem in (
            ("a list", [], "does not hold a JSON object"),
            ("hooks a list", {"hooks": []}, "has a hooks value that is not an object"),
            ("an event an object", {"hooks": {"Stop": {}}}, "has a hooks.Stop value that is not a list"),
            ("a group a string", {"hooks": {"Stop": ["x"]}}, "has a hooks.Stop entry that is not an object"),
        ):
            raw = self.write_settings(data)
            with self.assertRaises(ch.HooksError, msg=label) as caught:
                ch.install(REPO_ROOT)
            self.assertIn(problem, str(caught.exception), label)
            self.assertEqual(self.read_bytes(), raw, label)

    def test_bom_settings_read(self):
        raw = b"\xef\xbb\xbf" + json.dumps({"a": "\u00e9"}).encode("utf-8")
        self.write_settings(raw)
        change = ch.install(REPO_ROOT)
        data = self.read_settings()
        self.assertEqual(data["a"], "\u00e9")
        self.assertIn("hooks", data)
        self.assertEqual(self.read_bytes(change.backup), raw)
        self.assertIn('"a": "\u00e9"', self.read_bytes().decode("utf-8"))  # ensure_ascii=False

    def test_backup_written_with_original_bytes(self):
        raw = self.write_settings(b'{ "theme":"dark" ,\n\n  "x": [1,2] }')
        change = ch.install(REPO_ROOT)
        self.assertEqual(change.backup, self.settings + ch.BACKUP_SUFFIX)
        self.assertEqual(self.read_bytes(change.backup), raw)
        installed = self.read_bytes()
        self.assertTrue(installed.endswith(b"}\n"))
        change = ch.uninstall()
        self.assertEqual(self.read_bytes(change.backup), installed)
        code, out, _ = run_cli("hooks", "install")
        self.assertEqual(code, 0)
        self.assertIn("previous file kept as %s" % (self.settings + ch.BACKUP_SUFFIX), out)

    def test_atomic_write_leaves_no_temp_file(self):
        ch.install(REPO_ROOT)
        self.assertEqual(os.listdir(self.claude_dir), ["settings.json"])
        ch.uninstall()
        backup = "settings.json" + ch.BACKUP_SUFFIX
        self.assertEqual(sorted(os.listdir(self.claude_dir)), ["settings.json", backup])
        ch.install(REPO_ROOT)
        self.assertEqual(sorted(os.listdir(ch.hooks_dir())), sorted([ch.RECORD_NAME, ch.RELAY_NAME]))

    def test_race_rereads_once(self):
        self.write_settings({"a": 1})
        original = ch.merged
        calls: List[int] = []

        def another_writer_once(settings, install, python="", relay=""):
            calls.append(1)
            if len(calls) == 1:
                self.write_settings({"a": 1, "theme": "dark"})
            return original(settings, install, python, relay)

        with mock.patch.object(ch, "merged", another_writer_once):
            ch.install(REPO_ROOT)
        self.assertEqual(len(calls), 2)
        data = self.read_settings()
        self.assertEqual(data["theme"], "dark")
        self.assertIn("hooks", data)

        def another_writer_always(settings, install, python="", relay=""):
            calls.append(1)
            self.write_settings({"a": len(calls)})
            return original(settings, install, python, relay)

        self.write_settings({"a": 0})
        with mock.patch.object(ch, "merged", another_writer_always):
            with self.assertRaises(ch.HooksError) as caught:
                ch.install(REPO_ROOT)
        self.assertIn("changed while it was being edited", str(caught.exception))
        self.assertNotIn("hooks", self.read_settings())  # the other writer's file, untouched
        self.assertEqual([name for name in os.listdir(self.claude_dir) if name.endswith(".tmp")], [])

    def test_dry_run_writes_nothing(self):
        raw = self.write_settings({"a": 1})
        change = ch.install(REPO_ROOT, dry_run=True)
        self.assertTrue(change.changed)
        self.assertEqual(len(change.added), 3)
        self.assertEqual(len(change.relay_files), 2)
        self.assertEqual(self.read_bytes(), raw)
        self.assertFalse(os.path.lexists(ch.hooks_dir()))
        self.assertEqual(os.listdir(self.claude_dir), ["settings.json"])
        code, out, _ = run_cli("hooks", "install", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Would install the reply-language hooks in %s" % self.settings, out)
        self.assertIn("  + Stop: %s -I %s stop" % (ch.python_command(), slashes(ch.relay_path())), out)
        self.assertIn("would be kept as %s" % (self.settings + ch.BACKUP_SUFFIX), out)
        self.assertNotIn(cli_hooks.RESTART_NOTE, out)
        self.assertEqual(self.read_bytes(), raw)
        ch.install(REPO_ROOT)
        installed = self.read_bytes()
        code, out, _ = run_cli("hooks", "uninstall", "--dry-run")
        self.assertIn("Would remove the reply-language hooks from %s" % self.settings, out)
        self.assertEqual(self.read_bytes(), installed)
        self.assertTrue(os.path.isfile(ch.relay_path()))

    def test_exec_form_shape_and_forward_slashes(self):
        ch.install(REPO_ROOT)
        data = self.read_settings()
        for event, matcher, argument in ch.HOOK_EVENTS:
            group = data["hooks"][event][-1]
            self.assertEqual(group.get("matcher"), matcher, event)
            (hook,) = group["hooks"]
            self.assertEqual(sorted(hook), ["args", "command", "timeout", "type"])
            self.assertEqual(hook["type"], "command")
            self.assertEqual(hook["timeout"], ch.HOOK_TIMEOUT)
            self.assertEqual(hook["command"], ch.python_command())
            self.assertEqual(hook["args"], ["-I", slashes(ch.relay_path()), argument])
            self.assertNotIn("\\", hook["command"] + "".join(hook["args"]))
        self.assertEqual(data["hooks"]["SessionStart"][-1]["matcher"], "compact|resume")

    def test_refuses_store_redirected_settings_path(self):
        package = "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0"
        real = os.path.join(self.tmp, "AppData", "Local", "Packages", package, "LocalCache", "settings.json")
        with mock.patch.object(config_mod, "stored_elsewhere", lambda path: real):
            with self.assertRaises(ch.HooksError) as caught:
                ch.install(REPO_ROOT)
            self.assertIn("Microsoft Store", str(caught.exception))
            self.assertEqual(run_cli("hooks", "install")[0], 2)
        self.assertFalse(os.path.lexists(self.claude_dir))
        self.assertFalse(os.path.lexists(ch.hooks_dir()))

    def test_a_link_the_user_made_is_not_a_store_redirect(self):
        real = os.path.join(self.tmp, "dotfiles", "claude", "settings.json")
        with mock.patch.object(config_mod, "stored_elsewhere", lambda path: real):
            self.assertTrue(ch.install(REPO_ROOT).changed)
        self.assertIn("hooks", self.read_settings())

    def test_linked_settings_file_stays_a_link(self):
        """A dotfile manager's link: its target is rewritten, and the backup sits beside that."""
        target = os.path.join(self.tmp, "dotfiles", "settings.json")
        os.makedirs(os.path.dirname(target))
        with open(target, "w", encoding="utf-8") as handle:
            handle.write('{"theme": "dark"}')
        os.makedirs(self.claude_dir)
        try:
            os.symlink(target, self.settings)
        except (OSError, NotImplementedError):
            self.skipTest("cannot make a file symlink here")
        change = ch.install(REPO_ROOT)
        self.assertTrue(os.path.islink(self.settings))
        with open(target, encoding="utf-8") as handle:
            self.assertIn("hooks", json.load(handle))
        self.assertEqual(change.backup, os.path.realpath(target) + ch.BACKUP_SUFFIX)
        self.assertEqual(self.read_bytes(change.backup), b'{"theme": "dark"}')
        self.assertEqual(os.listdir(self.claude_dir), ["settings.json"])

    def test_refuses_null_hooks_and_bad_groups(self):
        for label, data, problem in (
            ("hooks null", {"hooks": None}, "has a hooks value that is not an object"),
            ("group without hooks", {"hooks": {"Stop": [{"matcher": ""}]}}, "whose hooks is not a list"),
            ("group hooks an object", {"hooks": {"Stop": [{"hooks": {}}]}}, "whose hooks is not a list"),
            ("a hook a string", {"hooks": {"Stop": [{"hooks": ["x"]}]}}, "whose hooks is not a list"),
        ):
            raw = self.write_settings(data)
            with self.assertRaises(ch.HooksError, msg=label) as caught:
                ch.install(REPO_ROOT)
            self.assertIn(problem, str(caught.exception), label)
            self.assertEqual(self.read_bytes(), raw, label)
            self.assertFalse(os.path.lexists(ch.relay_path()), label)

    def test_user_hooks_that_merely_look_like_ours_survive(self):
        audit = os.path.join(self.tmp, "audit", "hooks", ch.RELAY_NAME)
        os.makedirs(os.path.dirname(audit))
        with open(audit, "w", encoding="utf-8") as handle:
            handle.write("# my own audit hook\n")
        user_hooks = [
            # Same name, outside a hooks/ directory.
            {"type": "command", "command": "python3", "args": ["/srv/audit/%s" % ch.RELAY_NAME, "stop"]},
            # Named in the command string only.
            {"type": "command", "command": "python3 ~/.config/x/hooks/%s stop" % ch.RELAY_NAME},
            # Not a command hook.
            {"type": "prompt", "args": ["-I", slashes(ch.relay_path()), "stop"]},
            # Our exact shape, but the file holds something else.
            {"type": "command", "command": "python3", "args": ["-I", slashes(audit), "stop"]},
            # Our shape with an argument the relay never takes.
            {"type": "command", "command": "python3", "args": ["-I", slashes(ch.relay_path()), "audit"]},
        ]
        raw = self.write_settings({"hooks": {"Stop": [{"hooks": user_hooks}]}})
        self.assertFalse(any(ch.is_ours(hook) for hook in user_hooks))
        ch.install(REPO_ROOT)
        self.assertEqual(self.read_settings()["hooks"]["Stop"][0], {"hooks": user_hooks})
        ch.uninstall()
        self.assertEqual(self.read_settings(), json.loads(raw.decode("utf-8")))

    def test_failed_write_reports_and_leaves_the_file(self):
        raw = self.write_settings({"a": 1})
        real_replace = os.replace

        def failing(src, dst):
            if os.path.abspath(dst) == os.path.abspath(self.settings):
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch.object(os, "replace", failing):
            with self.assertRaises(ch.HooksError) as caught:
                ch.install(REPO_ROOT)
        self.assertIn("%s could not be written (disk full)" % self.settings, str(caught.exception))
        self.assertEqual(self.read_bytes(), raw)
        self.assertEqual([name for name in os.listdir(self.claude_dir) if name.endswith(".tmp")], [])
        # The backup was written before the main write failed, and is the original.
        self.assertEqual(self.read_bytes(self.settings + ch.BACKUP_SUFFIX), raw)
        # The relay is not left behind for entries that were never added.
        self.assertFalse(os.path.lexists(ch.hooks_dir()))

    def test_failed_install_puts_an_older_relay_back(self):
        old = os.path.join(self.tmp, "cache", "0.20.0")
        ch.write_relay(old)
        with open(ch.relay_path(), "wb") as handle:
            handle.write(b"# relay-version: 0\n")
        before = {path: self.read_bytes(path) for path in (ch.relay_path(), ch.record_path())}
        self.write_settings(b"{}")
        with mock.patch.object(ch, "write_settings", side_effect=OSError("read-only")):
            with self.assertRaises(ch.HooksError):
                ch.install(REPO_ROOT)
        self.assertEqual({path: self.read_bytes(path) for path in before}, before)

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_rewrite_keeps_the_file_mode(self):
        self.write_settings({"a": 1})
        os.chmod(self.settings, 0o600)
        ch.install(REPO_ROOT)
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o600)

    def test_claude_config_dir_honoured(self):
        self.assertEqual(ch.settings_path(), os.path.join(self.claude_dir, "settings.json"))
        other = os.path.join(self.tmp, "other-claude")
        os.environ[ch.CLAUDE_CONFIG_ENV] = other
        self.assertEqual(ch.settings_path(), os.path.join(other, "settings.json"))
        del os.environ[ch.CLAUDE_CONFIG_ENV]
        home = os.path.expanduser("~")
        self.assertEqual(ch.settings_path(), os.path.join(home, ".claude", "settings.json"))


class TestPythonChoice(HooksCase):
    def setUp(self) -> None:
        super().setUp()
        # Outside a virtual environment unless a test says otherwise, whatever runs the suite.
        patcher = mock.patch.object(sys, "prefix", sys.base_prefix)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_file(self, *parts: str) -> str:
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("")
        return path

    def in_venv(self, base_executable: Optional[str]):
        venv = os.path.join(self.tmp, "project-venv")
        patches = [
            mock.patch.object(sys, "prefix", venv),
            mock.patch.object(sys, "base_prefix", os.path.join(self.tmp, "base")),
            mock.patch.object(sys, "executable", os.path.join(venv, "bin", "python")),
            mock.patch.object(sys, "_base_executable", base_executable, create=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_virtual_environment_runs_its_base_python(self):
        """The hooks are global: a project's environment, and its .pth files, are not used."""
        base = self.make_file("base", "python-base")
        self.in_venv(base)
        self.assertTrue(ch.in_virtual_environment())
        self.assertEqual(ch.python_command(), slashes(base))
        ch.install(REPO_ROOT)
        self.assertEqual(self.ours("Stop")[0]["command"], slashes(base))
        self.assertEqual(ch.status(REPO_ROOT)["status"], "installed")

    def test_base_python_found_under_base_prefix(self):
        name = "python.exe" if config_mod.on_windows() else os.path.join("bin", "python3")
        base = self.make_file("base", name)
        self.in_venv(None)
        self.assertEqual(ch.python_command(), slashes(base))

    def test_no_base_python_refuses_and_writes_nothing(self):
        self.in_venv(os.path.join(self.tmp, "project-venv", "bin", "python"))
        with self.assertRaises(ch.HooksError) as caught:
            ch.install(REPO_ROOT)
        self.assertIn("virtual environment", str(caught.exception))
        self.assertFalse(os.path.lexists(self.claude_dir))
        self.assertFalse(os.path.lexists(ch.hooks_dir()))
        self.assertEqual(ch.status(REPO_ROOT)["status"], "not-installed")

    def test_python_command_not_realpath_resolved(self):
        real = os.path.join(self.tmp, "real-bin")
        os.makedirs(real)
        with open(os.path.join(real, "python.exe"), "w", encoding="utf-8") as handle:
            handle.write("")
        link = os.path.join(self.tmp, "linked-bin")
        try:
            make_dir_link(link, real)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("cannot make a directory link here")
        executable = os.path.join(link, "python.exe")
        with mock.patch.object(sys, "executable", executable):
            self.assertEqual(ch.python_command(), slashes(executable))
        self.assertNotEqual(slashes(os.path.realpath(executable)), slashes(executable))

    def test_store_alias_path_kept(self):
        alias = os.path.join(
            self.tmp,
            "AppData",
            "Local",
            "Microsoft",
            "WindowsApps",
            "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0",
            "python.exe",
        )
        with mock.patch.object(sys, "executable", alias):
            self.assertEqual(ch.python_command(), slashes(alias))
        relative = os.path.join("venv", "..", "venv", "python")
        with mock.patch.object(sys, "executable", relative):
            self.assertEqual(ch.python_command(), slashes(os.path.abspath(relative)))


class TestRelay(HooksCase):
    def setUp(self) -> None:
        super().setUp()
        os.makedirs(os.path.join(self.project, ".git"))

    def run_relay(self, event: str, payload: Dict[str, Any], env: Optional[Dict[str, str]] = None):
        environ = dict(os.environ)
        environ.pop(DELEGATED_ENV, None)
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, "-I", ch.relay_path(), event],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            cwd=self.project,
            env=environ,
            timeout=60,
        )

    def prompt_payload(self) -> Dict[str, Any]:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": SESSION,
            "cwd": self.project,
            "prompt": "/dev-orchestra:dev-orchestra go",
        }

    def write_project_language(self, tag: str) -> None:
        path = os.path.join(self.project, ".dev-orchestra.yaml")
        config_mod.write_config_file(path, {"version": 1, "language": {"reply": tag}}, "project")

    def write_record(self, record: Any) -> None:
        with open(ch.record_path(), "w", encoding="utf-8") as handle:
            handle.write(record if isinstance(record, str) else json.dumps(record))

    def test_relay_runs_hook_and_prints_block(self):
        self.write_project_language("ja")
        ch.write_relay(REPO_ROOT)
        transcript = os.path.join(self.tmp, "session.jsonl")
        skill = {"type": "tool_use", "id": "t", "name": "Skill", "input": {"skill": "dev-orchestra"}}
        with open(transcript, "w", encoding="utf-8", newline="\n") as handle:
            for entry in (
                {"type": "user", "message": {"role": "user", "content": "go"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [skill]}},
            ):
                handle.write(json.dumps(entry) + "\n")
        payload = {
            "hook_event_name": "Stop",
            "session_id": SESSION,
            "transcript_path": transcript,
            "cwd": self.project,
            "last_assistant_message": english_reply(),
        }
        result = self.run_relay("stop", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")
        result = self.run_relay("prompt", self.prompt_payload())
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertIn("language.reply: ja.", output["additionalContext"])

    def test_relay_silent_without_record(self):
        self.write_project_language("ja")
        ch.write_relay(REPO_ROOT)
        os.remove(ch.record_path())
        result = self.run_relay("prompt", self.prompt_payload())
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_relay_silent_when_plugin_root_missing(self):
        self.write_project_language("ja")
        ch.write_relay(REPO_ROOT)
        self.write_record(dict(ch.desired_record(REPO_ROOT), plugin_root=os.path.join(self.tmp, "gone")))
        result = self.run_relay("prompt", self.prompt_payload())
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_relay_silent_on_bad_record_json(self):
        self.write_project_language("ja")
        ch.write_relay(REPO_ROOT)
        for record in ("{not json", "[]", json.dumps({"plugin_root": REPO_ROOT})):
            self.write_record(record)
            result = self.run_relay("prompt", self.prompt_payload())
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), record)

    def test_relay_sets_home_and_config_from_record(self):
        ch.write_relay(REPO_ROOT)
        other = os.path.join(self.tmp, "other-home")
        config_mod.write_config_file(
            os.path.join(other, "config.yaml"), {"version": 1, "language": {"reply": "ko"}}, "global"
        )
        home = slashes(other)
        self.write_record(dict(ch.desired_record(REPO_ROOT), home=home, config=home + "/config.yaml"))
        # The process itself names a config home with no language at all.
        result = self.run_relay("prompt", self.prompt_payload(), {"DEV_ORCHESTRA_HOME": self.config_home})
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("language.reply: ko.", context)

    def fake_checkout(self, body: str) -> str:
        root = os.path.join(self.tmp, "fake-checkout")
        script = os.path.join(root, "scripts", "hooks", "reply_language.py")
        os.makedirs(os.path.dirname(script))
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(body)
        return root

    def test_relay_silent_when_the_hook_fails_or_exits_non_zero(self):
        for body in ("raise SystemExit(1)\n", "raise RuntimeError('boom')\n"):
            root = self.fake_checkout(body)
            ch.write_relay(root)
            result = self.run_relay("prompt", self.prompt_payload())
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), body)
            shutil.rmtree(root)

    @unittest.skipIf(os.name == "nt", "ownership and modes are POSIX")
    def test_relay_refuses_a_checkout_others_can_write(self):
        marker = os.path.join(self.tmp, "ran")
        root = self.fake_checkout("open(%r, 'w').close()\n" % marker)
        ch.write_relay(root)
        os.chmod(os.path.join(root, "scripts"), 0o777)
        self.assertEqual(self.run_relay("prompt", self.prompt_payload()).returncode, 0)
        self.assertFalse(os.path.exists(marker))
        os.chmod(os.path.join(root, "scripts"), 0o755)
        self.run_relay("prompt", self.prompt_payload())
        self.assertTrue(os.path.exists(marker))

    def test_relay_text_compiles_with_standard_library_only(self):
        tree = ast.parse(ch.RELAY_TEXT)
        compile(tree, ch.RELAY_NAME, "exec")
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, {"json", "os", "runpy", "sys"})
        self.assertLessEqual(imported, set(sys.stdlib_module_names))
        self.assertIn("relay-version: %d" % ch.RELAY_VERSION, ch.RELAY_TEXT)


class TestRefresh(HooksCase):
    def setUp(self) -> None:
        super().setUp()
        self.old = os.path.join(self.claude_dir, "plugins", "cache", "market", "dev-orchestra", "0.20.0")
        self.new = os.path.join(self.claude_dir, "plugins", "cache", "market", "dev-orchestra", "0.21.0")

    def record(self) -> Dict[str, Any]:
        with open(ch.record_path(), encoding="utf-8") as handle:
            return json.load(handle)

    def test_refresh_noop_without_relay(self):
        ch.refresh(self.new, IN_CLAUDE_CODE)
        self.assertFalse(os.path.lexists(ch.hooks_dir()))

    def test_refresh_updates_plugin_root(self):
        ch.write_relay(self.old)
        self.assertEqual(self.record()["plugin_root"], slashes(os.path.abspath(self.old)))
        ch.refresh(self.new, IN_CLAUDE_CODE)
        self.assertEqual(self.record(), ch.desired_record(self.new))

    def test_refresh_ignores_a_checkout_claude_code_did_not_install(self):
        """A fork, a branch or a clone in /tmp run once does not become every session's hook."""
        ch.write_relay(self.old)
        elsewhere = os.path.join(self.claude_dir, "elsewhere")
        for checkout in (REPO_ROOT, os.path.join(self.tmp, "clone"), elsewhere):
            ch.refresh(checkout, IN_CLAUDE_CODE)
            self.assertEqual(self.record()["plugin_root"], slashes(os.path.abspath(self.old)), checkout)

    def test_refresh_keeps_the_recorded_checkout_up_to_date(self):
        """The checkout `hooks install` recorded, outside the plugin cache, still gets a newer relay."""
        ch.write_relay(REPO_ROOT)
        with open(ch.relay_path(), "w", encoding="utf-8") as handle:
            handle.write("# relay-version: 0\n")
        ch.refresh(REPO_ROOT, IN_CLAUDE_CODE)
        self.assertEqual(self.read_bytes(ch.relay_path()), ch.RELAY_TEXT.encode("utf-8"))

    def test_refresh_keeps_the_recorded_config(self):
        """A one-off DEV_ORCHESTRA_CONFIG does not repoint the hooks."""
        ch.write_relay(self.old)
        recorded = self.record()
        os.environ["DEV_ORCHESTRA_CONFIG"] = os.path.join(self.project, "tmp.yaml")
        ch.refresh(self.new, IN_CLAUDE_CODE)
        record = self.record()
        self.assertEqual((record["home"], record["config"]), (recorded["home"], recorded["config"]))
        self.assertEqual(record["plugin_root"], slashes(os.path.abspath(self.new)))

    def test_refresh_rewrites_a_missing_or_corrupt_record(self):
        for damage in ("missing", "{trunc", "[]"):
            ch.write_relay(self.old)
            if damage == "missing":
                os.remove(ch.record_path())
            else:
                with open(ch.record_path(), "w", encoding="utf-8") as handle:
                    handle.write(damage)
            ch.refresh(self.new, IN_CLAUDE_CODE)
            self.assertEqual(self.record(), ch.desired_record(self.new), damage)

    def test_refresh_skipped_outside_claude_code(self):
        ch.write_relay(self.old)
        ch.refresh(self.new, {})
        self.assertEqual(self.record()["plugin_root"], slashes(os.path.abspath(self.old)))

    def test_refresh_skipped_when_delegated(self):
        ch.write_relay(self.old)
        ch.refresh(self.new, {**IN_CLAUDE_CODE, DELEGATED_ENV: "1"})
        self.assertEqual(self.record()["plugin_root"], slashes(os.path.abspath(self.old)))

    def test_every_command_refreshes_inside_claude_code(self):
        ch.write_relay(self.old)
        os.environ[ch.SESSION_ENV] = SESSION
        with mock.patch.object(hosts, "PLUGIN_ROOT", self.new):
            self.assertEqual(run_cli("config", "path")[0], 0)
        self.assertEqual(self.record(), ch.desired_record(self.new))

    def test_hooks_commands_do_not_refresh(self):
        """`hooks status` and the dry runs report what is there, and write nothing."""
        ch.write_relay(self.old)
        written = self.read_bytes(ch.record_path())
        os.environ[ch.SESSION_ENV] = SESSION
        with mock.patch.object(hosts, "PLUGIN_ROOT", self.new):
            for argv in (
                ["hooks", "status"],
                ["hooks", "install", "--dry-run"],
                ["hooks", "uninstall", "--dry-run"],
            ):
                self.assertEqual(run_cli(*argv)[0], 0, argv)
                self.assertEqual(self.read_bytes(ch.record_path()), written, argv)

    def test_refresh_errors_never_fail_command(self):
        ch.write_relay(self.old)
        with mock.patch.object(ch, "write_relay", side_effect=OSError("disk full")):
            ch.refresh(self.new, IN_CLAUDE_CODE)
        # A real OSError: the record's path is a directory, so it can be neither read nor replaced.
        os.remove(ch.record_path())
        os.makedirs(ch.record_path())
        ch.refresh(self.new, IN_CLAUDE_CODE)
        self.assertTrue(os.path.isdir(ch.record_path()))
        os.environ[ch.SESSION_ENV] = SESSION
        with mock.patch.object(ch, "refresh", side_effect=RuntimeError("boom")):
            code, out, err = run_cli("config", "path")
        self.assertEqual(code, 0, err)
        self.assertIn("global:", out)


class TestStatus(HooksCase):
    def edit_settings(self, change) -> None:
        data = self.read_settings()
        change(data)
        self.write_settings(data)

    def test_status_installed(self):
        self.assertEqual(ch.status(REPO_ROOT)["status"], "not-installed")
        ch.install(REPO_ROOT)
        report = ch.status(REPO_ROOT)
        self.assertEqual(report["status"], "installed")
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["events"], ["SessionStart", "Stop", "UserPromptSubmit"])
        self.assertEqual(report["python"], ch.python_command())
        self.assertEqual(report["plugin_root"], slashes(os.path.abspath(REPO_ROOT)))
        self.assertEqual(report["settings_path"], self.settings)
        self.assertEqual((report["hooks_disabled"], report["error"]), (False, None))

    def test_status_stale_reasons(self):
        ch.install(REPO_ROOT)
        other_python = os.path.join(self.tmp, "python-other")
        with open(other_python, "w", encoding="utf-8") as handle:
            handle.write("")

        def set_command(command: str):
            def change(data):
                for groups in data["hooks"].values():
                    for group in groups:
                        for hook in group["hooks"]:
                            hook["command"] = command

            return change

        self.edit_settings(set_command(slashes(other_python)))
        self.assertEqual(ch.status(REPO_ROOT)["reasons"], ["python-differs"])
        self.edit_settings(set_command(slashes(os.path.join(self.tmp, "gone", "python"))))
        self.assertEqual(ch.status(REPO_ROOT)["reasons"], ["python-differs", "python-missing"])
        ch.install(REPO_ROOT)
        self.assertEqual(ch.status(REPO_ROOT)["status"], "installed")
        with open(ch.relay_path(), "w", encoding="utf-8") as handle:
            handle.write("# relay-version: 0\n")
        self.assertEqual(ch.status(REPO_ROOT)["reasons"], ["relay-outdated"])
        os.remove(ch.relay_path())
        self.assertEqual(ch.status(REPO_ROOT)["reasons"], ["relay-missing"])
        ch.install(REPO_ROOT)
        report = ch.status(os.path.join(self.tmp, "another-checkout"))
        self.assertEqual((report["status"], report["reasons"]), ("stale", ["plugin-root-differs"]))
        self.edit_settings(lambda data: data["hooks"].pop("Stop"))
        self.assertEqual(ch.status(REPO_ROOT)["reasons"], ["events-missing"])

    def test_record_differs_is_not_a_plugin_root_difference(self):
        ch.install(REPO_ROOT)
        os.environ["DEV_ORCHESTRA_CONFIG"] = os.path.join(self.tmp, "elsewhere.yaml")
        self.assertEqual(ch.status(REPO_ROOT)["reasons"], ["record-differs"])
        del os.environ["DEV_ORCHESTRA_CONFIG"]
        os.remove(ch.record_path())
        self.assertEqual(ch.status(REPO_ROOT)["reasons"], ["record-differs"])

    def test_duplicated_event_is_stale(self):
        ch.install(REPO_ROOT)
        self.edit_settings(lambda data: data["hooks"]["Stop"].append(copy.deepcopy(data["hooks"]["Stop"][0])))
        report = ch.status(REPO_ROOT)
        self.assertEqual((report["status"], report["reasons"]), ("stale", ["events-missing"]))
        ch.install(REPO_ROOT)
        self.assertEqual(len(self.read_settings()["hooks"]["Stop"]), 1)
        self.assertEqual(ch.status(REPO_ROOT)["status"], "installed")

    def test_changed_matcher_timeout_or_group_is_stale_and_repaired(self):
        def matcher(data):
            data["hooks"]["SessionStart"][0]["matcher"] = "startup"

        def timeout(data):
            data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 1

        def shared_group(data):
            data["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "notify-send"})

        def foreign_event(data):
            data["hooks"]["PreToolUse"] = copy.deepcopy(data["hooks"]["Stop"])

        for change in (matcher, timeout, shared_group, foreign_event):
            ch.install(REPO_ROOT)
            self.edit_settings(change)
            report = ch.status(REPO_ROOT)
            stale = (report["status"], report["reasons"])
            self.assertEqual(stale, ("stale", ["entries-differ"]), change.__name__)
            ch.install(REPO_ROOT)
            self.assertEqual(ch.status(REPO_ROOT)["status"], "installed", change.__name__)

    def test_non_string_command_is_python_missing(self):
        ch.install(REPO_ROOT)

        def numeric(data):
            data["hooks"]["Stop"][0]["hooks"][0]["command"] = 5

        self.edit_settings(numeric)
        report = ch.status(REPO_ROOT)
        self.assertIn("python-missing", report["reasons"])
        self.assertIn("python-differs", report["reasons"])
        self.assertEqual(report["status"], "stale")

    def test_same_path_on_windows_ignores_case_and_separators(self):
        with mock.patch.object(config_mod, "on_windows", lambda: True):
            self.assertTrue(ch._same_path("C:\\Py\\python.exe", "c:/py/PYTHON.exe"))
            self.assertFalse(ch._same_path("C:\\Py\\python.exe", "C:/Py/python3.exe"))
        with mock.patch.object(config_mod, "on_windows", lambda: False):
            self.assertFalse(ch._same_path("/py/Python", "/py/python"))
            self.assertTrue(ch._same_path("/py/./python", "/py/python"))
        self.assertFalse(ch._same_path(5, "5"))

    def test_status_unreadable(self):
        self.write_settings(b"{not json")
        report = ch.status(REPO_ROOT)
        self.assertEqual(report["status"], "unreadable")
        self.assertIn(self.settings, report["error"])
        self.assertIn("it was not changed", report["error"])

    def test_status_hooks_disabled(self):
        self.write_settings({"disableAllHooks": True})
        code, _, err = run_cli("hooks", "install")
        self.assertEqual(code, 0)
        self.assertIn("sets disableAllHooks", err)
        report = ch.status(REPO_ROOT)
        self.assertEqual((report["status"], report["hooks_disabled"]), ("installed", True))
        code, out, _ = run_cli("hooks", "status")
        self.assertIn("note: disableAllHooks is set", out)


class TestCommands(HooksCase):
    def status(self) -> str:
        return json.loads(run_cli("hooks", "status", "--json")[1])["status"]

    def global_layer(self) -> Dict[str, Any]:
        return config_mod.read_config_file(config_mod.global_config_path())

    def test_hooks_install_uninstall_status_json(self):
        self.assertEqual(self.status(), "not-installed")
        code, out, err = run_cli("hooks", "install")
        self.assertEqual(code, 0, err)
        self.assertIn("Installed the reply-language hooks in %s" % self.settings, out)
        self.assertIn("  + Stop: %s -I %s stop" % (ch.python_command(), slashes(ch.relay_path())), out)
        self.assertIn(cli_hooks.RESTART_NOTE, out)
        self.assertIn("note: %s" % cli_hooks.SILENT_NOTE, out)
        report = json.loads(run_cli("hooks", "status", "--json")[1])
        self.assertEqual(report, json.loads(json.dumps(ch.status(doctor.PLUGIN_ROOT))))
        self.assertEqual(report["status"], "installed")
        code, out, _ = run_cli("hooks", "status")
        self.assertIn("Status: installed", out)
        self.assertIn("they stay silent; `dev-orchestra hooks uninstall` removes them", out)
        code, out, _ = run_cli("hooks", "uninstall")
        self.assertEqual(code, 0)
        self.assertIn("Removed the reply-language hooks from %s" % self.settings, out)
        self.assertEqual(self.status(), "not-installed")
        self.assertIn("No reply-language hooks to remove", run_cli("hooks", "uninstall")[1])

    def test_hooks_status_names_the_fix_when_a_language_is_set(self):
        self.set_global_language("ja", "--no-hooks")
        code, out, _ = run_cli("hooks", "status")
        self.assertEqual(code, 0)
        self.assertIn("Status: not-installed", out)
        self.assertIn("language.reply is ja; run: dev-orchestra hooks install", out)

    def test_config_set_language_installs_when_claude_dir_exists(self):
        os.makedirs(self.claude_dir)
        out = self.set_global_language("ja")
        self.assertIn("Installed the reply-language hooks in %s" % self.settings, out)
        self.assertEqual(self.status(), "installed")
        # Already installed: nothing more is said.
        self.assertNotIn("hooks", self.set_global_language("ko"))

    def test_config_set_language_skips_without_claude_dir(self):
        out = self.set_global_language("ja")
        self.assertIn("note: Claude Code settings not found; `dev-orchestra hooks install` creates them", out)
        self.assertFalse(os.path.lexists(self.claude_dir))
        self.assertFalse(os.path.lexists(ch.hooks_dir()))

    def test_config_set_no_hooks(self):
        os.makedirs(self.claude_dir)
        out = self.set_global_language("ja", "--no-hooks")
        self.assertNotIn("hooks", out)
        self.assertEqual(os.listdir(self.claude_dir), [])
        self.assertEqual(self.global_layer()["language"], {"reply": "ja"})

    def test_config_set_null_uninstalls_with_note(self):
        os.makedirs(self.claude_dir)
        self.set_global_language("ja")
        out = self.set_global_language("null")
        self.assertIn("Removed the reply-language hooks from %s" % self.settings, out)
        self.assertIn("note: %s" % cli_hooks.OTHER_PROJECTS_NOTE, out)
        self.assertEqual(self.status(), "not-installed")
        self.assertFalse(os.path.lexists(ch.relay_path()))

    def test_config_set_null_keeps_hooks_when_the_other_layer_still_sets(self):
        os.makedirs(self.claude_dir)
        os.makedirs(os.path.join(self.project, ".git"))
        self.set_global_language("ja")
        code, _, err = run_cli("config", "set", "language.reply", "ko", "--scope", "project")
        self.assertEqual(code, 0, err)
        out = self.set_global_language("null")
        self.assertNotIn("Removed", out)
        self.assertEqual(self.status(), "installed")
        self.set_global_language("ja")
        code, out, _ = run_cli("config", "reset", "--scope", "project")
        self.assertEqual(code, 0)
        self.assertNotIn("Removed the reply-language hooks", out)
        self.assertEqual(self.status(), "installed")

    def test_config_setup_preset_with_language_installs(self):
        os.makedirs(self.claude_dir)
        code, out, err = run_cli("config", "setup", "--preset", "standard", "--language", "JA")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer()["language"], {"reply": "ja"})
        self.assertEqual(self.global_layer()["preset"], "standard")
        self.assertIn("Installed the reply-language hooks", out)
        self.assertEqual(self.status(), "installed")

    def test_config_setup_defaults_with_language(self):
        code, out, err = run_cli("config", "setup", "--defaults", "--language", "zh-TW")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.global_layer(), {"version": 1, "language": {"reply": "zh-TW"}})
        self.assertIn("Claude Code settings not found", out)
        code, _, err = run_cli("config", "setup", "--defaults", "--language", "chinese")
        self.assertEqual(code, 2)
        self.assertIn("--language: 'chinese' is not a language tag", err)
        self.assertEqual(self.global_layer()["language"], {"reply": "zh-TW"})
        os.makedirs(self.claude_dir)
        self.assertEqual(run_cli("hooks", "install")[0], 0)
        code, out, _ = run_cli("config", "setup", "--defaults")
        self.assertEqual(code, 0)
        self.assertIn("Removed the reply-language hooks", out)
        self.assertEqual(self.status(), "not-installed")
        code, out, _ = run_cli("config", "setup", "--defaults", "--language", "ko", "--no-hooks")
        self.assertEqual(code, 0)
        self.assertEqual(self.status(), "not-installed")

    def test_config_reset_uninstalls(self):
        os.makedirs(self.claude_dir)
        self.set_global_language("ja")
        code, out, _ = run_cli("config", "reset", "--scope", "global")
        self.assertEqual(code, 0)
        self.assertIn("Removed the reply-language hooks", out)
        self.assertEqual(self.status(), "not-installed")
        self.set_global_language("ja")
        code, out, _ = run_cli("config", "reset", "--scope", "global", "--delete")
        self.assertIn("Removed the reply-language hooks", out)
        self.assertEqual(self.status(), "not-installed")

    def test_hooks_error_is_warning_config_still_saved(self):
        raw = self.write_settings(b"{not json")
        code, _, err = run_cli("config", "set", "language.reply", "ja", "--scope", "global")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: %s is not valid JSON" % self.settings, err)
        self.assertIn("warning: the configuration is saved; run `dev-orchestra hooks install`", err)
        self.assertEqual(self.global_layer()["language"], {"reply": "ja"})
        self.assertEqual(self.read_bytes(), raw)

    def write_project_language(self, tag: Optional[str]) -> None:
        os.makedirs(os.path.join(self.project, ".git"), exist_ok=True)
        path = os.path.join(self.project, ".dev-orchestra.yaml")
        config_mod.write_config_file(path, {"version": 1, "language": {"reply": tag}}, "project")

    def test_project_null_keeps_the_hooks_the_global_file_needs(self):
        """A project opting out undoes the global tag for that project only."""
        os.makedirs(self.claude_dir)
        os.makedirs(os.path.join(self.project, ".git"))
        self.set_global_language("ja")
        for value in ("ko", "null"):
            code, out, err = run_cli("config", "set", "language.reply", value, "--scope", "project")
            self.assertEqual(code, 0, err)
            self.assertNotIn("Removed", out)
        self.assertEqual(ch.configured_reply(self.project), (True, None))
        self.assertEqual(self.status(), "installed")
        code, out, _ = run_cli("config", "reset", "--scope", "project")
        self.assertNotIn("Removed", out)
        self.assertEqual(self.status(), "installed")

    def test_a_project_file_is_no_consent_to_install(self):
        """A cloned repository's language.reply never edits the user's settings."""
        os.makedirs(self.claude_dir)
        self.write_project_language("ja")
        self.set_global_language("null")
        self.assertEqual(run_cli("config", "reset", "--scope", "global")[0], 0)
        self.assertEqual(run_cli("config", "setup", "--defaults")[0], 0)
        code, _, err = run_cli("config", "set", "language.rewrite", "false", "--scope", "global")
        self.assertEqual(code, 0, err)
        self.assertEqual(os.listdir(self.claude_dir), [])
        self.assertFalse(os.path.lexists(ch.hooks_dir()))

    def test_no_hooks_choice_holds_when_the_language_changes(self):
        os.makedirs(self.claude_dir)
        self.set_global_language("ja", "--no-hooks")
        out = self.set_global_language("ko")
        self.assertIn("note: %s" % cli_hooks.LEFT_OUT_NOTE, out)
        self.assertNotIn("Installed", out)
        self.assertEqual(os.listdir(self.claude_dir), [])
        self.assertNotIn("note:", self.set_global_language("ko"))
        # Cleared and set again, it is newly set: that installs.
        self.set_global_language("null")
        self.assertIn("Installed the reply-language hooks", self.set_global_language("ja"))

    def test_config_set_repairs_stale_hooks(self):
        os.makedirs(self.claude_dir)
        self.set_global_language("ja")
        data = self.read_settings()
        data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 1
        self.write_settings(data)
        self.assertEqual(self.status(), "stale")
        out = self.set_global_language("ko")
        self.assertIn("Installed the reply-language hooks", out)
        self.assertEqual(self.status(), "installed")

    def test_other_language_keys_leave_the_settings_alone(self):
        os.makedirs(self.claude_dir)
        self.set_global_language("ja")
        data = self.read_settings()
        data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 1
        stale = self.write_settings(data)
        for value in ("false", "true"):
            code, out, err = run_cli("config", "set", "language.rewrite", value, "--scope", "global")
            self.assertEqual(code, 0, err)
            self.assertNotIn("hooks", out, value)
            self.assertEqual(self.read_bytes(), stale, value)

    def test_unparseable_config_never_touches_settings(self):
        os.makedirs(self.claude_dir)
        os.makedirs(os.path.join(self.project, ".git"))
        with open(config_mod.global_config_path(), "w", encoding="utf-8") as handle:
            handle.write("- not\n- a mapping\n")
        code, _, err = run_cli("config", "set", "language.reply", "ja", "--scope", "project")
        self.assertTrue(code != 0 or "warning" in err, (code, err))
        self.assertEqual(os.listdir(self.claude_dir), [])
        self.assertFalse(os.path.lexists(ch.hooks_dir()))

    def test_interactive_setup_offers_language_and_installs(self):
        os.makedirs(self.claude_dir)
        seen: Dict[str, Any] = {}

        def fake_run(prompter, existing=None, base=None, scope="global", **kwargs):
            seen.update(kwargs, scope=scope)
            return {"version": 1, "language": {"reply": "ja"}}, True

        with mock.patch.object(cli.wizard_mod, "run", fake_run):
            code, out, err = run_cli("config", "setup", "--force", "--language", "JA")
        self.assertEqual(code, 0, err)
        self.assertEqual((seen["reply"], seen["scope"]), ("ja", "global"))
        self.assertEqual(self.global_layer()["language"], {"reply": "ja"})
        self.assertIn("Installed the reply-language hooks", out)
        self.assertEqual(self.status(), "installed")

    def test_delegated_config_set_never_touches_settings(self):
        os.makedirs(self.claude_dir)
        os.environ[DELEGATED_ENV] = "1"
        os.environ[ch.SESSION_ENV] = SESSION
        self.set_global_language("ja")
        self.assertEqual(os.listdir(self.claude_dir), [])
        self.assertFalse(os.path.lexists(ch.hooks_dir()))
        self.assertEqual(self.global_layer()["language"], {"reply": "ja"})


class TestDoctor(HooksCase):
    def doctor_json(self) -> Dict[str, Any]:
        return json.loads(run_cli("doctor", "--fast", "--json")[1])

    def test_doctor_reports_installed_hooks(self):
        os.makedirs(self.claude_dir)
        self.set_global_language("ja")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn(
            "Reply language: ja (global) -- Claude Code: Stop-hook rewrite + reminder (hooks in %s); "
            "Codex, Antigravity: rule 11 only" % doctor._home_relative(self.settings),
            out,
        )
        self.assertNotIn("reply-language hooks", " ".join(self.doctor_json()["notes"]))

    def test_doctor_stale_hooks_note_with_fix_command(self):
        self.set_global_language("ja")
        notes = " ".join(self.doctor_json()["notes"])
        missing = "the reply-language hooks are not in %s; run: dev-orchestra hooks install" % self.settings
        self.assertIn(missing, notes)
        self.assertEqual(run_cli("hooks", "install")[0], 0)
        other = os.path.join(self.tmp, "another-checkout")
        with mock.patch.object(doctor, "PLUGIN_ROOT", other):
            _, out, _ = run_cli("doctor", "--fast")
            notes = " ".join(self.doctor_json()["notes"])
        self.assertIn(
            "Claude Code: hooks out of date (plugin-root-differs) -- run: dev-orchestra hooks install;", out
        )
        self.assertIn("are out of date (plugin-root-differs); run: dev-orchestra hooks install", notes)
        self.set_global_language("null", "--no-hooks")
        notes = " ".join(self.doctor_json()["notes"])
        self.assertIn("no language.reply is set: they stay silent; `dev-orchestra hooks uninstall`", notes)

    def test_doctor_json_language_block(self):
        self.write_settings({"disableAllHooks": True})
        self.set_global_language("ja")
        report = self.doctor_json()
        claude = report["language"]["hosts"]["claude"]
        self.assertEqual(claude, json.loads(json.dumps(ch.status(doctor.PLUGIN_ROOT))))
        self.assertEqual((claude["status"], claude["hooks_disabled"]), ("installed", True))
        self.assertIn("sets disableAllHooks", " ".join(report["notes"]))
        self.write_settings(b"{not json")
        report = self.doctor_json()
        self.assertEqual(report["language"]["hosts"]["claude"]["status"], "unreadable")
        self.assertIn("is not valid JSON", " ".join(report["notes"]))


if __name__ == "__main__":
    unittest.main()
