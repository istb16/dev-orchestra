"""Project workspace helpers: repo root discovery, ``.ai/`` layout, run state."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(args: Sequence[str], cwd: str, timeout: int = 60) -> "tuple[int, str, str]":
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def is_git_repo(path: str) -> bool:
    code, out, _ = git(["rev-parse", "--is-inside-work-tree"], path)
    return code == 0 and out.strip() == "true"


def repo_root(start: Optional[str] = None) -> str:
    start = os.path.abspath(start or os.getcwd())
    code, out, _ = git(["rev-parse", "--show-toplevel"], start)
    if code == 0 and out.strip():
        return os.path.abspath(out.strip())
    return start


class Workspace:
    """The ``.ai/`` directory for one project."""

    def __init__(self, root: str, directory: Optional[str] = None) -> None:
        self.root = os.path.abspath(root)
        self.dir = os.path.abspath(directory or os.path.join(self.root, ".ai"))

    # -- paths -------------------------------------------------------------

    @property
    def plan_path(self) -> str:
        return os.path.join(self.dir, "plan.md")

    @property
    def execution_dir(self) -> str:
        return os.path.join(self.dir, "execution")

    @property
    def reviews_dir(self) -> str:
        return os.path.join(self.dir, "reviews")

    @property
    def snapshot_path(self) -> str:
        return os.path.join(self.reviews_dir, "review-target.diff")

    @property
    def snapshot_meta_path(self) -> str:
        return os.path.join(self.reviews_dir, "review-target.json")

    @property
    def consolidated_md_path(self) -> str:
        return os.path.join(self.reviews_dir, "consolidated.md")

    @property
    def consolidated_json_path(self) -> str:
        return os.path.join(self.reviews_dir, "consolidated.json")

    @property
    def state_path(self) -> str:
        return os.path.join(self.dir, "state.json")

    def reviewer_report_path(self, reviewer_id: str) -> str:
        return os.path.join(self.reviews_dir, "%s.md" % reviewer_id)

    def relative(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.root).replace(os.sep, "/")
        except ValueError:  # pragma: no cover - different drives on Windows
            return path

    # -- lifecycle ---------------------------------------------------------

    def ensure(self) -> "Workspace":
        for directory in (self.dir, self.execution_dir, self.reviews_dir):
            os.makedirs(directory, exist_ok=True)
        gitignore = os.path.join(self.dir, ".gitignore")
        if not os.path.exists(gitignore):
            with open(gitignore, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    "# Working artifacts produced by ai-dev-orchestrator.\n"
                    "# Delete this file if you would rather commit them for team visibility.\n"
                    "*\n"
                )
        return self

    # -- state -------------------------------------------------------------

    def read_state(self) -> Dict[str, Any]:
        if not os.path.isfile(self.state_path):
            return {"version": 1, "runs": []}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {"version": 1, "runs": []}
        if not isinstance(data, dict):
            return {"version": 1, "runs": []}
        data.setdefault("runs", [])
        return data

    def write_state(self, state: Dict[str, Any]) -> None:
        self.ensure()
        write_json(self.state_path, state)

    def record_event(
        self, stage: str, status: str, detail: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Append one stage outcome to the run log (models included, secrets not)."""
        state = self.read_state()
        event = {"stage": stage, "status": status, "at": utcnow()}
        if detail:
            event.update(detail)
        state.setdefault("events", []).append(event)
        state["updated_at"] = utcnow()
        self.write_state(state)
        return event


def write_json(path: str, data: Any) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_text(path: str, text: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read_text(path: str, default: str = "") -> str:
    if not os.path.isfile(path):
        return default
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def list_files(directory: str, suffix: str = "") -> List[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(suffix) and os.path.isfile(os.path.join(directory, name))
    )
