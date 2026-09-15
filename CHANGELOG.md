# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The public surface covered by that promise is: the configuration schema, the
`dev-orchestra` commands and flags, and the `.ai/` artifact formats.

## [Unreleased]

## [0.4.0] - 2026-09-15

### Added

- **One directory per workflow.** Artifacts move from `.ai/` to
  `.ai/workflows/<id>/`. Two sessions working in the same checkout used to
  share `plan.md`, the review reports, the budgets and the round counter, and
  neither announced itself -- so the first session's plan was overwritten and
  its budget spent by the other one. The id is resolved per command, in order,
  from `--workflow`, `DEV_ORCHESTRA_WORKFLOW`, the host's session id (hashed to
  twelve characters, so another tool's internal identifier stays out of our
  paths, and deterministic, so every command in one session agrees without a
  file to coordinate through), `.ai/current.json`, and finally a new id.

  Commands keep naming artifacts the way they did: `--output .ai/plan.md` now
  means the plan *of this workflow* and lands in its directory. Paths outside
  `.ai/`, and paths that already name a workflow, are used as written.

  This separates the bookkeeping, not the work: the implementer edits the
  working tree and the reviewers read `git diff` of that same tree, of which a
  checkout has one. Workflows that really run at the same time need a worktree
  each (`git worktree add ../x x`), which is a different root and therefore a
  different `.ai/`. When another workflow looks live in the same tree, the
  commands say so rather than let the separate directories imply otherwise.

- `workflow list`, `workflow show`, `workflow use` and `workflow remove`, and a
  global `--workflow <id>`.

### Changed

- **`.ai/` layout (breaking).** Anything reading `.ai/plan.md` or
  `.ai/reviews/consolidated.json` by path now finds them under
  `.ai/workflows/<id>/`. An upgrade adopts the flat layout into the first
  workflow that runs, so a workflow interrupted by the upgrade keeps its plan,
  its reports and its budget; nothing is deleted.

### Fixed

- **`python` is not a name every machine has.** Recent Linux distributions and
  a Homebrew install put the interpreter on PATH as `python3` only, where
  anything spelling it `python` is a command that is not found. The wrappers
  in `bin/` already tried `python3` first; everything that *writes* or *prints*
  an invocation did not. The installers now resolve the interpreter on the
  machine they are running on and put that name in the pointer block they add
  to `AGENTS.md`, and the skill, the agent manifest and both READMEs say which
  names to try. `bin/dev-orchestra[.ps1]` remains the invocation that needs no
  such note.

- **The Windows wrapper tried `python3` first**, which on Windows is usually
  the Store's app execution alias: it opens the Microsoft Store and runs
  nothing. It now tries `python`, then the `py` launcher, then `python3`. The
  POSIX wrapper is unchanged and still prefers `python3`.

## [0.3.1] - 2026-09-15

### Fixed

- **The review round counter counted every snapshot ever taken in a project.**
  Reported from real use: after two rounds on one branch, a second branch with
  an unrelated change opened at round 3 and was refused. `budget reset` did
  not help -- it says in so many words that this is now a fresh workflow, and
  the one counter that was refusing the work lived in the consolidated report
  rather than the ledger it resets. Deleting `.ai/reviews/consolidated.json`
  by hand was the only way out.

  The count now belongs to a review rather than to a directory, keyed on the
  workflow, the branch and the base. A different branch, a different `--base`,
  or a `budget reset` starts it again; the report itself, with its triage, is
  kept. Coming back to a branch worked on earlier is a new review too --
  nothing tracks a count per branch, only whether this round continues the
  last one, and reviewing rather than refusing is the direction to fail in.

  The loop the budget exists to stop is unchanged: review, fix, re-review on
  one change, on one branch, still runs out.

  A report written by an earlier version has no such key, so the first round
  after upgrading starts at one. That is the fix arriving, not a surprise:
  the alternative is honouring a number that was counting the wrong thing.

- **`budget reset` twice within a second was one reset.** The workflow was
  identified by its start time, and `utcnow` counts in seconds. It has an id
  now. Found by the test written for the fix above -- and the same reading
  turned up that the identity was being minted per read rather than recorded,
  which would have reset the round counter continuously and quietly removed
  the budget altogether.

