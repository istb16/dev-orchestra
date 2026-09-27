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
    def command_line(mode, resolved, cwd, extra_args, options, resume_session) -> list[str]   # do not override
    def run(prompt, mode, cwd, model_spec, timeout, extra_args, resume_session) -> RunResult  # do not override
    def _launch(prompt, mode, cwd, model_spec, timeout, extra_args, resume_session) -> RunResult
    def refused_read_only_args(raw_args, source) -> list[str]   # default: refuse all
    def read_only_enforcement() -> dict                         # default: "unspecified"

    supports_resume: bool                                       # default: False
    def resume_support(root) -> dict                            # default: "unsupported" / "unspecified"
    def resume_args(session_id) -> list[str]                    # default: NotImplementedError
    def resume_rejected(outcome, mode, options, session_id) -> bool   # default: False
    def parse_session(outcome) -> dict                          # session_id, context_tokens, init
```

`detect`, `version` and `list_models` are memoised per process, so the doctor and
the wizard can ask repeatedly without re-spawning the CLI.

`run` is the gate every adapter shares: on a `plan` or `review` run it holds the
caller's raw arguments (`options.args` and `--extra`) to the adapter's
allowlist, before the adapter adds its own, and then calls `_launch`, which
starts the CLI. An adapter that needs to change how the CLI is started
overrides `_launch`; one that overrides `run` bypasses the gate and has to
carry it itself.

Continuing a session (`run architect --resume`) is opt-in per adapter.
`resume_session` travels as a keyword from `run` through `_launch` to
`command_line`, which calls `build_command` as it always did and appends the
adapter's own `resume_args(session_id)`; it is never a raw argument, so it
never meets the allowlist, and `run` refuses it on an `implement` run. The
orchestrator sends it only to an adapter that declares `supports_resume` and
whose `resume_support(root)` reports `verified`; `run` passes the keyword on
to `_launch` only when there is one, so an adapter that overrides `_launch`
with the signature from before still runs fresh runs unchanged. An adapter
that resumes has to accept the keyword and pass it to the base. After the run
the base asks `parse_session(outcome)` for the session the run ended in, the
context it last had (`context_tokens`) and what the CLI reported when the
session started (`init`), and, for a resumed run only,
`resume_rejected(outcome, mode, options, session_id)`: it must return True
only on a positive sign that the session asked for does not exist, because
the orchestrator then spends an attempt on a fresh run. The results are on
`RunResult.session_id`, `context_tokens`, `session_init` and
`resume_rejected`.

### Modes

| Mode | Meaning | Must the working tree be writable? |
| --- | --- | --- |
| `plan` | Investigate and design | No — read-only |
| `implement` | Write code and tests | Yes |
| `review` | Produce a review report | No — read-only |

Adapters translate the mode into whatever their CLI calls read-only. That
mapping is the adapter's contract with the rest of the skill: a `review` run
that can edit files is a bug in the adapter. Read-only means the CLI stops the
write, not that the prompt asks the model not to.

What each built-in adapter enforces, and by what:

| | Claude | Codex |
| --- | --- | --- |
| Edit / Write tools | refused (`--disallowed-tools`, and not in `--tools`) | OS sandbox (`-s read-only`) |
| Shell writes | no shell: only `Read`, `Grep` and `Glob` exist | OS sandbox (measured) |
| MCP tools (Slack, Drive, ...) | none: `--strict-mcp-config` | **not examined** |
| Hooks in settings files | not run: `--restricted` ignores user, project and local settings | not examined |
| Reading outside the working directory | confined to it and `--add-dir` (`--restricted`) | not confined |
| Raw arguments (`options.args`, `--extra`) | only `--add-dir <path>` | none |

`read_only_enforcement()` reports this per adapter, and `dev-orchestra doctor`
prints it: `verified` (the CLI stops writes and external side effects),
`partial` (writes are stopped, external side effects were not examined),
`unsupported` (the CLI does not advertise what enforcement needs), `unverified`
(that could not be checked), `unspecified` (the adapter says nothing). A `plan`
or `review` run on an `unsupported` or `unverified` CLI is refused (exit 2)
rather than run with less.

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
| `plan` / `review` | `--permission-mode plan --disallowed-tools Edit,Write,NotebookEdit --tools Read,Grep,Glob --strict-mcp-config --restricted` |
| `implement` | `--permission-mode acceptEdits` |
| Resume (`run architect --resume`) | the read-only command plus `--resume=<id> --fork-session`, on a CLI version verified to keep a resumed session read-only |
| Auth | Inherited environment; presence detected via `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, or the CLI's credential file |

