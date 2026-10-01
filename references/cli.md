# CLI reference

<!-- contents: start -->

**Contents**

- [config](#config)
- [model](#model)
- [reviewer](#reviewer)
- [doctor](#doctor)
- [run](#run)
  - [Revising the plan in the architect's own session (`--resume`)](#revising-the-plan-in-the-architects-own-session---resume)
- [review](#review)
- [design](#design)
- [status](#status)
- [jobs](#jobs)
- [budget](#budget)
- [tokens](#tokens)
- [optimization](#optimization)
  - [Architect revisions](#architect-revisions)
- [progress](#progress)
- [workflow](#workflow)
- [state / summary](#state--summary)
- [Environment variables](#environment-variables)
- [Troubleshooting](#troubleshooting)

<!-- contents: end -->

```
python scripts/dev_orchestra.py <command> [options]
bin/dev-orchestra <command> [options]           # POSIX wrapper
bin\dev-orchestra.ps1 <command> [options]       # Windows wrapper
```

Read `python` as `python3` where that is the only name the interpreter has.
The wrappers find it themselves: `python3` then `python` on POSIX, and
`python`, `py`, `python3` on Windows, where a `python3` on PATH is usually the
Microsoft Store alias rather than an interpreter.

Global options: `--cwd <dir>` (operate as if run from there),
`--workflow <id>` (the workflow these artifacts belong to; see below),
`--version`.

Exit codes: `0` success, `1` the operation ran but the outcome is negative
(invalid config, empty snapshot, every reviewer failed, role run failed), `2` a
usage or configuration error, `3` a budget is exhausted and the command refused
to run, `4` a `jobs wait` returned while the job was still running, `5` the
plan is not approved and `design.require_approval` is on, `130` interrupted.

## config

| Command | Description |
| --- | --- |
| `config show [--scope effective\|global\|project] [--json]` | Show the configuration. Default `effective` (merged); a scope shows that layer exactly as it is on disk, which is usually much shorter. The effective view names the preset in force after `Source:` (`Preset: quality (global; fitted to claude, codex)`) with a `note:` line for each thing that was refitted; `--json` has them under `preset` (`name`, `source`, `notes`). A `Providers:` line says where each provider it refers to comes from (built-in, a user module's path, or no adapter); `--json` has the same under `providers`. |
| `config path` | Print both layer locations. |
| `config setup [--scope global\|project] [--preset quality\|standard\|fast \| --defaults] [--force]` | Setup wizard; for the global file its first question is the preset. `--preset` asks nothing: it writes `version` and `preset`, keeps what the file held apart from the keys a preset governs (a role keeps its `options` and `model_tiers` and is then not fitted), and prints the configuration that preset resolves to on this machine with its notes (see `references/configuration.md`, Presets). Only the global file can name a preset: with `--scope project` it is refused (exit 2) and nothing is written. `--defaults` overrides nothing, so the file holds only `version: 1` and runs under preset `standard`. `--force` prompts even without a TTY. |
| `config reset [--scope …] [--delete]` | Clear this layer's overrides (the file stays, holding only `version`, and the global file its `preset` too when it names a known one; an unknown name is cleared with a `note:`), then print the configuration that is left. `--delete` removes the file; the global layer then runs under `standard`. |
| `config prune [--scope …] [--dry-run]` | Drop values a layer holds that are equal to what it inherits -- for files written before 0.6.0, which hold every default. A value goes only when the built-in defaults and the preset's fit agree on it, so pruning never changes the configuration in force. `--dry-run` lists them without writing. |
| `config set <path> <value> [--scope …] [--raw]` | Set one value. Paths support `a.b.c` and `reviewers[0].role`; an indexed edit copies the rest of the list from the layer below, and an index past the end exits 2. `preset` is written to the global file even when a project file exists; `--scope project` with it exits 2 and writes nothing. Setting a read-only seat's provider (`orchestrator.provider`, `architect.provider`, `<role>.model_tiers.<tier>.provider`, `reviewers[<n>].provider`) to `agy` in the project file exits 2 and writes nothing, naming the `--scope global` command instead; in the global file it is written and warned about. |
| `config suggest-roles [--write] [--json] [--provider P] [--model F]` | Propose path-scoped `database`, `frontend` and `backend` reviewers from the project's file names and the root `package.json`, with no model call. Prints each proposal's id, provider, family, `when.paths`, evidence, and how many listed files it matches (and how many of those `review.exclude` withholds), then every role not proposed with the reason. Inside a git repository only `git ls-files` is read; a failing or oversized listing exits 2. `--write` appends them to the `reviewers_extra` of the project file at the listing root and refuses (exit 2, nothing written) when the project file in force is elsewhere. `--json` prints one object (`root`, `source`, `files`, `truncated`, `notes`, `suggestions`, `skipped`, `written`), with notes on stderr. See `references/configuration.md`, "Suggesting path-scoped reviewers". |
| `config validate [--json]` | Validate the effective configuration. Exit 1 if invalid. A `Warnings:` section (`warnings` in `--json`) lists the raw arguments a read-only role's runs would refuse -- any `options.args` in the project file, and anything the adapter's allowlist does not take -- the read-only seats on `agy` that come from the project file, the `options.skip_permissions` or `options.args` an agy write role would take from the project file, and one `<seat>: read-only is NOT enforced by agy -- ...` line per read-only seat on agy from the global file, without changing the exit status. `config set` prints the same as `warning:` lines. |

```bash
dev-orchestra config set implementer.model.family opus
dev-orchestra config set review.max_review_iterations 3
dev-orchestra config set --scope project workspace.dir .agent-work
dev-orchestra config set --raw review.note "3 reviewers"
```

A saved file holds only what was set on it; everything else is resolved from
the layer below, so an improved default reaches it. `config reset --delete` can
uncover a second project file the deleted one was shadowing (`.dev-orchestra.yml`
beside `.dev-orchestra.yaml`, or one in a parent directory); `config reset`
leaves the file in place and goes on shadowing it.

## model

| Command | Description |
| --- | --- |
| `model list [--provider <name>] [--json]` | Models the installed CLIs advertise, with the discovery source for each (`cli-help`, `cli-catalog`, `cli-config`, `cli-default`, `builtin-fallback`). An adapter that raises is reported and the rest still listed, but the exit status is 1. For a CLI whose listed models are dated ids (agy), a `families to put in a config` section follows, one `family=<name> now <id>` line per family the adapter resolves on this machine; those are the names to store, as an id does not follow a newer model. Every `--json` entry has the same keys, including `origin`, `adapter_error` (`null` when it worked) and `families` (`[]` when there are none; each `{"family", "resolves_to"}`). |

## reviewer

| Command | Description |
| --- | --- |
| `reviewer list [--json]` | List the configured panel. |
| `reviewer add --provider <p> [--model <family>] [--role <r>] [--id <id>] [--pin <model-id>] [--when always\|high-risk \| --when-paths GLOB [GLOB ...]] [--scope …]` | Add a reviewer. The id is generated (`codex-security`, `codex-security-2`, …) when omitted. `--when high-risk` makes it join the code review only on rounds judged high-risk; `--when-paths "*migrate*/*" "*.sql"` makes it join only when a changed path matches one of those patterns, written as a `when:` mapping with `paths` in block form (see `references/configuration.md`). Quote each pattern so the shell does not expand it. `always`, the default, writes no key. Giving `--when` and `--when-paths` together exits 2. |
| `reviewer remove <id\|role\|position> [--scope …]` | Remove by id, by unique role, or by 1-based position. Refused (exit 2) when it would leave only conditional (`when: high-risk` or path-scoped) reviewers. |
| `reviewer set <selector> [--provider] [--model] [--role] [--id] [--pin] [--when always\|high-risk \| --when-paths GLOB [GLOB ...]] [--scope …]` | Change an existing reviewer. `--when always` removes the condition; `--when high-risk` and `--when-paths` each replace it whole, so `--when-paths` replaces the list rather than adding to it. Making the last reviewer that always runs conditional is refused (exit 2), and so is giving `--when` and `--when-paths` together. |

Without `--model`, `reviewer add` writes the CLI's default family (`opus` on
Claude, `default` on agy, `recommended-coding` otherwise), and `reviewer set
--provider <other>` without `--model` or `--pin` resets the family to the new
CLI's default, with a `note:` naming the old one. With `--provider agy`, both
exit 2 without writing in the project file, and in the global file print a
`warning:` line after writing: a reviewer on agy is not held to reading. With
any provider, `--scope project` copies the global panel into the project file
when it has none, agy reviewers included; after writing, each of those gets a
`warning: reviewer <id>: the reviewers list comes from the project config ...`
line, as `config set` prints it, since it is refused from then on.

`add`, `remove` and `set` edit the whole list in the file. When neither the
file they write nor one below it (the global file, under a project one) lists
`reviewers` yet, they start from the panel the preset gives this machine, copy
it into the file, and print one `note:` naming the reviewers they recorded:
from then on the panel is the file's and no longer follows the preset. `config
set reviewers[...]` does the same.

```bash
dev-orchestra reviewer add --provider codex --role security
dev-orchestra reviewer add --provider claude --role database --id db-review
dev-orchestra reviewer set 2 --role performance
dev-orchestra reviewer remove db-review
```

## doctor

| Command | Description |
| --- | --- |
| `doctor [--json] [--fast] [--strict]` | Diagnose CLIs, authentication presence, configuration, and whether each role's model resolves. `--fast` skips model discovery and the read-only enforcement probe. `--strict` exits 1 when problems are found. |

Never prints credential values -- only whether credentials appear to be present.

Each installed provider gets a `Read-only runs:` line saying how its `plan` and
`review` runs are held to reading: `enforced by <flags>` (verified), `enforced
by <flags>; <what is not covered>` (partial -- Codex, whose MCP servers were not
examined), `NOT ENFORCED (runs allowed, warned) -- <why>` (agy, which has no
read-only mode), `NOT ENFORCEABLE` (the CLI does not advertise the flags),
`UNVERIFIED` (its `--help` could not be read), `not reported by this adapter`,
or `not checked (--fast)`. agy's status is a constant, so its line is shown in
`--fast` mode and when the CLI is not installed as well. In `--json` it is
`providers.<name>.read_only_enforcement`, with `status` one of `verified`,
`partial`, `unenforced`, `unsupported`, `unverified`, `unspecified` and
`not-checked`. `NOT ENFORCEABLE` and `UNVERIFIED` are problems when a
read-only role (orchestrator, architect, a reviewer) uses that provider, since
its runs will be refused; each of a role's tiers is checked against the
provider it would run on, and reported as `<Role> (tier <name>)`. A read-only
seat on agy from the global file is a note instead -- `Reviewer agy-general:
read-only runs are NOT enforced by agy (allowed, warned) -- <why>` -- in both
modes and whether or not agy is installed, and its `--json` entry carries
`read_only: "unenforced"`; `--strict` still passes. The same seat from the
project file is a problem (the refusal `run` would give), so `--strict` fails.
The raw-argument warnings `config validate` prints are problems here as well,
under `config.warnings` in `--json`. `--fast` does not promise that no
`--help` is read: validating `options.permission_mode` reads one.

The "Pinned at a value the built-in default has moved off" section lists the
settings a file fixes where the recommendation has since changed. It is a
report, never a rewrite: a deliberate choice and an inherited default look
identical on disk. `config prune` drops the ones equal to the current default,
on request. It says nothing about `reviewers` -- a panel is nobody's default
-- nor about `optimization.extra_high_risk_paths`, which no release ever wrote
into a file, so a value there is always one somebody added.

A **Notes** block follows the problems when there is something worth knowing
that is not wrong; notes never count towards `--strict`, and in `--json` they
are `notes` (`[]` when there are none). One kind is the read-only seat on agy
described above. One names an
installed CLI version that has not been live-checked on this machine (see the
`Live check:` line below). The other: every `when:
high-risk` reviewer judged by the built-in `high_risk_paths` alone -- no list
of the repository's own, no `extra_high_risk_paths` -- is named in a single
note, because the defaults fit common names and can miss this repository's
sensitive paths, leaving the reviewer almost never running. A `high_risk_paths`
drawn only from the defaults, in any order, is not a list of the repository's
own: it is what a config written before 0.6.0 holds, perhaps an older default
list missing patterns added since. A path-scoped reviewer is never named in
it: its patterns were chosen. A reviewer line in
**Roles** ends in `(when: high-risk)` or `(when: paths *migrate*/*, *.sql)` for
a conditional reviewer, and its `--json` entry carries `when` (`high-risk` or
`paths`) and `condition` (the same label as the line) whenever it is not
`always`, plus `paths`, the reviewer's patterns, for a path-scoped one.

When an Antigravity install location (`~/.gemini/config/plugins/dev-orchestra`,
or `.agents/plugins/dev-orchestra` in the repository `doctor` runs in) is this
checkout, whether through a link or junction or because the checkout itself
sits there, `doctor` reports as a problem any `hooks.json`, `mcp_config.json`,
`plugins.json`, `rules/` or `agents/*.md` at the checkout root, because
Antigravity loads them with the skill on its next start; an `agents/` it
cannot list is a problem too. A copy install has none of them. A checkout
registered through a `plugins.json` entry, or a copy staged by `agy plugin
install`, is not checked by `doctor` (see "Installing from a skill checkout" in
`references/workflow.md`). In `--json` it is `antigravity`, with `root`, `live`
(the install locations that are this checkout) and `autoload`.

Each provider block has a `Source:` line -- `built-in` or `user module <path>`.
The **User providers** block is always shown: the directory user adapters are
imported from (or that it is not present, or disabled by
`DEV_ORCHESTRA_NO_USER_PROVIDERS`), what was imported, and every file that
failed to load, which is also a problem. An adapter that raises while being
diagnosed gets `Installed: unknown (adapter failed)` and an `Adapter error:`
line instead of a traceback, and the roles using it show `adapter-error`. In
`--json`: `providers.<name>.origin`, `providers.<name>.adapter_error` and a
top-level `user_providers`. See `references/providers.md`.

A `Resume:` line follows, saying whether `run architect --resume` can continue
a session on that CLI: `verified for claude <version> on <date> (built-in)` or
`(record: <path>)`, `trusted for claude <version> as newer than <version>
(verified on <date>, built-in); not verified itself -- run python
scripts/smoke_live.py --provider claude` (or `record: <path>`), `UNVERIFIED --
<why>; --resume runs fresh until then`, `NOT SUPPORTED -- <why>`, `not reported
by this adapter`, or `not checked (--fast)`. In `--json` it is
`providers.<name>.resume_support` (`status` -- `verified`, `trusted`,
`unverified`, `unsupported` or `unspecified` -- `detail`, `version`, `source`,
`record`, `verified_at`, `newer_than`, `missing`). It is never a problem, the
trusted line included: a run that cannot resume runs fresh.

Beside it, every installed provider except the offline `mock` gets a `Live
check:` line saying whether `scripts/smoke_live.py` has run this CLI version on
this machine -- it is the only thing that runs the real CLIs, and a CLI update
is when their output or flags can drift from the adapter: `passed for
<version> on <date>` (with `, N skipped` when checks were skipped), `FAILED for
<version> on <date> (<check names>)`, `not run for <version> (last passed:
<old version> on <date>)`, `never run on this machine`, or what is wrong with
the record (for example that it is inside the workspace). The record is
`verified/<provider>-smoke.json` in the config directory, written by the
script; reading it is a file read, so `--fast` shows the line too. A version
not yet checked also gets a note: `<provider> <version> has not been
live-checked on this machine (last passed: <version>|never); run python
<path>/smoke_live.py --provider <provider> -- it spends a few real tokens`,
with the real path of the script. A version whose check failed gets the line
only -- whoever ran it has seen the failure -- and so does a record inside the
workspace, which the script would refuse to write. An installed CLI whose
version could not be read shows `version unavailable` and no note: there is no
version to look up. In `--json` it is `providers.<name>.live_check` (`status`
one of `passed`, `failed`, `absent`, `version-unavailable`; `entry`,
`last_passed`, `problem`). It is never a problem.

## run

| Command | Description |
| --- | --- |
| `run <role> [--prompt <text>\|--prompt-file <path>] [--tier <name>] [--mode plan\|implement\|review] [--output <path>] [--timeout <s>] [--idle-timeout <s>] [--resume --resume-prompt-file <path>] [--detach] [--force] [--json] [--print-command] [--extra …]` | Run one configured role. `<role>` is `orchestrator`, `architect`, `implementer`, `review_fixer`, or a reviewer id. `--tier` picks one of that role's configured `model_tiers`; an unknown one is refused rather than run on the default model. Consumes an attempt from that stage's budget and refuses (exit 3) when it is spent, unless `--force`. `run implementer` refuses (exit 5) while a plan exists and is not approved -- in the parent and again in a detached worker, whose refusal lands in the job record whole; `--force` does not apply. |

The prompt may also be piped on stdin (`--prompt-file -` reads stdin
explicitly). Default modes: architect/orchestrator `plan`, implementer and
review_fixer `implement`, reviewers `review`. The orchestrator, the architect
and reviewers are read-only roles: `--mode implement` on one exits 2.
`--print-command` shows the exact CLI invocation without running it, an
argument holding spaces quoted; on agy the prompt file, which only a run
writes, appears as `.ai/agy-prompt-<pid>-<random>.md`. `--extra`
forwards every remaining argument to the provider CLI verbatim on an
`implement` run. On `plan` and `review` only `--add-dir <path>` gets through
(Claude) and nothing does (Codex); anything else exits 2 before an attempt is
spent, and so does any `options.args` a read-only role takes from the project
file. So does a `plan` or `review` run on an installed Claude CLI whose `--help`
does not list `--tools`, `--strict-mcp-config` and `--restricted`, or cannot be
read. A refusal names the flag, its position and where it came from, never its
value, and a detached worker's refusal is written to its job record.

A `plan` or `review` run on `agy`, which has no read-only mode, runs with a
`warning: <role>: read-only is NOT enforced by agy -- ...` line before it when
the seat comes from the global config, and is refused (exit 2, before the run
log is opened or the prompt read) when its provider comes from the project
file. The same holds for any adapter whose installed CLI reports `unenforced`
when the run asks it, including one whose report is not static and so is not
refused by the config commands. An `implement` run on agy is refused the same
way when the project file names `options.skip_permissions` or sets any
`options.args` for that role. Whatever the outcome, the adapter's run warnings
-- an agy status that is not success, the actions agy denied -- are printed as
`warning: <role>: ...` lines, and recorded as `warnings` in the run log's end
event and the job record; the enforcement warning is recorded there too, and
printed only the once, before the run.

A prompt that arrives empty is refused (exit 1) before anything is delegated,
so it costs no attempt: a `--prompt-file` that does not exist, one that is
there and empty, an explicit `--prompt ""`, and a pipe that carried nothing.
The message says which of those it was, and names the path as it was written
as well as where it was looked for. An unreadable `--prompt-file` used to read
as an empty prompt, which was delegated and answered by the provider CLI
complaining about its own stdin.

`--timeout` is the total deadline. `--idle-timeout` is the *no output* deadline:
a wedged agent goes quiet while a slow one keeps producing, so this catches a
stall in minutes rather than at the total deadline. It only applies to providers
that stream progress (the Claude and Codex adapters do, agy's does not; see `references/providers.md`), and is
ignored elsewhere rather than guessed at.

`--output` writes the run's stdout only when the run succeeded and printed
something; a stalled, timed-out or failed run leaves the existing file exactly
as it was and says so on stderr. The target is usually the file the run was
asked to revise, so overwriting it with a fragment destroys the input. Whatever
the refused run did print is kept beside the target as `<output>.rejected`,
named in the same message; a sidecar left by an earlier attempt is removed
rather than left to be read as this one's. A refused write exits 1 even when the
run itself succeeded: `--output` promises that the named file holds this run's
result, so a chained command must not read the stale one as if it were new. A
detached run says the same in its job record, where `jobs show` reports it — its
stderr goes nowhere. Without `--output` the stdout is printed whatever the
outcome, and the exit code is 1 on a failed run either way. It is also 1 when a
run that exited 0 printed nothing but whitespace: the same judgement `--output`
refuses a write on, because which of the two the caller used says nothing about
whether the run answered. That message names the role and quotes the start of
the raw stderr, which is where a CLI that refused the prompt says why.
The run's end event records the same judgement as `answered` (`true` only for
an `ok` run that printed something), so `status` can tell a revision or fix
from an exit 0 over silence.

`--detach` starts the run in its own process and returns a job id immediately,
so the call cannot block. See `jobs` below.

### Revising the plan in the architect's own session (`--resume`)

`--resume` revises this workflow's plan by continuing the session of the last
architect run instead of starting a new one, so the architect does not read
the code and rebuild its context again. It takes two prompts:
`--prompt-file` (or `--prompt`, or stdin) is the full prompt a fresh run gets,
and `--resume-prompt-file` is the short one a continued session gets. Both are
read before anything is spent; which one is sent is decided afterwards.

It is refused (exit 2, before anything is spent, with a fixed message that
names no argument's value) unless all of these hold: the role is `architect`;
the effective mode is `plan` (`--mode implement` is refused as it always was,
and `--mode review --resume` too); `--resume-prompt-file` is given (and it is
refused without `--resume`), and is `-` only when the fresh prompt comes from
`--prompt` or a file, since stdin is read once; and `--output` is this workflow's plan
(`.ai/plan.md`, or the path it resolves to). The same checks run for
`--print-command`, in the parent of `--detach`, and again in the worker.

The session continued is the one the last architect event in this workflow
ended in, forked so the original is left as it was: on Claude the run adds only
`--resume=<id> --fork-session` to the read-only command (Codex forks with the
command in `references/providers.md`), and a raw `--resume`
in `--extra` or `options.args` is refused as any other raw argument is. It
runs fresh instead, with the full prompt, and says why on stderr (`note:
--resume requested, running fresh: <reason>`), when the first of these applies:

| Reason (recorded as `resume.reason`) | When |
| --- | --- |
| `the provider cannot resume a session` | the provider does not resume (Codex, a user adapter) |
| `the provider cannot resume a session (unsupported)` | its `--help` does not list `--resume` and `--fork-session` (Codex: `codex exec fork --help` does not list every flag the fork needs) |
| `the provider cannot resume a session (unverified)` | this CLI version has not been checked to keep a resumed session read-only (see below) |
| `the provider cannot resume a session (unspecified)` | an adapter that resumes but does not say whether that stays read-only |
| `no earlier architect run in this workflow` | nothing to continue |
| `the last architect run is not resumable: it did not succeed` | it failed, stalled or was rejected |
| `the last architect run is not resumable: it did not answer` | it exited 0 over silence |
| `the last architect run is not resumable: it has no session id` | a run log from before this existed |
| `the last architect run is not resumable: its session id is not a UUID` | the recorded id is not one |
| `the last architect run is not resumable: its recorded mode is not plan` | it was not a read-only plan run |
| `the last architect run is not resumable: its output is not this workflow's plan` | it answered something else |
| `the last architect run is not resumable: its recorded provider differs` | the architect's provider changed since |
| `the last architect run is older than design.resume.max_age_seconds` | default an hour |
| `the last architect run's context exceeds design.resume.max_context_tokens` | only when a cap is set |
| `the last architect run's context is unknown and design.resume.max_context_tokens is set` | a cap is set and the run recorded no `context_tokens` to hold against it |
| `the CLI rejected the session it was asked to resume` | the retry described below |

With `(unverified)` a second note gives the adapter's detail: the CLI version
and `python scripts/smoke_live.py --provider claude`. A CLI version is cleared
for resuming when it is in the table shipped with the adapter, or when that
script passed on this machine and recorded it (`references/providers.md`). A
version newer than one cleared that way, and of the same major version,
resumes too, on trust, unless this machine recorded a failure at or below it
that no later pass here has superseded; it runs resumed with a second note
after `note: resuming the last architect session`, the adapter's detail: which
version it is trusted as newer than, and that it has not been checked itself.
So right after the CLI is updated, `--resume` continues on trust until the
script is run on the new version; a version older than every cleared one, a
new major version, or one after a failure here, runs fresh (`(unverified)`),
and `doctor` says the same on its `Resume:` line.

If the CLI rejects the session because it no longer exists, that run is
recorded as a failed event (`resume.outcome: "rejected"`, its stderr not
copied) and the run is made once more, fresh, with the full prompt. An adapter
may refuse to start the continued run itself, and is handled the same way:
Codex refuses when it has no model to pass the fork, or when the session was
not started in this workspace; the refusal's reason follows `note: the CLI
rejected the session it was asked to resume` on stderr, and is not recorded
either. After a rejection by the CLI that second run is another attempt: it is
checked against the architect's budget and consumes one, and when none is left
it is not made (exit 3; `running fresh would spend an attempt`). After a
refusal by the adapter nothing ran, so the fresh run spends the attempt the
continued one took, and is made even when that was the last. `--force` applies as the user gave it, including in a
detached worker. Any other failure of a continued run -- a stall, a timeout, an
error -- is reported as it is today and not retried; the next `--resume` then
runs fresh (`it did not succeed`). A continued Codex run whose fork's rollout
does not confirm a read-only filesystem sandbox is such a failure: its answer
is not used.

Every `run` end event now records `session_id`, `context_tokens` (the context
the model last saw, when the CLI reports it), `cost_usd` and
`cache_read_tokens`; a `--resume` run also records `resume` (`requested`,
`mode` `resumed` or `fresh`, `resumed_from`, `reason`, `outcome`, and
`resume.trust: "newer"` on a run resumed on trust -- never a version), in its
in-flight entry too. A continued run's usage is labelled `architect:resumed`
in `tokens show` (a `--tier` label takes precedence). With `--detach` both
prompts are copied into the job, so an edit to either file after the command
returned does not reach the run, and the job record carries `force`,
`resume_prompt_file`, `session_id` and `resume`.

```bash
dev-orchestra run architect --prompt-file .ai/execution/design-request.md --output .ai/plan.md
dev-orchestra run architect --resume \
  --prompt-file .ai/execution/design-revise-request.md \
  --resume-prompt-file .ai/execution/design-resume-request.md \
  --output .ai/plan.md
dev-orchestra run implementer --print-command
echo "explain the failure" | dev-orchestra run orchestrator
```

## review

| Command | Description |
| --- | --- |
| `review snapshot [--base <rev>] [--no-untracked] [--surrounding none\|enclosing] [--json]` | Freeze the change under review. Exit 1 if empty. A change over `review.context.max_chars` is warned about and still written — taking a snapshot spends nothing, and the refusal belongs to the command that would. `--json` says the same thing in numbers: `change_chars`, `max_chars` and `over_context`. With `review.context.surrounding: enclosing` it also freezes the symbol enclosing every hunk into `review-surrounding.json`, from the tree the diff was taken from, and prints a `context:` line saying how many symbols and characters were frozen and how many files were not extracted, and why; the metadata gains a `surrounding` block. `--surrounding` overrides the setting for this snapshot only: **`enclosing` freezes the candidates for this snapshot even with the setting at `none`**, and without that freeze `review run --surrounding enclosing` is refused; `none` freezes nothing and removes an older frozen file. The setting itself is not changed. See `references/reviews.md` and [Measuring what surrounding context does](limits.md#measuring-what-surrounding-context-does). |
| `review run [--design] [--request <path>] [--iteration N] [--only <ids/roles>] [--sequential] [--context <text>] [--base <rev>] [--timeout <s>] [--idle-timeout <s>] [--force] [--surrounding none\|enclosing] [--high-risk] [--json]` | Run every reviewer against the snapshot; write reports and the consolidated result. Exit 1 only if no reviewer came back `ok` — every reviewer failing, or a round whose change body was too large to inline and was handed over as a file, which is recorded as `partial` rather than clean. The round is derived from the snapshot unless `--iteration` is given, and a round past `review.max_review_iterations` is refused (exit 3) unless `--force` — the round that reached the limit still gets its fix and re-test; only the re-review is refused. A round refused by the optimization gate (tests recorded as failing) also exits 3, and is recorded as `refused` so `optimization report` can count it. So is a change body over `review.context.max_chars` (400,000): nothing is reviewed, the message names the size, the limit and the ways under it, and the round is recorded with `refused_by: "context"` — `--force` runs it anyway and records the round as `over_budget` everywhere it is reported. Whether the body goes into the prompt or over as a path is `review.context.inline_chars` (400,000, the same number by default), and each reviewer entry records the value that decided it. A round is refused the same way once `budgets.max_runtime_seconds` of delegated execution has been spent — a panel is the largest consumer of it — and the message names which budget it was. `--only` runs a subset but still consolidates every reviewer's current report, so nothing is lost — except the report of a conditional reviewer this round left out, which is not consolidated. Each conditional reviewer is added or left out with a `note:` naming why (a high-risk path or, for a path-scoped one, one of its own patterns, `--high-risk` for a `when: high-risk` one, its own open accepted finding on an incremental round, or `--only` naming it), and the decisions are in the event and in `--json` under `optimization.conditional`, with `optimization.declared`. `--high-risk` declares the change high-risk: it adds the `when: high-risk` reviewers and keeps the panel whole, and never changes the level, the findings cap or the gate, so a declared round on a red tree is refused like any other; it is refused with exit 2 on `--design`. With `review.context.surrounding: enclosing` the frozen symbols are adopted within `review.context.surrounding_chars` and what the diff leaves under both limits, a `Surrounding context:` line says how many were adopted and how many left out and why, `--json` carries the round's `surrounding` record, and the size the limit measures is the diff plus the context adopted. `--surrounding none\|enclosing` overrides `review.context.surrounding` for this run only, to review one snapshot with and without the context (see [Measuring what surrounding context does](limits.md#measuring-what-surrounding-context-does)); the setting is not changed, and the line reads `(--surrounding enclosing for this run)` or `Surrounding context: none (--surrounding none for this run; review.context.surrounding unchanged)`. It is refused with exit 2 before anything is charged: with `--design`; on an incremental round (the re-review prompt carries the accepted findings of the moment it runs, so two runs on it would differ in more than the context); with `enclosing` when nothing would be adopted, whatever the reason (a snapshot not frozen with `review snapshot --surrounding enclosing`, no candidates, file delivery, no budget); and on a second run of the same snapshot -- across a `budget reset` too -- when a finding's triage or triage note was set since the last run built it (one carried in from an earlier round does not count). A rerun of the same snapshot stays in its round and registers no findings signature, so the pair does not read as a fix that changed nothing; the first run registers it as usual. A run after a lineage change, or one whose `--iteration` names another round, is no rerun and registers its signature. The run event and `--json` gain a `measurement` block (`surrounding`, full `snapshot` sha256, frozen `tree`, `head`, `base`, `workflow` directory, budget `epoch`, `rerun`, and `inputs`: `context_sha256`, `max_findings`, `inline_chars`, `max_chars`, `force`), and `consolidated.json` gains `measurement` with `triage_at_build` (each finding's `triage` and `triage_note` by key). Without the flag none of this is written. |
| `review consolidate [--design] [--iteration N] [--json]` | Re-parse the existing reports and rebuild the consolidated result. On the code review it leaves out the reports of the conditional reviewers that the last round on the current snapshot left out, so it reads the reports that round did. |
| `review show [--design] [--accepted] [--json]` | Show the consolidated review. |
| `review triage [--design] <ids…> --status <status> [--note <text>]` | Record triage decisions. Each one stamps the finding with `triage_set_at`, `needs-triage` included, so putting a finding back is told apart from never deciding it. |
| `review fix-brief [--design] [--output <path>]` | Emit the accepted-findings brief for the fixer. |
| `review status [--design] [--json]` | Whether a re-review is warranted, the iteration budget, and the round's `coverage` — `round`, `change`, the `inline_chars` the round was measured against, plus the actions that would clear an `unverified` one: narrow the change, or raise `review.context.inline_chars`, then snapshot again — and once that limit has been raised past the size the round recorded, that the same snapshot would be inlined now and `review run` against it is all that is left. `over_budget` says the round only ran because `--force` sent it past `review.context.max_chars`. A round that carried surrounding context adds a `surrounding context:` line -- one per reviewer when they were not handed the same -- naming up to five symbols left out, and `--json` carries the report's `surrounding` block. Once the round budget is spent it also says where that round's last pass stands, read from the ledger, the run log and the approval state (nothing is cleared): `final_fix` — `pending` (fix once more), `retest` (fixed; record the re-test), `done`, `blocked` (no `review_fixer` attempt left) — or with `--design` `final_revision` — `pending` (revise once more), `done`, `blocked` (no `architect` attempt left), `approved`, `implemented` — each `null` before the limit and with a `final_fix_pending` / `final_revision_pending` flag; the last line names the next step and notes a round that repeated the previous one's findings. With `--design` the first line is `design review: <label>` — `on`, `off`, `auto -> run (<reason>)` or `auto -> skip (<reason>)`, the answer `status` gives — and `--json` adds `enabled` (whether the stage runs), `mode` (`on`, `off`, `auto`) and `reason` (`null` under `on` and `off`). See `references/reviews.md`. |

`--design` switches every one of those to the *design* review: `.ai/plan.md`
judged by the same panel before implementation, with its own reports, round
counter and triage under `.ai/reviews/design/`. `review run --design` freezes
the plan itself instead of a diff — there is no `review snapshot --design`,
and no git is needed — and hashes it with the request it answers
(`--request <path>`, default `.ai/execution/design-request.md`; a missing one
is noted, not fatal). No plan exits 2, a round past
`review.design.max_iterations` exits 3 unless `--force` (the round that
reached the limit still gets its revision; only the re-review is refused), and
every reviewer failing exits 1. `review.context.max_chars` is measured over the plan *and*
the request together, because both go into every reviewer's prompt, and the
round is refused before the plan is frozen — so the previous round's reports
and triage are still there to report on. The optimization gate and panel reduction do not apply,
every reviewer runs whatever its `when`, `--high-risk` is refused (exit 2), and
`--base` is ignored. Running it while `review.design.enabled` is false, or
`auto` and this plan would be skipped, prints a note (`note:
review.design.enabled is auto and this plan would be skipped (<reason>);
running because you asked`) and proceeds: the setting says whether the
orchestrator runs the stage, not whether you may. A round run that way is a
design round, so from then on `auto` answers run (`a design round already
ran`) and the loop proceeds as under `true`. See `references/reviews.md`.

A reviewer on `agy`, which has no read-only mode, is warned about before
either kind of round runs (`warning: reviewer <id>: read-only is NOT enforced
by agy -- ...`) when the panel comes from the global config, and fails in the
round with the refusal as its `error` -- the other reviewers still run -- when
the reviewers list comes from the project file -- as is a project reviewer on
any adapter whose installed CLI reports `unenforced` when asked before the
round. After the round, each reviewer's run warnings are printed as `warning:
reviewer <id>: ...` lines, less the enforcement warning already printed before
it, and all are kept as `warnings` on its entry.

`review status --json` reports the budget under the name of the setting it came
from: `max_review_iterations` without `--design`, `max_iterations` with it. The
rest of the payload is the same either way.

Every write of `consolidated.json` -- `review run`, `review consolidate` and
`review triage`, with or without `--design` -- also writes a copy to
`reviews/rounds/<sha12>-<round_id>.json` (`reviews/design/rounds/` for the
design review), overwritten only by a later write to the same round, so a
round's findings and triage survive the next round. `optimization report`
reads them. A report with no snapshot behind it gets no copy, and nor does one
built after the current freeze under an earlier round's id -- a design round
no reviewer returned a review for, or a `review consolidate` before the
round's reviewers are back -- since for the same tree or plan frozen again that
is the earlier round's copy.

`review snapshot` gives every code snapshot a new `round_id` in
`reviews/review-target.json`, including a second snapshot of the same tree,
and `review run` copies it into the consolidated report's `snapshot` and its
run event. `review run --design` gives every round a new `round_id` in
`reviews/design/review-target.json`, including a re-run of the same plan;
`review consolidate --design` and `review triage --design` leave it alone. The
consolidated report names a round only once `review run --design` has had
every reviewer of it return; `review consolidate --design` keeps the round the
previous report named, even while a newer round is running. An approval is
given over the round that was current at the time.

## design

| Command | Description |
| --- | --- |
| `design approve [--json]` | Record the user's approval of `.ai/plan.md` as it is now (sha256 of the plan alone) together with the design review round it was given over. A later revision, or a design review that runs afterwards, needs approving again. The round is the last one with a consolidated report, read together with the findings it names; while a `review run --design` round has started but has no report yet (still running, or it crashed), nothing is recorded and it exits 2. A round that ran to the end with no reviewer's review -- every reviewer failed -- is approved over instead, with a note that the plan has no design review findings at all, so a panel that keeps failing never leaves the user unable to go ahead. Open design findings -- every one not rejected or marked a duplicate, whatever its severity -- are printed, not refused: approving over them is the user's call; findings from a review of an earlier revision of the plan (the frozen `review-target.md` differs from the plan) are labelled as such. Approving the same plan over the same round again says `already approved` and records nothing. No plan exits 2. Run it only after the user said yes in conversation. |

The approval lives in `state.json` under `design_approval`, written by this
command only, with a `design_approval` / `approved` event beside it; `state
record` cannot forge one. `budget reset` keeps it and `workflow remove` deletes
it. A round written before rounds had ids counts as no round: approving over it
records none, and the next `review run --design` makes the approval stale.

## status

| Command | Description |
| --- | --- |
| `status [--json]` | The one command that answers *continue or stop*. Reports a `continue` / `stop-and-report` verdict with reasons, stalled or abandoned stages, what is in flight, remaining budgets, and the open review findings. |

Reading it also clears in-flight entries whose process is gone, so a stage that
died without recording an outcome shows up as `abandoned` instead of appearing
to run forever.

A round refused for size is a `stop-and-report` reason until a round actually
reviews the change — code or design. Nothing else clears it: not a later
refusal of either kind, and not the `abandoned` entry `status` itself writes
when it clears a stage whose process is gone. A refusal leaves the previous
round's consolidation in place, so without that rule an oversized change would
keep answering `continue` out of a clean review of something else.
The reason names the size and the limit; `review.refused_for_size` and
`design_review.refused_for_size` carry the same two numbers in `--json`.

The `Optimization:` line is what the next `review run` would decide, and names
each conditional reviewer it would add or leave out, with the reason:
`; claude-security left out (no high-risk path matched)`,
`; claude-security added (has open accepted finding F3)`, or
`; codex-database left out (no path matches *migrate*/*, *.sql)`. `--json` carries the
same under `optimization.conditional`. It is a prediction only: `status` reads
the configuration without validating it, so a panel `review run` would refuse
is reported by `config validate` and `doctor`, not here.

The `Design review:` line starts with whether the stage runs for this plan:
`on`, `off`, `auto -> run (<reason>)` or `auto -> skip (<reason>)`, as in
`Design review: auto -> skip (5 code files, none high-risk), round 0/2, 0
accepted, 0 blocking`. In `--json`, `design_review.enabled` is that answer
(whether the stage runs), `design_review.mode` is `on`, `off` or `auto`, and
`design_review.reason` is the reason, `null` under `on` and `off`. A skip is
not a reason and does not change the verdict.

`design_approval` and the `Plan approval:` line say whether the plan still has
to be put to the user (`references/workflow.md`). They are not a reason and do
not change the verdict: `run implementer` enforces them. The one exception runs
the other way: once the user has approved the current plan, a design review
budget spent with findings open is no longer a reason, because the user was
shown them and decided to go ahead.

A spent round budget is not a reason until the round that reached it has had
its last pass. Design (`design_review.final_revision`): `pending` — fold the
findings into the plan once more, no re-review — is `continue`; `done` (the
plan differs from the frozen one, or the architect answered after the round
with `--output` naming the plan)
is `stop-and-report` with `; revised after the last round, not re-reviewed --
present the plan and ask` or `; the architect left the plan unchanged …`;
`blocked` (no architect attempt left) stops at once; `approved` (an approval
recorded for this plan and round, whether or not `design.require_approval` is
on) gives no reason; `implemented` (the implementer already ran on this plan)
and `unaccepted` (none of the open findings is `accepted`, so there is nothing
to fold in) keep the plain reason. Code (`review.final_fix`): `pending` (fix
once more) and
`retest` (fixed; waiting for a `test` or `re-test` outcome recorded after the
fix) are `continue`; `done` is `stop-and-report` with `; fixed and re-tested
after the last round, not re-reviewed -- report`; `blocked` (no `review_fixer`
attempt left) stops at once; `unaccepted` (nothing accepted to fix) keeps the
plain reason. With a plan written, `architect has no attempts left` is a reason
only while a design round within its budget still has blocking findings to
revise for. The `Review:` and `Design review:` lines say when
a last pass is still owed.

Two reasons wait for it too. `the last (design) review round found exactly
what the previous one found` is held back while the revision or the fix and
its re-test are still owed — `identical_rounds` in `review` / `design_review`
carries the count meanwhile, and the human lines say `identical to the
previous round`. And `<stage> has no attempts left` is given only for a stage
still needed: `architect` only while there is no plan (a revision still owed
says so in its own reason, and a pending approval's line notes that no
attempt is left for changes), `review_fixer` only while the fix is not made.

```bash
dev-orchestra status --json
```

```json
{
  "verdict": "stop-and-report",
  "reasons": ["review budget spent (2/2 rounds) with 1 finding(s) still open; fixed and re-tested after the last round, not re-reviewed -- report"],
  "stalls": [],
  "review": {"iteration": 2, "max_review_iterations": 2, "blocking": ["F1"], "accepted": 1, "refused_for_size": null, "identical_rounds": 1, "final_fix": "done", "final_fix_pending": false},
  "design_review": {"enabled": true, "mode": "auto", "reason": "touches db/migrate/ (Files to Modify)", "iteration": 0, "max_iterations": 2, "blocking": [], "accepted": 0, "identical_rounds": 0, "final_revision": null, "final_revision_pending": false},
  "budgets": {"implementer": {"used": 2, "limit": 5, "remaining": 3}}
}
```

## jobs

Detached runs. The deadlines bound how long an agent can misbehave, but while
one runs the caller is *inside* that call — for an orchestrator that is itself
an agent, a long block is indistinguishable from a crash. Detaching removes
that: the work runs elsewhere and the wait has a deadline of your own.

| Command | Description |
| --- | --- |
| `jobs list [--json]` | Every recorded job, newest first. |
| `jobs show <id> [--output] [--json]` | One job, optionally with its output. |
| `jobs wait <id> [--timeout <s>] [--poll <s>] [--json]` | Wait, but never longer than `--timeout` (60s default). Exits 4 if the job was still running when the wait ended — a normal outcome, not an error. Exits 1 if the job refused its `--output` write, as the foreground run would. |
| `jobs cancel <id>` | Stop a running job and its process tree. |

```bash
id=$(dev-orchestra run implementer --prompt-file plan.md --detach --json | jq -r .id)
dev-orchestra jobs wait "$id" --timeout 120   # exit 4 means "still going"
dev-orchestra jobs show "$id" --output
```

A job's whole life is one JSON file under `.ai/jobs/`, written by the worker, so
progress survives the parent dying. A job whose worker process is gone without
recording an outcome is reported as `abandoned` rather than appearing to run
forever.

## budget

| Command | Description |
| --- | --- |
| `budget show [--json]` | Attempts spent per stage, delegated-run total, delegated runtime used. |
| `budget consume <stage> [--force]` | Claim an attempt at a stage the orchestrator runs itself (notably `test`). Exits 3 when the budget is spent. |
| `budget reset` | Start the budgets again, including the review round counter. The token account is kept — it is a record of what the work cost, not a budget, and no reset clears it. Also happens automatically once a ledger has been idle for `budgets.session_idle_reset_seconds`. |

`run` and `review run` consume their own budgets, so `budget consume` is only
needed for stages the orchestrator performs directly.

## tokens

| Command | Description |
| --- | --- |
| `tokens show [--json]` | What the workflow has spent, per stage and per reviewer. |

Accounting, not a budget: nothing here refuses a run. Deliberately separate from
`budget`, which is enforced -- reading one as the other is the mistake this
split exists to prevent.

The account covers the **whole workflow**, not the current budgets: `budget
reset` and the idle reset start the budgets again and carry the account across,
so the totals here span every reset the work has been through, and no reset
clears them; only deleting the workflow (`workflow remove`) does. A separate
account is therefore a separate workflow --
`dev-orchestra --workflow <id>` gives that work its own `.ai/workflows/<id>/`,
and with it its own ledger, budgets and account.

The numbers come from the delegated CLIs, so they are only as complete as the
CLIs are talkative. Claude Code reports input, output, cache-read and
cache-write counts plus a cost on its `result` event. Codex prints one total, in
prose, and a wording change makes it unreported rather than wrong. Every column
is therefore paired with `meas.` -- how many of that stage's runs reported
anything. When some did not, the output says the totals are a *floor*.

`billed` sums input + output + cache-write. Cache reads are excluded on purpose:
they cost about a tenth of fresh input, and folding them in would rank a
well-cached stage above an expensive one. Use `cost` for money.

`prompt_chars` is the size of the prompts dev-orchestra composed itself. It is
the only part of the input this repository can shorten, which is why it is
counted apart from the total.

`tools` and `tool out` are what the delegated agent did with its tools: how
many tool calls it made, and how many characters those tools printed back. They
come from Claude Code's event stream; **Codex reports neither**, and the footer
says how many runs the columns cover. In these two columns `-` means *the run
did not report* and `0` means *the run reported using no tools* — a distinction
the token columns do not make, and the one that makes a reviewer which opened
nothing visible.

**`tool out` is observed tool output, not source read.** A reviewer that runs
`wc -l` on a two-hundred-line file is counted three characters; one that runs
`cat` on the same file is counted the whole of it. The two are
indistinguishable from outside the CLI, so **how much source a reviewer read is
not knowable from here at all** — these figures are a proxy, useful for
comparing like with like (the same reviewer id, before and after a change) and
not for stating what was read.

Every tool call is counted, whatever it is named, with the breakdown kept in
`tool_uses_by_name` (`--json`, and in each run's `usage`). Counting only `Read`
would undercount: a read-only Claude run has no `Bash`, but `Grep` and `Glob`
read files too, and an implement run has every tool — the run measured while
designing this, before read-only runs lost `Bash`, read a file with `wc -l` and
never called `Read`.

A run recorded before these counts existed cannot say whether it used tools,
and is reported that way rather than as having used none. The first run written
into such an account creates `tool_reported_runs` -- whether or not it reports
tools itself -- so the count of runs that predate counting is carried in
`tool_unknown_runs` instead of being read off a missing key, and the caveat
survives every run recorded afterwards.

A Claude role configured with `options.output_format: json` reports no tool
activity either: that format prints one `result` object and never emits a tool
event, so its runs are `-` rather than a measured `0`.

```bash
dev-orchestra tokens show
dev-orchestra tokens show --json
```

## optimization

| Command | Description |
| --- | --- |
| `optimization report [--json]` | What `optimization.level` has decided, over every review round this project has recorded, which high-risk patterns escalated it, and what the reviewers billed. |

Read from the run log, not the ledger. A level's effect is a *rate* -- how
often it refused a round, how often it cut the panel -- and a rate needs
rounds; `budget reset` starts a fresh ledger, while the event log keeps
accumulating.

For the same reason it reads **every workflow** in `.ai/`, not just the current
one: one workflow is a handful of rounds, which is not a rate. `--workflow <id>`
narrows it to one, which answers "what did the level do in this piece of work"
rather than "in this repository".

```
Review rounds recorded: 14 (12 ran, 2 refused)
  levels in force        aggressive x14
  gate verdicts          allow x12, refuse x2
  panel reduced          5
  escalated (high risk)  3

Reviewer runs: 19 (19 reported usage), 823,104 billed
  68,592 billed per round that ran

Estimated saving from 2 refused round(s): ~137,184 billed tokens.
An estimate: what a round that did not happen would have cost is
unknowable, so this is the mean of the 12 that did.
```

With conditional reviewers configured, `when: high-risk` or path-scoped, a
`conditional reviewers` row
follows `panel reduced` -- `added x3, left out x7`, plus
`declared with --high-risk x2` when any round was declared -- counted over
every code round, refused ones included; `--json` has it as `conditional`
(`added`, `left_out`, `declared_rounds`). A log with no such decision prints no
row. A declared round is not an escalation and never appears under
`escalated (high risk)`; a hit on an `optimization.extra_high_risk_paths`
pattern is one, and its pattern is listed like any other. When every round
escalated, the advice names both `optimization.high_risk_paths` and
`optimization.extra_high_risk_paths`.

Everything above `Reviewer runs:` is code review's alone. `review.design`
rounds are reviewed against a plan, which has no diff to measure and no test
result to gate on, so no level decided anything for them -- they appear in the
cost and in no rate. With design review on, the spend splits:

```
Review rounds recorded: 4 (4 ran, 0 refused)
  levels in force        balanced x4
  gate verdicts          allow x4
  panel reduced          0
  escalated (high risk)  0

Reviewer runs: 12 (12 reported usage), 909,313 billed
  code review            8 (8 reported usage), 558,884 billed over 4 round(s), 139,721 each
  design review          4 (4 reported usage), 350,429 billed over 2 round(s), 175,214 each
```

The two are never averaged together: a round against a plan and a round against
a diff are not the same unit of work, so a figure spanning both describes
neither. With design review off there is no design row -- a `0` for a stage
that never ran is noise pretending to be a measurement. In `--json`,
`reviewer_runs`, `measured_runs`, `billed_tokens` and `billed_per_round` are
the code-review figures they have always been; the design ones are
`design_rounds`, `design_reviewer_runs`, `design_measured_runs`,
`design_billed_tokens` and `design_billed_per_round`. `design_rounds` counts
the rounds that *ran*: one that failed or was abandoned after a kill billed
nothing, so it is neither a round here nor a divisor under one.

Where the reviewers reported what they did with their tools, a block follows:

```
Tool activity, per run and only over the runs that reported it:
  code review            6.5 use(s)/run, 41,300.0 observed output chars/run (4 of 8 run(s) reported)
```

The denominator is `tool_reported_runs`, never `reviewer_runs`, and it is
printed for that reason: Codex reports no tool activity, so dividing by the
whole panel would halve the figure for no reason but the panel's composition —
and the comparison these numbers exist for would move whenever a reviewer is
added or dropped. Nothing reported means no row rather than a row of zeroes.

**Observed output is what the tools printed back, not source read**; `wc -l`
returns three characters for a two-hundred-line file. In `--json`:
`tool_reported_runs`, `tool_uses`, `tool_output_chars`, `tool_uses_per_run`,
`tool_output_chars_per_run`, and the `design_`-prefixed five beside them.

Once a code round has carried [surrounding context](reviews.md#surrounding-context),
the code rounds that ran are split in two:

```
Surrounding context (review.context.surrounding), code review rounds only:
  with context           3 round(s), 6 run(s), 412,300 billed, 137,433 per round; 4.0 use(s)/run, 12,400.0 observed output chars/run (3 of 6 run(s) reported); 114,360 context chars adopted, 9,812 left out
                         per run and 1k chars of change (3 sized round(s), 61,200 chars): 3,368.5 billed over 6 billed run(s), 607.8 observed output chars over 3 reporting run(s)
  without context        9 round(s), 18 run(s), 1,522,880 billed, 169,208 per round; 9.6 use(s)/run, 44,120.0 observed output chars/run (9 of 18 run(s) reported)
                         per run and 1k chars of change (7 sized round(s), 210,400 chars): 2,851.7 billed over 14 billed run(s), 1,425.9 observed output chars over 7 reporting run(s)
```

A round is `with` only if some reviewer was actually shown context; a round
with the setting on that adopted nothing is `without`. **Compare the second
line of each, not the first.** The raw figures move with the size of each
change and with the size of the panel -- a small change is routinely cut to one
reviewer -- so the second divides by the change's size weighted by the runs
that reported each figure: billed over `change_chars × runs that reported
billed`, tool output over `change_chars × runs that reported tools`, both over
the rounds that recorded their size. Nothing is printed until a round with
context exists. In `--json`, `by_context.with` and `by_context.without` are
always present, each with `rounds`, `reviewer_runs`, `measured_runs`,
`billed_tokens`, `billed_per_round`, the tool figures, `sized_rounds`,
`change_chars`, `sized_billed_tokens`, `billed_run_change_chars`,
`sized_billed_runs`, `sized_tool_output_chars`, `tool_run_change_chars`,
`sized_tool_runs`, `billed_per_run_per_1k_change_chars`,
`tool_output_chars_per_run_per_1k_change_chars`, `adopted_chars` and
`trimmed_chars`. `tokens show` is a ledger total and cannot split rounds, so it
is unchanged; the round-by-round comparison is this one.

That split compares different changes. Once one snapshot has been reviewed
both ways with `review run --surrounding none` and `--surrounding enclosing`
(see [Measuring what surrounding context does](limits.md#measuring-what-surrounding-context-does)),
a block pairs the two runs:

```
Paired on one snapshot (--surrounding none vs enclosing: the same frozen diff, tree, panel and prompt inputs):
  3f2a9c1b7e04 in issue-54   change 12,400 chars; panel claude-general (claude, opus), codex-general (codex, gpt-5); 3,100 context chars adopted (3,521 as carried), 0 left out
      with:    2 run(s), 41,200 billed, 20,600.0 per run; 3.0 use(s)/run, 9,800.0 observed output chars/run (1 of 2 run(s) reported)
      without: 2 run(s), 45,900 billed, 22,950.0 per run; 6.0 use(s)/run, 22,100.0 observed output chars/run (1 of 2 run(s) reported)
      delta:   -2,350.0 billed/run, -3.0 use(s)/run, -12,300.0 observed output chars/run
  total, 1 pair(s) counted   with: 20,600.0 billed/run, 3.0 use(s)/run, 9,800.0 observed output chars/run; without: 22,950.0, 6.0, 22,100.0; delta: -2,350.0, -3.0, -12,300.0
```

followed by a note on what a pair does and does not hold equal. Only runs made
with `--surrounding` are paired; the side is the flag's value, and the pair key
is the workflow directory, the full snapshot sha256, the frozen tree, `head` and
`base` -- never the budget epoch. The latest run of each side is the one
paired. Billed per run is over the runs that reported usage, the tool figures
over the runs that reported tools. A pair is listed but left out of the total,
with the reason at the end of its first line, when:

- its panels differ (`panels differ (without: ...)`): the sets of
  `(id, provider, model, role)` are not equal -- the role changes the prompt;
- a reviewer run on either side did not deliver (`not delivered (with: codex-general failed)`):
  any status but `ok`;
- the two runs had different prompt inputs (`inputs differ (context, max_findings)`);
- the enclosing run adopted nothing (`nothing adopted`).

In `--json`, `paired` is always present: `pairs` (each with `workflow`,
`epoch` for both sides, `snapshot`, `tree`, `same_panel`, `delivered`,
`same_inputs`, `nothing_adopted`, `counted`, `panel`, `without_panel`,
`undelivered`, `inputs_differ`, `change_chars`, `adopted_chars`,
`context_chars`, `trimmed_chars`, `with`, `without` and `delta`),
`pairs_total` (the counted pairs), `pairs_listed`, the totals `with`,
`without` and `delta` over the counted pairs, and `note`. Each side carries
`reviewer_runs`, `measured_runs`, `billed_tokens`, `billed_per_run`,
`tool_reported_runs`, `tool_uses`, `tool_output_chars`, `tool_uses_per_run` and
`tool_output_chars_per_run`; `delta` is with minus without per run, `null`
where either side has nothing to divide by. `by_context` is unchanged.

Once a round's report can be read, a scorecard follows for each stage: what
each reviewer reported, what the owner decided about it, and what its runs
cost. The findings and triage are read from the archived report of every
round (`reviews/rounds/`, see [Re-review](reviews.md#re-review)), the runs and
the cost from the run log, and the two are matched round by round:

```
Reviewer scorecard, code review: 18 of 27 recorded round(s) had a report to read.
  claude-general         49 reported: 43 accepted, 1 rejected, 4 duplicate, 1 open; 37 found alone (33 accepted)
                         18 run(s), 2,794,838 billed, $42.07 over 18 priced run(s); 2% rejected, 64,996 billed / $0.98 per accepted
  codex-general          25 reported: 15 accepted, 2 rejected, 6 duplicate, 2 open; 16 found alone (11 accepted)
                         17 run(s), 1,166,386 billed, no cost reported; 9% rejected, 77,759 billed per accepted, $ -
  localllm-qwen          22 reported: 1 accepted, 19 rejected, 2 duplicate, 0 open; 20 found alone (1 accepted)
                         9 run(s) (6 failed), nothing reported; 86% rejected, per accepted withheld under 10 accepted (when: high-risk; left out of 9 round(s))
  panel                  96 reported: 59 accepted, 22 rejected, 12 duplicate, 3 open
                         44 run(s), 3,961,224 billed, $42.07 over 18 of 44 run(s); 24% rejected, 67,139 billed / $0.71 per accepted

Review effort, code and design together: 128 accepted over 28 of 46 recorded round(s); 5,614,101 billed,
  $71.90 over 30 priced run(s); 43,860 billed / $0.56 per accepted
```

each block followed by notes on what its figures can claim. How a round is
told apart, and how its events find its report:

- **A round is a freeze**, named by the first twelve characters of the
  snapshot's sha256 and the `round_id` it was given. The same tree or plan
  frozen twice repeats the sha and is two rounds. A `--only` re-run and both
  runs of a `--surrounding` pair are one round: every event of it adds its
  cost, and the findings and triage are the last run's.
- **An event with a `round_id`** matches the report of exactly that round.
  One recorded before events carried it matches the report of its sha only
  when there is exactly one; two reports of one sha are two rounds, and taking
  the newer would pin the old round's cost on the new round's findings. An
  event whose reviewer entries carry no snapshot stamp matches the live report
  when their iterations agree and nothing more certain has taken it.
- **A round with no report to read is left out of every figure, its cost
  included**, so the cost and the findings describe the same rounds. Before
  0.11.0 only a workflow's last round was kept, and the lost rounds reviewed
  the change before its findings were fixed: a rate over what survives is
  biased, and the direction of the bias is not known. The header says how many
  recorded rounds could be read. A round no reviewer returned a review for is
  not a gap, and is not read even when it has a report -- a code round writes
  one whether or not anyone returned, and no reviewer of the round put what is
  in it there. Its runs and cost still count, and the header counts it apart.
- **A finding is counted once per workflow and stage**, by its `key`: one
  nobody fixed comes back in every round. The rounds are taken in the order
  their events were logged, and the last *explicit* triage wins --
  `accepted`, `rejected`, `duplicate`, `needs-investigation`, or anything
  carrying `triage_set_at`. A `needs-triage` without the stamp is the default
  a rebuilt report gives a finding, and does not undo an acceptance; one
  with it is `review triage --status needs-triage` and does. Undecided is
  `open`.
- **Found alone is an upper bound on what dropping the reviewer would lose**:
  reported by that reviewer alone, and not linked in any round, as a possible
  duplicate, to another reviewer's finding with either side triaged
  `duplicate`. A duplicate no candidate link joined is still counted.
- **Rates are printed from 10 decided findings** (`accepted`, `rejected`,
  `duplicate`), and per-accepted figures from 10 accepted; below that the
  counts stand alone. A per-accepted figure is over the readable rounds only,
  and the unread rounds could move it either way. The cost totals are floors:
  a run that reported nothing adds nothing, and one that reported tokens but
  no price adds no dollars, so a reviewer that prices nothing has no cost per
  accepted rather than `$0.00`.
- **The code and design figures are added only in the last line.** Its
  divisor is an accepted finding, one defect the owner decided to fix
  whichever stage found it, where a per-round figure divides by a round, which
  is a different unit of work for a plan and a diff. The mix of stages still
  moves it, so it is read beside the two stage blocks.
- **A conditional reviewer's cost line ends with its condition** and how many
  of the rounds whose cost is in it sat out, so "ran N, left out of M" says
  whether the condition is doing its job. A round is sat out once however
  many events it has, and not at all when one of them -- a `--only` re-run,
  say -- ran the reviewer. A reviewer left out of every round still gets a
  row, its counts zero. A round sat out has no run, so its cost per accepted
  is not diluted by it. The design review ignores conditions, and events from
  before conditional reviewers record none.

It says what review bought, not whether review got worse. A higher cost per
accepted finding is what better code under review looks like, and also what a
reviewer that has started missing defects looks like, and also what a price
rise looks like; the figure cannot tell the three apart.

A stage with no report to read prints no block. In `--json`, `scorecard` is
always present, with `code`, `design` and `total`. Each stage has
`rounds_recorded`, `rounds_read`, `rounds_unreviewed`, `rerun_rounds` (the
`--surrounding` pairs among the rounds whose cost is in), `workflows_read`,
`findings`, `reviewers` (by id) and `panel`. A reviewer carries `runs`,
`failed_runs`, `measured_runs`, `priced_runs`, `billed_tokens`, `cost_usd`,
`reported`, `accepted`, `rejected`, `duplicate`, `open`, `alone`,
`alone_accepted`, `rejection_rate`, `billed_per_accepted` and
`cost_per_accepted`, the last three `null` below their threshold. A reviewer
some counted round recorded as conditional also carries `when` (`high-risk`
or `paths`, from the latest such round) and `left_out_rounds`; `panel` and
`total` the same without the `alone` pair, a finding reported by two reviewers
counted once. `total` also sums the four round counts.

When every round escalated, the report says so outright: the level as
configured never applied, and the patterns that did it are named. A dial
escalated out of existence on every round and a dial that never fires look
identical in a count, and only the pattern says which -- `*.tf` matches
constantly in an infrastructure repository, and `optimization.high_risk_paths`
is the setting to narrow.

The saving is an estimate and says so. What a refused round *would* have cost
cannot be known -- it did not happen -- so the figure is the mean of the rounds
that did run in the same repository, which is the closest honest stand-in.

A round recorded with no test result is reported too. The gate reads what
`state record test ok|failed` wrote, so a round where nothing was written had
nothing to act on and cannot have fired -- which is a different thing from a
level that had no effect, and the two are easy to confuse from the totals alone.

### Architect revisions

What revising the plan cost, split by whether the revision continued the
architect's session (`resumed`, from `run architect --resume`) or ran fresh.
Per workflow, only architect runs whose `--output` was that workflow's plan
count: the first that succeeded and answered is the initial design, and every
one after it is an attempt at a revision. Each revision is measured as its
cost against its own workflow's initial run, because plans differ in size far
more than the two kinds of run differ; an initial run that reported no cost
above zero (the mock reports `0.0`) gives no ratio. A group's line counts the
completed revisions, the mean ratio, and the ratio per completed revision
counting what the failed attempts on the way cost:

```
Architect revisions (cost against each workflow's initial design run):
  resumed: 3 revisions (3 priced, 3 with ratio) cost ratio to initial 0.21 mean, 0.27 per completed incl. 1 stalled + 0 rejected + 0 failed attempts (0 priced)
  fresh: 1 revisions (1 priced, 0 with ratio) cost ratio to initial n/a (initial run has no usable cost); 0 stalled + 0 rejected + 0 failed attempts (0 priced)
  --resume ran fresh because:
    no earlier architect run in this workflow x2
```

The reasons are counted as the fixed phrases `run --resume` records. The block
is printed whenever there is an attempt, with or without review rounds, and not
at all otherwise. In `--json` it is `architect_revisions`: `attempts`,
`resumed` and `fresh` (each with `runs`, `measured_runs`, `priced_runs`,
`ratio_runs`, `billed_tokens`, `cost_usd`, `cost_ratio_sum`,
`duration_seconds`, `context_runs`, `context_tokens`, the means
`billed_per_run`, `cost_per_run`, `cost_ratio_mean`, `duration_per_run`,
`context_per_run`, `failed_attempts` and `cost_per_completed_ratio`), and
`fallbacks` (`total`, `reasons`).

## progress

| Command | Description |
| --- | --- |
| `progress record <stage> --signature <text> [--json]` | Record what a stage produced. Identical consecutive signatures mean the last attempt changed nothing, and the command says to stop. |

```bash
dev-orchestra progress record test --signature "3 failed: test_totals, test_discount, test_coupon"
```

Reviews register their own signature automatically, from the set of open
findings.

## workflow

Artifacts live in `.ai/workflows/<id>/`, one directory per workflow, so two
sessions in the same checkout no longer share a plan, a report, a budget or a
round counter. The id is resolved per command, in order, from `--workflow`,
`DEV_ORCHESTRA_WORKFLOW`, the host's session id (hashed to twelve characters),
`current.json`, and finally a new id. `workflow show` says which rule answered.

A path written against the container is resolved inside the workflow:
`--output .ai/plan.md` means the plan of *this* workflow. Paths outside `.ai/`,
and paths that already name a workflow, are used as written.

This separates the artifacts, not the working tree: one checkout has one set of
files, and the reviewers read `git diff` of it. For work that really runs at
the same time, give each workflow its own worktree (`git worktree add ../x x`),
which is a different root and therefore a different `.ai/`.

Workflow directories are never pruned. The first command of a new workflow
notes, once and on stderr, the other workflows that have not been active for
`workspace.stale_notice_days` days (default 30; `0` turns it off). A workflow
with a stage in flight is not named. Nothing is deleted: `workflow remove` is
still the only thing that deletes a workflow.

| Command | Description |
| --- | --- |
| `workflow list [--json]` | Every workflow here, most recently active first, marking the current one and any stage in flight. |
| `workflow show [--json]` | Which workflow this command is in, where its artifacts are, and which rule chose it. |
| `workflow use <id>` | Remember an id for this directory (`current.json`). For hosts that export no session id; a host that does export one still wins. |
| `workflow remove <id> --yes` | Delete one workflow's artifacts. Refuses without `--yes`, and refuses the workflow you are in. |

## state / summary

| Command | Description |
| --- | --- |
| `state show [--json]` | The recorded stage events for this project. |
| `state record <stage> <status> [--detail k=v …]` | Append a stage outcome (for stages not run through `run`). `state record test ok\|failed` is what the review gate reads; record a re-test the same way. |
| `summary [--json]` | The end-of-run stage + model summary, including `design_approval` once a plan was approved. |

## Environment variables

| Variable | Effect |
| --- | --- |
| `DEV_ORCHESTRA_CONFIG` | Use this exact file as the global config layer |
| `DEV_ORCHESTRA_HOME` | Use this directory instead of the platform config directory |
| `DEV_ORCHESTRA_WORKFLOW` | The workflow to use, ahead of any host session id |
| `DEV_ORCHESTRA_SESSION` | A session id to derive the workflow from, for hosts that export none |
| `DEV_ORCHESTRA_MOCK_DIR` | Canned responses for the mock provider |
| `DEV_ORCHESTRA_MOCK_RESPONSE` | Inline canned response for the mock provider |
| `DEV_ORCHESTRA_MOCK_FAIL` | Make mock runs fail (`1` = all, otherwise a prompt substring) |
| `CODEX_HOME` | Respected when locating the Codex CLI's config and credentials |
| `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI` | Test-only: hides both provider CLIs, reproducing CI |

## Troubleshooting

Start with `dev-orchestra doctor`; `doctor --json` gives a machine-readable
version of all of this.

| Symptom | Cause and fix |
| --- | --- |
| `Source: built-in defaults, fitted as preset standard` | No config file yet; the built-in `standard` preset fitted to the installed CLIs is in force. `dev-orchestra config setup`, or `config setup --preset <name>`. |
| `note: no config file; running preset standard …` | The same, from `run` and `review run`; a new workflow also prints the whole configuration once. |
| `codex: … does not vouch for …` | A family this Codex CLI does not offer. Check `dev-orchestra model list`, use `recommended-coding`, or pin an exact id. |
| `claude: cannot resolve model family 'x'` | Not an advertised alias. `dev-orchestra model list`. |
| `Installed: no` | The CLI is not on PATH. Install it yourself; the skill will not. |
| `Failed to authenticate` from a delegated CLI | Log in with that CLI directly (`claude`, `codex login`). `doctor` reports credential *presence*, not validity. |
| `review snapshot` says empty | Nothing changed vs `HEAD`. Use `--base <rev>`, or check the implementation ran. |
| `not a git repository` | Snapshots need git. `git init`, or review a committed repo. |
| One reviewer failed | Expected to be survivable. The workflow's `reviews/consolidated.md` gives the reason. |
| The implementer cannot run tests | `acceptEdits` auto-approves edits, not shell commands. Allow-list the command in the project's own CLI settings, or set `implementer.options.permission_mode`. |
| Two findings are obviously the same | Auto-merge is conservative by design. Check the "Possible duplicates" list and triage one as `duplicate`. |
| Reviews never finish | Lower `review.timeout_seconds`, or use `--sequential` to see which reviewer hangs. |
| Config parse error | The built-in YAML parser rejects anchors, aliases and block scalars. Simplify, or install PyYAML. |
| `—` appears as `\u2014` | The console cannot encode it -- cp932 on Japanese Windows, for instance. The character is escaped rather than dropped or fatal. `chcp 65001`, or `PYTHONIOENCODING=utf-8`, shows it properly. |
