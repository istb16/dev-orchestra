# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The public surface covered by that promise is: the configuration schema, the
`dev-orchestra` commands and flags, and the `.ai/` artifact formats.

## [Unreleased]

### Added

- **Stall detection and loop budgets.** Two failure modes were effectively
  undefended: a delegated agent that stops responding without anyone noticing,
  and a loop (review→fix→re-review, or the quieter fix→test→fix) that never
  ends. See `references/limits.md`.
  - Runs now go through a new execution layer that gives each child its own
    process group and kills the whole group on a breach, so `claude`'s and
    `codex`'s own children are not left as orphans -- and it can never block on
    a pipe a survivor still holds, which `subprocess.run` could.
  - A second, much shorter **idle deadline** (`review.idle_timeout_seconds`,
    300s) treats a run that has produced no output as wedged rather than slow,
    catching a stall in minutes instead of at the 1800s cap. It is applied only
    to providers measured to stream progress: `codex exec` emits output 0.4s
    into an 11.5s run, while `claude -p --output-format text` is silent until
    8.1s of an 8.9s run, so an idle deadline there would kill healthy runs.
  - Stages are recorded **before** they start, with a deadline and a pid, so a
    stall is visible from outside the blocked process and a stage that died
    without recording an outcome is detected and marked `abandoned` instead of
    leaving no trace.
  - New `budgets` config section, enforced by the actions themselves with exit
    code 3: attempts per stage, total delegated runs, and wall-clock runtime.
    `review run` now *refuses* a round past `review.max_review_iterations`
    rather than having `review status` advise against it.
  - **No-progress detection**: `progress record` (and reviews, automatically)
    register a signature, and the same outcome twice in a row refuses the next
    attempt -- the last fix changed nothing, which is a better stop signal than
    a budget because it arrives sooner.
  - New `dev-orchestra status`: one `continue` / `stop-and-report` verdict over
    budgets, stalls and open findings.

- **Claude runs stream now.** Its adapter asks for `--output-format stream-json`
  instead of `text`, so the idle deadline has something to watch: the text
  format prints nothing until a run is nearly over (first output 8.1s into an
  8.9s run), which made a wedged agent indistinguishable from a busy one. The
  final answer comes from the `result` event, degrading to assistant text blocks
  and then raw stdout so a schema change cannot lose the output.
  `options.output_format: text` opts back out.
- **Detached runs** (`run --detach`, `jobs list|show|wait|cancel`). Deadlines
  bound how long an agent misbehaves, but while one runs the caller is inside
  that call -- for an orchestrator that is itself an agent, a long block is
  indistinguishable from a crash. A detached run returns a job id immediately
  and `jobs wait` polls with a deadline of its own, so the worst case is a
  bounded wait rather than an open-ended block. A job whose worker died without
  recording an outcome is reported as `abandoned`.



### Fixed

A self-review of the first release found eight issues; four of them were the
same blind spot, that the test suite only ever exercised a single review round.

- **The re-review budget could never stop a loop.** `review run` defaulted
  `--iteration` to 1, so an orchestrator that omitted the flag - as the default
  invites - left the counter at 1 forever and `review status` kept recommending
  another round. The round is now derived from the snapshot: a new snapshot is a
  new round, re-running the same one is not, and `--iteration` only overrides.
- **Triage decisions were silently lost between rounds.** They were restored by
  the positional `F1..Fn` id, which is reassigned every consolidation, so fixing
  a critical finding renumbered everything below it and reverted those decisions
  to `needs-triage` - bringing rejected false positives back as blocking. They
  are now keyed by content (file + normalised problem text).
- **A report that could not be parsed was reported as a clean review.** Only
  `#`-style "Finding" headings were recognised, so a reviewer emitting
  `**Finding 1**` produced zero findings with no `NO_FINDINGS` sentinel and the
  change looked clean. Headings in any emphasis are now accepted, a report that
  lost its headings entirely is recovered from its `Severity:` lines, and
  anything still unreadable is recorded as `unparsed` and counted as failed.
- **`review run --only` discarded the other reviewers' findings and triage**,
  because it rebuilt the consolidated report from the subset alone. It now
  consolidates every configured reviewer's current report.
- **Stale reviewer reports were consolidated as if current.** Reports are
  stamped with their snapshot; one written against an earlier snapshot is now
  skipped with a note instead of handing the fixer already-fixed issues.
- **The setup wizard could save a config that broke every later command.** It
  accepted duplicate reviewer ids and saved despite failed validation, after
  which every `run` / `review run` exited 2. Duplicate and malformed ids are
  now rejected while the user is still there, and an invalid config is never
  written.
