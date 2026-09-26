# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The public surface covered by that promise is: the configuration schema, the
`dev-orchestra` commands and flags, and the `.ai/` artifact formats.

## [Unreleased]

### Added

- **`optimization report` scores each reviewer.** A block per stage lists,
  for every reviewer and the panel, the findings reported, accepted,
  rejected, duplicate and still open, the findings it alone reported
  (an upper bound on what dropping it would lose), its runs and failed runs,
  what it billed and cost, its rejection rate, and what it billed and cost per
  accepted finding. A last line adds the code and design panels together, the
  figure for what review as a whole cost per accepted finding. Rates are
  withheld under 10 decided findings, per-accepted figures under 10 accepted,
  and the counts are always printed. Cost comes from the run log and findings
  from each round's report, matched round by round; a round with no report to
  read is left out, its cost included, and the report says how many there
  were and that the figures over the rest are biased in a direction that is
  not known. The per-accepted cost measures efficiency, not quality: better
  code, a reviewer missing more, and a price rise all raise it, and it cannot
  tell them apart. `--json` carries it as `scorecard` (`code`, `design`,
  `total`).
- **Every round's consolidated report is kept**, as
  `reviews/rounds/<sha12>-<round_id>.json` (and `reviews/design/rounds/`),
  written alongside `consolidated.json` by `review run`, `review consolidate`
  and `review triage`, and overwritten only by a later write to the same
  round.
- **Code snapshots have a `round_id`**, new with every `review snapshot` as
  the design review's already was, copied into the consolidated report's
  `snapshot`. `review` and `design_review` run events record it too.
- **`review triage` stamps each decision with `triage_set_at`**,
  `needs-triage` included, and the stamp is carried to the next round with the
  decision. It is what tells a finding put back to `needs-triage` from one
  never decided.

### Changed

- `consolidated.json` is no longer the only record of a round: the triage
  decisions of earlier rounds used to be lost when the next round rewrote it.

## [0.10.0] - 2026-09-26

### Added

- **The references have a Japanese translation.** Every file in
  `references/` has one in `docs/ja/references/`, linked from `README.ja.md`.
  The English stays what the skill reads and what is authoritative. Each
  translation starts with the sha256 of the English file it was brought up to
  date with, and `tests/test_docs.py` fails once the English changes without
  it, so a translation cannot fall behind unnoticed; the same test checks
  that the code blocks, anchors and links match the English. After updating a
  translation, `python scripts/stamp_translation.py` rewrites its header.
- **Code reviewers can be handed the function around each hunk.**
  `review.context.surrounding: enclosing` (default `none`) makes
  `review snapshot` extract the Python function, method or class enclosing
  every hunk from the git tree the diff was taken from, freeze it in
  `review-surrounding.json`, and `review run` adopt it into every code
  reviewer's prompt within `review.context.surrounding_chars` (default
  15,000) and what the diff leaves under `max_chars` and `inline_chars` -- so
  it never refuses a round or sends a diff over as a file. A deletion with no
  surviving symbol around it marks its neighbours `adjacent` rather than
  enclosing. Whatever is left out -- over the budget, a file delivery, a file
  that cannot be extracted exactly (not Python, a new file, a symlink, a file
  edited while the snapshot was taken) -- is named, in the prompt, on each
  reviewer entry, in `consolidated.json` and `consolidated.md`, and in
  `review status`. Coverage is unchanged. `optimization report` splits code
  rounds into with and without context, per run and per 1k characters of
  change. With the setting off, every prompt and artifact is what it was.
  Measured on thirteen pairs of one snapshot each, it did not make a review
  cheaper -- at a 60,000 cap it added about 29% per run on a large change, at
  15,000 nothing outside run-to-run variation -- so it ships off with the
  15,000 cap.
- **The surrounding context can be measured on one snapshot.**
  `review snapshot --surrounding none|enclosing` and
  `review run --surrounding none|enclosing` override
  `review.context.surrounding` for one snapshot or run without editing the
  setting, so the same frozen change can be reviewed with and without the
  context. `optimization report` pairs the two runs -- keyed on the workflow
  directory, the full snapshot sha256, the frozen tree, `head` and `base` --
  and prints the per-run billed tokens, tool uses and observed tool output of
  each side and their delta; `--json` adds `paired`. A pair counts toward the
  total only when the panel (reviewer, provider, model and role), every run's
  delivery and the other prompt inputs match and the enclosing run adopted
  something; otherwise it is listed with the reason. The override is refused
  before any cost on a design round, on an incremental round, when `enclosing`
  would adopt nothing, and on a second run of a snapshot whose triage or
  triage notes changed since its last run, across a budget reset too. The
  rerun stays in its round and registers no findings signature. The run event
  and `review run --json` gain a `measurement` block, and `consolidated.json` a
  `measurement` record, only when the flag is given. See
  `references/limits.md`, "Measuring what surrounding context does".

## [0.9.0] - 2026-09-25

### Added

- **Implementation waits for the user's approval of the plan.**
  `design.require_approval` (default `true`) makes `run implementer` refuse --
  exit 5, before any budget is consumed and before `--detach` hands the work
  to a worker, and again inside the worker, which writes the whole refusal
  into its job -- while `.ai/plan.md` exists and the plan as it is now has not
  been approved. `design approve` records the approval against the sha256 of
  the plan alone and the design review round it was given over: every
  `review run --design` now writes a new `round_id` into
  `reviews/design/review-target.json`, so a later revision or a later design
  review -- even of the same plan -- needs approving again, while
  re-consolidating and triaging do not. The round is copied into the
  consolidated report only by the `review run --design` that ran it, once
  every reviewer has returned and at least one of them reviewed -- `review
  consolidate --design` keeps the round the previous report named -- and
  `design approve` binds to that, so it refuses (exit 2) while a round is
  still running; a round every reviewer failed is approved over with a note
  that the plan went unreviewed, rather than left unapprovable. Open design
  findings -- all of them, not only the blocking severities, and the same
  list in `status` -- are printed, not refused, and labelled when they come
  from a review of an earlier revision. A
  detached worker that refuses records the whole refusal in its job; the
  attempt its parent consumed is not given back. An empty
  `design.require_approval:` means the default rather than turning the gate
  off.
  `--force` does not apply: it overrides a budget, and approval is the user's
  consent. `status` reports the state under `design_approval` and says to ask
  the user; the verdict is unchanged, except that a design review budget spent
  with findings open stops being a reason once the user has approved the
  current plan over them. No plan, no gate. **A workflow in progress at
  upgrade time stops before implementing until its plan is approved**,
  including one whose implementer already ran: `status` does not ask about
  that one, but running the implementer again needs an approval. `config set
  design.require_approval false` restores the previous behaviour for
  unattended runs.

