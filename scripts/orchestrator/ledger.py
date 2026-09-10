"""The run ledger: what is in flight, what has been spent, and whether to stop.

Three failure modes this exists to catch, none of which the pipeline could
detect before:

**A stage that stalled without anyone noticing.** Stages were only recorded on
completion, so a run that died — or is still wedged — left no trace. Stages are
now written down *before* they start, with a deadline and a pid, so a stall is
visible from outside the blocked process and survives a crash.

**A loop that never ends.** The review budget used to be advertised by a query
command and enforced nowhere, which is a guard in name only. Budgets now live
here and are consumed by the actions themselves, so exhausting one refuses the
next attempt instead of advising against it.

**A loop that ends but achieves nothing.** Repeating a stage is only progress if
something changed. A stage can register a signature — the set of findings, a
test failure summary — and identical consecutive signatures stop the loop early,
regardless of budget.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

from . import execution
from . import workspace as ws

#: Stages that consume an attempt budget. ``review`` is governed by
#: ``review.max_review_iterations`` instead, since it already counts rounds.
BUDGETED_STAGES = ("architect", "implementer", "review_fixer", "test")

DEFAULT_BUDGETS: Dict[str, Any] = {
    "architect": 3,
    "implementer": 5,
    "review_fixer": 4,
    "test": 8,
    # Backstops for loops nobody anticipated. These bound the whole workflow
    # even if the orchestrator invents a cycle this module knows nothing about.
    "total_delegated_runs": 40,
    "max_runtime_seconds": 7200,
    # A ledger older than this is treated as a finished workflow, so a new
    # request starts with a full budget without anyone having to reset it.
    "session_idle_reset_seconds": 21600,
    #: Identical consecutive signatures that count as "going nowhere".
    "max_repeats_without_progress": 2,
}

EXIT_BUDGET_EXHAUSTED = 3


class BudgetExhausted(RuntimeError):
    """Raised when a stage may not be attempted again."""


def budget_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    settings = dict(DEFAULT_BUDGETS)
    configured = config.get("budgets") or {}
    if isinstance(configured, dict):
        settings.update({key: value for key, value in configured.items() if value is not None})
    return settings


# --------------------------------------------------------------------------- ledger


class Ledger:
    """Reads and writes the ledger section of ``.ai/state.json``."""

    def __init__(self, workspace: ws.Workspace, settings: Optional[Dict[str, Any]] = None) -> None:
        self.workspace = workspace
        self.settings = settings or dict(DEFAULT_BUDGETS)

    # -- state -------------------------------------------------------------

    def _read(self) -> Dict[str, Any]:
        state = self.workspace.read_state()
        ledger = state.get("ledger")
        if not isinstance(ledger, dict):
            ledger = {}
        return ledger

    def _write(self, ledger: Dict[str, Any]) -> None:
        state = self.workspace.read_state()
        state["ledger"] = ledger
        state["updated_at"] = ws.utcnow()
        self.workspace.write_state(state)

    def _locked(self):
        """Hold the state lock across a read-modify-write.

        A detached worker writes the same state file as its parent, so without
        this one of the two edits is silently lost.
        """
        return ws.file_lock(self.workspace.state_path)

    def load(self) -> Dict[str, Any]:
        """The current ledger, starting a fresh one if the last is stale."""
        ledger = self._read()
        if not ledger:
            return self._fresh()
        idle_limit = float(self.settings.get("session_idle_reset_seconds") or 0)
        last = float(ledger.get("last_activity_monotonic") or 0)
        if idle_limit and last and time.time() - last > idle_limit:
            return self._fresh()
        return ledger

    def _fresh(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "started_at": ws.utcnow(),
            "started_monotonic": now,
            "last_activity_monotonic": now,
            "attempts": {},
            "in_flight": {},
            "signatures": {},
            "total_delegated_runs": 0,
        }

    def reset(self) -> Dict[str, Any]:
        ledger = self._fresh()
        self._write(ledger)
        return ledger

    # -- budgets -----------------------------------------------------------

    def remaining(self, stage: str) -> Optional[int]:
        """Attempts left for ``stage``, or None when it is not budgeted."""
        limit = self.settings.get(stage)
        if not isinstance(limit, int):
            return None
        used = int((self.load().get("attempts") or {}).get(stage, 0))
        return max(limit - used, 0)

    def runtime_remaining(self) -> Optional[float]:
        limit = self.settings.get("max_runtime_seconds")
        if not isinstance(limit, (int, float)) or limit <= 0:
            return None
        ledger = self.load()
        elapsed = time.time() - float(ledger.get("started_monotonic") or time.time())
        return max(float(limit) - elapsed, 0.0)

    def check(self, stage: str) -> List[str]:
        """Reasons this stage must not be attempted again. Empty means go."""
        reasons: List[str] = []
        ledger = self.load()

        remaining = self.remaining(stage)
        if remaining is not None and remaining <= 0:
            reasons.append("%s has used its budget of %s attempts" % (stage, self.settings.get(stage)))

        total_limit = self.settings.get("total_delegated_runs")
        if isinstance(total_limit, int) and total_limit > 0:
            if int(ledger.get("total_delegated_runs") or 0) >= total_limit:
                reasons.append("this workflow has used its budget of %d delegated runs" % total_limit)

        runtime_left = self.runtime_remaining()
        if runtime_left is not None and runtime_left <= 0:
            reasons.append(
                "this workflow has been running longer than its %ss budget"
                % self.settings.get("max_runtime_seconds")
            )

        repeats = int((ledger.get("signatures") or {}).get(stage, {}).get("repeats", 0))
        allowed = int(self.settings.get("max_repeats_without_progress") or 0)
        if allowed and repeats >= allowed:
            reasons.append("%s has repeated the same outcome %d times without progress" % (stage, repeats))
        return reasons

    def consume(self, stage: str, force: bool = False) -> Dict[str, Any]:
        """Record an attempt at ``stage``, refusing when a budget is spent."""
        with self._locked():
            reasons = [] if force else self.check(stage)
            if reasons:
                raise BudgetExhausted("; ".join(reasons))
            ledger = self.load()
            attempts = ledger.setdefault("attempts", {})
            attempts[stage] = int(attempts.get(stage, 0)) + 1
            ledger["total_delegated_runs"] = int(ledger.get("total_delegated_runs") or 0) + 1
            ledger["last_activity_monotonic"] = time.time()
            self._write(ledger)
            return ledger

    # -- progress ----------------------------------------------------------

    def register_signature(self, stage: str, signature: str) -> int:
        """Record a stage outcome and return how many times it has repeated.

        A repeat count of 1 means "seen once"; the loop only stops when the
        same outcome comes back, because that is the evidence that the previous
        attempt changed nothing.
        """
        with self._locked():
            ledger = self.load()
            signatures = ledger.setdefault("signatures", {})
            entry = signatures.get(stage) or {}
            if entry.get("value") == signature:
                entry["repeats"] = int(entry.get("repeats", 1)) + 1
            else:
                entry = {"value": signature, "repeats": 1}
            entry["at"] = ws.utcnow()
            signatures[stage] = entry
            ledger["last_activity_monotonic"] = time.time()
            self._write(ledger)
            return int(entry["repeats"])

    def repeats(self, stage: str) -> int:
        return int((self.load().get("signatures") or {}).get(stage, {}).get("repeats", 0))

    # -- in-flight ---------------------------------------------------------

    def begin(
        self, stage: str, detail: Optional[Dict[str, Any]] = None, deadline: Optional[float] = None
    ) -> str:
        """Write down that ``stage`` started, before it can block us."""
        with self._locked():
            return self._begin_locked(stage, detail, deadline)

    def _begin_locked(self, stage: str, detail: Optional[Dict[str, Any]], deadline: Optional[float]) -> str:
        ledger = self.load()
        # A timestamp alone collides when two stages start in the same
        # millisecond, and two entries sharing a token lose one of them.
        token = "%s-%s" % (stage, uuid.uuid4().hex[:8])
        entry = {
            "stage": stage,
            "started_at": ws.utcnow(),
            "started_monotonic": time.time(),
            "pid": os.getpid(),
            "deadline_seconds": deadline,
        }
        if detail:
            entry.update(detail)
        ledger.setdefault("in_flight", {})[token] = entry
        ledger["last_activity_monotonic"] = time.time()
        self._write(ledger)
        return token

    def end(self, token: str, status: str, detail: Optional[Dict[str, Any]] = None) -> None:
        with self._locked():
            ledger = self.load()
            entry = (ledger.get("in_flight") or {}).pop(token, None)
            ledger["last_activity_monotonic"] = time.time()
            self._write(ledger)
        event = {"status": status}
        if entry:
            event["stage"] = entry.get("stage")
            event["elapsed_seconds"] = round(time.time() - float(entry.get("started_monotonic") or 0), 2)
        if detail:
            event.update(detail)
        stage = event.pop("stage", token.rsplit("-", 1)[0])
        self.workspace.record_event(str(stage), status, event)

    def in_flight(self) -> Dict[str, Any]:
        return dict(self.load().get("in_flight") or {})

    def stalls(self) -> List[Dict[str, Any]]:
        """In-flight stages that look stuck, dead, or abandoned."""
        found: List[Dict[str, Any]] = []
        now = time.time()
        for token, entry in self.in_flight().items():
            started = float(entry.get("started_monotonic") or now)
            elapsed = now - started
            deadline = entry.get("deadline_seconds")
            pid = int(entry.get("pid") or 0)
            alive = execution.pid_alive(pid)
            reason = ""
            if not alive:
                reason = "the process that started it (pid %d) is gone" % pid
            elif deadline and elapsed > float(deadline) * 1.5:
                reason = "running %.0fs, well past its %.0fs deadline" % (elapsed, float(deadline))
            if reason:
                found.append(
                    {
                        "token": token,
                        "stage": entry.get("stage"),
                        "started_at": entry.get("started_at"),
                        "elapsed_seconds": round(elapsed, 1),
                        "deadline_seconds": deadline,
                        "pid": pid,
                        "process_alive": alive,
                        "reason": reason,
                    }
                )
        return found

    def clear_stalls(self) -> List[str]:
        """Drop in-flight entries whose process is gone, recording the outcome."""
        cleared = []
        for stall in self.stalls():
            if stall["process_alive"]:
                continue
            self.end(stall["token"], "abandoned", {"reason": stall["reason"]})
            cleared.append(str(stall["stage"]))
        return cleared

    # -- reporting ---------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        ledger = self.load()
        attempts = ledger.get("attempts") or {}
        budgets = {}
        for stage in BUDGETED_STAGES:
            limit = self.settings.get(stage)
            if isinstance(limit, int):
                budgets[stage] = {
                    "used": int(attempts.get(stage, 0)),
                    "limit": limit,
                    "remaining": self.remaining(stage),
                }
        runtime_left = self.runtime_remaining()
        return {
            "started_at": ledger.get("started_at"),
            "budgets": budgets,
            "total_delegated_runs": {
                "used": int(ledger.get("total_delegated_runs") or 0),
                "limit": self.settings.get("total_delegated_runs"),
            },
            "runtime_remaining_seconds": None if runtime_left is None else round(runtime_left),
            "signatures": {
                stage: entry.get("repeats") for stage, entry in (ledger.get("signatures") or {}).items()
            },
            "in_flight": ledger.get("in_flight") or {},
            "stalls": self.stalls(),
        }
