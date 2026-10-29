"""Whether the plan needs the user's approval, under ``design.require_approval``.

``true`` always asks and ``false`` never does. ``auto`` (the default) leaves
the approval out only when all three hold, and asks otherwise:

- nobody is watching: ``CI`` or ``DEV_ORCHESTRA_UNATTENDED`` is set to
  anything but an empty, ``0``, ``false``, ``no`` or ``off`` value. The CLI is
  always run by an agent, so a terminal says nothing about a person;
- the plan is not high-risk: no token matches a high-risk pattern, and no
  design round of the workflow was run with ``--high-risk``. The patterns are
  the defaults' and the global config's: a project file can come with the
  branch under review, so it may add patterns but not take any away;
- the design review left nothing above Low: either it was skipped because
  ``review.design.enabled: auto`` judged the plan small and contained, or its
  latest round reviewed this very plan and every finding still open is Low.
  A round without a report, a review of an earlier revision, a review that is
  owed but has not run, and ``review.design.enabled: false`` all ask. So do a
  skip the global config would not make (its ``review.design.enabled`` is not
  ``auto``) and a round run by a design panel the project file lists.

Kept apart from ``approval`` because it reads the configuration, the plan's
risk and the design review's verdict, none of which the record itself needs.
The keys ``current`` adds to ``approval.current``'s answer are read here
alone, by ``pending_notes`` and ``refusal_lines``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

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

#: The high-risk pattern keys a project file may add to but not narrow.
_RISK_KEYS = ("high_risk_paths", "extra_high_risk_paths")


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
        if self.mode != "auto":
            return "%s (design.require_approval: %s)" % (
                config_mod.approval_mode_shown(self.mode),
                "true" if self.required else "false",
            )
        answer = "required" if self.required else "not required"
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


def trusted_risk_settings(
    loaded: config_mod.LoadedConfig, without_project: Optional[config_mod.LoadedConfig] = None
) -> Dict[str, Any]:
    """``optimization_settings`` with high-risk patterns a project file cannot narrow.

    ``high_risk_paths`` is every pattern in force without the project file
    (``LoadedConfig.without_project``), and ``extra_high_risk_paths``
    whatever the project file names under either key: added, never in place
    of the others. A project file can come with the branch under review, and
    an empty list there would otherwise let a plan on a sensitive path skip
    both the design review and the approval. ``without_project`` is that
    configuration when the caller has it; a global file that does not
    compose alone falls back to the defaults' patterns and those in force.
    """
    settings = loaded.optimization_settings()
    if without_project is None:
        without_project = loaded.without_project()
    if without_project is not None:
        trusted = opt_mod.risk_patterns(without_project.optimization_settings())
    else:
        trusted = opt_mod.risk_patterns(config_mod.default_config()["optimization"])
        trusted += [p for p in opt_mod.risk_patterns(settings) if p not in trusted]
    project_block = loaded.project_layer.get("optimization")
    project_block = project_block if isinstance(project_block, dict) else {}
    added: List[Any] = []
    for key in _RISK_KEYS:
        value = project_block.get(key)
        if isinstance(value, list):
            added.extend(value)
    settings["high_risk_paths"] = trusted
    settings["extra_high_risk_paths"] = added
    return settings


def requirement(
    loaded: config_mod.LoadedConfig,
    workspace: ws.Workspace,
    environ: Optional[Mapping[str, str]] = None,
    plan_text: Optional[str] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Requirement:
    """Whether the plan needs the user's approval.

    ``plan_text`` is the plan as the caller read it, strictly (``None``:
    read it here), so the decision is about the very text the caller gates.
    ``state`` is ``state.json`` as the caller read it (``None``: read it here).
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
    without_project = loaded.without_project()
    risk = trusted_risk_settings(loaded, without_project)
    hits = opt_mod.design_risk(risk, scanned[1], instructions.watched_paths(loaded))
    if hits:
        more = " and %d more" % (len(hits) - 1) if len(hits) > 1 else ""
        return Requirement(mode, True, "the plan touches %s%s" % (ws.redact(hits[0][0]), more), digest)
    if state is None:
        state = workspace.read_state()
    if _declared_high_risk(workspace, state):
        return Requirement(mode, True, "the design review ran with --high-risk", digest)
    required, reason = _by_design_review(loaded, without_project, workspace, plan_text, scanned, risk, state)
    return Requirement(mode, required, reason, digest)


