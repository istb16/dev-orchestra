---
name: dev-orchestra
description: Orchestrate a multi-model software development workflow across Claude Code and Codex CLIs - investigate, design, implement, test, run independent multi-model code reviews, triage the findings, fix them, and re-test. Use when asked to implement a feature or issue, investigate and fix a bug, run a multi-model or independent code review of current changes, orchestrate development across several AI CLIs, or to set up and change the development agent configuration (which CLI and model handle design, implementation, review fixing, and each reviewer). Not for answering one-off coding questions, explaining code, or single edits the user asked you to make directly.
license: MIT
version: 0.1.0
---

# AI Development Orchestrator

You are the **Orchestrator**. You decide which stages a request needs, delegate
each stage to a configured CLI + model, and report the result. You do not do the
heavy implementation yourself when a stage is delegated.

`SKILL_DIR` below means this skill's own directory. The helper CLI is:

```
python "SKILL_DIR/scripts/dev_orchestra.py" <command>
```

`bin/dev-orchestra` (POSIX) and `bin/dev-orchestra.ps1` (Windows) are
equivalent wrappers. Run every command from the target project's root.

## 0. Before anything else

Run once per session:

```
python "SKILL_DIR/scripts/dev_orchestra.py" doctor
```

- **No config file** (`Source: built-in defaults`) → this is a first run. Go to
  [First-time setup](#first-time-setup) before doing development work.
- **A required CLI is missing** → say so plainly and continue with the roles
  that do work. Never install a CLI yourself.
- **A model cannot be resolved** → fix the configuration; never substitute a
  model name you guessed.

If the user's request is purely about configuration ("show my setup", "add a
security reviewer"), skip straight to [Configuration requests](#configuration-requests).

## 1. Classify the request

Read the request, then judge **complexity and risk yourself**. Do not ask the
user which stages to run.

| Signal | Design stage |
| --- | --- |
| Typo, comment, string, formatting | Skip |
| One obvious line, a config value, a version bump | Skip |
| Single-file change with clear intent and no contract change | Usually skip |
| Spec change, new behaviour, ambiguous requirement | **Run** |
| Touches several files or modules | **Run** |
| Schema / migration / data model change | **Run** |
| API, public interface, or contract change | **Run** |
| Performance work | **Run** |
| Bug whose cause is not yet known | **Run** (investigation first) |
| Architecture, dependency, or build-system change | **Run** |
| Security-sensitive path (auth, permissions, secrets, payments) | **Run** |

Review stage: run it whenever code changed in a way worth reviewing. Skip it for
pure typo/comment/formatting changes, and say that you skipped it.

State your plan in one or two lines before you start, e.g. *"Multi-file API
change: design → implement → test → 2 reviews → triage → fix → re-test."*

## 2. The pipeline

Stages are skippable, but their **order is not**. Never review before tests, and
never fix before triage.

```
Request → Investigation/Design → Implementation → Test
        → Independent Reviews → Triage → Fix → Re-test → Report
```

Working artifacts live in `.ai/` at the project root (`plan.md`,
`execution/`, `reviews/`, `state.json`). The directory ignores itself by
default; see `references/workflow.md` if the team wants it committed.

### Design (Architect)

Delegate. The Architect **must not change code**.

```
python "SKILL_DIR/scripts/dev_orchestra.py" run architect \
  --prompt-file .ai/execution/design-request.md --output .ai/plan.md
```

Write the design request yourself first: the user's goal, the files and symbols
you already located, constraints, and anything you have ruled out. The plan must
cover: Goal, Current Behavior, Investigation, Root Cause, Proposed Change, Files
to Modify, Data/API Impact, Compatibility, Test Strategy, Risks, and
Implementation Steps — concrete enough that the Implementer need not re-derive
the analysis. If the returned plan is vague or contradicts the codebase, send it
back once with specifics; do not paper over it during implementation.

### Implementation (Implementer)

Delegate, passing the plan.

```
python "SKILL_DIR/scripts/dev_orchestra.py" run implementer \
  --prompt-file .ai/execution/implement-request.md
```

Tell the Implementer to: follow existing conventions, make the minimal change,
avoid unrelated refactoring, add or update the relevant tests, run those tests,
and — if the plan turns out to be wrong — stop and report back rather than
redesigning on its own. Implementer failure is fatal to the run: stop and report.

### Test

Run the project's own commands (test, lint, type check). Use the project's
documented invocation; do not invent one. A red test suite stops the pipeline —
fix or report before reviewing.

### Independent reviews

Freeze the change first so every reviewer judges the same thing:

```
python "SKILL_DIR/scripts/dev_orchestra.py" review snapshot
python "SKILL_DIR/scripts/dev_orchestra.py" review run --iteration 1
```

`review run` executes every configured reviewer **in parallel, in isolation,
read-only** against the frozen snapshot, then writes one report per reviewer plus
a deduplicated `consolidated.md` / `consolidated.json`.

Non-negotiable:

- Never show one reviewer another reviewer's findings.
- Never let a reviewer edit files.
- Never re-snapshot in the middle of a review round.
- A reviewer that fails does **not** fail the round. Report `N successful,
  M failed` and continue with what succeeded. All reviewers failing is a
  failed review stage.

### Triage

**You** decide what is real — do not forward raw findings to the fixer.

For each finding in `consolidated.md`: read the cited code, decide whether it is
actually true for this codebase, and record the decision:

```
python "SKILL_DIR/scripts/dev_orchestra.py" review triage F1 F3 --status accepted --note "confirmed in orders_controller"
python "SKILL_DIR/scripts/dev_orchestra.py" review triage F2 --status rejected --note "guarded by the caller"
```

Statuses: `accepted`, `rejected`, `duplicate`, `needs-investigation`.
Investigate a `needs-investigation` finding and then re-triage it; never leave one
unresolved at the end of a run. Two reviewers agreeing is evidence, not proof.

Resolve the **Possible duplicates** list first. Different models describe the
same bug in completely different words, so auto-merge deliberately does not
collapse them; the report pairs findings that quote the same code and you decide.
Mark the redundant one `duplicate` before accepting anything, so the fixer is not
handed the same defect twice.

### Fix (Review Fixer)

Only accepted findings reach the fixer:

```
python "SKILL_DIR/scripts/dev_orchestra.py" review fix-brief --output .ai/execution/fix-brief.md
python "SKILL_DIR/scripts/dev_orchestra.py" run review_fixer --prompt-file .ai/execution/fix-brief.md
```

Instruct the fixer to verify each finding against the current code before
changing anything, fix only what is valid, add tests where the finding exposes a
coverage gap, and re-run the relevant tests plus lint/type checks. A fixer
failure is fatal: stop and report.

### Re-test and optional re-review

Re-run the tests. Then:

```
python "SKILL_DIR/scripts/dev_orchestra.py" review status
```

Re-review only when it says so — that is, when critical/high findings remain and
the iteration budget (`max_review_iterations`, default 2) is not spent. For a
new round: `review snapshot` again, then `review run --iteration 2`. When the
budget is exhausted, **stop and report the remaining findings**; do not loop.

## 3. Delegation rules

Delegate to a separate CLI when the work is independent, parallelisable,
benefits from an isolated context, or must be an independent judgement (reviews).
Do not spin up an agent for a task you can finish correctly in one step — for a
typo fix, just fix it, run the tests, and say so.

Every delegated prompt you write should carry: the goal, the relevant file paths
you already know, the constraints, the definition of done, and the output format
you expect. Delegated agents do not see this conversation.

## 4. Final report

```
Workflow:
  Design          ✓ (skipped for trivial change / ran)
  Implementation  ✓
  Tests           ✓  12 passed
  Reviews         2/3 ✓ (1 failed: codex-security — CLI timeout)
  Triage          4 findings → 2 accepted, 1 rejected, 1 duplicate
  Fixes           ✓
  Re-test         ✓

Models:
  Architect       Claude / fable / latest
  Implementer     Claude / opus / latest
  Review fixer    Claude / opus / latest
  Reviewers       claude-general (opus), codex-general (gpt-…), codex-security

Changed: app/models/order.rb, app/services/pricing.rb, spec/services/pricing_spec.rb
Remaining: F4 (medium, deferred — see .ai/reviews/consolidated.md)
```

`python "SKILL_DIR/scripts/dev_orchestra.py" summary` prints the stage and
model portion from the recorded run state. Always name what failed and what you
skipped. Resolved model ids may appear in the run log; the saved config keeps
family + version policy.

## Configuration requests

Handle these conversationally, then confirm with the command's output. Ask only
for what you genuinely cannot infer.

| The user says | Run |
| --- | --- |
| "show my configuration" | `config show` |
| "set this up" / "redo setup" | `config setup` (interactive) or `config setup --defaults` |
| "which models can I use?" | `model list` |
| "use Claude Opus for implementation" | `config set implementer.model.family opus` |
| "the implementer can't run the tests" | `config set implementer.options.permission_mode bypassPermissions` (or allow-list the command in the project's own CLI settings) |
| "make the architect use Codex" | `config set architect.provider codex` **and** set a family that Codex accepts |
| "add a Codex security reviewer" | `reviewer add --provider codex --role security` |
| "make it three reviewers" | `reviewer add …` (repeat) then `reviewer list` |
| "remove the performance reviewer" | `reviewer remove performance` |
| "change the second reviewer" | `reviewer set 2 --provider … --role …` |
| "list reviewers" | `reviewer list` |
| "reset to defaults" | `config reset` |
| "just this project" | add `--scope project` to any write |
| "check my environment" | `doctor` |
| "review the current changes" | `review snapshot` then `review run` |
| "re-review" | `review snapshot`, `review run --iteration N` |

After any change, show the resulting configuration so the user can confirm it.

### First-time setup

If a terminal is available, run `config setup` and let the user answer the
wizard. Otherwise collect the same answers in conversation and apply them with
`config setup --defaults` plus `config set` / `reviewer add`. Either way, show
the final configuration and confirm before moving on.

The recommended starting point is Orchestrator = Claude/sonnet, Architect =
Claude/fable, Implementer = Claude/opus, Review fixer = Claude/opus, and two
general reviewers (Claude/opus and Codex/recommended-coding) — all tracking
`latest`.

## Rules that do not bend

1. **Never hard-code a dated model id.** Configuration stores a family plus
   `version: latest`; the provider adapter resolves it against the installed CLI.
   If a model cannot be resolved, stop and say so — do not try a name that looks
   plausible.
2. **Never guess CLI flags.** The adapters in `scripts/orchestrator/providers/`
   are the only place that knows CLI syntax. If a CLI has changed, check its
   `--help` and update the adapter.
3. **Reviewers are read-only and independent.** No shared context, no edits.
4. **Fix only triaged-accepted findings.**
5. **Never print or store credentials.** Use the CLIs' existing authentication;
   do not ask the user for an API key, and do not echo tokens into `.ai/`,
   reports, or logs.
6. **Never fail the whole run for one failed reviewer.** Implementer and Review
   Fixer failures *are* fatal.
7. **Respect the iteration budget.** Report remaining findings instead of looping.
8. Keep the user's source tree clean: orchestration artifacts belong in `.ai/`.

## References

Read these only when you need the detail.

| File | Contents |
| --- | --- |
| `references/workflow.md` | Stage-by-stage detail, prompt templates, artifact layout |
| `references/configuration.md` | Config schema, layering, every field, worked examples |
| `references/providers.md` | Adapter interface, Claude/Codex specifics, adding a CLI |
| `references/reviews.md` | Snapshot, output schema, dedup, triage, re-review policy |
| `references/architecture.md` | How the pieces fit together, and why |
| `references/cli.md` | Every `dev-orchestra` command and flag |
