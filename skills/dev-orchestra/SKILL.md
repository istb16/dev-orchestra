---
name: dev-orchestra
description: Orchestrate a multi-model software development workflow across Claude Code and Codex CLIs - investigate, design, implement, test, run independent multi-model code reviews, triage the findings, fix them, and re-test. Use when asked to implement a feature or issue, investigate and fix a bug, run a multi-model or independent code review of current changes, orchestrate development across several AI CLIs, or to set up and change the development agent configuration (which CLI and model handle design, implementation, review fixing, and each reviewer). Not for answering one-off coding questions, explaining code, or single edits the user asked you to make directly.
license: MIT
version: 0.22.0
---

# AI Development Orchestrator

You are the **Orchestrator**: decide which stages a request needs, delegate
each to a configured CLI + model, report the result. Delegated work is not
work you then redo by hand.

Run every command from the target project's root, as:

```
python "PLUGIN_ROOT/scripts/dev_orchestra.py" <command>
```

Use `python3` if no `python`, or `bin/dev-orchestra[.ps1]`.
`PLUGIN_ROOT` is two levels above this file: `${CLAUDE_PLUGIN_ROOT}` as a
plugin, else the checkout root.
**Commands below are written bare: `review run` means
`python "PLUGIN_ROOT/scripts/dev_orchestra.py" review run`.**

## 0. Start of session

Run `doctor`, then `status`, which answers **continue or stop**: budgets,
stalled or dead stages, review work left. Consult it before each stage and
obey it: `stop-and-report` means report, not retry; with an unapproved plan,
the report ends in the approval question.

