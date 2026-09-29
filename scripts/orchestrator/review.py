"""Independent multi-model review: snapshot, fan-out, parse, consolidate, triage.

Design rules enforced here:

* every reviewer sees the *same frozen snapshot*, taken before any reviewer runs
* reviewers never see each other's output -- no cross-contamination
* reviewers run read-only; a reviewer that edits files is a configuration bug
* one reviewer failing does not fail the batch

The same fan-out reviews a plan before implementation (``create_design_snapshot``),
under those same rules and with its own artifacts and round counter.

Deduplication is deliberately split in two. Auto-merge only collapses findings
whose wording is near-identical, because collapsing two distinct bugs hides one.
Cross-model duplicates almost never look alike in prose -- measured on real
two-provider output, a confirmed duplicate pair scored 0.03 text similarity
while an unrelated pair scored 0.29 -- so they are surfaced as *candidates*,
matched on the code they quote, for the orchestrator to confirm during triage.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import posixpath
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from . import context as context_mod
from . import workspace as ws
from .optimization import DEFAULT_LEVEL, MAX_FINDINGS_BY_LEVEL
from .providers import MODE_REVIEW, ModelResolutionError, Usage, get_provider
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
)
from .review_consolidation import (
    _BACKTICK_RE,
    _CODEISH_RE,
    _REPORT_SNAPSHOT_RE,
    _STOPWORDS,
    _WORD_RE,
    _absorb,
    _branch,
    _budget_chars,
    _built_for_another_round,
    _counts,
    _coverage,
    _coverage_line,
    _current_runs,
    _fingerprint,
    _line_number,
    _note_suffix,
    _over_budget_line,
    _reviewer_lines,
    _same_locus,
    _surrounding,
    _surrounding_line,
    _surrounding_names,
    _surrounding_section,
    _tally,
    accepted_findings,
    are_duplicates,
    build_consolidation,
    code_tokens,
    collect_reports,
    consolidate_findings,
    coverage_lineage,
    coverage_state,
    current_snapshot_stamp,
    duplicate_candidates,
    finding_key,
    findings_signature,
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
    snapshot_reviewers,
    summarise_runs,
    unresolved_blocking,
    unverified_phrase,
)
from .review_fanout import (
    BuiltPrompt,
    ReviewerRun,
    _delivery_of,
    _forced_consequence,
    _handover_note,
    _inline_limit,
    _snapshot_sha,
    _stamp,
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
    _FIELD_ALIASES,
    _FIELD_RE,
    _HEADER_MARKER,
    _HEADER_RE,
    _NO_FINDINGS_RE,
    _SEVERITY_LINE_RE,
    _join,
    _normalise_path,
    _normalise_severity,
    _parse_block,
    _split_blocks,
    _strip_report_header,
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
    _count_lines,
    _diff,
    _diff_line_counts,
    _head,
    _is_orchestrator_artifact,
    _maybe_int,
    _not_under_review,
    _numstat,
    _parse_numstat,
    _pathspecs,
    _reviewed_files,
    _reviewed_tree,
    _touched_paths,
    _untracked_paths,
    _withheld_entry,
    _write_tree,
    added_in_revision,
    carried_findings,
    create_design_snapshot,
    create_snapshot,
    design_digest,
    plan_tokens,
    render_design_round_context,
    render_round_context,
    render_withheld,
    withheld_lines,
    withholds,
    write_design_snapshot,
)
