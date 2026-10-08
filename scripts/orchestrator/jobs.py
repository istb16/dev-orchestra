"""Detached runs, so no single call can block the orchestrator forever.

The deadlines in :mod:`orchestrator.execution` bound how long a delegated agent
can misbehave, but while one is running the caller is inside that call. If the
orchestrator is itself an agent in a session, a thirty-minute block is
indistinguishable from a crash, and nothing it might do about the situation is
reachable.

Detaching removes that structurally: ``run --detach`` starts the work in its own
process and returns a job id immediately. ``jobs wait`` then polls with a
deadline *it* controls, so the worst case is a bounded wait and a clear status
rather than an open-ended block.

A job's whole life is one JSON file under ``.ai/jobs/``, written by the worker,
so progress survives the parent dying and can be read by anyone -- another
terminal, a later session, or a supervisor.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import activity, execution
from . import workspace as ws

#: Terminal states. Anything else means the job is still supposed to be running.
FINISHED = ("succeeded", "failed", "cancelled", "abandoned")

#: Placeholders the caller puts in the worker's argv for :func:`start` to fill
#: in. Both paths are built from the job id, so only this module can name them;
#: only the caller knows where on its own command line they may go, because
#: ``--extra`` is ``nargs=REMAINDER`` and swallows anything after it. Appending
#: them here instead is how the worker came to be handed its prompt path and
#: its job path as provider arguments, claiming nothing and reading nothing.
PROMPT_FILE = "{prompt_file}"
JOB_FILE = "{job_file}"
#: The prompt a resumed architect run sends, copied like the fresh one so the
#: worker reads what the parent checked, not the file as it is later.
RESUME_PROMPT_FILE = "{resume_prompt_file}"


def jobs_dir(workspace: ws.Workspace) -> str:
    return os.path.join(workspace.dir, "jobs")


def job_path(workspace: ws.Workspace, job_id: str) -> str:
    return os.path.join(jobs_dir(workspace), "%s.json" % job_id)


def stop_note_path(job_file: str) -> str:
    """Where a worker stopped by SIGTERM names a CLI group it could not end."""
    return os.path.splitext(job_file)[0] + ".stop"


def activity_path(job_file: str) -> str:
    """Where a worker keeps the tool lines of its run (see :mod:`orchestrator.activity`)."""
    return os.path.splitext(job_file)[0] + ".activity"


def read_activity(
    workspace: ws.Workspace, job: Dict[str, Any], since: int = 0, latest: int = 10
) -> Optional[Dict[str, Any]]:
    """The job's activity as ``jobs show/wait --json`` reports it, or None
    when the job has no activity file."""
    act = activity.read(activity_path(job_path(workspace, str(job.get("id")))), since, latest)
    if act is not None:
        act["elapsed_seconds"] = elapsed_seconds(job)
    return act


def _epoch(stamp: Any) -> Optional[float]:
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def elapsed_seconds(job: Dict[str, Any]) -> Optional[int]:
    """Seconds since the worker claimed the job (or it was started), up to
    when it finished or now; None when neither time can be read."""
    begun = _epoch(job.get("claimed_at"))
    if begun is None:
        begun = _epoch(job.get("started_at"))
    if begun is None:
        return None
    end = _epoch(job.get("finished_at")) if job.get("finished_at") else time.time()
    if end is None:
        return None
    return max(int(end - begun), 0)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return "%dh%02dm%02ds" % (hours, minutes, seconds)
    return "%dm%02ds" % (minutes, seconds)


def output_path(workspace: ws.Workspace, job_id: str) -> str:
    return os.path.join(jobs_dir(workspace), "%s.out" % job_id)


def new_job_id(stage: str) -> str:
    """A readable, unique id.

    The random tail matters: a timestamp alone collides when two runs start in
    the same millisecond, and two jobs sharing a file would overwrite each
    other's status.
    """
    return "%s-%s-%s" % (stage, time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])


def write_job(workspace: ws.Workspace, job: Dict[str, Any]) -> None:
    os.makedirs(jobs_dir(workspace), exist_ok=True)
    ws.write_json(job_path(workspace, str(job["id"])), job)


def read_job(workspace: ws.Workspace, job_id: str) -> Optional[Dict[str, Any]]:
    job = ws.read_json(job_path(workspace, job_id), None)
    if not isinstance(job, dict):
        return None
    return _reconcile(workspace, job)


def list_jobs(workspace: ws.Workspace) -> List[Dict[str, Any]]:
    found = []
    for path in ws.list_files(jobs_dir(workspace), ".json"):
        job = ws.read_json(path, None)
        if isinstance(job, dict):
            found.append(_reconcile(workspace, job))
    found.sort(key=lambda job: str(job.get("started_at") or ""), reverse=True)
    return found


def _reconcile(workspace: ws.Workspace, job: Dict[str, Any]) -> Dict[str, Any]:
    """Notice a worker that died without writing its outcome.

    Without this a killed worker leaves a job that claims to be running for
    ever, which is precisely the silent stall this module is meant to remove.
    """
    if job.get("status") in FINISHED:
        return job
    pid = int(job.get("pid") or 0)
    if pid and _worker_alive(pid, job) is False:

        def abandoned(current: Dict[str, Any]) -> None:
            # Read again under the lock: the worker may have written its
            # outcome after ``job`` was read and then exited, and that outcome
            # stands. A different pid is a worker this check never looked at.
            if current.get("status") in FINISHED or int(current.get("pid") or 0) != pid:
                return
            current["status"] = "abandoned"
            current["error"] = "the worker process (pid %d) is gone and recorded no outcome" % pid
            current["finished_at"] = ws.utcnow()

        job = update(job_path(workspace, str(job["id"])), abandoned, default=job)
    return job


def _worker_alive(pid: int, job: Dict[str, Any]) -> Optional[bool]:
    """Whether ``pid`` is the job's worker and still running; None when it is
    running but nothing recorded says whose it is.

    A pid is reused once its process is gone, and a worker that vanished
    without a word -- with a reboot, say -- leaves one that may be anyone's.
    So the pid is checked against the start time recorded with it, and a
    different start is a different process: the worker is gone. A record
    without one (written before it was kept, or on macOS, where none can be
    read) is None.
    """
    if not execution.pid_alive(pid):
        return False
    started = job.get("pid_started")
    if not started:
        return None
    return execution.process_started(pid) == started


def _record_pid(job: Dict[str, Any], pid: int, started: Optional[str]) -> None:
    job["pid"] = pid
    # Never left behind from an earlier pid: it would vouch for the wrong one.
    if started:
        job["pid_started"] = started
    else:
        job.pop("pid_started", None)


def start(
    workspace: ws.Workspace,
    stage: str,
    argv: Sequence[str],
    prompt: str = "",
    timeout: Optional[float] = None,
    resume_prompt: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Spawn ``argv`` as a detached worker and return the job record.

    ``force`` is whether the user passed ``--force``: the worker is always
    started with it, so it cannot tell from its own argv.
    """
    # Checked here because a placeholder the caller forgot is otherwise
    # invisible until the worker is already gone: it would run with no prompt
    # and no job to claim, and be reported as abandoned.
    missing = [name for name in (PROMPT_FILE, JOB_FILE) if name not in argv]
    if missing:
        raise ValueError(
            "the worker argv must carry %s and %s for start() to fill in; missing: %s"
            % (PROMPT_FILE, JOB_FILE, ", ".join(missing))
        )
    if (resume_prompt is not None) != (RESUME_PROMPT_FILE in argv):
        raise ValueError("%s and resume_prompt go together, or not at all" % RESUME_PROMPT_FILE)
    os.makedirs(jobs_dir(workspace), exist_ok=True)
    job_id = new_job_id(stage)
    prompt_file = os.path.join(jobs_dir(workspace), "%s.prompt" % job_id)
    ws.write_text(prompt_file, prompt)
    filled = {PROMPT_FILE: prompt_file, JOB_FILE: job_path(workspace, job_id)}
    resume_prompt_file = None
    if resume_prompt is not None:
        resume_prompt_file = os.path.join(jobs_dir(workspace), "%s.resume-prompt" % job_id)
        ws.write_text(resume_prompt_file, resume_prompt)
        filled[RESUME_PROMPT_FILE] = resume_prompt_file
    argv = [filled.get(arg, arg) for arg in argv]

    job = {
        "id": job_id,
        "stage": stage,
        "status": "starting",
        "started_at": ws.utcnow(),
        "command": list(argv),
        "timeout_seconds": timeout,
        # The id is new, so there is no output yet to read.
        "output": "",
        "prompt_file": prompt_file,
        "output_file": output_path(workspace, job_id),
        "force": bool(force),
    }
    if resume_prompt_file is not None:
        job["resume_prompt_file"] = resume_prompt_file
    write_job(workspace, job)

    # The worker is this same CLI, re-entered with --job-file. Its stdio goes
    # nowhere: everything it wants to say goes into the job file instead, so
    # nothing depends on a parent staying alive to read a pipe.
    command = [sys.executable, _entry_point(), *argv]
    try:
        proc = subprocess.Popen(
            command,
            cwd=workspace.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **execution.spawn_kwargs(),
        )
    except OSError as exc:
        job["status"] = "failed"
        job["error"] = "could not start the worker: %s" % exc
        job["finished_at"] = ws.utcnow()
        write_job(workspace, job)
        return job
    started = execution.process_started(proc.pid)

    def spawned(current: Dict[str, Any]) -> None:
        # Merged into what is on disk, not written over it: a fast worker can
        # already have claimed the job or finished it, and none of that may be
        # put back to how it looked before the spawn.
        if "pid" not in current:
            _record_pid(current, proc.pid, started)
        if current.get("status") == "starting":
            current["status"] = "running"

    return update(job_path(workspace, job_id), spawned, default=job)


