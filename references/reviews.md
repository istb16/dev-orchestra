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
```

Writes `.ai/reviews/review-target.diff` plus metadata:

```json
{
  "strategy": "git diff HEAD",
  "base": null,
  "head": "9f2c…",
  "files": ["app/services/pricing.rb", "spec/services/pricing_spec.rb"],
  "untracked_included": ["app/services/pricing.rb"],
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
- Problem: <what is wrong>
- Impact: <what breaks, and when>
- Evidence: <the code or hunk that shows it>
- Recommended fix: <the concrete change>
```

A clean review returns exactly `NO_FINDINGS`.

A reviewer that returns neither findings in a recognisable shape nor
`NO_FINDINGS` is recorded with status `unparsed` and counted as **failed**. A
report that cannot be read is not evidence that the code is fine, and treating
it as such is the worst way for a review tool to fail.

The parser is deliberately tolerant: it accepts `**Severity:** high`,
`**Severity**: high`, `- Severity: high`, any heading level for `Finding`,
`Recommendation`/`Fix` as synonyms for `Recommended fix`, multi-line values, and
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
