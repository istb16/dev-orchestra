# CLI reference

```
python scripts/dev_orchestra.py <command> [options]
bin/dev-orchestra <command> [options]           # POSIX wrapper
bin\dev-orchestra.ps1 <command> [options]       # Windows wrapper
```

Global options: `--cwd <dir>` (operate as if run from there), `--version`.

Exit codes: `0` success, `1` the operation ran but the outcome is negative
(invalid config, empty snapshot, every reviewer failed, role run failed), `2` a
usage or configuration error, `130` interrupted.

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
| `model list [--provider <name>] [--json]` | Models the installed CLIs advertise, with the discovery source for each (`cli-help`, `cli-config`, `cli-default`, `builtin-fallback`). |

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
| `run <role> [--prompt <text>\|--prompt-file <path>] [--mode plan\|implement\|review] [--output <path>] [--timeout <s>] [--print-command] [--extra …]` | Run one configured role. `<role>` is `orchestrator`, `architect`, `implementer`, `review_fixer`, or a reviewer id. |

The prompt may also be piped on stdin (`--prompt-file -` reads stdin
explicitly). Default modes: architect/orchestrator `plan`, implementer and
review_fixer `implement`, reviewers `review`. `--print-command` shows the exact
CLI invocation without running it. `--extra` forwards every remaining argument
to the provider CLI verbatim.

```bash
dev-orchestra run architect --prompt-file .ai/execution/design-request.md --output .ai/plan.md
dev-orchestra run implementer --print-command
echo "explain the failure" | dev-orchestra run orchestrator
```

## review

| Command | Description |
| --- | --- |
| `review snapshot [--base <rev>] [--no-untracked] [--json]` | Freeze the change under review. Exit 1 if empty. |
| `review run [--iteration N] [--only <ids/roles>] [--sequential] [--context <text>] [--base <rev>] [--timeout <s>] [--json]` | Run every reviewer against the snapshot; write reports and the consolidated result. Exit 1 only if every reviewer failed. |
| `review consolidate [--iteration N] [--json]` | Re-parse the existing reports and rebuild the consolidated result. |
| `review show [--accepted] [--json]` | Show the consolidated review. |
| `review triage <ids…> --status <status> [--note <text>]` | Record triage decisions. |
| `review fix-brief [--output <path>]` | Emit the accepted-findings brief for the fixer. |
| `review status [--json]` | Whether a re-review is warranted, and the iteration budget. |

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
