# Configuration

## Where it lives

| Layer | Path | Purpose |
| --- | --- | --- |
| Project | `<repo>/.ai-orchestrator.yaml` | Per-repository override, committed or not as you prefer |
| Global | see below | Your personal default for every project |
| Built-in | `scripts/orchestrator/config.py` | Recommended defaults, used when no file exists |

Global config path by platform:

| Platform | Path |
| --- | --- |
| Linux / BSD | `$XDG_CONFIG_HOME/ai-dev-orchestrator/config.yaml`, else `~/.config/ai-dev-orchestrator/config.yaml` |
| macOS | `~/.config/ai-dev-orchestrator/config.yaml` |
| Windows | `%APPDATA%\ai-dev-orchestrator\config.yaml` |

Environment overrides:

- `AI_ORCHESTRATOR_CONFIG` — use this exact file as the global layer.
- `AI_ORCHESTRATOR_HOME` — use this directory instead of the platform default.

`ai-orchestrator config path` prints both resolved locations.

The project file is found by walking up from the current directory and stopping
at the git root, so running the CLI from a subdirectory still finds it.
Accepted names, in order: `.ai-orchestrator.yaml`, `.ai-orchestrator.yml`,
`.ai-orchestrator.json`.

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
| `workspace.dir` | string | Where `.ai/` artifacts go. |

`mock` is a real, registered provider: an offline adapter used by the tests and
useful for dry-running the pipeline without spending tokens. It is hidden from
the setup wizard.

## Model families and version policy

Configuration stores **what kind of model you want**, not **which snapshot you
got**. `family: opus` + `version: latest` means "the newest Opus the installed
CLI offers", so the setup keeps working after a model release.

```yaml
# tracks the latest Opus, whatever that is today
model: {family: opus, version: latest}

# freezes an exact model - only do this deliberately
model: {family: opus, version: pinned, id: claude-opus-5}

# let the CLI pick entirely
model: {family: default, version: latest}
```

Resolution happens at run time in the provider adapter, and an adapter that
cannot verify a family raises `ModelResolutionError` rather than sending a
guessed name to the CLI. The *resolved* id is recorded in `.ai/state.json` for
traceability; the config file keeps the family.

## Editing

```bash
ai-orchestrator config show                    # effective configuration
ai-orchestrator config show --scope project    # just the project layer
ai-orchestrator config setup                   # interactive wizard
ai-orchestrator config setup --defaults        # non-interactive, recommended values
ai-orchestrator config set implementer.model.family sonnet
ai-orchestrator config set --scope project architect.provider codex
ai-orchestrator config set reviewers[1].role security
ai-orchestrator config reset                   # back to recommended defaults
ai-orchestrator config reset --delete          # remove the file entirely
ai-orchestrator config validate
```

`config set` coerces values: `3` becomes an int, `true` a bool, `[a, b]` a list,
anything else a string. Use `--raw` to force a string.

Writes go to the project layer when one exists, otherwise the global layer;
`--scope global|project` decides explicitly. Editing reviewers in a project layer
that has none seeds it from the effective list first, so you edit the panel you
actually see.

## Worked examples

**A repo where the whole team should use the same panel** — commit
`.ai-orchestrator.yaml`:

```yaml
version: 1
reviewers:
  - id: claude-general
    provider: claude
    model: {family: opus, version: latest}
    role: general
  - id: codex-security
    provider: codex
    model: {family: recommended-coding, version: latest}
    role: security
  - id: codex-database
    provider: codex
    model: {family: recommended-coding, version: latest}
    role: database
```

**Only one CLI installed** — drop the other provider everywhere:

```bash
ai-orchestrator config set architect.provider claude
ai-orchestrator reviewer remove codex-general
ai-orchestrator reviewer add --provider claude --role security
```

**A cheap, fast loop for a small repo**:

```bash
ai-orchestrator config set implementer.model.family sonnet
ai-orchestrator config set review.max_review_iterations 1
ai-orchestrator reviewer remove 2
```

**No reviews at all** (valid, and `doctor` will point it out):

```bash
ai-orchestrator reviewer remove 1
ai-orchestrator reviewer remove 1
```

## YAML dialect

Config files are parsed by PyYAML when it is installed, and otherwise by a
built-in parser covering block mappings, block sequences, inline empty
collections, inline scalar lists, comments, and quoted strings. Anchors,
aliases, multi-document streams, and block scalars (`|`, `>`) are rejected with
a clear error. JSON is always accepted.
