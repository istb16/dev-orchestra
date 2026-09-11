# Configuration

## Where it lives

| Layer | Path | Purpose |
| --- | --- | --- |
| Project | `<repo>/.dev-orchestra.yaml` | Per-repository override, committed or not as you prefer |
| Global | see below | Your personal default for every project |
| Built-in | `scripts/orchestrator/config.py` | Recommended defaults, used when no file exists |

Global config path by platform:

| Platform | Path |
| --- | --- |
| Linux / BSD | `$XDG_CONFIG_HOME/dev-orchestra/config.yaml`, else `~/.config/dev-orchestra/config.yaml` |
| macOS | `~/.config/dev-orchestra/config.yaml` |
| Windows | `%APPDATA%\dev-orchestra\config.yaml` |

Environment overrides:

- `DEV_ORCHESTRA_CONFIG` — use this exact file as the global layer.
- `DEV_ORCHESTRA_HOME` — use this directory instead of the platform default.

`dev-orchestra config path` prints both resolved locations.

The project file is found by walking up from the current directory and stopping
at the git root, so running the CLI from a subdirectory still finds it.
Accepted names, in order: `.dev-orchestra.yaml`, `.dev-orchestra.yml`,
`.dev-orchestra.json`.

## Precedence

```
project config  →  global config  →  built-in defaults
```

Mappings merge key by key, so a project file that only sets `implementer` keeps
your global architect. **Lists replace wholesale**: a project file that defines
`reviewers` defines the entire panel for that project. That is deliberate —
"this repo reviews with security + database only" must be expressible.

## Schema (version 1)

```yaml
version: 1

orchestrator:                 # decides stages, delegates, writes the report
  provider: claude
  model:
    family: sonnet
    version: latest

architect:                    # investigation + design, read-only
  provider: claude
  model:
    family: fable
    version: latest

implementer:                  # writes code and tests
  provider: claude
  model:
    family: opus
    version: latest

review_fixer:                 # fixes accepted findings
  provider: claude
  model:
    family: opus
    version: latest

reviewers:                    # 0..n independent reviewers
  - id: claude-general
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-general
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: general

review:
  max_review_iterations: 2            # hard stop on review→fix→re-review loops
  parallel: true                      # run reviewers concurrently
  re_review_severities: [critical, high]
  timeout_seconds: 1800               # per delegated CLI run

workspace:
  dir: .ai                            # relative to the repo root, or absolute
```

### Field reference

| Field | Type | Notes |
| --- | --- | --- |
| `version` | int | Must be `1`. |
| `<role>.provider` | string | A registered adapter: `claude`, `codex`, or `mock`. |
| `<role>.model.family` | string | A family/alias the provider can resolve (`opus`, `sonnet`, `fable`, `recommended-coding`). Omit or use `default` to let the CLI choose. |
| `<role>.model.version` | `latest` \| `pinned` | `latest` re-resolves on every run. `pinned` requires `model.id`. |
| `<role>.model.id` | string | Exact model id, only with `version: pinned`. |
| `reviewers[].id` | string | Unique, matching `[a-z0-9][a-z0-9._-]*`. Names the report file. |
| `reviewers[].role` | string | Built-in or your own; see `references/reviews.md`. |
| `review.max_review_iterations` | int ≥ 0 | `0` disables re-review entirely. |
| `review.parallel` | bool | `false` runs reviewers one at a time (easier to debug). |
| `review.re_review_severities` | list | Severities that count as blocking. |
| `review.timeout_seconds` | int > 0 | Per-run timeout; a timeout is reported, not raised. |
| `review.exclude` | list | Glob patterns whose diff body is withheld from reviewers. Replaces the default list wholesale; `[]` reviews everything. |
| `review.incremental_rounds` | bool | `true` (default) makes a second round diff against what the first round reviewed, carrying the findings the fix was meant to address. `false` re-diffs the whole change every round. |
| `review.max_findings` | int \| null | How many findings each reviewer is asked for. `null` (default) lets `optimization.level` decide, `0` lifts the cap. Findings that come back over the cap are kept, never trimmed. |
| `optimization.level` | `aggressive` \| `balanced` \| `quality` | How hard to try to be cheap. Default `balanced`. See below. |
| `optimization.high_risk_paths` | list | Globs that force `quality` for a change touching them. Replaces the default list wholesale. |
| `optimization.low_risk_max_files` | int | At `aggressive`, at most this many files still counts as a small change (default 2). |
| `optimization.low_risk_max_lines` | int | And at most this many changed lines (default 50). |
| `workspace.dir` | string | Where `.ai/` artifacts go. |
| `<role>.options` | mapping | Provider-specific knobs; see below. |

