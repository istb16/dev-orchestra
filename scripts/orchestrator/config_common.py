"""What every part of the configuration reads: its constants, the built-in
defaults, ``ConfigError`` and the readers of a few values.

The lowest of the ``config_*`` modules; ``config`` re-exports its public names.
"""

from __future__ import annotations

import re
from typing import Any, Dict, NamedTuple, Optional, TypeGuard

from . import config_trust
from . import optimization as opt_mod
from .clocks import MAX_SECONDS, is_seconds
from .review_common import DEFAULT_EXCLUDE

CONFIG_VERSION = 1
#: Ceiling on ``workspace.stale_notice_days`` (100 years). Commands load the
#: configuration unvalidated, so an unbounded integer would reach every one.
STALE_NOTICE_MAX_DAYS = 36500

#: Bounds on ``progress.interval_seconds``: how often the orchestrator reports
#: on a long run, and so the default wait of ``jobs wait``.
PROGRESS_INTERVAL_MIN = 30
PROGRESS_INTERVAL_MAX = 3600

#: Ceiling on ``implementation.max_parallel``: the number of concurrent ledger
#: charges measured to be safe.
MAX_PARALLEL_UNITS = 8

KNOWN_ROLES = ("orchestrator", "architect", "implementer", "review_fixer")

#: Roles whose runs are plan or review, never implement. Reviewers are too.
READ_ONLY_ROLES = ("orchestrator", "architect")

#: The write roles, which ``project_write_refusals`` looks at.
WRITE_ROLES = tuple(role for role in KNOWN_ROLES if role not in READ_ONLY_ROLES)

#: The total deadline of one ``run`` of each role, in seconds. The implementer
#: gets an hour: measured implementer runs went past half an hour, and one was
#: killed at 1800. Reviewers are bounded by ``review.timeout_seconds`` instead.
RUN_TIMEOUT_DEFAULTS = {
    "orchestrator": 1800,
    "architect": 1800,
    "implementer": 3600,
    "review_fixer": 1800,
}

#: The no-output deadline of a ``run`` that may change files, unless
#: ``review.idle_timeout_seconds`` is larger. Such a run runs the tests or a
#: build, which can be silent for longer than a reviewer ever is; with no
#: deadline at all, a wedged implementer would run until its total one (#273).
WRITE_RUN_IDLE_TIMEOUT_SECONDS = 1200

