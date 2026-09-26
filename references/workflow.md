# Workflow

Full detail for each stage. The skill has the short version; read this when a
stage needs more care than the summary gives.

## Stage selection

The orchestrator decides. Two questions settle almost every case:

1. **Is the change mechanical?** If the correct edit is obvious from the request
   and localised, skip design.
2. **Could this break something the request does not mention?** If yes — schema,
   API, shared module, concurrency, auth — run design, however small the diff.

Skipping a stage is a decision worth stating. "Skipped design: one-line typo in
a doc comment" is a useful sentence; silently skipping is not.

## Artifacts

```
.ai/
├── .gitignore                      # `*`, written once, covers every workflow
├── current.json                    # the workflow this directory last resolved
└── workflows/
    └── <workflow-id>/
        ├── plan.md                 # Architect output
        ├── execution/
        │   ├── design-request.md   # prompt you wrote for the Architect
        │   ├── design-fix-brief.md # generated from accepted design findings
        │   ├── design-revise-request.md
        │   ├── implement-request.md
        │   └── fix-brief.md        # generated from accepted findings
        ├── reviews/
        │   ├── review-target.diff  # frozen snapshot
        │   ├── review-target.json  # strategy, files, sha256, round_id
        │   ├── review-surrounding.json  # enclosing symbols, only with review.context.surrounding: enclosing
        │   ├── <reviewer-id>.md    # one per reviewer
        │   ├── consolidated.md
        │   ├── consolidated.json
        │   ├── rounds/             # consolidated.json of every round, <sha12>-<round_id>.json
        │   └── design/             # the design review, counted separately
        │       ├── review-target.md    # the frozen plan
        │       ├── review-target.json  # plan, request, sha256, round_id
        │       ├── <reviewer-id>.md
        │       ├── consolidated.md
        │       ├── consolidated.json
        │       └── rounds/
        └── state.json              # stage events, resolved model ids, plan approval
```

**One directory per workflow.** Two sessions working in the same checkout used
to share `plan.md`, the review reports, the budgets and the round counter, and
neither announced itself -- so the first session's plan was overwritten and its
budget spent by the other. The id is resolved per command, in order, from
`--workflow`, `DEV_ORCHESTRA_WORKFLOW`, the host's session id (hashed to twelve
characters, so another tool's internal identifier stays out of our paths),
`current.json`, and finally a new id.

Because commands name artifacts by their container-relative path, `--output
.ai/plan.md` means *the plan of this workflow* and lands in its directory. A
path outside `.ai/`, or one that already names a workflow, is used as written.

**This separates the bookkeeping, not the work.** The implementer edits the
working tree and the reviewers read `git diff` of that same tree, and there is
one of those per checkout. Two workflows running at the same time here still
see each other's half-finished edits. For work that really runs in parallel,
give each workflow its own worktree:

```
git worktree add ../feature-x feature-x
```

which is a different repository root, and therefore a different `.ai/`.
`workflow list` names any other workflow that looks live here, and the
commands say so rather than let the separate directories imply otherwise.

An upgrade from a version before 0.4.0 adopts the flat `.ai/` into the first
workflow that runs, so an interrupted workflow keeps its plan and its reports.

`.ai/` gets a `.gitignore` containing `*` on first use, so artifacts stay out of
the user's commits. Teams who want them reviewable can delete that file and
commit the directory; teams who never want it can add `.ai/` to the repo's own
`.gitignore`. Say which you did if you change it.

Nothing in `.ai/`, and no `.dev-orchestra.yaml`, ever enters a review
snapshot — the skill's own files are not the change under review.

## Design

Write the design request yourself — the Architect starts with no context from
this conversation.

```markdown
# Design request

## Goal
<what the user asked for, in your words>

## What I already know
- Entry point: app/controllers/orders_controller.rb:42
- Related: app/services/pricing.rb, spec/services/pricing_spec.rb
- The project uses <framework/conventions you observed>

## Constraints
- Must stay backward compatible with the v1 API
- No new dependencies

## Out of scope
- <anything you have already ruled out, and why>

## Deliverable
Print the complete plan to stdout as Markdown, with these sections: Goal,
Current Behavior, Investigation, Root Cause, Proposed Change, Files to Modify,
Data/API Impact, Compatibility, Test Strategy, Risks, Implementation Steps.
The caller captures stdout. Do not write it to a file: this role runs in plan
mode. Do not modify any file.
```