### Role options

`options` is provider-specific on purpose -- there is no honest way to map
Claude's permission modes onto Codex's sandbox policies, so the adapter that
owns the CLI owns its own keys. The adapter validates them, so a typo is caught
by `config validate`, not at run time.

| Provider | Key | Values |
| --- | --- | --- |
| any | `args` | List of extra CLI arguments, appended verbatim |
| `claude` | `output_format` | `stream-json` (default), `text`, `json`. `text` disables stall detection |
| `claude` | `permission_mode` | Whatever the installed CLI advertises for `--permission-mode` (`dev-orchestra model list` aside, run `claude --help` to see them) |
| `codex` | `sandbox` | `read-only`, `workspace-write`, `danger-full-access` |
| `codex` | `approve` | `true` (default) passes `--approve-for-me`; `false` omits it |
| any | `idle_timeout` | Override the no-output deadline for this role |

```yaml
implementer:
  provider: claude
  model:
    family: opus
    version: latest
  options:
    # The default, acceptEdits, auto-approves file edits but not shell commands,
    # so an Implementer told to "run the tests" may be unable to. Loosen it here
    # if your environment makes that appropriate.
    permission_mode: bypassPermissions
    args: ["--add-dir", "../shared-lib"]
```

**Options that would loosen a read-only stage are ignored.** The architect and
every reviewer always run read-only, whatever `permission_mode` or `sandbox`
says -- that invariant is what makes an independent review worth anything.
`dev-orchestra doctor` lists any option it is ignoring for that reason rather
than dropping it silently.

The alternative to loosening a permission mode is allow-listing the specific
commands in the CLI's own settings (for Claude Code, a `permissions.allow` entry
such as `Bash(pytest:*)` in `.claude/settings.json`). That is narrower, and it
lives with the project rather than with this skill.

`mock` is a real, registered provider: an offline adapter used by the tests and
useful for dry-running the pipeline without spending tokens. It is hidden from
the setup wizard.

## Optimization level

One dial over three savings. Default `balanced`.

| | `aggressive` | `balanced` | `quality` |
| --- | --- | --- | --- |
| Tests recorded as failing | refuse | refuse | review anyway |
| No test result recorded | warn, review | warn, review | review |
| Small, low-risk change | 1 reviewer | whole panel | whole panel |
| Findings asked for | 4 | 6 | 10 |

`--force` gets past the refusal. `--only` overrides the reduced panel, because
that flag is someone naming the reviewers by hand.

**The gate reads a recorded result, it does not run anything.** This tool has
no way to know a project's test command -- the orchestrator discovers it from
the repository and runs it directly -- so the gate reads whatever the last
`dev-orchestra state record test ok|failed` wrote. That is three states, not
two: passed, failed, and never recorded. Only a recorded failure refuses;
never recorded warns and continues, so a workflow that has not adopted
`state record` keeps working exactly as before.

**A high-risk change escalates to `quality`, whatever is configured.** A
change touching authentication, secrets, payments, migrations, SQL, crypto or
deploy configuration gets the full panel and the full findings budget. The
patterns are configurable (`optimization.high_risk_paths`, `[]` to clear
them); the fact that a match escalates is not. The escalation is printed with
the file and pattern that caused it, so it can be checked rather than only
obeyed.

The patterns over-match on purpose. `authors_controller.rb` matches `*auth*`
and costs one extra reviewer; missing `auth_controller.rb` costs an
authorisation bug.

