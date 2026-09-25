# Reviews

## The rules that make multi-model review worth doing

1. **One frozen snapshot.** Every reviewer sees exactly the same diff, taken
   before any of them starts.
2. **No cross-contamination.** No reviewer sees another's output. Independent
   disagreement is the entire value.
3. **Read-only.** Reviewers run in a read-only sandbox. A reviewer that edits
   code is a bug.
4. **Partial failure is normal.** One reviewer timing out does not invalidate
   the others.
5. **Findings are claims, not facts.** Nothing reaches the fixer until the
   orchestrator has triaged it.

## Snapshot

```bash
dev-orchestra review snapshot                 # working tree vs HEAD
dev-orchestra review snapshot --base main     # everything since main
dev-orchestra review snapshot --no-untracked  # tracked changes only
dev-orchestra review snapshot --no-exclude    # generated files included too
```

Writes `.ai/reviews/review-target.diff` plus metadata:

```json
{
  "strategy": "git diff HEAD",
  "base": null,
  "head": "9f2c…",
  "files": ["app/services/pricing.rb", "spec/services/pricing_spec.rb"],
  "untracked_included": ["app/services/pricing.rb"],
  "withheld": [
    {"path": "package-lock.json", "pattern": "package-lock.json", "added": 412, "deleted": 87}
  ],
  "exclude_patterns": ["*.lock", "package-lock.json", "dist/*", "..."],
  "bytes": 4213,
  "sha256": "…",
  "empty": false,
  "surrounding": {"mode": "enclosing", "path": ".ai/reviews/review-surrounding.json", "tree": "3f2a…",
                  "candidates": 7, "chars": 49371, "skipped": 3}
}
```

