"""The `review status` command: where the review stands and what to do next."""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from . import approval as approval_mod
from . import config as config_mod
from . import context as context_mod
from . import ledger as ledger_mod
from . import optimization_design as opt_design
from . import questions as questions_mod
from . import review as review_mod
from . import workspace as ws
from .cli_common import (
    _emit_json,
    _ledger,
    _load_lenient,
    _out,
    _review_workspace,
    _workspace,
    _wrote_plan,
)
from .round_progress import (
    code_final_pass_from,
    design_decision,
    design_final_pass_from,
    design_lineage,
    design_round_plan,
    left_out_suffix,
)
from .summary import DESIGN_PANEL_SOURCES


def cmd_review_status(args: argparse.Namespace) -> int:
    loaded = _load_lenient(args.cwd)
    workspace = _review_workspace(args)
    # The ledger, the run log and the plan belong to the workflow, not to the
    # review directory `--design` points at.
    base = _workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    severities = loaded.blocking_severities()
    blocking = review_mod.unresolved_blocking(data, severities)
    accepted = bool(review_mod.accepted_findings(data))
    iteration = int(data.get("iteration", 0) or 0)
    # Read only: stalls are `status`'s to clear.
    book = _ledger(args, base)
    events = [event for event in (base.read_state().get("events") or []) if isinstance(event, dict)]
    budget = _status_budget(args, loaded, base, book, events, blocking, iteration, accepted)
    coverage = _status_coverage(data)
    payload, decision = _status_payload(args, loaded, base, data, budget, blocking, iteration, coverage)
    if args.json:
        _emit_json(payload)
    else:
        _print_status(
            args, loaded, data, payload, decision, budget, severities, blocking, iteration, coverage
        )
    return 0


class _StatusBudget(NamedTuple):
    """The round budget ``review status`` reports, and the final pass it allows."""

    key: str
    max_iterations: int
    final: Dict[str, Any]
    repeats: int


def _status_budget(
    args: argparse.Namespace,
    loaded: config_mod.LoadedConfig,
    base: ws.Workspace,
    book: ledger_mod.Ledger,
    events: List[Dict[str, Any]],
    blocking: List[Dict[str, Any]],
    iteration: int,
    accepted: bool,
) -> _StatusBudget:
    """The budget of the review ``--design`` picks, and where its final pass stands."""
    # Each budget is reported under the name of the setting it came from, so a
    # consumer holding both payloads can tell which one it was handed. The code
    # review's key is what it always was; only --design carries the other name.
    if args.design:
        budget_key = "max_iterations"
        max_iterations = loaded.design_max_iterations()
        final = design_final_pass_from(
            base,
            events,
            blocking,
            iteration=iteration,
            max_iterations=max_iterations,
            of_current_plan=approval_mod.findings_of_current_plan(base, approval_mod.read_plan(base)[0]),
            approval=approval_mod.current(
                base, bool(loaded.design_settings().get("require_approval")), questions_mod.asking(loaded)
            ),
            architect_left=book.remaining("architect"),
            accepted=accepted,
            wrote_plan=_wrote_plan(base),
        )
        repeats = book.repeats("design_review")
    else:
        budget_key = "max_review_iterations"
        max_iterations = loaded.max_review_iterations()
        final = code_final_pass_from(
            events,
            blocking,
            iteration=iteration,
            max_iterations=max_iterations,
            fixer_left=book.remaining("review_fixer"),
            accepted=accepted,
        )
        repeats = book.repeats("review")
    return _StatusBudget(budget_key, max_iterations, final, repeats)


