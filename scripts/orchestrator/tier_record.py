"""What a run's ``tier_choice`` record says, and how its end event is read.

The vocabulary the run log is written in, apart from ``auto_tier`` (which
reads the plan and the report to decide) so that ``status`` and
``optimization report`` can read the records without loading the plan and
units parsing they never use. Imports nothing from the package.
"""

from __future__ import annotations

from typing import Any, Dict, NamedTuple, Optional, Sequence

#: The tier the CLI may choose. Any other tier is only ever chosen by ``--tier``.
LIGHT = "light"
#: The roles the CLI chooses a tier for.
ROLES = ("implementer", "review_fixer")

SOURCE_AUTO = "auto"
SOURCE_EXPLICIT = "explicit"
SOURCE_OFF = "off"

#: Why the CLI chose what it chose, as a record counts it; ``reason`` is the
#: sentence a person reads. ``light``: the work was small and not high-risk.
CAUSE_LIGHT = "light"
#: The role's own model because the last light run of the stage failed.
CAUSE_RETRY = "retry"
#: The role's own model because the work was too large.
CAUSE_SIZE = "size"
#: The role's own model because the work touches a high-risk path or severity.
CAUSE_RISK = "risk"
#: The role's own model because the plan or the findings could not be judged.
CAUSE_DOUBT = "doubt"
#: ``--tier`` or ``--no-auto-tier``: the caller chose.
CAUSE_CALLER = "caller"
CAUSES = (CAUSE_LIGHT, CAUSE_RETRY, CAUSE_SIZE, CAUSE_RISK, CAUSE_DOUBT, CAUSE_CALLER)

RETRY_REASON = "retrying on the role's own model after a failed light run"


class TierChoice(NamedTuple):
    """The tier a run uses, who chose it, why, and the kind of why."""

    #: The tier name, or None for the role's own model.
    tier: Optional[str]
    #: ``auto`` (the CLI), ``explicit`` (``--tier``) or ``off`` (``--no-auto-tier``).
    source: str
    #: Display text only; nothing counts on it.
    reason: str
    #: One of ``CAUSES``: what a report counts on. No default, so every
    #: choice names its own.
    cause: str

    def to_dict(self) -> Dict[str, Any]:
        """The choice as a run event records it."""
        return {"tier": self.tier, "source": self.source, "reason": self.reason, "cause": self.cause}


def is_run_end(event: Any) -> bool:
    """Whether a run log event is a run's end event: the one with a ``tier`` key.

    Every run writes it, a run that failed before the provider answered
    included (``cli_run._tier_detail``).
    """
    return isinstance(event, dict) and "tier" in event


def light_failed(event: Dict[str, Any]) -> bool:
    """Whether a run's end event is a failed ``light`` run.

    Failed: it did not end ``ok``, or it ended ``ok`` without answering.
    """
    if event.get("tier") != LIGHT:
        return False
    return event.get("status") != "ok" or event.get("answered") is False


def last_light_failure(
    events: Sequence[Any], stage: str, unit: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The most recent run of ``stage`` (and ``unit``, when given), if it ran on ``light`` and failed.

    A run is read by its end event (``is_run_end``). None when the most
    recent run of the stage was not on ``light`` or went well.
    """
    for event in reversed(list(events)):
        if not is_run_end(event) or event.get("stage") != stage:
            continue
        if unit and event.get("unit") != unit:
            continue
        return event if light_failed(event) else None
    return None
