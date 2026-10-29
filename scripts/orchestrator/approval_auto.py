"""Whether the plan needs the user's approval, under ``design.require_approval``.

``true`` always asks and ``false`` never does. ``auto`` (the default) leaves
the approval out only when all three hold, and asks otherwise:

- nobody is watching: ``CI`` or ``DEV_ORCHESTRA_UNATTENDED`` is set to
  anything but an empty, ``0``, ``false``, ``no`` or ``off`` value. The CLI is
  always run by an agent, so a terminal says nothing about a person;
- the plan is not high-risk: no token matches a high-risk pattern, and the
  design round of it was not run with ``--high-risk``;
- the design review left nothing above Low: either it was skipped because
  ``review.design.enabled: auto`` judged the plan small and contained, or its
  latest round reviewed this very plan and every finding still open is Low.
  A round without a report, a review of an earlier revision, a review that is
  owed but has not run, and ``review.design.enabled: false`` all ask.

Kept apart from ``approval`` because it reads the configuration, the plan's
risk and the design review's verdict, none of which the record itself needs.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, NamedTuple, Optional, Tuple

from . import approval as approval_mod
from . import config as config_mod
from . import instructions, review_loop, round_progress
from . import optimization as opt_mod
from . import questions as questions_mod
from . import workspace as ws

#: Says whether anybody is watching. Set, it decides alone, so ``0`` keeps the
#: approval where a tool sets ``CI`` only to keep other programs from prompting.
UNATTENDED_ENV = "DEV_ORCHESTRA_UNATTENDED"

_FALSE_WORDS = ("0", "false", "no", "off")


class Requirement(NamedTuple):
    """Whether this plan needs approving, and why when ``auto`` decided it."""

    #: ``on``, ``off`` or ``auto``.
    mode: str
    required: bool
    #: Empty under ``on`` and ``off``.
    reason: str = ""
    #: The sha256 of the plan text the decision was made on, "" when there was none.
    digest: str = ""

    def label(self) -> str:
        """``required`` or ``not required``, with the reason under ``auto``."""
        answer = "required" if self.required else "not required"
        if self.mode != "auto":
            return "%s (design.require_approval: %s)" % (answer, "true" if self.required else "false")
        return "%s (design.require_approval: auto -- %s)" % (answer, self.reason)


def unattended(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the environment says nobody is watching this run.

    ``DEV_ORCHESTRA_UNATTENDED``, when set to anything but empty, decides;
    otherwise ``CI`` does. Either one counts as unset when empty, and as
    "watching" when ``0``, ``false``, ``no`` or ``off``.
    """
    env = os.environ if environ is None else environ
    for key in (UNATTENDED_ENV, "CI"):
        value = str(env.get(key) or "").strip().lower()
        if value:
            return value not in _FALSE_WORDS
    return False