## [0.3.0] - 2026-09-14

### Added

- **`optimization report` names the patterns that escalated a round**, and
  says outright when every round escalated -- at which point the level as
  configured never applied at all. Measured on a real repository: `aggressive`
  was set, `*.tf` and `.github/workflows/*` matched every round, and the only
  thing the setting changed was raising the findings cap from 4 to 10. A dial
  escalated out of existence and a dial that never fires are identical in a
  count, and only the pattern tells them apart.

- **`optimization report`: what the level actually did, over time.** The
  effect of `optimization.level` is a rate -- how often it refused a round,
  how often it cut the panel -- and nothing could report one. `tokens show`
  covers a single workflow and `budget reset` clears it; the run log keeps
  accumulating, so the report reads that instead.

  Any saving is reported as an estimate and says so. What a refused round
  *would* have cost cannot be known, so the figure is the mean of the rounds
  that did run in the same repository, which is the closest honest stand-in.

### Changed

- **`summary` names what the level skipped.** A refused round runs nothing, so
  it appears in no other part of the final report -- and what was skipped is
  exactly what that report is required to name. A round cut to one reviewer
  says so too: one opinion reads exactly like two independent ones once it is
  in a summary.

- **A round the gate refuses is now recorded**, as `refused`, even though
  nothing ran -- and because nothing ran. Skipping a round is the largest
  thing the level ever saves, and it was returning before anything was
  written down, so the saving left no trace and could not be counted. It
  still consumes no budget and opens no ledger entry: there was no attempt to
  account for.

- **SKILL.md asks for the test result at the review stage**, not only at the
  test stage. The instruction was in a paragraph a review-only workflow never
  reads, and the one round recorded in this repository proves the point: it
  ran with `test_status: ""` and the test result was written down thirteen
  minutes *after* the review. A gate with nothing to act on cannot fire, and
  the totals cannot tell that apart from a level that had no effect -- so the
  report counts those rounds separately too.

### Fixed

- **Reviewing a branch against a base never narrowed the second round.**
  `--base main` switched incremental rounds off entirely, so the ordinary way
  to review a branch re-sent the whole branch to every reviewer, every round.
  The base decides what the *first* round covers; whether a later round may
  narrow to the fix is a separate question, and conflating them cost real
  money. Measured on one real three-round review: the diff grew 1,867 to
  2,472 to 3,228 lines while the findings fell 11 to 6 to 5 -- $0.20 per
  finding became $1.00, and the falling find rate was the reviewers being
  shown the same diff three times rather than diminishing returns.

  Changing the base between rounds still takes the whole change: a different
  base is a different definition of what is under review.

## [0.2.0] - 2026-09-13

### Added

- **`model_tiers`: one role, more than one model.** A role is a job, not a
  model, and until now the two were the same setting. A role can now carry
  named alternatives -- `run implementer --tier light` -- so a one-line fix
  goes to something cheap and a second opinion goes to another vendor, without
  maintaining two definitions of what the implementer is.

  The caller picks. Nothing infers a tier from the size of a diff: the
  orchestrator is the only thing that knows how hard the task is, and a wrong
  guess spends exactly what tiers exist to control. An unknown tier is refused
  rather than run on the default model, because a tier silently ignored hands
  you the expensive model when you asked for cheap and the cheap one when you
  asked for care.

  Each key is replaced whole rather than merged, so a tier naming a family
  cannot inherit a base `pinned` id and run a model nobody asked for, and
  switching provider drops the old provider's model and options with it.
  Every tier is validated as the whole role it would become, so a broken one
  fails at `config validate` rather than at the moment work is routed to it.

  `config show` lists them; `tokens show` gives a tiered run its own line, so
  *did the cheaper one actually cost less* has an answer. Reviewers have no
  tiers -- the panel is already one model per reviewer.

