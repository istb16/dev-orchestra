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
ai-orchestrator review snapshot                 # working tree vs HEAD
ai-orchestrator review snapshot --base main     # everything since main
ai-orchestrator review snapshot --no-untracked  # tracked changes only
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
workspace directory (`.ai/`) and any `.ai-orchestrator.yaml|yml|json` are
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

## Triage

```bash
ai-orchestrator review show                       # the consolidated report
ai-orchestrator review triage F1 F4 --status accepted --note "confirmed"
ai-orchestrator review triage F2 --status rejected --note "guarded by caller"
ai-orchestrator review triage F3 --status needs-investigation
ai-orchestrator review show --accepted            # what the fixer will see
```

| Status | Meaning |
| --- | --- |
| `needs-triage` | Default. Not yet judged. |
| `accepted` | Verified against the code; the fixer will address it. |
| `rejected` | False positive, or out of scope. Record why. |
| `duplicate` | Same as another finding the dedupe pass missed. |
| `needs-investigation` | Cannot decide yet. Investigate, then re-triage. |

Triage decisions survive re-consolidation as long as the finding's text is
unchanged, so a second round does not lose the first round's judgement.

Before accepting a finding: read the cited code as it is *now*, check the claim
is true in this codebase (not in general), and check the recommended fix does
not break something the reviewer could not see. Before rejecting: make sure you
are rejecting the claim, not just the tone.

## Re-review

```bash
ai-orchestrator review status --json
```

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
ai-orchestrator review snapshot --base main
ai-orchestrator review run
ai-orchestrator review show
```

Useful flags: `--only <id-or-role>` to run a subset, `--sequential` to run one
at a time while debugging, `--context "…"` to give every reviewer the same extra
background, `--timeout` to override the per-run limit.