**Size is never the only test.** The reduced panel needs the change to be
under both thresholds *and* to touch nothing high-risk, because one line in an
auth file is the exact shape an authorisation bug arrives in.

```yaml
optimization:
  level: aggressive
  low_risk_max_files: 3
  low_risk_max_lines: 80
```

## Model families and version policy

Configuration stores **what kind of model you want**, not **which snapshot you
got**. `family: opus` + `version: latest` means "the newest Opus the installed
CLI offers", so the setup keeps working after a model release.

```yaml
implementer:            # tracks the latest Opus, whatever that is today
  model:
    family: opus
    version: latest

architect:              # frozen to one snapshot - only do this deliberately
  model:
    family: opus
    version: pinned
    id: claude-opus-5

review_fixer:           # let the CLI pick entirely
  model:
    family: default
    version: latest
```

Resolution happens at run time in the provider adapter, and an adapter that
cannot verify a family raises `ModelResolutionError` rather than sending a
guessed name to the CLI. The *resolved* id is recorded in `.ai/state.json` for
traceability; the config file keeps the family.

## What the user asks for, and what to run

The skill carries the command grammar; this is the phrasebook.

| The user says | Run |
| --- | --- |
| "show my configuration" | `config show` |
| "set this up" / "redo setup" | `config setup`, or `config setup --defaults` |
| "which models can I use?" | `model list` |
| "use Claude Opus for implementation" | `config set implementer.model.family opus` |
| "make the architect use Codex" | `config set architect.provider codex` **and** a family Codex accepts |
| "the implementer can't run the tests" | `config set implementer.options.permission_mode bypassPermissions`, or allow-list the command in that CLI's own settings |
| "add a Codex security reviewer" | `reviewer add --provider codex --role security` |
| "make it three reviewers" | `reviewer add …` again, then `reviewer list` |
| "remove the performance reviewer" | `reviewer remove performance` |
| "change the second reviewer" | `reviewer set 2 --provider … --role …` |
| "just this project" | add `--scope project` to any write |
| "reset to defaults" | `config reset` |
| "check my environment" | `doctor` |

Show the resulting configuration after any write, so the user can confirm it.

## Editing

```bash
dev-orchestra config show                    # effective configuration
dev-orchestra config show --scope project    # just the project layer
dev-orchestra config setup                   # interactive wizard
dev-orchestra config setup --defaults        # non-interactive, recommended values
dev-orchestra config set implementer.model.family sonnet
dev-orchestra config set --scope project architect.provider codex
dev-orchestra config set reviewers[1].role security
dev-orchestra config reset                   # back to recommended defaults
dev-orchestra config reset --delete          # remove the file entirely
dev-orchestra config validate
```

`config set` coerces values: `3` becomes an int, `true` a bool, `[a, b]` a list,
anything else a string. Use `--raw` to force a string.

Writes go to the project layer when one exists, otherwise the global layer;
`--scope global|project` decides explicitly. Editing reviewers in a project layer
that has none seeds it from the effective list first, so you edit the panel you
actually see.

## Worked examples

**A repo where the whole team should use the same panel** — commit
`.dev-orchestra.yaml`:

```yaml
version: 1
reviewers:
  - id: claude-general
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-security
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: security
  - id: codex-database
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: database
```

**Only one CLI installed** — drop the other provider everywhere:

```bash
dev-orchestra config set architect.provider claude
dev-orchestra reviewer remove codex-general
dev-orchestra reviewer add --provider claude --role security
```

**A cheap, fast loop for a small repo**:

```bash
dev-orchestra config set implementer.model.family sonnet
dev-orchestra config set review.max_review_iterations 1
dev-orchestra reviewer remove 2
```

**No reviews at all** (valid, and `doctor` will point it out):

```bash
dev-orchestra reviewer remove 1
dev-orchestra reviewer remove 1
```

## YAML dialect

Config files are parsed by PyYAML when it is installed, and otherwise by a
built-in parser covering block mappings, block sequences, inline empty
collections, inline scalar lists, comments, and quoted strings. Anchors,
aliases, multi-document streams, and block scalars (`|`, `>`) are rejected with
a clear error. JSON is always accepted.
