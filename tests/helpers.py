"""Shared test scaffolding: isolated config homes and throwaway git repos."""

from __future__ import annotations

import atexit
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import Optional, TypeVar

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Importing the provider registry imports every `.py` in the user's config
# directory. Nothing has pointed DEV_ORCHESTRA_HOME anywhere yet at this point,
# so that would be the real user's adapters -- which is why this module has to
# be imported before anything from `orchestrator`, and why tests that want a
# user adapter write one and call `load_user_providers()` themselves.
os.environ.setdefault("DEV_ORCHESTRA_NO_USER_PROVIDERS", "1")
if "orchestrator.providers" in sys.modules:
    # Someone imported the registry first; drop whatever it picked up.
    sys.modules["orchestrator.providers"].unload_user_providers()

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
    "DEV_ORCHESTRA_MOCK_SUSPENDED",
    "DEV_ORCHESTRA_MOCK_SESSION",
    "DEV_ORCHESTRA_MOCK_RESUME",
    "DEV_ORCHESTRA_MOCK_CONTEXT_TOKENS",
    "DEV_ORCHESTRA_MOCK_TRACE",
    "DEV_ORCHESTRA_NO_USER_PROVIDERS",
    "XDG_CONFIG_HOME",
    "APPDATA",
)

#: CI runs with neither claude nor codex on PATH, and the suite must pass there.
#: Set this locally to reproduce that without uninstalling anything.
ASSUME_NO_CLI = "DEV_ORCHESTRA_TEST_ASSUME_NO_CLI"

#: The workflow every test runs in unless it says otherwise.
TEST_WORKFLOW = "test"

#: A directory holding an initialised `.git` that `init_git_repo()` copies. The
#: parallel runner creates one and hands it to its workers through this; it is
#: deliberately not in ENV_KEYS, which setUp clears for every test.
GIT_TEMPLATE_ENV = "DEV_ORCHESTRA_TEST_GIT_TEMPLATE"

#: The identity `git commit` needs, written straight into the template's config
#: instead of three `git config` processes per repository.
GIT_TEMPLATE_CONFIG = """\
[user]
\temail = test@example.invalid
\tname = Test
[commit]
\tgpgsign = false
"""

_git_template = None

#: ``claude --help`` as 2.1.283 prints it, trimmed to the options the adapter
#: reads. The three read-only flags are verbatim, including the ``--restricted``
#: description that mentions ``--tools`` mid-sentence.
CLAUDE_HELP_BEFORE_READ_ONLY = """Usage: claude [options] [command] [prompt]

Options:
  --mcp-config <configs...>             Load MCP servers from JSON files
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name (e.g.
                                        'claude-fable-5').
  -n, --name <name>                     Set a display name for this session
  --permission-mode <mode>              Permission mode to use for the session
                                        (choices: "acceptEdits", "auto",
                                        "bypassPermissions", "manual",
                                        "dontAsk", "plan")
  -p, --print                           Print response and exit
"""

CLAUDE_HELP_RESTRICTED = """\
  --restricted                          Restricted mode: removes the built-in
                                        tools that run commands or code (Bash,
                                        PowerShell, REPL and the other
                                        code-running tools) and WebFetch unless
                                        --tools names them, and ignores user,
                                        project and local settings files
                                        (managed settings and --settings still
                                        apply; add --strict-mcp-config to skip
                                        MCP servers too). Also confines the file
                                        tools to the working directories
                                        (--add-dir included), refuses
                                        bypassPermissions, and lets only a
                                        person or the configured permission
                                        handler approve writes to settings, git
                                        and tool-configuration files.
"""

CLAUDE_HELP_STRICT_MCP = """\
  --strict-mcp-config                   Only use MCP servers from --mcp-config,
                                        ignoring all other MCP configurations
"""

CLAUDE_HELP_TOOLS = """\
  --tools <tools...>                    Specify the list of available tools from
                                        the built-in set. Use "" to disable all
                                        tools, "default" to use all tools, or
                                        specify tool names (e.g.
                                        "Bash,Edit,Read").
"""

CLAUDE_HELP_FORK = """\
  --fork-session                        When resuming, create a new session ID
                                        instead of reusing the original (use
                                        with --resume or --continue)
"""

CLAUDE_HELP_RESUME_ONLY = """\
  -r, --resume [value]                  Resume a conversation by session ID, or
                                        open interactive picker with optional
                                        search term
"""

#: The two lines a resumed run needs, verbatim from 2.1.283.
CLAUDE_HELP_RESUME = CLAUDE_HELP_FORK + CLAUDE_HELP_RESUME_ONLY