`surrounding` is there only with `review.context.surrounding: enclosing`; see
[Surrounding context](#surrounding-context).

Untracked files are included by default (up to 512 KB each) — a new module is
usually the most important part of a change. The sha256 is stamped into every
reviewer report so you can prove they judged the same thing.

The skill's own files are never part of the snapshot: everything under the
workspace directory (`.ai/`) and any `.dev-orchestra.yaml|yml|json` are
skipped, so the config the setup wizard just wrote does not show up as a
"change" in every review.

Snapshotting requires git. An empty snapshot exits non-zero: there is nothing to
review, which usually means the implementation stage did not write anything.

### Withheld files

A reviewer reads a diff to judge code somebody wrote. A lockfile, a bundle and
a recorded snapshot were not written, and they cost the same tokens as real
code -- once per reviewer, once per round. A routine dependency bump therefore
regularly costs more than the change it accompanies. `review.exclude` withholds
the *body* of those diffs. Measured on a 400-package lockfile bump alongside a
two-line source change, one review round with two reviewers went from 44,783 to
1,711 input tokens.

Withheld is not hidden, and the distinction is the whole design:

- the file is still named to the reviewer, with how many lines changed, so a
  review that genuinely turns on a dependency version can go and read it
- `review snapshot` prints what it withheld and which pattern did it
- `--no-exclude` sends everything, once
- the patterns in force are recorded in the snapshot metadata, so a snapshot
  can explain itself after the fact

Matching is fnmatch, case-sensitive on every platform so a snapshot taken on
Windows contains what Linux would produce, against two targets: the full
repository-relative path, and -- for a pattern with no `/` in it -- the base
name alone, so `*.lock` catches a lockfile at any depth. `*` crosses `/`, which
is why the defaults spell out both `dist/*` and `*/dist/*` rather than relying
on a `**` that is not implemented.

The default list covers lockfiles, `dist/`, `vendor/`, `node_modules/`,
minified output, source maps and `*.snap`. Anything ambiguous is deliberately
left out: `build/` is conventionally output but is hand-written often enough
that excluding it by default would sometimes hide real work. Quietly dropping a
real change is a worse failure than paying for a lockfile.

If *every* changed file is withheld, the snapshot is empty and says so in those
terms -- that is a different situation from "nothing changed", and `review run`
names the files and points at `--no-exclude` rather than reporting an empty
diff.

Renames are detected (`-M`), so a moved file costs a header instead of twice
its length. This needs the move to be staged: an unstaged `mv` leaves git with
a deletion and an untracked file, which are two unrelated facts as far as `git
diff` is concerned.

## Surrounding context

```yaml
review:
  context:
    surrounding: enclosing      # none (default) | enclosing
    surrounding_chars: 60000    # the most it may add; null means the default
```

A reviewer handed a diff sees three lines either side of every hunk, and nearly
always opens the function the hunk sits in before judging it -- at a cost paid
once per reviewer and once per round, and invisible from here. With
`surrounding: enclosing` the prompt carries that function too, after the diff,
labelled as context and not as part of the change. `off` reads as `false` in
YAML, and `false` and `null` both mean `none`; `true` names no mode and is
refused. It ships off, because what it saves has not been measured yet:
`optimization report` compares rounds with and without it (see
[optimization](cli.md#optimization)).

**Frozen with the snapshot.** `review snapshot` extracts the symbols from the
git tree the diff was taken from and writes them to `review-surrounding.json`,
stamped with the diff's sha256. `review run` only reads that file, so editing a
file between the snapshot and the run changes nothing a reviewer is shown. A
snapshot taken with the setting off has nothing frozen, and a run with it on
says so -- `not frozen: … take it again` -- rather than reading the working
tree.

**Which symbol.** Every changed line is a pair of new-side line numbers: an
added line `n` is `(n, n)`, and a deleted line, which sat between `n - 1` and
`n`, is `(n - 1, n)`. The smallest function, method or class holding both ends
**encloses** the change. When none does, the smallest symbol holding either end
is **adjacent**: a deletion sits on its edge and nothing that survived encloses
it. Delete a whole function between A and B and A and B are adjacent, never
enclosing. Adjacent symbols are adopted after enclosing ones and marked
`adjacent to a deletion, not enclosing it`.

A symbol every line of which was added is not a candidate, because the diff
already shows it whole; the change moves on to the symbol outside it, which is
how a class becomes the candidate when a method is added to it. A new file is
not extracted at all, for the same reason.

**Python only.** `.py` files, through `ast`. Anything else is not extracted and
is counted in the prompt as `not python`. There is no fallback to a window of
lines: the diff already carries three lines either side, and a window that
stops half way through a function is not the enclosing symbol -- misleading
context is worse than none.

**Nothing is guessed at.** A file whose symbols cannot be taken exactly is
skipped, with a reason:

| Reason | When |
| --- | --- |
| `not python` | Not a `.py` file |
| `new file (the diff already shows all of it)` | Added by this change |
| `deleted` | Removed by this change |
| `symlink`, `submodule` | A symlink or a gitlink in the tree -- never followed |
| `file over 512,000 bytes` | Too large to hand over whole |
| `not utf-8`, `syntax error` | `ast` cannot read the file as it is stored |
| `no enclosing symbol beyond the diff` | Only module-level code changed, or only whole new symbols |
| `ambiguous path (x.py or b/x.py)` | Both names are in the snapshot and the diff header does not say which this is |
| `changed while the snapshot was taken -- take it again` | Edited between the tree and the diff (see [limits](limits.md#surrounding-context-within-the-budget)) |
| `not in tree`, `unreadable`, `no tree object (git write-tree failed)` | git could not produce the file from the tree |

The `+++` path depends on git configuration this tool does not set
(`diff.noprefix`, `diff.mnemonicPrefix`), so no prefix is assumed. A rename's
`rename to` line wins; otherwise the written name, or the name less its first
two characters, whichever the snapshot lists; and only when it lists both is
the `diff --git` line asked which it is. When that does not settle it, both
files are skipped by name.

**Adopted within a budget.** At `review run` the candidates are adopted
enclosing first, then adjacent, smallest first within each -- which gives the
most hunks their symbol under a fixed budget -- up to
`min(surrounding_chars, max_chars - change, inline_chars - change)`. The budget
is spent on the block as the prompt carries it -- headings, fences and the
left-out list included -- and that size is recorded as `context_chars`. The diff
and its context together never pass either limit, so turning this on cannot
refuse a round or turn an inlined diff into a file. A class and a method inside
it can both be candidates: the method is taken first, and the class only if
what it adds still fits, in which case the method is folded into it and named
under `includes`. A method adjacent to a deletion inside a class already taken
is folded in the same way -- nothing is shown twice. A round whose diff goes
over as a file adopts nothing, and names every candidate as left out for
`file delivery`.

**Named wherever it is reported.** Every symbol left out is named, with its
path, lines and size: in the prompt (`Left out (…): read these yourself if a
hunk needs them.`), on each reviewer entry, in `consolidated.json` and
`consolidated.md`, and in `review status`. The prompt names the first 20 and
counts the rest (`- and N more, named in `review status` and consolidated.md`):
the list is part of the block the budget pays for, and a long one would crowd
out the symbols it was meant to point past. When no symbol fits, the prompt and
the reports say `no symbol fits within the budget`. Files not extracted are counted by
reason in the prompt, and the two reasons a fresh snapshot cures are named;
`consolidated.md` and `review status` name every one with its reason.

```json
"surrounding": {
  "shared": true,
  "mode": "enclosing",
  "reason": "",
  "budget": 60000,
  "adopted_chars": 3100,
  "trimmed_chars": 9812,
  "context_chars": 3521,
  "adopted": [
    {"path": "app/models/user.py", "symbol": "User", "kind": "class", "relation": "encloses",
     "start": 12, "end": 88, "chars": 3100, "includes": ["User.save"]}
  ],
  "trimmed": [
    {"path": "app/cli.py", "symbol": "run", "kind": "function", "relation": "encloses",
     "start": 610, "end": 826, "chars": 9812, "reason": "budget"}
  ],
  "skipped": [{"path": "web/app.js", "reason": "not python"}]
}
```

That is the top-level block of `consolidated.json`, and it is `shared: true`
only when every reviewer of this snapshot was handed the same context. When
they were not -- `--only` after the setting changed -- it is `shared: false`
with `by_reviewer`, one record per reviewer, and `null` for a reviewer that
built its prompt with the setting off. A reviewer that fell over before its
prompt was built has no record and is not there at all; the Reviewers table
already says it failed. With the setting off, or on a design round, the key is
not written anywhere.

**Coverage is unchanged.** Coverage is decided by whether the *diff* was
inlined. Context adopted or left out moves neither `coverage.round` nor
`coverage.change`: a round that left every symbol out is still complete, and a
round whose diff went over as a file is still partial. `snapshot.budget_chars`
does include it -- see [Coverage](#coverage).

**Which rounds.** Every code round, fix rounds included: an incremental round
gets the symbols around the fix's hunks, read from that round's tree. Design
rounds carry none.

**What comes after the enclosing symbol.** The order is fixed now, so that
adding a kind of context later does not reorder what is already adopted:

| Priority | Context | Status |
| --- | --- | --- |
| 1 | Around the change: the enclosing function, method or class, and `adjacent` symbols beside a deletion | Implemented |
| 2 | Functions and methods the change calls | Not implemented -- needs symbols resolved across files |
| 3 | The class or module the change belongs to | Not implemented -- the class candidate of 1 covers part of it |
| 4 | Callers and callees | Not implemented -- needs the same, plus a reverse index |
| 5 | Related tests | Not implemented -- needs a convention for finding them |
| 6 | Anything else | Not implemented |

Candidates rank on `(priority, relation, chars, path, start)`, which with
priority 1 alone is `(relation, chars, path, start)`. Priorities 2 and 4 would
need a cross-file index and resolver, doubling `context.py`, while the effect of
1 has not been measured yet -- so 1 ships first, and off.

## Design review

The same panel, before any code exists to be wrong. `review.design.enabled`
(default `false`) turns it on:

```bash
dev-orchestra config set review.design.enabled true
dev-orchestra review run --design
dev-orchestra review show --design
dev-orchestra review triage --design F1 --status accepted --note "confirmed"
dev-orchestra review fix-brief --design --output .ai/execution/design-fix-brief.md
```

**The input is the plan, not a diff.** `review run --design` freezes
`.ai/plan.md` into `.ai/reviews/design/review-target.md` and hashes it
together with the design request it answers (`--request <path>`, default
`.ai/execution/design-request.md`). Same plan and same request means the same
round; a rewritten plan is the next one. No git is involved, so a design review
works in a directory that was never a repository. With no plan on disk the
command exits 2 and says to run the architect first — which is also what makes
"no design stage, no design review" true mechanically rather than by
convention.

**The prompt asks a different question.** Not "is this code correct" but
"would following this produce something correct": do the files and symbols the
plan names exist, is its account of the current behaviour true, does it cover
every caller, is the Test Strategy enough. Each built-in role is re-pointed the
same way — `security` asks what the proposal would let through rather than what
the diff does. `File:` therefore takes a plan section
(`plan.md#Proposed Change`) or the repository path the plan misjudges, and
`Line:` is usually `n/a`.

**The artifacts are its own**, under `reviews/design/`: one report per
reviewer, `consolidated.md` / `.json`, and the frozen plan. That is the whole
reason it is a directory rather than a filename prefix — the round counter and
the triage live in the consolidated report, and a design round must never
advance, or be refused by, the code review's count. `review show`,
`review triage`, `review fix-brief` and `review status` all take `--design` to
read that copy instead; without the flag they never see a design finding.
`--base` means nothing here and is ignored.

**The optimization gate and panel reduction do not apply.** There is no test
result that says anything about a plan and no diff to measure, and a design
decision is precisely where cross-model disagreement earns its cost, so the
whole panel runs every round. `review.max_findings` still applies, and
`optimization.level` still sets the cap when it is unset. Design rounds are
deliberately absent from every rate `optimization report` prints -- the levels
in force, the gate verdicts, the panel reduction, the escalations -- because no
level decided anything for them. Their **cost** is reported, in a row of its
own beside code review's, and is never averaged with it.

**Reflecting the findings** is a re-run of the architect, not a new stage:
write a revision request (the original request, plus the brief, plus "read
`.ai/plan.md` and rewrite it keeping every section; say for each finding
whether you addressed it or why not") and run `run architect` over it. That
spends `budgets.architect`, which is why no new budget key exists. Only the
immediately previous plan survives, frozen in `review-target.md`; a rewrite
overwrites the rest. The findings of the round that reaches the limit are
reflected too: `review status --design` says `final_revision: pending` until
the plan differs from the frozen one or the architect has answered after that
round, and only the re-review of that revision is refused.

**Cost.** A round is about what a code review round costs: reviewers read the
files the plan names. `review.design.max_iterations` (default 2) bounds the
rounds, and `1` is the cheap setting — one round, one revision that is not
re-reviewed, then ask.

## When a review does not run, or runs smaller

`optimization.level` (default `balanced`) decides three things about a round
before any reviewer starts. The full table is in
`references/configuration.md`; what matters here is what it can and cannot do
to a review.

**It can refuse a round outright**, when the last `state record test ok|failed`
recorded a failure. A refusal is recorded as a `refused` round even though
nothing ran -- a skipped round is the largest thing the level ever saves, and a
saving that leaves no trace cannot be counted. `optimization report` counts
them. Reviewing a tree that does not pass its own tests spends a
reviewer on a problem already known. `--force` overrides. A tree with no
recorded test result is *not* refused -- it warns and runs, because "nobody
wrote it down" is not "it failed".

**A change too large is refused before any of that.** `review.context.max_chars`
(400,000) is the most change body a round will send at all — the diff, or the
plan plus the request it answers — and over it `review run` exits 3 with nothing
reviewed, before the round is charged and, on the design path, before the plan
is frozen. Trimming to fit is the one thing it will not do: a reviewer handed
part of a change cannot tell which part is missing. The ways under it are
`--base`, `review.exclude`, splitting the change, or a shorter plan; `--force`
is the human's, so an automated workflow over the limit reports *not reviewed*
and stops, and `status` keeps saying so until a round actually reviews the
change — a second refusal is not that, and neither is bookkeeping. The
refusal is recorded like the gate's, with `refused_by: "context"` to tell them
apart, and a forced round carries `over_budget` on every reviewer entry and on
`consolidated.json`'s `snapshot` block. See `references/limits.md` for the
number and what forcing does and does not promise.

**It can cut the panel to one reviewer**, at `aggressive`, when the change is
under `low_risk_max_files` and `low_risk_max_lines` and touches no high-risk
path. That reduced panel keeps a `general` reviewer in preference to a
specialist: a lone security reviewer reports no correctness bugs, because it
was told not to look for them. `--only` overrides, and the reduction is
printed with the counts that produced it.

**It cannot make a high-risk change cheap.** Anything matching
`optimization.high_risk_paths` -- auth, secrets, payments, migrations, SQL,
crypto, deploy config -- escalates to `quality` whatever the level says: full
panel, full findings budget, no gate. The patterns are yours to replace; the
escalation is not yours to switch off.

What it never does is drop findings, merge reviewers' reports, or hide that
it acted. Every decision is printed, recorded in the run state, and returned
by `review run --json` and `status --json` under `optimization`.

## Roles

Built-in roles get sharper guidance in the prompt:

| Role | Emphasis |
| --- | --- |
| `general` | Correctness, regressions, edge cases, security, performance, data consistency, concurrency, error handling, test gaps, unnecessary complexity |
| `security` | Authn/authz, injection, deserialisation, SSRF, path traversal, secret handling, unsafe defaults, access-control regressions |
| `performance` | Complexity, N+1 queries, missing indexes, unnecessary I/O, unbounded memory, blocking calls, cache invalidation |
| `test` | Coverage of the changed behaviour, meaningless assertions, flaky patterns, untested error paths |
| `architecture` | Layering, responsibilities, leaky abstractions, coupling, contract shape, fit with existing conventions |
| `database` | Migration safety, locking, backfills, reversibility, constraints, index coverage, transaction boundaries |
| `frontend` | State handling, rendering cost, accessibility, responsive layout, error/loading states, client-only validation |
| `backend` | API contracts, validation, status codes, idempotency, transactions, job semantics, observability |

Any other string is a valid custom role and gets a generic specialist framing
(`role: accessibility` → "review as an accessibility specialist"). Custom roles
are worth writing down in the project config so the panel is reproducible.

For `general`, style-only observations are low severity at most. A review that
returns four naming nits and misses a null-pointer path is a failed review.

## Output schema

Reviewers are asked for this exact shape:

```markdown
## Finding
- Severity: critical | high | medium | low
- File: <path relative to the repo root>
- Line: <number, range, or n/a>
- Category: <correctness | security | performance | tests | …>
- Problem: <what is wrong, 1-2 sentences>
- Impact: <what breaks, and when>
- Evidence: <the code or hunk that shows it>
- Fix: <the concrete change>
```

A clean review returns exactly `NO_FINDINGS`.

### Output limits

The prompt caps what comes back:

| Limit | Default | Where |
|---|---|---|
| Findings per reviewer | 6 | `review.max_findings` (0 = no cap) |
| Evidence lines per finding | 3 | `MAX_EVIDENCE_LINES` in `review.py` |
| Fix lines per finding | 2 | `MAX_FIX_LINES` in `review.py` |
| Preamble, summary, sign-off | none | fixed in the prompt |

Output is the expensive direction: per token it costs several times what input
does, and a reviewer's output is billed again when it is consolidated and again
as the fixer's brief. An uncapped prompt invites twenty low findings and a
screenful of quoted context each.

The cap is on volume, not judgement. A reviewer over the limit is asked for its
worst findings, not asked to keep quiet, and **nothing that does come back is
dropped** — every finding is parsed and kept, and a reviewer that overshoots is
reported on stderr. Deciding which findings to discard is triage, and triage is
the orchestrator's, not the prompt's.

A reviewer that returns neither findings in a recognisable shape nor
`NO_FINDINGS` is recorded with status `unparsed` and counted as **failed**. A
report that cannot be read is not evidence that the code is fine, and treating
it as such is the worst way for a review tool to fail.

## Coverage

A change body over **`review.context.inline_chars`** (default 400,000
characters) does not go into the prompt. The reviewer is handed the path of
the frozen snapshot instead and asked to read it — and how much of it actually
gets read is not knowable from here. Claude Code's `Read` stops at 2,000 lines
by default, so a reviewer can answer `NO_FINDINGS` having seen a fifth of the
change, and nothing in that answer distinguishes it from a genuinely clean
review.

So the verdict is not taken from the answer. A reviewer whose change body was
handed over as a file is recorded with status **`partial`**, whatever it
returned. Its findings are kept and triaged like any others; what is missing is
the guarantee that it saw the whole change. `partial` is counted apart from
`failed` — `reviewers_ok`, `reviewers_partial` and `reviewers_failed` sum to
`reviewers_total` — and `unparsed` wins over it, because a report nobody can
read is the more specific fact.

The default is the same number as `review.context.max_chars`, which leaves one
boundary rather than two: at or under it the round runs and is complete, over
it the round is refused, and a round forced past it is `partial`. Set
`inline_chars` below `max_chars` and a band opens between the two where the
round runs and is recorded `partial` — that is what the setting is for, and
what it costs. Because the limit is configuration, every round records the
number it was measured against: `coverage.inline_chars` in
`consolidated.json`, and `inline_chars` on each reviewer entry.

Those four count the whole reviewer table, which outlives the round on purpose
(see below). `counts` carries the same four again as
`snapshot_reviewers_total`, `snapshot_reviewers_ok`, `snapshot_reviewers_partial`
and `snapshot_reviewers_failed`, counted over the entries stamped with the
snapshot the report is about — the set every `coverage` value is derived from.
Both are named so that neither is read as the other: `0 ok / 0 total` beside
`no reviewer has run against this snapshot` is one answer, `2 ok / 2 total`
beside it is two snapshots in one document. `consolidated.md` prints the
table's tally, and the snapshot's on a second line whenever the two differ;
`review status` reports the snapshot's (its top-level `reviewers_partial` is
that one, and `counts` carries both).

The reviewer is never asked to declare any of this. A declaration cannot be
checked for the case where it was *not* made, and the prompt ends with
"Findings or `NO_FINDINGS` only", which overrides anything asked before it. The
prompt says what the round is recorded as; the recording is done here.

`consolidated.json` carries the round's answer at the top level:

```json
{
  "coverage": {
    "round": "complete",
    "change": "unverified",
    "unverified_since": 1,
    "change_chars": 130412
  }
}
```

Every value below is derived from the reviewer entries stamped with the
snapshot the report is about. Each entry carries `snapshot`, the same short sha
the reviewer reports are stamped with, because the reviewer table deliberately
outlives the round: `--only` merges a fresh run into it and a reviewer that did
not run this time is kept so the table stays complete. An entry from an earlier
snapshot is no more this round's coverage than a report from an earlier
snapshot is this round's findings. `snapshot` is a new key; a reader that does
not know it sees the entry it always saw.

**`coverage.round`** — whether **this round's change body**
(`review-target.diff`, or `review-target.md` for a design round; on an
incremental round that diff is the fix alone) was inlined whole into every
reviewer's prompt this round.

- `complete`: inlined for every reviewer. **Says nothing about the whole
  change.** On an incremental round it means "the fix was shown in full".
- `unverified`: handed to one or more reviewers as a file.
- `none`: no reviewer ran against this snapshot (none configured, none run
  since it was taken, or every entry predates this version).

**`coverage.change`** — **the whole change under review**.

- `unverified`: this workflow and branch has a round whose `round` was
  `unverified`, and since then no non-incremental snapshot (empty
  `incremental_from`) has been reviewed with `round: complete` and at least one
  reviewer *of that snapshot* `ok`.
- `complete`: that has happened, or no round was ever unverified.
- `none`: nothing on this workflow and branch has been reviewed yet.

**`coverage.unverified_since`** — the round number `change: unverified` started
at, when that number belongs to the current count. `null` otherwise — which
includes an `unverified` change whose mark was carried across a lineage change:
the round counter restarts there (see [Re-review](#re-review)), so the number
would name a round the count does not have, `iteration 1/2` printed beside
"since round 3". The mark is what the carry is for and it survives; the number
is not and is dropped with the count it belonged to. Read the mark from
`coverage.change`, never from this being set.

**`coverage.change_chars`** — how many characters this round's change body was,
as the runs recorded it (`null` when no run did).

**`coverage.inline_chars`** — the `review.context.inline_chars` that size was
measured against. Both numbers are read off the entry that decided the round's
mark — the first reviewer handed a file, when there is one — so the pair is one
round's two numbers and never one round's size against another's limit: after
`--only`, entries of the same snapshot can carry limits from two
configurations. `null` where `change_chars` is, and for a round recorded before
the limit was configurable: it was 120,000 then, but nothing wrote it down and
this does not invent it. Recorded because the limit is a setting — a `partial`
round and a size alone do not say whether it was a large change or a low limit.

**`snapshot.over_budget`** — beside `sha256` and `files`: whether this round was
sent past `review.context.max_chars` by `--force`. Derived from the same
entries, for the same reason — the limit and the flag belong to the run, not to
the frozen file — and `false` both for a round that was not forced and for
every report written before the limit existed, which is the true answer for all
of them.

**`snapshot.budget_chars`** — the size that limit measured, which is what the
`Change:` line in `consolidated.md` prints. Not `coverage.change_chars`: on a
design round the budget counts the plan *and* the request, while the body a
reviewer was handed is the plan alone. On a code round that adopted
[surrounding context](#surrounding-context) it is the diff plus the context
adopted, since both went into the prompt. `null` when no run recorded one. Like
`over_budget`, it is recorded on every reviewer entry as well as on this block,
and this block is derived from those entries.

The two are separate because an incremental round inlines only the fix. Judging
the whole change by what *this* round inlined would let a fix-only round launder
a partial one: accept the finding, fix it, inline the fix, `NO_FINDINGS`, clean
— with four fifths of the change still unread by anyone. So the mark carries
across rounds.

It carries on the **workflow and the branch** — the round counter's key with
the base dropped. The base is the other way of saying which change, which is
exactly why the mark cannot hang on it: narrowing with `--base` makes the round
smaller and the change no more read than it was, so a mark keyed on the base
would be cleared by the first thing anyone tries after seeing one. The price,
and it is the one worth paying: a second, unrelated change on the same branch
inherits the mark until a full snapshot is reviewed inline.

What that buys is that the mark *survives* a change of base — it is not a
guarantee against every narrowing. `coverage.change` says a reviewer saw, in
full, the whole change the snapshot defines; re-base to a nearer commit and
review the smaller change in full and it reports `complete` about that smaller
change. Which is why the remedy named is splitting the change and reviewing the
parts, each part in full, and not moving the base and calling it done.

Clearing it is a change to the change, not another round, and not a narrower
view of the same one. Re-running the same snapshot sends the same prompt and
gets the same verdict:

| State | What clears it |
| --- | --- |
| `round: unverified` | Split the change and review the parts, so that each body fits inline — or raise `review.context.inline_chars` and snapshot again, if the larger prompt is worth paying for |
| `change: unverified`, `round: none` | `review run` — no reviewer has run against this snapshot, so nothing about it has been read yet |
| `change: unverified`, this round complete, no reviewer `ok` | Re-run the reviewers that did not come back `ok` — this snapshot was inlined and read by nobody |
| `change: unverified`, this round complete, some reviewer `ok` | `review snapshot --full` once the whole change fits inline, then `review run` — the round inlined the fix alone |
| Any of them, on a design round | Shorten `.ai/plan.md` until it fits inline (or raise `review.context.inline_chars`), then run the design round again — `review snapshot` writes the code snapshot and has no `--design` form |

The first row has one exception, and `review status` says so instead: once
`review.context.inline_chars` has been raised past the size the round recorded,
the same snapshot *would* be inlined now, so it says the limit has changed and
to run `review run` against it again. Splitting a change that already fits, or
raising a limit already raised, is what the standing advice would otherwise
tell the reader to do.

The three middle rows are one mark with three different things missing, and
naming the wrong one sends the reader to redo what they just did. So the state
is classified once (`coverage_state`) and `consolidated.md` and `review status`
word that one answer: the report says what the round is, the status line says
what to do about it, and they never describe the same report differently.

`review run` exits **1** on a round where no reviewer came back `ok`, partial
included: the round produced no claim that the change is fine, which is the
same thing every reviewer failing means. `review status` prints both values and
the one action that would change them.

Rounds recorded before this version have neither `delivery` nor `snapshot` on
their reviewer entries and are left out of the derivation rather than assumed.
A report from before this version has no `coverage` block at all, and neither
`consolidated.md` nor `review status` describes it as `none`: an unmeasured
round is not a round measured and found empty. Every run recorded so far was
comfortably under the limit — the largest measured 99,814 characters — so
nothing in the existing history is being read as clean when it was not; the
record simply does not say.

The design review's request section is a known exception to the *coverage*
rule: it is inlined whatever its size, because the change body a design round
reviews is the plan. It is not an exception to the size refusal below — that is
measured over the plan and the request together, since both go into the prompt.

The parser is deliberately tolerant: it accepts `**Severity:** high`,
`**Severity**: high`, `- Severity: high`, any heading level for `Finding`,
`Recommended fix`/`Recommendation` as synonyms for `Fix`, multi-line values, and
normalises paths (`./a\b.py` → `a/b.py`). Unknown severities become `medium`;
`nit`, `minor`, `style`, `info` become `low`; `blocker` becomes `critical`. A
block with neither a problem nor evidence is discarded.

## Deduplication

Two findings are merged when they sit at the **same locus** — same file, and
line numbers within 10 of each other (or no line number) — and their
problem + recommended-fix text matches with a similarity ratio ≥ 0.72.

Merging keeps the **highest** severity, the **longest** version of each text
field, and records every reviewer that reported it:

```json
{
  "id": "F1",
  "severity": "critical",
  "file": "app/models/user.rb",
  "line": "42",
  "category": "correctness",
  "reported_by": ["claude-general", "codex-security"],
  "duplicate_count": 2,
  "triage": "needs-triage",
  "triage_note": ""
}
```

Findings are numbered `F1…Fn` in severity order, so `F1` is always the most
serious. Two independent reviewers agreeing raises the prior that a finding is
real — it does not make it true.

### Why auto-merge stays conservative

Auto-merge only catches near-identical restatements. **Cross-model duplicates
almost never look alike in prose**, and the numbers are not close: measured on
real two-provider output over the same diff, a confirmed duplicate pair scored
**0.03** text similarity while an unrelated pair scored **0.29**. Any threshold
that merged the former would merge plenty of the latter — and collapsing two
distinct bugs hides one, which is a worse failure than listing one issue twice.

So the loose signal is separated out. Findings from **different** reviewers, in
the same file, that quote the same code are reported as **possible duplicates**:

```markdown
## Possible duplicates (confirm during triage)

- F1 ~ F7  (`cart.py`, shared: `code.split`)
- F3 ~ F8  (`cart.py`, shared: `i.price`)
```

Pairs are ranked by how rare the shared token is — code only those two findings
mention is much stronger evidence than an expression every finding quotes.
Findings from the *same* reviewer are never paired: reviewers are told not to
report an issue twice, so two of theirs are two issues by construction.

Confirm or dismiss each pair during triage:

```bash
dev-orchestra review triage F7 --status duplicate --note "same as F1"
```

On the sample above this caught every real duplicate (3 of 3) at the cost of
3 suggestions that were not. That trade is deliberate: a missed duplicate costs
the fixer redundant work, while a wrong merge costs a bug.

## Triage

```bash
dev-orchestra review show                       # the consolidated report
dev-orchestra review triage F1 F4 --status accepted --note "confirmed"
dev-orchestra review triage F2 --status rejected --note "guarded by caller"
dev-orchestra review triage F3 --status needs-investigation
dev-orchestra review show --accepted            # what the fixer will see
```

| Status | Meaning |
| --- | --- |
| `needs-triage` | Default. Not yet judged. |
| `accepted` | Verified against the code; the fixer will address it. |
| `rejected` | False positive, or out of scope. Record why. |
| `duplicate` | Same as another finding the dedupe pass missed. |
| `needs-investigation` | Cannot decide yet. Investigate, then re-triage. |

Triage decisions are keyed by content (file + normalised problem text), not by
the `F1..Fn` numbering, which is positional and gets reassigned every round. So
a decision follows its finding even when a more severe finding is fixed and
everything below it renumbers. Reword a finding and the decision is lost, which
is the honest outcome: it is no longer the same claim.

Reviewer reports are stamped with the snapshot they were written against, and a
report from an earlier snapshot is skipped rather than folded into the current
round -- otherwise re-consolidating would hand the fixer issues that were
already fixed.

Before accepting a finding: read the cited code as it is *now*, check the claim
is true in this codebase (not in general), and check the recommended fix does
not break something the reviewer could not see. Before rejecting: make sure you
are rejecting the claim, not just the tone.

## Re-review

The round counter belongs to *this* review, not to the project. It lives in
the consolidated report, which outlives any one change, so it is keyed on the
workflow, the branch and the base: a different branch, a different `--base`,
or a `budget reset` starts the count again. Without that key it counted every
snapshot ever taken in the directory, and a second branch with an unrelated
change opened at round 3 and was refused -- with no supported way to clear it,
because `budget reset` resets the ledger and the counter was not in it.

Returning to a branch you worked on earlier is a new review too. Nothing keeps
a count per branch; the question is only whether this round continues the last
one. Reviewing rather than refusing is the direction to fail in.

What has not changed is the loop the budget exists to stop: review, fix,
re-review on one change, on one branch, still runs out.

```bash
dev-orchestra review status --json
```

`iteration` is derived from the snapshot by `review run`, so the budget cannot
be defeated by forgetting to increment a counter.

`re_review_recommended` is true when unresolved findings at
`review.re_review_severities` (default critical + high) remain **and**
`iteration < max_review_iterations` (default 2). Rejected and duplicate findings
never block.

A re-review round is: `review snapshot` (the code changed, so the snapshot must
too) → `review run --iteration 2` → triage → fix. When the budget is exhausted,
the last round still gets its fix and re-test: `review fix-brief`, `run
review_fixer`, the tests again, recorded with `state record test ok|failed`
(`final_fix` goes `pending` → `retest` → `done`). Then report what remains and
stop; do not re-review. Two rounds catch the overwhelming majority of what
this pipeline is going to catch; a third mostly re-litigates.

### The second round only diffs the fix

Including when the change is a branch. `--base main` says what the *first*
round covers; whether a later round narrows to the fix is a separate question,
and the two used to be conflated -- so the ordinary way to review a branch
re-sent the whole branch every round. Measured on one real three-round review:
the diff grew 1,867 to 3,228 lines while the findings fell 11 to 5, and the
cost per finding went from $0.20 to $1.00.

Changing the base between rounds does take the whole change again. A different
base is a different definition of what is under review, and narrowing to a fix
for the previous definition would answer the question nobody asked this time.

Re-diffing everything against `HEAD` made round 2 cost the same as round 1 --
once per reviewer -- to look at a one-line fix. So a snapshot records the
working tree as a git tree object, and the next round diffs against the tree
the previous round actually reviewed:

```
$ dev-orchestra review snapshot
  strategy: git diff <previous round> <now>
  scope:    what changed since the last reviewed round, not the whole change
            whole change kept at .ai/reviews/review-target-full.diff
            reviewers also get the findings the fix was meant to address
```

The tree is written through a throwaway index -- the same trick `git stash
create` uses -- so the user's own index is never touched, and untracked files
are included because a new module is usually the most important part of a
change.

**The premise travels with it.** A reviewer is stateless and sees no other
reviewer's output, so a fix diff on its own is a change with no stated purpose:
"is this correct" cannot be answered without knowing what it was correcting. An
incremental round therefore also carries

- the accepted findings the fix was meant to address, one line each
- a pointer to `review-target-full.diff`, the whole change, frozen
- an instruction to say whether each finding is actually fixed, to report any
  new problem the fix introduced, and *not* to assume a listed item was real

Who reported what is deliberately left out. The findings arrive as the brief
the fixer worked from -- a fact about the change -- rather than as another
reviewer's opinion still in play, so reviewers still never see each other's
output. Measured, the premise costs about 80 tokens and replaces a few thousand
of re-sent diff.

With `review.context.surrounding: enclosing` a fix round also carries the
symbols enclosing the fix's hunks, read from this round's tree -- see
[Surrounding context](#surrounding-context).

It narrows the scope only when there is a round to be incremental to:

| Situation | Scope |
| --- | --- |
| First round | Whole change |
| Previous round was reviewed, produced accepted findings, and the tree changed since | The fix |
| Previous round found nothing, or everything was rejected | Whole change |
| Previous snapshot was never reviewed (a reviewer failed, so you re-snapshot) | Whole change |
| Nothing changed since the reviewed round | Whole change |
| `--base` given | Whole change from that base |
| `--full`, or `review.incremental_rounds: false` | Whole change |

Every row after the second is a case where narrowing would cost more than it
saves. Without the "never reviewed" and "nothing changed" rows an ordinary
re-snapshot would quietly become an empty diff. And a round following a review
that produced nothing to fix is reviewing *new work*, not checking a fix: there
is no brief to hand the reviewer, so it gets the whole change instead of a
fragment with nothing to judge it against.

**Triage before you re-snapshot.** The scope narrows on the accepted findings
existing, so re-snapshotting first gives you the whole change again.

**A narrowed round is only clean about the fix.** `coverage.round: complete` on
round 2 says the fix was inlined in full, and nothing about the change round 1
was handed as a file — which is why `coverage.change` is carried separately and
why round 2 answering `NO_FINDINGS` does not clear it. `review snapshot --full`
re-sends the whole change; once that fits inline and one reviewer of *that*
snapshot comes back `ok`, `coverage.change` goes back to `complete`. See
[Coverage](#coverage).

The tree is only recorded when the round might use one. `--base` and
`review.incremental_rounds: false` both say it will not, and writing it means
hashing every untracked-but-not-ignored file into the object database, which on
a repository with a large directory nobody remembered to ignore is neither
cheap nor invisible. The round after such a snapshot finds no tree and takes
the whole change, which is the safe direction to fall back in.
`review.context.surrounding: enclosing` writes one anyway, before the diff, to
extract the enclosing symbols from; it is kept as `surrounding.tree` and not as
`tree`, so it does not make the next round incremental.

## Running reviews on their own

The review pipeline is useful without the rest of the workflow:

```bash
dev-orchestra review snapshot --base main
dev-orchestra review run
dev-orchestra review show
```

Useful flags: `--only <id-or-role>` to run a subset, `--sequential` to run one
at a time while debugging, `--context "…"` to give every reviewer the same extra
background, `--timeout` to override the per-run limit.
