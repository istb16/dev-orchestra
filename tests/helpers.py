"""Shared test scaffolding: isolated config homes and throwaway git repos."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

ENV_KEYS = (
    "DEV_ORCHESTRA_HOME",
    "DEV_ORCHESTRA_WORKFLOW",
    "DEV_ORCHESTRA_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_SESSION_ID",
    "DEV_ORCHESTRA_CONFIG",
    "DEV_ORCHESTRA_MOCK_DIR",
    "DEV_ORCHESTRA_MOCK_RESPONSE",
    "DEV_ORCHESTRA_MOCK_FAIL",
    "DEV_ORCHESTRA_MOCK_DELAY",
    "XDG_CONFIG_HOME",
    "APPDATA",
)

#: CI runs with neither claude nor codex on PATH, and the suite must pass there.
#: Set this locally to reproduce that without uninstalling anything.
ASSUME_NO_CLI = "DEV_ORCHESTRA_TEST_ASSUME_NO_CLI"

#: The workflow every test runs in unless it says otherwise.
TEST_WORKFLOW = "test"


class IsolatedCase(unittest.TestCase):
    """Runs each test with its own config home and working directory."""

    def setUp(self) -> None:
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self._saved_cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="devorchestra-test-")
        self.config_home = os.path.join(self.tmp, "cfg")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.config_home)
        os.makedirs(self.project)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["DEV_ORCHESTRA_HOME"] = self.config_home
        # Artifacts live in `.ai/workflows/<id>/`, and the id is resolved from
        # the environment. Pinning it keeps every test's paths predictable and
        # keeps the host session this suite runs under out of them; the tests
        # that are *about* resolution set their own.
        os.environ["DEV_ORCHESTRA_WORKFLOW"] = TEST_WORKFLOW
        os.chdir(self.project)
        # Discovery is memoised per process; tests patch CLIs, so start clean.
        from orchestrator.providers import base as provider_base

        provider_base.clear_discovery_cache()
        self.addCleanup(provider_base.clear_discovery_cache)
        if os.environ.get(ASSUME_NO_CLI):
            self._hide_provider_clis()

    def _hide_provider_clis(self) -> None:
        from orchestrator.providers.claude import ClaudeProvider
        from orchestrator.providers.codex import CodexProvider

        for cls in (ClaudeProvider, CodexProvider):
            original = cls.which
            cls.which = lambda self: None
            self.addCleanup(setattr, cls, "which", original)

    def tearDown(self) -> None:
        os.chdir(self._saved_cwd)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def write(self, relative: str, content: str) -> str:
        path = os.path.join(self.project, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return path

    def cli_workspace(self, workflow: str = TEST_WORKFLOW):
        """The workspace the CLI writes to in this test.

        Artifacts moved under `.ai/workflows/<id>/` in 0.4.0, so a test that
        reads what a command wrote has to ask for the same workflow the command
        resolved rather than for `.ai/` itself.
        """
        from orchestrator import workspace as ws

        return ws.Workspace(self.project, workflow=workflow).ensure()

    def init_git_repo(self) -> None:
        for args in (
            ["init", "-q"],
            ["config", "user.email", "test@example.invalid"],
            ["config", "user.name", "Test"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", *args], cwd=self.project, check=True, capture_output=True)

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.project, check=True, capture_output=True, text=True)

    def commit_all(self, message: str = "wip") -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", message)


def has_git() -> bool:
    return shutil.which("git") is not None
