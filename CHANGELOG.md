# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

What that promise covers -- the configuration schema, the `dev-orchestra`
commands, flags, exit codes and `--json` output, the `.ai/` artifact formats
and what the built-in adapters do -- and what it does not, is under
"Compatibility" in `README.md`. The `Provider` base class a user adapter
subclasses is not covered and may change in a minor version; such an entry
below begins **User adapters** so adapter authors can find it.

## [Unreleased]

### Added

- **Keys nothing reads are reported.** A misspelt `reveiw:`,
  `review.timout_seconds` or `implementer.provder` used to pass as valid and
  be ignored. `config validate` now lists each one under *Warnings*
  (`warnings` in `--json`) with the file it is in and the known key it
  resembles, `doctor` lists them as notes, and `config set` warns when the
  key it writes is one. A warning, not a problem: the exit status, `doctor
  --strict` and every command go on as before. Provider `options` (a user
  adapter's own keys included), tier names and `budgets` take keys of their
  own and are not checked (#281).

- **A project file that loosens a review gate is reported.** The project file
  can come with the branch under review, so when it makes `reviewers`,
  `review.design.reviewers`, `review.max_review_iterations`,
  `review.re_review_severities`, `review.exclude`, `review.max_findings`,
  `review.design.enabled`, `review.design.max_iterations`,
  `optimization.level`, the high-risk, security or architecture patterns,
  the low-risk thresholds or `optimization.skip_unneeded_roles` looser than
  the global config, its preset and the defaults would, `config validate`
  warns, `doctor` notes it (`--strict` still passes; `config.loosened` in
  `--json`) and `review run` prints a `warning:`. The values still take
  effect. What counts as looser for each key is in
  `references/configuration.md` ("What the project file may not loosen")
  (#279).

### Changed

- **`review.re_review_severities` and `review.parallel` are validated.** A
  mistake in the severities used to leave nothing blocking while `config
  validate` answered valid: a single `critical` not in a list was read a
  letter at a time, `[crit]` matched no finding, and a number crashed. The
  list must now name at least one of `critical`, `high`, `medium` and `low`;
  a single name, an unknown name, a non-string and `[]` are refused, and
  `review.parallel` must be `true` or `false`. Names are read in any case,
  so `[Critical, High]`, which used to block nothing, now blocks as written.
  `status` and `review status`, which read the file without validating it,
  block on the default `[critical, high]` for a value that would be refused
  (#277).

- **`workspace.dir` must be a non-empty string.** `dir: 5` was valid and
  crashed `status`; it is now refused, and a command that reads the file
  without validating it uses `.ai`, as `config suggest-roles` already did
  (#281).

- **Run from a subdirectory holding its own `.dev-orchestra.yaml`, every
  command now reads that file.** `workspace.dir` used to be read from the
  repository root's file while roles and budgets came from the
  subdirectory's, and a `run --detach` worker, started in the root, ran the
  model the root's file named rather than the one the foreground run would
  have used. The worker is now given the parent's directory, and `.ai/` is
  placed by the same file as everything else (#282).

- **Without PyYAML, a config file indented with tabs, or using anchors,
  aliases, tags or block scalars, is refused with the line number.** It used
  to be read, sometimes wrongly; write it with spaces and plain values (#275,
  #278).

- **`design.require_approval`, and a `workspace.dir` outside the repository,
  are taken only from the global config.** In the project file they are now
  ignored: the global value or the default is used, `config validate` and
  `review run` warn, and a refused `run implementer` says the project file's
  `false` was ignored. `doctor` reports a problem when the ignored value would
  have loosened what is in force, and a note otherwise, so a project
  `design.require_approval: true` does not fail `--strict`. A `workspace.dir`
  holds the approval record, so one pointed outside the repository by a
  branch could bring an approval nobody gave; a relative one inside the
  repository still works from the project file, unless a symlink or junction
  in the repository takes it outside. A record the branch commits inside the
  workspace is not something this can refuse; `references/configuration.md`
  says what the approval is read from. `config set
  design.require_approval false` now writes the global file even inside a
  project that has its own, and `--scope project` with either key, or with
  a `design` or `workspace` block holding one, exits 2.
  To keep running a project without plan approval, move the setting:
  `dev-orchestra config set --scope global design.require_approval false`
  (#279).

- **`workflow remove` refuses a workflow that is still in use** (exit 2):
  one with a stage in flight, or with a detached job in its `jobs/` that has
  not finished (a job whose worker is gone is marked `abandoned` first and
  does not count). It used to delete it, and a detached worker finishing
  afterwards wrote its state back, leaving a workflow with an empty record,
  the job's files and empty `execution/` and `reviews/`. Recent activity
  alone is no reason to refuse, so a workflow finished a moment ago is still
  deleted. `--force` deletes it anyway, for an in-flight mark a crashed stage
  left behind (#285).

- **`state record test` and `state record re-test` accept only `ok` and
  `failed`** (exit 2 otherwise). Any other word was recorded, and the review
  gate, which knows only `failed`, `fail`, `error` and `red` as failures, read
  it as a pass: `failure` or `NG` sent red tests to review. Other stages
  still take any status. `--detail` can no longer set `stage`, `status` or
  `at`, which overwrote the event's own fields (exit 2) (#287).

### Fixed

- **Deadlines and poll intervals must be a number of seconds above zero.**
  `run --timeout`, `run --idle-timeout`, the same two on `review run`, and
  `jobs wait --poll` took anything: `0` or a negative stalled a run at once
  after spending its attempt, `--timeout 0` quietly meant the configured
  deadline, `--poll -1` ended `jobs wait` on a traceback and `--poll 0`
  spun for the whole wait. They are now a usage error (exit 2), as are
  `nan`, `inf` and anything over 1,000,000,000 seconds, which ended on an
  overflow; `jobs wait --timeout 0` still looks once. `options.idle_timeout`
  is held to the same rule by `config validate` for every adapter that takes
  it, where `0`, `true` or `"abc"` used to pass and `"abc"` ended `run` on a
  traceback that left its in-flight entry open, and the deadlines in the
  configuration file get the same upper limit. Should a run still raise
  before the provider hands back a result, Ctrl+C included, the entry is
  ended as failed, and a detached job failed, before the error surfaces
  (#272).

- **A UTF-8 prompt piped to `run` is no longer read as cp932 on a Japanese
  Windows.** `--prompt-file -` and a bare pipe read stdin in the encoding
  Python gives a pipe there, the ANSI code page (cp932), so a UTF-8 prompt
  was delegated as mojibake. Stdin is now read as UTF-8, as a
  `--prompt-file` is. Bytes that are mostly not UTF-8 and read cleanly in
  the code page (`type` of a file saved as cp932) are read in it; anything
  else stays UTF-8 with its undecodable bytes replaced, so one stray byte
  neither raises nor garbles the rest. A byte-order mark at the start of a
  prompt, piped or in a file, is dropped. Windows PowerShell 5.1 still turns
  non-ASCII text into `?` before it reaches the pipe unless
  `$OutputEncoding` is set to UTF-8 (#284).

- **On Windows, a `claude.cmd` or `codex.cmd` installed by npm runs.**
  `doctor` found it through PATHEXT and reported it installed, but it was
  started by its bare name, which Windows looks up as an `.exe` only, so
  `--version` could not run and every run exited 126. A CLI is now started
  from the absolute path `doctor` found, and both look only in the absolute
  directories on PATH: never in the current directory, which Windows
  searches first and which may be a repository carrying a `claude.cmd` of
  its own. A name not found there is not started. An npm shim is bypassed for the `node` and
  script (or the `.exe`) it would run, so no argument passes through
  cmd.exe; any other `.cmd` or `.bat` runs under cmd.exe with every argument
  quoted, and an argument holding `"`, `%`, `!` or a line break, which
  cmd.exe would read as its own syntax, is refused (exit 126) rather than
  passed on (#269).

- **Without PyYAML, a Windows path or a string of digits written to a config
  reads back unchanged** (#275). The bundled parser decoded `\\n` in a
  double-quoted string as a backslash and a newline, so `C:\work\new` came
  back as `C:\work\` and `ew` on a new line; it now decodes every escape in
  one pass, refuses one YAML does not define, and refuses text after a closing
  quote. A string that looks like a number or a date (`"123"`, `"1.0"`,
  `".5"`, `"2026-10-06"`) is written in quotes, so it no longer comes back as
  a number; a tab inside a value is kept instead of becoming two spaces, a
  tab in the indentation is refused, and an apostrophe in an unquoted value
  (`it's`) no longer hides the comment after it.

- **Without PyYAML, YAML the bundled parser cannot read is refused instead of
  read wrongly** (#278). A sequence item with more than one space after its
  `-` (`-   id: a`) lost every key after the first, which surfaced as an
  unrelated "provider is required"; any spacing now reads, and an item line
  out of column is an error naming the line. Anchors, aliases (including an
  unquoted `- *.sql`), tags, merge keys, every block scalar form (`|-`,
  `>2`, `- |`) and a second document after `---` or `...` are refused with
  the line number, where they were taken as plain text or joined into one
  document. On the command line, `config set` still takes `*.sql` as text.

- **A configuration file saved as UTF-8 with a BOM keeps its first key.**
  Notepad and PowerShell 5's `Out-File -Encoding utf8` start the file with
  one, and it became part of the first key: `review:` was read as another
  key, ignored, and `config validate` still answered valid. Every file is now
  read with the BOM dropped, YAML and JSON alike (#276).

- **Writing to a `.dev-orchestra.json` keeps it JSON.** `config set --scope
  project`, `reviewer add` and every other writer wrote YAML with `#`
  comments into it, which dev-orchestra still read but editors, `jq` and CI
  checks did not. A file whose name ends in `.json` is now written as
  indented JSON, without the header comment. A value JSON cannot hold
  (`.inf`, `.nan`) is refused, and the file left as it was (#280).

- **`config set language.reply no` saves the tag `no`** (Norwegian) rather
  than `false`. In a file, a bare `no` is still read as false and refused;
  the message now says to quote it. `no` is the only such word kept as a
  tag: `off`, `yes`, `on` and the like name no language, and are saved and
  warned about as before (#281).

- **The Claude Code install and uninstall no longer delete a directory they
  did not make.** They removed whatever was at `.claude/skills/dev-orchestra`,
  so running the installer from a clone made there deleted the clone itself,
  and a directory of the user's own went the same way. They now apply the
  Antigravity install's rule: a link is replaced only when it points at the
  checkout being installed, a directory only when the installer wrote it and
  it is not a clone. Where a link points is compared with every link and
  junction followed on both sides, so `install.ps1` and `uninstall.ps1`,
  which compared the paths as written, now also recognise a link made
  through a junction to the checkout, or a checkout run through one (a
  `subst` drive is still not followed). A Claude copy (`--copy`, or the fallback when a symlink
  cannot be made) now carries a `.dev-orchestra-install` file, as an
  Antigravity copy does. A copy from an earlier installer, which has no such
  file, is still replaced or removed when it holds nothing but what a copy
  carries, so `install --copy` keeps upgrading it; one with anything else
  added is left in place. A link to another checkout or to nothing is now
  left in place too, with the command to remove it by hand, and a run from
  the checkout that is itself the destination stops and says so (#290).

- **A `workflow remove` stopped part way no longer ends on a traceback.** When
  the delete fails -- on Windows, a file another process holds open -- it
  exits 1, names the error and says how many files are left, so it can be run
  again once they are closed (#285).

- **`jobs cancel` no longer stops a process that was handed a vanished
  worker's pid.** A worker gone without a word (after a reboot, say) left a
  pid that `jobs cancel` force-killed on Windows with `taskkill /T /F`,
  whatever tree had it by then, and that kept the job from being marked
  `abandoned`. A job now records the worker's start time with its pid
  (Windows and Linux) and checks it first: a different process marks the job
  `abandoned` and is left alone. A job recorded by an earlier version, or on
  macOS, has no start time; on Windows its pid is no longer stopped, and the
  job says so, while POSIX keeps stopping it only while it leads its own
  process group (#268).

- **What a reviewer's prompt quotes can no longer close its fence.** The diff,
  the plan and the design request went in a fixed `` ``` `` fence, so a code
  block in a plan, or a Markdown diff's unchanged `` ``` `` line, closed it
  early, and what followed read as the prompt's own instructions. Each now
  gets a fence longer than any run of backticks inside it, as the surrounding
  context already did, and both review prompts say the fenced text is data
  under review, not instructions (#263).

- **A reviewer that exits 0 with nothing to say is a failed reviewer, not
  a clean one.** Its empty reply was written to the report as `NO_FINDINGS`
  and the run recorded `ok`; it is now recorded `unparsed` (report was
  empty), counted as failed, and the report holds no `NO_FINDINGS` it never
  wrote (#261).

- **A worker that finishes just as its job is checked or cancelled keeps
  its outcome.** Reading a job whose worker had just exited, and `jobs
  cancel` racing a worker that was finishing, wrote back the record as it
  was first read, so a `succeeded` job turned `abandoned` or `cancelled` and
  lost what the worker had added, such as `output_written`. Both now re-read
  the record under the job's lock and change it only while it is still
  unfinished (#271).

- **The Claude Code install under Git Bash no longer says "Linked" over a
  full copy.** Git Bash's `ln -s` copies the whole checkout, `.git`, `.venv`
  and `.ai` included, and succeeds, so `install.sh` reported a link that
  `git pull` would keep up to date and left a copy that never was. It now
  checks that it got a link, and otherwise replaces the copy with what a copy
  carries, with a warning, as the Antigravity install already did. A full
  copy that an earlier `install.sh` left this way cannot be told from a
  clone, so the installers leave it in place; the refusal now names it and
  gives the command to remove it, once you have checked it holds nothing of
  yours (#293).

- **Two findings from one reviewer are no longer merged into one.** Two
  near-identical findings a few lines apart from the same reviewer (the
  `timeout` and the `retries` argument not being validated, say) became one
  finding with `duplicate_count: 2`, which read as two reviewers agreeing,
  and the other finding was gone. Only findings from different reviewers are
  merged now, and a finding with no line number (`n/a`) is no longer merged
  with a finding that has one anywhere in its file; such a pair is listed as
  a possible duplicate when the two quote the same code. Two findings
  without a line number, such as design findings on one plan section, can
  still be merged. A merged finding keeps each report as its reviewer wrote
  it in `merged_reports`, listed under it in `consolidated.md`, and
  `duplicate_count` is the number of reviewers who reported it (#259).

- **`review triage` run in parallel no longer loses decisions.** Each call
  read `consolidated.json`, set its own decision and wrote the file back, so
  a later write dropped an earlier decision while every call printed
  `Triaged … as …`. Triage now reads and writes the report under a lock, and
  `review run` and `review consolidate` build and save it under the same
  lock, so a decision made while a round is being consolidated is carried
  into it (#262).

- **`review run --base` is no longer ignored when a snapshot is already on
  disk.** The base was used only to take a missing snapshot, so a snapshot
  left from earlier was reviewed instead -- an empty one failed with
  `review snapshot is empty … or pass --base`, and passing `--base` failed
  the same way. A snapshot taken against another base is now retaken
  against the one given, with a `note:` saying so, and the round count
  starts again as it does after `review snapshot --base`. A snapshot taken
  without a base counts as taken against `HEAD`, and one taken against a
  name for the same commit as `--base` is kept. Without `--base` the
  snapshot on disk is reviewed as before (#264).

- **Code in a finding's fenced block is read as code.** A `#` comment inside
  ```` ``` ```` or `~~~` was dropped, a line such as `fix: …` started a new
  field, a line that read like a `Finding` heading cut the finding in two and
  dropped the rest of it, and every line was joined with a space, so the fix
  brief handed the fixer code without its comments or its shape. Lines inside
  a fence are now kept as written and never start a finding, and `Evidence`
  and `Fix` keep their line breaks; the other fields are still one line. A
  fence that is never closed is read as before. In the fix brief and in
  `consolidated.md` such a value goes under its label, indented into the
  list item, so its fences no longer leave the list and swallow the rest of
  the document (#265).
## [0.22.0] - 2026-10-06

### Added

- **`run.timeout_seconds.<role>`: the total deadline of one `run`**, per role
  (`orchestrator`, `architect`, `implementer`, `review_fixer`; default 3600
  for the implementer, 1800 for the others). `--timeout` still overrides it
  for one run, and a detached worker resolves the same value. A top-level key
  rather than part of the role's block, so setting it never takes a role out
  of the preset's fit and `config setup --preset` keeps it. `config show`
  lists every deadline under *Deadlines*, with the file that set it, and a
  run killed at its deadline names the key and where it was set (#258).

### Changed

- **`review.timeout_seconds` no longer bounds `run`.** It bounds reviewers
  only: `review run` and `run <reviewer-id>`. `review.idle_timeout_seconds`
  is still shared by both (#258). What changes for `run`, by what the file
  set:
  - `review.timeout_seconds` raised, e.g. to 3600: the implementer keeps
    3600 by default; architect, review_fixer and orchestrator runs drop to
    1800.
  - Set low, e.g. 900: runs lengthen to 1800, or 3600 for the implementer.
  - Left at 1800: only the implementer changes, to 3600.

  To keep a previous cap, set it per role, e.g.
  `dev-orchestra config set run.timeout_seconds.architect 900`. Reviewers
  keep whatever `review.timeout_seconds` says; lower it again if it was
  raised for the implementer's sake.
- **The implementer's default run deadline goes from 1800 to 3600 seconds.**
  Measured implementer runs went past half an hour, and one was killed at
  1800. Orchestrator, architect and review_fixer stay at 1800 (#258).

## [0.21.0] - 2026-10-06

### Added

- **`language.reply`: the language the orchestrator answers in**, as a
  language tag (`ja`, `ko`, `zh-TW`, `ru`, `es`, `fr`, ...; default `null`, which
  leaves it to rule 11 as before). `doctor` prints it as *Reply language*,
  which the skill reads as the language the user asked for on every host. In
  Claude Code, dev-orchestra adds three hooks to the user's settings
  (`~/.claude/settings.json`, or under `$CLAUDE_CONFIG_DIR`; never a
  project's): a short reminder naming the language before each prompt and
  after a compaction or a resume, and a Stop-hook check that asks once for a
  reply clearly in another language to be written again. They are added when
  `config set language.reply` or `config setup --language` sets a tag in a
  file that held none and Claude Code's settings directory exists -- never
  because a project's file sets one -- and removed when neither the global
  file nor the project's sets one any more; `--no-hooks` leaves the settings
  alone. Only dev-orchestra's own entries are changed, the previous file is
  kept as `settings.json.dev-orchestra-backup`, a linked settings file stays
  linked, and a file it cannot parse is refused and left as it is. The
  entries run the Python that runs dev-orchestra (the one a virtual
  environment was made from, when it runs in one), with no shell, through a
  small relay in the config directory that a dev-orchestra command run inside
  Claude Code from a plugin Claude Code installed points at that checkout, so
  they keep working across plugin updates and on Windows without Git Bash.
  Users who never set a language get no hook at all.
  The check judges by script: the languages written in their own script are
  checked against Latin text and other scripts (a Japanese reply fails under
  `zh`, a Chinese one under `ja`, a Korean one under either); English,
  Spanish, French, German, Portuguese and Italian are told apart by their
  common words; any other Latin-script language is checked only against a
  reply mostly in another script; and a tag it does not know gets the
  reminder alone. The hooks act only in sessions that used dev-orchestra, never in a
  run it delegated (the providers mark their children with
  `DEV_ORCHESTRA_DELEGATED`), and print nothing on any error.
  `language.rewrite: false` keeps the reminders and turns the check off.
  Codex and Antigravity get the setting through `doctor` and rule 11 only.
  `doctor --json` has it under `language`, with the hooks' state under
  `language.hosts.claude`, and `config show` adds a `Reply language:` line
  (#254).
- **`dev-orchestra hooks install|uninstall|status`**: add, remove or inspect
  the reply-language hooks by hand. `install` and `uninstall` take
  `--dry-run`; `status --json` reports `installed`, `stale` with its reasons,
  `not-installed` or `unreadable` (#254).
- **`config setup --language <tag>`**, and a last wizard question for the
  reply language (#254).

### Changed

- **claude 2.1.290 resumes the architect's session**: it is added to the
  verified table after passing every required check on 2026-10-06 (the two
  symlink checks were skipped, as before; they are not required).

- **The compatibility promise is written down once**, under "Compatibility"
  in `README.md`: what 1.0 covers (the configuration schema, commands, flags,
  exit codes and `--json` output, the `.ai/` artifacts, what the built-in
  adapters do) and what it does not (the `Provider` base class, text output,
  the maintenance scripts and their `verified/` records, module APIs). A
  `--json` key is never removed, renamed or retyped. The preamble above,
  CONTRIBUTING, `references/workflow.md` and `references/providers.md` point
  to it (#242).

### Removed

- **The adoption of a flat `.ai/` written before 0.4.0.** A command run where
  `.ai/` still holds `plan.md`, `state.json`, `execution/`, `reviews/` or
  `jobs/` at the top level refuses (exit 2), names them and says what to do:
  move them aside or delete them, or run dev-orchestra 0.20.0 once first,
  which adopts them. Nothing is moved or deleted; `workflow list` notes them.
  Meant as the last breaking change before 1.0 (#242).
- **The `workflow` key of the budget ledger**, renamed `epoch` in 0.4.2, is no
  longer read as such. A ledger with only the old key has it renamed `epoch`
  when it is loaded, keeping the same value, so the review round counters and
  a stage in flight across the upgrade carry on as before; its budgets, its
  token account and the events are untouched (#242).

## [0.20.0] - 2026-10-06

### Added

- **`smoke_live.py --model <provider>=<model>` runs a provider's checks on
  another model.** That provider writes no live-check record and no resume
  pass, but a resume failure is still recorded. `--json` carries the note
  that says so under `notes`.

- **A design review panel of its own** (#248). `review.design.reviewers`
  replaces the code panel for design rounds, and `review.design.reviewers_extra`
  adds to whichever design panel is inherited; with neither, design rounds
  run the preset's fitted design panel (below), or the code panel with `when`
  ignored where a file lists `reviewers`, as before. A design seat may be
  `when: high-risk` (it joins a plan with a high-risk hit) but not
  `when: paths`. `reviewer list|add|remove|set --design` manage it, and
  `config show`, `doctor` and `review status --design` show it.

- **`high_risk_model` per seat**, in either panel: the model a seat runs on a
  round with a high-risk hit or `--high-risk`, with a note saying so. The
  provider and options stay the seat's. `reviewer add|set --high-risk-model`,
  `reviewer set --clear-high-risk-model`; `reviewer set --provider` to another
  CLI removes it, with a note, unless `--high-risk-model` names the new one.

- **`review run --design --high-risk`** is accepted: it adds the design
  panel's `when: high-risk` seats, keeps every role and switches seats to
  their `high_risk_model`.

- **`reviewer add|set --relevance security|test|architecture|always`**
  (`set` also takes `default`), and the settings
  `optimization.skip_unneeded_roles`, `security_paths`,
  `extra_security_paths`, `architecture_paths` and
  `extra_architecture_paths` (see Changed).

- **Each preset fits a design panel** (#248), to the installed CLIs as the
  code panel is: `standard` general sonnet (opus on a high-risk plan),
  security sonnet and test sonnet, all on Claude; `quality` adds a Codex
  general seat, security on opus and architecture on opus; `fast` general
  sonnet. A config that names no panel runs it on design rounds.
  `review.design.reviewers` is a key a preset governs. A design seat that is
  refitted gets a note naming `design reviewer seat <n>`. The design panel's
  source reads `the preset's fit` (`fit` in JSON) in `reviewer list
  --design`, `review status --design` and `doctor`, whose `Design:` line now
  shows it; fitted seats are labelled `fit design`. `reviewer add --design`
  says the design panel still follows `preset standard's fit`, and `reviewer
  set|remove --design` say they copied it, recording the seats, in either
  file. `doctor` notes a design list that holds the inherited design panel
  plus more -- in a project file under a global `reviewers` list, that is the
  global code panel without `when`.

- **Vendor seats in presets.** A Claude seat sits on Claude whenever it is
  installed; a Codex seat sits on Codex, else on another installed CLI, else
  is not added with a note (`it is a second vendor's opinion and nothing
  installed stands in for one`). Neither moves with the implementer. A seat's
  `high_risk_model` is kept on Claude only; one dealt elsewhere loses it, and
  its note ends `; no high-risk model on <cli>`.

- **`optimization report` scores each seat's usual and high-risk model
  apart** (#248). A seat that ran its `high_risk_model` gets a row per slot
  under its own, labelled with the models the slot ran (`usual (sonnet)`,
  `high-risk (opus)`), with runs, cost, cost per run, findings and rates; a
  finding counts for the slot of the run that first reported it. A seat that
  only ran its usual model prints as before.

- **Records, additive**: a reviewer entry that ran on its high-risk model
  carries `model_slot: high-risk` (an entry without it is the usual slot); a
  design round's event carries an `optimization` block (`level`,
  `high_risk`, `declared`, `conditional`, `files`); `optimization report
  --json` gains `relevance` and `design_relevance`, and each scorecard
  reviewer gains `models` (per slot, each with `by_model`) while every
  scorecard group gains `billed_per_run` and `cost_per_run`. Records written
  before read as they did, a run without a slot as `usual`.

### Changed

- **A `test` or `architecture` seat sits out a round with nothing for it**,
  in code review and design review and at every optimization level,
  `quality` included. On code review: a docs-only change (test), or one that
  stays in one directory under 6 files with no contract, schema, config or
  CLI path (`optimization.architecture_paths`); on design review, the same
  rules on the plan -- the first time a design round leaves anyone out.
  Nothing changes for a `general` seat, a `when: high-risk` or `when: paths`
  seat, or a round with a high-risk hit or `--high-risk`; a carried finding
  or `--only` also keeps the role -- on a design round, an accepted finding
  of the live design report keeps its seat on a revised plan too. The
  default `standard` panel's `claude-test` is the seat affected; a `quality`
  preset user's `architecture` and `test` seats are too.
  `config set optimization.skip_unneeded_roles false` restores the previous
  behaviour whole; `reviewer set claude-test --relevance always` restores
  one seat. `review run` prints a note for each seat left out and the way back
  in (`--only <id>`), `status` adds them to its Optimization and Design review
  lines, and `optimization report` prints a `roles skipped` row per stage with
  the count by level and an estimated saving.

- **A `security` seat keeps running every round unless it opts in** with
  `relevance: security`. Its rule reads changed path names, never what the
  change does, so a vulnerability added to an ordinarily named file would
  miss it; opting in trades that security coverage for cost. Opted in, it
  sits out a change matching none of `optimization.security_paths` plus every
  high-risk pattern, a list that also covers file, path, URL, fetch, client,
  query, SQL and database names.

- **A configured design panel honours `when: high-risk`** against the plan.

- **New preset defaults, and who they reach** (#248). A config that names no
  panel -- the implicit `standard` included -- gets the new code and design
  panels below. A file that lists `reviewers` keeps exactly that code panel,
  and its design rounds keep running a copy of it without `when`, with no new
  fit notes; a file that lists `review.design.reviewers` keeps that design
  panel. `claude-security-2` is no longer a built-in seat: a script that
  names it in `--only`, or a `reviewer remove claude-security-2`, has nothing
  to find.
  - **The built-in security seats merge into one.** `claude-security` runs
    sonnet, and opus on a high-risk change (`high_risk_model`), in place of
    sonnet plus a second opus seat on high-risk changes. The default code
    panel is four seats; a high-risk round runs security once, on opus.
  - **`standard`'s design rounds** run three seats -- general sonnet (opus on
    a risky plan), security sonnet, test sonnet -- instead of the five-seat
    code copy: no Codex design seat, and general on sonnet unless the plan is
    high-risk. `reviewer add --design` or `config set
    review.design.reviewers ...` brings a seat back.
  - **`quality`** keeps its specialists on Claude and adds Codex general and
    security seats beside them, so it no longer rotates with a project
    implementer on Codex; its design review is `auto` instead of `true`.
    Preset security seats set no `relevance`, so they run every round,
    including `quality`'s opus design security seat on each design round
    `auto` lets through; `reviewer set claude-security --design --relevance
    security` trades that for cost.
  - **`fast`** turns the design review off and its high-risk security seat is
    sonnet instead of opus.
  - **Codex alone under `standard`** keeps `codex-security` on every round,
    where it was `when: high-risk`; the sonnet test seat is still not added
    there.
  - **`reviewers_extra` no longer joins design rounds under the preset's
    design panel.** Code extras -- from `reviewer add` or `suggest-roles
    --write` -- joined design rounds when those ran a copy of the code panel;
    a file that lists no `reviewers` now runs the fit's design panel there,
    and code extras review code only. `doctor` notes each such extra; adding
    it to `review.design.reviewers_extra` as well brings it back to design
    rounds.
  - **Listing `reviewers` also takes the design rounds off the preset's
    design panel**, onto a copy of that list without `when`. A writer that
    first copies the panel into a file (`reviewer set|remove`, `config set
    reviewers[n]...`) says so on its `now lists the reviewers` note; `config
    prune` keeps such a list even when it equals the fit (`Kept reviewers in
    <file>: ...`), and the wizard says so when an adjusted preset saves one.
  - **`config setup --preset`** now also removes a `review.design.reviewers`
    list from the file, keeping `review.design.reviewers_extra`.
  - **The wizard** describes the presets as they now are, offers the merged
    four-seat panel, and keeps a seat's `high_risk_model` and `relevance`
    through its reviewer questions while the CLI stays, so pressing enter
    through setup keeps the security seat's opus escalation; picking another
    CLI drops `high_risk_model` with a note, as `reviewer set --provider`
    does. A `relevance` rule stays only while the seat keeps its role
    (`always` stays whatever the role); another role drops it with a note.

- **The design review guidance for `general` and `architecture` is reworded**:
  `general` leads with whether following the plan builds the right thing, and
  `architecture` also checks compatibility -- existing callers, config and
  record formats, and the CLI surface.

- **Adapters declare what `smoke_live.py` checks** (`confines_read_only`,
  `repository_hooks_file`, `tool_activity_reported`, `denied_action_items`,
  `resumed_session_problem`, ...). A user adapter that sets none is checked
  as before; see `references/providers.md` (#211).

- **agy runs read `--output-format stream-json`** (#193). They report tool
  activity in `jobs wait`, `jobs show` and `review run --progress`, the
  context size (live and in the run's end event), and `usage.tool_uses`.
  agy still takes no idle deadline: it prints nothing while its model thinks.
  `run --print-command` shows `stream-json`.

- **An agy run whose result is missing, is not `SUCCESS` or has an empty
  response is now a failed run**, warned as `agy: no answer: ...`, and agy's
  raw output is never taken as the answer. Before, raw JSON could become the
  answer, and a non-`SUCCESS` status was only a warning. Partial text goes
  only to `run --output`'s `.rejected` file. Denied actions alone remain a
  warning. A bare `run` prints none of that text and a detached job records
  none of it as output.

- **The optimization report divides output chars by the runs that reported
  them**, not by every run that reported tools: agy counts its calls but not
  their output. `--json` gains `tool_output_reported_runs` and
  `design_tool_output_reported_runs` (and `tool_output_reported_runs` in the
  context groups and the pair sides). Where no run reported output, the
  figure shows `-`. In `by_context`, `sized_tool_runs` and
  `tool_run_change_chars` are renamed `sized_output_runs` and
  `output_run_change_chars`, as they now count the runs that reported output.

### Fixed

- **A quoted `;` no longer ends a PowerShell assignment in the activity
  line.** `$env:TOKEN='ab; cd'; npm test` showed `Bash: cd`, a word of the
  value; it now shows `Bash: npm test`, for every adapter's shell tool. A
  `$` statement holding a backtick, `(` or `{` shows `Bash` alone.

## [0.19.0] - 2026-10-04

### Changed

- **SKILL.md is a fifth smaller.** 14,653 characters down to 11,934, and
  the `validate_skill.py` ceiling from 14,750 to 11,950. Each instruction is
  now said once; detail the references already held is left to them, with a
  pointer. The margin above the document is for wording: a new rule is paid
  for by cutting elsewhere first.

- **claude 2.1.289 and codex-cli 0.160.0 resume the architect's session**:
  both are added to the verified table after passing every required check on
  2026-10-04 (claude's two symlink checks were skipped, as before; they are
  not required).

### Fixed

- **Under the Microsoft Store Python, the global config's real location is
  printed** (#235). That Python has Windows keep the files it writes under
  AppData in its package folder, so `%APPDATA%\dev-orchestra` looked empty to
  Explorer, editors and other Pythons. On Windows, a path that is really
  stored elsewhere is now followed by `(stored at <real path>)` in
  `config path`, `config show --scope global`, the messages of the commands
  that write the global file, `doctor` (the global file, the user adapter
  directory and each adapter, the resume record) and
  `scripts/smoke_live.py`. `doctor` also marks the Environment line
  `(Microsoft Store package)` and adds a note on how to move off it.
  `--json` gains, only when a path is redirected, `source_real` and
  `global_real` (`config show`) and `platform.store_python`,
  `config.global_real`, `user_providers.directory_real`, `path_real` on each
  `user_providers.loaded[]` and `errors[]` entry, and
  `providers.<name>.resume_support.record_real` (`doctor`). Which files are
  read and written does not change.
- **`config show --json` names the files of the effective view** as
  `project` and `global` (`null` when there is none), beside `source`. When
  the config cannot be read, `doctor` now still names the global file and the
  project override.
- **The orchestrator answers in the user's language** (#244). After reading
  English material (the skill, the plan, review findings, agents' reports,
  CLI output), its progress notes, questions, final report and tool-call
  descriptions drifted into English, even against a standing instruction.
  SKILL.md now makes this a rule: messages to the user are in the language
  the user asked for, else the one they write in. English plans and findings
  are restated in full, never dropping or softening one. Ids, severities,
  paths, commands, code and quoted text stay as written, as do relayed tool
  lines. Approval names `.ai/plan.md` as the text being approved. Prompts to
  delegated agents, CLI output and `.ai/` formats stay English.

## [0.18.0] - 2026-10-03

### Added

- **What a running job or review is doing** (#220). While a detached job
  runs, `jobs wait` and `jobs show` print `elapsed:` and, once its CLI has
  used a tool, how many tool uses, the context tokens the CLI last reported
  (Claude; Codex reports usage only when its turn ends), the latest tool lines
  and a `next:` cursor. New flags on both: `--since <n>` (0 to 10^9) and
  `--activity <m>` (0 to 100, default 10); `--json` gains an `activity` key
  only when the job has activity. The worker keeps the lines in
  `.ai/jobs/<id>.activity`, at most 500 before it is rotated to `.1`.
  `review run --progress` echoes each reviewer's tool lines to stderr as
  `[<reviewer> +mm:ss] <line>`, then `done: <status>`. A line names the tool
  and, through an allowlist, a relative path, a program (with its subcommand
  only for a known set such as `git` and `npm`; unwrapped from the shell Codex
  runs it in), a `Glob` pattern that stays inside the project, or a URL's
  scheme, host and port; never the model's text or a tool's free-text
  arguments, and every line is cleaned and redacted. Adapters gain
  `activity_of(line, cwd)`, which shows nothing by default, and
  `execution.execute` an optional `on_line`, called from a thread of its own
  so a slow one never holds up reading the CLI's output. Codex
  streams tool uses only on architect runs, the ones run with `--json`.
  Results, ledgers, budgets, run logs, `.out` files and `review run` without
  `--progress` are unchanged.
- **`summary --json` holds what the text shows** (#230). Beside `stages`,
  `counts` and `tokens` it now has `design_counts` (`reviewers_ok`,
  `reviewers_total`), `models` (each role's `provider`, `family` and
  `version`), `reviewers` (each entry's `id`, `provider`, `model` as the text
  prints it, and `status`) and `skipped` (`refused`, `refused_by`,
  `design_refused` and `panel_reduced`, named as in `optimization report
  --json`). Nothing else of a recorded reviewer entry is copied. The text and
  the three existing keys are unchanged.

### Changed

- **Adapter contract:** `resume_support` is one method on `Provider`; an
  adapter opts in with `verified_resume()` (its module's `VERIFIED_RESUME`),
  `resume_help_text()`, `resume_advertises()`, `resume_flags` and the wording
  attributes. A `ClaudeProvider` subclass with `supports_resume = False` now
  reports `unsupported` in `doctor`, as a Codex one did; its runs were already
  fresh.
- **Adapter contract:** work before or after the CLI runs goes in
  `around_launch(launch, proceed)`, which gets the run as a `Launch` and adds
  the adapter's own arguments with `own_args`; changing a field `run` gated
  raises `ValueError`. Hooks can read stdout's JSON lines, decoded once per
  run, from `stdout_events(outcome)` (#233). Overriding `_launch` still works.
- The read-only seat and write-role refusal policy moves from `config.py` to
  `config_policy.py`, and `warned_provider` to `providers`, with no change in
  behaviour; code that calls or replaces those functions has to find them
  there (#234).

### Fixed

- **`doctor`, `reviewer list`, `config show` and `summary` no longer crash on
  a `reviewers` entry that is not a mapping, or on `reviewers` that is not a
  list** (#222). They skip the broken entry and keep the rest's origins; an
  all-broken or non-list panel reads as none configured, `doctor` leaves the
  problem to `config validate`'s message instead of adding "no reviewers
  configured", and `reviewer list --json` shows a broken entry as
  `"[invalid entry]"` rather than its contents. `summary` falls back to the
  configured panel when the recorded one is not a list. `review
  consolidate` refuses such a panel (exit 2) instead of crashing, and leaves
  the last consolidation as it was.
- **Stopping a run's process tree on POSIX now waits for its whole process
  group**, not just its leader (was: once the worker or CLI exited on
  `SIGTERM`, a child that ignored it was never sent `SIGKILL`, so a timed-out
  run could leave e.g. a test server behind). **`jobs cancel` now also stops
  the CLI a worker is running**, and what that CLI started (was: the worker
  died on `SIGTERM` and its CLI, in a session of its own, ran on). When the
  worker's own process group cannot be confirmed gone, `jobs cancel` says the
  worker may still be running, and when the worker could not end its CLI's
  group, the job's error names that group (#214).

## [0.17.0] - 2026-10-02

### Changed

- **Claude's `permission_mode` and Codex's `sandbox` / `approve` on a write
  role are taken only from the global config or `--extra`**, as agy's
  `skip_permissions` already was (was: from either file). The project file can
  come with the branch under review, and `bypassPermissions` or
  `danger-full-access` there would turn its own implementer's safeguards off. A
  project file that names one of them (whatever the value) or sets any
  `options.args` on the implementer, the review fixer or one of their tiers now
  has that role's `implement` runs refused before anything is spent, and
  `config validate` and `doctor` say so. Move the setting to the global file
  (`config set --scope global implementer.options.permission_mode
  bypassPermissions`). See "Role options" in `references/configuration.md`
  (#195).
- **The search for the project file stops at a worktree's or a submodule's
  root**, where `.git` is a file (was: only at a `.git` directory, so it went
  on into the parent directories and could load a `.dev-orchestra.yaml` that
  belongs to another checkout). A checkout is configured by its own file only.

### Fixed

- **The warning for a read-only seat on agy no longer says it was your choice
  in the global config** when the seat came from the preset's fit on a machine
  with only agy installed. It now names both sources and says how to keep the
  seat off agy.
- **`jobs cancel` reports a worker stopped only once it has exited.** It now
  escalates as a timed-out run does (on POSIX, `SIGKILL` after `SIGTERM`) and
  says the worker may still be running when it is still there. On POSIX it
  signals the recorded pid only while that pid still leads its own process
  group, as a worker does, so a reused pid is left alone; and the last-resort
  kill of the bare pid is skipped once the tree is already gone.
- **An agy run names its prompt file without keeping it on the adapter**, so
  two runs sharing one adapter can no longer send each other's prompt.
- **Claude's resume verification asks a record for the checks the adapter
  requires**, as Codex's does, and **agy reads a negative token count as
  unreported**, as Claude and Codex do.
- **A `run --detach` worker records into the workflow its parent was given**
  (was: it resolved its own, from the environment or the session, so with
  `--workflow` its ledger, run log and token account went to another
  workflow, and its own approval check read that workflow's plan). It also
  no longer changes which workflow is current.
- **A user's own adapter can import `orchestrator.config`, `presets`,
  `optimization` or `review_common` at its top** (was: it failed to load with
  a circular-import error, because the adapters ran while `optimization` was
  still being imported).

## [0.16.0] - 2026-10-01

### Added

- **A user adapter can take part in preset fitting** by declaring
  `preset_family` on its class. It then takes the implementer and the review
  fixer when neither Claude, Codex nor agy is on PATH, and the orchestrator,
  the architect and the reviewer seats when neither Claude nor Codex is and its
  static read-only report is `verified` or `partial`. `doctor` prints a
  `Preset fitting:` line in the adapter's block (`preset_fit` in `--json`),
  and a static report that raises is now `status: error` there rather than
  an adapter failure. Adapters without the attribute are fitted to nothing, as
  before. See "Taking part in preset fitting" in `references/providers.md`
  (#186).

- **`reviewers_extra` adds reviewers beside the panel a file inherits**, so the
  panel keeps following the preset's fit or the global file's list. `reviewer
  add` now writes it in a file that lists no `reviewers`, where it used to copy
  the whole panel in; `reviewer remove` and `reviewer set` edit an extra in
  place. An extra whose id is taken runs under a new one, with a note. Every
  panel writer, `config set reviewers[...]` included, now refuses (exit 2)
  before writing a panel problem it would introduce. A project add on an
  agy-only machine keeps the fitted agy reviewer running, warned. `config
  show`, `reviewer list` and `doctor` mark each extra, and `doctor` notes a
  list that holds the inherited panel plus more. Files already written resolve
  as before; an older release ignores the key. See "Adding reviewers beside the
  panel" in `references/configuration.md` (#187).

- **`config suggest-roles` proposes path-scoped specialist reviewers from the
  project's files**: database, frontend and backend, from directory names,
  extensions and the root `package.json`, with no model call. `--write` adds
  them to the `reviewers_extra` of the project file at the repository root,
  each with `when: paths`, and refuses when the project file in force is
  elsewhere. Roles already on the panel, and patterns matching most files, are
  skipped and named. Inside a git repository only tracked files are read, and
  a failing or oversized `git ls-files` is an error. See "Suggesting
  path-scoped reviewers" in `references/configuration.md` (#183).

### Changed

- **On a machine with agy and neither Claude, Codex nor an eligible user
  adapter, the orchestrator, the architect and the reviewer panel are fitted
  to agy**, the implicit `standard` preset included, instead of expanding as
  written and failing. Those seats are warned wherever a global-file agy seat
  is, and each fit note says how to keep agy off: set the role, or list
  `reviewers`, in the global file. A project-scope reviewer write on such a
  machine no longer copies the fitted agy reviewer into the project file.
  Nothing changes with Claude or Codex installed. See "Presets" in
  `references/configuration.md` (#186).

- **`run architect --resume` continues on a Claude Code version newer than one
  that passed the resume checks and of the same major version**, unless a
  failure recorded on this machine stands in between; a version older than
  every pass, or a new major version, still runs fresh. Such a
  run prints a second note saying it resumed on trust, records `resume.trust:
  "newer"`, and `doctor` shows `Resume: trusted for ... as newer than ...`
  (`status: trusted` and `newer_than` in `--json`). Codex gets a fork
  (`codex exec fork` under `--ignore-user-config`, checked from its rollout),
  verified on codex-cli 0.156.1, and a Codex `plan` run now adds
  `--json` and reads its usage from the events. See "Resuming a session" in
  `references/providers.md` (#181).
- **The runtime budget no longer charges sleep on Windows.** A delegated run
  that spanned a sleep is charged its duration less the time the machine was
  asleep; Linux and macOS already left sleep out. The sleep left out is
  recorded as `suspended_seconds` on the run event, each reviewer entry and a
  detached run's job when it is non-zero, totalled in the ledger as
  `runtime_suspended_seconds`, and shown by `budget show` and `status` (and as
  `runtime.suspended` in their `--json`). `duration_seconds` and the run
  deadlines are unchanged. See "What the runtime budget counts" in
  `references/limits.md` (#191).

- **Presets fit more review roles.** `standard`, the implicit preset, adds a
  security and a test reviewer on Claude sonnet, plus a security reviewer on the
  full model that runs only on high-risk changes. Codex and user adapters get
  the high-risk one but no sonnet seats, because no cheap model of theirs can be
  named offline. On agy alone the panel is unchanged, since agy cannot be held
  to reading. `quality` adds a test reviewer (not on agy), and `fast` is
  unchanged. Small-change rounds still run one reviewer. A file that lists
  `reviewers` keeps exactly those seats and gets none of the new ones; `config
  show` prints the panel in force. An extra whose id a new seat now holds
  (`claude-security`, `claude-test`) runs under the id its note gives.
  `reviewer remove`/`set` with the old id are refused and name the new one.
  With `optimization.high_risk_paths: []` and no `extra_high_risk_paths`, the
  fitted high-risk seat stays and runs only on rounds declared with `review run
  --high-risk`; a `when: high-risk` reviewer a file writes is still refused.
  See "Presets" in `references/configuration.md` (#183).

## [0.15.0] - 2026-09-30

### Added

- **`review.design.enabled: auto`** runs the design review only for a plan
  that calls for it: one that names a high-risk path anywhere (matched in any
  case), or whose `Files to Modify` names 6 or more code files (docs, tests
  and `.md` files not counted), a glob, a directory or a path through `..`.
  Entries there count whether or not they are in backticks. A plan that
  cannot be read runs too, and before a plan is written the answer is
  `auto -> run (once a plan is written)`. Once a design round has run for the workflow the answer stays
  run, so a revision cannot switch the loop off half way. `status`,
  `review status --design` and the `review run --design` note show the answer
  and its reason (`auto -> skip (3 code files, none high-risk)`); their
  `--json` gains `mode` and `reason`, and `enabled` now says whether the stage
  runs (#80).

- **Setup presets fitted to the installed CLIs.** `config setup --preset
  quality|standard|fast` writes `preset: <name>` to the global file, and the
  wizard asks for a preset first. A preset sets the roles, the reviewer panel,
  the design review and the optimization level, worked out at every load
  against the CLIs on PATH, so a Claude-only machine gets a Claude-only panel.
  Only what no file sets is fitted, and only the global file can name a preset
  for now. `config show` and `doctor` name the preset and what was refitted;
  the configuration summary shows the optimization level and each reviewer's
  condition; a workflow started with no config file prints the configuration
  once. An older dev-orchestra ignores `preset:` and its `config validate`
  does not report it. See Presets in `references/configuration.md` (#163).

- **An `agy` (Antigravity CLI) provider**, verified against agy 1.2.13, for
  the implementer and the review fixer. agy has no read-only mode, so a new
  enforcement status, `unenforced`, lets a plan or review seat on it run with
  a warning at every place it is set or run when it comes from the global
  config, and refuses it when it comes from the project file. On agy write
  roles the permission bypass (`options.skip_permissions`) and any
  `options.args` are taken only from the global config or `--extra`. Presets
  give agy the write roles only when neither Claude nor Codex is on PATH. Run
  warnings (such as actions agy denied) are shown and recorded on success
  too. The prompt reaches agy in a file inside `.ai/`, never on the command
  line, and `model list --provider agy` lists the families to put in a
  config with the id each resolves to now. See "Antigravity CLI adapter" in
  `references/providers.md` (#182).

### Changed

- **`reviewer set --provider` without `--model` resets the family** to the new
  CLI's default (was: the old family was kept, which the new CLI could not
  resolve), with a `note:` naming the old one. `available_providers()` now
  lists `agy` first, so `model list` and `doctor` show it first (#182).

- **No config file, or a global file that names no preset, runs under preset
  `standard` fitted to the installed CLIs** (was: the built-in defaults as
  written). With Claude Code and Codex both installed, or neither, nothing
  changes. With one of them, the roles and the reviewer panel that no file
  sets move to that CLI: Claude alone gets `claude-general` and
  `claude-general-2` (sonnet) instead of a Codex reviewer that failed every
  round. Roles and panels a file sets are untouched (#163).

- **`config reset` on the global file keeps its `preset`** (was: only
  `version` was kept), and both scopes print the resulting configuration.
  `reviewer add` / `remove` / `set` and `config set reviewers[...]` print a
  note when they copy this machine's fitted panel into the file, since from
  then on the panel no longer follows the preset (#163).

- **The last design revision is asked to simplify, not patch.** The revision
  that follows the last design round, which no review sees, is asked to remove
  or simplify what an earlier revision added where a finding is a hole in it,
  and to fail toward the safe side. The round limits stay at 2. See the design
  revision templates in `references/workflow.md` (#133).

- **A fixer's new test must fail without the fix.** The fix template asks for
  a test that fails on the code as it was, or a reason there cannot be one,
  and the re-test step has the orchestrator reverse only the fixer's diff
  (against a tree recorded before the fixer ran, untracked files included) and see the test fail
  on its assertion, whatever the finding's severity. See "Re-test and re-review" in
  `references/workflow.md` (#87).

- **`review.design.enabled` defaults to `auto`** (was `false`), and an explicit
  `enabled:` with no value now means `auto` too. `true` and `false` keep their
  meaning; `config set review.design.enabled false` restores the old
  behaviour. An older dev-orchestra reading `auto` from a shared project file
  treats it as `true` and its `config validate` reports it (#80).

- **claude 2.1.285 resumes the architect's session**: it is added to the
  verified table after passing every required check on 2026-09-30 (the two
  symlink checks were skipped, as before; they are not required).

## [0.14.0] - 2026-09-29

### Added

- **Google Antigravity as a third plugin host.** A root `plugin.json` (only
  `$schema`, `name` and `description`, as Antigravity's schema allows) makes
  the repository an Antigravity plugin. The installers gain `--antigravity`
  (`--gemini`; `-Antigravity`/`-Gemini` in PowerShell), which links the
  checkout into `~/.gemini/config/plugins/` or, with `--project`,
  `<path>/.agents/plugins/`; restart Antigravity afterwards. It refuses to
  link a checkout that holds other files Antigravity would load (`hooks.json`,
  `rules/`, `agents/*.md`, ...), and `doctor` reports a live install that has
  them later; `doctor --json` gains an `antigravity` block. The other routes,
  `agy plugin install` and `plugins.json`, get neither guard. A Claude Code
  copy install now includes `plugin.json`. Details are in
  `references/workflow.md` under "Installing from a skill checkout" (#160).

- **The first command of a new workflow notes the workflows that have gone
  quiet**, once, on stderr: those whose `state.json` was last updated
  `workspace.stale_notice_days` days ago or more (a new key, default 30; `0`
  turns it off; at most 36500). The current workflow and any with a stage in
  flight are left out, and nothing is deleted or moved; `workflow remove <id>
  --yes` is still the only thing that deletes one. `workflow list`, `status`
  and `optimization report` now read a workflow with an unreadable
  `state.json` as one with nothing recorded instead of failing. See
  `workspace.stale_notice_days` in `references/configuration.md` (#112).

- **Type checking with Pyright in CI**, over `scripts/` and `tests/`. The
  development tools are pinned in `requirements-dev.txt`; CONTRIBUTING says
  how to set them up (#125).

- **`doctor` says whether the installed CLI version has been live-checked**,
  on a `Live check:` line (`providers.<name>.live_check` in `--json`), from
  what `scripts/smoke_live.py` records in `verified/<provider>-smoke.json` in
  the config directory. An unchecked version gets a note; notes never change
  the exit code. See `doctor` in `references/cli.md` (#122).

### Changed

- **claude 2.1.284 resumes the architect's session**: it is added to the
  verified table after passing every required check on 2026-09-29 (the two
  symlink checks were skipped, as before; they are not required).

- **The `.ai/` artifact compatibility rule is written down once**, under "How
  the formats change" in `references/workflow.md`. The artifacts get no
  format version (#126).

- **The long references open with a table of contents**, written by
  `scripts/doc_contents.py` and checked by the tests (#164).

- **Python 3.11 or later is required** (was 3.9). An older interpreter stops
  with a message naming the version it found (exit 2), and the `bin/`
  wrappers and the installers pass over one. A machine whose only Python is
  older needs a newer one installed (#167).

- **`cli.py` and `review.py` are split into modules**, with no change in
  behaviour. Both still re-export every name, but code that replaces a
  function (a user adapter's tests, say) has to replace it in the module that
  looks it up; CONTRIBUTING's "Where things go" lists the modules (#119).

- **The README is now the way in**, about 250 lines, and the detail it used
  to hold is in `references/`; its "Reference documents" section says which
  file covers what. Running the plugin from a clone moved to CONTRIBUTING
  (#164, #117).

### Fixed

- **The Claude Code install on Windows no longer empties what a link at its
  destination points at**: `install.ps1` and `uninstall.ps1` remove a link as
  a link, dangling ones included. A relative `-Project` works from another
  directory, and the project's `.git/info/exclude` line is written only after
  the install succeeded, on its own line and without a byte order mark
  (#160).

## [0.13.2] - 2026-09-29

### Added

- **Reviewers scoped to paths.** A reviewer's `when` can now be a mapping whose
  one key, `paths`, lists its own glob patterns, so a specialist such as a
  `database` reviewer joins a code review round only when the change touches
  the files it knows about. Write it in block form, as
  `references/configuration.md` shows. A path-scoped reviewer joins when a changed path matches one of its own
  patterns, when it must re-check its own open accepted finding, or when
  `--only` names it, and nothing else adds it: a high-risk path hit and
  `review run --high-risk` still add only the `when: high-risk` reviewers, and
  its left-out note says so and names `--only` as the way in. A match never
  escalates the level or lifts the gate. It is matched against the change a
  reviewer is shown, withheld files and rename sources included; the snapshot
  meta gains `condition_paths` for that, and files suppressed from an
  incremental round (the orchestrator's own, excluded untracked ones) never
  add it. It counts as conditional for the "one reviewer must run always"
  rule and needs no `high_risk_paths` pattern in force. `reviewer add` and
  `reviewer set` take `--when-paths GLOB [GLOB ...]`, which writes the mapping
  in block form and replaces any existing condition whole. `reviewer list`,
  `doctor` (`condition` and `paths` in `--json`), `status` and the round event
  show the new kind as `when: paths`. Security and other roles that judge risk
  should stay `when: high-risk`. A config with a `when` mapping is refused by
  0.13.x and earlier at `review run` and `config validate`.
- **Left-out rounds in the reviewer scorecard.** `optimization report` now ends
  a conditional reviewer's cost line with its condition and how many of the
  scorecard's rounds it sat out, as `(when: high-risk; left out of 2
  round(s))`, so its runs and the rounds it was left out of read side by side.
  A round with several events is sat out once, and not at all when a `--only`
  re-run ran the reviewer; one left out of every round still gets a row. In
  `--json`, such a reviewer gains `when` and `left_out_rounds`; other
  reviewers are unchanged.

### Changed

- The reasons recorded for conditional reviewers, including the existing
  high-risk ones, are now redacted: a credential-shaped part of a path or
  pattern is replaced with `[redacted]` in notes, `status`, `--json` and the
  event log. So are the escalation note, the `high_risk` path/pattern pairs in
  the round's `optimization` record, and the `when.paths` patterns in
  `reviewer list --json`. Two validation messages are reworded to cover the new form:
  `reviewers[i].when: must be one of always, high-risk, or a mapping with
  paths`, and `... every reviewer is conditional (when: high-risk or when:
  paths)`.
- **Design revisions stay small and say what they add.** The four revision
  request templates in `references/workflow.md` now ask the architect to answer
  each finding or change with the smallest change that does it, and to end the
  plan with `## Added in this revision`, listing each new record, flag, rule,
  state or code path, the finding it answers and why nothing smaller would do
  (`None.` when there is none). A design re-review then points every reviewer
  at that list first -- its failure paths, older or malformed input, and how
  the items interact with existing behaviour and each other -- and asks it to
  check that nothing else changed unlisted. When the list is `None.` or
  missing, the reviewer is asked whether the revision added a mechanism without
  saying so. In the recorded rounds, most new findings on a re-review came from
  what the revision itself added. First-round prompts are unchanged.

### Fixed

- **A detached run's usage is recorded before its job says it finished.**
  The worker wrote `succeeded` to the job record first and the run's usage
  and ledger entry a moment later, so a `tokens show` read straight after
  `jobs wait` could miss the run. The books are now closed first, and the job
  still records its outcome if closing them fails (#158).

## [0.13.1] - 2026-09-29

### Added

- **`doctor` notes a `when: high-risk` reviewer judged by the built-in
  patterns alone.** With no `high_risk_paths` of the repository's own and no
  `extra_high_risk_paths`, one note names every such reviewer and points at
  `optimization.extra_high_risk_paths`: the defaults fit common names and can
  miss a repository's own sensitive paths, leaving the reviewer almost never
  running. A `high_risk_paths` drawn only from the defaults, such as the older
  default list a config written before 0.6.0 holds, counts as none of the
  repository's own. A new **Notes** block (`notes` in `--json`) carries it; notes are
  not problems and never fail `--strict`. Reviewer lines in **Roles** now end
  in `(when: high-risk)` for a conditional reviewer, as in `reviewer list`, and
  the `--json` entry carries `when` when it is not `always`.

### Fixed

- **`doctor` no longer lists `optimization.extra_high_risk_paths` as "pinned at
  a value the built-in default has moved off".** That section finds values an
  older release copied into a file and later improved; no release ever wrote
  this key, so a value there is always one somebody added (#152).

## [0.13.0] - 2026-09-29

### Added

- **`reviewers[].when: high-risk` runs a reviewer on high-risk code rounds
  only.** A conditional reviewer joins a code review round when the change
  matches a high-risk path, when the round is declared with
  `review run --high-risk`, or when it must re-check its own open accepted
  finding on an incremental round or a re-run of the same snapshot; otherwise
  it is left out. `--only` naming it
  runs it. The design review ignores the condition and runs every reviewer.
  Every decision is printed as a `note:` with its reason and recorded under
  `optimization.conditional` in the round's event (refused rounds included),
  in `review run --json` and in `status --json`. Whenever a conditional
  reviewer joins, the low-risk panel reduction does not apply. A left-out
  reviewer's report of the same snapshot is not consolidated, by `review run`
  or by `review consolidate`. A config without `when` behaves as before.
- **`review run --high-risk`** declares a code round high-risk: it adds the
  conditional reviewers and keeps the panel whole, and never changes the
  level, the findings cap or the gate. Recorded as `optimization.declared`.
  Refused (exit 2) with `--design`.
- **`optimization.extra_high_risk_paths`** (default `[]`) adds patterns to
  `high_risk_paths` instead of replacing it. A hit escalates to `quality` and
  lets a red tree through the gate exactly as a `high_risk_paths` hit does.
- **Validation:** an unknown `when`, a panel where every reviewer is
  `high-risk`, and a `high-risk` reviewer with no pattern in force are
  configuration problems. `reviewer add` and `reviewer set` take
  `--when always|high-risk`; `reviewer set` and `reviewer remove` refuse to
  leave no reviewer that always runs. `reviewer list` shows the condition.
- **`status`** names each conditional reviewer the next round would add or
  leave out on its `Optimization:` line, and **`optimization report`** has a
  `conditional reviewers` row (`conditional` in `--json`: `added`, `left_out`,
  `declared_rounds`). The "every round escalated" advice names both pattern
  lists.

Rolling it out: add `extra_high_risk_paths` for the paths the defaults miss
first (for this repository, `*/providers/*` and `*/config.py`), then switch a
reviewer to `when: high-risk`. A project value of `extra_high_risk_paths`
replaces a global one, like every list, and every hit on an extra pattern
escalates the round to `quality` and lets a red tree through the gate.
Path-scoped conditions, per-reviewer scorecard rows for conditional reviewers
and a `doctor` note for a `high-risk` reviewer judged by the default patterns
are left for later.

## [0.12.0] - 2026-09-27

### Added

- **`run architect --resume` revises the plan in the architect's own
  session.** With `--resume-prompt-file <short>` beside the usual full prompt,
  a revision continues the last architect session of the workflow (forked:
  the claude command is the read-only one plus `--resume=<id>
  --fork-session`) instead of reading the code and rebuilding the context
  again. Refused (exit 2) unless the role is the architect, the mode is
  `plan` and `--output` is this workflow's plan. When the session cannot be
  continued -- no earlier run, it failed, it is older than
  `design.resume.max_age_seconds`, the provider cannot resume, the CLI
  version has not been verified, and so on -- the run goes fresh with the full
  prompt, as before, and records why as a fixed phrase. A session the CLI
  says no longer exists is run again fresh once, which costs an attempt; any
  other failure is reported and not retried. Codex does not resume.
- **Resuming is enabled per claude version.** A version is resumed only when
  it is in `VERIFIED_RESUME` in the claude adapter or recorded as passed on
  this machine in `<config dir>/verified/claude-resume.json`, which
  `scripts/smoke_live.py` writes after checking that a resumed session keeps
  the read-only tools, no MCP servers, plan mode, confinement and no
  repository hooks, and that a missing session is recognised. A record inside
  the workspace is ignored. `doctor` has a `Resume:` line
  (`providers.<name>.resume_support` in `--json`). The table ships with
  claude 2.1.283, which passed every required check on 2026-09-27 (the two
  symlink checks were skipped on a Windows machine without the privilege to
  create a symlink; they are not required).
- **`design.resume.max_age_seconds`** (default 3600) and
  **`design.resume.max_context_tokens`** (default `null`, no cap).
- **Every `run` end event records `session_id`, `context_tokens`, `cost_usd`
  and `cache_read_tokens`**; a `--resume` run also records `resume`
  (`requested`, `mode`, `resumed_from`, `reason`, `outcome`). A continued
  run's usage is labelled `architect:resumed` in `tokens show`, and job
  records carry `force`, `resume_prompt_file`, `session_id` and `resume`.
- **`optimization report` has an "Architect revisions" block**
  (`architect_revisions` in `--json`): resumed and fresh revisions, each
  measured as its cost against the workflow's initial design run, with the
  failed attempts and why `--resume` ran fresh. It is printed without any
  review round too.
- **Adapter contract:** `supports_resume`, `resume_support(root)`,
  `resume_args`, `resume_rejected`, `parse_session` and `command_line`, a
  `resume_session` keyword on `run` and `_launch` (passed to `_launch` only
  when there is one), and `session_id`, `context_tokens`, `session_init` and
  `resume_rejected` on `RunResult`. `build_command` is unchanged.

### Changed

- **The design review and approval steps of `references/workflow.md`** write
  two revision prompts and pass `--resume`.

### Fixed

- **A healthy Claude run writing a long answer was killed as a stall (#138).**
  The `thinking_tokens` events stop once the answer starts, and the answer
  arrives only when it is finished: a 17k-character answer was measured to
  leave 141s with no output, and a long plan crossed the 300s idle deadline.
  On `stream-json` the adapter now adds `--include-partial-messages` when
  `claude --help` lists it (largest gap 1.7s on the same prompt; stdout about
  8× larger). The answer, usage, tool counts, session and the missing-session
  check read the same as before. A run killed while writing its final message
  keeps the text it had streamed in the fallback answer.

## [0.11.0] - 2026-09-27

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
- **Read-only runs are read-only by construction.** This changes what every
  architect, orchestrator and reviewer run can do, and refuses configurations
  that used to be accepted -- under 1.x it would be a major version.
  - Claude `plan` and `review` runs add `--tools Read,Grep,Glob
    --strict-mcp-config --restricted` to plan mode and the denied edit tools.
    They have `Read`, `Grep` and `Glob` and nothing else: no `Bash` or
    `PowerShell`, no subagents, no MCP tools, no `ToolSearch`. The settings
    files of the user, the project and `.claude/settings.local.json` are not
    read on these runs, so a branch under review cannot run its own hooks in
    its reviewers. That includes **`permissions.deny`**: a rule such as
    `Read(./.env)` no longer keeps an architect or reviewer away from a file in
    the repository; move it to managed settings, which still apply. `env`,
    `apiKeyHelper`, `permissions.allow` and `permissions.additionalDirectories`
    in those files stop applying to these runs too. `Read`, `Grep` and `Glob`
    are confined to the working directory and `--add-dir` (measured for
    absolute paths on claude 2.1.283; symlinks were not tested). The implementer
    and the review fixer are unchanged.
  - What is lost: git history, running a command to check a claim, reading
    outside the project, and subagents. The design request template has a line
    for the history that matters, and SKILL.md says to fill it.
  - A read-only run takes raw arguments from an allowlist, checked in
    `Provider.run` before an adapter adds its own: Claude accepts only
    `--add-dir <path>` (or `--add-dir=<path>`), Codex none. Anything else in
    `options.args` or `--extra` makes `run` exit 2 before a budget attempt is
    spent, and fails that reviewer in `review run`. `--add-dir` is taken only
    from the global config and `--extra`: any `options.args` a read-only role
    gets from the project file is refused, whatever it holds, because that file
    can come with the branch under review. A refusal names the flag, its
    position and its source, never its value, and a detached worker writes it
    to its job record.
  - `run <read-only role> --mode implement` exits 2.
  - A Claude CLI whose `--help` does not list all three flags has its `plan`
    and `review` runs refused (exit 2) rather than run with less; one whose
    `--help` cannot be read is refused the same way. A CLI that is not
    installed is still reported as missing (127).
  - `doctor` prints a `Read-only runs:` line per provider (`verified`,
    `partial`, `NOT ENFORCEABLE`, `UNVERIFIED`, `not reported by this adapter`,
    `not checked (--fast)`; `providers.<name>.read_only_enforcement` in
    `--json`). Codex is `partial`: its sandbox stops writes, and its MCP servers
    were not examined. A read-only role, or one of its tiers, on a provider
    that cannot enforce is a problem, and so is every raw argument a read-only run would refuse, which
    `config validate` lists as warnings (`warnings` in `--json`) without
    changing its exit status and `config set` prints as `warning:` lines.
    `config.load()` does not raise over any of this, so other commands carry
    on. The orchestrator now counts as a read-only role for ignored options.
  - `claude --help` is read once per process, shared by model, permission-mode
    and read-only discovery.
  - Adapters override `_launch`, not `run`: `run` is now the gate every
    adapter shares, and one that overrides it runs without it. The built-in
    Codex and mock adapters moved their overrides to `_launch`.

### Fixed

- **A read-only Claude run could write, reach external services, and run the
  repository's hooks (#140).** Refused `Write`, a plan-mode run wrote the file
  with `Bash`; MCP tools such as sending a Slack message were reachable through
  `ToolSearch`; command hooks in the reviewed branch's `.claude/settings.json`
  ran; and the architect and reviewers could read anything the user can. The
  tool allowlist, `--strict-mcp-config` and `--restricted` above close each.
- **Configured or `--extra` arguments could undo read-only mode (#139).** They
  were appended after the adapter's own flags, so `--tools default`,
  `--permission-mode acceptEdits`, a `--settings` file or a Codex `-s
  workspace-write` reopened what the adapter had closed. They are now held to
  the allowlist above.
- `scripts/smoke_live.py` no longer passes a read-only check for a run that
  never started or did not complete, asks for the shell fallback outright, and
  checks that a read-only Claude run stays in its working directory (by
  absolute path and by symlink) and that `--add-dir` widens it. A symlink it
  cannot create is reported as `SKIP` with the reason, and makes the run exit
  1 instead of reading as all passed.

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

[Unreleased]: https://github.com/istb16/dev-orchestra/compare/v0.22.0...HEAD
[0.22.0]: https://github.com/istb16/dev-orchestra/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/istb16/dev-orchestra/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/istb16/dev-orchestra/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/istb16/dev-orchestra/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/istb16/dev-orchestra/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/istb16/dev-orchestra/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/istb16/dev-orchestra/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/istb16/dev-orchestra/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/istb16/dev-orchestra/compare/v0.13.2...v0.14.0
[0.13.2]: https://github.com/istb16/dev-orchestra/compare/v0.13.1...v0.13.2
[0.13.1]: https://github.com/istb16/dev-orchestra/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/istb16/dev-orchestra/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/istb16/dev-orchestra/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/istb16/dev-orchestra/compare/v0.10.0...v0.11.0
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