Ask for the plan on stdout and nowhere else. `--output` saves what the role
printed, and this role runs in plan mode, where it cannot write outside its own
plans directory: told to write a file, it writes one you will never see and
reports having done so -- and that *report* is what lands in `.ai/plan.md`,
plausible enough that the design review then reviews it.

```bash
dev-orchestra run architect \
  --prompt-file .ai/execution/design-request.md \
  --output .ai/plan.md
```

Read the plan before passing it on. Send it back once if it is vague,
contradicts the codebase, or skips the risky part. If the second attempt is
still weak, say so in the report rather than quietly improvising.

## Design review

Off unless `review.design.enabled` is true; `status` reports which. The same
panel judges `.ai/plan.md` against the codebase before any code is written.

```bash
dev-orchestra review run --design
dev-orchestra review show --design
dev-orchestra review triage --design F1 --status accepted --note "confirmed"
dev-orchestra review fix-brief --design --output .ai/execution/design-fix-brief.md
dev-orchestra review status --design
```

Triage exactly as for a code review: read what the plan claims, check it
against the code, decide. Then write the revision request yourself — the
Architect starts again with no context, so the brief alone is not a prompt:

```markdown
# Revise the plan

<the original design request, unchanged>

Read .ai/plan.md and revise it. Keep every section it already has.

<paste .ai/execution/design-fix-brief.md here>

For each finding: say whether you addressed it and how, or why it is not a
problem. Do not widen the scope beyond the original request.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

```bash
dev-orchestra run architect \
  --prompt-file .ai/execution/design-revise-request.md \
  --output .ai/plan.md