#: The wording 2.1.283 prints; the line breaks follow the other options.
CLAUDE_HELP_PARTIAL_MESSAGES = """\
  --include-partial-messages            Include partial message chunks as they
                                        arrive (only works with --print and
                                        --output-format=stream-json)
"""

#: A CLI that advertises read-only runs and nothing about resuming.
CLAUDE_HELP_NO_RESUME = (
    CLAUDE_HELP_BEFORE_READ_ONLY
    + CLAUDE_HELP_PARTIAL_MESSAGES
    + CLAUDE_HELP_RESTRICTED
    + CLAUDE_HELP_STRICT_MCP
    + CLAUDE_HELP_TOOLS
)

#: A CLI that advertises everything read-only and resumed runs need.
CLAUDE_HELP = CLAUDE_HELP_NO_RESUME + CLAUDE_HELP_RESUME

#: The same CLI without ``--fork-session``.
CLAUDE_HELP_NO_FORK = CLAUDE_HELP_NO_RESUME + CLAUDE_HELP_RESUME_ONLY

#: The same CLI without ``--tools``: ``--tools`` still appears, inside the
#: ``--restricted`` description, which must not count as the option.
CLAUDE_HELP_OLD = CLAUDE_HELP_BEFORE_READ_ONLY + CLAUDE_HELP_RESTRICTED + CLAUDE_HELP_STRICT_MCP

#: The minimal user adapter from references/providers.md, verbatim; the docs
#: test holds the two together and the contract test runs this one.
USER_ADAPTER_SOURCE = '''\
"""Adapter for the ``mycli`` CLI. Verified against mycli 1.2.0."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from orchestrator.providers.base import (
    READ_ONLY_MODES,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
)


class MyCliProvider(Provider):
    name = "mycli"  # what `provider:` takes in config.yaml; must not be agy, claude, codex or mock
    display_name = "My CLI"
    executable = "mycli"

    fallback_models = (ModelCandidate("", "default", "CLI default", "builtin-fallback"),)
    fallback_updated = "2026-09-24"

    def _resolve_latest(self, family: str) -> ResolvedModel:
        if family in ("", "default"):
            return ResolvedModel(self.name, "default", "latest", None, "mycli default", "cli-default")
        raise ModelResolutionError(
            "mycli: %r is not something the installed CLI vouches for; "
            "pin it with model.version: pinned and model.id" % family
        )

    def build_command(
        self,
        mode: str,
        resolved: ResolvedModel,
        cwd: str,
        extra_args: Sequence[str] = (),
        options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        # The prompt arrives on stdin; plan and review must be read-only.
        command = [self.executable, "run", "--cwd", cwd]
        if mode in READ_ONLY_MODES:
            command.append("--read-only")
        if resolved.argument:
            command += ["--model", resolved.argument]
        command += self.option_args(options)
        command += list(extra_args)
        return command


def build_provider(executable: Optional[str] = None) -> MyCliProvider:
    return MyCliProvider(executable)
'''


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
        from orchestrator import doctor, providers
        from orchestrator.providers import base as provider_base

        # The Antigravity global install is under the home directory: every
        # doctor test would otherwise look at the developer's own. Named, not
        # created; a test that needs it makes it.
        self.os_home = os.path.join(self.tmp, "home")
        original_home = doctor.user_home
        setattr(doctor, "user_home", lambda: self.os_home)
        self.addCleanup(setattr, doctor, "user_home", original_home)
        provider_base.clear_discovery_cache()
        self.addCleanup(provider_base.clear_discovery_cache)
        # Likewise user adapters: none unless the test writes and loads one.
        providers.unload_user_providers()
        self.addCleanup(providers.unload_user_providers)
        if os.environ.get(ASSUME_NO_CLI):
            self._hide_provider_clis()

    def _hide_provider_clis(self) -> None:
        from orchestrator.providers.agy import AgyProvider
        from orchestrator.providers.claude import ClaudeProvider
        from orchestrator.providers.codex import CodexProvider

        for cls in (AgyProvider, ClaudeProvider, CodexProvider):
            original = cls.which
            setattr(cls, "which", lambda self: None)
            self.addCleanup(setattr, cls, "which", original)

    def fake_clis(self, claude: bool = False, codex: bool = False, agy: bool = False) -> None:
        """Pretend exactly these built-in CLIs are on PATH, whatever the machine has.

        Nothing is ever spawned: ``_capture`` answers as a CLI that cannot be
        run, and Codex reads no configuration of the user's.
        """
        from orchestrator.providers.agy import AgyProvider
        from orchestrator.providers.claude import ClaudeProvider
        from orchestrator.providers.codex import CodexProvider

        replacements = []
        for cls, present in ((ClaudeProvider, claude), (CodexProvider, codex), (AgyProvider, agy)):
            found = (lambda self: self.executable) if present else (lambda self: None)
            replacements += [(cls, "which", found), (cls, "_capture", lambda self, command, timeout=30: None)]
        replacements.append((CodexProvider, "configured_model", lambda self: None))
        for cls, name, value in replacements:
            self.addCleanup(setattr, cls, name, getattr(cls, name))
            setattr(cls, name, value)
        from orchestrator.providers import base as provider_base

        provider_base.clear_discovery_cache()

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

    def write_user_provider(self, stem: str, source: str = USER_ADAPTER_SOURCE) -> str:
        """Write ``<config home>/providers/<stem>.py``; loading it is up to the test."""
        path = os.path.join(self.config_home, "providers", stem + ".py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(source)
        return path

    def load_user_providers(self):
        from orchestrator import providers

        return providers.load_user_providers()

    def cli_workspace(self, workflow: str = TEST_WORKFLOW):
        """The workspace the CLI writes to in this test.

        Artifacts moved under `.ai/workflows/<id>/` in 0.4.0, so a test that
        reads what a command wrote has to ask for the same workflow the command
        resolved rather than for `.ai/` itself.
        """
        from orchestrator import workspace as ws

        return ws.Workspace(self.project, workflow=workflow).ensure()

    def init_git_repo(self) -> None:
        # Copying a template is what `git init` plus three `git config` calls
        # leave behind, without the processes. Copying over an existing `.git`
        # would reset HEAD, which re-running `git init` does not, so refuse.
        target = os.path.join(self.project, ".git")
        if os.path.exists(target):
            raise AssertionError("%s is already a git repository" % self.project)
        template = os.path.join(ensure_git_template(), ".git")
        shutil.copytree(template, target, ignore=shutil.ignore_patterns("hooks"))

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.project, check=True, capture_output=True, text=True)

    def commit_all(self, message: str = "wip") -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", message)


