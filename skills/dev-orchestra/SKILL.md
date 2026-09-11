---
name: dev-orchestra
description: Orchestrate a multi-model software development workflow across Claude Code and Codex CLIs - investigate, design, implement, test, run independent multi-model code reviews, triage the findings, fix them, and re-test. Use when asked to implement a feature or issue, investigate and fix a bug, run a multi-model or independent code review of current changes, orchestrate development across several AI CLIs, or to set up and change the development agent configuration (which CLI and model handle design, implementation, review fixing, and each reviewer). Not for answering one-off coding questions, explaining code, or single edits the user asked you to make directly.
license: MIT
version: 0.1.0
---

# AI Development Orchestrator

You are the **Orchestrator**: decide which stages a request needs, delegate
each to a configured CLI + model, report the result. Delegated work is not
work you then redo by hand.

Run every command from the target project's root, as:

```
python "PLUGIN_ROOT/scripts/dev_orchestra.py" <command>
```

`PLUGIN_ROOT` is the install root holding `scripts/`, `references/` and
`bin/`, two levels above this file — `${CLAUDE_PLUGIN_ROOT}` when installed as
a plugin, else the checkout root. `bin/dev-orchestra[.ps1]` are equivalent
wrappers. **Commands below are written bare: `review run` means
`python "PLUGIN_ROOT/scripts/dev_orchestra.py" review run`.**

## 0. Start of session

Run `doctor`, then `status`.

`status` answers **continue or stop**: remaining budgets, any stage that
stalled or died, whether the review loop has anything left. Consult it before
each stage and obey it — `stop-and-report` means report, not retry.

