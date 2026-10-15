"""The `design questions` commands: what was asked before the plan, and what the user answered."""

from __future__ import annotations

import argparse
import os
from typing import Optional

from . import questions as questions_mod
from . import workspace as ws
from .cli_common import _emit_json, _err, _in_workflow, _load_lenient, _out, _read_stdin, _workspace


def _read_blocks(path: str, workspace: ws.Workspace) -> Optional[str]:
    """The text of ``--file``; None, with the reason on stderr, when there is none to read."""
    if path == "-":
        return _read_stdin()
    resolved = _in_workflow(workspace, path)
    if not os.path.isfile(resolved):
        _err("question file does not exist: %s" % resolved)
        return None
    try:
        with open(resolved, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        _err("question file cannot be read: %s (%s)" % (resolved, exc))
        return None


def cmd_design_questions_record(args: argparse.Namespace) -> int:
    """Record question blocks as asked by the orchestrator or the architect, all or none."""
    loaded = _load_lenient(args.cwd)
    workspace = _workspace(args)
    text = _read_blocks(args.file, workspace)
    if text is None:
        return 2
    questions, problems = questions_mod.parse(text, args.by)
    if problems:
        _err("nothing recorded: %s" % args.file)
        for problem in problems:
            _err("  - %s" % problem)
        _err("Fix the format and keep the content, or run the round again.")
        return 2
    info = questions_mod.current_for(loaded, workspace)
    if args.by == "orchestrator" and not questions:
        _out("no questions to record")
        return 0
    try:
        result = questions_mod.record(
            workspace, questions, args.by, max_rounds=info["max_rounds"], force=bool(args.force)
        )
    except questions_mod.RoundCapReached as exc:
        # Checked again under the state lock: another record may have landed since.
        info = dict(info, architect_rounds=exc.rounds)
        for line in questions_mod.cap_refusal_lines(info, "record another architect round"):
            _err(line)
        return questions_mod.EXIT_QUESTIONS_PENDING
    decisions = workspace.relative(workspace.decisions_path)
    if result["ids"]:
        _out("recorded %s (asked by the %s) in %s" % (", ".join(result["ids"]), args.by, decisions))
    else:
        _out("recorded architect round %d with no questions in %s" % (result["round"], decisions))
    if info["mode"] == "off":
        _err("note: design.ask_questions is false; recorded, the gate is off")
    return 0


def cmd_design_questions_answer(args: argparse.Namespace) -> int:
    """Record the user's answer: their option, their words, or their "leave it to you"."""
    given = [args.option is not None, args.text is not None, bool(args.delegate)]
    if given.count(True) != 1:
        _err("give exactly one of --option, --text or --delegate")
        return 2
    if len(args.ids) > 1 and not args.delegate:
        _err("several ids take --delegate only: answer the others one at a time")
        return 2
    if args.text is not None and not args.text.strip():
        _err("--text is empty: record the user's own words")
        return 2
    loaded = _load_lenient(args.cwd)
    workspace = _workspace(args)
    kind = "option" if args.option is not None else ("text" if args.text is not None else "delegated")
    try:
        answered = questions_mod.answer(
            workspace, args.ids, kind, text=args.text or "", option=args.option or "", revise=args.revise
        )
    except questions_mod.QuestionError as exc:
        _err(str(exc))
        return 2
    for question in answered:
        _out("%s: %s" % (question["id"], question["answer"]["text"]))
    info = questions_mod.current_for(loaded, workspace)
    if info["open"]:
        _out("still open: %s" % ", ".join(info["open"]))
    return 0


def cmd_design_questions_show(args: argparse.Namespace) -> int:
    """The recorded questions and answers, as text or as one JSON object."""
    loaded = _load_lenient(args.cwd)
    workspace = _workspace(args)
    info = questions_mod.current_for(loaded, workspace)
    data = questions_mod.read(workspace.read_state())
    questions = data["questions"]
    if args.open:
        questions = [q for q in questions if not isinstance(q.get("answer"), dict)]
    if args.json:
        _emit_json(
            {
                "mode": info["mode"],
                "state": info["state"],
                "pending": info["pending"],
                "questions": questions,
                "rounds": data["rounds"],
                "decisions_file": info["decisions_file"],
                "workflow": workspace.workflow,
            }
        )
        return 0
    _out(
        "Design questions: %s (design.ask_questions: %s), %d architect round(s) of %s"
        % (info["state"], info["mode"], info["architect_rounds"], info["max_rounds"])
    )
    if not questions:
        _out("  no open questions" if args.open else "  none recorded")
        return 0
    for question in questions:
        given = question.get("answer")
        status = "answered" if isinstance(given, dict) else "open"
        _out("")
        _out(
            "%s [%s] %s (asked by the %s)"
            % (question.get("id"), status, question.get("topic"), question.get("by"))
        )
        _out("  %s" % question.get("question"))
        for option in question.get("options") or []:
            _out("  %s: %s" % (option.get("key"), option.get("text")))
        recommended = question.get("recommended")
        if isinstance(recommended, dict):
            _out("  recommended: %s" % recommended.get("key"))
        if isinstance(given, dict):
            _out("  answer: %s" % given.get("text"))
    return 0
