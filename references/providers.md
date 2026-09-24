# Providers

A provider adapter is the only place that knows how to talk to a particular CLI.
Adapters live in one of two places:

- **Built-in**: `scripts/orchestrator/providers/`, registered in that package's
  `__init__.py`. These ship with the plugin.
- **Your own**: `<config dir>/providers/*.py`, outside the plugin, so they
  survive a plugin update. See
  [Adding a CLI without editing the plugin](#adding-a-cli-without-editing-the-plugin).

## The interface

```python
class Provider:
    name: str                 # config key, e.g. "claude"
    display_name: str
    executable: str           # command looked up on PATH
    fallback_models: Sequence[ModelCandidate]
    fallback_updated: str     # date the fallback list was last checked

    def detect() -> Detection                  # installed? version? auth present?
    def version() -> tuple[str | None, str | None]
    def list_models() -> list[ModelCandidate]  # discovered from the installed CLI
    def resolve_model(spec) -> ResolvedModel   # family + policy -> CLI argument
    def build_command(mode, resolved, cwd, extra_args) -> list[str]
    def run(prompt, mode, cwd, model_spec, timeout, extra_args) -> RunResult
```

`detect`, `version` and `list_models` are memoised per process, so the doctor and
the wizard can ask repeatedly without re-spawning the CLI.

### Modes

| Mode | Meaning | Must the working tree be writable? |
| --- | --- | --- |
| `plan` | Investigate and design | No — read-only |
| `implement` | Write code and tests | Yes |
| `review` | Produce a review report | No — read-only |

Adapters translate the mode into whatever their CLI calls read-only. That
mapping is the adapter's contract with the rest of the skill: a `review` run
that can edit files is a bug in the adapter.

### Progress and the idle deadline

An adapter sets `streams_progress = True` only when a healthy run of the command
it builds emits output *while working*. That must be measured, not assumed:
claiming it falsely turns a slow but working agent into a killed one. Both
shipped adapters stream, for different reasons -- Codex does so natively, Claude
because its adapter asks for `stream-json`. A role can override the deadline
with `options.idle_timeout`. See `references/limits.md` for the measurements.

### Model resolution contract

`resolve_model` returns a `ResolvedModel` whose `argument` is either

- a model name the installed CLI advertised or the user explicitly pinned, or
- `None`, meaning "omit the model flag and let the CLI choose".

Anything else must raise `ModelResolutionError`. **Adapters never construct a
model name that they have not seen.** This is what keeps the skill working
across model releases without shipping a stale catalogue.

## Claude Code adapter

Verified against `claude` 2.1.x.

| Aspect | How |
| --- | --- |
| Non-interactive run | `claude -p --output-format stream-json --verbose`, prompt on stdin |
| Model | `--model <alias-or-name>`, omitted when the family is `default` |
| Model discovery | Parses the aliases the CLI advertises in its own `--model` help text |
| `plan` / `review` | `--permission-mode plan --disallowed-tools Edit,Write,NotebookEdit` |
| `implement` | `--permission-mode acceptEdits` |
| Auth | Inherited environment; presence detected via `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, or the CLI's credential file |

Aliases such as `opus`, `sonnet` and `fable` already mean "the latest snapshot
of that family", so `version: latest` simply passes the alias through. A full
model name (`claude-opus-5`) is also accepted as a family and passed through.
The built-in fallback list is used only when `claude --help` cannot be read, and
contains aliases only — never dated snapshot ids.

Prompts are sent on **stdin**, not as an argument, which avoids command-line
length limits and quoting differences between shells.

The output format is `stream-json`, not `text`, for one reason: measured,
`text` prints nothing until a run is nearly over (first output 8.1s into an
8.9s run), so there is no way to tell a wedged agent from a busy one. The
streaming format emits `system`/`thinking_tokens` events throughout, which is
what the idle deadline watches. The final answer comes from the `result` event,
falling back to assistant text blocks and then to raw stdout, so a schema change
degrades instead of losing the output. `options.output_format: text` opts back
out — at the cost of stall detection, which is why it is not the default.

The permission modes this adapter accepts are read from the CLI's own
`--permission-mode` help text, exactly like the model aliases, so
`options.permission_mode` is validated against what is actually installed.

`acceptEdits` auto-approves file edits but not shell commands, so an Implementer
asked to run the test suite may be unable to. Two ways out, in order of
preference:

1. Allow-list the commands in the project's own `.claude/settings.json`
   (`permissions.allow`: `Bash(pytest:*)`). Narrow, and it lives with the project.
2. Set a looser mode for that role only:

```yaml
implementer:
  provider: claude
  options:
    permission_mode: bypassPermissions
```

Or ad hoc, for one run:

```bash
dev-orchestra run implementer --prompt-file plan.md --extra --permission-mode bypassPermissions
```

`--extra` forwards everything after it to the CLI verbatim, and later flags win.
Either way, `plan` and `review` modes stay read-only: the adapter ignores a
loosening `permission_mode` there by design.

## Codex adapter

Verified against `codex` 0.154.x.

| Aspect | How |
| --- | --- |
| Non-interactive run | `codex exec --skip-git-repo-check --color never -C <cwd>`, prompt on stdin |
| Model | `-m <model>`, **omitted** for the `recommended-coding` family |
| Model discovery | `codex debug models` (the CLI's own catalogue, 0.154+) plus the `model` key from `$CODEX_HOME/config.toml`; models marked `hide` are skipped |
| `plan` / `review` | `-s read-only` |
| `implement` | `-s workspace-write --approve-for-me` |
| Final answer | Captured with `-o <file>` rather than scraped from the event stream |
| Auth | Inherited environment; presence detected via `OPENAI_API_KEY` or `$CODEX_HOME/auth.json` |

Role options: `sandbox` (`read-only` / `workspace-write` / `danger-full-access`)
and `approve` (`false` drops `--approve-for-me`). Both are ignored for `plan`
and `review`, which always use `-s read-only`.

The `recommended-coding` family deliberately resolves to *no* `-m` flag. That is
the honest way to say "use the current recommended coding model": the CLI's own
default is, by definition, current.

Any other family has to be vouched for by the *installed* CLI -- it is either
the model in its `config.toml`, or a slug from the catalogue `codex debug
models` prints. `dev-orchestra model list` shows exactly what that machine
offers (`source=cli-catalog` for the catalogue). A family the CLI does not
know is refused rather than guessed:

```yaml
reviewers:
  - id: codex-independent
    provider: codex
    model:
      family: gpt-5.6-terra   # only if `dev-orchestra model list` shows it
      version: latest
    role: general
```

Older CLIs print no catalogue. There, a specific model must be pinned by hand,
which also freezes it -- do that deliberately:

```yaml
reviewers:
  - id: codex-pinned
    provider: codex
    model:
      family: gpt-something
      version: pinned
      id: gpt-something
    role: general
```

## Mock adapter

An offline adapter for tests and dry runs. It never spawns a process.

| Environment variable | Effect |
| --- | --- |
| `DEV_ORCHESTRA_MOCK_DIR` | Directory of `<mode>.txt` canned responses (`review.txt`, `implement.txt`, `plan.txt`) |
| `DEV_ORCHESTRA_MOCK_RESPONSE` | Inline canned response |
| `DEV_ORCHESTRA_MOCK_FAIL` | `1` fails every run; any other value fails only runs whose prompt contains it (e.g. one reviewer id) |

It is also always "installed", and the model family `unresolvable` raises
`ModelResolutionError` on purpose, so the failure paths are reachable on a
machine with no provider CLI at all.

Swapping a reviewer to `--provider mock` is the cheapest way to exercise the
pipeline end to end without spending tokens.

## Adding a CLI

This section is for an adapter that ships *with the plugin*. To use a CLI of
your own without editing the plugin, see
[Adding a CLI without editing the plugin](#adding-a-cli-without-editing-the-plugin)
-- the same steps apply, only the file goes somewhere else.

1. Copy `providers/codex.py` as a starting point.
2. **Read the real CLI's `--help`.** Do not write flags from memory — that is
   how adapters rot. Record the version you verified against in the module
   docstring.
3. Implement `_discover_models`, `_resolve_latest`, `build_command`, and
   `auth_status`. Override `run` only if the CLI needs special output capture.
4. Make sure `plan` and `review` map to a genuinely read-only mode.
5. Register it:

```python
# providers/__init__.py
def _bootstrap() -> None:
    from . import claude, codex, mock, yourcli

    for module, cls in (..., (yourcli, yourcli.YourProvider)):
        register(cls.name, module.build_provider, ProviderOrigin("builtin", None, module.__name__))
```

6. Add tests mirroring `tests/test_providers.py`: alias parsing, latest vs
   pinned resolution, refusal to guess, read-only vs implement command shape,
   and missing-CLI handling. No test may invoke the real CLI.

`dev-orchestra model list --provider yourcli` and `dev-orchestra doctor`
pick the new adapter up automatically.

## Adding a CLI without editing the plugin

The plugin is installed into a versioned cache, so an adapter added to
`scripts/orchestrator/providers/` disappears on the next update while the
`config.yaml` that refers to it stays. Put your adapter in the config directory
instead; it is imported at startup, after the built-ins.

| Platform | Directory |
| --- | --- |
| Windows | `%APPDATA%\dev-orchestra\providers\` |
| macOS / Linux | `$XDG_CONFIG_HOME/dev-orchestra/providers/` (default `~/.config/dev-orchestra/providers/`) |
| Any, when `DEV_ORCHESTRA_HOME` is set | `$DEV_ORCHESTRA_HOME/providers/` |

`DEV_ORCHESTRA_CONFIG` does not move it: that variable names one file, which
may sit in a project checkout, and the code beside it is not yours to import.
`dev-orchestra doctor` prints the directory it reads, whether or not it exists.

### The contract

Each `<name>.py` in that directory defines a module-level
`build_provider(executable=None)` that returns an instance of
`orchestrator.providers.base.Provider`. It is called once while loading, and the
instance's `name` -- lowercase letters, digits, `.`, `_` and `-` -- is what
`provider:` takes in `config.yaml`. Import from `orchestrator.providers.base`,
not from `.base`: the file is not part of the package, so a relative import
fails. Copying a built-in adapter therefore means changing that one import line.

A minimal adapter, for a CLI that reads its prompt on stdin:

```python
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
    name = "mycli"  # what `provider:` takes in config.yaml; must not be claude, codex or mock
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
```

Then refer to it like any other provider:

```yaml
reviewers:
  - id: mycli-general
    provider: mycli
    model:
      family: default
      version: latest
    role: general
```

### Rules

- Files load in sorted order. Names starting with `_` or `.`, anything not
  ending in `.py`, and directories (packages) are skipped. Keep the file name
  an identifier (`my_cli.py`, not `my.cli.py`).
- A built-in name (`claude`, `codex`, `mock`) is refused: the built-in wins.
- Two files providing the same name: the first in sorted order wins, and the
  second is reported.
- Do not call `register()` yourself, and do not touch the registry. The loader
  registers what `build_provider()` returns, under the name it read from the
  first call; a module that registers anything itself, at import time or from
  `build_provider()` -- on the loader's call or any later one -- is refused and
  whatever it changed is put back. That catches a mistake, such as an adapter
  copied from the plugin with its `register()` call still in it. It is not a
  defence against a module written to get round it, which can rebind anything
  in the process (see below).
- Do not start the CLI from `build_provider()` or `__init__`, and do not call
  `sys.exit()` at module level -- the file is imported on every command. A
  `SystemExit` raised while loading is recorded as a load error, like any other.

### When it goes wrong

A file that fails to import, breaks the contract or is refused never stops the
CLI: every other command carries on without it, and `dev-orchestra doctor`
lists it under **User providers** with the error, and among its problems (so
`doctor --strict` fails). `doctor` also catches an adapter that raises while it
is being diagnosed -- from `detect()`, `list_models()` or model resolution --
and reports it as `adapter-error` against the file it came from. Write the
adapter, then run `doctor` before anything else.

The directory holds trusted code, not a sandbox: every file in it is imported
into the CLI's own process with your permissions every time the CLI starts,
and can change anything the CLI does. Put only code you would run yourself
there. That is why `doctor` always says where it is and what it imported.
Set `DEV_ORCHESTRA_NO_USER_PROVIDERS=1` to skip the directory entirely, for
example when a broken adapter is in the way. While it is set, `config
validate`, `config show` and `doctor` say so next to any provider they cannot
find, so a configured user adapter is not mistaken for a missing file.

Loading the directory again in the same process (`load_user_providers()`) also
clears the memoised discovery results, so an edited adapter is detected afresh.

### Interface stability

`base.Provider` and the types around it (`ModelCandidate`, `ResolvedModel`,
`RunResult`, `Usage`, `Detection`) are internal to the plugin and may change
between minor versions. Pin the plugin version, or run `dev-orchestra doctor`
after an update to check that your adapter still loads. The signature of `run()`
is the surface most likely to move; `tests/test_provider_contract.py` holds every
adapter, including the example above, to it.

## Failure semantics

| Situation | Result |
| --- | --- |
| CLI not on PATH | `RunResult(ok=False, exit_code=127)` with a clear message |
| CLI cannot be executed | `exit_code=126` |
| Timeout | `exit_code=124`, `timed_out=True` — reported, never raised |
| Non-zero exit | `ok=False`, stderr captured and redacted |
| Unresolvable model | `ModelResolutionError` before anything runs |

Every captured stream passes through `redact()`, which scrubs
credential-shaped substrings before anything reaches `.ai/` or the console.
