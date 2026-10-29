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
from typing import Any, Dict, List, Mapping, NamedTuple, Optional

from . import approval as approval_mod
from . import config as config_mod
from . import instructions, round_progress
from . import optimization as opt_mod
from . import questions as questions_mod
from . import workspace as ws
from .review_common import SEVERITY_RANK

#: Set to say nobody is watching, where ``CI`` is not set.
UNATTENDED_ENV = "DEV_ORCHESTRA_UNATTENDED"

_FALSE_WORDS = ("", "0", "false", "no", "off")


class Requirement(NamedTuple):
    """Whether this plan needs approving, and why when ``auto`` decided it."""

    #: ``on``, ``off`` or ``auto``.
    mode: str
    required: bool
    #: Empty under ``on`` and ``off``.
    reason: str = ""

    def label(self) -> str:
        """``required`` or ``not required``, with the reason under ``auto``."""
        answer = "required" if self.required else "not required"
        if self.mode != "auto":
            return "%s (design.require_approval: %s)" % (answer, "true" if self.required else "false")
        return "%s (design.require_approval: auto -- %s)" % (answer, self.reason)


def unattended(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the environment says nobody is watching this run."""
    env = os.environ if environ is None else environ
    return any(str(env.get(key) or "").strip().lower() not in _FALSE_WORDS for key in ("CI", UNATTENDED_ENV))


def nobody_watching(loaded: config_mod.LoadedConfig, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a question put to the user now would go unanswered.

    ``false`` says so by itself; ``auto`` says so when the environment does.
    ``true`` keeps waiting, as it always has.
    """
    mode = config_mod.approval_mode(loaded.design_settings().get("require_approval"))
    return mode == "off" or (mode == "auto" and unattended(environ))


def requirement(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, environ: Optional[Mapping[str, str]] = None
) -> Requirement:
    """Whether the plan as it is now needs the user's approval."""
    mode = config_mod.approval_mode(loaded.design_settings().get("require_approval"))
    if mode != "auto":
        return Requirement(mode, mode == "on")
    if not unattended(environ):
        return Requirement(mode, True, "someone may be watching: neither CI nor %s is set" % UNATTENDED_ENV)
    plan_state, scan = round_progress.plan_scan(workspace)
    if plan_state != "ok":
        return Requirement(mode, True, "no readable plan")
    hits = opt_mod.design_risk(loaded.optimization_settings(), scan, instructions.watched_paths(loaded))
    if hits:
        more = " and %d more" % (len(hits) - 1) if len(hits) > 1 else ""
        return Requirement(mode, True, "the plan touches %s%s" % (ws.redact(hits[0][0]), more))
    return _by_design_review(loaded, workspace, mode)


def _by_design_review(loaded: config_mod.LoadedConfig, workspace: ws.Workspace, mode: str) -> Requirement:
    """The design review's half of ``auto``, for an unattended plan that is not high-risk."""
    design_data = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    latest = approval_mod.design_round(workspace)
    if latest is None and not int(design_data.get("iteration", 0) or 0):
        decision = round_progress.design_decision(loaded, workspace)
        if decision.mode == "off":
            return Requirement(mode, True, "the design review is off (review.design.enabled: false)")
        if decision.run:
            return Requirement(mode, True, "the design review has not run on this plan")
        return Requirement(
            mode, False, "nobody watching; the design review was skipped (%s)" % decision.reason
        )
    if latest is None or latest != approval_mod.reported_round(design_data):
        return Requirement(mode, True, "the latest design review round has no report")
    if _declared_high_risk(workspace, latest):
        return Requirement(mode, True, "the design review ran with --high-risk")
    plan_text, _ = approval_mod.read_plan(workspace)
    if approval_mod.findings_of_current_plan(workspace, plan_text) is not True:
        return Requirement(mode, True, "the latest design review was of an earlier revision of the plan")
    above = _above_low(approval_mod.open_findings(design_data))
    if above:
        return Requirement(
            mode, True, "%d design finding(s) above Low still open: %s" % (len(above), ", ".join(above))
        )
    return Requirement(
        mode, False, "nobody watching, not high-risk, and the design review left nothing above Low"
    )


def _above_low(findings: List[Dict[str, Any]]) -> List[str]:
    """The ids of the findings above Low; one with no known severity counts as Medium, as elsewhere."""
    low = SEVERITY_RANK["low"]
    return [
        str(f.get("id"))
        for f in findings
        if SEVERITY_RANK.get(str(f.get("severity") or "medium").lower(), SEVERITY_RANK["medium"]) < low
    ]


def _declared_high_risk(workspace: ws.Workspace, round_id: str) -> bool:
    """Whether the design round ``round_id`` was run with ``--high-risk``, from its run-log event."""
    for event in workspace.read_state().get("events") or []:
        if not isinstance(event, dict) or event.get("stage") != "design_review":
            continue
        if str(event.get("round_id") or "") != round_id:
            continue
        optimization = event.get("optimization")
        if isinstance(optimization, dict) and optimization.get("declared"):
            return True
    return False


def current(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, environ: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    """``approval.current`` under the requirement in force, with its mode and reason."""
    need = requirement(loaded, workspace, environ)
    info = approval_mod.current(workspace, need.required, questions_mod.asking(loaded))
    info["mode"] = need.mode
    info["reason"] = need.reason or None
    info["requirement"] = need.label()
    return info
