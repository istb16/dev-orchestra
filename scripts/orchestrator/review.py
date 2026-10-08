"""Independent multi-model review: snapshot, fan-out, parse, consolidate, triage.

Design rules enforced here:

* every reviewer sees the *same frozen snapshot*, taken before any reviewer runs
* reviewers never see each other's output -- no cross-contamination
* reviewers run read-only; a reviewer that edits files is a configuration bug
* one reviewer failing does not fail the batch

The same fan-out reviews a plan before implementation (``design_digest``, then
``write_design_snapshot``), under those same rules and with its own artifacts
and round counter.

Deduplication is deliberately split in two. Auto-merge only collapses findings
whose wording is near-identical, because collapsing two distinct bugs hides one.
Cross-model duplicates almost never look alike in prose -- measured on real
two-provider output, a confirmed duplicate pair scored 0.03 text similarity
while an unrelated pair scored 0.29 -- so they are surfaced as *candidates*,
matched on the code they quote, for the orchestrator to confirm during triage.
"""

from __future__ import annotations

from .review_common import (
    DEFAULT_EXCLUDE,
    DEFAULT_MAX_FINDINGS,
    DESIGN_REVIEW_PROMPT_TEMPLATE,
    DESIGN_ROLE_GUIDANCE,
    MAX_EVIDENCE_LINES,
    MAX_FIX_LINES,
    REVIEW_PROMPT_TEMPLATE,
    ROLE_GUIDANCE,
    SEVERITIES,
    SEVERITY_RANK,
    TRIAGE_STATUSES,
    accepted_findings,
)
from .review_consolidation import (
    are_duplicates,
    build_consolidation,
    code_tokens,
    collect_reports,
    consolidate_findings,
    consolidation_from,
    coverage_lineage,
    duplicate_candidates,
    finding_key,
    findings_signature,
    listed,
    next_iteration,
    read_reports,
    recorded_rounds,
    render_consolidation,
    render_fix_brief,
    report_snapshot,
    review_lineage,
    round_key,
    save_consolidation,
    set_triage,
    summarise_runs,
    surrounding_records,
    unresolved_blocking,
    update_consolidation,
)
from .review_coverage import (
    coverage_advice,
    coverage_headline,
    coverage_state,
    snapshot_reviewers,
    unverified_phrase,
)
from .review_fanout import (
    BuiltPrompt,
    FanoutOptions,
    ReviewerRun,
    RoundStamp,
    build_design_review_prompt,
    build_review_prompt,
    coverage_unverified_error,
    default_inline_chars,
    delivery_of,
    over_budget_note,
    over_context,
    prompt_delivery,
    render_limits,
    run_reviews,
    snapshot_chars,
)
from .review_parsing import (
    parse_findings,
    unparsed_report_warning,
)
from .review_snapshot import (
    ADDED_HEADING_RE,
    FENCE_RE,
    HEADING_RE,
    NONE_ADDED_RE,
    PlanScan,
    PlanToken,
    ReviewError,
    added_in_revision,
    carried_findings,
    create_design_snapshot,
    create_snapshot,
    current_snapshot_stamp,
    design_digest,
    plan_tokens,
    render_design_round_context,
    render_round_context,
    render_withheld,
    same_base,
    snapshot_stamp,
    withheld_lines,
    withholds,
    write_design_snapshot,
)