- **A relative `--cwd` was applied twice** - once by `chdir`, then again as the
  config search-start path - so `--cwd ..` missed the project override and could
  target the wrong repository. It is resolved to an absolute path first.
- **Documented YAML could not be pasted into a config file.** The examples in
  both READMEs and `references/` used flow mappings, which the bundled parser
  rejects, so they worked only where PyYAML happened to be installed. All
  blocks are block-style now, and a test parses every documented YAML block
  with the bundled parser.

### Changed

- Renamed every user-visible identifier to `dev-orchestra`: the skill name, the
  `dev-orchestra` command, the config directory, the `.dev-orchestra.yaml`
  project override, and the `DEV_ORCHESTRA_*` environment variables. Three
  competing names for one tool was one too many.

### Fixed

- Two tests silently depended on `claude` and `codex` being installed, so they
  passed locally and failed on every CI platform. Both now drive the `mock`
  provider instead, and `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI=1` reproduces the CI
  environment on a developer machine that has the real CLIs.
- `doctor` now reports options that a read-only role will ignore even when the
  provider CLI is missing: whether an option takes effect is a fact about the
  configuration, not about what happens to be installed.

### Added

- **Role options.** Each role can carry provider-specific `options`:
  `permission_mode` and `args` for Claude, `sandbox` / `approve` / `args` for
  Codex. The adapter validates them against what the installed CLI actually
  accepts, so `config validate` catches a typo instead of a run failing later.
  This exists because the default `acceptEdits` auto-approves file edits but
  not shell commands, which can stop an Implementer from running the tests it
  was told to run. Options that would loosen a read-only stage are ignored and
  reported by `doctor` — the architect and every reviewer stay read-only.
- **Duplicate candidates.** Findings from different reviewers that quote the
  same code are reported as possible duplicates, ranked by how rare the shared
  code token is, for the orchestrator to confirm as `duplicate` during triage.
  Auto-merge stays conservative: measured on real two-provider output over one
  diff, a confirmed duplicate pair scored 0.03 text similarity while an
  unrelated pair scored 0.29, so no threshold on wording can separate them —
  and a wrong merge hides a bug, while a missed one only costs redundant work.
- Japanese README (`README.ja.md`), linked from the English one and checked by
  skill validation.

## [0.1.0] - 2026-09-10

First release.

### Added

- **Orchestrated workflow skill** (`SKILL.md`) covering design, implementation,
  test, independent review, triage, fix and re-test — with stage selection left
  to the orchestrator rather than made mandatory.
- **Provider adapters** for Claude Code (`claude` 2.1.x) and Codex CLI
  (`codex` 0.154.x), plus an offline `mock` adapter for tests and dry runs.
  Adding a CLI is one module and one `register()` call.
- **Model handling without dated snapshot ids**: configuration stores a family
  plus `version: latest`, resolved at run time against the installed CLI.
  Claude aliases come from the CLI's own `--model` help; the Codex
  `recommended-coding` family resolves by omitting `-m`. Unverifiable families
  raise instead of being guessed.
- **Layered configuration**: project `.dev-orchestra.yaml` over an
  OS-appropriate global file over built-in defaults, with `DEV_ORCHESTRA_HOME`
  and `DEV_ORCHESTRA_CONFIG` overrides.
- **Setup wizard**, interactive or `--defaults`, covering all four roles and an
  arbitrary reviewer panel.
- **Independent review pipeline**: frozen git snapshot (including untracked
  files), parallel read-only reviewers, tolerant finding parser, locus+text
  deduplication, triage states, accepted-findings fix brief, and an iteration
  budget that stops review loops.
- **`dev-orchestra` CLI**: `config`, `model`, `reviewer`, `doctor`, `run`,
  `review`, `state`, `summary`.
- **Doctor** diagnostics for CLI presence, version, credential *presence*,
  model resolution per role, and configuration validity.
- **Credential redaction** on every captured stream, and read-only enforcement
  for planning and review runs.
- Installers for Claude Code (skills directory) and Codex CLI (marked
  `AGENTS.md` pointer), keeping `SKILL.md` as the single source of truth. A
  per-project install also excludes itself via the target repo's
  `.git/info/exclude`, so the nested checkout stays out of that repo.
- 157 tests covering config layering, validation, wizard flows, adapters,
  review parsing/dedup/triage, partial reviewer failure and secret redaction —
  none of which invoke a real CLI.
- CI on Linux, macOS and Windows: lint, tests, skill validation.

[Unreleased]: https://github.com/istb16/dev-orchestra/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/istb16/dev-orchestra/releases/tag/v0.1.0