```

A revision that stalls, times out or fails leaves `.ai/plan.md` as it was --
the run is being asked to rewrite its own input, so a bad one must not consume
it. Whatever it did print goes to `.ai/plan.md.rejected`; read that before
spending the next attempt — it is this attempt's, one from an earlier attempt
having been removed. The command exits non-zero whenever it refuses the write,
so a chained `review run --design` does not review the old plan as the new one.

That spends an attempt from `budgets.architect`, which is why the design review
has no budget key of its own. Re-review with `review run --design` only when
`review status --design` says so — the rewritten plan hashes differently, so
the next round is derived, never passed in.

The round that reaches `review.design.max_iterations` still gets its revision:
the limit counts reviews, and what it refuses is the re-review of that
revision, not the revision. Until it is made, `status` says `continue` and
`review status --design` says to fold the accepted findings in once more
(`final_revision: pending`). Once the plan differs from the frozen
`review-target.md`, or the architect answered after that round with its
`--output` on the plan, `status` says
`stop-and-report` (`final_revision: done`): present the unreviewed revision
with the findings still open and ask, because implementing a plan whose known
problems are unanswered is the mistake this stage exists to prevent. With no
`budgets.architect` attempt left to revise it, the stop comes at once
(`blocked`) and the question is whether to approve over the findings or free an
attempt. A plan already approved (`approved`, even with
`design.require_approval` turned off since) or already implemented
(`implemented`) is not asked to change. Only accepted findings are folded in:
with none of them `accepted` -- untriaged, `needs-triage` or
`needs-investigation` -- the spent budget stops at once (`unaccepted`). A round that found exactly what the
previous one found does not skip the revision either; the repeat is said
beside it and becomes a reason once the revision is made.

Only the previous plan survives, frozen in `reviews/design/review-target.md`.
A rewrite overwrites everything older.

## Approval

With `design.require_approval` on (the default), nothing is implemented until
the user has approved the plan. Put to them:

- the plan's **Goal**, **Proposed Change**, **Files to Modify** and **Risks**;
- any design findings still open, and whether they came from a review of this
  plan or of an earlier revision (`design approve` says which);

and ask. Only their explicit yes is recorded, with `dev-orchestra design
approve` -- never on your own judgement, and never to get past a refusal. If
they ask for changes, revise the plan (and re-review it if the design review is
on), then ask again: the revision hashes differently, so the old approval no
longer counts.

A design review budget spent with findings still open is `stop-and-report`
once the last round's revision is made, and that report ends in this question:
present the revised plan, name the findings still open from the review of the
earlier revision, and ask. If the architect left the plan unchanged, ask
whether to approve over the findings or to triage them again; if no architect
attempt was left to revise it, ask whether to approve over them or to free an
attempt (`budget reset`) and revise. Once they approve, the stop is answered --
`status` drops that reason for the approved plan.

`run implementer` refuses (exit 5) while the plan is not approved, before it
spends any budget and before `--detach` starts a worker; a worker checks again
and writes the whole refusal into its job. `--force` does not apply. No plan
(no design stage) means no gate.

An approval is of the plan text alone (sha256) and of the design review round
it was given over. It goes stale when the plan changes (`plan-changed`) or when
`review run --design` runs again afterwards, even over the same plan
(`reviewed-since`). Re-consolidating and triaging do not.

`status --json` reports it under `design_approval`:

| Field | Meaning |
| --- | --- |
| `required` | `design.require_approval` |
| `state` | `pending`, `stale`, `approved`, `no-plan`, `not-required`, or `implemented-unapproved` |
| `pending` | `true` when the user has to be asked (`pending` or `stale`) |
| `stale_reason` | `plan-changed`, `reviewed-since`, or `null` |
| `plan_sha256`, `approved_sha256`, `approved_at` | the plan now, and the one approved |
| `matches_current_plan` | whether the approved sha is the current plan's; `null` without both |
| `reviewed_since_approval` | whether a design review round ran after the approval; `null` without one |
| `open_findings`, `open_findings_of_current_plan` | the blocking design findings, and whether that round reviewed this plan text |
| `design_review_exhausted` | the design review budget is spent with findings open |

Beside it, `design_review.final_revision` says where the last round's revision
stands (`pending`, `done`, `blocked`, `approved`, `implemented`, `unaccepted`, or `null`
before the limit), `final_revision_pending` is `true` while it is still owed,
and `identical_rounds` is how many rounds in a row found the same findings.

`implemented-unapproved` is a workflow whose implementer last finished `ok`
after the plan file was last written, with no approval on record -- one that
was already implemented when the gate arrived. `status` says there is nothing
to ask, so it is not raised at every later stage; it is not an exemption, and
running the implementer again on it is refused like `pending`. A failed
implementer run never counts, and a plan written afterwards is asked about.

## Implementation

```markdown
# Implementation request

Follow the plan in .ai/plan.md.

Rules:
- Match the conventions already in this codebase; do not introduce new ones.
- Make the minimal change that satisfies the plan.
- No unrelated refactoring, renaming, or formatting.
- Add or update the tests the plan's Test Strategy calls for.
- Run those tests and report the result.
- If the plan is wrong or impossible, STOP and explain why. Do not redesign.

Report: files changed, tests added/updated, test output, anything you could not do.
```

```bash
dev-orchestra run implementer --prompt-file .ai/execution/implement-request.md
```

Implementer failure is fatal: stop, report what happened, leave the tree in a
state the user can inspect.

## Test

Use the project's own commands, discovered from the repo (`package.json`,
`Makefile`, `Rakefile`, `pyproject.toml`, CI config, CONTRIBUTING). Prefer the
narrowest command that covers the change, then broaden if time allows. Never
invent a test command; if you cannot find one, say so.

A failing suite stops the pipeline. Fix it or report it — do not review a broken
tree.

## Reviews

```bash
dev-orchestra review snapshot   # freeze it
dev-orchestra review run        # fan out
```

`review snapshot` diffs the working tree against `HEAD` by default and folds in
untracked files, so brand-new modules are reviewed too. Use `--base <rev>` to
review everything since a branch point:

```bash
dev-orchestra review snapshot --base main
```

See `references/reviews.md` for the output schema, deduplication and triage.

## Fix

```bash
dev-orchestra review fix-brief --output .ai/execution/fix-brief.md
dev-orchestra run review_fixer --prompt-file .ai/execution/fix-brief.md
```

Prepend your instructions to the generated brief:

```markdown
For each finding below:
1. Read the cited code as it is now.
2. Verify the finding is still true. If it is not, say so and change nothing.
3. Fix valid findings with the smallest correct change.
4. Add a test where the finding exposes a coverage gap.
5. Run the relevant tests plus lint/type checks, and report the output.