def _entry_point() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dev_orchestra.py")


def wait(
    workspace: ws.Workspace,
    job_id: str,
    timeout: float = 60.0,
    poll: float = 1.0,
) -> Dict[str, Any]:
    """Wait for a job, but never longer than ``timeout``.

    Returning while the job is still running is a normal outcome, not an error:
    the caller gets a bounded wait and can decide what to do next.
    """
    deadline = time.monotonic() + timeout
    while True:
        job = read_job(workspace, job_id) or {"id": job_id, "status": "unknown"}
        if job.get("status") in FINISHED:
            return job
        if time.monotonic() >= deadline:
            job = dict(job)
            job["waited_out"] = True
            return job
        time.sleep(min(poll, max(deadline - time.monotonic(), 0.01)))


def cancel(workspace: ws.Workspace, job_id: str) -> Dict[str, Any]:
    job = read_job(workspace, job_id)
    if job is None:
        raise KeyError(job_id)
    if job.get("status") in FINISHED:
        return job
    pid = int(job.get("pid") or 0)
    alive = _worker_alive(pid, job) if pid else False
    killed = False
    note = " (the worker may still be running)"
    if alive or (alive is None and not execution.IS_WINDOWS):
        # Unconfirmed on POSIX, kill_tree still signals the pid only while it
        # leads its own group, as the worker does. Windows has no such check,
        # and taskkill /T /F there would end whatever tree now has the pid.
        killed = execution.kill_tree(pid, execution.KILL_GRACE_SECONDS)
    elif alive is None:
        note = " (pid %d was not stopped: it may no longer be the worker, which may still be running)" % pid
    error = "cancelled by request%s" % ("" if killed else note)
    # Written by the worker's SIGTERM handler before it exited, so it is there
    # by now when the kill was confirmed.
    left = _read_stop_note(stop_note_path(job_path(workspace, job_id)))
    if left:
        error += "; %s" % "; ".join(left)

    def cancelled(current: Dict[str, Any]) -> None:
        # Read again under the lock: a worker that finished before the kill
        # reached it has written its outcome, and that outcome stands.
        if current.get("status") in FINISHED:
            return
        current["status"] = "cancelled"
        current["finished_at"] = ws.utcnow()
        current["error"] = error

    return update(job_path(workspace, job_id), cancelled, default=job)