- **`scripts/smoke_live.py`: the checks that need a real CLI.** The suite
  cannot make them. It has to pass on a machine with neither `claude` nor
  `codex` installed, so it reviews with the `mock` provider, which overrides
  `run` outright and exercises no adapter's signature but its own. That is how
  every Codex run came to raise `TypeError` for weeks with 654 tests green,
  and how a configured `sandbox: read-only` was silently replaced by the
  default.

  The script starts the real things instead, on a handful of cheap prompts:
  the command line still works, the CLI still reports what a run cost in a
  shape the parser reads, and a read-only mode still refuses to write --
  checked by asking for a file and then looking for it, rather than by
  believing what the agent said about itself. That last one had never been
  verified against a real CLI, despite being the invariant the review design
  rests on.

  It is not part of `unittest discover` and must not become part of it: it
  spends real tokens. `CONTRIBUTING.md` puts it in the release checklist, and
  `tests/test_smoke_script.py` covers everything around the calls -- a missing
  CLI, a run that raises, unreported usage -- without making any.

- **`optimization.level`: one dial over three savings.** `aggressive`,
  `balanced` (default) or `quality` decides whether a round runs against a
  tree whose tests are recorded as failing, whether a small change gets one
  reviewer or the whole panel, and how many findings each reviewer is asked
  for (4 / 6 / 10, unless `review.max_findings` names a number).

  **The gate reads a recorded result; it runs nothing.** This tool has no way
  to know a project's test command -- the orchestrator discovers that from the
  repository and runs it directly -- so the gate reads whatever the last
  `state record test ok|failed` wrote, which `references/workflow.md` has
  always told the orchestrator to write. That is three states, not two: a
  recorded failure refuses (`--force` overrides), and *no record at all* warns
  and continues. Refusing on the third would have broken every existing
  workflow the day it shipped, to punish people for not having written down
  something that was previously optional.

  **A high-risk change escalates to `quality` whatever is configured.** Auth,
  secrets, payments, migrations, SQL, crypto, deploy config: full panel, full
  findings budget, no gate. `optimization.high_risk_paths` replaces the
  patterns; nothing switches the escalation off. They over-match on purpose --
  `authors_controller.rb` matching `*auth*` costs one extra reviewer, and
  missing `auth_controller.rb` costs an authorisation bug.

  **Size is never the only test**, for the same reason: one line in an auth
  file is the shape an authorisation bug arrives in. A reduced panel needs the
  change to be under both thresholds *and* to touch nothing high-risk, keeps a
  `general` reviewer over a specialist -- a lone security reviewer reports no
  correctness bugs, having been told not to look for them -- and prints the
  counts it decided from. `--only` overrides it, because that flag is someone
  naming the reviewers by hand.

  Nothing here drops a finding or acts quietly: every decision is printed,
  recorded in the run state, and returned by `review run --json` and
  `status --json` under `optimization`.

- `state record` now has a reader: the review gate asks the workspace for the
  last recorded status of a stage. Snapshot metadata gained `lines_added` and
  `lines_deleted`, counted from the diff itself so a withheld lockfile
  contributes none of its twelve thousand lines to the size of the change.

- **Reviewer output is capped.** The review prompt now asks for at most
  `review.max_findings` findings (6 by default, `0` lifts the cap), three lines
  of evidence and two lines of fix per finding, and no preamble, summary or
  sign-off.

  Output is the expensive direction: per token it costs several times what
  input does, and a reviewer's output is billed again when it is consolidated
  and again as the fixer's brief. An uncapped prompt invites the padding that
  costs most -- twenty low findings, a screenful of quoted context each, a
  patch where a sentence would do.

  The cap is on volume, not judgement. A reviewer over the limit is asked for
  its worst findings, severity first, rather than told to stop looking, and
  nothing that does come back is dropped: every finding is parsed and kept, and
  a reviewer that overshoots is reported on stderr. Which findings to discard
  is triage, and triage stays with the orchestrator.

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

  The scope only narrows when narrowing is safe. A re-snapshot of a round
  nobody reviewed, a working tree that has not changed since, an explicit
  `--base`, `--full`, and `review.incremental_rounds: false` all take the whole
  change -- without the first two, an ordinary re-snapshot would quietly become
  an empty diff. So does a round following a review that produced nothing to
  fix: with no accepted findings there is no brief to hand the reviewer, and
  that round is reviewing new work rather than checking a fix.

  The tree is written through a throwaway index, the same trick `git stash
  create` uses, so the user's own index is never touched, and only when the
  round might actually use one -- writing it hashes every
  untracked-but-not-ignored file into the object database, which on a
  repository with a large directory nobody remembered to ignore is neither
  cheap nor invisible.

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

  A run that never started a CLI -- an unresolvable model, a CLI that is not
  installed -- is left out entirely rather than counted as one that failed to
  report, so the "these totals are a floor" caveat fires on missing data and
  not on a run there was never any data for.

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

