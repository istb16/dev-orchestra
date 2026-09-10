# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The public surface covered by that promise is: the configuration schema, the
`dev-orchestra` commands and flags, and the `.ai/` artifact formats.

## [Unreleased]

### Added

- **Re-review diffs only the fix.** A snapshot now records the working tree as
  a git tree object, and the next round diffs against the tree the previous
  round actually reviewed instead of re-diffing everything against `HEAD`.
  Measured on a 60-function change followed by a one-line fix, round 2's diff
  went from 8,617 to 199 bytes -- and that is per reviewer.

  The premise travels with it, because a reviewer is stateless and sees no
  other reviewer's output: a fix diff on its own is a change with no stated
  purpose. So an incremental round also carries the accepted findings the fix
  was meant to address (one line each, about 80 tokens in total), a pointer to
  the frozen whole change at `.ai/reviews/review-target-full.diff`, and an
  instruction not to assume a listed finding was real. Who reported what is
  left out, so reviewers still never see each other's output.

  The scope only narrows when there is a round to be incremental to. A
  re-snapshot of a round nobody reviewed, a working tree that has not changed
  since, an explicit `--base`, `--full`, or
  `review.incremental_rounds: false` all take the whole change -- without the
  first two, an ordinary re-snapshot would quietly become an empty diff.

  The tree is written through a throwaway index, the same trick `git stash
  create` uses, so the user's own index is never touched.

- **Generated and vendored files are withheld from the review diff.**
  `review.exclude` (lockfiles, `dist/`, `vendor/`, `node_modules/`, minified
  output, source maps, `*.snap`) keeps the *body* of those diffs out of every
  reviewer prompt. Measured on a 400-package lockfile bump alongside a two-line
  source change, one round with two reviewers went from 44,783 to 1,711 input
  tokens -- the diff is sent once per reviewer and once per round, so that
  multiplies.

  Withheld is not hidden: the file is still named to the reviewer with how many
  lines changed, `review snapshot` prints what it withheld and which pattern
  did it, the patterns are recorded in the snapshot metadata, and
  `--no-exclude` sends everything. A change that is *entirely* generated
  produces an empty snapshot that says so in those terms rather than reading as
  "nothing changed". Anything ambiguous is deliberately not in the defaults:
  `build/` is hand-written often enough that hiding it would sometimes drop
  real work, which is a worse failure than paying for a lockfile.

- **Renames are detected in the snapshot** (`git diff -M`), so a staged move
  costs a header instead of twice the file's length.

- **Token accounting per stage and per reviewer.** Delegated runs now record
  what they cost, read from the CLI's own report -- Claude Code's `result`
  event (input, output, cache-read, cache-write and a price) and whatever
  `codex exec` prints. `dev-orchestra tokens show` breaks the total down by
  stage and by reviewer, and `status` and `summary` carry it too.

  Deliberately *not* a budget: nothing refuses a run over what it would cost,
  and the command is separate from `budget` so neither reads as the other.
  Nothing is ever estimated either -- an estimate from the prompt we sent would
  ignore the child CLI's system prompt, tool schemas and file reads, which are
  most of the input, so a silent CLI is reported as unmeasured and every total
  it touches is labelled a floor rather than a total. Reviewers are counted
  individually because they are the pipeline's most duplicated cost: the same
  diff, once per reviewer, once per round.

- **Codex model discovery from the CLI's own catalogue.** The adapter now reads
  `codex debug models` (0.154+) in addition to `$CODEX_HOME/config.toml`, so a
  named family such as `gpt-5.6-terra` resolves with `version: latest` instead
  of having to be pinned by hand. Models the CLI marks as hidden are skipped, a
  CLI without the command is tolerated, and an unknown family is still refused
  rather than guessed.
- **READMEs restructured around using the thing.** Both languages now lead with
  the orchestra itself -- who does what, a lineup mapped onto the families the
  two CLIs currently offer, and the full configuration for it -- followed by
  the workflow stage by stage, how to hand a large corpus (logs, a legacy
  module, a long spec) to one model before designing from its summary, and a
  worked example. The architecture section moved to the developer-facing tail.

- **Claude Code and Codex plugin packaging.** The repository is now installable
  as a plugin on both hosts, and is its own marketplace -- nothing is published
  to Anthropic's or OpenAI's official marketplaces.
  - `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` for Claude
    Code, `.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` for
    Codex. Both marketplaces source the plugin from `./`, so
    `/plugin marketplace add istb16/dev-orchestra` and
    `codex plugin marketplace add istb16/dev-orchestra` install the same
    package.
  - `SKILL.md` moved to `skills/dev-orchestra/SKILL.md`. Codex only discovers
    skills at `skills/<name>/SKILL.md` -- a document at the plugin root is
    ignored there (verified against codex-cli 0.154) and would be a second copy
    of the skill for Claude Code, which discovers both.
  - The skill's own path variable is now `PLUGIN_ROOT` (`${CLAUDE_PLUGIN_ROOT}`
    when installed as a plugin, which Codex sets too). Both hosts run the
    plugin from a copy in their cache, and `scripts/`, `references/` and `bin/`
    all ship inside that copy, so nothing resolves back to a checkout.
  - `scripts/validate_skill.py` now validates both hosts' manifests: names,
    versions, marketplace entries and the declared skills path.
  - The existing installers, the `AGENTS.md` pointer for Codex, and every CLI
    entry point are unchanged; a checkout install keeps working, and Claude Code
    loads it as a skills-directory plugin.

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

- **Shared-file safety for detached workers.** CI found this on one platform
  only: a `jobs wait` failed because the worker was rewriting the job file while
  the parent read it. Three bugs, one after the other:
  - Writes truncated before rewriting, so a reader could see a partial file. All
    state, job and report writes now go through a temp file and one atomic
    rename.
  - `read_json` returned its default on a parse error, turning "unreadable" into
    "absent" -- which is how a live job came to look like a missing one. It
    retries first, and only then gives up.
  - Read-modify-write on the run state could lose one of two concurrent edits,
    so budget consumption and in-flight entries are now taken under a lock, and
    in-flight tokens use a random suffix instead of a millisecond timestamp that
    collided.
  - On Windows a reader's handle blocked the rename, making a *reader* break a
    *writer*. Reads now ask for `FILE_SHARE_DELETE`, so renames succeed while a
    poller is reading -- which matters because polling `jobs` and `status` is
    exactly what the detached path is for.



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
