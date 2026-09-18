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
next attempt instead of advising against it. The runtime budget is charged from
what ``execute()`` measured for each delegated run, so a run nobody measured --
one whose wrapper died before ``end()`` -- costs nothing, and time spent not
delegating costs nothing either.

**A loop that ends but achieves nothing.** Repeating a stage is only progress if
something changed. A stage can register a signature — the set of findings, a
test failure summary — and identical consecutive signatures stop the loop early,
regardless of budget.

It also keeps the token account. That is bookkeeping, not a budget: nothing
here refuses a run because of what it would cost. The counts come from the
delegated CLIs, and not all of them report any, so every total is paired with
how many runs it actually covers -- an unqualified sum over partial data reads
as authoritative when it is only a floor.
"""

from __future__ import annotations

import copy
import math
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
    # Backstops for delegating loops nobody anticipated: only runs that go
    # through ``consume()`` count against the first, every run that reaches
    # ``end()`` with a measurement against the second. A cycle that delegates
    # nothing is bounded by neither.
    #
    # 14400 is the heaviest workflow actually observed (5619s of delegated
    # execution) plus one round at its deadline ceiling -- a two-reviewer round
    # and a fixer is 5400s on its own -- not an average. 7200 did not fit that.
    "total_delegated_runs": 40,
    "max_runtime_seconds": 14400,
    # A ledger older than this is treated as a finished workflow, so a new
    # request starts with a full budget without anyone having to reset it.
    "session_idle_reset_seconds": 21600,
    #: Identical consecutive signatures that count as "going nowhere".
    "max_repeats_without_progress": 2,
}

EXIT_BUDGET_EXHAUSTED = 3

#: Countable fields on a usage report. Money is kept apart because it is a
#: float, and because a CLI can price a run without breaking down its tokens.
USAGE_COUNTS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "billed_tokens",
    "prompt_chars",
)


def _blank_account() -> Dict[str, Any]:
    # ``priced_runs`` apart from ``measured_runs``: a CLI can report its tokens
    # and no money, which Codex does on every run. Counting the two together
    # made a cost total that omits one provider entirely look complete.
    account: Dict[str, Any] = {"runs": 0, "measured_runs": 0, "priced_runs": 0, "cost_usd": 0.0}
    account.update(dict.fromkeys(USAGE_COUNTS, 0))
    return account


def _accumulate(account: Dict[str, Any], usage: Dict[str, Any]) -> Dict[str, Any]:
    """Fold one run's usage into a running account.

    A field the CLI did not report contributes nothing rather than zero, so a
    silent CLI cannot make a stage look free.
    """
    account["runs"] = int(account.get("runs") or 0) + 1
    if usage.get("measured"):
        account["measured_runs"] = int(account.get("measured_runs") or 0) + 1
    for field in USAGE_COUNTS:
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        account[field] = int(account.get(field) or 0) + value
    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        account["cost_usd"] = round(float(account.get("cost_usd") or 0.0) + float(cost), 6)
        if float(cost) > 0:
            account["priced_runs"] = int(account.get("priced_runs") or 0) + 1
    return account


def _merge_accounts(accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = _blank_account()
    # An account written before ``priced_runs`` existed cannot answer "how many
    # of these runs reported money", and reading its absence as zero made every
    # pre-0.4.2 account claim its costs were unreported. Absent is unknown.
    total["priced_known"] = all("priced_runs" in account for account in accounts)
    for account in accounts:
        total["runs"] += int(account.get("runs") or 0)
        total["measured_runs"] += int(account.get("measured_runs") or 0)
        total["priced_runs"] += int(account.get("priced_runs") or 0)
        for field in USAGE_COUNTS:
            total[field] += int(account.get(field) or 0)
        total["cost_usd"] = round(total["cost_usd"] + float(account.get("cost_usd") or 0.0), 6)
    return total


def _carried_account(previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The token account a reset hands on to the ledger that replaces it.

    A reset resets the budgets; it does not reset the account. The account
    answers what this *workflow* has cost and refuses nothing, so there was
    never a reason to clear it. It was cleared anyway, and since every writer
    goes through ``load`` the first write after a six-hour idle gap erased the
    account of a workflow still running: issue #33 lost the architect's
    462,124 and the implementer's 351,701, recoverable only because the event
    log in `.ai/state.json` happened to survive.

    Deep-copied rather than shared: the result is written to disk and mutated
    by ``record_usage``, while the ledger it came from is still in the hands
    of whoever passed it in.

    A ledger written before the account existed -- or one whose ``tokens`` is
    ``None`` or not a dict -- resets to a blank account rather than raising, and
    so does a single mangled section, or a single mangled entry within one, of
    an otherwise usable account.
    """
    tokens = (previous or {}).get("tokens")
    if not isinstance(tokens, dict):
        return {"by_stage": {}, "by_label": {}}
    carried = copy.deepcopy(tokens)
    for section in ("by_stage", "by_label"):
        # Replaced, not filled in behind a missing key: a section that is
        # present and mangled -- ``{"tokens": {"by_stage": "oops"}}`` -- used
        # to be healed by the reset blanking the account, and is now carried
        # into every ledger that follows. ``record_usage`` promises never to
        # raise on a malformed report; left as found, the next one does.
        entries = carried.get(section)
        if not isinstance(entries, dict):
            carried[section] = {}
            continue
        # The same trap one level down: ``{"by_stage": {"architect": "oops"}}``
        # clears the section check, and ``_accumulate`` calls ``.get`` on the
        # entry. Dropped rather than blanked, so the stages beside it keep
        # their numbers.
        carried[section] = {key: value for key, value in entries.items() if isinstance(value, dict)}
    return carried