- **A change too big to review is refused rather than half-reviewed.**
  `review.context.max_chars` (default 400,000) is the most change body a
  review round will send at all -- the diff for a code round, the plan *and*
  the request it answers for a design one, because both go into every
  reviewer's prompt whole and measuring one of them would let a round past
  the limit on a technicality. Over it, `review run` and `review run --design`
  exit 3 before any reviewer starts, before anything is charged, and -- on the
  design path -- before the plan is frozen, so the previous round's reports
  and triage survive the refusal. `review snapshot` warns and still writes the
  snapshot: taking one spends nothing.

  400,000 chars is roughly 100k tokens, four times the largest prompt this
  repository has recorded (99,814), so **the default refuses nothing anyone
  has recorded here**. Characters rather than tokens because that is the unit
  everything else already measures in and the standard library cannot count
  the other; CJK-heavy text is two to four times as many tokens per character,
  and the docs say so rather than implying a constant.

  It refuses rather than trimming. Dropping hunks to fit hands a reviewer a
  change it cannot judge and says so nowhere, so the tool reports *not
  reviewed* instead of calling an incomplete review complete. **`--force`
  belongs to a human**, which means an automated workflow over the limit runs
  no review at all and reports that: `status` answers `stop-and-report` until a
  round actually reviews the change, so a clean consolidation from an earlier
  round cannot stand in for this change having been reviewed. Nothing short of
  a round that ran clears it -- not a second refusal of either kind, and not
  the `abandoned` entry `status` writes when it clears a stage whose process is
  gone. A forced round is recorded as `over_budget` on each reviewer entry, on
  `consolidated.json`'s `snapshot` block -- beside `budget_chars`, the size the
  limit measured -- in `consolidated.md` and in `review status`. What forcing
  does *not* promise is that every reviewer comes back `partial` -- that is the
  inline limit's answer, not this one's, and it is decided by the body that is
  handed over, which on the design path is the plan without its request. The
  two coincide under the defaults and do not when either limit is configured
  away from the other, and the refusal message says which case the change in
  front of it is.

  `review snapshot --json` carries `change_chars`, `max_chars` and
  `over_context` alongside the snapshot's metadata, so a wrapper reading that
  form learns what the human-readable warning says before `review run` exits 3.
  An explicit `review.context.max_chars: null` means the default, as `null`
  does on `review.max_findings`; there is no value that switches the limit off.

  The refusal is visible where refusals are counted: the event carries the
  round's `optimization` block, as the gate's does, so `optimization report`
  sees it at all, plus `refused_by` (`gate` or `context`) so the two can be
  told apart. `estimated_saving` prices the gate's refusals only -- a round
  refused for size was going to cost more than the mean, so charging it the
  mean understates it -- and says so. Refused *design* rounds are counted on
  their own, because the design tally counts rounds that ran.

  Additions only: a config key an older `validate` ignores, `over_budget` and
  `refused_by` keys an older reader skips. An older `optimization report`
  counts a size refusal as one of the gate's, which overstates the gate and
  keeps the refused total right. Nothing about what an unrefused round sends
  changed.

- **What a delegated run did with its tools is now counted.** Reducing what
  reviewers read is the point of the work this belongs to, and there was no
  way to measure it at all. A Claude run's `usage` gains `tool_uses`,
  `tool_uses_by_name` and `tool_output_chars`, read from the `tool_use` and
  `tool_result` blocks the CLI already streams: the breakdown from the names
  on the calls, the characters as one total over the results -- one whose call
  the stream never showed included rather than dropped. `tokens show` gains a
  `tools` and a `tool out` column, `optimization report` a per-run block, and
  `scripts/smoke_live.py` a check that a real run still reports it -- the
  event shape is the CLI's to change, and the unit tests read a fixture.

  **Every tool call is counted, not just `Read`.** Review mode denies
  `Edit,Write,NotebookEdit` and nothing else, so `Bash`, `Grep` and `Glob` are
  all legitimate ways to read a file: the run this was measured against read a
  file with `wc -l` and never called `Read`, and counting `Read` alone would
  have recorded it as having opened nothing.

  **`tool_output_chars` is observed tool output, not source read.** That
  `wc -l` returned three characters for a two-hundred-line file; `cat` would
  have returned all of it, and the two are indistinguishable from outside the
  CLI. How much source a reviewer read is not knowable from here, and the
  docs say so rather than letting the figure be read as if it were.

  **Codex reports none of it**, by design: its usage comes from a prose
  footer, and matching prose for tool calls would match the code under review.
  So the ledger counts `tool_reported_runs` beside `runs`, and every per-run
  figure is divided by it -- a mixed panel would otherwise halve the figure for
  no reason but its composition. In the two new columns `-` means *did not
  report* and `0` means *reported using no tools*; a run recorded before this
  existed is reported as unable to say, never as having used none, and it goes
  on saying so once later runs are recorded beside it. The absence of
  `tool_reported_runs` is what marked those runs, and the first run recorded
  into such an account converts it once and for all: it stamps how many runs
  predate counting into `tool_unknown_runs` *and* creates `tool_reported_runs`
  at zero, whether or not that run itself reports tools. A panel can therefore
  hold both kinds at once -- a stage that predates counting and a Codex stage
  that reports none -- and `tokens show` says both things rather than the first
  of them.

  Only what the CLI streams as a stream is counted: `output_format: json`
  prints a single `result` object and never emits a tool event, so such a run
  is unreported rather than a measured zero.

  Additions only: `usage` keys an older reader ignores, ledger keys older
  code skips as unknown (`_accumulate` walks a fixed list), and two columns.
  Nothing about what is sent to a provider changed -- this stage only
  observes, and the prompts come out byte-identical.

