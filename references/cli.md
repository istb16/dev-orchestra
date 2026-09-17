# CLI reference

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
to run, `4` a `jobs wait` returned while the job was still running, `130`
interrupted.

## config

| Command | Description |
| --- | --- |
| `config show [--scope effective\|global\|project] [--json]` | Show the configuration. Default `effective` (merged). |
| `config path` | Print both layer locations. |
| `config setup [--scope global\|project] [--defaults] [--force]` | Setup wizard. `--defaults` writes the recommended config without prompting. `--force` prompts even without a TTY. |
| `config reset [--scope …] [--delete]` | Restore recommended defaults, or delete the file. |
| `config set <path> <value> [--scope …] [--raw]` | Set one value. Paths support `a.b.c` and `reviewers[0].role`. |
| `config validate [--json]` | Validate the effective configuration. Exit 1 if invalid. |

```bash
dev-orchestra config set implementer.model.family opus
dev-orchestra config set review.max_review_iterations 3
dev-orchestra config set --scope project workspace.dir .agent-work
dev-orchestra config set --raw review.note "3 reviewers"
```

## model

| Command | Description |
| --- | --- |
| `model list [--provider <name>] [--json]` | Models the installed CLIs advertise, with the discovery source for each (`cli-help`, `cli-catalog`, `cli-config`, `cli-default`, `builtin-fallback`). |

## reviewer

| Command | Description |
| --- | --- |
| `reviewer list [--json]` | List the configured panel. |
| `reviewer add --provider <p> [--model <family>] [--role <r>] [--id <id>] [--pin <model-id>] [--scope …]` | Add a reviewer. The id is generated (`codex-security`, `codex-security-2`, …) when omitted. |
| `reviewer remove <id\|role\|position> [--scope …]` | Remove by id, by unique role, or by 1-based position. |
| `reviewer set <selector> [--provider] [--model] [--role] [--id] [--pin] [--scope …]` | Change an existing reviewer. |

```bash
dev-orchestra reviewer add --provider codex --role security
dev-orchestra reviewer add --provider claude --role database --id db-review
dev-orchestra reviewer set 2 --role performance
dev-orchestra reviewer remove db-review
```

## doctor

| Command | Description |
| --- | --- |
| `doctor [--json] [--fast] [--strict]` | Diagnose CLIs, authentication presence, configuration, and whether each role's model resolves. `--fast` skips model discovery. `--strict` exits 1 when problems are found. |

Never prints credential values -- only whether credentials appear to be present.

It also lists any setting fixed at a value the built-in default has since moved
off. `config setup --defaults` writes every default into the file, so improving
a default never reaches a configuration that already recorded the old one --
which is how the low-risk thresholds raised in 0.4.2 failed to reach anyone who
had run setup before it. Reported, never rewritten: a deliberate choice and an
inherited default are the same characters on disk.

## run

| Command | Description |
| --- | --- |
| `run <role> [--prompt <text>\|--prompt-file <path>] [--tier <name>] [--mode plan\|implement\|review] [--output <path>] [--timeout <s>] [--idle-timeout <s>] [--detach] [--force] [--json] [--print-command] [--extra …]` | Run one configured role. `<role>` is `orchestrator`, `architect`, `implementer`, `review_fixer`, or a reviewer id. `--tier` picks one of that role's configured `model_tiers`; an unknown one is refused rather than run on the default model. Consumes an attempt from that stage's budget and refuses (exit 3) when it is spent, unless `--force`. |

The prompt may also be piped on stdin (`--prompt-file -` reads stdin
explicitly). Default modes: architect/orchestrator `plan`, implementer and
review_fixer `implement`, reviewers `review`. `--print-command` shows the exact
CLI invocation without running it. `--extra` forwards every remaining argument
to the provider CLI verbatim.

`--timeout` is the total deadline. `--idle-timeout` is the *no output* deadline:
a wedged agent goes quiet while a slow one keeps producing, so this catches a
stall in minutes rather than at the total deadline. It only applies to providers
that stream progress (both adapters do; see `references/providers.md`), and is
ignored elsewhere rather than guessed at.

`--detach` starts the run in its own process and returns a job id immediately,
so the call cannot block. See `jobs` below.

```bash
dev-orchestra run architect --prompt-file .ai/execution/design-request.md --output .ai/plan.md
dev-orchestra run implementer --print-command
echo "explain the failure" | dev-orchestra run orchestrator
```

## review

