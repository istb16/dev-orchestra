# Workflow

<!-- contents: start -->

**Contents**

- [Stage selection](#stage-selection)
- [Artifacts](#artifacts)
- [Design](#design)
- [Design review](#design-review)
- [Approval](#approval)
- [Implementation](#implementation)
- [Test](#test)
- [Reviews](#reviews)
- [Fix](#fix)
- [Re-test and re-review](#re-test-and-re-review)
- [Recording and reporting](#recording-and-reporting)
- [When something goes wrong](#when-something-goes-wrong)
- [Example workflows](#example-workflows)
- [Installing from a skill checkout](#installing-from-a-skill-checkout)

<!-- contents: end -->

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
        │   ├── design-resume-request.md
        │   ├── implement-request.md
        │   ├── fix-brief.md        # generated from accepted findings
        │   └── review-run.log      # only if you redirect `review run --progress` here
        ├── jobs/
        │   ├── <id>.json           # a detached run (`run --detach`)
        │   ├── <id>.out            # its output
        │   └── <id>.activity       # its tool uses, one line each; .activity.1 after 500
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
budget spent by the other.

```
$ dev-orchestra workflow show
Workflow: 5942d94f5248 (from: session)
Artifacts: /code/app/.ai/workflows/5942d94f5248

$ dev-orchestra workflow list
5942d94f5248  2026-09-15T09:12:04Z  runs=6  review/ok  [current]
9c1e07b3a880  2026-09-15T08:40:11Z  runs=2  implementer/ok
```

The id is resolved per command, in this order:

| Source | |
| --- | --- |
| `--workflow <id>` | An explicit name, e.g. `--workflow auth-fix` |
| `DEV_ORCHESTRA_WORKFLOW` | The same, from the environment |
| the host's session id | Hashed to twelve characters, so another tool's internal identifier stays out of our paths. Deterministic, so every command in one session agrees without a file to coordinate through |
| `current.json` | What this directory last resolved, for hosts that export no session id |
| a new id | First run |

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
workflow that runs, so an interrupted workflow keeps its plan, its reports and
its budget.

**How the formats change.** The `.ai/` artifacts are part of the public
surface, and they change by addition only: a new version adds a key or a file,
a reader skips a key it does not know, and a key an older version never wrote
reads as what that older file meant -- which is "unknown" rather than zero
where zero would be a claim, as with `priced_runs` in the token ledger. A key is not removed, renamed or given a new meaning.
A change that cannot be made by addition is a breaking change -- a major
release, or a minor one while the version is below 1.0 -- and it ships with a
migration that moves the old form into the new, as the 0.4.0 layout change
did. The files carry no format version for that
reason: nothing reads one, and the rule above is what keeps an older workflow
readable. The `--json` output of the commands changes the same way, as with the
`*_real` keys that appear only when a path is stored somewhere else.

`.ai/` gets a `.gitignore` containing `*` on first use, so artifacts stay out of
the user's commits. Teams who want them reviewable can delete that file and
commit the directory; teams who never want it can add `.ai/` to the repo's own
`.gitignore`. Say which you did if you change it.

`jobs/<id>.activity` (and `.activity.1`, once a run passes 500 tool uses) is
what `jobs wait` reports while a detached run works, and
`execution/review-run.log` exists only when you redirect a background
`review run --progress` there. The tool lines in both went through the same
allowlist and cleaning -- no model text, no free-text arguments -- and the rest
of the log is `review run`'s usual output. Both are under `.ai/`, which ignores
itself; a team that deleted `.ai/.gitignore` has to ignore them in its own
`.gitignore`.

Nothing in `.ai/`, and no `.dev-orchestra.yaml`, ever enters a review
snapshot — the skill's own files are not the change under review.

## Design

Write the design request yourself — the Architect starts with no context from
this conversation. It runs read-only, and on Claude with only `Read`, `Grep`
and `Glob`: no shell and no git, so put the history that matters in the
request.

```markdown
# Design request

## Goal
<what the user asked for, in your words>

## What I already know
- Entry point: app/controllers/orders_controller.rb:42
- Related: app/services/pricing.rb, spec/services/pricing_spec.rb
- The project uses <framework/conventions you observed>
- History that matters: <git log --oneline -- path, or blame of the lines in question -- the architect has no shell>

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

Runs when `review.design.enabled` is `true`, or `auto` (the default) and the
plan is risky or large or a round has already run; `status` says which and
why. The design panel -- `review.design.reviewers` when a file sets one, else
the code panel with `when` ignored -- judges `.ai/plan.md` against the
codebase before any code is written, and a specialist role the plan has
nothing for sits the round out with a note saying why (report it, as for a
code round). A design mistake otherwise costs an implementation and a review to
find, so this is the cheapest place to catch one -- but it is a reviewer run
per panel member per round, which is why `auto` spends it only where the plan
calls for it.

It keeps its own reports, round counter and triage under `reviews/design/`, so
a design round never advances -- or is refused by -- the code review's count.
Skipping the design stage skips this with it.

```bash
dev-orchestra review run --design
dev-orchestra review show --design
dev-orchestra review triage --design F1 --status accepted --note "confirmed"
dev-orchestra review fix-brief --design --output .ai/execution/design-fix-brief.md
dev-orchestra review status --design
```

Triage exactly as for a code review: read what the plan claims, check it
against the code, decide. Then write the revision request yourself, as two
files. The architect continues the session in which it designed the plan when
it can, and starts again with no context when it cannot -- `run` knows which
and sends the matching one, so write both. Both ask the architect to answer
with the smallest change and to list what it added under `## Added in this
revision`: in the recorded rounds most new high findings on a re-review came
from what the revision itself added, so the re-review is pointed at that list.

`design-revise-request.md`, for a fresh run (the brief alone is not a prompt):

```markdown
# Revise the plan

<the original design request, unchanged>

Read .ai/plan.md and revise it. Keep every section it already has.

<paste .ai/execution/design-fix-brief.md here>

For each finding: say whether you addressed it and how, or why it is not a
problem. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which finding it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

`design-resume-request.md`, for a continued session -- it does not repeat the
plan, which the session already holds, but has it read once, because you may
have trimmed what the architect printed and the findings point into the file:

```markdown
# Revise the plan

You are continuing the session in which you designed this plan. Read
.ai/plan.md once before changing anything: it is the plan you printed, as
saved by the orchestrator, and the findings below refer to its sections.
Do not re-read code you already read unless a finding contradicts what you
remember.

<paste .ai/execution/design-fix-brief.md here>

For each finding: say whether you addressed it and how, or why it is not a
problem. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which finding it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

When `review status --design` says `final_revision: pending` -- the revision
no review will see -- add this to both. Its findings are mostly holes in what the previous
revision added, and patching each hole adds the next one; the last revisions
that settled did it by taking machinery out:

```markdown
This is the last revision; no review will see it. Where a finding is a hole
in something an earlier revision added, remove or simplify that mechanism
rather than patching it, and where a rule is left uncertain, make it fail
toward the safe side. Under `## Added in this revision`, also list each
removal as `Removed:` with the finding it answers; write `None.` only when
you neither added nor removed anything.
```

```bash
dev-orchestra run architect --resume \
  --prompt-file .ai/execution/design-revise-request.md \
  --resume-prompt-file .ai/execution/design-resume-request.md \
  --output .ai/plan.md
```

Pass `--resume` only to revise the plan the last architect run wrote. A new
design request in the same workflow is run without it. Whether it continued or
ran fresh, and why, is in the note on stderr and in the run log
(`references/cli.md`). If the note says `running fresh: the provider cannot
resume a session (unverified)`, this version of the CLI has not been checked
to keep a continued session read-only: tell the user, who can run `python
scripts/smoke_live.py --provider claude` to check it. Do not run it yourself --
it spends real tokens on the real CLI. A second note after `resuming the last
architect session` saying the version `is trusted to resume as newer than`
another means it continued on trust: mention it to the user once, with the
same command.

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

A revision the user asked for has no fix brief, so its two files carry their
words instead. `design-change-request.md`, for a fresh run:

```markdown
# Revise the plan

<the original design request, unchanged>

Read .ai/plan.md and revise it. Keep every section it already has.

The owner reviewed the plan and asked for these changes, in their words:

<the changes the owner asked for, unchanged>

For each change: say how you made it, or why it conflicts with the original
request. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which change it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

`design-change-resume-request.md`, for a continued session:

```markdown
# Revise the plan

You are continuing the session in which you designed this plan. Read
.ai/plan.md once before changing anything: it is the plan you printed, as
saved by the orchestrator. Do not re-read code you already read unless a
change below contradicts what you remember.

The owner reviewed the plan and asked for these changes, in their words:

<the changes the owner asked for, unchanged>

For each change: say how you made it, or why it conflicts with the original
request. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which change it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

```bash
dev-orchestra run architect --resume \
  --prompt-file .ai/execution/design-change-request.md \
  --resume-prompt-file .ai/execution/design-change-resume-request.md \
  --output .ai/plan.md
```

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
4. Where a test can show the defect, add one that fails on the code as it
   was and passes after the fix; where none can (wording, documentation,
   a design choice), say why.
5. Run the relevant tests plus lint/type checks, and report the output.

Do not fix anything that is not listed here.
```

## Re-test and re-review

A test the fixer added is only evidence if it fails without the fix, and
one that passes either way reads the same in a green run. So before `run
review_fixer`, record the tree as it is. The change under review is usually
uncommitted and may hold files never added, so neither `HEAD` nor `git stash`
is "before the fix"; a throwaway index takes all of it, `git add -A` as the
snapshot does, and touches neither the real index nor the files. Keep it in
files, not shell variables: the commands run in separate calls.

```bash
GIT_INDEX_FILE=.ai/pre-fix.index git add -A
GIT_INDEX_FILE=.ai/pre-fix.index git write-tree > .ai/pre-fix.tree
```

After the fix, and before recording the re-test, take out only what the fixer
changed, run each new test, and put the fix back:

```bash
GIT_INDEX_FILE=.ai/post-fix.index git add -A
GIT_INDEX_FILE=.ai/post-fix.index git diff --cached "$(cat .ai/pre-fix.tree)" -- <files the fix changed, not the test> > .ai/fix.patch
git apply -R .ai/fix.patch
<the project's test command> <the new test>    # must fail, on its assertion
git apply .ai/fix.patch
rm .ai/pre-fix.index .ai/post-fix.index .ai/pre-fix.tree .ai/fix.patch
```

An empty `fix.patch` means the files named are not the ones the fix changed.
The test has to fail for the reason the finding gives, on its assertion. One
that passes there reproduces nothing, and one that fails to import or collect
(it calls something only the fix adds) proves nothing either: send it back
to the fixer with that result. This holds whatever the finding's severity;
the fixer often cannot run commands itself, so this check is yours.

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

## Example workflows

**A feature in an existing Rails application**

> "Add a per-customer spending cap to the checkout flow."

```
Codex gpt-5.6-sol      reads the existing checkout code and recent logs,
                       breaks the request into stages
        |
Claude fable           designs: where the cap lives, what it touches, migrations
        |
Claude opus            implements the change and its tests
        |
Claude opus            reviews the frozen diff
Codex gpt-5.6-terra    reviews the same diff, independently
        |
Codex gpt-5.6-sol      consolidates both reports, drops duplicates, triages
        |
Claude opus            fixes the accepted findings only
        |
                       tests re-run, report
```

From the user's side that is one sentence to the agent. The artifacts land in
`.ai/`: the plan, each review, the consolidated finding list, and the triage
decisions.

**Feature with an API change**

> "Add pagination to the orders endpoint."

Design (Fable) → Implementation (Opus) → tests → 2 independent reviews →
triage (2 accepted, 1 rejected) → fix (Opus) → re-test → report.

**Typo**

> "Fix the typo in the README heading."

One edit, no design, no review. The orchestrator says so in the report.

**Review only**

> "Review everything on this branch since main with all three reviewers."

```bash
dev-orchestra review snapshot --base main
dev-orchestra review run
dev-orchestra review show
```

**Cheap dry run** — swap a reviewer to the offline mock provider:

```bash
dev-orchestra reviewer add --provider mock --id dry --role general
DEV_ORCHESTRA_MOCK_RESPONSE=NO_FINDINGS dev-orchestra review run --only dry
```

## Installing from a skill checkout

The installers from before the plugin still work and are unchanged. The plugin
is the shorter path; this one keeps a checkout that `git pull` upgrades.

```bash
git clone https://github.com/istb16/dev-orchestra.git
cd dev-orchestra
```

**Claude Code:**

```bash
./install/install.sh              # symlinks into ~/.claude/skills/
```

```powershell
.\install\install.ps1             # Windows
```

The installer links (or copies, with `--copy`) this repository into your skills
directory, so `git pull` upgrades the skill in place. Use `--project <path>` to
install into a single repository's `.claude/skills/` instead of globally; that
path is also added to the target repo's `.git/info/exclude`, so the nested
checkout never appears in *their* `git status` and never gets committed
(without it, `git add -A` there fails with "does not have a commit checked
out").

On Windows, prefer `install.ps1` over running `install.sh` in Git Bash: Git Bash
writes MSYS-style paths (`/c/...`) that native Python cannot open. Symlinks need
Developer Mode or an elevated shell; the installer falls back to a copy on its
own if it cannot link.

**Codex CLI:** without the plugin, the installer appends a short, marked
pointer block to `AGENTS.md` referencing this checkout:

```bash
./install/install.sh --codex                    # ~/.codex/AGENTS.md
./install/install.sh --codex --project /path    # <project>/AGENTS.md
```

`skills/dev-orchestra/SKILL.md` stays the single source of truth — the pointer
references it rather than duplicating it.

**Antigravity:** the installer links this checkout into Antigravity's plugins
folder. The root `plugin.json` is what makes the directory a plugin, and
Antigravity finds `skills/` under it on its own:

```bash
./install/install.sh --antigravity                    # ~/.gemini/config/plugins/dev-orchestra
./install/install.sh --antigravity --project /path    # <project>/.agents/plugins/dev-orchestra
```

```powershell
.\install\install.ps1 -Antigravity                    # Windows
```

`--gemini` (`-Gemini`) is the same switch. On Windows the installer makes a
junction, which needs no Developer Mode, then tries a symlink, then copies;
elsewhere it makes a symlink, then copies. It says which one it used, and a
copy has to be re-run after `git pull`. `--copy` always copies, and a copy
carries a `.dev-orchestra-install` file so that a later run knows it made it.
With `--project`, the entry goes into that repository's `.git/info/exclude`
under a marker comment, and the uninstaller removes it only when the marker is
there. The installer never replaces a link to somewhere else, a link to
nothing, or a directory it did not write, a clone included; it stops and says
how to remove it by hand. It also refuses to link while the checkout has a
`hooks.json`, `mcp_config.json`, `plugins.json`, `rules/` or `agents/*.md` at
its root, which Antigravity would load as well; a copy leaves `agents/*.md`
out. That check comes first, so a refused run leaves the existing install in
place. A clone made directly into the
plugins folder is already installed and needs no installer run.
`./install/uninstall.sh --antigravity` (with the same `--project`) removes the
install. Restart Antigravity after installing, upgrading or uninstalling: it
only discovers a plugin directory on startup. A linked install loads whatever
branch the checkout has, so look at an untrusted branch with `--copy` or from
a separate worktree. `dev-orchestra doctor` reports it when a later checkout
adds one of the entries above to a linked checkout, or to a clone in the
plugins folder.

A plugin can reach Antigravity three other ways. The Marketplace is curated by
Google: there is no user-added marketplace, and a listing goes through an
interest form. The Antigravity CLI installs a local plugin with
`agy plugin install /path/to/dev-orchestra` (or `/plugin install <local-path>`
inside a session), which stages a copy into
`~/.gemini/antigravity-cli/plugins/dev-orchestra/`; `agy plugin uninstall
dev-orchestra` removes it. It copies the whole directory it is given, as it
is: not the installer's payload list, and with `agents/*.md` left in. Run it
only on a clean checkout, one with nothing Antigravity would load besides the
skill (no `hooks.json`, `mcp_config.json`, `plugins.json`, `rules/` or
`agents/*.md`, which `python scripts/validate_skill.py` reports) and no
untrusted branch checked out. The copy does not follow `git pull`, so run it
again after pulling, on a checkout that is clean again. Finally, a
`plugins.json` in a customization root (`~/.gemini/config/plugins.json`, or
`.agents/plugins.json` in a project) can point Antigravity at a checkout kept
elsewhere. An entry names the parent directory that contains the plugin
directory, not the plugin directory itself:
`{"entries":[{"path":"C:/Projects","include_only":["dev-orchestra"]}]}` loads
a checkout at `C:/Projects/dev-orchestra`, while an entry whose `path` is the
checkout itself loads nothing; a `C:/` path works on Windows. This loads the
live working tree, like a link: a branch checked out later goes live on the
next restart, with the same untrusted-branch caution as a linked install, and
nothing guards it. The installer's refusal runs only when the installer links,
and `doctor` checks only the two installer locations, not `plugins.json`
entries or a copy staged by `agy plugin install`. The installer stays the
recommended route: its link works in the app, the IDE and the CLI, follows
`git pull`, is refused while the checkout would load anything besides the
skill, and is what `doctor` watches afterwards; the other routes have none of
those guards.

Optionally put the CLI on PATH, then verify:

```bash
export PATH="$PWD/bin:$PATH"      # then `dev-orchestra doctor` works anywhere
./bin/dev-orchestra doctor
```

**Upgrading** a checkout install:

```bash
cd /path/to/dev-orchestra
git pull
./bin/dev-orchestra doctor
```

A symlink install picks the new version up immediately; with `--copy`, re-run
the installer.

**Uninstalling** removes the skill link and the `AGENTS.md` block, and leaves
the configuration alone:

```bash
./install/uninstall.sh
```

```powershell
.\install\uninstall.ps1
```