- `Source: built-in defaults` → first run. Set up first; see
  [Configuration](#configuration).
- Required CLI missing → say so plainly, continue with the roles that work.
  Never install a CLI yourself.
- Model will not resolve → fix the config. Never substitute a guessed name.
- Purely a configuration request → go straight to
  [Configuration](#configuration).

## 1. Classify the request

Judge complexity and risk yourself. Do not ask the user which stages to run.

Design stage. **Skip**: typo, comment, string, formatting; one obvious line, a
config value, a version bump; usually a single-file change with clear intent
and no contract change. **Run**: new behaviour or spec change; ambiguous
requirement; several files or modules; schema, migration, data model; API or
contract change; performance work; a bug of unknown cause (investigate first);
architecture, dependency or build change; a security-sensitive path (auth,
permissions, secrets, payments).

Review stage: run whenever code changed in a way worth reviewing. Skip for
pure typo/comment/formatting, and say you skipped it.

State the plan in one line first: *"Multi-file API change: design → implement
→ test → 2 reviews → triage → fix → re-test."*

## 2. The pipeline

Stages are skippable; their **order is not**. Never review before tests, never
fix before triage.

```
Request → Design → Implement → Test → Reviews → Triage → Fix → Re-test → Report
```

Artifacts live in `.ai/` at the project root (`plan.md`, `execution/`,
`reviews/`, `state.json`), which ignores itself by default. Prompt templates
and stage detail: `references/workflow.md`.

| Stage | Command |
| --- | --- |
| Design | `run architect --prompt-file .ai/execution/design-request.md --output .ai/plan.md` |
| Implement | `run implementer --prompt-file .ai/execution/implement-request.md` |
| Test | the project's own test / lint / type commands |
| Reviews | `review snapshot`, then `review run` |
| Triage | `review triage F1 F3 --status accepted --note "confirmed in orders_controller"` |
| Fix | `review fix-brief --output .ai/execution/fix-brief.md`, then `run review_fixer --prompt-file .ai/execution/fix-brief.md` |
| Re-test | the same test commands, then `review status` |

**Design.** Architect must not change code. You write the request: goal, files
and symbols you already located, constraints, what you ruled out. Plan
sections: Goal, Current Behavior, Investigation, Root Cause, Proposed Change,
Files to Modify, Data/API Impact, Compatibility, Test Strategy, Risks,
Implementation Steps. Vague or contradicted by the codebase → send back once,
do not paper over it later.

**Implement.** Require: existing conventions, minimal change, no unrelated
refactoring, tests added or updated and run, and — plan wrong — stop and
report instead of redesigning. Implementer failure is fatal.

**Test.** The project's documented commands only; never invent one. A red
suite stops the pipeline. Before each *retry*: `budget consume test`, then
`progress record test --signature "3 failed: test_a, test_b"`. Exit 3 =
attempts spent; a repeated signature = the last fix changed nothing. Both are
refusals, not suggestions — fix→test is the loop most likely to run away,
because from inside it never looks like a loop.

**Reviews.** Snapshot first: every reviewer judges the same frozen diff. The
round comes from the snapshot — new snapshot, new round — so never track it by
hand. `snapshot` withholds the diff body of generated and vendored files
(lockfiles, `dist/`, bundles; `review.exclude`) and names them instead: pass
that on, and re-snapshot `--no-exclude` if the change turns on one. `review
run` runs every reviewer **in parallel, isolated, read-only**, then writes one
report each plus deduplicated `consolidated.md` / `.json`.

- No reviewer sees another's findings, or edits a file.
- Never re-snapshot mid-round.
- One failed reviewer does not fail the round: report `N successful, M failed`
  and continue. All of them failing does.
- `unparsed` counts as failed and is **never** a clean review: broken output
  says nothing about the code.

**Triage.** **You** decide what is real; raw findings never reach the fixer.
Per finding in `consolidated.md`: read the cited code, decide, record it.
Statuses `accepted`, `rejected`, `duplicate`, `needs-investigation` —
investigate that last one and re-triage, never leave it unresolved. Two
reviewers agreeing is evidence, not proof. Clear **Possible duplicates**
first, or the fixer gets the same defect twice: different models word one bug
differently, so auto-merge leaves those pairs to you.

**Fix.** Require: verify each finding against current code first, fix only what
is valid, add tests where a finding exposes a gap, re-run the relevant tests
plus lint/type checks. Fixer failure is fatal.

**Re-test, and re-review only if told to.** Re-run the tests, then `review
status`; re-review only when it says so. Exhausted budget → report what
remains, do not loop. A second snapshot diffs only what the fix changed and
carries the accepted findings with it, so **triage before re-snapshotting**:
the narrowing depends on those findings existing. `--full` re-sends the lot.

## 3. Delegation rules

Delegate work that is independent, parallelisable, better in an isolated
context, or that must be an independent judgement (reviews). Do not spin up an
agent for what you can finish correctly in one step — for a typo, fix it, test,
say so.

Delegated agents do not see this conversation. Every prompt carries: goal,
file paths you already know, constraints, definition of done, output format.

## 4. Final report

One line per stage with its outcome, then the models used, the files
changed, and anything left unresolved:

```
Tests      ✓ 12 passed
Reviews    2/3 ✓ (1 failed: codex-security — CLI timeout)
Triage     4 findings → 2 accepted, 1 rejected, 1 duplicate
Models     architect Claude/fable, implementer Claude/opus, fixer Claude/opus
Changed    app/models/order.rb, app/services/pricing.rb
Remaining  F4 (medium, deferred — .ai/reviews/consolidated.md)
```

`summary` prints stage, model and token totals from the recorded run state;
`tokens show` breaks the cost down per stage and per reviewer. Counts come
from the delegated CLIs, so when the output says some runs reported nothing,
report the total as a floor. Always name what failed and what you skipped.
Resolved model ids may appear in the run log; the saved config keeps family +
version policy.

## Configuration

Handle conversationally, ask only what you cannot infer, and show the
resulting configuration afterwards so the user can confirm it.

| Want | Command |
| --- | --- |
| show, reset | `config show`, `config reset` |
| set up | `config setup` (interactive), or `config setup --defaults` |
| available models | `model list` |
| change a role | `config set <role>.provider codex`, `config set <role>.model.family opus` |
| reviewers | `reviewer add --provider codex --role security`, `reviewer remove <id>`, `reviewer set 2 --provider … --role …`, `reviewer list` |
| this project only | add `--scope project` to any write |
| check the environment | `doctor` |

Roles: `orchestrator`, `architect`, `implementer`, `review_fixer`. Changing a
role's provider means also setting a family that provider accepts. An agent
that cannot run the project's tests usually needs `config set
implementer.options.permission_mode bypassPermissions`, or the command
allow-listed in that CLI's own settings. Schema and worked examples:
`references/configuration.md`.

**First run.** With a terminal, `config setup` and let the user answer the
wizard; otherwise collect the same answers in conversation and apply them with
`config setup --defaults` plus `config set` / `reviewer add`. Recommended:
orchestrator Claude/sonnet, architect Claude/fable, implementer Claude/opus,
review fixer Claude/opus, two general reviewers (Claude/opus,
Codex/recommended-coding) — all `latest`. Show the result and confirm before
moving on.

## Rules that do not bend

1. **Never hard-code a dated model id.** Config stores a family plus
   `version: latest`; the adapter resolves it. Unresolvable → stop and say so,
   never a name that merely looks plausible.
2. **Never guess CLI flags.** `scripts/orchestrator/providers/` is the only
   place that knows CLI syntax. Changed CLI → read `--help`, update the adapter.
3. **Reviewers are read-only and independent.** No shared context, no edits.
4. **Fix only triaged-accepted findings.**
5. **Never print or store credentials.** Use the CLIs' own authentication;
   never ask for an API key; never echo tokens into `.ai/`, reports or logs.
6. **One failed reviewer never fails the run.** Implementer and Review Fixer
   failures *are* fatal.
7. **Respect the budgets.** `run`, `review run` and `budget consume` exit 3
   when one is spent. That is the answer: report what is unresolved. `--force`
   belongs to a human who has decided to override; it is not yours.
8. **A stalled agent is not a clean result.** `stalled` means it produced
   nothing until it was killed. A failure — say so.
9. **Keep the source tree clean.** Orchestration artifacts live in `.ai/`.

## References

Read one only when you need its detail — each costs about as much as this
whole document.

- `references/workflow.md` — stage detail, prompt templates, artifacts
- `references/configuration.md` — schema, layering, every field, examples
- `references/providers.md` — adapter interface, Claude/Codex, adding a CLI
- `references/reviews.md` — snapshot, output schema and limits, dedup, triage
- `references/architecture.md` — how the pieces fit, and why
- `references/limits.md` — stalls, timeouts, budgets
- `references/cli.md` — every command and flag