The read-only flags were measured on claude 2.1.283. Plan mode and the three
denied tools alone did not stop a run: refused `Write`, it wrote the file with
`Bash`, and MCP tools such as sending a Slack message were reachable. With
`--tools Read,Grep,Glob --strict-mcp-config` the session has those three tools
and no MCP servers, in a resumed session too. Command hooks in the
repository's `.claude/settings.json` still ran; `--restricted` stopped them.

`--restricted` is what keeps a branch under review from bringing its own
settings, and it has consequences worth knowing:

- User, project and local `settings.json` files are not read on these runs --
  their hooks, `env`, `apiKeyHelper`, `permissions.allow`,
  `permissions.additionalDirectories`, model defaults and **`permissions.deny`**.
  A deny rule such as `Read(./.env)` that kept the model away from a secret in
  the repository no longer applies to the architect or a reviewer. Managed
  settings still apply, so move such rules there. The implementer and the
  review fixer are unchanged.
- `Read`, `Grep` and `Glob` are confined to the working directory and
  `--add-dir`. Measured on claude 2.1.283: with `--restricted`, Read of an
  absolute path outside the working directory, Grep of that directory and Glob
  of it all fail; without it the same run reads the file. Symlinks were not
  tested.

A CLI whose `claude --help` does not list `--tools`, `--strict-mcp-config` and
`--restricted` gets its `plan` and `review` runs refused, and `doctor` says
`NOT ENFORCEABLE`; one whose `--help` cannot be read is refused as `UNVERIFIED`.
`claude --help` is read once per process and shared with model and
permission-mode discovery.

### Resuming a session

`run architect --resume` continues the last architect session: the adapter
appends `--resume=<id> --fork-session` to the same read-only command. The `=`
form because `--resume` takes an optional value, and forked so the session
continued is left as it was. Only a UUID is accepted as the id. Measured on
claude 2.1.283 with the read-only flags: the forked session started with
`Glob`, `Grep` and `Read`, no MCP servers and permission mode `plan` (its init
event), under a new session id, and asked to write a file it wrote nothing. A
session that no longer exists exits 1 with one `result` event -- no turn, zero
usage, and an `errors` entry naming the id asked for -- which is how the
adapter recognises a rejection (with `stream-json` only; `text` and `json`
carry no such sign, and such a rejection is reported as an ordinary failure).

Whether a resumed session keeps those restrictions belongs to the CLI version,
so it is checked per version, in two layers, and a version in neither is not
resumed -- `--resume` runs fresh and says `(unverified)`:

- **The table shipped with the adapter**, `VERIFIED_RESUME` in
  `providers/claude.py`: versions checked before a release. Each entry names
  the read-only flags it was checked with; changing the flags makes every
  entry stale until the check is run again.
- **The record on this machine**, `<config dir>/verified/claude-resume.json`,
  written only by `python scripts/smoke_live.py --provider claude`. It runs
  the checks against the installed CLI -- a resumed session's tools, MCP
  servers and permission mode, a fork, a missing session, confinement, and
  that repository hooks do not run, fresh or resumed -- and records the
  version as passed (`versions`) or failed (`failed`), with the flags and the
  check names. A failure recorded here wins over the shipped table. The record
  is not read, and not written, when its real path is inside the workspace
  (`DEV_ORCHESTRA_HOME` pointing into the checkout); the table still applies.

