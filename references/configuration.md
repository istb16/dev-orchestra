# Configuration

<!-- contents: start -->

**Contents**

- [Where it lives](#where-it-lives)
- [Precedence](#precedence)
- [Schema (version 1)](#schema-version-1)
  - [Field reference](#field-reference)
  - [Role options](#role-options)
- [Optimization level](#optimization-level)
  - [Reviewers that run only on high-risk changes](#reviewers-that-run-only-on-high-risk-changes)
- [Model tiers](#model-tiers)
- [Model families and version policy](#model-families-and-version-policy)
- [What the user asks for, and what to run](#what-the-user-asks-for-and-what-to-run)
- [Editing](#editing)
  - [The wizard](#the-wizard)
- [Worked examples](#worked-examples)
- [YAML dialect](#yaml-dialect)

<!-- contents: end -->

## Where it lives

| Layer | Path | Purpose |
| --- | --- | --- |
| Project | `<repo>/.dev-orchestra.yaml` | Per-repository override, committed or not as you prefer. Read-only roles do not take `options.args` from it ([below](#role-options)) |
| Global | see below | Your personal default for every project |
| Built-in | `scripts/orchestrator/config.py` | Recommended defaults, used when no file exists |

In the config directory, a `providers/` directory holds your own provider
adapters (`%APPDATA%\dev-orchestra\providers\` on Windows,
`~/.config/dev-orchestra/providers/` elsewhere, `$DEV_ORCHESTRA_HOME/providers/`
when that is set; `DEV_ORCHESTRA_CONFIG` does not move it). See
`references/providers.md`.

Beside it, `verified/<provider>-resume.json` records the CLI versions that
`python scripts/smoke_live.py` checked on this machine for resuming a session
(`run architect --resume`); only that script writes it. It is read only when
its real path is outside the workspace: point `DEV_ORCHESTRA_HOME` into the
checkout and the record is neither read nor written, and `doctor` and the
`--resume` note say so.

Global config path by platform:

| Platform | Path |
| --- | --- |
| Linux / BSD | `$XDG_CONFIG_HOME/dev-orchestra/config.yaml`, else `~/.config/dev-orchestra/config.yaml` |
| macOS | `~/.config/dev-orchestra/config.yaml` |
| Windows | `%APPDATA%\dev-orchestra\config.yaml` |

Environment overrides:

- `DEV_ORCHESTRA_CONFIG` — use this exact file as the global layer.
- `DEV_ORCHESTRA_HOME` — use this directory instead of the platform default.
- `DEV_ORCHESTRA_NO_USER_PROVIDERS` — any value but empty or `0` skips the
  user adapter directory (`<config dir>/providers/`) entirely.

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

reviewers:                    # 0..n independent reviewers; two or more recommended
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
  design:
    enabled: false                    # review .ai/plan.md before implementing
    max_iterations: 2                 # design review -> revise -> re-review

design:
  require_approval: true              # implementer waits for the user's yes (design approve)
  resume:                             # run architect --resume
    max_age_seconds: 3600             # older than this, the revision runs fresh
    max_context_tokens: null          # no cap on the context a resumed session carries

workspace:
  dir: .ai                            # relative to the repo root, or absolute
```

### Field reference

| Field | Type | Notes |
| --- | --- | --- |
| `version` | int | Must be `1`. |
| `<role>.provider` | string | A registered adapter: `claude`, `codex`, `mock`, or a user adapter (see `references/providers.md`). |
| `<role>.model.family` | string | A family/alias the provider can resolve (`opus`, `sonnet`, `fable`, `recommended-coding`). Omit or use `default` to let the CLI choose. |
| `<role>.model.version` | `latest` \| `pinned` | `latest` re-resolves on every run. `pinned` requires `model.id`. |
| `<role>.model.id` | string | Exact model id, only with `version: pinned`. |
| `reviewers[].id` | string | Unique, matching `[a-z0-9][a-z0-9._-]*`. Names the report file. |
| `reviewers[].role` | string | Built-in or your own; see `references/reviews.md`. |
| `reviewers[].when` | `always` \| `high-risk` \| mapping with `paths` | When the reviewer runs on a **code** review round (default `always`). `high-risk` joins only the rounds judged high-risk; a mapping with `paths` joins only the rounds whose change matches one of its own patterns. The design review ignores both and runs every reviewer. At least one reviewer must stay `always`. See [Reviewers that run only on high-risk changes](#reviewers-that-run-only-on-high-risk-changes) and [Reviewers scoped to paths](#reviewers-scoped-to-paths). |
| `review.max_review_iterations` | int ≥ 0 | Rounds per review, not per project: the count restarts on a new branch, a new `--base`, or `budget reset`. `0` disables re-review entirely. |
| `review.parallel` | bool | `false` runs reviewers one at a time (easier to debug). |
| `review.re_review_severities` | list | Severities that count as blocking. |
| `review.timeout_seconds` | int > 0 | Per-run timeout; a timeout is reported, not raised. |
| `review.exclude` | list | Glob patterns whose diff body is withheld from reviewers. Replaces the default list wholesale; `[]` reviews everything. |
| `review.incremental_rounds` | bool | `true` (default) makes a second round diff against what the first round reviewed, carrying the findings the fix was meant to address. `false` re-diffs the whole change every round. |
| `review.max_findings` | int \| null | How many findings each reviewer is asked for. `null` (default) lets `optimization.level` decide, `0` lifts the cap. Findings that come back over the cap are kept, never trimmed. |
| `review.context.max_chars` | int ≥ 1 \| null | The largest change body a review round will send at all: the diff, or the plan plus the request it answers (default 400,000 ≈ 100k tokens, four times the largest prompt recorded here — it refuses nothing anyone has run). Over it, `review run` exits 3 without reviewing anything; the ways under it are `--base`, `review.exclude`, splitting the change, or a shorter plan. `--force` is a human's override, and records the round as over budget. `null` means the default, as it does on `review.max_findings`; there is no value that switches the limit off. See `references/limits.md`. |
| `review.context.inline_chars` | int ≥ 1 \| null | How much of the change body goes into the reviewer's prompt (default 400,000, the same number as `max_chars`). At or under it the body is inlined and the round can be clean; over it the reviewer is handed the path of the frozen snapshot and the round is recorded `partial` — coverage unverified — whatever comes back. **Setting it below `max_chars` opens a band between the two where rounds run and are recorded `partial`**: that is the explicit choice of somebody who will not pay for very large prompts, and `partial` is what it costs. Setting it *above* `max_chars` is also allowed and is not a mistake — it means the body is only ever handed over as a file on a round a human forced. `null` means the default. Every round records the number it was measured against, so a `partial` round says which limit made it one. See `references/limits.md`. |
| `review.context.surrounding` | `none` \| `enclosing` | `enclosing` also hands every code reviewer the Python function, method or class enclosing each hunk, extracted from the snapshot's git tree when the snapshot is taken (default `none`: the diff alone). Off, because measured on one snapshot it did not make a review cheaper — `optimization report` compares rounds with and without it. `false` and `null` mean `none` (`off` reads as `false`); `true` is refused. See `references/reviews.md`. |
| `review.context.surrounding_chars` | int ≥ 1 \| null | The most surrounding context a round may add (default 15,000: measured on one snapshot, it left the cost per run where it was, while 60,000 added what it carried). Capped further by what the diff leaves under `max_chars` and `inline_chars`, so context never refuses a round or sends a diff over as a file. What does not fit is left out by name, in the prompt and in every report. `null` means the default. See `references/limits.md`. |
| `review.design.enabled` | bool | `false` (default) skips the design review entirely. `true` puts `.ai/plan.md` in front of the same panel before implementation; the stage costs a reviewer run per panel member per round, which is why it is opt-in. |
| `review.design.max_iterations` | int ≥ 0 | Design review rounds (review → triage → revise), counted apart from `max_review_iterations` (default 2). The round that reaches the limit still gets its revision; the limit refuses only the re-review after it. `1`: one round, one revision, then ask. `0`: no design review. `budgets.architect` (default 3) covers the design plus one revision per round at the default; raise it with `max_iterations`, and by one more if changes asked for at approval are expected. |
| `design.require_approval` | bool | `true` (default) makes `run implementer` refuse (exit 5) while `.ai/plan.md` exists and the plan as it is now has not been approved with `design approve` -- after the user said yes. `false` is for runs nobody is watching (CI, batch), and restores the behaviour from before the gate existed. Top-level rather than under `review.design`: approval matters whether or not the panel reviewed the plan. `--force` does not bypass it; only this setting does. |
| `design.resume.max_age_seconds` | int ≥ 0 \| null | How old the last architect run may be for `run architect --resume` to continue its session (default 3600, how long the CLI kept its prompt cache when this was measured). Older, the revision runs fresh with the full prompt. `0` always runs fresh; `null` means the default. |
| `design.resume.max_context_tokens` | int > 0 \| null | The largest context, in tokens, a session may have ended with and still be continued (default `null`: no cap). Every run records its `context_tokens`, so a cap can be set from what was measured. Setting one of the two keeps the other's default. |
| `optimization.level` | `aggressive` \| `balanced` \| `quality` | How hard to try to be cheap. Default `balanced`. See below. |
| `optimization.high_risk_paths` | list | Globs that force `quality` for a change touching them. Replaces the default list wholesale. |
| `optimization.extra_high_risk_paths` | list | Globs added to `high_risk_paths` rather than replacing it (default `[]`). A hit escalates exactly as a `high_risk_paths` hit does. Like every list, a project value replaces a global one. |
| `optimization.low_risk_max_files` | int | Below `quality`, at most this many files still counts as a small change (default 5). |
| `optimization.low_risk_max_lines` | int | And at most this many changed lines (default 150). |
| `workspace.dir` | string | Where `.ai/` artifacts go. |
| `workspace.stale_notice_days` | int 0–36500 | When a new workflow starts, its first command notes, once and on stderr, the other workflows whose last activity (`updated_at`, else `started_at`, in `state.json`) is this many days old or more (default 30). The current workflow is left out, and so is any workflow with a stage in flight: that mark clears only when that workflow itself runs `status`, so a workflow abandoned mid-stage is never named here; `workflow list` shows it as `in flight`. A workflow with no usable timestamp, or an unreadable `state.json`, is not counted. Nothing is deleted: `workflow remove <id> --yes` is still the only thing that deletes one. `0` turns the note off; `null` means the default. The note never changes what the command does. |
| `<role>.options` | mapping | Provider-specific knobs; see below. |
| `<role>.model_tiers` | mapping | Named alternatives for this role's model; see below. Optional. |

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

On a read-only role (the orchestrator, the architect, their tiers, every
reviewer) `--add-dir <path>` is the only raw argument Claude accepts, and Codex
accepts none. It is also taken only from the global config or from `--extra`:
the same `args` in the project file makes that role's runs refuse, whatever
they hold, because the project file can come with the branch under review, and
a branch that names its own reviewers' directories can widen what they read. An
implementer or review fixer takes `args` from either file, as before.

**Options that would loosen a read-only stage are ignored or refused.** The
orchestrator, the architect and every reviewer always run read-only, whatever
`permission_mode` or `sandbox` says -- that invariant is what makes an
independent review worth anything. `dev-orchestra doctor` lists any option it
is ignoring for that reason rather than dropping it silently. Any other raw
argument in `options.args` makes that role's runs refuse (exit 2, and a failed
reviewer in a review round); `config validate` and `doctor` warn about it
before a run does.

The alternative to loosening a permission mode is allow-listing the specific
commands in the CLI's own settings (for Claude Code, a `permissions.allow` entry
such as `Bash(pytest:*)` in `.claude/settings.json`). That is narrower, and it
lives with the project rather than with this skill.

Read-only Claude runs pass `--restricted`, so project and user `settings.json`
files are not read there at all. **`permissions.deny` is not read either**: a
deny rule such as `Read(./.env)` that keeps the model away from a secret in the
repository does not apply to architect or reviewer runs. Managed settings still
apply, so move such rules there. The implementer and the review fixer read
their settings files as before. Those read-only runs read only inside the
working directory and `--add-dir`: with the workspace container outside the
repository (`workspace.dir` set to an absolute path), add `--add-dir <container>`
to that role's `options.args` in the global config, or pass it with `--extra`.

`mock` is a real, registered provider: an offline adapter used by the tests and
useful for dry-running the pipeline without spending tokens. It is hidden from
the setup wizard.

## Optimization level

One dial over three savings. Default `balanced`.

| | `aggressive` | `balanced` | `quality` |
| --- | --- | --- | --- |
| Tests recorded as failing | refuse | refuse | review anyway |
| No test result recorded | warn, review | warn, review | review |
| Small, low-risk change | 1 reviewer | 1 reviewer | whole panel |
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

**`balanced` reduces the panel too, as of 0.4.2.** Restricting that to
`aggressive` made it unreachable in the repositories that most needed it: a
high-risk match escalates to `quality`, and `quality` is not `aggressive`, so
in an infrastructure repository where `*.tf` matches on most rounds the dial
could not fire at all. Measured over eleven real rounds at `balanced`: the
panel was reduced zero times, and no round came close to the old 2 file / 50
line thresholds either. Both were raised at the same time.

`quality` is now the only level that always pays for the whole panel, which is
what that level means.

**Size is never the only test.** The reduced panel needs the change to be
under both thresholds *and* to touch nothing high-risk, because one line in an
auth file is the exact shape an authorisation bug arrives in.

```yaml
optimization:
  level: aggressive
  low_risk_max_files: 3
  low_risk_max_lines: 80
```

### Reviewers that run only on high-risk changes

A specialist such as a `security` reviewer can be made to join the code
review only when the round is judged high-risk, and cost nothing otherwise:

```yaml
reviewers:
  - id: claude-general
    provider: claude
    role: general            # when: always (default)
  - id: claude-security
    provider: claude
    role: security
    when: high-risk          # always (default) | high-risk; code review only
optimization:
  extra_high_risk_paths: ["*/providers/*", "*/config.py"]   # added to high_risk_paths; default []
```

**A `when: high-risk` reviewer runs on a code round when the change matches a
high-risk path, when the orchestrator declares the round high-risk with
`review run --high-risk`, or when it must re-check its own open accepted
finding on an incremental round or a re-run of the same snapshot. Otherwise it
is left out.** On the design
review it always runs, and `--only` naming it runs it. The judgement is the one
that escalates a round to `quality`; every decision to add or leave out a
conditional reviewer is printed and recorded with its reason. See
`references/reviews.md` for how a round decides.

Two rules keep such a panel able to review anything, and `config validate`,
`doctor` and `review run` all refuse a configuration that breaks either:

- **At least one reviewer must run always.** A panel where every reviewer is
  `high-risk` would leave a quiet round with nobody to review it.
  `reviewer set --when high-risk` on the last unconditional reviewer, and
  `reviewer remove` of it, are refused.
- **A pattern must be in force.** `high_risk_paths: []` with no
  `extra_high_risk_paths` leaves a `high-risk` reviewer nothing to be judged by,
  so it would never run.

`extra_high_risk_paths` adds to whichever `high_risk_paths` list is in force, so
a repository can name the one path the defaults miss without copying the thirty
they already cover. **Every hit on an extra pattern escalates the round to
`quality` and lets a red tree through the gate, exactly as a `high_risk_paths`
hit does**, so add only paths whose changes deserve the whole panel. Between
config layers it replaces like every list: a project that sets it discards the
global value.

Roll it out in this order: add `extra_high_risk_paths` for the paths the
defaults miss first, then switch a reviewer to `when: high-risk`. The other way
round, the reviewer sits out the rounds the new patterns were meant to catch.
`doctor` notes a `high-risk` reviewer that is judged by the built-in patterns
alone, so a switch made without that first step does not go unnoticed.

```bash
dev-orchestra reviewer set claude-security --when high-risk
dev-orchestra reviewer set claude-security --when always      # back to every round
```

#### Reviewers scoped to paths

A domain specialist, such as a `database` reviewer, can be made to join the
code review only when the change touches the files it knows about:

```yaml
reviewers:
  - id: codex-database
    provider: codex
    role: database
    when:
      paths:
        - "*migration*/*"
        - "*migrate*/*"
        - "*.sql"
```

**A path-scoped reviewer joins a code round when a changed path matches one
of its own patterns, when it must re-check its own open accepted finding, or
when `--only` names it, and nothing else adds it.** Like any conditional
reviewer, it runs on every design review, it keeps the panel whole when it
joins, and every decision to add or leave it out is printed and recorded with
its reason. It counts as conditional for the "at least one reviewer must run
always" rule, and it needs no `high_risk_paths` pattern in force, since it
brings its own.

- **It is for domain specialists, not for roles that judge risk.** A database,
  frontend or docs reviewer is interested in a set of files; a security
  reviewer is interested in how risky the round is. Keep security and other
  risk-judging roles `when: high-risk` (or `always`): scoped to paths, a
  security reviewer sits out a change to `.env`, `*secret*`, `*.tf` or
  `.github/workflows/*` even though the round escalates. Membership that is
  both "on high-risk rounds" and "on these paths" is not offered. Do not get it
  by adding a specialist's path to `extra_high_risk_paths`: that changes the
  risk policy, escalating every such change to `quality` and letting a red
  tree through the gate for a path only one reviewer cares about.
- **A match never escalates.** The reviewer's patterns say what it knows, not
  how dangerous the change is, so a hit does not move the level or the gate.
  Escalation stays with `high_risk_paths` and `extra_high_risk_paths`. When a
  pattern is in both (the defaults include `*.sql`, `*migration*/*` and
  `*migrate*/*`), the round escalates through the high-risk list and the
  reviewer joins through its own, each with its own note.
- **A high-risk round does not add it.** A high-risk path hit and
  `review run --high-risk` add the `when: high-risk` reviewers only. When
  either happens and the path-scoped reviewer is left out, its note says so.
  `--only <ids>` naming every reviewer that should run includes it for one
  round; `reviewer set <id> --when high-risk` is the lasting fix.
- **It is matched against the change a reviewer sees**: the reviewed files,
  withheld files and rename sources, but not the orchestrator's own files or
  untracked files left out of an incremental round, which a reviewer is never
  shown.

The patterns are matched the way `high_risk_paths` is:

- against the whole path, and, for a pattern with no `/`, against the basename
  too, so `*.sql` matches at any depth;
- with no `**`: `db/**/*.sql` behaves as `db/*/*.sql`, and a directory pattern
  needs both forms (`migrations/*` and `*/migrations/*`), or `*migration*/*`;
  note that `*migration*/*` does not match `migrate`;
- **case-sensitively**, so list both cases when a repository mixes them;
- as written: a pattern that can never match is not reported.

Write the mapping in block form, as above, one pattern per line and every
pattern that starts with `*` in quotes. An unquoted `*.sql` is read as a YAML
alias by PyYAML, and without PyYAML the bundled parser refuses an inline
mapping and any pattern containing `[` inside a flow list, quoted or not:

```yaml
when:
  paths:
    - "*.sql"
    - "*.SQL"
    - "*[Mm]igration*/*"
```

`reviewer add` and `reviewer set` write this form for you. Quote each pattern
on the command line too, so the shell does not expand it:

```bash
dev-orchestra reviewer add --provider codex --role database --when-paths "*migrate*/*" "*.sql"
dev-orchestra reviewer set codex-database --when-paths "*.sql"   # replaces the list
dev-orchestra reviewer set codex-database --when always          # back to every round
```

## Model tiers

The same role, the same prompt, a different model behind it. A one-line fix
handed to something cheap, a gnarly design handed to something expensive, a
second opinion handed to another vendor:

```yaml
implementer:
  provider: claude
  model:
    family: opus
    version: latest
  model_tiers:
    light:
      model:
        family: sonnet
        version: latest
    second-opinion:
      provider: codex
```

```bash
dev-orchestra run implementer --tier light --prompt-file .ai/execution/fix.md
```

A tier is a per-role override rather than a second role, because keeping two
definitions of what the implementer *is* would be the expensive part. Only
`provider`, `model` and `options` can be overridden; anything else is a
configuration error rather than a silently ignored key.

**Who picks is the caller.** Nothing infers a tier from the size of a diff.
The orchestrator is the only thing that knows how hard the task is, and a
wrong guess spends exactly what tiers exist to control.

**An unknown tier is an error, never a fallback.** A tier quietly ignored runs
the work on the default model and says nothing -- the expensive one when you
asked for cheap, or the cheap one when you asked for care.

**Each key is replaced whole, not merged into.** A tier naming a family over a
base pinned to an id would otherwise inherit the pin and run a model nobody
asked for. Changing `provider` drops the previous provider's `model` and
`options` with it: `opus` means nothing to Codex, and `permission_mode` is not
a thing it has. A tier that wants something specific on the new provider says
so.

Tiers are shown by `config show`, and a tiered run is recorded against its own
line in `tokens show`, so the question a tier raises -- did the cheaper one
actually cost less -- has an answer.

Reviewers have no tiers. The panel is already one model per reviewer, which is
the same routing by another name.

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

The adapter checks, in order:

1. What the installed CLI advertises — `claude --help` for aliases,
   `codex debug models` plus `$CODEX_HOME/config.toml` for Codex.
2. The provider's current aliases.
3. A built-in fallback list of **families only**, carrying the date it was last
   checked.

If none of those can verify the family, the run stops with an explanation.
`model list` shows what each source offers on this machine:

```bash
dev-orchestra model list
```

```
claude: installed
  fable    family=fable    source=cli-help
  opus     family=opus     source=cli-help
  sonnet   family=sonnet   source=cli-help
codex: installed
  CLI default (recommended coding model)  family=recommended-coding  source=cli-default
  gpt-6-astra (this CLI's configured model) family=gpt-6-astra       source=cli-config
  GPT-5.6-Terra                           family=gpt-5.6-terra       source=cli-catalog
```

`recommended-coding` resolves by *omitting* the `-m` flag — the CLI's own
current default is, by definition, current. Any other Codex family has to
appear in that listing: the adapter accepts the model the CLI is configured
with and the slugs its own catalogue publishes, and refuses everything else.
Adapter detail is in `references/providers.md`.

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
| "review the design too" | `config set review.design.enabled true` |
| "don't ask me to approve plans" / running in CI | `config set design.require_approval false` |
| "add a Codex security reviewer" | `reviewer add --provider codex --role security` |
| "run the security reviewer only on risky changes" | `reviewer set <id> --when high-risk` (see [above](#reviewers-that-run-only-on-high-risk-changes)) |
| "make it three reviewers" | `reviewer add …` again, then `reviewer list` |
| "remove the performance reviewer" | `reviewer remove performance` |
| "change the second reviewer" | `reviewer set 2 --provider … --role …` |
| "just this project" | add `--scope project` to any write |
| "undo my own settings" | `config reset --scope global` (clears your overrides; the built-in defaults are what is left) |
| "drop this project's overrides" | `config reset --scope project` (the project then follows the global layer) |
| "my config is from an old version" | `config prune --dry-run`, then `config prune` |
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
dev-orchestra config reset                   # clear this layer's overrides
dev-orchestra config reset --delete          # remove the file entirely
dev-orchestra config prune                   # drop values equal to what is inherited
dev-orchestra config validate
```

**A saved config holds only what you set.** Everything else is resolved from
the layer below when the config is loaded, so a default improved in a later
release reaches your installation instead of being shadowed by the copy that
was current the day you ran setup. `config setup --defaults` therefore writes
`version: 1` and nothing else: choosing the recommended configuration is
choosing to override nothing. `config show --scope global|project` prints the
layer exactly as it is on disk, and `config show` the configuration it resolves
to.

**Files written before 0.6.0 still hold every default**, which is how the
low-risk thresholds raised in 0.4.2 failed to reach anyone who had run setup
before them. `doctor` lists any setting whose value the built-in default has
moved off, with both numbers. `config prune` drops the values equal to what the
layer inherits, on request only -- a deliberate choice and an inherited default
look identical on disk, so this reads equality as evidence and says so:

```bash
dev-orchestra config prune --dry-run         # list what would be dropped
dev-orchestra config prune --scope project
```

A project file is pruned against *your* global layer, not against the built-in
defaults, so a value placed there to cancel a global one survives. The other
side of that: prune a `.dev-orchestra.yaml` the team shares only while your own
global layer overrides nothing, or the result will be shaped by your machine.

`config set` coerces values: `3` becomes an int, `true` a bool, `[a, b]` a list,
anything else a string. Use `--raw` to force a string.

Writes go to the project layer when one exists, otherwise the global layer;
`--scope global|project` decides explicitly. **Editing one entry of a list
writes the whole list**, because a list replaces the one below it wholesale:
`reviewers[1].role`, `review.exclude[0]` and `optimization.high_risk_paths[2]`
all copy the rest of the list from the layer below first -- the built-in
defaults for the global layer, the global layer for a project one. A project's
panel therefore never ends up in your global file. An index past the end of the
list is an error (exit 2), not a new entry.

### The wizard

On first use the skill notices there is no configuration and runs it:

```
AI Development Orchestrator setup

Detected CLIs:
  claude:  installed
  codex:   installed

1. Orchestrator
   CLI:
     1) Claude Code (2.1.x) (recommended)
     2) Codex CLI (0.154.x)
   Model:
     1) sonnet [cli-help] (recommended)
     2) opus [cli-help]
     3) fable [cli-help]
     4) custom (type a family or exact model id)
...
5. External Reviewers
   How many reviewers? [2]
   reviewer #1  CLI / Model / Review role / id
   reviewer #2  CLI / Model / Review role / id
   Add another reviewer? [y/N]

Configuration
  Orchestrator    claude / sonnet / latest
  Architect       claude / fable  / latest
  Implementer     claude / opus   / latest
  Review Fixer    claude / opus   / latest
  Reviews
    1. claude / opus / latest / general / claude-general
    2. codex / recommended-coding / latest / general / codex-general

Save configuration? [Y/n]
```

`config setup` saves the answers you gave and nothing else. Its recommended
answers, and the summary you approve, come from whatever the layer you are
editing would inherit: setting up a project layer over a global one that chose
sonnet offers sonnet, and the summary shows the design review as on if your
global layer turned it on. So what you see before saving is what `config show`
reports afterwards.

An answer you accepted by pressing enter is still an answer, and is saved. For
the four roles that is visible later -- `doctor` reports a role whose family the
built-in default has moved off. **For `reviewers` it is not**: `doctor` never
compares a panel to the default one, because that would flag every installation
that added a reviewer. A panel written by the wizard therefore stays as it was
that day, silently. The two ways back are `config show --scope global|project`,
which shows the panel sitting in the layer, and `config prune`, which drops it
when it still equals the current default panel. If you want to override nothing
at all, `config setup --defaults`.

## Worked examples

**One vendor designs, another reviews** — a lineup that maps onto what the two
CLIs offered when this was written:

```yaml
version: 1

orchestrator:
  provider: codex
  model:
    family: gpt-5.6-sol
    version: latest

architect:
  provider: claude
  model:
    family: fable
    version: latest

implementer:
  provider: claude
  model:
    family: opus
    version: latest

review_fixer:
  provider: claude
  model:
    family: opus
    version: latest

reviewers:
  - id: claude-review
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-independent
    provider: codex
    model:
      family: gpt-5.6-terra
      version: latest
    role: general
```

Check those families with `dev-orchestra model list` before copying them: model
names change, and the two CLIs offer different ones per version and account.
Every adapter refuses a family the installed CLI does not vouch for instead of
guessing, so a stale name fails loudly during setup rather than quietly running
something else. The particular families matter less than the shape: two models
from the same family share the blind spot that produced the bug.

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