def _read_stop_note(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    return [line.strip().removeprefix("warning: ") for line in lines if line.strip()]


# --------------------------------------------------------------------------- worker side


def update(
    job_file: str,
    change: Callable[[Dict[str, Any]], None],
    default: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply ``change`` to the job record on disk, under the job's lock.

    The parent and its worker both write the one file, and each used to read
    it, edit its copy and write the copy back -- so whichever wrote second
    undid the other. ``default`` stands in for a record that is not there.
    """
    with ws.file_lock(job_file):
        job = ws.read_json(job_file, None)
        if not isinstance(job, dict):
            job = dict(default or {})
        change(job)
        ws.write_json(job_file, job)
    return job


def claim(job_file: str) -> Dict[str, Any]:
    """Called by the worker: record that it owns this job."""

    started = execution.process_started(os.getpid())

    def claimed(job: Dict[str, Any]) -> None:
        _record_pid(job, os.getpid(), started)
        job["status"] = "running"
        job["claimed_at"] = ws.utcnow()

    return update(job_file, claimed)


def finish(
    job_file: str, status: str, output: str = "", error: str = "", detail: Optional[Dict[str, Any]] = None
) -> None:
    """Called by the worker: record the outcome, whatever happened."""

    def finished(job: Dict[str, Any]) -> None:
        job["status"] = status
        job["finished_at"] = ws.utcnow()
        if error:
            job["error"] = error
        if detail:
            job.update(detail)
        if output and job.get("output_file"):
            ws.write_text(str(job["output_file"]), output)

    update(job_file, finished)


def render(job: Dict[str, Any], act: Optional[Dict[str, Any]] = None) -> str:
    """A job as text. ``act`` is what :func:`read_activity` returned for it;
    without one a finished job reads exactly as it did before activity."""
    running = job.get("status") not in FINISHED
    lines = [
        "%s  %s" % (job.get("id"), str(job.get("status", "?")).upper()),
        "  stage:    %s" % job.get("stage"),
        "  started:  %s" % job.get("started_at"),
    ]
    if running:
        elapsed = elapsed_seconds(job)
        if elapsed is not None:
            lines.append("  elapsed:  %s" % _duration(elapsed))
    if job.get("finished_at"):
        lines.append("  finished: %s" % job["finished_at"])
    if job.get("pid"):
        lines.append("  pid:      %s" % job["pid"])
    if job.get("error"):
        lines.append("  error:    %s" % job["error"])
    if job.get("output_file") and os.path.isfile(str(job["output_file"])):
        lines.append("  output:   %s" % job["output_file"])
    # A refused ``--output`` write is otherwise invisible for a detached run:
    # the worker's stderr went nowhere, and the target looks merely unchanged.
    if job.get("output_written") is False:
        lines.append("  note:     %s was not updated (nothing usable to write)" % job.get("output_target"))
        if job.get("rejected_file"):
            lines.append("  rejected: %s" % job["rejected_file"])
    if job.get("waited_out"):
        lines.append("  note:     still running when the wait timed out")
    if act is not None:
        lines += _render_activity(job, act, running)
    return "\n".join(lines)


def _render_activity(job: Dict[str, Any], act: Dict[str, Any], running: bool) -> List[str]:
    count = int(act.get("count") or 0)
    summary = "  activity: %d tool use%s" % (count, "" if count == 1 else "s")
    tokens = act.get("context_tokens")
    if isinstance(tokens, int):
        summary += ", context {:,} tokens".format(tokens)
    lines = [summary]
    for entry in act.get("lines") or ():
        seconds = entry.get("s")
        stamp = "+%02d:%02d" % divmod(int(seconds), 60) if isinstance(seconds, (int, float)) else "+--:--"
        lines.append("    %s  %s" % (stamp, entry.get("line")))
    omitted = int(act.get("omitted") or 0)
    if omitted:
        lines.append("    (%d earlier line%s not shown)" % (omitted, "" if omitted == 1 else "s"))
    if act.get("dropped"):
        lines.append("    (some lines were dropped)")
    if running:
        lines.append("  next:     jobs wait %s --since %d" % (job.get("id"), count))
    return lines
