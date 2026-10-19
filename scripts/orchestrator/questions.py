"""The questions asked before the plan is written, and the user's answers.

With ``design.ask_questions`` on, the orchestrator asks the user its own
questions, then ``run architect --questions`` asks the architect's, and the run
that writes the plan is refused until every one recorded has an answer. Left
to prose, the architect settles every open point silently while writing the
plan and nothing records what it chose; this is the part that cannot be
skipped.

The record is kept under its own key in ``state.json``, written only by
``design questions record`` and ``design questions answer``, as the approval is
by ``design approve``. ``decisions.md`` is rendered from it after every write:
it is what people and the architect read, never what the gate reads.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import workspace as ws
from .config import LoadedConfig, ask_questions_mode

#: The state key the questions are kept under, and the stage of their run-log events.
STAGE = "design_questions"

#: The ledger, budget and event stage of ``run architect --questions``.
RUN_STAGE = "architect_questions"

#: The plan run refused while a question is open, or a question round refused
#: at its cap. Not 5: that one means ask the user to approve the plan, where
#: this one means ask them the recorded questions.
EXIT_QUESTIONS_PENDING = 6

DECISIONS_FILE = "decisions.md"

#: Who may have asked a question.
ASKERS = ("orchestrator", "architect")

#: What an architect round with nothing to ask says, exactly.
NO_QUESTIONS = "No questions."

OPTION_KEYS = "ABCDE"

#: What ``--delegate`` records as the answer.
DELEGATED = "left to the architect"

#: What the plan run records for a question still open when nobody is there
#: to answer it (``design.require_approval`` false).
UNATTENDED = "left to the architect: nobody was there to answer (design.require_approval is false)"

_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*question\s*$", re.IGNORECASE)
#: Any option letter is read as an option, so a sixth one is refused by name
#: instead of ending the block and hiding the fields after it.
_FIELD_RE = re.compile(
    r"^\s*[-*+]\s*\*{0,2}(topic|question|why it matters|option [a-z]|recommended)\*{0,2}\s*:\s*(.*)$",
    re.IGNORECASE,
)
#: ``B -- why``, and the ways models dress it: ``Option B``, ``**B**``.
_RECOMMENDED_RE = re.compile(
    r"^\*{0,2}(?:option\s+)?([A-E])\*{0,2}(?![A-Za-z0-9])\s*(?:--|-|:)?\s*(.*)$", re.IGNORECASE
)
#: A list item that is not a field: always reported, never read as closing prose.
_BULLET_RE = re.compile(r"^[-*+](\s|$)")
_ID_RE = re.compile(r"^Q(\d+)$")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


class QuestionError(ValueError):
    """A question that cannot be answered as asked: an unknown id or option, or a second answer."""


class RoundCapReached(Exception):
    """An architect round refused under the state lock: ``design.max_question_rounds`` is reached."""

    def __init__(self, rounds: int) -> None:
        super().__init__("round cap reached at %d architect round(s)" % rounds)
        self.rounds = rounds


class QuestionsOpen(Exception):
    """An approval refused under the state lock: a recorded question is still open."""

    def __init__(self, ids: List[str]) -> None:
        super().__init__("design question(s) still open: %s" % ", ".join(ids))
        self.ids = ids


def read(state: Dict[str, Any]) -> Dict[str, Any]:
    """``state["design_questions"]`` with anything missing or mangled read as nothing recorded."""
    data = state.get(STAGE)
    if not isinstance(data, dict):
        data = {}
    questions = [q for q in (data.get("questions") or []) if isinstance(q, dict)]
    rounds = [r for r in (data.get("rounds") or []) if isinstance(r, dict)]
    return {"questions": questions, "rounds": rounds}


def architect_rounds(data: Dict[str, Any]) -> int:
    return len([r for r in data["rounds"] if r.get("by") == "architect"])


def open_ids(data: Dict[str, Any]) -> List[str]:
    return [str(q.get("id")) for q in data["questions"] if not isinstance(q.get("answer"), dict)]


# --------------------------------------------------------------------------- parsing


def parse(text: str, by: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The question blocks in ``text``, and what is wrong with them.

    Any problem means nothing may be recorded: a half-recorded round would
    look answered with a question missing. A text that is exactly
    ``No questions.`` is no questions and no problem. What comes before the
    first ``## Question`` is not a block and is ignored, and so is a closing
    line after a block's last field (a list item never is one); an indented
    line under a field continues that field. Code fence lines are dropped first: models
    wrap their answer in one often enough that a round must not fail on it.
    """
    lines = [line for line in text.splitlines() if not _FENCE_RE.match(line)]
    if "\n".join(lines).strip() == NO_QUESTIONS:
        return [], []
    blocks: List[List[str]] = []
    for line in lines:
        if _HEADER_RE.match(line):
            blocks.append([])
        elif blocks:
            blocks[-1].append(line)
    if not blocks:
        problem = "no `## Question` block found (an architect round with nothing to ask says `%s`)"
        return [], [problem % NO_QUESTIONS]
    questions: List[Dict[str, Any]] = []
    problems: List[str] = []
    for number, block in enumerate(blocks, start=1):
        question, found = _parse_block(block, by)
        problems.extend("question block %d: %s" % (number, problem) for problem in found)
        questions.append(question)
    return (questions if not problems else []), problems


