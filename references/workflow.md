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
├── plan.md                     # Architect output
├── execution/
│   ├── design-request.md       # prompt you wrote for the Architect
│   ├── implement-request.md    # prompt you wrote for the Implementer
│   └── fix-brief.md            # generated from accepted findings
├── reviews/
│   ├── review-target.diff      # frozen snapshot
│   ├── review-target.json      # strategy, files, sha256
│   ├── <reviewer-id>.md        # one per reviewer
│   ├── consolidated.md
│   └── consolidated.json
└── state.json                  # stage events + resolved model ids
```

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
Write a plan to .ai/plan.md with these sections: Goal, Current Behavior,
Investigation, Root Cause, Proposed Change, Files to Modify, Data/API Impact,
Compatibility, Test Strategy, Risks, Implementation Steps.
Do not modify any file.
```

```bash
dev-orchestra run architect \
  --prompt-file .ai/execution/design-request.md \
  --output .ai/plan.md
```

Read the plan before passing it on. Send it back once if it is vague,
contradicts the codebase, or skips the risky part. If the second attempt is
still weak, say so in the report rather than quietly improvising.

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
  "iteration_budget_exhausted": false
}
```

The round advances automatically: `review run` derives it from the snapshot, so
a new snapshot is a new round and re-running the same one (after a reviewer
failed, say) stays in the current round. Pass `--iteration` only to override
that deliberately.

Re-review only when `re_review_recommended` is true, and only after a fresh
`review snapshot`. When the budget is spent, report the remaining findings with
their severity and location and let the user decide. Looping past the budget is
how a run turns into an expensive no-op.

## Recording and reporting

Stages delegated through `run` are recorded automatically. Record the rest so
the summary is complete:

```bash
dev-orchestra state record test ok --detail command="pytest -q" passed=128
dev-orchestra state record re-test ok
dev-orchestra summary
```

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
| Implementer or fixer fails | Stop the pipeline and report |
| Snapshot is empty | There is nothing to review — check whether the implementation actually wrote anything |
| Tests fail after a fix | Report the failure with output; do not keep fixing blindly |
| A run comes back `stalled` | It produced no output until killed. Report it as a failure, and check for orphan processes if warned about them |
| A command exits 3 | A budget is spent. Report what is unresolved; do not retry, and do not reach for `--force` |
| `status` says `stop-and-report` | Stop. It has already weighed budgets, stalls and open findings |