def nobody_watching(loaded: config_mod.LoadedConfig, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a question put to the user now would go unanswered.

    ``false`` says so by itself; ``auto`` says so when the environment does.
    ``true`` keeps waiting, as it always has.
    """
    mode = config_mod.approval_mode(loaded.design_settings().get("require_approval"))
    return mode == "off" or (mode == "auto" and unattended(environ))


def requirement(
    loaded: config_mod.LoadedConfig,
    workspace: ws.Workspace,
    environ: Optional[Mapping[str, str]] = None,
    plan_text: Optional[str] = None,
) -> Requirement:
    """Whether the plan needs the user's approval.

    ``plan_text`` is the plan as the caller read it, strictly (``None``:
    read it here), so the decision is about the very text the caller gates.
    """
    mode = config_mod.approval_mode(loaded.design_settings().get("require_approval"))
    if mode != "auto":
        return Requirement(mode, mode == "on")
    if not unattended(environ):
        return Requirement(mode, True, "someone may be watching: neither CI nor %s is set" % UNATTENDED_ENV)
    if plan_text is None:
        plan_text = ws.read_text_strict(workspace.plan_path)
    scanned = round_progress.scan_of(plan_text)
    if scanned[0] != "ok" or plan_text is None:
        return Requirement(mode, True, "no readable plan")
    digest = approval_mod.plan_digest(plan_text)
    hits = opt_mod.design_risk(loaded.optimization_settings(), scanned[1], instructions.watched_paths(loaded))
    if hits:
        more = " and %d more" % (len(hits) - 1) if len(hits) > 1 else ""
        return Requirement(mode, True, "the plan touches %s%s" % (ws.redact(hits[0][0]), more), digest)
    required, reason = _by_design_review(loaded, workspace, plan_text, scanned)
    return Requirement(mode, required, reason, digest)


def _by_design_review(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, plan_text: str, scanned: Tuple[str, Any]
) -> Tuple[bool, str]:
    """The design review's half of ``auto``, for an unattended plan that is not high-risk."""
    design_data = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    latest = approval_mod.design_round(workspace)
    if latest is None and not int(design_data.get("iteration", 0) or 0):
        decision = round_progress.design_decision(loaded, workspace, scanned, design_data)
        if decision.mode == "off":
            return True, "the design review is off (review.design.enabled: false)"
        if decision.run:
            return True, "the design review has not run on this plan"
        return False, "nobody watching; the design review was skipped (%s)" % decision.reason
    if latest is None or latest != approval_mod.reported_round(design_data):
        return True, "the latest design review round has no report"
    short = _short_coverage(design_data)
    if short:
        return True, short
    if _declared_high_risk(workspace, latest):
        return True, "the design review ran with --high-risk"
    if approval_mod.findings_of_current_plan(workspace, plan_text) is not True:
        return True, "the latest design review was of an earlier revision of the plan"
    # The same line the review loop draws: Medium and up, plus whatever
    # review.re_review_severities makes blocking.
    wanted = review_loop.above_low_severities(loaded.blocking_severities())
    above = [
        str(f.get("id"))
        for f in approval_mod.open_findings(design_data)
        if str(f.get("severity") or "medium").lower() in wanted
    ]
    if above:
        return True, "%d design finding(s) above Low still open: %s" % (len(above), ", ".join(above))
    return False, "nobody watching, not high-risk, and the design review left nothing above Low"


def _short_coverage(design_data: Dict[str, Any]) -> str:
    """Why the latest design round did not review the whole plan, or "".

    Its findings say nothing about what a reviewer did not read: a reviewer
    that failed or ran ``partial``, or a plan handed over as a file
    (``coverage.round`` other than ``complete``), asks.
    """
    counts = design_data.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    failed = int(counts.get("snapshot_reviewers_failed") or 0)
    partial = int(counts.get("snapshot_reviewers_partial") or 0)
    if failed or partial:
        return "%d design reviewer(s) failed and %d ran partial in the latest round" % (failed, partial)
    if not int(counts.get("snapshot_reviewers_ok") or 0):
        return "no design reviewer finished the latest round"
    coverage = design_data.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("round") != "complete":
        return "the latest design round did not show the whole plan to every reviewer"
    return ""


def _declared_high_risk(workspace: ws.Workspace, round_id: str) -> bool:
    """Whether the design round ``round_id`` was run with ``--high-risk``, from its run-log event.

    Read from the end: the round's event is among the last, and the first one
    found for it answers.
    """
    for event in reversed(workspace.read_state().get("events") or []):
        if not isinstance(event, dict) or event.get("stage") != "design_review":
            continue
        if str(event.get("round_id") or "") != round_id:
            continue
        optimization = event.get("optimization")
        return isinstance(optimization, dict) and bool(optimization.get("declared"))
    return False


def current(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, environ: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    """``approval.current`` under the requirement in force, with its mode and reason.

    The plan is read once for the decision. If the plan ``approval.current``
    then reads is another text, it changed in between, and approval is
    required: the decision was about a plan nobody is about to run.
    """
    need = requirement(loaded, workspace, environ, ws.read_text_strict(workspace.plan_path))
    asking = questions_mod.asking(loaded)
    info = approval_mod.current(workspace, need.required, asking)
    if not need.required and need.digest and info.get("plan_sha256") != need.digest:
        need = need._replace(required=True, reason="the plan changed while it was being judged")
        info = approval_mod.current(workspace, True, asking)
    info["mode"] = need.mode
    info["reason"] = need.reason or None
    info["requirement"] = need.label()
    info["nobody_watching"] = nobody_watching(loaded, environ)
    # Answered by nobody: whoever approves the plan is shown them too.
    info["unattended_questions"] = questions_mod.unattended_ids(workspace.read_state())
    return info