def _parse_block(lines: List[str], by: str) -> Tuple[Dict[str, Any], List[str]]:
    fields: Dict[str, str] = {}
    problems: List[str] = []
    last: Optional[str] = None
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = _FIELD_RE.match(line)
        if not match and last is not None and line[:1].isspace():
            # An indented line under a field is that field wrapped: joined, so
            # the fields after it are still read.
            if last in fields:
                fields[last] = "%s %s" % (fields[last], line.strip())
            continue
        if not match and last is not None and not _BULLET_RE.match(line):
            # Prose after the last field closes the block. Prose with a field
            # still to come is not that: ending there would drop the fields
            # after it without a word.
            if not any(_FIELD_RE.match(rest) for rest in lines[index + 1 :]):
                break
        if not match:
            problems.append("not a `- Field: value` line: %r" % line.strip()[:80])
            continue
        name, value = match.group(1).lower(), match.group(2).strip()
        last = name
        if name.startswith("option ") and name[-1].upper() not in OPTION_KEYS:
            problems.append("%s: at most five options, A to E" % name.capitalize())
            continue
        if name in fields:
            problems.append("%s is given twice" % name.capitalize())
            continue
        if not value:
            problems.append("%s is empty" % name.capitalize())
            continue
        fields[name] = value
    required = ("topic", "question", "why it matters") if by == "architect" else ("topic", "question")
    for name in required:
        if name not in fields and not any(p.startswith(name.capitalize()) for p in problems):
            problems.append("%s is missing" % name.capitalize())

    options = []
    for key in OPTION_KEYS:
        text = fields.get("option %s" % key.lower())
        if text is None:
            break
        options.append({"key": key, "text": text})
    offered = [name for name in fields if name.startswith("option ")]
    if len(offered) != len(options):
        problems.append("options must run A, B, C... without a gap")
    if by == "architect" and len(options) < 2:
        problems.append("an architect question needs at least Option A and Option B")
    elif len(options) == 1:
        problems.append("one option is no choice: give at least two, or none")

    recommended = None
    if "recommended" in fields:
        match = _RECOMMENDED_RE.match(fields["recommended"])
        key = match.group(1).upper() if match else ""
        if not match or key not in [option["key"] for option in options]:
            problems.append("Recommended must name one of the options given (as `B -- why`)")
        else:
            recommended = {"key": key, "reason": match.group(2).strip()}
    elif by == "architect":
        problems.append("Recommended is missing")

    question = {
        "topic": fields.get("topic", ""),
        "question": fields.get("question", ""),
        "why": fields.get("why it matters", ""),
        "options": options,
        "recommended": recommended,
    }
    return question, problems


# --------------------------------------------------------------------------- writes


def _next_number(questions: List[Dict[str, Any]]) -> int:
    numbers = [int(m.group(1)) for m in (_ID_RE.match(str(q.get("id") or "")) for q in questions) if m]
    return max(numbers, default=0) + 1