def _epoch(ledger: Dict[str, Any]) -> str:
    """The budget epoch of a ledger, whichever key it was written with."""
    return str(ledger.get("epoch") or ledger.get("workflow") or "")


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
            return self._fresh(ledger)
        return ledger

    def workflow_id(self) -> str:
        """The current budget epoch: a value that changes when the budgets do.

        Named ``epoch`` on disk rather than ``workflow``, which it was called
        until 0.4.2. A workflow is now a directory under `.ai/workflows/`, and
        two identifiers sharing one word cost a real analysis an hour: a
        ledger whose ``workflow`` did not match the directory holding it read
        as a ledger carried between directories, when it was only the other
        namespace. A ledger written before the rename is read as it stands.

        Which is what ``budget reset`` and the idle reset both do. Anything
        counting *per workflow* has to be keyed on this, or the reset says it
        happened without having happened.

        An id rather than the start time: ``utcnow`` has second granularity,
        so resetting within a second of the last reset produced the same
        string and the reset silently did nothing. A ledger written before
        this field existed falls back to the timestamp, which is what it has.

        Settled on disk here when it has not been already. ``load`` mints a
        fresh ledger for a workflow that has not started, or one that has gone
        stale, and does not write it -- so reading the identity without
        recording it returned a different answer every time, and a counter
        keyed on it reset continuously. That turns a budget into no budget,
        which is worse than the bug this field exists to fix.
        """
        with self._locked():
            stored = self._read() or {}
            ledger = self.load()
            if _epoch(ledger) != _epoch(stored):
                self._write(ledger)
            return _epoch(ledger) or str(ledger.get("started_at") or "")

    def _fresh(self, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Fresh budgets, carrying ``previous``'s token account across.

        Everything minted here *is* the budget. The account is not, which is
        why it is the one thing taken from the ledger being replaced. No
        ledger to replace -- the first request of a workflow -- carries
        nothing.
        """
        now = time.time()
        return {
            # Identity, not a timestamp: two epochs can start in the same
            # second, and anything keyed on "are the budgets still the ones I
            # was counting against" has to tell them apart.
            "epoch": uuid.uuid4().hex[:12],
            "started_at": ws.utcnow(),
            # When this epoch began. Kept because it is part of the published
            # format -- and used in no budget calculation: the runtime budget
            # measures delegated work, not how long the ledger has existed,
            # which is the whole of issue #40.
            "started_monotonic": now,
            "last_activity_monotonic": now,
            "attempts": {},
            "in_flight": {},
            "signatures": {},
            "total_delegated_runs": 0,
            # Measured seconds charged to *this* epoch. A budget, not an
            # account: a reset starts it at zero and carries nothing, because
            # what the workflow has cost in total is already answered by the
            # ``duration_seconds`` on the events, which outlive every reset.
            "runtime_seconds": 0.0,
            "tokens": _carried_account(previous),
        }

    def reset(self) -> Dict[str, Any]:
        # Locked, like every other mutating method here, now that this is a
        # read-modify-write rather than a blind overwrite: the account it
        # carries across is read here and written back below, so a detached
        # worker's ``record_usage`` landing in between is erased -- which is
        # exactly the loss carrying the account exists to prevent.
        with self._locked():
            # From what is on disk, not through ``load``: a human resetting the
            # budgets to keep working has not asked to be told the work so far
            # was free.
            ledger = self._fresh(self._read())
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

    def runtime_used(self) -> float:
        """Seconds of delegated execution charged to the current budgets.

        Only what has been measured and closed. A run still in flight
        contributes nothing: projecting ``now - started`` onto it would be a
        wall clock again -- the measure this budget exists to stop using --
        and it would make ``budget show``, which does not clear stalls, climb
        past a ``status`` that had already buried the same dead entry.
        """
        return max(float(self.load().get("runtime_seconds") or 0.0), 0.0)

    def runtime_remaining(self) -> Optional[float]:
        limit = self.settings.get("max_runtime_seconds")
        if not isinstance(limit, (int, float)) or limit <= 0:
            return None
        return max(float(limit) - self.runtime_used(), 0.0)

    def runtime_refusal(self) -> Optional[str]:
        """Why delegating again is refused, or None while there is budget left."""
        left = self.runtime_remaining()
        if left is None or left > 0:
            return None
        return (
            "this workflow's delegated runs have used their runtime budget of %ss "
            "(%.0fs of measured execution)" % (self.settings.get("max_runtime_seconds"), self.runtime_used())
        )

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

        spent = self.runtime_refusal()
        if spent:
            reasons.append(spent)

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

    # -- token accounting --------------------------------------------------

    def record_usage(self, stage: str, usage: Dict[str, Any], label: str = "") -> Dict[str, Any]:
        """Add one delegated run's cost to the account.

        Never refuses, and never raises on a malformed report: the run has
        already been paid for, and this is the only record that it happened.
        """
        if not isinstance(usage, dict):
            usage = {}
        with self._locked():
            ledger = self.load()
            tokens = ledger.setdefault("tokens", {})
            by_stage = tokens.setdefault("by_stage", {})
            by_stage[stage] = _accumulate(by_stage.get(stage) or _blank_account(), usage)
            if label:
                by_label = tokens.setdefault("by_label", {})
                by_label[label] = _accumulate(by_label.get(label) or _blank_account(), usage)
            ledger["last_activity_monotonic"] = time.time()
            self._write(ledger)
            return tokens

    def token_report(self) -> Dict[str, Any]:
        """The account, plus how much of it is actually measured.

        Read from the ledger *on disk*, not through ``load``. ``load`` hands
        back a blank ledger once one has been idle past
        ``session_idle_reset_seconds``, which is right for a budget -- the next
        request should start with a full one -- and wrong for the account,
        which refuses nothing. It only hid yesterday's numbers: reported from
        real use as "tokens show says no runs while state.json holds 446,430".
        """
        stored = self._read() or {}
        tokens = (stored.get("tokens") if stored else None) or {}
        by_stage = {
            stage: account
            for stage, account in (tokens.get("by_stage") or {}).items()
            if isinstance(account, dict)
        }
        by_label = {
            label: account
            for label, account in (tokens.get("by_label") or {}).items()
            if isinstance(account, dict)
        }
        totals = _merge_accounts(list(by_stage.values()))
        return {
            "by_stage": by_stage,
            "by_label": by_label,
            "totals": totals,
            # False means the sums are a floor: at least one CLI ran without
            # reporting what it spent.
            "complete": totals["runs"] > 0 and totals["runs"] == totals["measured_runs"],
            # And this means the *money* is a floor, which is a separate thing:
            # a CLI can report its tokens and no cost, as Codex does on every
            # run, so a complete token total can sit beside a cost that omits
            # one provider entirely. An account too old to say counts as
            # priced: claiming a caveat we cannot support is the worse error.
            "priced": not totals.get("priced_known", True)
            or totals["runs"] == 0
            or totals["runs"] == totals["priced_runs"],
        }

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
            # Which budgets this run may be charged to. A worker that finishes
            # after another process reset the ledger would otherwise bill the
            # fresh budget for work the old one authorised.
            "epoch": _epoch(ledger),
        }
        if detail:
            entry.update(detail)
        ledger.setdefault("in_flight", {})[token] = entry
        ledger["last_activity_monotonic"] = time.time()
        self._write(ledger)
        return token

    def end(
        self,
        token: str,
        status: str,
        detail: Optional[Dict[str, Any]] = None,
        charged_seconds: float = 0.0,
    ) -> None:
        """Close an in-flight stage and charge what its run was measured to take.

        ``charged_seconds`` is the caller's own measurement -- ``result.duration``
        for a run, the sum over the panel for a review batch -- and defaults to
        zero because a caller that has no measurement must not invent one.

        The charge lands only when the pop returned an entry *and* that entry
        belongs to the epoch the ledger is on now. Both halves matter:

        * No entry means either a reset threw it away while the run was still
          going, or this is the second ``end()`` for the same token. Either way
          there is no claim on these budgets, so the run is recorded and not
          billed. That is what stops a worker outliving a ``budget reset`` from
          spending a budget minted after it started, and what stops a double
          ``end()`` billing twice.
        * A stamp from another epoch means the entry survived a writer that did
          not go through ``_fresh``. There is no such writer today; the stamp
          is here so that adding one cannot quietly reintroduce the first case.

        An entry written before the stamp existed is read as belonging to the
        current epoch: it is in *this* ledger, and every reset path goes through
        ``_fresh``, which drops in-flight entries -- so its presence is itself
        the evidence that no reset has happened since it was written.

        The event always says what was charged, including when that is zero and
        why, so a run that happened but could not be billed leaves a record of
        both facts rather than looking free.
        """
        with self._locked():
            ledger = self.load()
            entry = (ledger.get("in_flight") or {}).pop(token, None)
            current = _epoch(ledger)
            charged, skipped = 0.0, ""
            if entry is None:
                skipped = "no in-flight entry for this token"
            elif entry.get("epoch", current) != current:
                skipped = "entry began in epoch %s; the ledger is epoch %s" % (
                    entry.get("epoch"),
                    current,
                )
            else:
                charged = max(float(charged_seconds or 0.0), 0.0)
                ledger["runtime_seconds"] = float(ledger.get("runtime_seconds") or 0.0) + charged
            ledger["last_activity_monotonic"] = time.time()
            self._write(ledger)
        event = {"status": status, "charged_seconds": round(charged, 2)}
        if skipped:
            event["charge_skipped"] = skipped
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
        runtime_used = self.runtime_used()
        # Rounded up, never to nearest: a reported 0 has to mean what
        # ``runtime_refusal`` means, or a caller keying off it stops over half a
        # second a ``run`` would still spend from.
        runtime_left = None if runtime_left is None else math.ceil(runtime_left)
        return {
            "started_at": ledger.get("started_at"),
            "budgets": budgets,
            "total_delegated_runs": {
                "used": int(ledger.get("total_delegated_runs") or 0),
                "limit": self.settings.get("total_delegated_runs"),
            },
            # Kept alongside the block below, not replaced by it: callers and
            # saved payloads already read this key.
            "runtime_remaining_seconds": runtime_left,
            "runtime": {
                "used": round(runtime_used, 2),
                "limit": self.settings.get("max_runtime_seconds"),
                "remaining": runtime_left,
            },
            "signatures": {
                stage: entry.get("repeats") for stage, entry in (ledger.get("signatures") or {}).items()
            },
            "in_flight": ledger.get("in_flight") or {},
            "stalls": self.stalls(),
            "tokens": self.token_report(),
        }