_T = TypeVar("_T")


def present(value: Optional[_T]) -> _T:
    """``value``, asserted to be there: a test's precondition, said out loud.

    For a call that returns ``None`` when there is nothing to return, used where
    the test has just made sure there is. Failing here says so, instead of a
    ``TypeError`` one line later -- and raises rather than asserts, so it holds
    under ``python -O`` too.
    """
    if value is None:
        raise AssertionError("expected a value, got None")
    return value


def has_git() -> bool:
    return shutil.which("git") is not None


def is_link(path: str) -> bool:
    """A symlink, or on Windows any reparse point, which a junction is.

    ``os.path.islink`` is false for a junction before Python 3.12, so a walk
    or a copy of the tree would follow one -- into the checkout itself, when
    a project install was made there.
    """
    if os.path.islink(path):
        return True
    if os.name != "nt":
        return False
    try:
        attributes = os.lstat(path).st_file_attributes
    except OSError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def make_dir_link(link: str, target: str) -> None:
    """A link to a directory: a junction on Windows, which needs no privilege; a symlink elsewhere."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", link, target], check=True, capture_output=True)
    else:
        os.symlink(target, link)


def remove_link(path: str) -> None:
    """Remove a link made by ``make_dir_link`` without touching what it points at."""
    if os.name == "nt":
        os.rmdir(path)
    else:
        os.unlink(path)


def remove_tree(path: str) -> None:
    """``rmtree`` that also removes read-only files, which git's objects are."""

    def retry_writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if not os.path.exists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry_writable)
    else:
        shutil.rmtree(path, onerror=retry_writable)


def create_git_template() -> str:
    """Make a directory holding a freshly initialised `.git`; removing it is the caller's."""
    path = tempfile.mkdtemp(prefix="devorchestra-git-template-")
    try:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
        with open(os.path.join(path, ".git", "config"), "a", encoding="utf-8", newline="\n") as handle:
            handle.write(GIT_TEMPLATE_CONFIG)
    except BaseException:
        remove_tree(path)
        raise
    return path


def ensure_git_template() -> str:
    """The template `init_git_repo()` copies: the runner's if it passed one, else this process's."""
    global _git_template
    shared = os.environ.get(GIT_TEMPLATE_ENV)
    if shared and os.path.isdir(os.path.join(shared, ".git")):
        return shared
    if _git_template is None:
        _git_template = create_git_template()
        atexit.register(remove_tree, _git_template)
    return _git_template