So after the CLI is updated, `--resume` runs fresh until the script has been
run or a release listing the new version is installed. `resume_support(root)`
reports which (`status`, `detail`, `version`, `source`, `record`,
`verified_at`, `missing`), and `doctor` prints it on its `Resume:` line.

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

`--extra` forwards everything after it to the CLI verbatim on an `implement`
run. On `plan` and `review` the adapter ignores a loosening `permission_mode`
by design, and refuses every raw argument except `--add-dir <path>` (or
`--add-dir=<path>`): the run exits 2 before anything is spent. `--add-dir` is
accepted only from the global config and from `--extra`; the project file's
`options.args` is refused on read-only roles whatever it holds. A refusal names
the flag, its position and where it came from, never its value.

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
| Resume | not supported: `--resume` runs fresh |
| Auth | Inherited environment; presence detected via `OPENAI_API_KEY` or `$CODEX_HOME/auth.json` |

Role options: `sandbox` (`read-only` / `workspace-write` / `danger-full-access`)
and `approve` (`false` drops `--approve-for-me`). Both are ignored for `plan`
and `review`, which always use `-s read-only`.

The read-only sandbox was measured refusing a shell write ("Access to the path
... is denied", Windows). Its MCP servers were not examined, so external side
effects are not covered: `doctor` reports Codex as `partial`. A `plan` or
`review` run takes no raw arguments at all -- `-s`, `-sdanger-full-access`,
`-c sandbox_mode=...` and `--profile` are all refused, whatever the spelling.
The `-o` the adapter adds itself is not a raw argument.

Codex does not resume sessions. `codex exec resume` (0.156.1) takes no `-s`,
so nothing yet shows that a resumed session keeps the read-only sandbox, and
its session id is only printed by `--json`, which this adapter does not read.
Enabling it needs `-c sandbox_mode="read-only"` checked on a resumed session
by the same write, hook and confinement probes `smoke_live.py` runs for
Claude, and the output and usage read from `--json`.

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
   `auth_status`. Override `_launch` only if the CLI needs special output
   capture. **Override `_launch`, not `run`**: an adapter that overrides `run`
   carries the read-only raw-argument gate itself.
4. Make sure `plan` and `review` map to a genuinely read-only mode: one the CLI
   enforces without the model's cooperation. Implement
   `refused_read_only_args` if a read-only run needs any raw argument (the
   default refuses all of them), and `read_only_enforcement` to say what the
   CLI enforces; without it `doctor` says `not reported by this adapter`.
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
after an update to check that your adapter still loads. The signatures of
`run()` and `_launch()` are the surface most likely to move;
`tests/test_provider_contract.py` holds every adapter, including the example
above, to them.

The example implements only `build_command`, and still gets the read-only gate:
it lives in the `run` the adapter inherits, so a `plan` or `review` run with
any raw argument is refused (the base allowlist is empty), and `doctor` reports
`not reported by this adapter` until it implements `read_only_enforcement`.
Whether its `--read-only` flag really stops writes is the adapter's
responsibility; nothing here verifies it.

## Failure semantics

| Situation | Result |
| --- | --- |
| CLI not on PATH | `RunResult(ok=False, exit_code=127)` with a clear message |
| CLI cannot be executed | `exit_code=126` |
| Timeout | `exit_code=124`, `timed_out=True` — reported, never raised |
| Non-zero exit | `ok=False`, stderr captured and redacted |
| Unresolvable model | `ModelResolutionError` before anything runs |
| Raw argument refused on `plan` / `review` | `exit_code=2`, `invoked=False`, one line naming the flag but not its value |
| Read-only enforcement `unsupported` / `unverified` | `exit_code=2`, `invoked=False`, only when the CLI is installed |

Every captured stream passes through `redact()`, which scrubs
credential-shaped substrings before anything reaches `.ai/` or the console.
