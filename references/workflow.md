# Workflow

<!-- contents: start -->

**Contents**

- [Stage selection](#stage-selection)
- [Artifacts](#artifacts)
- [Design questions](#design-questions)
- [Design](#design)
- [Design review](#design-review)
- [Approval](#approval)
- [Implementation](#implementation)
- [Test](#test)
  - [Reviewing alongside the tests](#reviewing-alongside-the-tests)
- [Reviews](#reviews)
- [Fix](#fix)
- [Re-test and re-review](#re-test-and-re-review)
- [Recording and reporting](#recording-and-reporting)
  - [Progress reports](#progress-reports)
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

**Skip** design for a typo, comment, string or
formatting change; one obvious line, a config value, a version bump; usually a
single-file change with clear intent and no contract change. **Run** it for
new behaviour or spec; an ambiguous requirement; several files or modules;
schema, migration, data model; API or contract; performance; a bug of unknown
cause (investigate first); architecture, dependency or build;
security-sensitive paths (auth, permissions, secrets, payments).

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
        ├── decisions.md            # the design questions and the user's answers, rendered from state.json
        ├── execution/
        │   ├── design-questions-request.md  # prompt for `run architect --questions`
        │   ├── design-questions.md # its question blocks, fed to `design questions record`
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
        │       ├── review-target-decisions.md  # the frozen decisions, only with design questions
        │       ├── review-target.json  # plan, request, sha256, round_id
        │       ├── <reviewer-id>.md
        │       ├── consolidated.md
        │       ├── consolidated.json
        │       └── rounds/
        └── state.json              # stage events, resolved model ids, plan approval, design questions
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

Only a command's arguments are read that way. A file you write yourself -- the
request a `--prompt-file` names -- and a shell redirect go where the path
says, so write them under the directory `workflow show` prints as
`Artifacts:` (`dir` in its `--json`): the request at
`<Artifacts>/execution/design-request.md`, then pass `--prompt-file
.ai/execution/design-request.md`, which resolves to it; a background review's
log with `review run --progress > "<Artifacts>/execution/review-run.log"
2>&1` (quoted: the path is absolute and may hold a space).
A request written at `.ai/execution/` itself is not read: `run` says the file
does not exist, names the path it resolved to, and says a file is there as
written.

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

Anything else directly in `.ai/` -- a `plan.md`, or an `execution/` holding
a request or a log the orchestrator wrote there itself -- belongs to no
workflow. The commands neither read it nor move it, and it stays until you
delete it.

**How the formats change.** The `.ai/` artifacts are part of the public
surface, and from 1.0 they change by addition only: a new version adds a key
or a file, a reader skips a key it does not know, and a key an earlier 1.x
release never wrote reads as what that file meant -- "unknown" rather than
zero where zero would be a claim. A key is not removed, renamed or given a new
meaning or type. A change that cannot be made by addition is a breaking change -- a
major release -- and it ships with a way through: a migration that moves the
old form into the new, or a refusal that names the old form and says what to
do with it. The files carry no format version for that reason: nothing reads one, and the rule above is what keeps an older workflow
readable. The `--json` output of the commands changes the same way: a key may
be added, as the `*_real` keys that appear only when a path is stored
elsewhere; a key is not removed, renamed or given a new meaning or type. The
whole of what is covered is under "Compatibility" in the README.

`.ai/` gets a `.gitignore` containing `*` on first use, so artifacts stay out of
the user's commits. Teams who want them reviewable can delete that file and
commit the directory; teams who never want it can add `.ai/` to the repo's own
`.gitignore`. Say which you did if you change it.

`jobs/<id>.activity` (and `.activity.1`, once a run passes 500 tool uses) is
what `jobs wait` reports while a detached run works -- each tool line with its
offset from the start (`s`) and its wall-clock time (`t`), from which the
stuck notice is measured ([Progress reports](#progress-reports)) -- and
`execution/review-run.log` exists only when you redirect a background
`review run --progress` there. The tool lines in both went through the same
allowlist and cleaning -- no model text, no free-text arguments -- and the rest
of the log is `review run`'s usual output. Both are under `.ai/`, which ignores
itself; a team that deleted `.ai/.gitignore` has to ignore them in its own
`.gitignore`.

Nothing in `.ai/`, and no `.dev-orchestra.yaml`, ever enters a review
snapshot — the skill's own files are not the change under review. A change to
the project file under any of its names, deleting it included, or to a file an
`instructions` entry reads, is still
noticed: the snapshot records those paths as `instruction_changes`, and the
round is high-risk, so `optimization.level` escalates and the `when:
high-risk` reviewers sit. Such a file can come with the branch under review
and change what the reviewers are told ([configuration](configuration.md#standing-instructions-instructions)).

## Design questions

Off by default (`design.ask_questions: false`). Turned on, the open points of a
design go to the user before the plan is written, instead of being settled
by the architect without anyone seeing the choice. `status` prints a `Design
questions:` line whenever the setting is on or something is recorded.

**When to ask.** Under `true`, always: your own questions when you have any,
then one architect round. Under `auto`, ask when the request has any of:

- an ambiguous requirement -- two readings that lead to different code;
- new behaviour or a new spec -- something the user has to want, not something
  the code already implies;
- several viable approaches that differ in cost, risk or what they rule out.

Say in one line whether you are asking and why ("Asking first: the request
does not say whether existing orders get a channel"), or why not ("No
questions: a bug with one obvious cause"). Under `auto` with nothing to ask,
go straight to the design; nothing is refused.

**The flow.**

1. Your own requirement-level questions, if any: write them as question blocks
   under `<Artifacts>/execution/`, record them with `--by orchestrator`, ask,
   and record the answers.
2. The architect's round: write `design-questions-request.md` from the
   template below, run it with `--questions`, record its answer with `--by
   architect`, ask, and record the answers. A second round is for what the
   answers opened up, not for asking the same thing again.
3. Write the design request with a `## Decisions` section holding
   `<Artifacts>/decisions.md` as it is, and run the design as usual.

Ask in batches: every open question of a round in one message, each with its
options and the recommendation, so the user answers once. Their "leave it to
you" -- for one question or all of them -- is `--delegate`. Record only what
they said: `--option` for the option they chose, `--text` for their own words,
verbatim. Never answer for them; this is theirs as the approval is (SKILL.md
rule 10).

The block format, the same for both askers:

```markdown
## Question
- Topic: <a few words>
- Question: <one or two sentences the owner can answer without reading code>
- Why it matters: <what in the design changes with the answer>
- Option A: <...>
- Option B: <...>
- Recommended: B -- <why>
```

One line per field, options `A` to `E`. An architect question needs `Why it
matters`, at least two options and a `Recommended` that names one of them; a
question of your own may leave out the options and the recommendation. A file
that says exactly `No questions.` records an architect round that asked
nothing.

`design-questions-request.md`:

```markdown
# Design questions

<the design request, without its Deliverable section>

## Answers so far

<paste <Artifacts>/decisions.md here, or write None.>

## Deliverable

Investigate the codebase as you would to design this, but do not write a
plan. List only the decisions the plan depends on that the request and the
answers above leave open, and that the owner -- not the code -- has to settle.
Ask nothing the code answers and nothing already answered. Print each as one
block in exactly this shape, one line per field:

## Question
- Topic: <a few words>
- Question: <one or two sentences the owner can answer without reading code>
- Why it matters: <what in the design changes with the answer>
- Option A: <...>
- Option B: <...>
- Recommended: <option key> -- <why>

Give two to five options, A to E. With nothing to ask, print exactly
`No questions.` and nothing else. Print to stdout; do not write a file and do
not modify any file.
```

```bash
dev-orchestra design questions record --by orchestrator --file .ai/execution/my-questions.md
dev-orchestra run architect --questions \
  --prompt-file .ai/execution/design-questions-request.md \
  --output .ai/execution/design-questions.md
dev-orchestra design questions record --by architect --file .ai/execution/design-questions.md
dev-orchestra design questions show --open
dev-orchestra design questions answer Q2 --option B
dev-orchestra design questions answer Q3 --text "Only for orders placed after March"
dev-orchestra design questions answer Q4 Q5 --delegate
```

`record` takes all the blocks or none: a malformed one exits 2 naming the block
and the field, and nothing is recorded. Fix the format by hand and keep the
content, or run the round again, which costs an attempt. It prints the ids it
gave, `Q1`, `Q2`, ... in order across the workflow. A question of yours is
numbered in round 0 until the architect has had a round, then in the round it
follows. `answer` records `--option B` as `B: <the option's text>`,
`--delegate` as `left to the architect`; several ids go together only with
`--delegate`. Changing an answer takes `--revise`, which keeps the earlier one
in the question's `history`. An unknown id or option, and a `--text` with a line
break, exit 2. A code fence around the blocks and a closing line after a
block's last field are ignored, an indented line under a field continues that
field, and a `Recommended` may name its key as `Option B` or `**B**`. A line of
prose with a field still to come in its block, and a list item that is not a
field, are refused as `not a \`- Field: value\` line`. With the setting
`false`, `record` still records and says the gate is off.

`run architect --questions` runs on the architect's seat -- its provider, model,
mode and `run.timeout_seconds.architect` -- but spends
`budgets.architect_questions` (3), not `budgets.architect`, and is recorded
under the stage `architect_questions`, so it never becomes the session
`--resume` continues. It refuses (exit 2) `--resume`, an `--output` on the plan,
and any role but the architect. `run` does not read what it printed: `record`
does.

**The gate.** While any recorded question is open, the `run architect` whose
`--output` is the plan exits 6 -- before it spends anything and before
`--detach` starts a worker; a worker checks again and writes the refusal into
its job. `--force` does not apply: the answers are the user's. Under `true` it
is also refused until an architect round is recorded, except a `--resume`
revision; an open question refuses that one too, because a revision past it
would contradict `decisions.md` -- answer it, or `--delegate`. An architect run
with no plan output, and a `--questions` run, are not gated. With answers on
record, a plan run whose prompt names neither `decisions.md` nor a `##
Decisions` section prints a note: the likeliest mistake is not passing the
answers on. `design approve` refuses the same way (exit 6, naming the open ids)
while a question is open, whatever wrote the plan: a question recorded after
the plan, or a plan not written through `--output`, cannot be approved past. It
checks the questions and takes the answers it records from one read, under
the lock it writes the approval under. `run implementer` refuses too (exit 6),
whatever the approval says -- one recorded before the answers were kept with
it, or while the setting was `false` -- before it spends anything and before
`--detach`, and a worker checks again.

| Setting | Recorded | `state` | The plan run |
| --- | --- | --- | --- |
| `false` | anything | `off` | runs; what is recorded is shown, not enforced |
| `auto` | nothing | `none` | runs |
| `auto` or `true` | an open question | `open` | refused, exit 6, `--resume` included |
| `true` | no architect round, nothing open | `round-required` | refused, exit 6, except `--resume` |
| `auto` or `true` | every question answered | `answered` | runs |

The setting is read at each run: turned to `false`, the gate stops at once;
turned back on, what is recorded gates again.

**Nobody watching.** With `design.require_approval: false`, or `auto` with
`CI` or `DEV_ORCHESTRA_UNATTENDED` set, nobody is there to answer, so the questions are asked and recorded but not waited for. Run the
rounds as usual, record what is asked, and do not stop to ask. The plan run
records every question still open as left to the architect (`left to the
architect: nobody was there to answer ...`), says so in a `note:`, and goes
on; `decisions.md` keeps the questions for someone to look at later. It does
so only once nothing else refuses the run -- after the round check and once
its attempt is spent, and with `--detach` before it starts the worker, which
does not do it again -- so a run refused with exit 6 or 3 leaves them open.
Under `true` the architect round is still required, and an answer that was
given is kept.

**The round cap.** After `design.max_question_rounds` (2) architect rounds,
`run architect --questions` and `design questions record --by architect` exit
6: present the open points together and ask the user how to proceed. `--force`
runs or records one more, and only when the user asks for it. On the run it
also overrides a spent budget, and names each one it overrides on stderr.

`decisions.md` is rewritten from `state.json` on every change and says not to
edit it. It is what the design request, the revision requests and the design
reviewers read; the gate reads `state.json`. `status --json` reports the
questions under `design_questions`, never as a reason to stop:

| Field | Meaning |
| --- | --- |
| `mode` | `off`, `auto` or `on`, from `design.ask_questions` |
| `state` | `off`, `none`, `open`, `round-required` or `answered` |
| `pending` | `true` only while a question is open |
| `open` | the ids not answered yet |
| `answered`, `total` | how many are answered, of how many |
| `architect_rounds`, `max_rounds` | the architect rounds recorded, and `design.max_question_rounds` |
| `decisions_file` | `decisions.md`, relative to the root; `null` until something is recorded |

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

## Decisions
<paste <Artifacts>/decisions.md here, unchanged; leave this section out when no design questions were asked>

## Deliverable
Print the complete plan to stdout as Markdown, with these sections: Goal,
Current Behavior, Investigation, Root Cause, Proposed Change, Decisions, Files
to Modify, Data/API Impact, Compatibility, Test Strategy, Risks,
Implementation Steps. Under Decisions, list each decision from the Decisions
section above and each open point you settled yourself: what it is, whether
the user or you decided it, and why; write `None.` when there are none. The
user's decisions are fixed: design within them.
Keep it short: everyone after you reads the whole plan. Current Behavior,
Investigation and Root Cause are bullets that cite `path:line` instead of
quoting code. Test Strategy is one line per test: its name and what it
asserts. Each Implementation Step names the files and functions it touches.
When the work splits into pieces that change different files, or the plan has
more than four Implementation Steps, add a section `## Units` holding one
fenced `yaml` block: `units:`, a list of entries with `name` (a slug), `kind`
(feature, fix, refactor, docs, test, ci or integration), `files` (the paths or
globs it changes) and `depends_on` (the units it needs first). Under
Implementation Steps, give each unit a section `### Unit: <name>` with its
steps. Units that do not depend on each other, however indirectly, must not
list the same file; files every piece touches, such as CHANGELOG, go to one
`integration` unit that depends on every other. Write a long plan's parts as
units chained by `depends_on`, one per natural group of steps. Otherwise leave
`## Units` out.
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

**Designing per unit (optional).** By default one architect writes one plan,
with a section per unit. When the units are clear before the design, an
architect can design each one instead: `run architect --unit <name> --output
.ai/execution/plan-<name>.md`, its request asking for a plan whose `## Units`
holds that unit alone. `units join --part <name>=<path> ... --output
.ai/plan.md` then makes one plan of them, and the design review and the
approval run once, on that plan. The per-unit runs of one design take one
attempt from `budgets.architect` between them, for up to 8 units; each unit
past them takes one more.

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

With design questions on record, each reviewer is also given the user's
decisions, after the design request, as premises: the plan is not reported for
following them. A reviewer who thinks a decision itself is wrong reports one
finding with `Category: decision` and the decision's id in `File`
(`decisions.md#Q3`). Do not triage that one on your own judgement: put it to
the user. If they decide again, record it with `design questions answer Q3
--revise ...`, triage the finding `accepted` and fold it into the revision; if
they keep the decision, triage it `rejected --note "user kept Q3"`. The
decisions are part of the round's sha, so a decision changed over an unchanged
plan is a new review target. They are frozen beside the plan in
`reviews/design/review-target-decisions.md`, which `review-target.json` names
under `decisions`; `design approve` labels findings from a round reviewed under
other answers as of an earlier revision. Without questions, the prompt and the
sha are what they always were.

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

`<Artifacts>` in the templates is the directory `workflow show` prints:
write it out in the prompt. The model reads files where they are, and
`.ai/plan.md` resolves only in a command's arguments.

`design-revise-request.md`, for a fresh run (the brief alone is not a prompt):

```markdown
# Revise the plan

<the original design request, unchanged but for its `## Decisions`, replaced by the current <Artifacts>/decisions.md>

Read <Artifacts>/plan.md and revise it. Keep every section it already has.

<paste <Artifacts>/execution/design-fix-brief.md here>

For each finding: say whether you addressed it and how, or why it is not a
problem. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which finding it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown, leaving the sections
no finding touches as they were. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

`design-resume-request.md`, for a continued session -- it does not repeat the
plan, which the session already holds, but has it read once, because you may
have trimmed what the architect printed and the findings point into the file:

```markdown
# Revise the plan

You are continuing the session in which you designed this plan. Read
<Artifacts>/plan.md once before changing anything: it is the plan you printed, as
saved by the orchestrator, and the findings below refer to its sections.
Do not re-read code you already read unless a finding contradicts what you
remember. If this workflow has <Artifacts>/decisions.md, read it once; its
answers are fixed. Keep the plan's `## Decisions` in line with it.

<paste <Artifacts>/execution/design-fix-brief.md here>

For each finding: say whether you addressed it and how, or why it is not a
problem. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which finding it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown, leaving the sections
no finding touches as they were. The caller captures
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

With `design.require_approval` asking for it, nothing is implemented until the
user has approved the plan. `true` always asks. `auto`, the default, asks too,
unless nobody is watching (`CI` or `DEV_ORCHESTRA_UNATTENDED` is set), the plan
touches nothing high-risk, and the design review left nothing above Low -- it
was skipped as unneeded, or its latest round reviewed this plan and only Low
findings are open. `status` gives the answer and its reason on the `Plan
approval:` line; `not required` there means go on to the implementer. When it
asks with nobody watching, the line says which condition failed. When it asks,
put to them:

- the plan's **Goal**, **Proposed Change**, **Files to Modify** and **Risks**;
- its **Units**, when it has them: each unit's kind, files and dependencies,
  and the waves `units check` prints -- the approval covers the split too;
- any design findings still open, and whether they came from a review of this
  plan or of an earlier revision (`design approve` says which);
- the design questions a run nobody watched left to the architect, when there
  are any: nobody answered them (the pending line and the exit-5 refusal name
  them);
- the standing instructions in force, the `Instructions:` line of `status`,
  when there are any: the pending `Plan approval:` line then reads `present
  <plan> and the instructions in force to the user and ask`;

and ask. Only their explicit yes is recorded, with `dev-orchestra design
approve` -- never on your own judgement, and never to get past a refusal. If
they ask for changes, revise the plan (and re-review it if the design review is
on), then ask again: the revision hashes differently, so the old approval no
longer counts.

A change to a recorded decision goes the same way. Record the user's new words
first, with `design questions answer Q2 --revise --text "..."`, which rewrites
`decisions.md`; then revise the plan as below, naming the decisions that
changed by id.

A revision the user asked for has no fix brief, so its two files carry their
words instead. `design-change-request.md`, for a fresh run:

```markdown
# Revise the plan

<the original design request, unchanged but for its `## Decisions`, replaced by the current <Artifacts>/decisions.md>

Read <Artifacts>/plan.md and revise it. Keep every section it already has.

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

Print the complete revised plan to stdout as Markdown, leaving the sections
no finding touches as they were. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

`design-change-resume-request.md`, for a continued session:

```markdown
# Revise the plan

You are continuing the session in which you designed this plan. Read
<Artifacts>/plan.md once before changing anything: it is the plan you printed, as
saved by the orchestrator. Do not re-read code you already read unless a
change below contradicts what you remember. If this workflow has
<Artifacts>/decisions.md, read it once; its answers are fixed. Keep the
plan's `## Decisions` in line with it.

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

Print the complete revised plan to stdout as Markdown, leaving the sections
no finding touches as they were. The caller captures
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
(no design stage) means no gate. While a recorded design question is open it
refuses with exit 6 instead, whatever the approval says (see Design questions).

An approval is of the plan text alone (sha256) and of the design review round
it was given over. It goes stale when the plan changes (`plan-changed`) or when
`review run --design` runs again afterwards, even over the same plan
(`reviewed-since`). Re-consolidating and triaging do not. With design
questions, it is also of the answers as they were (`decisions_sha256`, `null`
when none were asked): an answer revised afterwards makes it stale even over
an unchanged plan (`decisions-changed`) -- revise the plan and ask again. An
approval recorded before this key existed never goes stale by it.

`status --json` reports it under `design_approval`:

| Field | Meaning |
| --- | --- |
| `required` | whether `design.require_approval` asks for this plan now |
| `mode` | `on`, `off` or `auto` |
| `reason` | why `auto` decided as it did; `null` under `on` and `off` |
| `requirement` | the two above as one line, as `status` prints it |
| `nobody_watching` | whether a question now would go unanswered (`false`, or `auto` with nobody watching) |
| `unattended_questions` | ids of the design questions a run nobody watched left to the architect |
| `state` | `pending`, `stale`, `approved`, `no-plan`, `not-required`, or `implemented-unapproved` |
| `pending` | `true` when the user has to be asked (`pending` or `stale`) |
| `stale_reason` | `plan-changed`, `reviewed-since`, `decisions-changed`, or `null` |
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

Follow the plan in <Artifacts>/plan.md.

Rules:
- Match the conventions already in this codebase; do not introduce new ones.
- Make the minimal change that satisfies the plan.
- No unrelated refactoring, renaming, or formatting.
- Add or update the tests the plan's Test Strategy calls for.
- Read only what a step needs: start from the files and lines the plan names,
  and read a large file by the range you need.
- Run lint and type checks once per step, not after every edit.
- Run those tests and report the result.
- If the plan is wrong or impossible, STOP and explain why. Do not redesign.

Report: files changed, tests added/updated, test output, anything you could not do.
```

```bash
dev-orchestra run implementer --prompt-file .ai/execution/implement-request.md
```

**A long plan runs in parts.** A run costs more per tool call the longer it
gets, because every call re-reads what the run has read so far: on this
project's own records, runs of 160 or more tool calls cost about 1.5 times as
much per call as runs under 80. So a plan with more than four
Implementation Steps is split into as many serial units, chained by
`depends_on`, as it has natural groups of steps; there is no cap on the
number of parts. Each unit is a separate, shorter run that starts from the
tree the one before it left: nothing could run beside it, so it runs in place,
with no worktree and no merge. The whole pass takes one attempt from
`budgets.implementer` (default 5), and only a rerun of a part takes another;
`total_delegated_runs` still counts every run.

**Work units.** When the approved plan has `## Units`, run `units check`
first; it refuses (exit 2) units that cannot run and prints the waves. Then,
until every unit is merged:

1. Start each unit `units status` lists as ready, up to
   `implementation.max_parallel`, with `run implementer --unit <name>
   --prompt-file .ai/execution/implement-request.md --detach --json`. The CLI
   puts the unit's name, files and plan section in front of the prompt, so one
   request serves every unit.
2. `jobs wait <id> <id> ... --any` returns when the first one finishes. To
   relay only new activity, give each job its own cursor, `--since <id>=<n>`:
   the last `next:` line is that command.
3. `units merge <name>` brings a finished unit into the working tree. On exit
   7 it conflicted: run `run review_fixer --prompt-file
   .ai/execution/units/<name>-conflicts.md` in the main tree, then `units
   merge <name> --resolved`. The resolver spends `budgets.review_fixer`. A
   conflict without markers (a binary file, a delete against a change, a
   symlink or submodule) keeps the working version, and `--resolved` refuses
   while that file is unchanged: when keeping that version is the resolution,
   say so with `--keep <path>` for each such file.
4. Back to `units status` for the units now ready.

A unit runs in a worktree of its own when another could run beside it, and in
place when nothing could -- no unit is running and every unit not yet merged
depends on it; a unit run in place is merged when it ends, and nothing else
starts while it runs. The units of one plan take one attempt from
`budgets.implementer` between them, and only a rerun of a unit takes another;
a new approval or `budget reset` begins a new pass. The light-tier rule below
applies to each unit on its own `files`. If a unit fails, it is not merged
and the units that depend on it are refused: let the running units finish and
merge them, then stop and report; the failed unit's diff stays in
`.ai/execution/units/<name>.diff`. `units clean` removes the worktrees units
left behind, and records a unit whose worker is gone as failed. A plan changed
while an earlier plan's unit is running or holds a worktree is refused (exit
2) until those jobs are cancelled or `units clean` has run. Test and review
then run once, on the merged tree, as without units.

**Tests after the merge.** By default each unit runs its own checks in its
worktree (`implementation.unit_tests: worktree`). Units that run side by side
and share a test database, a port or a similar resource can trip over each
other there; `implementation.unit_tests: after_merge` has each unit write its
code and tests, run lint and type checks only, and report the tests as "not
run". The unit header says so, and that overrides the request's own "run the
tests". The suite then runs once in [Test](#test), after every unit is merged.
A unit already told not to run checks (a link into its worktree failed) keeps
that stricter wording. Git-ignored files a worktree needs for its checks, such
as `config/database.yml` or `.env.test`, are listed in
`implementation.worktree_files` and copied in when the worktree is made; one
that is missing, names a `..`, or is reached through a link is skipped with a
`note:`.

**Standing instructions.** The CLI adds the `instructions` text set for a
role ([configuration](configuration.md#standing-instructions-instructions))
to the prompt itself, under the heading `## Project instructions`; do not
paste it into a request. A run of the architect, the implementer or the review
fixer gets it in front of the request; a unit run gets the unit header first,
then the instructions, then the request. A reviewer gets it after the task's
rules and before the change, and may only add things to look at. A detached
worker gets it once, from the run that started it; `--resume` never gets it
again, so a session started before an instruction was added never sees it --
run fresh to pick it up. `run <reviewer-id>` and `run orchestrator` get none.

**Small work goes to the light tier.** When the role has a `light` tier
(`config show`; Model tiers in `references/configuration.md`), run the
implementation with `--tier light` when the plan lists at most three Files to
Modify and none of them is on a high-risk path (`optimization.high_risk_paths`
and `extra_high_risk_paths`). A retry after a light run failed runs on the
role's own model.

Implementer failure is fatal: stop, report what happened, leave the tree in a
state the user can inspect.

## Test

Use the project's own commands, discovered from the repo (`package.json`,
`Makefile`, `Rakefile`, `pyproject.toml`, CI config, CONTRIBUTING). Prefer the
narrowest command that covers the change, then broaden if time allows. Never
invent a test command; if you cannot find one, say so.

Under `implementation.unit_tests: after_merge` the units did not run the
tests: run the suite once here, after every unit is merged.

A failing suite stops the pipeline. Fix it or report it — do not review a broken
tree.

<a id="reviewing-alongside-the-tests"></a>

### Reviewing alongside the tests

With `review.start_before_tests: true` (default `false`) the code review round
does not wait for the tests. Right after the implementer (or the review fixer)
finishes, take the snapshot and start `review run` in the background, then run
the tests and record them as usual. Measured here, the tests took a median 381s
from the end of an implement or fix run to their record, and a round 117s, so
the round is hidden behind them; only 3 of 219 recorded test runs failed, so
the round thrown away with a failure is rare.

The round counts only once the tests pass on the tree it reviewed. With the
option on, `state record test` (and `re-test`) also keeps the working tree as a
git tree object, as a snapshot does, and `review triage` and `review fix-brief`
refuse (exit 2) until the last result recorded on the snapshot's tree is `ok`.
`review status` says where the round stands on a `tests on the reviewed tree:`
line and in `round_tests`:

- `pending`: no test result since the snapshot. Record it, then look again.
- `failed`: the tests failed on that tree. The round is not used: fix, re-test,
  take a new snapshot and review again. A pass recorded afterwards on the same
  tree (a flaky test run again) makes it usable.
- `changed`: results were recorded since the snapshot, but on another tree --
  say a lint fix made while the tests ran. The reviewers did not see that
  change, so the round is not used either: re-test, snapshot, review again.
- `usable`: triage it. Fixing it changes the tree afterwards; that does not
  take back the result the round was judged on.

The gate of a round started this way reads the result recorded on the
snapshot's own tree, not the last one of all: a failure recorded before a fix
is about a tree nobody is reviewing. A failure recorded on the snapshot's tree
still refuses the round below `quality`, as it always has.

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

**Small fixes go to the light tier.** When `review_fixer` has a `light` tier,
run it with `--tier light` when every accepted finding is medium or lower and
the brief cites at most three files. A retry after a light run failed runs on
the role's own model.

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
  "wanted": ["F1"],
  "re_review_recommended": true,
  "iteration_budget_exhausted": false,
  "final_fix": null,
  "final_fix_pending": false,
  "whole_change_recheck": true,
  "round_scope": "whole",
  "next_review": "fix"
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

With `review.whole_change_recheck` on (the `standard` and `quality` presets),
follow `next_review` and the `next:` line of `review status`. Anything above
Low keeps the loop open (`wanted`, which can list a Medium that `blocking`
does not). A fix-only round that finds nothing above Low is not the end: fix
and re-test its accepted Lows, then take a plain `review snapshot`, which
takes the whole change by itself, and review that. That round may be one past
`max_review_iterations` -- the one spare round -- so a round 3 with a budget
of 2 is expected there. A whole-change round that finds nothing above Low
finishes the review once its accepted Lows are fixed and re-tested
(`final_fix` goes `pending` → `retest` → `done`, and `status` then says
`stop-and-report` with `review finished with nothing above Low`). See
[Closing on the whole change](reviews.md#closing-on-the-whole-change).

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

### Progress reports

A long run leaves the user looking at nothing unless the orchestrator says
what is happening. What it says is a setting, `progress.*`
([configuration](configuration.md#field-reference)), and `status` prints it at
the start of a session: `Progress: auto, every 180s, stuck notice after 600s
(progress.*)`.

| `progress.updates` | Detached runs | Each report (every `jobs wait` exit 4) | `review run` |
| --- | --- | --- | --- |
| `auto` (default) | implementer, review_fixer, a big architect run | elapsed time, tool count, context tokens, the last tool lines | `--progress` when run in the background; its new lines relayed |
| `true` (`on`) | the same, and every architect run, `--questions` and `--resume` included | the same | always in the background with `--progress`; its new lines relayed |
| `false` (`off`) | the same as `auto` | only a `may be stuck` note, when there is one | `--progress` when run in the background; only its `may be stuck` lines relayed |

`progress.interval_seconds` (default 180, 30 to 3600) is how often: it is the
wait of `jobs wait <id> --since <n>` without `--timeout`, and how often a
`review run --progress` checks its reviewers. The `next:` line of each report
names the wait that follows.

`progress.quiet_notice_seconds` (default 600; `0` turns it off) adds `may be
stuck: no new tool line for 12m03s` to a report on a run that has shown no new
tool line for that long, as a `note:` line in `jobs wait` and as `[<reviewer>
+mm:ss] may be stuck: ...` in a review's log. It comes again with every report
while the run stays silent, under every value of `progress.updates`. Relay it
and keep waiting: it is never a reason to cancel, since a long silent test run
or a model thinking without tools looks the same. The deadlines still decide
when a run is ended. What the notice can and cannot see: `references/limits.md`
("Not blocking in the first place").

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
| `run architect` exits 6 | A design question is open, or `design.ask_questions` is `true` and no architect round is recorded, or the question rounds are at their cap. Ask the user; record their words with `design questions answer`. Do not retry and do not answer for them |
| `run implementer` or `design approve` exits 6 | A recorded design question is open. Ask the user and record their answer; revise the plan if it changes it, then ask for approval again |
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
out"). The entry goes in under a `# added by dev-orchestra install --claude`
comment, and the uninstaller removes it only when that comment is right above
it. An entry already there without the comment is not added again, and the
uninstaller leaves it untouched.

A re-run, and the uninstaller, replace only what the installer made: a link
to this checkout, or a copy, which carries a `.dev-orchestra-install` file.
Anything else there -- a link to somewhere else, a directory of your own, a
clone, or a copy without that file -- is left in place: the installer says on
stderr that the installer did not write it and that it must be removed by
hand, names the path, and exits 1. A clone
made directly into the skills directory is already installed; the installer
run from it stops instead of deleting it.

On Windows, prefer `install.ps1` over running `install.sh` in Git Bash: Git Bash
writes MSYS-style paths (`/c/...`) that native Python cannot open. Symlinks need
Developer Mode or an elevated shell; the installer falls back to a copy on its
own if it cannot link. Git Bash's `ln -s` copies the whole checkout, `.git`
included, and reports success; `install.sh` notices that it got no link,
replaces the copy with what a copy carries, and says so. A full copy that an
earlier `install.sh` left there looks like a clone, so it is left in place;
the refusal says how to remove it once you have checked it holds nothing of
yours.

**Codex CLI:** without the plugin, the installer appends a short, marked
pointer block to `AGENTS.md` referencing this checkout:

```bash
./install/install.sh --codex                    # ~/.codex/AGENTS.md
./install/install.sh --codex --project /path    # <project>/AGENTS.md
```

`skills/dev-orchestra/SKILL.md` stays the single source of truth — the pointer
references it rather than duplicating it.

A re-run replaces the block, and `uninstall --codex` removes it; the rest of
`AGENTS.md` keeps its text, its line endings and a UTF-8 BOM if it has one.
A file whose last line has no newline gets the block on a line of its own,
and the block then ends without a newline, so that uninstalling gives the
file back as it was. `install.ps1` reads and writes it as UTF-8 in Windows
PowerShell 5.1 as in PowerShell 7, and leaves a file that is not UTF-8
untouched -- UTF-16 and UTF-32 included, and text in another encoding after
a UTF-8 BOM -- with a note to save it as UTF-8 first.

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