| Command | Description |
| --- | --- |
| `review snapshot [--base <rev>] [--no-untracked] [--json]` | Freeze the change under review. Exit 1 if empty. |
| `review run [--design] [--request <path>] [--iteration N] [--only <ids/roles>] [--sequential] [--context <text>] [--base <rev>] [--timeout <s>] [--idle-timeout <s>] [--force] [--json]` | Run every reviewer against the snapshot; write reports and the consolidated result. Exit 1 only if every reviewer failed. The round is derived from the snapshot unless `--iteration` is given, and a round past `review.max_review_iterations` is refused (exit 3) unless `--force`. A round refused by the optimization gate (tests recorded as failing) also exits 3, and is recorded as `refused` so `optimization report` can count it. `--only` runs a subset but still consolidates every reviewer's current report, so nothing is lost. |
| `review consolidate [--design] [--iteration N] [--json]` | Re-parse the existing reports and rebuild the consolidated result. |
| `review show [--design] [--accepted] [--json]` | Show the consolidated review. |
| `review triage [--design] <ids…> --status <status> [--note <text>]` | Record triage decisions. |
| `review fix-brief [--design] [--output <path>]` | Emit the accepted-findings brief for the fixer. |
| `review status [--design] [--json]` | Whether a re-review is warranted, and the iteration budget. |

`--design` switches every one of those to the *design* review: `.ai/plan.md`
judged by the same panel before implementation, with its own reports, round
counter and triage under `.ai/reviews/design/`. `review run --design` freezes
the plan itself instead of a diff — there is no `review snapshot --design`,
and no git is needed — and hashes it with the request it answers
(`--request <path>`, default `.ai/execution/design-request.md`; a missing one
is noted, not fatal). No plan exits 2, a round past
`review.design.max_iterations` exits 3 unless `--force`, and every reviewer
failing exits 1. The optimization gate and panel reduction do not apply, and
`--base` is ignored. Running it while `review.design.enabled` is false prints a
note and proceeds: the setting says whether the orchestrator runs the stage,
not whether you may. See `references/reviews.md`.

`review status --json` reports the budget under the name of the setting it came
from: `max_review_iterations` without `--design`, `max_iterations` with it. The
rest of the payload is the same either way.

## status

| Command | Description |
| --- | --- |
| `status [--json]` | The one command that answers *continue or stop*. Reports a `continue` / `stop-and-report` verdict with reasons, stalled or abandoned stages, what is in flight, remaining budgets, and the open review findings. |

Reading it also clears in-flight entries whose process is gone, so a stage that
died without recording an outcome shows up as `abandoned` instead of appearing
to run forever.

```bash
dev-orchestra status --json
```

```json
{
  "verdict": "stop-and-report",
  "reasons": ["review budget spent (2/2 rounds) with 1 finding(s) still open"],
  "stalls": [],
  "design_review": {"enabled": false, "iteration": 0, "max_iterations": 2, "blocking": [], "accepted": 0},
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
| `jobs wait <id> [--timeout <s>] [--poll <s>] [--json]` | Wait, but never longer than `--timeout` (60s default). Exits 4 if the job was still running when the wait ended — a normal outcome, not an error. |
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
| `budget show [--json]` | Attempts spent per stage, delegated-run total, runtime left. |
| `budget consume <stage> [--force]` | Claim an attempt at a stage the orchestrator runs itself (notably `test`). Exits 3 when the budget is spent. |
| `budget reset` | Start a fresh workflow, including the review round counter. Also happens automatically once a ledger has been idle for `budgets.session_idle_reset_seconds`. |

`run` and `review run` consume their own budgets, so `budget consume` is only
needed for stages the orchestrator performs directly.

## tokens

| Command | Description |
| --- | --- |
| `tokens show [--json]` | What the workflow has spent, per stage and per reviewer. |

Accounting, not a budget: nothing here refuses a run. Deliberately separate from
`budget`, which is enforced -- reading one as the other is the mistake this
split exists to prevent.

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

```bash
dev-orchestra tokens show
dev-orchestra tokens show --json
```

## optimization

| Command | Description |
| --- | --- |
| `optimization report [--json]` | What `optimization.level` has decided, over every review round this project has recorded, and which high-risk patterns escalated it. |

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
| `state record <stage> <status> [--detail k=v …]` | Append a stage outcome (for stages not run through `run`). `state record test ok\|failed` is what the review gate reads. |
| `summary [--json]` | The end-of-run stage + model summary. |

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