BUILTIN_ROLES = (
    "general",
    "security",
    "performance",
    "test",
    "architecture",
    "database",
    "frontend",
    "backend",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def is_valid_reviewer_id(value: Any) -> TypeGuard[str]:
    """The same rule ``validate`` applies, exposed for input-time checking."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def whole_seconds(value: Any) -> bool:
    """A deadline in whole seconds: a positive int, not a bool, that a wait can hold."""
    return isinstance(value, int) and is_seconds(value)


def design_review_mode(value: Any) -> str:
    """``on``, ``off`` or ``auto`` for a ``review.design.enabled`` value.

    Null means the default, ``auto``. Anything else keeps the truth test the
    readers applied before ``auto`` existed, so a quoted ``"true"`` or a ``1``
    loaded without validation is still on and a ``0`` or ``""`` still off.
    """
    if value is None:
        return "auto"
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    return "on" if value else "off"


def ask_questions_mode(value: Any) -> str:
    """``on``, ``off`` or ``auto`` for a ``design.ask_questions`` value; null is the default, off."""
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    if value is None:
        return "off"
    return "on" if value else "off"


def progress_updates_mode(value: Any) -> str:
    """``on``, ``off`` or ``auto`` for a ``progress.updates`` value.

    Null, and anything ``validate`` would refuse, is the default, ``auto``:
    the key has no older values to keep working, and the default is what the
    orchestrator did before the key existed.
    """
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "auto"


class Progress(NamedTuple):
    """The ``progress`` block as ``progress_settings_of`` reads it."""

    #: ``on``, ``off`` or ``auto`` (``progress_updates_mode``).
    updates: str
    interval_seconds: int
    #: 0 means no stuck notice.
    quiet_notice_seconds: int


def progress_settings_of(data: Dict[str, Any]) -> Progress:
    """The ``progress`` block of ``data``, each value the default where it is absent or unusable.

    Read leniently, as ``stale_notice_days`` is: commands load unvalidated,
    and what the user is told must not hang on a value ``validate`` refuses.
    """
    defaults = default_config()["progress"]
    configured = data.get("progress")
    block = configured if isinstance(configured, dict) else {}
    interval = block.get("interval_seconds")
    if not (
        isinstance(interval, int)
        and not isinstance(interval, bool)
        and PROGRESS_INTERVAL_MIN <= interval <= PROGRESS_INTERVAL_MAX
    ):
        interval = defaults["interval_seconds"]
    quiet = block.get("quiet_notice_seconds")
    if not (isinstance(quiet, int) and not isinstance(quiet, bool) and 0 <= quiet <= MAX_SECONDS):
        quiet = defaults["quiet_notice_seconds"]
    return Progress(progress_updates_mode(block.get("updates")), interval, quiet)


#: A BCP 47 language tag, loosely: a primary subtag of two or three letters
#: and any number of further subtags. Enough to refuse a sentence or a typo
#: such as ``japanese``, without a registry to check every subtag against.
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")


def normalise_language_tag(value: Any) -> Optional[str]:
    """``value`` as a tag with its primary subtag in lower case, or None if it is not one."""
    if not isinstance(value, str) or not _LANGUAGE_TAG_RE.match(value):
        return None
    primary, _sep, rest = value.partition("-")
    return primary.lower() + ("-" + rest if rest else "")


def language_settings_of(data: Dict[str, Any]) -> Dict[str, Any]:
    """The ``language`` block of ``data``, with anything absent filled in.

    An explicit null means the default, as it does in ``design_settings``. A
    ``reply`` that is not a tag reads as unset, and only ``rewrite: false``
    turns the check off: commands and the hooks read the files unvalidated,
    and a value ``validate`` would refuse must not switch anything on.
    """
    settings = default_config()["language"]
    configured = data.get("language")
    if isinstance(configured, dict):
        settings["reply"] = normalise_language_tag(configured.get("reply"))
        settings["rewrite"] = configured.get("rewrite") is not False
    return settings


def workspace_dir_of(data: Dict[str, Any]) -> str:
    """``workspace.dir`` as written, or ``.ai`` for anything but a non-empty string.

    Commands and the hooks read the files unvalidated, and a ``dir: 5`` that
    ``validate`` refuses must not reach ``os.path`` and crash them.
    """
    workspace = data.get("workspace")
    directory = workspace.get("dir") if isinstance(workspace, dict) else None
    return directory if isinstance(directory, str) and directory.strip() else ".ai"


def trusted_project_layer(project_layer: Dict[str, Any], root: Optional[str] = None) -> Dict[str, Any]:
    """The project layer less what only the global file may set (``config_trust``).

    The one place the rules are applied: ``compose`` merges this, the
    reply-language hook reads it, and ``config_policy`` reports what it left
    out. With ``root``, the repository a command runs in, a ``workspace.dir``
    a link takes out of it goes too; ``compose`` is not given one, so
    ``workspace_dir_in`` drops that one where the workspace is resolved.
    """
    return config_trust.without_ignored(project_layer, root)


def workspace_dir_in(
    root: str, data: Dict[str, Any], global_layer: Dict[str, Any], project_layer: Dict[str, Any]
) -> str:
    """``workspace.dir`` as the commands use it in ``root``, before it is joined to it.

    ``workspace_dir_of(data)``, unless that is the project file's value and
    ``trusted_project_layer`` drops it in ``root``: then the global file's
    value, or ``.ai``. The global file's value and the default are the user's
    own and are not checked, so a ``.ai`` the user links elsewhere keeps
    working.
    """
    directory = workspace_dir_of(data)
    if (config_trust.WORKSPACE_DIR, directory) in config_trust.ignored(project_layer, root):
        return workspace_dir_of(global_layer)
    return directory


def default_config() -> Dict[str, Any]:
    """Recommended out-of-the-box configuration."""
    return {
        "version": CONFIG_VERSION,
        "orchestrator": {
            "provider": "claude",
            "model": {"family": "sonnet", "version": "latest"},
        },
        "architect": {
            "provider": "claude",
            "model": {"family": "fable", "version": "latest"},
        },
        "implementer": {
            "provider": "claude",
            "model": {"family": "opus", "version": "latest"},
        },
        "review_fixer": {
            "provider": "claude",
            "model": {"family": "opus", "version": "latest"},
        },
        "reviewers": [
            {
                "id": "claude-general",
                "provider": "claude",
                "model": {"family": "opus", "version": "latest"},
                "role": "general",
            },
            {
                "id": "codex-general",
                "provider": "codex",
                "model": {"family": "recommended-coding", "version": "latest"},
                "role": "general",
            },
            {
                "id": "claude-security",
                "provider": "claude",
                "model": {"family": "sonnet", "version": "latest"},
                "high_risk_model": {"family": "opus", "version": "latest"},
                "role": "security",
            },
            {
                "id": "claude-test",
                "provider": "claude",
                "model": {"family": "sonnet", "version": "latest"},
                "role": "test",
            },
        ],
        # The total deadline of one `run`, per role. Top-level rather than in
        # the role's block: a preset owns those blocks whole, and a deadline is
        # not what a preset chooses.
        "run": {"timeout_seconds": dict(RUN_TIMEOUT_DEFAULTS)},
        "review": {
            "max_review_iterations": 2,
            "parallel": True,
            "re_review_severities": ["critical", "high"],
            # Reviewers only: `review run` and `run <reviewer-id>`.
            "timeout_seconds": 1800,
            # A wedged agent stops producing output while a slow one keeps
            # ticking, so this catches a stall in minutes instead of half an
            # hour -- but only for providers that stream progress at all.
            # Shared with `run`; a run that may change files gets at least
            # WRITE_RUN_IDLE_TIMEOUT_SECONDS (#273).
            "idle_timeout_seconds": 300,
            # Generated and vendored files whose diff body is withheld from
            # reviewers. A list replaces this wholesale, so [] reviews
            # everything; see review.DEFAULT_EXCLUDE for why these.
            "exclude": list(DEFAULT_EXCLUDE),
            # A second round diffs against what the first round reviewed, so
            # re-review sees the fix instead of the whole change again. The
            # findings the fix was meant to address ride along with it.
            "incremental_rounds": True,
            # How many findings each reviewer is asked for. Output costs several
            # times what input does, and a reviewer's output is billed again as
            # the fixer's brief, so an uncapped reviewer costs twice over. 0
            # lifts the cap, and null lets optimization.level decide. Nothing
            # that comes back is ever dropped.
            "max_findings": None,
            # max_chars: the largest change body a review round will send at
            # all -- the diff, or the plan and the request it answers. 400,000
            # chars is roughly 100k tokens -- half a 200k window spent on the
            # change alone -- and four times the largest prompt this repository
            # has recorded (99,814 chars), so it refuses nothing anyone has
            # run. Over it, `review run` exits 3 rather than reviewing part of
            # a change and calling the round complete.
            #
            # inline_chars: how much of that body goes into the prompt itself.
            # Equal to max_chars, and deliberately so: the 120,000 this
            # replaces had no measured basis -- the prompt reaches the CLI on
            # stdin, so no argv length is involved -- and what handing the body
            # over as a file buys is a review this tool cannot verify. Equal
            # means one boundary: at or under it the round runs and is
            # complete, over it the round is refused, and a forced round is
            # partial. Setting it lower is the explicit choice of somebody who
            # will not pay for very large prompts, and costs them a `partial`
            # round for every change between the two numbers; setting it higher
            # than max_chars is allowed and means the same thing it always did.
            #
            # surrounding: "enclosing" also hands each reviewer the Python
            # function, method or class enclosing every hunk, frozen with the
            # snapshot; "none" hands over the diff alone. Off: measured on one snapshot at
            # a time (#130), it did not make a review cheaper.
            # surrounding_chars caps what that adds. Measured on one snapshot
            # (#130), 15,000 left the cost per run where it was, while 60,000
            # added what it carried -- about 29% on a 90,000-character change.
            "context": {
                "max_chars": 400_000,
                "inline_chars": 400_000,
                "surrounding": "none",
                "surrounding_chars": 15_000,
            },
            # Review the plan with the same panel before any code is written.
            # "auto" by default: a round runs for a plan that names a
            # high-risk path anywhere or 6 or more code files in Files to
            # Modify, or once a round has already run for the workflow, and
            # is skipped for the rest. Measured, the
            # design loop was 40-50% of each workflow that had one, which a
            # risky or large plan earns and a small, contained one does not.
            # `true` always reviews the plan, `false` never does. A file may
            # give the design round a panel of its own with `reviewers` here,
            # or add to the one it inherits with `reviewers_extra`; with
            # neither, a design round runs the preset's fitted design panel,
            # or the code panel with `when` ignored where a file lists one.
            "design": {"enabled": "auto", "max_iterations": 2},
        },
        # Implementation waits for the user's explicit approval of the plan.
        # On by default because the point is to catch a plan the user never
        # saw; `false` is for pipelines nobody is watching (CI, batch runs),
        # where nobody could answer the question anyway. Top-level rather
        # than under review.design: approval matters whether or not the
        # panel reviewed the plan.
        #
        # ``resume`` bounds `run architect --resume`, which continues the
        # architect's last session to revise the plan. An hour is how long the
        # CLI kept the prompt cache when this was measured; past it, a resumed
        # run pays to rebuild the context anyway. No context cap until one is
        # measured to hurt a revision.
        #
        # ``ask_questions`` puts the user's answers in front of the plan: the
        # orchestrator asks its own questions, then `run architect --questions`
        # asks the architect's, and the run that writes the plan is refused
        # until every one is answered. Off by default, so a workflow runs as it
        # did before; `auto` asks when the orchestrator finds something worth
        # asking, `true` always runs one architect round first.
        "design": {
            "require_approval": True,
            "ask_questions": False,
            "max_question_rounds": 2,
            "resume": {"max_age_seconds": 3600, "max_context_tokens": None},
        },
        # How hard to try to be cheap. See orchestrator/optimization.py: the
        # level gates a review of a tree whose tests are recorded as failing,
        # decides whether a small change gets one reviewer or the whole panel,
        # and sets the findings cap when review.max_findings is unset. A
        # change touching a high-risk path escalates to `quality` whatever is
        # configured here -- that part is not negotiable, only its patterns.
        "optimization": {
            "level": opt_mod.DEFAULT_LEVEL,
            "high_risk_paths": list(opt_mod.DEFAULT_HIGH_RISK_PATHS),
            # Added to high_risk_paths rather than replacing it, so a
            # repository can name the one path the defaults miss. Its hits
            # escalate exactly as the others do.
            "extra_high_risk_paths": [],
            "low_risk_max_files": opt_mod.DEFAULT_LOW_RISK_MAX_FILES,
            "low_risk_max_lines": opt_mod.DEFAULT_LOW_RISK_MAX_LINES,
            # A security, test or architecture reviewer sits out a round with
            # nothing for it, in both stages and at every level -- see
            # optimization.relevance_records. false runs every seat as before.
            "skip_unneeded_roles": True,
            # What the security and architecture rules look for. Replaced whole
            # by a list, or added to by the extra_ keys, as high_risk_paths is.
            "security_paths": list(opt_mod.DEFAULT_SECURITY_PATHS),
            "extra_security_paths": [],
            "architecture_paths": list(opt_mod.DEFAULT_ARCHITECTURE_PATHS),
            "extra_architecture_paths": [],
        },
        "budgets": {
            "architect": 3,
            "architect_questions": 3,
            "implementer": 5,
            "review_fixer": 4,
            "test": 8,
            "total_delegated_runs": 40,
            "max_runtime_seconds": 14400,
            "max_repeats_without_progress": 2,
        },
        # Work units of a plan's ``## Units`` block (orchestrator/units.py).
        # ``max_parallel`` is how many run at once, up to
        # MAX_PARALLEL_UNITS. ``hub_paths`` are
        # files every unit tends to touch, so with two or more units only the
        # integration unit may list them.
        "implementation": {
            "max_parallel": 2,
            "hub_paths": ["CHANGELOG*", "**/CHANGELOG*"],
        },
        "workspace": {
            "dir": ".ai",
            # The first command of a new workflow notes the others quiet for
            # this many days or more. Nothing is deleted; 0 turns it off.
            "stale_notice_days": 30,
        },
        # The language the orchestrator answers the user in, as a BCP 47 tag
        # (`ja`, `zh-TW`, `ko`, `en`). Null leaves it to SKILL.md rule 11 and
        # keeps the Claude Code hooks silent. ``rewrite: false`` keeps
        # the reminders and drops the Stop-hook check, for a check that
        # misjudges someone's replies.
        "language": {"reply": None, "rewrite": True},
        # What the orchestrator tells the user during a long run. ``updates``:
        # `auto` reports as SKILL.md "Long stages" says, `true` also detaches
        # every architect run and every `review run`, `false` keeps only the
        # stuck notice. ``interval_seconds`` is how often, and the default wait
        # of `jobs wait`. ``quiet_notice_seconds`` adds "may be stuck" to a
        # report once a run has shown no new tool line for that long; 0 = off.
        "progress": {"updates": "auto", "interval_seconds": 180, "quiet_notice_seconds": 600},
    }


class ConfigError(ValueError):
    """Raised for malformed or invalid configuration."""