def _by_design_review(
    loaded: config_mod.LoadedConfig,
    without_project: Optional[config_mod.LoadedConfig],
    workspace: ws.Workspace,
    plan_text: str,
    scanned: Tuple[str, Any],
    risk: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[bool, str]:
    """The design review's half of ``auto``, for an unattended plan that is not high-risk.

    ``risk`` is ``trusted_risk_settings``: a skip is accepted only where the
    patterns a project file cannot narrow would skip too. The project file
    can come with the branch under review, so a skip is also accepted only
    where ``review.design.enabled`` is ``auto`` without it
    (``without_project``), and a clean round only from a panel it did not set.
    """
    # Before anything a past round says: turned off, it reviews nothing
    # from now on, so an earlier clean round does not speak for this plan.
    if config_mod.design_review_mode(loaded.design_review_settings().get("enabled")) == "off":
        return True, "the design review is off (review.design.enabled: false)"
    design_data = ws.read_json(workspace.design_review().consolidated_json_path, {}) or {}
    latest = approval_mod.design_round(workspace)
    if latest is None and not int(design_data.get("iteration", 0) or 0):
        decision = round_progress.design_decision(
            loaded, workspace, scanned=scanned, design_data=design_data, optimization=risk
        )
        if decision.run:
            return True, "the design review has not run on this plan"
        if not _skips_without_project(without_project):
            return True, (
                "the design review was skipped by review.design.enabled: auto from the project config"
            )
        return False, "nobody watching; the design review was skipped (%s)" % decision.reason
    if latest is None or latest != approval_mod.reported_round(design_data):
        return True, "the latest design review round has no report"
    if _panel_from_project(loaded):
        return True, "the design review panel is the project config's"
    short = _short_coverage(design_data)
    if short:
        return True, short
    if approval_mod.findings_of_current_plan(workspace, plan_text, state) is not True:
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


def _skips_without_project(without_project: Optional[config_mod.LoadedConfig]) -> bool:
    """Whether ``review.design.enabled`` is ``auto`` without the project file.

    A global file that does not compose alone says nothing, and asks.
    """
    if without_project is None:
        return False
    enabled = without_project.design_review_settings().get("enabled")
    return config_mod.design_review_mode(enabled) == "auto"


def _panel_from_project(loaded: config_mod.LoadedConfig) -> bool:
    """Whether the design panel's own seats are the project file's.

    A project file that lists either panel replaces it whole, and its seats
    may be ones that never report a finding. Its extras only join the panel
    the defaults and the global file give, so they do not count.
    """
    return any(
        origin.layer == "project" and not origin.extra
        for origin in (loaded.design_reviewer_origin(i) for i in range(len(loaded.design_reviewers())))
    )


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


def _declared_high_risk(workspace: ws.Workspace, state: Dict[str, Any]) -> bool:
    """Whether any design round of this workflow was run with ``--high-risk``.

    The snapshot's ``declared_high_risk`` says so from the moment a round
    starts, and stays once given (``review_snapshot.write_design_snapshot``);
    only ``review run --design`` writes it. A run-log event that says so is
    believed too, for a round from before that key, but an event never
    clears it: ``state record`` can append one that says anything.
    """
    meta = workspace.design_review().read_snapshot_meta()
    if isinstance(meta, dict) and meta.get("declared_high_risk") is True:
        return True
    for event in state.get("events") or []:
        if not isinstance(event, dict) or event.get("stage") != "design_review":
            continue
        optimization = event.get("optimization")
        if isinstance(optimization, dict) and optimization.get("declared"):
            return True
    return False


def current(
    loaded: config_mod.LoadedConfig, workspace: ws.Workspace, environ: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    """``approval.current`` under the requirement in force, with its mode and reason.

    ``state.json`` is read once, and so is the plan for the decision. If the
    plan ``approval.current`` then reads is another text, it changed in
    between, and approval is required: the decision was about a plan nobody
    is about to run.
    """
    state = workspace.read_state()
    need = requirement(loaded, workspace, environ, ws.read_text_strict(workspace.plan_path), state)
    asking = questions_mod.asking(loaded)
    info = approval_mod.current(workspace, need.required, asking, state)
    if not need.required and need.digest and info.get("plan_sha256") != need.digest:
        need = need._replace(required=True, reason="the plan changed while it was being judged")
        info = approval_mod.current(workspace, True, asking, state)
    info["mode"] = need.mode
    info["reason"] = need.reason or None
    info["requirement"] = need.label()
    info["nobody_watching"] = nobody_watching(loaded, environ)
    # Answered by nobody: whoever approves the plan is shown them too.
    info["unattended_questions"] = questions_mod.unattended_ids(state)
    return info


def pending_notes(info: Dict[str, Any]) -> List[str]:
    """What a pending approval adds to its `status` line, each starting with ``; ``.

    ``info`` is ``current``'s answer. Under ``auto`` with nobody watching,
    why it still asks -- the one of its conditions that failed. And the
    design questions a run nobody watched left to the architect: nobody
    answered them, so whoever approves sees them too.
    """
    notes: List[str] = []
    if info["mode"] == "auto" and info["nobody_watching"] and info["reason"]:
        notes.append("; asked because %s (design.require_approval: auto)" % info["reason"])
    left = info["unattended_questions"]
    if left:
        notes.append(
            "; also present the design questions left to the architect while nobody was watching (%s)"
            % ", ".join(left)
        )
    return notes


def refusal_lines(info: Dict[str, Any], plan_relative: str) -> List[str]:
    """What `run implementer` prints on stderr, or writes to the job, when it refuses.

    ``info`` is ``current``'s answer.
    """
    approved = str(info.get("approved_sha256") or "")[:12]
    now = str(info.get("plan_sha256") or "")[:12]
    if info.get("stale_reason") == "plan-changed":
        first = (
            "refusing to run implementer: the plan at %s changed after it was approved "
            "(approved %s, now %s). Ask again." % (plan_relative, approved, now)
        )
    elif info.get("stale_reason") == "reviewed-since":
        first = (
            "refusing to run implementer: a design review ran after the plan at %s was approved. "
            "Present its findings and ask again." % plan_relative
        )
    elif info.get("stale_reason") == "decisions-changed":
        first = (
            "refusing to run implementer: a recorded decision changed after the plan at %s was approved: "
            "revise the plan and ask again." % plan_relative
        )
    else:
        first = "refusing to run implementer: the plan at %s is not approved (%s)." % (
            plan_relative,
            info["requirement"],
        )
    left = info["unattended_questions"]
    also = (
        ", and the design questions left to the architect while nobody was watching (%s)" % ", ".join(left)
        if left
        else ""
    )
    return [
        first,
        "Present the plan to the user -- Goal, Proposed Change, Files to Modify, Risks, "
        "open design findings%s -- and ask." % also,
        "Record their yes with `dev-orchestra design approve`; never run it without one. Exit 5.",
    ]