### Changed

- `review.max_findings` now ships unset instead of `6`, which lets
  `optimization.level` decide it. The effective default is unchanged: an unset
  cap at `balanced` is still 6. A number set explicitly still wins over the
  level, and `0` still lifts the cap.

- **SKILL.md is a quarter smaller.** 15,877 characters down to 11,533 when it
  was compressed, and 12,148 by the end of this release -- roughly 3,970
  tokens to 3,040, with the difference spent on `optimization.level` and
  `model_tiers`. That document is resident for the whole of every session, so
  its size is a running cost rather than a one-off, and `validate_skill.py`
  now enforces a character ceiling alongside the line one -- a table row and a
  paragraph are one line each and cost very differently.

  What went was words, not steps. Prose became tables and clauses, the
  46-character helper-CLI prefix is stated once instead of fourteen times, and
  the eighteen-row "the user says / you run" phrasebook moved to
  `references/configuration.md`, replaced by the command grammar it was
  examples of. Every command the pipeline needs is still in the document,
  because a reader who has to open `references/workflow.md` to find the next
  step has saved nothing: that file costs more than this one.

- **The review prompt is written as instructions, not prose.** The template
  dropped from 1,247 to 752 characters and the role guidance from an average of
  235 to 195, so the fixed part of every reviewer prompt went from 1,574 to
  1,309 characters -- roughly 66 tokens saved per reviewer, per round, on top
  of the output cap that replaced part of it.

  Nothing the reviewer is held to was dropped, only shortened: read-only, judge
  this change alone, read any file for context, and every field the parser
  reads. The `Finding` block keeps its exact shape, because a renamed heading
  would cost a whole delegated run to a parse failure. `- Recommended fix:`
  became `- Fix:`, which the parser has always accepted, and still does.

  The fixer's brief was trimmed the same way: an empty field is omitted rather
  than sent as a bare label, and who reported a finding is left out -- the fix
  is the same whoever noticed.

- Renamed every user-visible identifier to `dev-orchestra`: the skill name, the
  `dev-orchestra` command, the config directory, the `.dev-orchestra.yaml`
  project override, and the `DEV_ORCHESTRA_*` environment variables. Three
  competing names for one tool was one too many.

### Fixed

- **A role that named no model never had its options validated.** Omitting
  `model` is how you let a CLI pick its own, and the model checks returned
  early -- taking the option checks with them. A typo in a Codex `sandbox`
  policy passed `config validate` and was discovered at run time. Found while
  adding tiers, because a tier that only switches provider is exactly such a
  role.

Found by measuring: a two-model review was run against the whole of this
release's work to see what the output cap saves. It reported these instead.

- **A reviewer's own prose was being read as its token accounting.** The Codex
  adapter scanned the CLI's output for a usage report, and that output
  contains the agent's answer. A reviewer reading this repository writes about
  token counts: one real review quoted `input_tokens: 12` as a finding's
  evidence, and the adapter recorded twelve billed tokens for that run and
  discarded the total the CLI had actually printed. The finding was about this
  bug, and the text of it caused the bug to happen -- which is as direct a
  reproduction as anyone is going to get.

  The pattern is anchored to the start of a line now, the number has to start
  with a digit (the old character class matched a bare comma), and the
  speculative input/output patterns are gone. They were written against a
  format this CLI has never emitted, and the only thing they ever matched was
  prose. `tokens used` on its own line, followed by one number, is what Codex
  prints and now all that is read.

- **Binary and mode-only changes were not counted as files.** The
  reviewed-file list was read out of the diff text by looking for `+++ b/`
  headers, and git prints none for either: a binary file gets "Binary files
  a/x and b/x differ" and a mode change gets `old mode` / `new mode`. So a
  change to three images and one source file counted as one file -- small
  enough to be handed a reduced review panel. The list comes from `git diff
  --numstat` now, less what was withheld and what is not under review.

  The mode-only case is tested one layer down rather than end to end:
  `git update-index --chmod` stages a mode while `git diff HEAD` reads the
  working tree, so whether those disagree depends on `core.filemode` -- false
  on Windows, true elsewhere. A test that passed on one platform and failed on
  the other would be testing git's configuration, not this.

