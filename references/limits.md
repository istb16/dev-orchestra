# Limits: stalls, timeouts and budgets

<!-- contents: start -->

**Contents**

- [Stalls](#stalls)
  - [Why a timeout was not enough](#why-a-timeout-was-not-enough)
  - [Two deadlines, because "slow" and "wedged" differ](#two-deadlines-because-slow-and-wedged-differ)
  - [Seeing a stall from outside](#seeing-a-stall-from-outside)
- [Budgets](#budgets)
  - [A change too big to review](#a-change-too-big-to-review)
  - [The change body in the prompt](#the-change-body-in-the-prompt)
  - [Surrounding context within the budget](#surrounding-context-within-the-budget)
  - [Measuring what surrounding context does](#measuring-what-surrounding-context-does)
  - [What the runtime budget counts](#what-the-runtime-budget-counts)
  - [What a delegated run's tool activity can and cannot say](#what-a-delegated-runs-tool-activity-can-and-cannot-say)
  - [Re-fetching the source: reported as tool activity, not counted, not limited](#re-fetching-the-source-reported-as-tool-activity-not-counted-not-limited)
- [No progress](#no-progress)
- [Not blocking in the first place](#not-blocking-in-the-first-place)
- [The verdict](#the-verdict)
- [Feeding it a lot of text](#feeding-it-a-lot-of-text)
- [What a run costs](#what-a-run-costs)
- [How hard to try to be cheap](#how-hard-to-try-to-be-cheap)
  - [Did it work?](#did-it-work)
- [What is still not covered](#what-is-still-not-covered)

<!-- contents: end -->

Two failure modes matter more than they look, because in both of them the
pipeline appears to be working:

* **A delegated agent stops responding** and nobody notices, because a blocked
  call is indistinguishable from a slow one.
* **A loop never ends** — review, fix, re-review, or the quieter fix→test→fix —
  burning tokens without converging.

Everything below exists for those two. The guiding rule: a guard that only
*advises* is not a guard. Every limit here is enforced by the action that would
breach it, because the first version advertised the review budget from a query
command and enforced it nowhere, so it never stopped anything.

## Stalls

### Why a timeout was not enough

`subprocess.run(..., timeout=...)` kills only the direct child. `claude` and
`codex` spawn their own children (ripgrep, node, git), which survive. It then
calls `communicate()`, which waits for EOF on pipes a surviving grandchild still
holds — so the timeout machinery could block forever on the very failure it
exists to handle. `tests/test_execution.py` reproduces that shape with a real
grandchild and asserts the call returns in seconds.

Runs now go through `orchestrator/execution.py`, which gives the child its own
process group, kills the whole group on a breach (`taskkill /T` on Windows,
`killpg` elsewhere), and drains output with daemon threads it can abandon.
stdin is written from its own thread because a review prompt with an inlined
diff is several times larger than a pipe buffer. The short questions
detection asks a CLI (`--version`, `--help`, `agy models`, `codex debug
models`) go the same way, so `doctor` and the checks before a run cannot hang
on a helper such a query started either.

The same holds when the CLI exits cleanly but leaves something running that
still holds its output -- a dev server the implementer started in the
background, say. The readers get a few seconds to finish; a pipe whose reader
is still blocked is never closed, because closing it waits for that reader,
and so for the process holding the pipe, on any platform. From then on the
reader drops what it reads, so a process that keeps writing neither blocks on
a full pipe nor fills memory, and what it wrote after the CLI exited is left
out of the run's output, with a warning giving its size. What is left is then
stopped:
on POSIX its process group is signalled as on a breach, and the run warns that
it was stopped. On Windows it cannot be reached once the CLI has exited
(`taskkill /T` needs the parent), so it is left running and the run reports
`orphans_possible` with a warning to check for it. Either way the CLI's exit
code stands.

### Two deadlines, because "slow" and "wedged" differ

| Deadline | Config | Meaning |
| --- | --- | --- |
| Total, `run` | `run.timeout_seconds.<role>` (implementer 3600, others 1800) | Hard cap on one `run` of a role |
| Total, review | `review.timeout_seconds` (1800) | Hard cap on each reviewer: `review run` and `run <reviewer-id>` |
| Idle | `review.idle_timeout_seconds` (300) | No output for this long → wedged; reviewers and read-only `run`s |
| Idle, write `run` | 1200, or `review.idle_timeout_seconds` if larger | The same, for a `run` that may change files |

`--timeout` replaces either total for one call. The implementer has an hour
because measured implementer runs went past half an hour and one was killed at
1800s. The idle deadline stays shared: it measures silence, which does not
grow with the task.

Except for a run that may change files -- the implementer, the review fixer,
or any `run --mode implement` -- whose idle deadline is 1200s (20 minutes),
or `review.idle_timeout_seconds` when that is larger. Such a run runs the
tests or a build, and Claude's stream prints nothing while a command runs, so
300s would kill a healthy run whose suite takes longer. No idle deadline at
all would leave a wedged implementer running silently until its total
deadline, an hour. 1200s is the trade: a command quiet for longer than that
is still killed as a stall, and a wedged run is noticed in twenty minutes
rather than sixty. `--idle-timeout` or the role's `options.idle_timeout`
sets another value.

Any output counts, not only a whole line: a CLI printing dots, or redrawing a
progress bar, is not silent. Output is read as it arrives, not a line at a
time.

The idle deadline is the useful one: a working agent keeps producing, a wedged
one goes silent, so a stall surfaces in minutes instead of half an hour.

**It only applies where a healthy run actually streams.** That was measured, not
assumed:

| Command | First output | During the run |
| --- | --- | --- |
| `codex exec` | 0.4s of an 11.5s run | keeps ticking (≤3.5s gaps) |
| `claude -p --output-format stream-json` | 2.5s of a 6.5s run | `thinking_tokens` events until the answer starts, then nothing until it is finished |
| `claude -p --output-format stream-json --include-partial-messages` | — | ≤1.7s gaps, the answer included |
| `claude -p --output-format text` | **8.1s of an 8.9s run** | nothing until the end |
| `agy --output-format stream-json -p` | at once (`init`, the prompt step) | a line per tool step and per answer chunk, nothing while the model thinks (≤6s gaps on a 12s run) |

The `thinking_tokens` events stop once the model starts writing its answer, and
the answer arrives in one piece when it is finished. On a 17k-character answer
that left the plain streaming format silent for 141s of a 191s run; a plan
three times that size crosses the 300s idle deadline while perfectly healthy.
So the Claude adapter asks for partial messages too, when `claude --help` lists
the flag: on the same prompt the largest gap was 1.7s. The price is stdout
about 8× larger (810 KB against 97 KB there), which the adapter reduces to the
answer as before.

An idle deadline on the text row would kill healthy runs, so an adapter must
declare `streams_progress = True` before one is applied to it, and only on
evidence. Where it does not apply, the total deadline is the only protection —
which is exactly why the observability below matters.

agy takes no idle deadline until a long run is measured. Its stream reports
tool activity, but a model step that is not the last prints a single line when
it ends, so generating a tool call's arguments — a large `write_to_file`
included — is silent, and only short runs were measured. A wedged agy run
surfaces at the total deadline.

A killed run is reported as `stalled` rather than merely failed, and if the
process group did not fully exit you are warned that orphans may remain.

Both deadlines run on `time.monotonic()`, which counts sleep on Windows and not
on Linux or macOS. So on Windows a run that sleeps past its deadline is stopped
when the machine wakes, while elsewhere the deadline waits out the sleep. The
runtime budget leaves sleep out on every platform; see
[What the runtime budget counts](#what-the-runtime-budget-counts).

### Seeing a stall from outside

A blocked orchestrator cannot rescue itself, so stages are written down
*before* they start, with a deadline and a pid:

```bash
dev-orchestra status          # in another terminal, or after a crash
```

That gives three things a completion-only log could not:

* a stage still running well past its deadline,
* a stage whose process is **gone** — it crashed without recording an outcome,
  which is the silent case that used to leave no trace at all,
* an accurate picture after the orchestrator itself dies.

Reading `status` clears entries whose process has gone and records them as
`abandoned`, so they show up in `state show` instead of appearing to run
forever.

## Budgets

Enforced by the action, refusing with **exit code 3**.

| Limit | Config | Enforced by |
| --- | --- | --- |
| Review rounds | `review.max_review_iterations` (2) | `review run` |
| Design review rounds | `review.design.max_iterations` (2) | `review run --design` |
| Change size | `review.context.max_chars` (400,000 chars) | `review run`, `review run --design` |
| Attempts per stage | `budgets.architect` (3), `.implementer` (5), `.review_fixer` (4), `.test` (8) | `run <role>`, `budget consume <stage>` |
| Delegated runs in a workflow | `budgets.total_delegated_runs` (40) | every `run` |
| Delegated runtime | `budgets.max_runtime_seconds` (14400) | `run <role>`, `review run`, `review run --design`, `budget consume` |

Exit 5 is not a budget: `run implementer` waits for `design approve`; see
`references/workflow.md`.

A round budget refuses the next review, not the revision or fix after the
round that reached it: that round's findings still go into the plan once more,
or get fixed and re-tested, and only then does `status` say stop. For the same
reason a spent attempt budget is a `status` reason only while that stage is
still needed: `architect` only while there is no plan, `review_fixer` only
while the last round's fix has not been made.

The last two are backstops for cycles that delegate: every run that consumes an
attempt — `run <role>` and `budget consume <stage>` — counts against the first,
every run that reaches its end with a measurement against the second. A cycle
that delegates nothing is bounded by neither; the round, attempt and no-progress
rules above are what stop the cycles this tool knows about.

`review.context.inline_chars` is not in that table because it refuses nothing:
over it the change body is handed over as a file and the round is recorded
`partial` — see [The change body in the prompt](#the-change-body-in-the-prompt).

Runtime is charged from the measurement each run produces, so a run killed
before it can record one — `jobs cancel`, Ctrl-C, an OOM kill — is charged
nothing. On `run <role>` that costs an attempt and a delegated run before the
child starts, so the first two rows hold it. Reviewer runs consume neither, and
the round only advances once a round is consolidated: retrying the same snapshot
stays in the same round. So a `review run` or `review run --design` killed
before it consolidates is bounded by nothing — not the round budget, not the
delegated-run count, and not runtime.

`run` and `review run` consume their own budgets. Stages the orchestrator
performs itself — running the test suite, above all — must claim theirs:

```bash
dev-orchestra budget consume test     # exit 3 when spent
dev-orchestra budget show
dev-orchestra budget reset            # start the budgets again
```

A ledger idle for `budgets.session_idle_reset_seconds` (6h) is treated as a
finished workflow, so the next request starts with full budgets without anyone
remembering to reset.

Either reset clears the budgets only. The token account behind `tokens show`
is carried across, because it records what the work cost rather than what is
left to spend, and clearing it made a workflow that paused for six hours
report the runs before the pause as free. So the account covers the whole
workflow, across every reset it has been through, and no reset clears it; only
deleting the workflow (`workflow remove`) does.

A separate account means a separate workflow: `dev-orchestra --workflow <id>`
gives the work its own `.ai/workflows/<id>/`, and therefore its own ledger,
budgets and account.

`--force` overrides a refusal. It is there for a human who has decided to
override; the skill tells the orchestrator not to reach for it.

### A change too big to review

`review.context.max_chars` (400,000) is the most change body a round will send
at all: the diff for a code round, the plan **and the request it answers** for
a design one — both go into every reviewer's prompt whole, so measuring only
the plan would let a round past the limit on a technicality. Over it,
`review run` exits 3 before any reviewer starts, before the round is charged,
and — on the design path — before the plan is frozen, so the reports and triage
of the previous round survive the refusal.

400,000 characters is roughly 100k tokens: half a 200k window spent on the
change alone, and four times the largest prompt this repository has ever
recorded (99,814 chars). **The default refuses nothing anyone here has run.**
Characters are the unit because they are the unit everything else measures in
(`inline_chars`, `prompt_chars`), and because the standard library
cannot count tokens. The conversion is not constant: CJK-heavy text is two to
four times as many tokens per character, so the same budget is that much looser
for it.

This is a refusal rather than a trim. Dropping hunks to fit would hand a
reviewer a change it cannot judge — nothing in the output would say which
parts were never shown — so the tool says *not reviewed* instead of calling an
incomplete review complete. The ways under the limit are all a person's:
`--base <rev>`, `review.exclude`, splitting the change, or a shorter plan.

**`--force` is the human's, and in an automated workflow that means no review
runs at all.** The skill tells the orchestrator to report a refusal and stop,
so a change over `max_chars` ends the run with nothing reviewed, and that is
what the orchestrator reports. `status` answers `stop-and-report` until a round
actually reviews the change — a later refusal does not clear it, and neither
does bookkeeping — so a clean consolidation from an earlier round cannot be
mistaken for this change having been reviewed.

When a human does force it, the round runs and every record of it says so:
`over_budget` on each reviewer entry and on `consolidated.json`'s `snapshot`
block, a `Change:` line in `consolidated.md`, and a line in `review status`.
What it does **not** promise is that forcing makes every reviewer `partial` —
that depends on `review.context.inline_chars`, not on this one. With the
defaults the two are the same number, so anything over `max_chars` is over the
inline limit too and does go over as a file. That is a fact about the shipped
configuration and not about the code: raise `inline_chars` above `max_chars`,
or lower `max_chars` below it, and a forced round is over budget and still
delivered inline. The refusal message says which of the two applies to the
change in front of it.

### The change body in the prompt

`review.context.inline_chars` (400,000) is how much of the change body goes
into the reviewer's prompt. At or under it the body is inlined and the round
can be clean; over it the reviewer is handed the path of the frozen snapshot
instead, and the round is recorded `partial` whatever comes back — see
[Coverage](reviews.md#coverage) for why the verdict cannot be taken from the
answer.

The default matches `max_chars` on purpose. The previous value, 120,000, had no
measured basis: the prompt reaches the CLI on stdin, so no argv length is
involved. What handing the body over as a file buys is a review this tool
cannot verify, so by default it happens only where a human asked for it, and
the shipped behaviour is one boundary rather than two.

Set it below `max_chars` and a band opens between the two: rounds in it run,
are delivered as a file, and are recorded `partial`. That is the explicit
choice of somebody who will not pay for very large prompts, and `partial` is
what it costs. Set it above `max_chars` and the file handover is reachable only
on a round forced past the budget. Both validate; neither is a mistake.

Every round records the number it was measured against — `inline_chars` on each
reviewer entry, `coverage.inline_chars` on `consolidated.json` — because a
`partial` round and a size alone do not say whether it was a large change or a
low limit.

### Surrounding context within the budget

`review.context.surrounding: enclosing` hands each code reviewer the Python
function, method or class around every hunk as well as the diff (see
[Surrounding context](reviews.md#surrounding-context)). It is off by default,
because measured on one snapshot it did not make a review cheaper (see
[Measuring what surrounding context does](#measuring-what-surrounding-context-does)).

What it adds is capped, and the cap is taken out of both limits above rather
than added to them:

```
budget = min(surrounding_chars, max_chars - change_chars, inline_chars - change_chars)
```

So the diff and its context together never go past `max_chars` or
`inline_chars`: turning context on cannot refuse a round the diff alone would
have run, and cannot turn an inlined diff into a file.
`review.context.surrounding_chars` defaults to 15,000: measured on one
snapshot, that left the cost per run where it was, while 60,000 added what it
carried. What does not fit is left out by
name, never silently.

The context adopted counts toward what `max_chars` measures: `budget_chars` on
each reviewer entry and `snapshot.budget_chars` are the diff plus the context
adopted as the prompt carries it (`context_chars`: the source with its headings,
fences and notes), and so is the size a refusal would state. Delivery, and with
it coverage, is still decided by the diff alone.

The symbols are extracted when the snapshot is taken, from a git tree object
written *before* the diff, and never from the working tree afterwards. With the
setting on, that tree is written even under `review.incremental_rounds: false`,
which goes on meaning only that the next round will not narrow to the fix:
`tree` in the snapshot's metadata stays empty. A round diffed against the
working tree then hashes each file it extracted from (`git hash-object`, which
does not go through the index, so an untracked file is not taken for a deleted
one) and compares it with its blob in the tree. A file edited in between is left
out as `changed while the snapshot was taken`, named in the prompt, and cured by
taking the snapshot again.

### Measuring what surrounding context does

The split in `optimization report` compares rounds with and without context
across *different* changes. To see what it does on one change, review the same
frozen snapshot twice -- once without context, once with -- using one-run
overrides that leave `review.context.surrounding` as it is, and read the pair.

Before starting: keep the setting at its default (`none`) and do not change any
setting between the two runs; run the same panel both times (the same `--only`
arguments, if any); pass the same `--context` to both runs or to neither; do it
**before triage**; and do it on a **whole-change round** -- the first round of a
change, or a round after a clean or all-rejected review. An incremental round is
refused. **Always start from step 1**: a snapshot that was not frozen with the
context refuses `--surrounding enclosing`.

```bash
# 1. Freeze the change and its candidates (even with the setting at none). The pair starts here
dev-orchestra review snapshot --surrounding enclosing
#    note "sha256 <12 chars>" and "context:  enclosing -- N symbol(s)"

# 2. Control: no context. The pair's first run, so its signature is registered as usual
dev-orchestra review run --surrounding none

# 3. Treatment: the same snapshot with context. The round does not advance; no signature is registered
dev-orchestra review run --surrounding enclosing
#    "Surrounding context: N symbol(s), X chars adopted (--surrounding enclosing for this run)"
#    exit 2 before any cost if nothing would be adopted (not frozen, no candidates, file delivery, no budget)
#    exit 2 if a finding was triaged or given a note after step 2

# 4. Read
dev-orchestra optimization report          # the "Paired on one snapshot" block
dev-orchestra optimization report --json   # paired.pairs[*], paired.delta
```

- Steps 2 and 3 may be swapped. Either way the first run registers the findings
  signature and the second -- a rerun of the same snapshot -- does not. The
  report that triage then works from, `reports/*.md` and `consolidated.*`, is the
  last run's; the signature the next round is compared with is the first run's.
- Pass `--surrounding` to both runs. A run without it records no `measurement`
  and is never paired.
- **What the numbers mean.** Only the `delta` of a *counted* pair -- the same
  panel, every run delivered (`ok`), the same prompt inputs, and context
  actually adopted -- can be read as the effect of the context. A negative delta
  means the context saved that much on that pair. Pairs that fail one of those
  are listed with the reason and left out of the total.
- **What it cannot say.** One pair is one observation: the reviewers vary from
  run to run on identical input, and nothing here holds that equal. Take pairs on
  three to five different changes before letting the figures decide anything.
  Codex reports no tool activity, so the tool figures are the reporting runs'
  (Claude's and agy's) alone. agy counts calls but not their output, so output
  chars per run are divided by the runs that reported output.
- **Cost.** One pair runs the snapshot's panel twice. The billed tokens and the
  runtime budget (`charged_seconds`) are each run's measured values added as they
  are; the two prompts and their durations differ, so the total is not exactly
  double. Read the real figures in `review run --json`'s `usage` and in the
  pair's `with` / `without`. `review run` itself consumes no `review` attempt,
  but a procedure that calls `budget consume review` before every run consumes
  one per run. The second run may be refused because the first spent what was
  left of `budgets.max_runtime_seconds`.
- **Why whole-change rounds only.** The prompt of an incremental round carries
  the accepted findings of the moment it runs ("The fix was meant to address:"),
  read from `consolidated.json` -- which the first run rewrites. Two runs on it
  would differ in more than the context, so `--surrounding` is refused there
  before any cost.
- **Why always from the snapshot.** On an incremental round whose previous
  review had an accepted finding, with the same base, the snapshot diffs against
  the previous round's tree; taking it again with `--surrounding enclosing` then
  finds that tree equal to the working tree, goes back to the whole diff, and the
  sha256 changes. After a clean or all-rejected review the diff is whole already
  and the sha256 stays. Either way, a snapshot without the freeze refuses the
  enclosing run, so the procedure starts from step 1.
- **A budget reset between the two runs.** `budget reset` or the idle reset
  changes the review's lineage, so the second run counts from round 1 again,
  records `rerun: false` and registers its signature. The pair still forms --
  it is keyed on the workflow directory and the snapshot, not the budget epoch --
  and `epoch` in `--json` shows the two values. The second run is still refused
  if a finding was triaged after the first: it rebuilds the same findings, and
  triage is carried over by finding key whatever the lineage.
- **An explicit `--iteration`.** A run whose `--iteration` names a round other
  than the one the first run recorded is a round of its own: it records
  `rerun: false` and registers its signature.

**What was measured.** Thirteen counted pairs on four changes (3,669 to 91,562
characters), with Claude and Codex on both sides (issue #130). The control runs
of the largest change varied by about 5.6% in billed tokens from run to run, and
Claude's tool output by up to 3.7 times. With `surrounding_chars` at 15,000 the
mean delta stayed within 6% either way on every change size -- inside that
variation. At 60,000, about 57,000 characters adopted on the largest change added
29% per run on both pairs, roughly the context carried. No setting made a review
cheaper, so the context stays off, the cap is 15,000, and priorities 2 to 6 of
[Surrounding context](reviews.md#surrounding-context) are not pursued.

### What the runtime budget counts

Seconds of delegated execution, as measured by the run itself — never the time
since the workflow started.

**Counted.** The lifetime of each delegated child process, from the same
measurement that already appears as `duration_seconds` in the run log, less the
time the machine spent asleep during it. A review round is counted per panel
member: three reviewers running in parallel for 25 minutes each spend 75 minutes
of it. A run killed at its deadline, one that failed, and one that stalled all
spent what they spent, and are all charged.

**Not counted.** The orchestrator's own work, the test suite, and a human
thinking: an interactive session where nothing is delegated never spends this
budget, and is not bounded by it. Neither is a run still in flight — it is
charged when it ends, so `budget show` moves in steps rather than continuously.
Nor is a run whose wrapper process died before it could record the outcome — a
`jobs cancel`, a Ctrl-C, an OOM kill. Nobody measured it, so nobody bills it,
and the only things bounding that shape of runaway are
`budgets.total_delegated_runs` and the per-stage attempt budgets.

Nor is **sleep**: a laptop closed on a detached run has not executed anything
while it was shut. How that is measured depends on the platform, because the
clock a run is timed with, Python's `time.monotonic()`, counts sleep on one and
not on the others:

| Platform | `duration_seconds` | What is left out of the charge | `suspended_seconds` |
| --- | --- | --- | --- |
| Windows | includes sleep | `duration_seconds` less the run's delta on `QueryUnbiasedInterruptTime`, which stops during sleep | written when non-zero |
| Linux, macOS | excludes sleep: the clock stops | nothing; there is no sleep in it to leave out | never written |

So on every platform `charged_seconds` is `duration_seconds` less
`suspended_seconds`, and a run that did not sleep is charged exactly its
duration. Two consequences of how it is measured:

* A difference under one second is not read as sleep. The interrupt-time clock
  advances once per timer tick, about 15.6 ms, so it and the far finer
  `time.monotonic()` disagree by a little on every run. A shorter sleep is
  charged.
* Modern Standby is not the classic sleep state, and whether the interrupt-time
  clock stops during it is not documented. Some standby time may still be
  charged.

The sleep left out is recorded as `suspended_seconds` on the run event, on each
reviewer entry of a review event, and on a detached run's job. It is totalled
per budget epoch and reset with the runtime budget; `budget show` and `status`
add a line saying how much was left out, only when there was some, and their
`--json` carries it as `runtime.suspended`. Like the charge, the figure is
summed per run: two reviewers asleep for the same ten minutes add twenty, so it
is delegated run time spent asleep, not how long the machine slept.

`duration_seconds` itself is unchanged, and so are the reports built from it:
on Windows, and only there, `optimization report` counts sleep in a run's time.
And `status` judges a live stage against its deadline on the wall clock, so a
run that slept can be listed under "Stalled stages" until it ends. That line is
advice only: a stage whose process is still alive is never cleared.

Because concurrent work is summed, **the total can exceed the wall clock**. On
the workflows measured while designing this, autonomous ones came to 1.23–1.45×
their own wall clock; the heaviest came to 5619s of delegated execution.

**How far past the limit a workflow can get.** A refusal happens before a run
starts, never during one, and work in flight is not counted — so the overshoot
is everything that began after the last check passed. With `D` for an entry's
recorded deadline (`--timeout`, else `run.timeout_seconds.<role>` for a run and
`review.timeout_seconds` for a reviewer — neither has an upper bound) and `G`
for the kill grace plus output drain (`KILL_GRACE_SECONDS`, 5s, plus a little):

```
overshoot ≤ Σ over in-flight runs (D + G)  +  Σ over in-flight review batches N × (D + G)
```

`N` is every reviewer *admitted* to the batch, including the ones still waiting:
a panel runs at most 8 at a time and there is no budget check inside a batch, so
a ninth reviewer in a second wave still runs to its deadline. Driving stages one
at a time, that is one run (≤ 1805s, or ≤ 3605s for the implementer) or one
two-reviewer round (≤ 3610s).
Overlapping detached workers adds a term each.

A ledger written before this became a measured budget starts it at zero: what
that ledger recorded was a wall clock, and reconstructing measurements from it
would be inventing them.

### What a delegated run's tool activity can and cannot say

Nothing here bounds what a delegated agent reads — no CLI accepts a limit on
it — but a Claude run can be counted afterwards, from the events it already
streams. `tokens show` and `optimization report` report two figures per run:
how many tool calls it made, and how many characters those tools printed back.
An agy run reports the first and not the second: its stream names each tool
step, and gives only a summary of what the tool returned (`4 lines, 17
bytes`).

**Every tool call is counted, not just `Read`.** A read-only Claude run has
`Read`, `Grep` and `Glob` and nothing else -- no `Bash` -- and `Grep` and
`Glob` read files as surely as `Read` does; an implement run has every tool.
The one run measured while designing this, from before read-only Claude runs
were narrowed to those three, was asked to report the line count of
`CONTRIBUTING.md` and did it with `Bash` (`wc -l`), never calling `Read` at all
— counting `Read` alone would have recorded that reviewer as having opened
nothing.

**The character count is observed tool output, not source read.** That same
`wc -l` returned three characters for a two-hundred-line file; `cat` on the
same file would have returned all of it. From outside the CLI the two are
indistinguishable, so **how much source a delegated run actually read is not
knowable from here**. The figures are a proxy for it, good for comparing the
same reviewer before and after a change and not for stating what was read.

Three consequences worth keeping in mind:

* **Codex reports neither.** Its adapter takes the final message from a file
  and reads usage from a prose footer; counting tool calls out of prose would
  match the code under review as readily as the CLI's own output. Its runs are
  unreported, which is why every per-run figure is divided by the runs that
  reported rather than by every run. Output chars per run are divided by the
  runs that reported output, so agy's calls count toward uses per run and not
  toward chars per run; where only agy reported, that figure prints `-`.
* **Unreported is not zero.** A run that used no tools reports `0`; a run that
  could not say reports nothing, and the output prints `-`. A role set to
  `options.output_format: json` cannot say — that format prints one `result`
  object and never emits a tool event. Runs recorded before these counts
  existed cannot say either, and go on saying so after counted runs are
  recorded beside them.
* **The event shape belongs to the CLI.** A change to it degrades to
  unreported rather than to a wrong number; `scripts/smoke_live.py` is what
  catches the drift, since the unit tests read a fixture.

### Re-fetching the source: reported as tool activity, not counted, not limited

Handing a reviewer the enclosing function is meant to save it opening the file
again. Whether it does can only be seen indirectly, and this is the whole of
what can be said about it:

* **Claude's counts are proxies.** `tool_uses`, `tool_uses_by_name` and
  `tool_output_chars` count calls, by tool name, and the characters those calls
  printed back. A `cat` through `Bash` and a `Read` are one call each, the
  second read of the same file cannot be told from the first, and `wc -l`'s
  three characters and `cat`'s whole file are not an amount read. **A re-fetch
  of source the prompt already carried is not something these counts can
  see.**
* **agy's count is calls alone.** `tool_uses` and `tool_uses_by_name` come from
  its tool steps; there is no output count.
* **Codex has no proxy at all.** Its usage comes from a prose footer, and there
  are no tool events to count.
* **Nothing limits it.** The adapters pass
  `--permission-mode plan --disallowed-tools Edit,Write,NotebookEdit --tools
  Read,Grep,Glob --strict-mcp-config --restricted` (Claude) and `-s read-only`
  (Codex). That bounds *how* and *where* a Claude run reads -- with `Read`,
  `Grep` and `Glob`, inside the working directory and `--add-dir` (measured for
  absolute paths on claude 2.1.283; symlinks were not tested) -- and not how
  much. Neither CLI is passed a flag that bounds how many reads or how many
  characters a run may take, and none is added on a guess about what a CLI
  might accept.

So this tool reports the proxies, and neither counts nor forbids re-fetching.
The one way it has to reduce reading is to hand over the context a reviewer
needs -- which is what [surrounding context](reviews.md#surrounding-context)
is -- and the way to see whether that worked is `optimization report`, which
splits code rounds into those that carried context and those that did not.
**Compare the per-run lines, not the raw ones.** The raw billed and tool
figures move with the size of each change and with the number of reviewers in
the panel -- a small change is routinely cut to one reviewer -- so each group
is also given per run and per 1k characters of change, each round's size
weighted by the runs that reported the figure.

## No progress

A budget stops a loop eventually. Repeating an outcome proves it was pointless
straight away, which is both sooner and more accurate.

```bash
dev-orchestra progress record test --signature "3 failed: test_a, test_b, test_c"
```

The same signature twice in a row means the last fix changed nothing, and the
next `budget consume` for that stage is refused. A *different* signature resets
the count, because something moved.

Reviews register their own signature automatically, from the set of open
findings: an identical round is called out in the output and counted here, so
review→fix→review stops when the fixer stops achieving anything. At the round
limit the repeat does not stop the last round's revision or fix and re-test --
there is no re-review left to save -- so `status` gives it as a reason only
once those are done, and reports `identical_rounds` meanwhile.

`budgets.max_repeats_without_progress` (2) sets the threshold.

## Not blocking in the first place

Deadlines bound the damage; they do not stop the caller from waiting. While a
delegated run is in progress the orchestrator is inside that call, so it cannot
report the situation or act on it. Detaching fixes that structurally:

```bash
id=$(dev-orchestra run implementer --prompt-file plan.md --detach --json | jq -r .id)
dev-orchestra jobs wait "$id" --timeout 120     # exit 4: still running
dev-orchestra status                            # meanwhile, visible from anywhere
```

The worst case becomes a bounded wait and a clear status rather than an
open-ended block. A job whose worker died without recording an outcome is
reported as `abandoned`, the same reconciliation the in-flight ledger does — a
dead worker must never look like one that is still working.

Detaching is optional. It costs a round trip per poll and is worth it for the
long stages (implementation, a big review) rather than every call.

A bounded wait still leaves the user looking at nothing for minutes, so each
wait also says what the job is doing. While it runs, `jobs wait` and `jobs
show` print the elapsed time and, once the CLI has used a tool, how many tool
uses and the **context tokens** -- the size of the context the model last saw,
not a running total -- with the latest tool lines and a `next:` cursor:
`jobs wait <id> --since <n>` lists only what is new, `--activity <m>` (0 to
100) how many lines. A line says which tool, and only through an allowlist what
it touched: a path relative to the project, a program (and the subcommand of
a few, such as `git`), a URL's host. The model's text and a tool's free-text
arguments are never shown, and every line is cut, stripped of control characters and
redacted before it is written and again when it is read. The file holds at most
500 lines before it is rotated, so it cannot grow without bound; a gap in the
numbering is reported as dropped lines. For a review round the orchestrator
runs in the background, `review run --progress` echoes the same lines to
stderr, one tagged line per tool use and reviewer. The flags, the JSON and the
allowlist are in [the CLI reference](cli.md#jobs).

## The verdict

`dev-orchestra status` folds all of it into one answer:

```json
{
  "verdict": "stop-and-report",
  "reasons": ["review budget spent (2/2 rounds) with 1 finding(s) still open; fixed and re-tested after the last round, not re-reviewed -- report"],
  "stalls": [],
  "abandoned_stages": ["implementer"],
  "budgets": {"test": {"used": 8, "limit": 8, "remaining": 0}}
}
```

`continue` or `stop-and-report`, with the reasons. One command to consult rather
than four rules to remember — and it is a mechanism, so it does not depend on
the orchestrator having read this page.

## Feeding it a lot of text

Logs, a legacy module, a long specification: hand the bulk to the model you
configured for it, keep the *conclusions*, and let design and review work from
those instead of from the raw pile. Context spent on a 40 MB log is context the
reviewer no longer has for the diff.

Write the analysis request to a file (pointing at paths in the repo rather than
pasting their contents) under `workflow show`'s `Artifacts:` directory, as
`execution/analysis-request.md`, and run it through the role you gave the
large-context model:

```bash
dev-orchestra run orchestrator \
  --prompt-file .ai/execution/analysis-request.md \
  --output .ai/analysis.md
```

A request that works well:

> Read `log/production-2026-09-08.log` and `app/services/checkout/*.rb`. List
> the distinct failure patterns, how often each occurs, and the code paths
> involved. No fixes yet — findings only, grouped, with `file:line`
> references.

Then design from the summary, not from the log:

```bash
dev-orchestra run architect --prompt-file .ai/analysis.md --output .ai/plan.md
dev-orchestra run implementer --prompt-file .ai/plan.md
dev-orchestra review snapshot --base main
dev-orchestra review run
```

The same split works for a spec review or a dependency audit: one model digests,
another designs, two more disagree about the result.

## What a run costs

Every delegated run records what it spent, so the question "where did the
tokens go" has an answer other than a guess:

```bash
dev-orchestra tokens show
```

```
  stage              meas.     input    output     total    billed      cost
  architect            1/1     8,200     2,100         -    11,500   $0.0421
  implementer          1/1    21,300     8,400         -    31,900   $0.2140
  review               4/4    58,000     6,400         -    64,400   $0.3900
  ALL                  6/6    87,500    16,900         -   107,800   $0.6461

Per reviewer:
  claude-general       2/2    29,100     3,300         -    32,400   $0.1950
  codex-general        2/2    28,900     3,100         -    32,000   $0.1950
```

`status` and `summary` show the total too. What each column means, and what
`meas.` and `billed` leave out, is in [`tokens`](cli.md#tokens). Two things
matter when reading it:

- **Nothing is estimated.** An estimate from the prompt alone would miss the
  child CLI's system prompt, tool schemas and the files it chose to read, which
  are most of the input, so every figure is what the CLI reported.
- **Reviewers are counted one by one** because review is the most duplicated
  cost in the pipeline: the same diff, once per reviewer, once per round. The
  per-reviewer rows are what tell you whether a third reviewer is earning its
  keep.

This is accounting, not a budget: nothing refuses a run over what it would
cost. That is what the [budgets](#budgets) are for.

## How hard to try to be cheap

`optimization.level` decides three things about a review round before any
reviewer starts: whether a red tree is reviewed at all, whether a small,
low-risk change gets the whole panel, and how many findings each reviewer is
asked for. The levels, their table and the keys are in
[Optimization level](configuration.md#optimization-level); what the level can
and cannot do to a round is in
[When a review does not run, or runs smaller](reviews.md#when-a-review-does-not-run-or-runs-smaller).
What it looks like from the command line:

```
$ dev-orchestra review run
refusing to review: the last recorded test run failed. Reviewing code that
does not pass its own tests spends a reviewer on a problem you already know
about. Fix the tests, record the result, and run again -- or pass --force.
```

The gate reads whatever the last `dev-orchestra state record test ok|failed`
wrote. A tree with no recorded result warns and is reviewed.

```
$ dev-orchestra review run
note: aggressive → quality: db/migrate/003_drop_orders.rb matches *migrate*/*
```

A high-risk change escalates to `quality` whatever is configured.

```
note: low-risk change (1 file(s), 12 line(s)): 1 reviewer instead of the full
panel (claude-general). Cross-model disagreement is what a second reviewer
buys; raise optimization.level or the low_risk thresholds to keep it.
```

A reduced panel keeps a `general` reviewer and says so.

### Did it work?

```bash
dev-orchestra optimization report
```

A level's effect is a rate -- how often it refused, how often it cut the panel
-- so the report reads the run log of every workflow rather than one
workflow's `tokens show`. Its output, the design review's separate spend, and
what the estimated saving is and is not are in
[`optimization`](cli.md#optimization).

## What is still not covered

Honest limits of the above:

* **A pid can be recycled**, so "the process is gone" is best-effort. A stage's
  pid is used to flag and clear, never to kill. A detached job's worker pid is
  also what `jobs cancel` stops, so the job records the worker's start time
  with it (Windows and Linux) and checks it first: a pid that now belongs to
  another process marks the job abandoned and is not touched. A record
  without one -- written by an earlier version, or on macOS, where none is
  read -- or a live pid whose start cannot be read now, is stopped on POSIX
  only while the pid still leads its own process group, as the worker does,
  and never on Windows. A cancel that leaves such a pid running leaves the job
  unfinished, so `workflow remove` still waits for it, and exits 1.
* **Budgets are per project workspace**, keyed on `.ai/state.json`. Two
  concurrent workflows in one checkout share them.
* **Nothing here bounds a single reviewer's token spend**, only its wall clock.
  `review.context.max_chars` bounds what is *sent*, in characters, which is a
  different thing from tokens and a different thing again from what the
  reviewer then goes and reads for itself.
* **Nothing bounds how much a reviewer reads**, and how much it read cannot be
  measured either. A Claude reviewer reads only with `Read`, `Grep` and `Glob`,
  inside the working directory and `--add-dir`; a Codex reviewer is not
  confined. The counts above are tool calls and the output those tools
  printed: `wc -l` and `cat` read the same file and report three characters
  and the whole of it. Codex reports neither figure.
* **The stream-json parse depends on an event schema** that the CLI owns. It
  degrades rather than failing — result event, then assistant text blocks and
  the streamed text of a message cut off mid-way, then raw stdout — but a format change would still cost the structured extras
  (`is_error`, `num_turns`).
* **A continued architect session can stall like any run** (`run architect
  --resume`). A long plan no longer looks like one, since the adapter asks for
  partial messages — unless the installed CLI does not list the flag, and then
  nothing streams while the final message is written. A stalled or failed
  continued run is reported and not retried; the
  next `--resume` runs fresh (`it did not succeed`). Only a session the CLI
  says no longer exists is run again fresh, once, and that costs an attempt.
* **`state.json` is trusted to name the session `--resume` continues**, as it
  is trusted with the plan approval and the budgets. Anyone who can edit it can
  make the architect continue another of their own sessions on this machine --
  which they could already read directly. The id has to be a UUID before it is
  used or recorded, so it cannot put a flag on the command line. That checked
  id is the only value read from it that is repeated: on the command line
  (which `--print-command` prints) and as `resume.resumed_from` in the event,
  the job record and the in-flight entry.
* **A rejected session is recognised only with `stream-json`.** With
  `options.output_format: text` or `json` the CLI's output carries no sign that
  the session was missing, so such a run is reported as an ordinary failure.
* **Whether a resumed session stays read-only is checked per CLI version**,
  by `scripts/smoke_live.py`, not continuously. A version newer than one that
  was checked, and of the same major version, resumes on trust, so a resumed Claude session that lost
  read-only on a newer version is noticed only when the script is run on it;
  a change in behaviour that keeps the same version string would go unnoticed
  until the script is run again. A Codex fork is confirmed read-only from its
  rollout after the run, which detects a write but does not prevent one.
