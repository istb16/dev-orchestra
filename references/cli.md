# CLI reference

```
python scripts/dev_orchestra.py <command> [options]
bin/dev-orchestra <command> [options]           # POSIX wrapper
bin\dev-orchestra.ps1 <command> [options]       # Windows wrapper
```

Global options: `--cwd <dir>` (operate as if run from there), `--version`.

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

Never prints credential values — only whether credentials appear to be present.

## run

| Command | Description |
| --- | --- |
| `run <role> [--prompt <text>\|--prompt-file <path>] [--mode plan\|implement\|review] [--output <path>] [--timeout <s>] [--idle-timeout <s>] [--detach] [--force] [--json] [--print-command] [--extra …]` | Run one configured role. `<role>` is `orchestrator`, `architect`, `implementer`, `review_fixer`, or a reviewer id. Consumes an attempt from that stage's budget and refuses (exit 3) when it is spent, unless `--force`. |

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
| `review run [--iteration N] [--only <ids/roles>] [--sequential] [--context <text>] [--base <rev>] [--timeout <s>] [--idle-timeout <s>] [--force] [--json]` | Run every reviewer against the snapshot; write reports and the consolidated result. Exit 1 only if every reviewer failed. The round is derived from the snapshot unless `--iteration` is given, and a round past `review.max_review_iterations` is refused (exit 3) unless `--force`. `--only` runs a subset but still consolidates every reviewer's current report, so nothing is lost. |
| `review consolidate [--iteration N] [--json]` | Re-parse the existing reports and rebuild the consolidated result. |
| `review show [--accepted] [--json]` | Show the consolidated review. |
| `review triage <ids…> --status <status> [--note <text>]` | Record triage decisions. |
| `review fix-brief [--output <path>]` | Emit the accepted-findings brief for the fixer. |
| `review status [--json]` | Whether a re-review is warranted, and the iteration budget. |

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
| `budget reset` | Start a fresh workflow. Also happens automatically once a ledger has been idle for `budgets.session_idle_reset_seconds`. |

`run` and `review run` consume their own budgets, so `budget consume` is only
needed for stages the orchestrator performs directly.

## progress

| Command | Description |
| --- | --- |
| `progress record <stage> --signature <text> [--json]` | Record what a stage produced. Identical consecutive signatures mean the last attempt changed nothing, and the command says to stop. |

```bash
dev-orchestra progress record test --signature "3 failed: test_totals, test_discount, test_coupon"
```

Reviews register their own signature automatically, from the set of open
findings.

## state / summary

| Command | Description |
| --- | --- |
| `state show [--json]` | The recorded stage events for this project. |
| `state record <stage> <status> [--detail k=v …]` | Append a stage outcome (for stages not run through `run`). |
| `summary [--json]` | The end-of-run stage + model summary. |

## Environment variables

| Variable | Effect |
| --- | --- |
| `DEV_ORCHESTRA_CONFIG` | Use this exact file as the global config layer |
| `DEV_ORCHESTRA_HOME` | Use this directory instead of the platform config directory |
| `DEV_ORCHESTRA_MOCK_DIR` | Canned responses for the mock provider |
| `DEV_ORCHESTRA_MOCK_RESPONSE` | Inline canned response for the mock provider |
| `DEV_ORCHESTRA_MOCK_FAIL` | Make mock runs fail (`1` = all, otherwise a prompt substring) |
| `CODEX_HOME` | Respected when locating the Codex CLI's config and credentials |
| `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI` | Test-only: hides both provider CLIs, reproducing CI |
