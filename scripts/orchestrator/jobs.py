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
from typing import Any, Dict, List, Optional, Sequence

from . import execution
from . import workspace as ws

#: Terminal states. Anything else means the job is still supposed to be running.
FINISHED = ("succeeded", "failed", "cancelled", "abandoned")


def jobs_dir(workspace: ws.Workspace) -> str:
    return os.path.join(workspace.dir, "jobs")


def job_path(workspace: ws.Workspace, job_id: str) -> str:
    return os.path.join(jobs_dir(workspace), "%s.json" % job_id)


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
    if pid and not execution.pid_alive(pid):
        job = dict(job)
        job["status"] = "abandoned"
        job["error"] = "the worker process (pid %d) is gone and recorded no outcome" % pid
        job["finished_at"] = ws.utcnow()
        write_job(workspace, job)
    return job


def start(
    workspace: ws.Workspace,
    stage: str,
    argv: Sequence[str],
    prompt: str = "",
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Spawn ``argv`` as a detached worker and return the job record."""
    os.makedirs(jobs_dir(workspace), exist_ok=True)
    job_id = new_job_id(stage)
    prompt_file = os.path.join(jobs_dir(workspace), "%s.prompt" % job_id)
    ws.write_text(prompt_file, prompt)

    job = {
        "id": job_id,
        "stage": stage,
        "status": "starting",
        "started_at": ws.utcnow(),
        "command": list(argv),
        "timeout_seconds": timeout,
        "output": ws.read_text(output_path(workspace, job_id), ""),
        "prompt_file": prompt_file,
        "output_file": output_path(workspace, job_id),
    }
    write_job(workspace, job)

    # The worker is this same CLI, re-entered with --job-file. Its stdio goes
    # nowhere: everything it wants to say goes into the job file instead, so
    # nothing depends on a parent staying alive to read a pipe.
    command = [sys.executable, _entry_point(), *argv, "--job-file", job_path(workspace, job_id)]
    try:
        proc = subprocess.Popen(
            command,
            cwd=workspace.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **execution._spawn_kwargs(),
        )
    except OSError as exc:
        job["status"] = "failed"
        job["error"] = "could not start the worker: %s" % exc
        job["finished_at"] = ws.utcnow()
        write_job(workspace, job)
        return job

    job["pid"] = proc.pid
    job["status"] = "running"
    write_job(workspace, job)
    return job


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
    killed = False
    if pid and execution.pid_alive(pid):
        killed = _kill_pid(pid)
    job = dict(job)
    job["status"] = "cancelled"
    job["finished_at"] = ws.utcnow()
    job["error"] = "cancelled by request%s" % ("" if killed else " (the worker may still be running)")
    write_job(workspace, job)
    return job


def _kill_pid(pid: int) -> bool:
    if execution.IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=execution.KILL_GRACE_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return False
    return True


# --------------------------------------------------------------------------- worker side


def claim(job_file: str) -> Dict[str, Any]:
    """Called by the worker: record that it owns this job."""
    job = ws.read_json(job_file, None) or {}
    job["pid"] = os.getpid()
    job["status"] = "running"
    job["claimed_at"] = ws.utcnow()
    ws.write_json(job_file, job)
    return job


def finish(
    job_file: str, status: str, output: str = "", error: str = "", detail: Optional[Dict[str, Any]] = None
) -> None:
    """Called by the worker: record the outcome, whatever happened."""
    job = ws.read_json(job_file, None) or {}
    job["status"] = status
    job["finished_at"] = ws.utcnow()
    if error:
        job["error"] = error
    if detail:
        job.update(detail)
    if output and job.get("output_file"):
        ws.write_text(str(job["output_file"]), output)
    ws.write_json(job_file, job)


def render(job: Dict[str, Any]) -> str:
    lines = [
        "%s  %s" % (job.get("id"), str(job.get("status", "?")).upper()),
        "  stage:    %s" % job.get("stage"),
        "  started:  %s" % job.get("started_at"),
    ]
    if job.get("finished_at"):
        lines.append("  finished: %s" % job["finished_at"])
    if job.get("pid"):
        lines.append("  pid:      %s" % job["pid"])
    if job.get("error"):
        lines.append("  error:    %s" % job["error"])
    if job.get("output_file") and os.path.isfile(str(job["output_file"])):
        lines.append("  output:   %s" % job["output_file"])
    if job.get("waited_out"):
        lines.append("  note:     still running when the wait timed out")
    return "\n".join(lines)
