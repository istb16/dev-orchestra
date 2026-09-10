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
| Attempts per stage | `budgets.architect` (3), `.implementer` (5), `.review_fixer` (4), `.test` (8) | `run <role>`, `budget consume <stage>` |
| Delegated runs in a workflow | `budgets.total_delegated_runs` (40) | every `run` |
| Wall clock | `budgets.max_runtime_seconds` (7200) | every `run` |

The last two are backstops. They bound a workflow even when the orchestrator
invents a cycle nothing here anticipated, which is the failure that is hardest
to design against.

`run` and `review run` consume their own budgets. Stages the orchestrator
performs itself — running the test suite, above all — must claim theirs:

```bash
dev-orchestra budget consume test     # exit 3 when spent
dev-orchestra budget show
dev-orchestra budget reset            # start a fresh workflow
```

A ledger idle for `budgets.session_idle_reset_seconds` (6h) is treated as a
finished workflow, so the next request starts with full budgets without anyone
remembering to reset.

`--force` overrides a refusal. It is there for a human who has decided to
override; `SKILL.md` tells the orchestrator not to reach for it.

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

* **Claude has no idle deadline yet.** Its adapter uses the text output format,
  which is silent until the end, so only the total deadline applies. Moving it
  to `stream-json` would fix that; the measurements are above.
* **`status` observes, it does not interrupt.** While a call is blocked, the
  orchestrator is blocked with it. Interruption comes from the deadlines; the
  cure for the blocking itself is detached execution (`--detach`, `jobs wait`).
* **A pid can be recycled**, so "the process is gone" is best-effort. It is used
  to flag and clear, never to kill.
* **Budgets are per project workspace**, keyed on `.ai/state.json`. Two
  concurrent workflows in one checkout share them.