def _status_coverage(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ``coverage`` ``review status`` reports for this consolidated report."""
    # No consolidated report at all is the only real "none": nothing has been
    # reviewed. A report with no `coverage` block is a round that happened
    # before coverage was recorded, and calling that `round none, change none`
    # would claim it had been measured and found empty. `render_consolidation`
    # leaves its line out for exactly that reason, and the two commands read
    # the same file -- they must not describe it differently.
    coverage = data.get("coverage")
    if not data:
        coverage = {
            "round": "none",
            "change": "none",
            "unverified_since": None,
            "change_chars": None,
            "inline_chars": None,
        }
    elif not isinstance(coverage, dict):
        coverage = None
    return coverage


def _status_payload(
    args: argparse.Namespace,
    loaded: config_mod.LoadedConfig,
    base: ws.Workspace,
    data: Dict[str, Any],
    budget: _StatusBudget,
    blocking: List[Dict[str, Any]],
    iteration: int,
    coverage: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Optional[opt_design.DesignDecision]]:
    """``review status --json``'s payload, and the design decision a ``--design`` one names."""
    max_iterations, final = budget.max_iterations, budget.final
    counts = data.get("counts", {})
    payload: Dict[str, Any] = {
        "iteration": iteration,
        budget.key: max_iterations,
        "blocking": [f["id"] for f in blocking],
        "blocking_count": len(blocking),
        "accepted_count": len(review_mod.accepted_findings(data)),
        "re_review_recommended": bool(blocking) and iteration < max_iterations,
        "iteration_budget_exhausted": iteration >= max_iterations,
        "coverage": coverage,
        # Counted over the snapshot the `coverage` beside it describes, because
        # this is the number printed in the same breath as that value. The
        # table-wide column is still in `counts`, under its own name.
        "reviewers_partial": review_mod.snapshot_reviewers(counts, "partial"),
        # A round that only ran because a human forced it past
        # review.context.max_chars. False for every round that was not, and
        # for every report written before the limit existed -- which is the
        # true answer for all of them: none of them was forced past it.
        "over_budget": bool((data.get("snapshot") or {}).get("over_budget")),
        "counts": counts,
    }
    # Only when the report recorded one, as `consolidated.md` does.
    surrounding = data.get("surrounding")
    if isinstance(surrounding, dict):
        payload["surrounding"] = surrounding
    decision = None
    if args.design:
        payload["final_revision"] = final["state"]
        payload["final_revision_pending"] = final["pending"]
        decision = design_decision(loaded, base)
        payload["enabled"] = decision.run
        payload["mode"] = decision.mode
        payload["reason"] = decision.reason or None
        # The panel the next design round would run, and who it would leave
        # out: the same inputs `review run --design` uses, the whole panel.
        panel = loaded.design_reviewers()
        payload["reviewers"] = [str(r.get("id")) for r in panel if isinstance(r, dict)]
        payload["panel_source"] = loaded.design_panel_source
        lineage = design_lineage(base, _ledger(args, base))
        payload["optimization"] = design_round_plan(loaded, base, panel, lineage=lineage).to_dict()
    else:
        payload["final_fix"] = final["state"]
        payload["final_fix_pending"] = final["pending"]
    return payload, decision


def _print_status(
    args: argparse.Namespace,
    loaded: config_mod.LoadedConfig,
    data: Dict[str, Any],
    payload: Dict[str, Any],
    decision: Optional[opt_design.DesignDecision],
    budget: _StatusBudget,
    severities: Tuple[str, ...],
    blocking: List[Dict[str, Any]],
    iteration: int,
    coverage: Optional[Dict[str, Any]],
) -> None:
    """``review status`` without ``--json``."""
    counts = data.get("counts", {})
    surrounding = data.get("surrounding")
    if decision is not None:
        _out("design review: %s" % decision.label())
        _out("design panel: %s" % _design_panel_line(payload))
    _out("iteration %d/%d" % (iteration, budget.max_iterations))
    _out("accepted findings: %d" % payload["accepted_count"])
    _out("blocking (%s): %d %s" % ("/".join(severities), len(blocking), ", ".join(payload["blocking"])))
    _out("re-review recommended: %s" % ("yes" if payload["re_review_recommended"] else "no"))
    if coverage is None:
        _out("coverage: not recorded -- this report predates it")
    else:
        _out("coverage: round %s, change %s" % (coverage.get("round"), coverage.get("change")))
    if payload["over_budget"]:
        _out(
            "over budget: this round was sent past review.context.max_chars by --force; "
            "say so when you report it"
        )
    if isinstance(surrounding, dict):
        for line in _surrounding_status_lines(surrounding):
            _out(line)
    for line in review_mod.coverage_advice(
        coverage or {},
        counts,
        payload["iteration_budget_exhausted"],
        _configured_inline_chars(loaded),
        args.design,
    ):
        _out(line)
    if payload["iteration_budget_exhausted"] and blocking:
        _out(
            "iteration budget exhausted -- %s" % _final_pass_advice(args.design, budget.final, budget.repeats)
        )


def _design_panel_line(payload: Dict[str, Any]) -> str:
    """``a, b (the code panel; when conditions ignored); b left out (reason)``."""
    line = "%s (%s)" % (
        ", ".join(payload.get("reviewers") or []) or "none",
        DESIGN_PANEL_SOURCES.get(str(payload.get("panel_source")), str(payload.get("panel_source"))),
    )
    return line + left_out_suffix((payload.get("optimization") or {}).get("conditional") or [])


def _surrounding_status_lines(block: Dict[str, Any]) -> List[str]:
    """The context this snapshot's reviewers were shown, and what was left out by name."""
    lines = []
    for reviewer_id, record in review_mod.surrounding_records(block):
        label = "surrounding context (%s)" % reviewer_id if reviewer_id else "surrounding context"
        if not isinstance(record, dict):
            lines.append("%s: none (ran with review.context.surrounding none)" % label)
            continue
        lines.append("%s: %s" % (label, context_mod.status_line(record)))
        trimmed = review_mod.listed(record, "trimmed")
        for candidate in trimmed[:5]:
            lines.append(
                "  left out: %s:%s-%s %s"
                % (
                    candidate.get("path"),
                    candidate.get("start"),
                    candidate.get("end"),
                    candidate.get("symbol"),
                )
            )
        if len(trimmed) > 5:
            lines.append("  and %d more" % (len(trimmed) - 5))
        skipped = review_mod.listed(record, "skipped")
        for entry in skipped[:5]:
            lines.append("  not extracted: %s -- %s" % (entry.get("path"), entry.get("reason") or "?"))
        if len(skipped) > 5:
            lines.append("  and %d more not extracted" % (len(skipped) - 5))
    return lines


def _final_pass_advice(design: bool, final: Dict[str, Any], repeats: int) -> str:
    """What `review status` says to do once the round budget is spent."""
    state = final["state"]
    if design and state == "pending":
        advice = (
            "fold the accepted findings into the plan once more (review fix-brief --design, then "
            "run architect) and do not re-review it; then present the plan and the findings to the "
            "user and ask"
        )
    elif design and state == "done" and final.get("plan_changed") is True:
        advice = (
            "the plan was revised after this round and is not re-reviewed; present it with the "
            "findings still open from the earlier revision and ask"
        )
    elif design and state == "done" and final.get("plan_changed") is False:
        advice = (
            "the architect ran after this round and left the plan unchanged; present the plan and "
            "the open findings and ask whether to approve over them or to triage them again"
        )
    elif design and state == "approved":
        advice = "the plan is approved over the open findings; do not revise or re-review it"
    elif design and state == "blocked":
        advice = (
            "the accepted findings are not folded in and no architect attempt is left "
            "(budgets.architect); report them and ask whether to approve over them (`design approve`) "
            "or to free an attempt (`budget reset`) and revise"
        )
    elif not design and state == "pending":
        advice = (
            "fix the accepted findings once more (review fix-brief, then run review_fixer), re-test "
            "and record it (state record test ok|failed), and do not re-review; then report what is "
            "still open"
        )
    elif not design and state == "retest":
        advice = (
            "the fix after this round is not re-reviewed; re-run the tests, record them "
            "(state record test ok|failed), then report the remaining findings"
        )
    elif not design and state == "done":
        advice = "fixed and re-tested after this round, not re-reviewed; report the remaining findings"
    elif not design and state == "blocked":
        advice = (
            "the accepted findings are not fixed and no review_fixer attempt is left "
            "(budgets.review_fixer); report them, or free an attempt (`budget reset`) and fix"
        )
    else:
        # `implemented`, `unaccepted` (nothing to fold in or fix), and a
        # design round with no frozen plan to compare.
        advice = "report the remaining findings instead of looping"
    # The repeat does not stop a revision or fix still owed; it is said so the
    # report can.
    if repeats > 1 and state in ("pending", "retest"):
        advice += " (this round repeated the previous round's findings)"
    return advice


def _configured_inline_chars(loaded: config_mod.LoadedConfig) -> int:
    """``review.context.inline_chars``, resolved without trusting the file.

    ``cmd_review_status`` loads with ``validate_result=False`` on purpose: it
    is the one review command written to answer while the configuration is
    broken. So the value here can be whatever a hand edit left behind --
    ``400k``, a list, ``true`` -- and ``int()`` on it raises ``ValueError``,
    which ``main`` does not catch. That is the command written to survive a
    broken config dying on one, while every other command reports "invalid
    configuration" and exits 2. Anything that is not a plain integer falls
    back to the shipped default, which is what an unreadable setting is worth.
    Thread any further setting into this command the same way.
    """
    value = loaded.context_settings().get("inline_chars")
    if ws.is_int(value):
        return value
    return review_mod.default_inline_chars()