- **The uncapped limits block no longer drops its opening instruction.**
  `review.max_findings: 0` omitted the line naming findings as the output,
  leaving the block to open with a rule about evidence length. The one run
  observed returning a prose summary instead of finding blocks -- recorded
  `unparsed`, and so counted as a failed review, which is the behaviour
  working -- was the uncapped one. One run is not a cause and this is not
  offered as the fix for it, but an instruction that is weaker in one mode
  than the other is worth levelling either way.

- **Every Codex run failed with a `TypeError`.** `CodexProvider.run`
  overrides the base method so the agent's final message can be captured to a
  file instead of scraped from a log. `idle_timeout` was added to
  `Provider.run` and never to that override, so the call raised
  `TypeError: CodexProvider.run() got an unexpected keyword argument
  'idle_timeout'` -- from `run architect`, `run implementer`, and every Codex
  reviewer, because the CLI always passes it. A Codex reviewer had been
  reporting `FAILED` for that reason alone.

- **A configured Codex `sandbox`, `approve` or `args` was silently ignored.**
  The same override accepted `options` and did not pass them on, so
  `build_command` was always given `None` and always used its defaults. A role
  pinned to `sandbox: read-only` ran `workspace-write` instead, and said
  nothing about it.

  The suite missed both for one reason: it reviews with `MockProvider`, which
  overrides `run` outright and so exercises no adapter's signature but its
  own. `tests/test_provider_contract.py` now tests the seam rather than a
  provider -- every registered adapter, present and future, is called with
  every keyword the orchestrator sends, and checked against the base
  signature, without starting a process. Six of its eight tests fail on the
  code this entry describes.

Four defects in the risk detection that decides whether a review can be made
cheaper, all found by running a real multi-model review against the change
that introduced them.

- **A deleted file was not treated as a change.** The changed-file list was
  read from the diff's `+++ b/` side, which is `/dev/null` for a deletion, so
  deleting `app/auth.py` escalated nothing and counted for nothing. Deleting
  an auth file is not a smaller change than editing one. Both sides of every
  header are read now, and a rename records the name it came from as well --
  the diff only carries where the file landed, and a file renamed away from
  `auth.py` was an auth file until this commit.

- **Diff content that looked like a diff header was not counted.** Line
  counting skipped anything starting with `+++` or `---`, but a content line
  carries its own `+`/`-` prefix with nothing between it and the text: a
  deleted `---` (Markdown front matter, a YAML separator) arrives as `----`,
  an added `++i` as `+++i`. Both were dropped, undercounting the change --
  the one direction that matters, because the count decides whether a change
  is small enough for a single reviewer. Counting now tracks hunk boundaries
  instead of guessing from the first characters.

- **Withheld files spent the file budget.** The size threshold counted every
  path the change touched, including the lockfiles whose diffs are
  deliberately not sent, so a one-line fix beside a dependency bump stopped
  counting as small while the diff a reviewer saw was two lines long. The
  line count had excluded them from the start; the file count now does too.
  Risk is still judged from the whole set -- a withheld `.env` is still a
  secret.

- **`k8s/` and `deploy/` at the repository root were not high-risk.** Only
  the nested forms (`*/k8s/*`) were listed, and `fnmatch` has no `**`, so the
  directories were caught everywhere except where they usually live. Both
  forms of each are listed now.

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

- Two tests silently depended on `claude` and `codex` being installed, so they
  passed locally and failed on every CI platform. Both now drive the `mock`
  provider instead, and `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI=1` reproduces the CI
  environment on a developer machine that has the real CLIs.
- `doctor` now reports options that a read-only role will ignore even when the
  provider CLI is missing: whether an option takes effect is a fact about the
  configuration, not about what happens to be installed.

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

[Unreleased]: https://github.com/istb16/dev-orchestra/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/istb16/dev-orchestra/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/istb16/dev-orchestra/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/istb16/dev-orchestra/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/istb16/dev-orchestra/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/istb16/dev-orchestra/releases/tag/v0.1.0
