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
  "empty": false
}
```

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
overwrites the rest.

**Cost.** A round is about what a code review round costs: reviewers read the
files the plan names. `review.design.max_iterations` (default 2) bounds it, and
`1` is the cheap setting — one round, then report what is still open.

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
report what remains and stop. Two rounds catch the overwhelming majority of what
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

The tree is only recorded when the round might use one. `--base` and
`review.incremental_rounds: false` both say it will not, and writing it means
hashing every untracked-but-not-ignored file into the object database, which on
a repository with a large directory nobody remembered to ignore is neither
cheap nor invisible. The round after such a snapshot finds no tree and takes
the whole change, which is the safe direction to fall back in.

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
