# Providers

A provider adapter is the only place that knows how to talk to a particular CLI.
Adapters live in `scripts/orchestrator/providers/` and are registered in that
package's `__init__.py`.

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
| Non-interactive run | `claude -p --output-format text`, prompt on stdin |
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

If you want fuller autonomy for the implement stage (for example a different
permission mode), pass it explicitly rather than changing the default:

```bash
ai-orchestrator run implementer --prompt-file plan.md --extra --permission-mode bypassPermissions
```

`--extra` forwards everything after it to the CLI verbatim, and later flags win.
Only do this where the sandboxing situation makes it appropriate.

## Codex adapter

Verified against `codex` 0.154.x.

| Aspect | How |
| --- | --- |
| Non-interactive run | `codex exec --skip-git-repo-check --color never -C <cwd>`, prompt on stdin |
| Model | `-m <model>`, **omitted** for the `recommended-coding` family |
| Model discovery | The CLI has no "list models" command, so the adapter reads the `model` key from `$CODEX_HOME/config.toml` |
| `plan` / `review` | `-s read-only` |
| `implement` | `-s workspace-write --approve-for-me` |
| Final answer | Captured with `-o <file>` rather than scraped from the event stream |
| Auth | Inherited environment; presence detected via `OPENAI_API_KEY` or `$CODEX_HOME/auth.json` |

The `recommended-coding` family deliberately resolves to *no* `-m` flag. That is
the honest way to say "use the current recommended coding model" for a CLI that
does not publish a model list: the CLI's own default is, by definition, current.
Any other family must match the CLI's configured model, or be pinned explicitly:

```yaml
reviewers:
  - id: codex-pinned
    provider: codex
    model: {family: gpt-something, version: pinned, id: gpt-something}
    role: general
```

## Mock adapter

An offline adapter for tests and dry runs. It never spawns a process.

| Environment variable | Effect |
| --- | --- |
| `AI_ORCHESTRATOR_MOCK_DIR` | Directory of `<mode>.txt` canned responses (`review.txt`, `implement.txt`, `plan.txt`) |
| `AI_ORCHESTRATOR_MOCK_RESPONSE` | Inline canned response |
| `AI_ORCHESTRATOR_MOCK_FAIL` | `1` fails every run; any other value fails only runs whose prompt contains it (e.g. one reviewer id) |

Swapping a reviewer to `--provider mock` is the cheapest way to exercise the
pipeline end to end without spending tokens.

## Adding a CLI

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

    ...
    register(yourcli.YourProvider.name, yourcli.build_provider)
```

6. Add tests mirroring `tests/test_providers.py`: alias parsing, latest vs
   pinned resolution, refusal to guess, read-only vs implement command shape,
   and missing-CLI handling. No test may invoke the real CLI.

`ai-orchestrator model list --provider yourcli` and `ai-orchestrator doctor`
pick the new adapter up automatically.

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