def record(
    workspace: ws.Workspace,
    questions: List[Dict[str, Any]],
    by: str,
    max_rounds: Any = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Add ``questions`` as asked by ``by``; return ``{"ids", "round"}``.

    An architect record is a round, whether or not it asked anything. An
    orchestrator's questions take the number of architect rounds so far, so 0
    is "about the requirements, before the architect looked". An architect
    round at ``max_rounds`` raises RoundCapReached unless ``force``: checked
    under the lock, so two records at once cannot both pass it.
    """
    now = ws.utcnow()
    with ws.file_lock(workspace.state_path):
        state = workspace.read_state()
        data = read(state)
        if by == "architect":
            rounds = architect_rounds(data)
            if not force and cap_reached({"architect_rounds": rounds, "max_rounds": max_rounds}):
                raise RoundCapReached(rounds)
            round_number = rounds + 1
            data["rounds"].append(
                {"round": round_number, "by": by, "recorded_at": now, "count": len(questions)}
            )
        else:
            round_number = architect_rounds(data)
        number = _next_number(data["questions"])
        ids: List[str] = []
        for offset, question in enumerate(questions):
            entry: Dict[str, Any] = {"id": "Q%d" % (number + offset), "round": round_number, "by": by}
            entry.update(question)
            entry.update({"asked_at": now, "answer": None, "history": []})
            data["questions"].append(entry)
            ids.append(entry["id"])
        state[STAGE] = data
        ws.append_event(state, ws.new_event(STAGE, "recorded", {"by": by, "ids": ids, "round": round_number}))
        workspace.write_state(state)
        ws.write_text(workspace.decisions_path, render(data))
    return {"ids": ids, "round": round_number}


def answer(
    workspace: ws.Workspace,
    ids: List[str],
    kind: str,
    text: str = "",
    option: str = "",
    revise: bool = False,
) -> List[Dict[str, Any]]:
    """Record the user's answer to each of ``ids``; QuestionError, with nothing written, if one cannot be.

    ``kind`` is ``option`` (``option`` names the key), ``text`` (the user's
    words as given, on one line) or ``delegated``.
    """
    if kind == "text" and ("\n" in text or "\r" in text):
        # One answer is one field of its question: a line break could forge
        # another `## Qn` heading or `- Answer:` line in decisions.md.
        raise QuestionError("--text must be one line: join the user's words with spaces")
    ids = list(dict.fromkeys(qid.strip().upper() for qid in ids))
    now = ws.utcnow()
    with ws.file_lock(workspace.state_path):
        state = workspace.read_state()
        data = read(state)
        by_id = {str(q.get("id")): q for q in data["questions"]}
        answered: List[Dict[str, Any]] = []
        revised = False
        for qid in ids:
            question = by_id.get(qid)
            if question is None:
                raise QuestionError("no question %s in this workflow" % qid)
            if question.get("answer") and not revise:
                raise QuestionError(
                    "%s is already answered; pass --revise to change the answer" % question["id"]
                )
            if kind == "option":
                offered = question.get("options") or []
                chosen = [o for o in offered if o.get("key") == option.strip().upper()]
                if not chosen:
                    keys = ", ".join(str(o.get("key")) for o in offered) or "none"
                    raise QuestionError(
                        "%s offers no option %s (offered: %s)" % (question["id"], option, keys)
                    )
                words = "%s: %s" % (chosen[0]["key"], chosen[0]["text"])
            elif kind == "delegated":
                words = DELEGATED
            else:
                words = text
            if question.get("answer"):
                revised = True
                history = question.setdefault("history", [])
                history.append(dict(question["answer"], revised_at=now))
            question["answer"] = {"kind": kind, "text": words, "answered_at": now}
            answered.append(question)
        state[STAGE] = data
        event = {"ids": [q["id"] for q in answered], "kind": kind}
        ws.append_event(state, ws.new_event(STAGE, "revised" if revised else "answered", event))
        workspace.write_state(state)
        ws.write_text(workspace.decisions_path, render(data))
    return answered


def delegate_unattended(workspace: ws.Workspace) -> List[str]:
    """Leave every open question to the architect, for a run nobody is watching; the ids.

    The questions are still asked and recorded, so what was left open is on
    the record for someone to look at later; only the wait for an answer that
    cannot come is skipped. Under the lock, as ``answer`` writes.
    """
    now = ws.utcnow()
    with ws.file_lock(workspace.state_path):
        state = workspace.read_state()
        data = read(state)
        ids: List[str] = []
        for question in data["questions"]:
            if isinstance(question.get("answer"), dict):
                continue
            question["answer"] = {"kind": "delegated", "text": UNATTENDED, "answered_at": now}
            ids.append(str(question.get("id")))
        if not ids:
            return ids
        state[STAGE] = data
        ws.append_event(state, ws.new_event(STAGE, "delegated", {"ids": ids, "kind": "unattended"}))
        workspace.write_state(state)
        ws.write_text(workspace.decisions_path, render(data))
    return ids


# --------------------------------------------------------------------------- reads


def decisions_text(state: Dict[str, Any]) -> str:
    """The questions and answers as reviewers are given them; "" when none were asked."""
    data = read(state)
    if not data["questions"]:
        return ""
    return _body(data)


def decisions_digest(state: Dict[str, Any]) -> Optional[str]:
    """sha256 of every answer as given, or None when no question was asked.

    The answers alone, not the wording around them: an approval is of what
    the user decided, and re-rendering the file must not make it stale.
    """
    questions = read(state)["questions"]
    if not questions:
        return None
    answers = []
    for question in questions:
        given = question.get("answer") if isinstance(question.get("answer"), dict) else {}
        answers.append([question.get("id"), given.get("kind"), given.get("text")])
    return hashlib.sha256(json.dumps(answers, ensure_ascii=False).encode("utf-8")).hexdigest()


def render(data: Dict[str, Any]) -> str:
    """``decisions.md`` as a whole."""
    head = [
        "# Decisions",
        "",
        "Rewritten by `dev-orchestra design questions` on every change; do not edit.",
        "",
    ]
    if not data["questions"]:
        rounds = architect_rounds(data)
        head.append("No questions were asked (%d architect round(s) recorded)." % rounds)
        return "\n".join(head) + "\n"
    return "\n".join(head) + _body(data)


def _body(data: Dict[str, Any]) -> str:
    lines: List[str] = []
    for question in data["questions"]:
        asked = "asked by the %s" % question.get("by")
        if question.get("round"):
            asked += ", round %s" % question.get("round")
        else:
            asked += ", before the architect"
        lines.append("## %s: %s" % (question.get("id"), question.get("topic") or "(no topic)"))
        lines.append("")
        lines.append("- Asked: %s" % asked)
        lines.append("- Question: %s" % question.get("question"))
        if question.get("why"):
            lines.append("- Why it matters: %s" % question["why"])
        for option in question.get("options") or []:
            lines.append("- Option %s: %s" % (option.get("key"), option.get("text")))
        recommended = question.get("recommended")
        if isinstance(recommended, dict):
            reason = " -- %s" % recommended["reason"] if recommended.get("reason") else ""
            lines.append("- Recommended: %s%s" % (recommended.get("key"), reason))
        given = question.get("answer")
        if isinstance(given, dict):
            lines.append("- Answer: %s" % _one_line(given.get("text")))
        else:
            lines.append("- Answer: (open -- not answered yet)")
        if question.get("history"):
            earlier = "; ".join(_one_line(h.get("text")) for h in question["history"])
            lines.append("- Earlier answers: %s" % earlier)
        lines.append("")
    return "\n".join(lines)


def _one_line(value: Any) -> str:
    """An answer as one field: a line break in a hand-edited state cannot start a heading."""
    return " ".join(str(value).split())


def prompt_carries_decisions(text: str) -> bool:
    """Whether a plan request points the architect at the answers, as the template does."""
    return DECISIONS_FILE in text or "## Decisions" in text


def asking(loaded: LoadedConfig) -> bool:
    """Whether ``design.ask_questions`` is on or ``auto``: the records count only then."""
    return ask_questions_mode(loaded.design_settings().get("ask_questions")) != "off"


def current_for(loaded: LoadedConfig, workspace: ws.Workspace) -> Dict[str, Any]:
    """``current`` under the settings in ``loaded``: the one place they are read for it."""
    settings = loaded.design_settings()
    return current(workspace, settings.get("ask_questions"), settings.get("max_question_rounds"))


def current(workspace: ws.Workspace, setting: Any, max_rounds: Any) -> Dict[str, Any]:
    """The question state `status` prints and the plan run gates on.

    Read from the setting as it is now, so turning it off stops the gate at
    once and turning it back on gates the records already there.
    """
    name = ask_questions_mode(setting)
    data = read(workspace.read_state())
    questions = data["questions"]
    still_open = open_ids(data)
    rounds = architect_rounds(data)
    if name == "off":
        state = "off"
    elif still_open:
        state = "open"
    elif name == "on" and rounds == 0:
        state = "round-required"
    elif not questions and rounds == 0:
        state = "none"
    else:
        state = "answered"
    recorded = bool(questions or data["rounds"])
    return {
        "mode": name,
        "state": state,
        "pending": state == "open",
        "open": still_open,
        "answered": len(questions) - len(still_open),
        "total": len(questions),
        "architect_rounds": rounds,
        "max_rounds": max_rounds,
        "decisions_file": workspace.relative(workspace.decisions_path) if recorded else None,
    }


def as_if_answered(info: Dict[str, Any]) -> Dict[str, Any]:
    """``info`` with every open question answered.

    What an unattended plan run is judged on before it leaves the open ones
    to the architect: the answers are written only once nothing else refuses
    the run, so a refused one leaves them open.
    """
    if info["state"] != "open":
        return info
    round_required = info["mode"] == "on" and info["architect_rounds"] == 0
    return dict(
        info,
        state="round-required" if round_required else "answered",
        pending=False,
        open=[],
        answered=info["total"],
    )


def refused(info: Dict[str, Any], resume: bool) -> bool:
    """Whether the run that writes the plan is refused; a ``--resume`` revision needs no round."""
    return info["state"] == "open" or (info["state"] == "round-required" and not resume)


def refusal_lines(info: Dict[str, Any]) -> List[str]:
    """What the refused plan run prints on stderr, or writes to the job."""
    if info["state"] == "open":
        first = "refusing to run architect: %d design question(s) still open (%s) (design.ask_questions)." % (
            len(info["open"]),
            ", ".join(info["open"]),
        )
        how = (
            "Ask the user; record their words with `dev-orchestra design questions answer <ID> "
            '--option <key>` or `--text "..."`, or `--delegate` when they leave it to the architect.'
        )
    else:
        first = (
            "refusing to run architect: design.ask_questions is true and no architect question "
            "round is recorded."
        )
        how = (
            "Run `run architect --questions`, record its answer with `design questions record --by "
            "architect --file <path>`, and ask the user what it asks."
        )
    return [first, how, "Never answer for the user. Exit 6."]


def implementer_refusal_lines(info: Dict[str, Any]) -> List[str]:
    """What a refused `run implementer` prints while a recorded question is still open."""
    return _open_question_lines("refusing to run implementer", info["open"])


def approval_refusal_lines(ids: List[str]) -> List[str]:
    """What `design approve` prints when the questions ``ids`` are still open."""
    return _open_question_lines("not recording an approval", ids)


def _open_question_lines(what: str, ids: List[str]) -> List[str]:
    return [
        "%s: %d design question(s) still open (%s) (design.ask_questions)."
        % (what, len(ids), ", ".join(ids)),
        "Ask the user and record their answers with `dev-orchestra design questions answer`; "
        "revise the plan if an answer changes it, then ask for approval again.",
        "Never answer for the user. Exit 6.",
    ]


def cap_refusal_lines(info: Dict[str, Any], what: str = "run architect --questions") -> List[str]:
    """What a question round past ``design.max_question_rounds`` is refused with."""
    return [
        "refusing to %s: round cap reached (%s of %s, design.max_question_rounds): "
        "present the open points together and ask how to proceed."
        % (what, info["architect_rounds"], info["max_rounds"]),
        "Pass --force for one more round only when the user asks for it. Exit 6.",
    ]


def cap_reached(info: Dict[str, Any]) -> bool:
    limit = info.get("max_rounds")
    return ws.is_int(limit) and info["architect_rounds"] >= limit
