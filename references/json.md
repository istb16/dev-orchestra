# `--json` output

<!-- contents: start -->

**Contents**

- [Reading the tables](#reading-the-tables)
- [Configuration and setup](#configuration-and-setup)
  - [`config show`](#config-show)
  - [`config validate`](#config-validate)
  - [`config suggest-roles`](#config-suggest-roles)
  - [`model list`](#model-list)
  - [`reviewer list`](#reviewer-list)
  - [`doctor`](#doctor)
  - [`hooks status`](#hooks-status)
- [Runs and jobs](#runs-and-jobs)
  - [`run`](#run)
  - [`jobs list`](#jobs-list)
  - [`jobs show`](#jobs-show)
  - [`jobs wait`](#jobs-wait)
- [Reviews](#reviews)
  - [`review snapshot`](#review-snapshot)
  - [`review run`](#review-run)
  - [`review consolidate`](#review-consolidate)
  - [`review show`](#review-show)
  - [`review status`](#review-status)
  - [`design approve`](#design-approve)
- [Workflow state](#workflow-state)
  - [`status`](#status)
  - [`state show`](#state-show)
  - [`budget show`](#budget-show)
  - [`tokens show`](#tokens-show)
  - [`optimization report`](#optimization-report)
  - [`progress record`](#progress-record)
  - [`workflow list`](#workflow-list)
  - [`workflow show`](#workflow-show)
  - [`summary`](#summary)

<!-- contents: end -->

What each command prints with `--json`, in one place: the type of the value
on stdout, its top-level keys, and the values a key with a fixed set can take.
The commands are the ones [cli](cli.md) lists as taking `--json`; what a
command does, and what its text output says, is there.

What of this is promised is under "Compatibility" in the README: a key may be
added; a key is not removed, renamed or given a new meaning or type; the
values listed here for a key that takes one of a set are kept; the sentences
inside `notes` and `warnings` are text and are not.

## Reading the tables

- The value on stdout is one JSON value, printed after everything else has
  gone to stderr. It is an **object** unless the heading's first line says
  **list** (one entry per item, each an object) or **mapping** (an object
  keyed by a name, each value an object). For a list or a mapping the table
  gives the keys of each entry.
- A key whose meaning starts with **Only** is printed only then; every other
  key is always there, unless the text under the table names an exception.
  `tests/test_json_keys.py` holds each command to these tables: every key
  that is always there is printed, and every key the test sees printed is
  listed here.
- Types: `string`, `integer`, `number` (integer or fractional), `boolean`,
  `object`, `list`, and `null` where a key can be empty. A path is a
  `string`, absolute unless the meaning says relative. A time is a `string`
  in UTC, `2026-10-12T04:31:27Z`.
- A nested object's keys are named here where the list is short; the rest are
  in the command's section of [cli](cli.md) or the reference linked here.
  A `*_real` key beside a path (`global_real`, `path_real`, `record_real`,
  ...) appears only when the file is really stored somewhere else, as under
  a Microsoft Store Python on Windows ([configuration](configuration.md)).
- A command refused before it has anything to report -- a usage error, a
  refused run or round -- prints nothing on stdout: its exit code and stderr
  say why.

## Configuration and setup

### `config show`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `source` | string | Which configuration is shown: `effective (project: <path or none>, global: <path or none>)`, or the file of the `--scope` layer. |
| `config` | object | The configuration: merged, or the layer as it is on disk with `--scope`. |
| `providers` | object | Keyed by every provider the configuration refers to: where it comes from, `{"kind", "path"}` (`kind` `builtin` or `user`), or `null` for a name with no adapter. |
| `preset` | object | The preset in force: `name` (`null` when none), `source` (`implicit`, `global` or `invalid`) and `notes` (list of strings: what was refitted). |
| `project` | string or null | The project file in force. Not printed with `--scope`. |
| `global` | string or null | The global file in force. Not printed with `--scope`. |
| `reviewer_origins` | list of strings | Where each seat of the code panel was written, in panel order: `fit`, `global`, `project`, `global extra` or `project extra`. Not printed with `--scope`. |
| `design_reviewer_origins` | list of strings | Only in the effective view with a design panel the fit or a file sets: where each seat of it was written (`fit design`, `global design`, `project design`, `global design extra`, `project design extra`), or the code panel's label for a seat copied from it. |
| `source_real` | string | Only with `--scope global` when the file is stored elsewhere. |
| `global_real` | string | Only in the effective view when the global file is stored elsewhere. |

### `config validate`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `valid` | boolean | Whether the effective configuration is valid; the exit code is 1 when not. |
| `problems` | list of strings | What makes it invalid. |
| `warnings` | list of strings | What does not make it invalid but refuses or ignores something: see `config validate` in [cli](cli.md#config). |

### `config suggest-roles`

Object. Notes go to stderr as well.

| Key | Type | Meaning |
| --- | --- | --- |
| `root` | string | The listing root: the repository, or the directory outside one. |
| `source` | string | How the files were listed: `git` (`git ls-files`) or `walk`. |
| `files` | integer | How many files were listed. |
| `truncated` | boolean | Whether the listing stopped at its limit. |
| `notes` | list of strings | What the listing says about itself. |
| `suggestions` | list | One object per proposed reviewer: `role`, `id`, `provider`, `family`, `paths` (its `when.paths`), `evidence` (list of strings), `matches`, `withheld_matches` (integers). |
| `skipped` | list | One object per role not proposed: `role` and `reason`. |
| `written` | string or null | The project file `--write` appended to, `null` without `--write`. |

### `model list`

Mapping, keyed by provider name, every known provider (one with `--provider`).
The exit code is 1 when an adapter raised.

| Key | Type | Meaning |
| --- | --- | --- |
| `installed` | boolean | Whether its CLI is on PATH. |
| `version` | string or null | The CLI's version, `null` when not installed or not read. |
| `models` | list | The models it advertises, `[]` when not installed: `value` (what is passed to the CLI), `family`, `label`, `source` (`cli-help`, `cli-catalog`, `cli-config`, `cli-default` or `builtin-fallback`). |
| `families` | list | `{"family", "resolves_to"}` per family to put in a config for a CLI whose models are dated ids; `[]` otherwise. |
| `fallback_updated` | string or null | When the adapter's fallback list was last checked; `null` when the adapter raised first. |
| `adapter_error` | string or null | What the adapter raised, `null` when it worked. |
| `origin` | object or null | `{"kind", "path"}`: `kind` `builtin` or `user`, `path` the user module. |

### `reviewer list`

List, one entry per seat of the code panel (`--design`: the design panel),
in panel order.

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | string | The seat's id. |
| `provider` | string | Its provider. |
| `model` | object | Its model as configured: `family`, `version`, and `id` when pinned. |
| `role` | string | Its role. |
| `origin` | string | Where it was written, as `config show`'s `reviewer_origins` names it. |

A seat also carries every other key it is configured with: `when`,
`high_risk_model`, `relevance`, `options`.

### `doctor`

Object. `--strict` exits 1 when `problems` is not empty.

| Key | Type | Meaning |
| --- | --- | --- |
| `platform` | object | `system`, `release`, `python`; `store_python: true` on a Microsoft Store Python. |
| `providers` | object | Keyed by provider name: its detection, `origin`, `read_only_enforcement`, `resume_support`, `live_check`, `adapter_error` and the rest -- see [doctor](cli.md#doctor). |
| `config` | object | `status` (`ok`, `invalid` or `error`), the files in force (`global`, `project_override`), `preset`, `problems`, `warnings`, `loosened`. |
| `roles` | object | Keyed by `orchestrator`, `architect`, `implementer`, `review_fixer`: each seat's provider, model and `status` (`ok`, `missing`, `unknown-provider`, `cli-missing`, `unresolvable-model` or `adapter-error`). |
| `reviewers` | list | The code panel, each seat as a role is, with `id`, `role`, `origin`, and `when`, `condition`, `paths`, `high_risk_model`, `relevance`, `read_only` where they apply. |
| `problems` | list of strings | What is wrong. |
| `notes` | list of strings | What is worth knowing and not wrong; never counted by `--strict`. |
| `user_providers` | object | The user adapter directory: `directory`, `enabled`, `present`, `loaded` (`name`, `path`, `module`), `errors` (`path`, `error`: files that failed to load) and `problems` (`path`, `name`, `problem`: adapters that loaded with something that will not work, such as a `_resolve_latest` not renamed). |
| `antigravity` | object | `root`, `live` (the install locations that are this checkout) and `autoload` (what Antigravity would load from it). |
| `design_panel_source` | string | Where the design panel comes from: `fit`, `code`, `global` or `project`. |
| `design_reviewers` | list | The design panel, diagnosed as `reviewers` is; a seat copied from the code panel carries only `id`, `role` and `origin`. |
| `language` | object | `reply` (a tag or `null`), `rewrite`, `layer` (`project`, `global` or `default`), `check` (`script`, `words`, `latin` or `none`) and `hosts` (`claude`: what `hooks status --json` prints; `codex`, `agy`: `status: not-enforced`). |

A configuration that cannot be read ends the diagnosis early: `config` is
`{"status": "error", "error", "global", "project_override"}`, `roles` and
`reviewers` are empty, and `design_panel_source`, `design_reviewers` and
`language` are not printed.

### `hooks status`

Object. Exit 0 whatever the status.

| Key | Type | Meaning |
| --- | --- | --- |
| `settings_path` | string | The Claude Code settings file looked at. |
| `status` | string | `installed`, `stale`, `not-installed` or `unreadable`. |
| `reasons` | list of strings | Why it is stale, any of: `python-differs`, `python-missing`, `relay-missing`, `relay-outdated`, `plugin-root-differs` (the record names another checkout), `record-differs` (the record is missing or names another config directory or file), `events-missing` (an event has no hook of ours, or more than one), `entries-differ` (one is not in a group of its own as `install` writes it: another matcher, timeout or arguments, or under another event). |
| `python` | string or null | The command the entries run. |
| `relay` | string | Where the relay script is, or would be. |
| `plugin_root` | string or null | The checkout the relay's record names. |
| `events` | list of strings | The events that have a hook of ours. |
| `hooks_disabled` | boolean | Whether the settings set `disableAllHooks`. |
| `error` | string or null | Why the settings file could not be read. |

## Runs and jobs

### `run`

Object: the outcome, after the run, in the keys a finished job record keeps it
under and when it keeps them. With `--detach` it is the job as started
instead (see `jobs show`). With `--print-command` the command line is printed
as text. The exit code is the same as without `--json`.

| Key | Type | Meaning |
| --- | --- | --- |
| `stage` | string | The role run. |
| `status` | string | `succeeded` or `failed`: how the run went. |
| `exit_code` | integer or null | The CLI's exit code. |
| `stalled` | boolean | Whether it was killed for producing no output. |
| `timed_out` | boolean | Whether it was killed at its total deadline. |
| `duration_seconds` | number | How long it ran. |
| `model` | string or null | The model it ran on, as resolved. |
| `session_id` | string or null | The CLI's session id, when it reported one. |
| `output` | string | Only without `--output`: the model's answer (a job keeps it in its `.out` file). |
| `answered` | boolean | `true` only for a `succeeded` run that printed something. |
| `error` | string | Only on a failed run that said something: the start of its stderr. |
| `resume` | object | Only with `--resume`: `requested`, `mode` (`resumed` or `fresh`), `resumed_from`, `reason` (one of the phrases in [cli](cli.md#revising-the-plan-in-the-architects-own-session---resume)), `outcome`, and `trust: "newer"` on a run resumed on trust. |
| `warnings` | list of strings | Only when the adapter had run warnings. |
| `suspended_seconds` | number | Only when the machine slept during the run. |
| `output_written` | boolean | Only when an `--output` write was refused: `false`. |
| `output_target` | string | Only when an `--output` write was refused: the file left as it was. |
| `rejected_file` | string or null | Only when an `--output` write was refused: where what the run printed was kept, `null` when nothing was. |

### `jobs list`

List, one entry per recorded job, newest first: each the job record, as
`jobs show` prints it without `activity`.

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | string | The job id. |
| `stage` | string | The role it runs. |
| `status` | string | `starting`, `running`, `succeeded`, `failed`, `cancelled` or `abandoned`. |
| `started_at` | string | When it was started. |
| `output_file` | string | Where the run's answer is written. |

### `jobs show`

Object: the job record.

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | string | The job id. |
| `stage` | string | The role it runs. |
| `status` | string | As in `jobs list`. |
| `started_at` | string | When it was started. |
| `output_file` | string | Where the run's answer is written. |
| `activity` | object | Only once the job has an activity file: `count`, `context_tokens`, `elapsed_seconds`, `lines` (`n`, `s`, `line`), `dropped`, `omitted` -- see [jobs](cli.md#jobs). |

The record also carries what the job recorded as it went: `command`,
`timeout_seconds`, `prompt_file`, `force`, `pid`, `claimed_at`,
`finished_at`, and, once finished, the outcome keys `run --json` has (less
`output` and `answered`).

### `jobs wait`

Object: the job record, as `jobs show` prints it, once it finished or the wait
ended.

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | string | The job id. |
| `stage` | string | The role it runs. |
| `status` | string | As in `jobs list`. |
| `started_at` | string | When it was started. |
| `output_file` | string | Where the run's answer is written. |
| `waited_out` | boolean | Only when the wait ended while the job still ran: `true` (exit 4). |
| `activity` | object | Only once the job has an activity file, as in `jobs show`. |

## Reviews

### `review snapshot`

Object: the snapshot's metadata, as `reviews/review-target.json` holds it, and
three keys about the size limit.

| Key | Type | Meaning |
| --- | --- | --- |
| `generated_at` | string | When it was taken. |
| `round_id` | string | A new id for every snapshot. |
| `strategy` | string | How the diff was taken, such as `git diff HEAD`. |
| `base` | string or null | The `--base` given, `null` without one. |
| `head` | string | The commit `HEAD` was at. |
| `base_commit` | string | The commit the diff was taken against. |
| `tree` | string | The working tree, as a git tree object. |
| `incremental_from` | string | The tree of the round this one narrows to the fix from; `""` for a full snapshot. |
| `full_diff` | string | The whole change's diff on an incremental round; `""` otherwise. |
| `files` | list of strings | The files a reviewer is shown. |
| `changed_paths` | list of strings | Every path the change touches, withheld files and rename sources included. |
| `condition_paths` | list of strings | The paths a path-scoped reviewer is matched against. |
| `untracked_included` | list of strings | The untracked files in the diff. |
| `withheld` | list | The files left out of the diff: each with its `path`, and the `pattern` that matched or the `reason` it was not read. |
| `exclude_patterns` | list of strings | The `review.exclude` patterns in force. |
| `bytes` | integer | The diff's size in bytes. |
| `lines_added` | integer | Lines added. |
| `lines_deleted` | integer | Lines deleted. |
| `sha256` | string | The diff's sha256. |
| `empty` | boolean | Whether nothing changed (exit 1). |
| `change_chars` | integer | The size `review.context.max_chars` measures. |
| `max_chars` | integer | `review.context.max_chars`. |
| `over_context` | boolean | Whether `change_chars` is over it. |
| `surrounding` | object | Only when the enclosing symbols were frozen: what was frozen and what was not extracted. |

### `review run`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `ok` | integer | Reviewers that came back `ok`. |
| `failed` | integer | Reviewers that failed. |
| `partial` | integer | Reviewers handed the change as a file. |
| `reviewers` | list | One entry per reviewer that ran: `id`, `provider`, `role`, `model`, `status` (`ok`, `partial`, `failed`, `stalled` or `unparsed`), `error`, `duration_seconds`, `findings`, `report`, `delivery` (`inline` or `file`), `change_chars`, `inline_chars`, `snapshot`, `over_budget`, `budget_chars`, `usage`, and `model_slot`, `warnings` where they apply. |
| `counts` | object | The consolidated report's counts, as `review show` has them. |
| `optimization` | object | What the round decided. Code: `requested_level`, `level` (`aggressive`, `balanced` or `quality`), `escalated`, `high_risk`, `gate` (`allow`, `warn` or `refuse`), `test_status`, `max_findings`, `reviewer_limit`, `files`, `lines`, `conditional`, `declared`. `--design`: `level`, `high_risk`, `declared`, `conditional`, `files`. |
| `plan` | string | Only with `--design`: the plan reviewed. |
| `surrounding` | object | Only on a code round with `review.context.surrounding: enclosing` in force: the context adopted and left out. |
| `measurement` | object | Only with `--surrounding`: `surrounding`, `snapshot`, `tree`, `head`, `base`, `workflow`, `epoch`, `rerun`, `inputs`. |

A refused round (exit 3) prints nothing on stdout.

### `review consolidate`

Object: the consolidated report, as `consolidated.json` holds it.

| Key | Type | Meaning |
| --- | --- | --- |
| `generated_at` | string | When it was built. |
| `iteration` | integer | The round. |
| `lineage` | string | The workflow, branch and base the round count is kept for. |
| `snapshot` | object | What was reviewed: `sha256`, `files`, `round_id`, `over_budget`, `budget_chars`. |
| `coverage` | object | `round` and `change`, each `complete`, `unverified` or `none`; `unverified_since`, `change_chars`, `inline_chars` -- see [Coverage](reviews.md#coverage). |
| `reviewers` | list | Every reviewer's entry, as in `review run`. |
| `counts` | object | `findings_total`, `by_severity`, `duplicates_merged`, `duplicate_candidates`, `reviewers_total`, `reviewers_ok`, `reviewers_partial`, `reviewers_failed`, and the same four with a `snapshot_` prefix. |
| `duplicate_candidates` | list | Pairs of findings that may be the same. |
| `findings` | list | One entry per finding: `id`, `key`, `severity` (`critical`, `high`, `medium` or `low`), `file`, `line`, `category`, `problem`, `impact`, `evidence`, `recommended_fix`, `reported_by`, `duplicate_count`, `triage` (`needs-triage`, `accepted`, `rejected`, `duplicate` or `needs-investigation`), `triage_note`, and `triage_set_at` once triaged. |
| `surrounding` | object | Only when the round carried surrounding context. |
| `measurement` | object | Only after a `review run --surrounding`: with `triage_at_build`. |

### `review show`

Object: the consolidated report, as for `review consolidate`; with
`--accepted`, `findings` holds the accepted ones only.

| Key | Type | Meaning |
| --- | --- | --- |
| `generated_at` | string | When it was built. |
| `iteration` | integer | The round. |
| `lineage` | string | The workflow, branch and base the round count is kept for. |
| `snapshot` | object | As in `review consolidate`. |
| `coverage` | object | As in `review consolidate`. |
| `reviewers` | list | As in `review consolidate`. |
| `counts` | object | As in `review consolidate`. |
| `duplicate_candidates` | list | As in `review consolidate`. |
| `findings` | list | As in `review consolidate`. |
| `surrounding` | object | Only when the round carried surrounding context. |
| `measurement` | object | Only after a `review run --surrounding`. |

### `review status`

Object. With `--design` the round budget is `max_iterations` and the last
pass `final_revision`, in place of the two code keys named below.

| Key | Type | Meaning |
| --- | --- | --- |
| `iteration` | integer | The round. |
| `max_review_iterations` | integer | The round budget. With `--design`: `max_iterations`. |
| `blocking` | list of strings | The ids of the unresolved blocking findings. |
| `blocking_count` | integer | How many. |
| `accepted_count` | integer | How many findings are accepted. |
| `re_review_recommended` | boolean | Whether another round is warranted and within budget. |
| `iteration_budget_exhausted` | boolean | Whether the round budget is spent. |
| `coverage` | object or null | As in `review consolidate`; `null` for a report without one. |
| `reviewers_partial` | integer | Reviewers of this snapshot handed the change as a file. |
| `over_budget` | boolean | Whether the round ran only because `--force` sent it past `review.context.max_chars`. |
| `counts` | object | As in `review consolidate`. |
| `final_fix` | string or null | Where the last round's fix stands once the budget is spent: `pending`, `retest`, `done`, `blocked` or `unaccepted`; `null` before. With `--design`: `final_revision` -- `pending`, `done`, `blocked`, `approved`, `implemented` or `unaccepted`. |
| `final_fix_pending` | boolean | Whether that pass is still owed. With `--design`: `final_revision_pending`. |
| `surrounding` | object | Only when the round carried surrounding context. |
| `enabled` | boolean | Only with `--design`: whether the stage runs. |
| `mode` | string | Only with `--design`: `on`, `off` or `auto`. |
| `reason` | string or null | Only with `--design`: why `auto` decided as it did; `null` under `on` and `off`. |
| `reviewers` | list of strings | Only with `--design`: the design panel's ids. |
| `panel_source` | string | Only with `--design`: `fit`, `code`, `global` or `project`. |
| `optimization` | object | Only with `--design`: what the next design round would decide. |

### `design approve`

Object: the approval, as `state.json` holds it under `design_approval`.

| Key | Type | Meaning |
| --- | --- | --- |
| `sha256` | string | The plan's sha256. |
| `plan` | string | The plan, relative to the repository. |
| `approved_at` | string | When it was approved. |
| `open_findings` | list of strings | The ids of the design findings still open. |
| `open_findings_of_current_plan` | boolean or null | Whether those come from a review of this plan text; `null` with none open. |
| `design_round` | string or null | The design round it was given over; `null` with none. |
| `already_approved` | boolean | Whether this plan was already approved over this round, so nothing was recorded. |
| `workflow` | string | The workflow. |

## Workflow state

### `status`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `verdict` | string | `continue` or `stop-and-report`. |
| `reasons` | list of strings | Why it is `stop-and-report`. |
| `stalls` | list | In-flight stages that look stuck or dead: `token`, `stage`, `started_at`, `elapsed_seconds`, `deadline_seconds`, `pid`, `process_alive`, `reason`. |
| `abandoned_stages` | list of strings | The stages this call found dead and marked `abandoned`. |
| `in_flight` | object | What is running now, keyed by token. |
| `review` | object | `iteration`, `max_review_iterations`, `blocking`, `accepted`, `refused_for_size`, `identical_rounds`, `final_fix`, `final_fix_pending` (values as in `review status`). |
| `design_review` | object | `enabled`, `mode`, `reason`, `iteration`, `max_iterations`, `blocking`, `accepted`, `refused_for_size`, `identical_rounds`, `final_revision`, `final_revision_pending`, `optimization` (values as in `review status --design`). |
| `design_approval` | object | `required`, `state` (`pending`, `stale`, `approved`, `no-plan`, `not-required` or `implemented-unapproved`), `pending`, `stale_reason` (`plan-changed`, `reviewed-since` or `null`) and the rest -- see [Approval](workflow.md#approval). |
| `budgets` | object | Per stage: `used`, `limit`, `remaining`. |
| `total_delegated_runs` | object | `used` and `limit`. |
| `runtime_remaining_seconds` | integer or null | The delegated runtime left, rounded up; `null` without a limit. |
| `runtime` | object | `used`, `limit`, `remaining`, `suspended`. |
| `tokens` | object | What `tokens show --json` prints. |
| `optimization` | object | What the next `review run` would decide, as in `review run`. |
| `workflow` | string | The workflow. |
| `also_active` | list of strings | Other workflows that look live in this working tree. |

### `state show`

Object: `state.json` as it is.

| Key | Type | Meaning |
| --- | --- | --- |
| `version` | integer | `1`. |
| `runs` | list | Kept for the file's format; nothing writes to it. |
| `ledger` | object | The budget ledger: `epoch`, `started_at`, `attempts`, `in_flight`, `signatures`, `total_delegated_runs`, `runtime_seconds`, `tokens` and the rest. |
| `updated_at` | string | When the file was last written. |
| `events` | list | Every recorded stage event, oldest first: `stage`, `status`, `at`, and what the stage recorded besides. |
| `design_approval` | object | Only once a plan was approved: as `design approve --json` prints it, less `already_approved` and `workflow`. |

### `budget show`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `started_at` | string | When the budgets started. |
| `budgets` | object | Per stage: `used`, `limit`, `remaining`. |
| `total_delegated_runs` | object | `used` and `limit`. |
| `runtime_remaining_seconds` | integer or null | The delegated runtime left, rounded up; `null` without a limit. |
| `runtime` | object | `used`, `limit`, `remaining`, `suspended`. |
| `signatures` | object | Per stage: how many times in a row it produced the same outcome. |
| `in_flight` | object | What is running now, keyed by token. |
| `stalls` | list | As in `status`. |
| `tokens` | object | What `tokens show --json` prints. |

### `tokens show`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `by_stage` | object | The account per stage. |
| `by_label` | object | The account per label: a reviewer id, `architect:resumed`, a tier label. |
| `totals` | object | The sums: `runs`, `measured_runs`, `priced_runs`, `tool_reported_runs`, `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_tokens`, `cache_write_tokens`, `billed_tokens`, `cost_usd`, `prompt_chars`, `tool_uses`, `tool_output_chars`, `tool_uses_by_name`. |
| `complete` | boolean | Whether every run reported its tokens; `false` makes the sums a floor. |
| `priced` | boolean | Whether every run reported a cost. |

### `optimization report`

Object. The keys below `by_context` are described in [optimization](cli.md#optimization).

| Key | Type | Meaning |
| --- | --- | --- |
| `rounds` | integer | Code review rounds recorded. |
| `ran` | integer | Those that ran. |
| `refused` | integer | Those refused. |
| `refused_by` | object | Refusals by what refused them: `gate`, `context`. |
| `levels` | object | Rounds per level in force. |
| `gates` | object | Rounds per gate verdict: `allow`, `warn`, `refuse`. |
| `escalated` | integer | Rounds escalated by a high-risk path. |
| `escalation_patterns` | object | Escalations per pattern. |
| `always_escalated` | boolean | Whether every round escalated. |
| `panel_reduced` | integer | Rounds whose panel was cut. |
| `conditional` | object | `added`, `left_out`, `declared_rounds`. |
| `relevance` | object | Roles a code round had nothing for: `judged`, `added`, `left_out`, `left_out_by_level`, `estimated_saving`. |
| `design_relevance` | object | The same for design rounds. |
| `rounds_without_a_test_result` | integer | Rounds with no test result recorded for the gate. |
| `reviewer_runs` | integer | Code review reviewer runs. |
| `measured_runs` | integer | Those that reported usage. |
| `billed_tokens` | integer | What they billed. |
| `billed_per_round` | number or null | Per round that ran. |
| `estimated_saving` | number | The estimate for the refused rounds. |
| `design_rounds` | integer | Design rounds that ran. |
| `design_refused` | integer | Design rounds refused. |
| `design_reviewer_runs` | integer | Design reviewer runs. |
| `design_measured_runs` | integer | Those that reported usage. |
| `design_billed_tokens` | integer | What they billed. |
| `design_billed_per_round` | number or null | Per design round that ran. |
| `tool_reported_runs` | integer | Code runs that reported tool use. |
| `tool_output_reported_runs` | integer | Code runs that reported tool output. |
| `tool_uses` | integer | Their tool calls. |
| `tool_output_chars` | integer | Their observed tool output. |
| `tool_uses_per_run` | number or null | Per reporting run. |
| `tool_output_chars_per_run` | number or null | Per run that reported output. |
| `design_tool_reported_runs` | integer | `tool_reported_runs`, for design runs. |
| `design_tool_output_reported_runs` | integer | `tool_output_reported_runs`, for design runs. |
| `design_tool_uses` | integer | `tool_uses`, for design runs. |
| `design_tool_output_chars` | integer | `tool_output_chars`, for design runs. |
| `design_tool_uses_per_run` | number or null | `tool_uses_per_run`, for design runs. |
| `design_tool_output_chars_per_run` | number or null | `tool_output_chars_per_run`, for design runs. |
| `by_context` | object | Code rounds `with` and `without` surrounding context. |
| `paired` | object | Runs of one snapshot with and without it: `pairs`, `pairs_total`, `pairs_listed`, `with`, `without`, `delta`, `note`. |
| `scorecard` | object | `code`, `design` and `total`: what each reviewer reported, what was decided, what it cost. |
| `architect_revisions` | object | `attempts`, `resumed`, `fresh`, `fallbacks`. |

### `progress record`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `stage` | string | The stage recorded. |
| `repeats` | integer | How many times in a row it produced this signature. |
| `stop` | boolean | Whether that reached the limit, so the loop should stop. |

### `workflow list`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `current` | string | The workflow this command resolved to. |
| `workflows` | list | Every workflow here, most recently active first: `workflow`, `dir`, `started_at`, `updated_at`, `last_activity_monotonic`, `last_stage`, `last_status`, `in_flight` (list of stages), `runs`. |

### `workflow show`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `workflow` | string | The workflow id. |
| `origin` | string | Which rule chose it: `requested` (`--workflow`), `environment` (`DEV_ORCHESTRA_WORKFLOW`), `session` (the host's session id), `pointer` (`current.json`) or `new`. |
| `dir` | string | Its artifacts directory. |
| `container` | string | The `.ai/` directory. |
| `root` | string | The repository root. |
| `also_active` | list of strings | Other workflows that look live in this working tree. |

### `summary`

Object.

| Key | Type | Meaning |
| --- | --- | --- |
| `stages` | object | The last status recorded for each stage. |
| `counts` | object | The code review's counts, as in `review consolidate`; `{}` without a report. |
| `design_counts` | object | `reviewers_ok` and `reviewers_total` of the design review; `{}` without a design report. |
| `tokens` | object | What `tokens show --json` prints. |
| `models` | object | Per role: `provider`, `family`, `version`. |
| `reviewers` | list | `id`, `provider`, `model` (as the text prints it), `status`, in the text's order. |
| `skipped` | object | `refused`, `refused_by`, `design_refused`, `panel_reduced`, named as `optimization report --json` names them. |