Do not fix anything that is not listed here.
```

## Re-test and re-review

```bash
dev-orchestra review status
```

```json
{
  "iteration": 1,
  "max_review_iterations": 2,
  "blocking": ["F1"],
  "re_review_recommended": true,
  "iteration_budget_exhausted": false,
  "final_fix": null,
  "final_fix_pending": false
}
```

The round advances automatically: `review run` derives it from the snapshot, so
a new snapshot is a new round and re-running the same one (after a reviewer
failed, say) stays in the current round. Pass `--iteration` only to override
that deliberately.

Re-review only when `re_review_recommended` is true, and only after a fresh
`review snapshot`. When the budget is spent, fix once more, re-test and record
it, then report; do not re-review. The round that reached the limit still gets
its fix (`final_fix: pending`, `status` says `continue`) and its re-test
(`retest`, still `continue`, even if the fix used the last `review_fixer`
attempt); once a `test` outcome is recorded after the fix (`done`), `status`
says `stop-and-report`. Report the remaining findings with their severity and
location and let the user decide. A final round that repeated the previous
one's findings still gets its fix; the repeat becomes a reason after it. With
no `review_fixer` attempt left for the fix, the stop comes at once (`blocked`),
and so it does with no finding `accepted` to fix (`unaccepted`).
Looping past the budget is how a run turns into an expensive no-op.

## Recording and reporting

Stages delegated through `run` are recorded automatically. Record the rest so
the summary is complete:

```bash
dev-orchestra state record test ok --detail command="pytest -q" passed=128
dev-orchestra state record test ok --detail phase=re-test
dev-orchestra summary
```

Record the re-test under `test` too: the review gate and `status`'s re-test
check read stage `test`. A `re-test` stage is accepted by `status` as the
re-test, but the gate never sees it.

The final report names: which stages ran, which were skipped and why, test
results, review outcome including failures, triage counts, files changed, and
anything left unresolved.

## When something goes wrong

| Situation | Do this |
| --- | --- |
| A required CLI is missing | Report it; run the stages that still work; never install it yourself |
| A model will not resolve | Fix the config or ask the user; never substitute a guess |
| One reviewer fails | Continue; report `N successful, M failed` |
| Every reviewer fails | Treat the review stage as failed; do not claim the change is reviewed |
| A round comes back `partial` | The change body was handed over as a file; the findings are real, the review is not clean. Triage them, then report the round as not reviewed in full. `review status` says how to clear it |
| Implementer or fixer fails | Stop the pipeline and report |
| Snapshot is empty | There is nothing to review — check whether the implementation actually wrote anything |
| `review run --design` says there is no plan | You skipped the design stage, so skip the design review with it; otherwise run the Architect first |
| Tests fail after a fix | Report the failure with output; do not keep fixing blindly |
| A run comes back `stalled` | It produced no output until killed. Report it as a failure, and check for orphan processes if warned about them |
| A command exits 3 | A budget is spent. Report what is unresolved; do not retry, and do not reach for `--force` |
| `run implementer` exits 5 | The plan is not approved, or changed / was reviewed after approval. Present it and ask the user; record a yes with `design approve`. Do not retry and do not approve it yourself |
| A detached implementer job is `failed` with "not approved" | Same cause, found by the worker; once the user approves, start it again |
| `status` says `stop-and-report` | Stop. It has already weighed budgets, stalls and open findings |