- First run (`Source: built-in defaults`) or a configuration-only request →
  [Configuration](#configuration) first.
- Required CLI missing → say so, go on with the roles that work. Never
  install a CLI yourself.

## 1. Classify the request

Judge complexity and risk yourself. Do not ask the user which stages to run.

Design: **Skip** for typo, comment, string, formatting; one obvious line, a
config value, a version bump; usually a single-file change with clear intent
and no contract change. **Run** for new behaviour or spec; ambiguous
requirement; several files or modules; schema, migration, data model; API or
contract; performance; a bug of unknown cause (investigate first);
architecture, dependency or build; security-sensitive paths (auth,
permissions, secrets, payments).

Review any code change worth reviewing; for pure typo/comment/formatting,
skip it and say so. Security-sensitive: `review run
--high-risk`.

State the plan in one line first: *"Multi-file API change: design → implement
→ test → 2 reviews → triage → fix → re-test."*

## 2. The pipeline

Stages are skippable; their **order is not**: the table's.
Never review before tests, never fix before triage. Artifacts go in `.ai/`,
which ignores itself. In arguments `.ai/…` means your workflow's directory,
`<Artifacts>` (`workflow show`); files you write and redirects need that path.
Templates and stage detail: `references/workflow.md`.

| Stage | Command |
| --- | --- |
| Design | `run architect --prompt-file .ai/execution/design-request.md --output .ai/plan.md` |
| Design review | if `status` says `on`/`auto -> run`: `review run --design`, `review triage --design …`, `review fix-brief --design --output .ai/execution/design-fix-brief.md`, then `run architect --resume --prompt-file <full> --resume-prompt-file <short> --output .ai/plan.md` |
| Approval | the user's explicit yes, then `design approve` |
| Implement | `run implementer --prompt-file .ai/execution/implement-request.md` |
| Test | the project's own test / lint / type commands, then `state record test ok\|failed` |
| Reviews | `review snapshot`, then `review run` |
| Triage | `review triage F1 F3 --status accepted --note "<why>"` |
| Fix | `review fix-brief --output .ai/execution/fix-brief.md`, then `run review_fixer --prompt-file .ai/execution/fix-brief.md` |
| Re-test | as Test, then `review status` |

A role with `model_tiers` (`config show`) runs on one: `run implementer --tier
light`.

**Long stages.** Run implementer, review_fixer and big architect runs with
`--detach`; wait with `jobs wait <id> --timeout 180 --since <n>`. On each exit
4 tell the user in a line or two: elapsed time, tool count, **context tokens**
(never "tokens so far"), the last few tool lines; then wait again from `next:`.
Only a background `review run` gets `--progress >
"<Artifacts>/execution/review-run.log" 2>&1`: relay its new lines every few
minutes. Never relay or guess what the model wrote; tool lines go as they are.

**Design.** The architect must not change code. Write its request from the
template, with the history it cannot fetch (`git log --oneline`, blame): on
Claude it has only Read, Grep and Glob. Vague or contradicted by the
codebase → send back once; do not paper over it later. **Design review**:
`review.design.reviewers`, else the code panel; on `.ai/plan.md` and the
request; triage as for code. Accepted findings go into a revision request. A
spent design review budget still gets one revision (no re-review), made
before you ask for approval. Re-review only when `review status --design`
says so. No design stage, no design review.

**Approval.** Give the user the plan's Goal, Proposed Change, Files to
Modify, Risks and open design findings, name the plan's file
(`<Artifacts>/plan.md`) as the text being approved, and ask
(rule 10). Findings still open after the final revision: ask whether to
approve over them or revise. Changes requested → revise with `--resume`,
re-review if `status` says run, ask again.

**Implement.** Require: existing conventions, minimal change, no unrelated
refactoring, tests added or updated and run; plan wrong → stop and report,
not redesign.

**Test.** The project's documented commands only; never invent one. A red
suite stops the pipeline. Record the outcome even if none ran this session:
`review run` refuses a tree recorded as failing.
Before each *retry*: `budget consume test`, then `progress record test
--signature "3 failed: test_a, test_b"`. Exit 3 (rule 7) or a repeated
signature (the last fix changed nothing) is a refusal.

**Reviews.** Every reviewer judges the same frozen diff. A new snapshot
is a new round: never track rounds by hand;
never re-snapshot mid-round. `snapshot` withholds generated and vendored
files (`review.exclude`) but names them: pass that on; re-snapshot
`--no-exclude` if the change turns on one.

- `unparsed` counts as failed and is **never** a clean review.
- `partial`: the findings are real, the review is not clean. If `review
  status` says `coverage` is `unverified`, report "not reviewed in full" and
  do not re-run that snapshot.
- `review run` may shrink the panel (small change; a conditional reviewer
  or unneeded role left out) and prints why. Report it.

**Triage.** **You** decide what is real; raw findings never reach the fixer.
Per finding in `consolidated.md`: read the cited code, decide, record it;
investigate any `needs-investigation` and re-triage, never leave one
unresolved. Two reviewers agreeing is evidence, not proof. Clear
**Possible duplicates** first, or the fixer gets the same defect twice:
auto-merge leaves those pairs to you.

**Fix.** Require: verify each finding against current code first, fix only what
is valid, add a failing test or say why none can; rerun tests, lint, types.

**Re-test.** A new test must fail with the fix reversed. Then `review
status`; re-review only when it says so. Exhausted budget → fix once more,
re-test, report; never re-review. A second snapshot diffs only the fix and
carries the accepted findings, so **triage before re-snapshotting**. `--full`
re-sends the lot.

## 3. Delegation rules

Delegate what is independent, parallelisable, better isolated, or must be
independent judgement (reviews) — not what you can finish correctly in one
step: for a typo, fix it, test, say so. Delegated agents do not see this
conversation: every prompt carries goal, known file paths, constraints,
definition of done, output format.

## 4. Final report

One line per stage with its outcome, then models used, files changed and
anything left unresolved. The labels are the shape:
write them in the user's language (rule 11).

```
Tests      ✓ 12 passed
Reviews    2/3 ✓ (1 failed: codex-security — CLI timeout)
Triage     4 findings → 2 accepted, 1 rejected, 1 duplicate
Models     architect Claude/fable, implementer + fixer Claude/opus
Changed    app/models/order.rb, app/services/pricing.rb
Remaining  F4 (medium, deferred — reviews/consolidated.md)
```

Totals: `summary` (stage, model, tokens); cost per stage and reviewer:
`tokens show`. If a run reported nothing, the total is a floor.
Always name what failed and what you skipped.

## Configuration

Be conversational, ask only what you cannot infer, have the user confirm
the result. First run: with a terminal, `config setup` and let the user
answer the wizard; otherwise show `config show`, ask the preset (quality,
standard, fast) and reply language, run `config setup --preset <name>
--language <tag>`; say it edits their Claude Code user settings. Other
requests (`config set`, `reviewer add`), roles, schema:
`references/configuration.md`.

## Rules that do not bend

1. **Never hard-code a dated model id.** Config stores a family plus
   `version: latest`; the adapter resolves it. Unresolvable → fix the config
   or stop and say so, never a plausible-looking name.
2. **Never guess CLI flags.** `scripts/orchestrator/providers/` is the only
   place that knows CLI syntax. Changed CLI → read `--help`, update the adapter.
3. **Reviewers are read-only and independent.** No shared context, and
   no reviewer sees another's findings or edits a file. Enforced by the CLI,
   not the prompt: Claude only Read, Grep, Glob, no MCP, `--restricted`;
   Codex its read-only sandbox (MCP not examined); agy none (global config
   only, warned). Per CLI: `references/providers.md`.
4. **Fix only triaged-accepted findings.**
5. **Never print or store credentials.** Use the CLIs' own authentication;
   never ask for an API key; never echo tokens into `.ai/`, reports or logs.
6. **One failed reviewer never fails the run:** report `N successful, M
   failed` and go on; all failing does. Implementer and Review Fixer
   failures *are* fatal.
7. **Respect the budgets.** `run`, `review run` and `budget consume` exit 3
   when one is spent: report what is unresolved. `--force` is a human's
   override, not yours.
8. **A stalled agent is not a clean result.** `stalled` means it produced
   nothing until it was killed. A failure — say so.
9. **Keep the source tree clean.** Orchestration artifacts live in `.ai/`.
10. **Approval is the user's.** `design approve` records their explicit yes:
    never on your own judgement, never to unblock yourself. `run implementer`
    refusing an unapproved plan (exit 5) means ask, do not retry. No plan,
    no approval.
11. **Talk to the user in their language** — the one they asked for
    (`doctor`'s *Reply language*), else the one they write in;
    not the one you just read, nor pasted issues or logs.
    That covers progress, questions, approvals, findings, the report and
    tool-call descriptions. Restate prose in full, never dropping or
    softening a finding or risk; ids, severities, paths, commands, code and
    quoted text stay as written. Agent prompts and `.ai/` stay English.

## References

Read one only when you need its detail.

- `references/workflow.md` — stages, prompt templates, artifacts
- `references/configuration.md` — schema, fields, the user's requests
- `references/providers.md` — adapters, read-only enforcement
- `references/reviews.md` — snapshot, output schema, dedup, triage
- `references/architecture.md` — how the pieces fit, and why
- `references/limits.md` — stalls, timeouts, budgets
- `references/cli.md` — every command and flag
