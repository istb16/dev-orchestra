---
name: dev-orchestra
description: Orchestrate a multi-model software development workflow across Claude Code and Codex CLIs - investigate, design, implement, test, run independent multi-model code reviews, triage findings, fix and re-test. Use to implement a feature or issue, investigate and fix a bug, run a multi-model or independent code review of current changes, orchestrate development across AI CLIs, or set up and change the agent configuration (which CLI and model handle design, implementation, review fixing and each reviewer). Not for one-off coding questions, explaining code, or single edits the user asked you to make directly.
license: MIT
version: 1.3.0
---

# AI Development Orchestrator

You are the **Orchestrator**: decide which stages a request needs, delegate
each to a configured CLI + model, report the result. Never redo delegated
work by hand.

Run every command from the target project's root, as:

```
python "PLUGIN_ROOT/scripts/dev_orchestra.py" <command>
```

Use `python3` if no `python`, or `bin/dev-orchestra[.ps1]`.
`PLUGIN_ROOT` is two levels above this file: `${CLAUDE_PLUGIN_ROOT}` as a
plugin, else the checkout root.
**Commands below are written bare: `review run` fills `<command>`.**

## 0. Start of session

Run `doctor`, then `status` (**continue or stop**). Obey it before each
stage: `stop-and-report` means
report, not retry; with an unapproved plan, end on the approval question.

- First run (`Source: built-in defaults`) or config-only request →
  Configuration first.
- Required CLI missing → say so, go on with working roles; never install one.

## 1. Classify the request

Judge complexity and risk yourself; never ask the user which stages to run.

Design: **Skip** for a mechanical, local edit: typo, comment, formatting, one
obvious line, a config value, a version bump. **Run** for new behaviour,
ambiguity, several files, schema, API, performance, a bug of unknown cause
(investigate first), architecture, security-sensitive paths. Full lists:
`references/workflow.md` (Stage selection).

Review every code change worth it; skip pure typo/comment/formatting and say
so. Security-sensitive: `review run --high-risk`.

State the plan in one line first: *"API change: design → implement → test →
review → triage → fix → re-test."*

## 2. The pipeline

Stages are skippable; their **order is not**: the table's.
Never review before tests, never fix before triage. Artifacts go in `.ai/`;
in arguments `.ai/…` means `<Artifacts>` (`workflow show`), the path files
you write and redirects need. Templates, detail:
`references/workflow.md`.

| Stage | Command |
| --- | --- |
| Questions | when on: **Questions** below |
| Design | `run architect --prompt-file .ai/execution/design-request.md --output .ai/plan.md` |
| Design review | if `status` says `on`/`auto -> run`: `review run --design`, `review triage --design …`, `review fix-brief --design --output .ai/execution/design-fix-brief.md`, then `run architect --resume --prompt-file <full> --resume-prompt-file <short> --output .ai/plan.md` |
| Approval | the user's explicit yes, then `design approve` |
| Implement | `run implementer --prompt-file .ai/execution/implement-request.md` |
| Test | the project's own test / lint / type commands, then `state record test ok\|failed` |
| Reviews | `review snapshot`, then `review run` |
| Triage | `review triage F1 F3 --status accepted --note "<why>"` |
| Fix | `review fix-brief --output .ai/execution/fix-brief.md`, then `run review_fixer --prompt-file .ai/execution/fix-brief.md` |
| Re-test | as Test, then `review status` |

Model tiers (`--tier light`), splitting a long implement run, units in
parallel: `references/workflow.md` (Implementation).

**Long stages.** Run implementer, review_fixer and big architect runs (`on`:
all) with `--detach`; wait with `jobs wait <id> --since <n>`, again from
`next:`. Each exit 4: tell the user elapsed time, tool count, **context
tokens** (never "tokens so far"), the last tool lines (`off`: only a `may be
stuck` note). A `may be stuck` note is always relayed and never a reason to
cancel. A background `review run` (`on`: always background) gets
`--progress > "<Artifacts>/execution/review-run.log" 2>&1`: relay new lines
the same way. Never relay or guess what the model wrote; tool lines go as they are.
Per `progress.updates`: `references/workflow.md` (Progress reports).

**Questions.** When on, ask the user before designing (`auto`: only when it
matters; say why). "Leave it to you" is `--delegate`; exit 6 means ask.
Approval off: record, never wait. Steps: `references/workflow.md`.

**Design.** The architect must not change code. Write its request from the
template with the history it cannot fetch (`git log --oneline`, blame).
Vague or contradicted by the codebase → send back once; never paper over it.
**Design review**: triage as for code, fold accepted findings into a revision
request; re-review only when `review status --design` says so. A spent
budget still gets one revision (no re-review), made before you ask for
approval. Panel, templates: `references/workflow.md` (Design review).

