# Limits: stalls, timeouts and budgets

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
diff is several times larger than a pipe buffer.

### Two deadlines, because "slow" and "wedged" differ

| Deadline | Config | Meaning |
| --- | --- | --- |
| Total | `review.timeout_seconds` (1800) | Hard cap on one delegated run |
| Idle | `review.idle_timeout_seconds` (300) | No output for this long → wedged |

The idle deadline is the useful one: a working agent keeps producing, a wedged
one goes silent, so a stall surfaces in minutes instead of half an hour.

**It only applies where a healthy run actually streams.** That was measured, not
assumed:

| Command | First output | During the run |
| --- | --- | --- |
| `codex exec` | 0.4s of an 11.5s run | keeps ticking (≤3.5s gaps) |
| `claude -p --output-format stream-json` | 2.5s of a 6.5s run | `thinking_tokens` events throughout |
| `claude -p --output-format text` | **8.1s of an 8.9s run** | nothing until the end |

An idle deadline on that last row would kill healthy runs, so an adapter must
declare `streams_progress = True` before one is applied to it, and only on
evidence. Where it does not apply, the total deadline is the only protection —
which is exactly why the observability below matters.

A killed run is reported as `stalled` rather than merely failed, and if the
process group did not fully exit you are warned that orphans may remain.

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

The last two are backstops for cycles that delegate: every run that consumes an
attempt — `run <role>` and `budget consume <stage>` — counts against the first,
every run that reaches its end with a measurement against the second. A cycle
that delegates nothing is bounded by neither; the round, attempt and no-progress
rules above are what stop the cycles this tool knows about.

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
(`MAX_INLINE_DIFF_CHARS`, `prompt_chars`), and because the standard library
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
that depends on the inline limit, not on this one. With the defaults the two
coincide, because anything over 400,000 chars is far over the 120,000-char
inline limit and does go over as a file; with `max_chars` set below 120,000 a
forced round is over budget and still delivered inline. The refusal message
says which of the two applies to the change in front of it.

### What the runtime budget counts

Seconds of delegated execution, as measured by the run itself — never the time
since the workflow started.

**Counted.** The lifetime of each delegated child process, from the same
measurement that already appears as `duration_seconds` in the run log. A review
round is counted per panel member: three reviewers running in parallel for 25
minutes each spend 75 minutes of it. A run killed at its deadline, one that
failed, and one that stalled all spent what they spent, and are all charged.

**Not counted.** The orchestrator's own work, the test suite, and a human
thinking: an interactive session where nothing is delegated never spends this
budget, and is not bounded by it. Neither is a run still in flight — it is
charged when it ends, so `budget show` moves in steps rather than continuously.
Nor is a run whose wrapper process died before it could record the outcome — a
`jobs cancel`, a Ctrl-C, an OOM kill. Nobody measured it, so nobody bills it,
and the only things bounding that shape of runaway are
`budgets.total_delegated_runs` and the per-stage attempt budgets.

Because concurrent work is summed, **the total can exceed the wall clock**. On
the workflows measured while designing this, autonomous ones came to 1.23–1.45×
their own wall clock; the heaviest came to 5619s of delegated execution.

**How far past the limit a workflow can get.** A refusal happens before a run
starts, never during one, and work in flight is not counted — so the overshoot
is everything that began after the last check passed. With `D` for an entry's
recorded deadline (`--timeout`, or `review.timeout_seconds`, which has no upper
bound) and `G` for the kill grace plus output drain (`KILL_GRACE_SECONDS`, 5s,
plus a little):

```
overshoot ≤ Σ over in-flight runs (D + G)  +  Σ over in-flight review batches N × (D + G)
```

`N` is every reviewer *admitted* to the batch, including the ones still waiting:
a panel runs at most 8 at a time and there is no budget check inside a batch, so
a ninth reviewer in a second wave still runs to its deadline. Driving stages one
at a time, that is one run (≤ 1805s) or one two-reviewer round (≤ 3610s).
Overlapping detached workers adds a term each.

A ledger written before this became a measured budget starts it at zero: what
that ledger recorded was a wall clock, and reconstructing measurements from it
would be inventing them.

### What a delegated run's tool activity can and cannot say

Nothing here bounds what a delegated agent reads — neither CLI accepts a limit
on it — but a Claude run can be counted afterwards, from the events it already
streams. `tokens show` and `optimization report` report two figures per run:
how many tool calls it made, and how many characters those tools printed back.

**Every tool call is counted, not just `Read`.** Review mode denies
`Edit,Write,NotebookEdit` and nothing else, so `Bash`, `Grep` and `Glob` are
all legitimate ways for a reviewer to read a file. The one run measured while
designing this was asked to report the line count of `CONTRIBUTING.md` and did
it with `Bash` (`wc -l`), never calling `Read` at all — counting `Read` alone
would have recorded that reviewer as having opened nothing.

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
  reported rather than by every run.
* **Unreported is not zero.** A run that used no tools reports `0`; a run that
  could not say reports nothing, and the output prints `-`. A role set to
  `options.output_format: json` cannot say — that format prints one `result`
  object and never emits a tool event. Runs recorded before these counts
  existed cannot say either, and go on saying so after counted runs are
  recorded beside them.
* **The event shape belongs to the CLI.** A change to it degrades to
  unreported rather than to a wrong number; `scripts/smoke_live.py` is what
  catches the drift, since the unit tests read a fixture.

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
review→fix→review stops when the fixer stops achieving anything.

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

## The verdict

`dev-orchestra status` folds all of it into one answer:

```json
{
  "verdict": "stop-and-report",
  "reasons": ["review budget spent (2/2 rounds) with 1 finding(s) still open"],
  "stalls": [],
  "abandoned_stages": ["implementer"],
  "budgets": {"test": {"used": 8, "limit": 8, "remaining": 0}}
}
```

`continue` or `stop-and-report`, with the reasons. One command to consult rather
than four rules to remember — and it is a mechanism, so it does not depend on
the orchestrator having read this page.

## What is still not covered

Honest limits of the above:

* **A pid can be recycled**, so "the process is gone" is best-effort. It is used
  to flag and clear, never to kill.
* **Budgets are per project workspace**, keyed on `.ai/state.json`. Two
  concurrent workflows in one checkout share them.
* **Nothing here bounds a single reviewer's token spend**, only its wall clock.
  `review.context.max_chars` bounds what is *sent*, in characters, which is a
  different thing from tokens and a different thing again from what the
  reviewer then goes and reads for itself.
* **Nothing bounds what a reviewer reads**, and how much it read cannot be
  measured either. The counts above are tool calls and the output those tools
  printed: `wc -l` and `cat` read the same file and report three characters
  and the whole of it. Codex reports neither figure.
* **The stream-json parse depends on an event schema** that the CLI owns. It
  degrades rather than failing — result event, then assistant text blocks, then
  raw stdout — but a format change would still cost the structured extras
  (`is_error`, `num_turns`).