- **The inline limit is a setting.** `review.context.inline_chars` decides
  whether the change body goes into the reviewer's prompt or is handed over as
  a path to the frozen snapshot. It replaces `MAX_INLINE_DIFF_CHARS`, which was
  a constant in `review.py`; the default now lives in `default_config()` beside
  `max_chars`, where every other default lives, and is read from there rather
  than copied, so the two cannot drift apart.

  Every round records the number it was measured against -- `inline_chars` on
  each reviewer entry and `coverage.inline_chars` in `consolidated.json`, with
  the limit named in `consolidated.md`'s `Coverage:` line, in the handover note
  the reviewer is given, and in the `coverage unverified` reason a `partial`
  run carries. A limit that can be configured is one a reader cannot assume: a
  `partial` round and a size alone do not say whether it was a large change or
  a low limit. A round recorded before this reports `null` -- it was 120,000
  then, but nothing wrote it down and this does not invent it.

  `review status` now names both ways out of an unverified round: narrow the
  change, **or raise `review.context.inline_chars`**, then snapshot again. Once
  that limit has been raised past the size the round recorded, it says so
  instead -- the same snapshot would be inlined now, so splitting the change
  and raising a limit already raised are both wasted work, and what is left to
  do is run `review run` against that snapshot again.

  Both numbers of the pair are read off the entry that decided the round's
  mark -- the first reviewer handed a file, when there is one -- because
  `--only` merges entries made under two configurations into one snapshot's
  table, and a size against another round's limit is a line that contradicts
  itself.

  `inline_chars` above `max_chars` validates and is not warned about -- it
  means the body is only ever handed over on a round a human forced. So does
  `inline_chars` below `max_chars`, which opens a band between the two where
  rounds run and are recorded `partial`; `references/configuration.md` states
  that consequence. `null` means the default, as it does on `max_chars`.

  An older reader sees one more key in `review.context` that its `validate`
  does not check, and one more key on each reviewer entry and on `coverage`
  that it skips. Nothing existing changed its meaning or type.