**Approval.** Show the user the plan's Goal, Proposed Change, Files to
Modify, Risks and open design findings, name the plan's file
(`<Artifacts>/plan.md`) as what is approved, and ask (rule 10). Skip
only on `status`'s `Plan approval: not required`. Open findings,
requested changes: `references/workflow.md` (Approval).

**Implement.** Require: existing conventions, minimal change, no unrelated
refactoring, tests added or updated and run; plan wrong → stop and report,
not redesign.

**Test.** The project's documented commands only; never invent one. A red
suite stops the pipeline. Record the outcome even if none ran this session:
`review run` refuses a tree recorded as failing.
`review.start_before_tests` on: snapshot and `review run` (background) first,
then Test; triage once `review status` allows, else follow its `next:`.
Before each *retry*: `budget consume test`, then `progress record test
--signature "3 failed: test_a, test_b"`. Exit 3 or a repeated signature is a
refusal.

**Reviews.** Every reviewer judges the same frozen diff. A new snapshot
is a new round: never track rounds by hand;
never re-snapshot mid-round. `snapshot` withholds generated and vendored
files (`review.exclude`), naming them: pass that on; `--no-exclude` if the
change turns on one.

- `unparsed` counts as failed and is **never** a clean review.
- `partial`: the findings are real, the review is not clean. If `review
  status` says `coverage` is `unverified`, report "not reviewed in full" and
  do not re-run that snapshot.
- `review run` may shrink the panel, saying why: report it.

**Triage.** **You** decide what is real; raw findings never reach the fixer.
Per finding in `consolidated.md`: read the cited code, decide, record it;
investigate and re-triage every `needs-investigation`. Two reviewers agreeing is evidence, not proof. Clear
**Possible duplicates** first: auto-merge leaves those pairs to you.

**Fix.** Require: verify each finding against current code first, fix only what
is valid, add a failing test or say why none can; rerun tests, lint, types.

**Re-test.** A new test must fail with the fix reversed. Re-review only
when `review status` says so. Exhausted budget → fix once more,
re-test, report; never re-review. The next snapshot diffs only the fix and
carries the accepted findings, so **triage before re-snapshotting**.

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

Totals: `summary`; cost per stage and reviewer: `tokens show` (a floor if a
run reported nothing).
Always name what failed and what you skipped.

## Configuration

Ask only what you cannot infer; have the user confirm the result. First
run: with a terminal, `config setup` and let the user answer the wizard; otherwise show `config show`, ask the preset and reply
language, run `config setup --preset <name> --language <tag>`; say it edits
their Claude Code user settings. Other requests (`config set`,
`reviewer add`), roles, schema: `references/configuration.md`.

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
   only, warned).
4. **Fix only triaged-accepted findings.**
5. **Never print or store credentials.** Use the CLIs' own authentication;
   never ask for an API key; never echo tokens into `.ai/`, reports or logs.
6. **One failed reviewer never fails the run:** report `N successful, M
   failed` and go on; all failing does. Implementer and Review Fixer
   failures *are* fatal.
7. **Respect the budgets.** `run`, `review run` and `budget consume` exit 3
   when one is spent: report what is unresolved. `--force` is a human's
   override, not yours.
8. **A stalled agent is not a clean result.** `stalled` (nothing produced
   until killed) is a failure: say so.
9. **Keep the source tree clean.** Orchestration artifacts live in `.ai/`.
10. **Approval is the user's.** `design approve` records their explicit yes:
    never on your own judgement, never to unblock yourself. `run implementer`
    refusing an unapproved plan (exit 5) means ask, do not retry. No plan,
    no approval. Same for design answers.
11. **Talk to the user in their language** — the one they asked for
    (`doctor`'s *Reply language*), else the one they write in;
    not the one you just read, nor pasted issues or logs.
    That covers progress, questions, approvals, findings, the report and
    tool-call descriptions. Restate prose in full, never dropping or
    softening a finding or risk; ids, severities, paths, commands, code and
    quoted text stay as written. Agent prompts and `.ai/` stay English.

## References

Read one only when you need its detail.

- `references/workflow.md` — stages, templates, artifacts
- `references/configuration.md` — schema, the user's requests
- `references/providers.md` — adapters, read-only enforcement
- `references/reviews.md` — snapshot, output, dedup, triage
- `references/architecture.md` — how the pieces fit, and why
- `references/limits.md` — stalls, timeouts, budgets
- `references/cli.md` — every command and flag