- **Provider adapters of your own load from the config directory, so they
  survive a plugin update.** Every `.py` in `<config dir>/providers/`
  (`%APPDATA%\dev-orchestra\providers\` on Windows,
  `~/.config/dev-orchestra/providers/` elsewhere,
  `$DEV_ORCHESTRA_HOME/providers/` when that is set; `DEV_ORCHESTRA_CONFIG`
  does not move it) is imported after the built-ins and registers what its
  `build_provider()` returns. The plugin installs into a versioned cache, so
  an adapter dropped into `scripts/orchestrator/providers/` vanished on update
  while the `config.yaml` naming it stayed behind. Built-in names cannot be
  taken over and the first file to claim a name keeps it. A module that fails
  to load never stops the CLI; `doctor` lists the directory, what it imported
  and what failed (a problem under `--strict`), and a `Source:` line on every
  provider. `doctor`, `model list` and the setup wizard now report an adapter
  that raises -- built-in or not -- instead of dying with a traceback, and
  `config show` says where each provider it refers to comes from.
  `DEV_ORCHESTRA_NO_USER_PROVIDERS=1` skips the directory, and an unknown
  provider says so while it is set. The directory is trusted code run in the
  CLI's process, not a sandbox. See `references/providers.md`, including what
  is and is not a stable interface.

### Changed

- **`register()` refuses a name that is already registered, and a call
  without an origin once the built-ins are in.** It used to overwrite
  silently, which is how a user module could replace a built-in. Nothing in
  the plugin registered twice; code outside it that swapped a built-in this way
  was never documented and now gets `ProviderRegistrationError`. The discovery
  cache is also keyed by the adapter's module, so a user adapter copied from a
  built-in with its class name intact no longer shares the built-in's results.

- **The default inline limit is 400,000 characters, up from 120,000, and this
  changes what is sent.** A change body between 120,000 and 400,000 characters
  now goes into every reviewer's prompt where it previously went over as a path
  to the frozen snapshot. That is a larger prompt and a real cost change, and
  it is the point: 120,000 had no measured basis -- the prompt reaches the CLI
  on stdin, so no argv length is involved -- and what a file handover buys is a
  review this tool cannot verify. Stage one made such a round `partial`; this
  closes the band it left, where a change in that range was recorded `partial`
  with no way out but editing a constant.

  With `inline_chars` and `max_chars` both 400,000 the shipped behaviour is one
  boundary: at or under it the round runs and is complete, over it the round is
  refused, and a forced round is `partial`.

  **`review.context.inline_chars: 120000` restores the old behaviour exactly.**
  At or under 120,000 chars -- which is every round this repository has
  recorded, the largest measuring 99,814 -- the prompt is byte-identical to
  what it was either way.

- **CI runs the tests in parallel, and a test repository is a copy.**
  `tests/run_parallel.py` runs what `python -m unittest discover -s tests -t
  tests` finds, one test class per worker process, and fails if the counts
  differ, a worker dies or a class hangs; `init_git_repo()` copies a `.git`
  made once instead of starting four `git` processes per test. The Windows
  test job took about 4m15s, almost all of it one process running the suite
  serially. The discover command is unchanged and still what to run locally.

### Fixed

- **A run's charge or its event could be lost when two ended at once.**
  `record_event` rewrites the whole state file to append one line, and the
  ledger lives in that file: unlocked, it read the state, another process
  committed a charge, and it wrote its own copy back over it. Measured with
  eight concurrent charges, seven landed. `record_event` now appends under the
  state lock, and `Ledger.end` writes the charge and its event together under
  one hold of it rather than reading the state back after releasing it.

- **On Windows, a state file being rewritten could read as absent.** A reader
  that opens the file in the instant `os.replace` swaps it in is refused with
  an `OSError`, and `read_json` returned the default for that at once while it
  retried a torn read. The same short retry now covers both; a file that is
  really gone still returns the default without waiting.

- **The last review round's findings were reported instead of reflected
  (#68).** Once a round reached `review.design.max_iterations`, `status` said
  `stop-and-report` straight after its triage, so its accepted findings never
  reached the plan; the code review stopped the same way before
  `run review_fixer`. The limit counts reviews, and now refuses only the
  re-review: `status` says `continue` until the last design round's revision
  is made (the plan differs from the frozen `review-target.md`, or the
  architect answered after the round with `--output` on the plan), and until
  the last code round's fix is made and a `test` or `re-test` outcome is
  recorded after it. Only accepted findings get that last pass: with none
  accepted the stop comes at once, as before (`unaccepted`). `review status
  [--design] --json` reports it as `final_revision` / `final_fix` with a
  `*_pending` flag, `status --json` also under `design_review` / `review`
  beside a new `identical_rounds`, and the last line of `review status` names
  the next step. A plan already approved (even with `design.require_approval`
  turned off since) or implemented is not asked to change; with no
  `architect` or `review_fixer` attempt left for it, the stop comes at once.
  The repeated-findings reason waits until that last pass is done,
  `architect has no attempts left` is a reason only while there is no plan or
  a design round within its budget still has blocking findings, and `review_fixer has no attempts left` only while the fix is not
  made. A run's end event now records `answered`, so an exit 0 over silence
  does not count as the revision or the fix. The refusals of `review run` and
  `review run --design` past the limit say the last round still gets its fix
  or revision. `references/workflow.md` recorded the re-test as stage
  `re-test`, which the review gate never reads; it now says `state record
  test`. Default budgets are unchanged.

## [0.8.0] - 2026-09-19

### Added

- **A review whose change body was not fully inlined is no longer reported as
  clean.** A change over `MAX_INLINE_DIFF_CHARS` (120,000) has always been
  handed to reviewers as a path to the frozen snapshot rather than inlined,
  and how much of that file gets read is not knowable from here -- Claude
  Code's `Read` stops at 2,000 lines by default, so a reviewer could answer
  `NO_FINDINGS` having seen a fifth of the change and be recorded `ok`. Such a
  reviewer is now recorded `partial`: its findings are kept and triaged like
  any others, and the round is not a clean one. Reviewer entries carry
  `delivery` and `change_chars`, `counts` carries `reviewers_partial`, and
  `consolidated.json` carries a top-level `coverage` block -- `round`,
  `change`, `unverified_since`, `change_chars` -- which `consolidated.md` and
  `review status` both print. `review status --json` gains `coverage` and
  `reviewers_partial`; `review run --json` gains `partial`.

  `coverage.round` and `coverage.change` are separate values and both are
  needed. An incremental round inlines only the fix, so judging the whole
  change by what *this* round inlined would let a fix-only round launder a
  partial one: accept the finding, fix it, inline the fix, `NO_FINDINGS`,
  clean -- with most of the change still unread by anybody. `coverage.change`
  therefore carries across rounds, and is cleared only by a non-incremental
  snapshot that was inlined whole and reviewed by at least one reviewer.

  Both values are derived only from the reviewer entries stamped with the
  snapshot the report is about. Reviewer entries gain `snapshot`, the same
  short sha the reviewer reports carry -- an addition an older reader simply
  does not see. The reviewer table is meant to outlive one round (`--only`
  merges a fresh run into it, and a reviewer that did not run this time is
  kept so the table stays complete), so without the stamp a fresh snapshot
  nobody had opened could be reported as reviewed in full on the strength of
  the previous round's entries.

  The carry is keyed on the workflow and the branch -- the round counter's key
  with the base dropped. `--base` is the other way of saying which change, so
  a mark keyed on it would be cleared by narrowing the diff, which makes the
  round smaller and the change no more read than it was. The cost is
  deliberate: an unrelated second change on the same branch inherits the mark
  until a full snapshot is reviewed inline. What carries across a lineage
  change is the mark alone: the round counter restarts there, so
  `unverified_since` is `null` on a carried mark whose round number belongs to
  a count that no longer exists -- otherwise `review status` printed
  `iteration 1/2` and "since round 3" in the same breath. Read the mark from
  `coverage.change`; `unverified_since` is the round when there is one to give.

  `counts` carries the four reviewer columns twice: `reviewers_*` over the
  whole table, which deliberately outlives the round, and
  `snapshot_reviewers_*` over the entries stamped with the snapshot the report
  is about -- the set `coverage` is derived from. Counting only the first put
  `Reviewers: 2 ok / 2 total` beside "no reviewer has run against this
  snapshot", one report describing two snapshots. `consolidated.md` prints the
  snapshot's tally on a second line whenever the two differ, and `review
  status` reports the snapshot's (`reviewers_partial` in its payload is that
  one; `counts` carries both).

  `review status` names the one action that clears each and nothing else:
  split the change for an unverified round, `review run` for a snapshot no
  reviewer has run against, re-running the reviewers for a round none came
  back `ok` from, `review snapshot --full` for a round that inlined the fix
  alone, and -- on `--design`, where neither command can be aimed at the plan
  -- shorten `.ai/plan.md` and run the design round again. Which of those it
  is, is decided once, by `coverage_state`, and worded by both
  `consolidated.md` and `review status`, so the two readers of one report
  cannot describe it differently.

  A consolidated report written before this version has no `coverage` block;
  `consolidated.md` and `review status` both leave the value unstated rather
  than call an unmeasured round `none` (`review status --json` reports
  `"coverage": null` for it, and the `none` values only when there is no
  report at all).

  The reviewer is not asked to declare any of this. A self-report cannot be
  checked for the case where it was not made, and both prompt templates end
  with "Findings or `NO_FINDINGS` only", which overrides anything asked before
  it. The prompt for a file handover states what the round is recorded as and
  asks for nothing back. The inlined prompt is byte-identical to what it was.

### Changed

- **`review run` exits 1 when no reviewer came back `ok`**, which now includes
  a round that was entirely `partial`, not only one where every reviewer
  failed. Such a round made no claim that the change is fine.
- **`counts.reviewers_failed` no longer includes partial reviewers.** It was
  `status != "ok"`, which would have counted a partial round in both columns;
  `reviewers_ok`, `reviewers_partial` and `reviewers_failed` now sum to
  `reviewers_total`. An older reader of `consolidated.json` still sees
  `partial` as a status it does not know and treats it as not-ok, so nothing
  reads such a round as clean. No round recorded so far reached the inline
  limit -- the largest measured 99,814 characters -- and entries written
  before this version have neither `delivery` nor `snapshot`, so they are left
  out of the coverage derivation rather than assumed.
- `summarise_runs` returns `(ok, failed, partial)`, and `build_review_prompt` /
  `build_design_review_prompt` return a `BuiltPrompt` -- the text plus what it
  carried -- instead of a bare string. Both are internal to `review.py` and its
  one caller in `cli.py`.

### Fixed

- **`run --detach` never delivered its prompt.** The worker was started with
  `--prompt-file -` and its stdin on `DEVNULL`, so it read nothing and
  delegated an empty prompt -- while the prompt sat, written and unread, in the
  `.prompt` file `jobs.start` had already created for it beside the job.
  `_detached_argv`'s own docstring said the prompt came from a file; only the
  wiring was missing. Every test passed because the mock provider does not read
  a prompt, so nothing ever asked the provider what it had been handed. Two
  tests now do: one fails the run on a marker in the prompt's text, the other
  checks the prompt's length in the usage the run reports.

  Naming the file was not enough by itself. `--extra` is `nargs=REMAINDER`, and
  `jobs.start` appended both `--prompt-file` and `--job-file` to the end of the
  worker's command line, so `run <role> --detach --extra ...` handed the two
  paths to the provider CLI as arguments of its own: that worker read no
  prompt, claimed no job, exited on its `DEVNULL` stdin, and the run ended
  `abandoned` with the attempt already spent. `--job-file` had been appended
  that way from the beginning, so a detached run carrying `--extra` has never
  worked. `_detached_argv` now places a marker for each where its own command
  line has room for one, and `jobs.start` substitutes the paths that only it,
  holding the job id, can build.

- **A detached worker now records why it gave up before running anything.**
  Refusing an unreadable or empty `--prompt-file`, and failing to resolve the
  model it was asked for, both write the reason to stderr, and a worker's
  stderr is `DEVNULL`, so the reason was destroyed: the run surfaced only as
  "the worker process (pid N) is gone and recorded no outcome", which is also
  what is said about one that was killed. A run with `--job-file` now records
  the message as the job's failure at both exits. The model one matters as of
  this release: forwarding `--tier` is what puts an unresolvable model in front
  of the worker rather than quietly running the default. The foreground call is
  unchanged -- its caller is watching the stderr the message goes to.

- **`--detach` dropped `--tier`.** `_detached_argv` forwarded `--mode`,
  `--output`, `--timeout`, `--idle-timeout` and `--extra`, but not the tier, so
  `run <role> --tier <t> --detach` quietly ran the role's default model --
  usually the more expensive of the two -- and recorded what it spent without
  the `role:tier` label a tier exists to be read against.

- **An empty prompt is no longer delegated as though it were a request.**
  `_read_prompt` read a `--prompt-file` through `ws.read_text`, whose default
  answers `""` for anything that is not a readable file, so a mistyped path
  became an empty prompt: the attempt was spent, the provider was started, and
  what came back was that CLI's own complaint about its stdin, naming neither
  the file nor the mistake. The path is now read so that failure is visible,
  and "there is no such file" is reported apart from "it is there and it is
  empty" -- different mistakes, and the reader needs to know which they made.
  The message names the path as it was written as well as the one
  `--workflow` resolved it to. The other ways a prompt arrives empty are
  refused the same way and say which source was empty: an explicit
  `--prompt ""` (`if args.prompt:` was falsy for it, so it fell through to the
  stdin branch), and a pipe that carried nothing. All of it happens before the
  budget is consulted, so a broken invocation costs no attempt.

- **A run that produced nothing no longer exits 0.** Without `--output` an
  `ok` run whose stdout was whitespace printed the whitespace, said nothing,
  and exited 0; with `--output` the same result had its write refused and
  exited 1. The two paths now share the judgement, because which one the
  caller used says nothing about whether the run answered. The complaint names
  the role and quotes the beginning of the raw stderr, which is where a CLI
  that refused the prompt says why. A `--job-file` run is unchanged: its
  stdout is recorded in the job and `jobs wait` reports the outcome.

## [0.7.0] - 2026-09-18

### Changed

- **`budgets.max_runtime_seconds` now measures delegated execution, not the
  calendar.** It subtracted the ledger's start time from the current time, so
  it was a wall clock under another name: an interactive session spent the
  whole budget by existing for two hours, having delegated nothing. Reported
  from a real session that opened with every budget untouched — `0` of every
  stage's attempts, `0/40` delegated runs — beside `runtime 0s left` and a
  `STOP-AND-REPORT` verdict, which is the one combination that cannot be true.
  The budget is now charged from the measurement each delegated run already
  produced: `execute()` times the child, `end()` adds that figure to the
  ledger, and a review round is charged once per panel member. A run still in
  flight is charged nothing until it ends, and a run whose wrapper died before
  it could record an outcome — `jobs cancel`, Ctrl-C, an OOM kill — is charged
  nothing at all, because nothing measured it. That last case is a deliberate
  gap and the only one: across the 41 recorded events of the six workflows
  measured while designing this, none was `abandoned` (38 `ok`, 2 `stalled`,
  1 `failed`), and a stall or a timeout is detected by a live wrapper that
  reaches `end()` with its measurement intact. What still bounds a runaway of
  killed runs depends on the path: `run <role>` consumes an attempt and a
  delegated run before it starts, so the per-stage budgets and
  `budgets.total_delegated_runs` hold it. The review paths consume neither, and
  a round only advances once one is consolidated — a retry of the same snapshot
  stays in the round it was already in — so a reviewer killed before it
  consolidates is bounded by nothing at all.

- **`review run` and `review run --design` consult the runtime budget.** They
  never did: a round is a delegated run per panel member per round, which
  makes review the largest consumer of runtime, and it was the one consumer
  that could not be refused. Both now exit 3 once the budget is spent, with a
  message naming it, and `--force` overrides it as it does the round budget.

- **The default `budgets.max_runtime_seconds` is 14400, was 7200.** Not a
  round-up: because concurrent work is summed, the new measurement is *larger*
  than the old one on workflows nobody interrupts — 1.23×, 1.30× and 1.45× the
  wall clock on the three measured. The figure is the heaviest workflow
  observed (5619s of delegated execution) plus one further round at its
  deadline ceiling (two reviewers and a fixer at 1800s each is 5400s), which
  comes to 77% of 14400 and does not fit in 7200 at all. **A configuration that
  sets `max_runtime_seconds: 7200` explicitly keeps that value with its new
  meaning**, which is stricter than it used to be for an autonomous workflow:
  remove the key to take the new default, or raise it. `config setup
  --defaults` does not write budget values, so a configuration that never set
  one gets 14400 automatically. `status --json` and `budget show --json` gain a
  `runtime` block (`used`, `limit`, `remaining`); `runtime_remaining_seconds`
  is unchanged and still present.

### Fixed

- **On Windows, a killed worker was reported as still running.** `pid_alive`
  asked `OpenProcess` for a handle and answered "alive" whenever it got one.
  A terminated process keeps its object, and its pid, for as long as anyone
  holds a handle to it -- and the killer holds one -- so the answer was yes
  for a worker `taskkill` had already reported dead. Measured: `taskkill`
  returned 0, `tasklist` no longer listed the pid, and this still said True.
  Nothing cleared such a stage, so a cancelled run stayed in flight until it
  passed 1.5x its deadline three quarters of an hour later, and `status` never
  named it. It now waits on the handle, which is what `SYNCHRONIZE` was being
  requested for. The test that should have caught it asserted the answer was
  `False` *or* `True`.

- **An adapter that could not read its CLI's output threw the measurement away
  with it.** `postprocess()` and `parse_usage()` ran outside every `try` in
  `Provider.run`, so an exception in either propagated past the point where the
  child's duration was known. `run_reviews` caught it two frames up and
  recorded a reviewer that had been running for minutes as a failure that took
  `0.0` seconds — the same reviewer, one branch over, is recorded with its
  duration under the comment "a failed review is not a free one". Both calls
  are now guarded, and each answers for what it reads: a `postprocess()` that
  raises means the answer is unreadable, so the run is not ok but still carries
  its duration; a `parse_usage()` that raises means only the invoice is
  unreadable, so the run keeps its output and its exit status and reports its
  usage as unmeasured. Neither reports a cost of zero. Exceptions raised before
  the child starts are unchanged: those runs really did cost nothing.

- **`optimization report` counts what the design review cost.** It selected
  rounds on `stage == "review"` *and* a recorded `optimization` block, and a
  design round has neither: its stage is `design_review`, and it carries no
  block because a plan has no diff to measure and no test result to gate on.
  So the one command whose job is to say what review cost reported half of it.
  Measured on one workflow with two design rounds and one code round:
  `Reviewer runs: 8 ... 558,884 billed`, while `tokens show` had a further
  four runs and 350,429 billed tokens that appeared nowhere. The
  `Reviewer runs:` line is now a total with a row per stage beneath it, and
  `--json` gains `design_rounds`, `design_reviewer_runs`,
  `design_measured_runs`, `design_billed_tokens` and
  `design_billed_per_round`. The existing keys keep their meaning:
  `reviewer_runs` and `billed_per_round` are still code review's.
  `design_rounds` counts the rounds that *ran*: one that failed or was
  abandoned after a kill billed nothing, so it neither counts as a round nor
  halves the per-round figure of the round beside it. The two
  per-round figures are never averaged together -- a round against a plan and
  a round against a diff are not the same unit of work -- and `levels in
  force`, the gate verdicts, the panel reduction and the escalations stay
  code-review-only, because no level decided anything for a design round. A
  project with design review off sees the line it always saw.

- **A budget reset no longer destroys the token account.** Every ledger writer
  goes through `load`, which hands back a blank ledger once one has been idle
  past `budgets.session_idle_reset_seconds` (6h) -- so the first write after a
  gap erased the account of a workflow that was still running. Measured on the
  workflow for issue #33: a six-hour pause between the code review and the fix
  stage turned `$13.78` across six stages into `$5.11` across one, and the
  architect's 462,124 and the implementer's 351,701 tokens had to be
  reconstructed from the event log that happened to survive beside them. A
  reset -- idle or `budget reset` -- now resets the budgets and carries the
  account across. The account answers what the workflow has cost, refuses
  nothing, and was never a budget. 0.4.2 fixed the reading side of this;
  the writing side is the rest of it. `budget reset` says what it did rather
  than announcing a fresh workflow.

- **`run --output` no longer overwrites the target with a bad result.** The
  file was written from `result.stdout` before the outcome was so much as
  looked at, so a stalled Architect replaced the 50,088-byte plan it had been
  asked to revise with the 150 bytes it managed to emit. The write now happens
  only when the run is `ok` and printed something; otherwise the existing file
  is left untouched and stderr says so, beside the `stalled` / `timed out` /
  `failed` line that explains it. The refused stdout is not thrown away either
  -- it goes to `<output>.rejected`, named in the same message, because it is
  usually the only account of what the run did instead of the work; a sidecar
  from an earlier attempt is removed rather than left to be read as this
  attempt's. A refused write exits 1 even when the run itself succeeded, so a
  chained command cannot take an untouched file for a fresh one, and a detached
  run records the refusal in its job, where `jobs show` reports it and `jobs
  wait` exits 1 on it, because the worker's stderr goes nowhere. Runs without `--output` and detached jobs' recorded
  output are unchanged.

- **The design request template asks for the plan on stdout.** It told the
  Architect to "write a plan to .ai/plan.md", which that role cannot do: it
  runs in plan mode and cannot write outside its own plans directory. It wrote
  the plan where nobody would look, exited 0, and printed a *report* of having
  done so -- and `--output` saved that report as the plan, plausible enough
  that the design review reviewed it and the implementer built from it. The
  Design and revision templates in `references/workflow.md` now ask for the
  plan on stdout and say why.

## [0.6.0] - 2026-09-18

### Added

- **`config prune [--scope …] [--dry-run]`** drops the values a layer holds
  that are equal to what it inherits, for the files written before this release
  -- the ones holding every default of their day. Opt-in, and it says what it
  assumed: nothing on disk tells a deliberate choice from an inherited default,
  so this reads equality as evidence. A project file is compared against your
  global layer rather than the built-in defaults, so a value placed there to
  cancel a global one survives.

### Changed

- **A saved configuration holds only what you set.** Every writer used to seed
  the file with the whole of `default_config()`, so one `config set` froze
  every other default beside it and a default improved in a later release
  never reached the installation -- that is how the low-risk thresholds raised
  in 0.4.2 failed to reach anyone who had run setup before them. `config setup
  --defaults`, the first `config set`, the wizard and `config reset` now write
  only what was asked for, and everything else is resolved from the layer below
  on every load. Existing files are read exactly as before and are not
  rewritten; `config prune` converts one on request.

- **`config reset` clears this layer's overrides** instead of writing the
  recommended configuration into it. For the global layer that comes to the
  same effective configuration; for a project layer it no longer pins the
  built-in defaults on top of your global choices, which was the worst place to
  freeze them -- a committed file the whole team reads. The subcommand's own
  help says so now, and `config set --scope project` still pins a value
  deliberately.

- **`config show --scope global|project` prints the layer as it is on disk**,
  raw, with a note that the rest is inherited, rather than summarising it as
  though it were a whole configuration. With `--json` the `config` key is now
  that layer and is `{}` when the file does not exist -- it used to answer with
  the built-in defaults for a global file that was never created. `config show`
  without a scope is unchanged.

- **The interactive `config setup --scope project` takes the global layer as
  its starting point**, so the values it recommends and the summary it asks you
  to approve are what the project will actually resolve to. It used to offer
  the built-in defaults, and pressing enter through it overruled the global
  layer with values nobody chose.

### Fixed

- **Editing reviewers in the global layer no longer copies the current
  project's panel into it.** The seed took the *effective* list, project layer
  included, so `reviewer add|remove|set --scope global` run inside a repository
  with its own panel wrote that panel into the machine-wide file. It was
  visible only with a hand-written sparse global file until now; with sparse
  writers it would have been the normal case.

- **An index past the end of a list is an error rather than a traceback.**
  `config set 'reviewers[99].role' …` exits 2 with `index out of range`.

- **Indexed edits work in a layer that does not already hold the list.**
  `config set 'review.exclude[0]' …` and
  `config set 'optimization.high_risk_paths[2]' …` copy the rest of the list
  from the layer below first; they used to fail with `not a list` anywhere the
  file did not already contain it.

- **Editing a file with no `version` writes one.** Empty and version-less files
  are still accepted by the loader, but `config set` and the `reviewer`
  commands no longer write them back without the key. A `version` the file
  states is left alone for `config validate` to report.

## [0.5.0] - 2026-09-17

### Added

- **The plan can be reviewed before it is implemented.** `review run --design`
  puts `.ai/plan.md` in front of the same panel, under the same rules --
  parallel, independent, read-only -- with `review show`, `review triage`,
  `review fix-brief` and `review status` all taking `--design` to match.
  Accepted findings become a revision request and the Architect rewrites the
  plan. A design mistake otherwise costs an implementation and a code review
  to discover, which is the most expensive way to find one.

  **Off by default** (`review.design.enabled`), because turning it on is a
  reviewer run per panel member per round plus an Architect re-run, and no
  existing workflow should start paying that without being asked. One command
  enables it: `dev-orchestra config set review.design.enabled true`.
  `review.design.max_iterations` (2) bounds the rounds; `1` is the cheap
  setting.

  Its artifacts live in `.ai/reviews/design/` -- its own reports, consolidated
  report, round counter and triage -- so a design round can never advance, or
  be refused by, the code review's count. No new `budgets` key: the rounds are
  bounded by `review.design.max_iterations` and the rewrites by
  `budgets.architect`. The optimization gate and panel reduction do not apply:
  there is no test result that says anything about a plan and no diff to
  measure, and a design decision is where cross-model disagreement earns its
  cost.

### Changed

- **The SKILL.md character ceiling is 13,000, up from 12,500.** The document
  was within twelve characters of the old one. Five paragraphs were compressed
  to pay for most of the new stage before the ceiling moved for the rest; the
  budget exists because the document is resident in every session, so it is
  still not a target to grow into.

### Fixed

- **A cp932 console no longer loses a finished run to one em dash.** The
  tolerance added for this only relaxed a `strict` stream, which is the one
  handler Windows never supplies: CPython gives `sys.stdout` the
  `surrogateescape` handler there, and that rescues lone surrogates and
  nothing else. So the guard skipped the only console it existed for, and
  `run implementer` went on crashing in `_out` after the delegated CLI had
  finished and applied every edit. Any stream that cannot encode everything is
  now relaxed unless its handler is one that already cannot raise, and `_out`
  degrades the characters rather than dropping the message when the stream
  cannot be reconfigured at all. The wizard prints through the same writer, so
  `config setup` cannot lose the console it is configuring.

  `--json` escapes non-ASCII itself on such a console rather than leaving the
  character to be degraded: `\xe9` is not an escape JSON defines, so tolerating
  the console would otherwise have replaced a crash with output that parses as
  nothing. Where the console can encode everything the output is unchanged.

  The test console was built with `strict`, so the suite agreed with the guard
  and both were wrong about the same thing. It is built the way Windows builds
  it now.

## [0.4.4] - 2026-09-17

### Added

- **`doctor` lists settings pinned at a value the built-in default has moved
  off**, with both numbers. `config setup --defaults` writes every default into
  the file, so improving a default never reaches an existing installation: the
  file goes on answering with the number that was current when it was written.
  0.4.2 raised the low-risk thresholds from 2 files / 50 lines to 5 / 150 so
  the panel reduction could fire at all, and every configuration written before
  it kept reporting the old pair -- so that release did nothing for anyone who
  had already run setup. Found by reading a report where `panel reduced` stayed
  at 0 after the upgrade.

  Reported, never corrected: "chose 2 deliberately" and "inherited 2 from an
  older default" are the same two characters on disk. Not a problem either, so
  `--strict` does not fail over a configuration that works.

### Fixed

- **A queue of writers ran the file lock's deadline down and lost an edit.**
  The wait was five seconds in total, which quietly made the guarantee depend
  on how many writers were ahead: measured at twelve contenders holding for
  half a second each, two gave up and clobbered each other's budget consume.
  Caught by a Windows CI run where the whole suite was slow enough to reach
  it. A deadline exists to survive a holder that died, and a queue moving
  along is not that -- so every visible change of hands pushes it out again,
  and `max_wait` backstops the case that is neither: a holder alive enough to
  keep the file fresh and stuck enough never to release it.

  Waiters read the lock file to tell a queue from a corpse, and that read goes
  through the shared-delete path for the reason it exists: a plain `open` on
  Windows does not grant delete sharing, so reading the file to detect
  progress stopped the holder releasing it -- a poll meant to observe progress
  preventing it. Each acquisition writes a token nobody else repeats, because
  the pid cannot tell one holder from the next when the contenders are threads
  of one process, which is the ordinary case here.

## [0.4.3] - 2026-09-17

### Fixed

- **A console that could not encode one character killed a finished run.** On
  Japanese Windows the console is cp932, and a delegated agent writes prose: a
  single em dash in a summary and `sys.stdout.write` raised
  `UnicodeEncodeError`. Every edit had already been applied, so the work was
  done and only the report of it was lost. Unencodable characters are now
  escaped (`\u2014`) rather than dropped or fatal -- the output is read by the
  orchestrating agent as well as by a person, and `?` throws away which
  character it was. A stream whose error handler someone chose deliberately is
  left alone.

- **And the crash took the bookkeeping with it.** The output was printed
  *before* the ledger was written, so the failure lost the run's token
  accounting and left the stage marked in flight -- and the next command then
  reported that finished run as abandoned. A success read as a stall. The
  account is written and the stage closed before anything is printed: nothing
  below that line decides whether the run happened.

## [0.4.2] - 2026-09-17

Everything here came out of one real measurement: eleven review rounds across
two workflows, 2,136,694 billed tokens, and an `optimization report` that had
to be run three times by hand to see them.

### Changed

- **`balanced` reduces the panel for a small, low-risk change.** Restricting
  that to `aggressive` made it unreachable in the repositories that most needed
  it: a high-risk match escalates to `quality`, and `quality` is not
  `aggressive`, so in an infrastructure repository where `*.tf` matches on most
  rounds the dial could not fire at all. Measured over those eleven rounds at
  `balanced`: the panel was reduced zero times. `quality` is now the only level
  that always pays for the whole panel, which is what that level means. What
  stops a reduction is still risk, not size -- a one-line change to an auth
  file gets the full panel.

- **The low-risk thresholds were raised to 5 files / 150 lines** (from 2 / 50).
  No round in the measured set came close to the old pair, and a threshold that
  never fires is not a conservative default, it is a dead one.

- `ledger.workflow` is now `ledger.epoch`. A workflow is a directory under
  `.ai/workflows/` as of 0.4.0, and two identifiers sharing one word cost a
  real analysis an hour: a ledger whose `workflow` did not match the directory
  holding it read as a ledger carried between directories, when it was only the
  other namespace. A ledger written before the rename is read as it stands.

### Fixed

- **`tokens show` hid the account of any workflow idle for six hours.** It read
  the ledger through the same call the budgets use, which hands back a blank
  ledger once one has gone stale -- right for a budget, since the next request
  should start with a full one, and wrong for an account that refuses nothing.
  Reported from real use as "tokens show says no runs while state.json holds
  446,430". It now reads what is on disk.

- **The cost column read as authoritative while one provider never priced
  anything.** Codex reports its tokens and no money, so a run counted as
  measured and the "totals are a floor" caveat stayed quiet -- over a cost
  total that omitted a provider entirely. Runs that reported tokens without a
  cost are now counted separately and said out loud. An account written before
  this release cannot answer the question and makes no claim either way.

## [0.4.1] - 2026-09-17

### Fixed

- **`optimization report` only saw one workflow.** It reads the run log rather
  than the ledger for a reason: a level's effect is a *rate* -- how often it
  refused a round, how often it cut the panel -- and a rate needs rounds, which
  the event log accumulates and `budget reset` does not clear. Splitting the run
  log per workflow in 0.4.0 quietly took that away again: one workflow is a
  handful of rounds. It now reads every workflow in `.ai/`, and `--workflow
  <id>` narrows it to one -- "what did the level do in this piece of work"
  rather than "in this repository".

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

[Unreleased]: https://github.com/istb16/dev-orchestra/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/istb16/dev-orchestra/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/istb16/dev-orchestra/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/istb16/dev-orchestra/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/istb16/dev-orchestra/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/istb16/dev-orchestra/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/istb16/dev-orchestra/compare/v0.4.4...v0.5.0
[0.4.4]: https://github.com/istb16/dev-orchestra/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/istb16/dev-orchestra/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/istb16/dev-orchestra/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/istb16/dev-orchestra/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/istb16/dev-orchestra/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/istb16/dev-orchestra/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/istb16/dev-orchestra/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/istb16/dev-orchestra/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/istb16/dev-orchestra/releases/tag/v0.1.0
