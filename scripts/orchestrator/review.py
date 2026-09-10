"""Independent multi-model review: snapshot, fan-out, parse, consolidate, triage.

Design rules enforced here:

* every reviewer sees the *same frozen snapshot*, taken before any reviewer runs
* reviewers never see each other's output -- no cross-contamination
* reviewers run read-only; a reviewer that edits files is a configuration bug
* one reviewer failing does not fail the batch
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import workspace as ws
from .providers import MODE_REVIEW, ModelResolutionError, get_provider

SEVERITIES = ("critical", "high", "medium", "low")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
TRIAGE_STATUSES = ("accepted", "rejected", "duplicate", "needs-investigation", "needs-triage")

#: Inline the diff up to this size; beyond it, reviewers read the file instead.
MAX_INLINE_DIFF_CHARS = 120_000

ROLE_GUIDANCE: Dict[str, str] = {
    "general": (
        "Prioritise, in this order: correctness bugs, regressions, missing edge cases, "
        "security problems, performance problems, data consistency, concurrency issues, "
        "error handling, and insufficient tests. Also flag unnecessary complexity. "
        "Style-only observations are low severity at most, and usually not worth reporting."
    ),
    "security": (
        "Focus on authentication and authorisation gaps, injection (SQL/command/template), "
        "unsafe deserialisation, SSRF, path traversal, secret handling and leakage, unsafe "
        "defaults, missing input validation, and access-control regressions in changed code paths."
    ),
    "performance": (
        "Focus on algorithmic complexity, N+1 queries, missing indexes, unnecessary I/O, "
        "unbounded memory growth, blocking calls on hot paths, and cache invalidation mistakes. "
        "Quantify the cost where the diff gives you enough information."
    ),
    "test": (
        "Focus on test coverage of the changed behaviour: missing edge cases, tests that assert "
        "nothing meaningful, flaky patterns (time, ordering, network), and untested error paths. "
        "Name the specific case that is missing, not just 'add more tests'."
    ),
    "architecture": (
        "Focus on layering violations, misplaced responsibilities, leaky abstractions, coupling "
        "introduced by the change, public API/contract shape, and whether the change fits the "
        "conventions already present in this codebase."
    ),
    "database": (
        "Focus on schema changes, migration safety (locking, backfills, reversibility), "
        "nullability and constraint changes, index coverage for new queries, transaction "
        "boundaries, and data-consistency risk during deploy."
    ),
    "frontend": (
        "Focus on component state handling, rendering performance, accessibility (roles, labels, "
        "keyboard and focus behaviour), responsive layout, error and loading states, and "
        "client-side validation that is not mirrored server-side."
    ),
    "backend": (
        "Focus on API contracts and compatibility, validation, error responses and status codes, "
        "idempotency, transactional integrity, background job semantics, and observability of "
        "the changed paths."
    ),
}

REVIEW_PROMPT_TEMPLATE = """You are an independent code reviewer.

Reviewer id: {reviewer_id}
Review role: {role}
Repository root: {root}

## Your task

Review ONLY the change described by the snapshot below. Judge it on its merits.
You are read-only: do not modify, create, or delete any file. Do not run
commands that mutate the repository or the network.

You may read any file in the repository to understand context.

{role_guidance}

## Change under review

{diff_section}

## Required output format

Report every issue as a block in exactly this format:

## Finding
- Severity: critical | high | medium | low
- File: <path relative to the repository root>
- Line: <line number or range, or "n/a">
- Category: <short category, e.g. correctness, security, performance, tests>
- Problem: <what is wrong, one or two sentences>
- Impact: <what breaks, and under what conditions>
- Evidence: <the specific code or diff hunk that shows it>
- Recommended fix: <concrete change you would make>

Rules:
- Report only issues you can point at in the code. Do not speculate.
- Do not report the same issue twice.
- If the change is sound and you find nothing worth fixing, reply with exactly:

NO_FINDINGS

Output nothing except findings (or NO_FINDINGS). No preamble, no summary.
"""


# --------------------------------------------------------------------------- snapshot


def create_snapshot(
    workspace: ws.Workspace,
    base: Optional[str] = None,
    include_untracked: bool = True,
) -> Dict[str, Any]:
    """Freeze the change under review into ``.ai/reviews/review-target.diff``."""
    workspace.ensure()
    root = workspace.root
    if not ws.is_git_repo(root):
        raise ReviewError(
            "%s is not a git repository; review snapshots need git. "
            "Initialise a repo or review a specific set of files manually." % root
        )

    if base:
        code, diff, err = ws.git(["diff", "--no-color", base, "--"], root)
        strategy = "git diff %s" % base
    else:
        code, diff, err = ws.git(["diff", "--no-color", "HEAD", "--"], root)
        strategy = "git diff HEAD"
        if code != 0:
            # A repository with no commits yet: everything is untracked.
            code, diff, err = 0, "", ""
            strategy = "untracked-only (no HEAD commit)"
    if code != 0:
        raise ReviewError("git diff failed: %s" % (err.strip() or code))

    untracked: List[str] = []
    if include_untracked:
        ucode, uout, _ = ws.git(["ls-files", "--others", "--exclude-standard"], root)
        if ucode == 0:
            for name in [line.strip() for line in uout.splitlines() if line.strip()]:
                path = os.path.join(root, name)
                if not os.path.isfile(path) or os.path.getsize(path) > 512_000:
                    continue
                if _is_orchestrator_artifact(name, workspace):
                    continue
                dcode, dout, _ = ws.git(["diff", "--no-color", "--no-index", "--", os.devnull, name], root)
                # --no-index exits 1 when files differ, which is the normal case.
                if dcode in (0, 1) and dout.strip():
                    diff += dout
                    untracked.append(name)

    head = ""
    hcode, hout, _ = ws.git(["rev-parse", "HEAD"], root)
    if hcode == 0:
        head = hout.strip()

    ws.write_text(workspace.snapshot_path, diff)
    meta = {
        "generated_at": ws.utcnow(),
        "strategy": strategy,
        "base": base,
        "head": head,
        "files": _changed_files(diff),
        "untracked_included": untracked,
        "bytes": len(diff.encode("utf-8")),
        "sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "empty": not diff.strip(),
    }
    ws.write_json(workspace.snapshot_meta_path, meta)
    return meta


def _is_orchestrator_artifact(name: str, workspace: ws.Workspace) -> bool:
    """The skill's own files are not part of the change under review.

    Without this, the config the setup wizard just wrote and the artifacts in
    ``.ai/`` show up as untracked "changes" in every snapshot.
    """
    normalised = name.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    workspace_prefix = workspace.relative(workspace.dir).rstrip("/") + "/"
    if normalised == workspace_prefix.rstrip("/") or normalised.startswith(workspace_prefix):
        return True
    from .config import PROJECT_CONFIG_NAMES

    return os.path.basename(normalised) in PROJECT_CONFIG_NAMES


def _changed_files(diff: str) -> List[str]:
    files: List[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            name = line[6:].strip()
            if name and name != "/dev/null" and name not in files:
                files.append(name)
    return files


class ReviewError(RuntimeError):
    """Raised for review-flow problems that should stop the current stage."""


# --------------------------------------------------------------------------- fan-out


def build_review_prompt(
    reviewer: Dict[str, Any],
    workspace: ws.Workspace,
    diff_text: str,
    extra_context: str = "",
    template: Optional[str] = None,
) -> str:
    role = str(reviewer.get("role") or "general")
    guidance = ROLE_GUIDANCE.get(
        role,
        "Review the change from the perspective of a %s specialist. Report only concrete, "
        "evidence-backed issues within that perspective." % role,
    )
    if len(diff_text) <= MAX_INLINE_DIFF_CHARS:
        diff_section = "```diff\n%s\n```" % diff_text.rstrip()
    else:
        diff_section = (
            "The diff is too large to inline. Read it from this file, which is frozen "
            "for the duration of this review:\n\n    %s\n\nReview only what that diff contains."
            % workspace.relative(workspace.snapshot_path)
        )
    prompt = (template or REVIEW_PROMPT_TEMPLATE).format(
        reviewer_id=reviewer.get("id", "reviewer"),
        role=role,
        role_guidance=guidance,
        root=workspace.root,
        diff_section=diff_section,
    )
    if extra_context.strip():
        prompt += "\n## Additional context from the orchestrator\n\n%s\n" % extra_context.strip()
    return prompt


class ReviewerRun:
    def __init__(
        self,
        reviewer: Dict[str, Any],
        status: str,
        report_path: Optional[str] = None,
        error: str = "",
        model_display: str = "",
        duration: float = 0.0,
        findings: int = 0,
    ) -> None:
        self.reviewer = reviewer
        self.status = status  # ok | failed
        self.report_path = report_path
        self.error = error
        self.model_display = model_display
        self.duration = duration
        self.findings = findings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.reviewer.get("id"),
            "provider": self.reviewer.get("provider"),
            "role": self.reviewer.get("role", "general"),
            "model": self.model_display,
            "status": self.status,
            "error": self.error,
            "duration_seconds": round(self.duration, 2),
            "findings": self.findings,
            "report": self.report_path,
        }


def run_reviews(
    reviewers: Sequence[Dict[str, Any]],
    workspace: ws.Workspace,
    parallel: bool = True,
    timeout: int = 1800,
    extra_context: str = "",
    template: Optional[str] = None,
) -> List[ReviewerRun]:
    """Run every configured reviewer against the frozen snapshot."""
    if not reviewers:
        return []
    diff_text = ws.read_text(workspace.snapshot_path)
    if not diff_text.strip():
        raise ReviewError(
            "review snapshot is empty -- run `review snapshot` after making changes, "
            "or pass --base to compare against a different revision"
        )

    def run_one(reviewer: Dict[str, Any]) -> ReviewerRun:
        reviewer_id = str(reviewer.get("id") or "reviewer")
        try:
            provider = get_provider(str(reviewer.get("provider")))
        except Exception as exc:
            return ReviewerRun(reviewer, "failed", error=str(exc))
        prompt = build_review_prompt(reviewer, workspace, diff_text, extra_context, template)
        try:
            result = provider.run(prompt, MODE_REVIEW, workspace.root, reviewer.get("model"), timeout=timeout)
        except ModelResolutionError as exc:
            return ReviewerRun(reviewer, "failed", error=str(exc))
        except Exception as exc:
            return ReviewerRun(reviewer, "failed", error="%s: %s" % (type(exc).__name__, exc))

        model_display = result.resolved.display if result.resolved else ""
        if not result.ok:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            return ReviewerRun(
                reviewer,
                "failed",
                error=(detail[-1] if detail else "exit code %s" % result.exit_code),
                model_display=model_display,
                duration=result.duration,
            )
        body = result.stdout.strip() or "NO_FINDINGS"
        header = (
            "# Review\n\n"
            "- Reviewer: %s\n- Provider: %s\n- Model: %s\n- Role: %s\n- Snapshot: %s\n\n---\n\n"
            % (
                reviewer_id,
                reviewer.get("provider"),
                model_display or "unknown",
                reviewer.get("role", "general"),
                _snapshot_sha(workspace),
            )
        )
        path = workspace.reviewer_report_path(reviewer_id)
        ws.write_text(path, header + body + "\n")
        findings = parse_findings(body, reviewer_id)
        return ReviewerRun(
            reviewer,
            "ok",
            report_path=workspace.relative(path),
            model_display=model_display,
            duration=result.duration,
            findings=len(findings),
        )

    if parallel and len(reviewers) > 1:
        with ThreadPoolExecutor(max_workers=min(len(reviewers), 8)) as pool:
            return list(pool.map(run_one, reviewers))
    return [run_one(reviewer) for reviewer in reviewers]


def _snapshot_sha(workspace: ws.Workspace) -> str:
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    return str(meta.get("sha256", ""))[:12] or "unknown"


# --------------------------------------------------------------------------- parsing

_FIELD_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?\*{0,2}(severity|file|line|lines|category|problem|impact|evidence|"
    r"recommended fix|recommendation|fix)\*{0,2}\s*:\s*(.*)$",
    re.IGNORECASE,
)
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*finding\b.*$", re.IGNORECASE)
_FIELD_ALIASES = {
    "lines": "line",
    "recommendation": "recommended_fix",
    "fix": "recommended_fix",
    "recommended fix": "recommended_fix",
}


def parse_findings(text: str, reviewer_id: str) -> List[Dict[str, Any]]:
    """Parse a reviewer report into structured findings. Tolerant by design."""
    if not text:
        return []
    body = _strip_report_header(text)
    if not _HEADER_RE.search(body) and re.search(r"^\s*NO_FINDINGS\s*$", body, re.MULTILINE):
        return []

    blocks: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in body.splitlines():
        if _HEADER_RE.match(line):
            current = []
            blocks.append(current)
            continue
        if current is not None:
            current.append(line)

    findings: List[Dict[str, Any]] = []
    for block in blocks:
        finding = _parse_block(block)
        if not finding:
            continue
        finding["reviewer"] = reviewer_id
        findings.append(finding)
    return findings


def _strip_report_header(text: str) -> str:
    marker = "\n---\n"
    head, sep, tail = text.partition(marker)
    if sep and head.lstrip().startswith("# Review"):
        return tail
    return text


def _parse_block(lines: List[str]) -> Optional[Dict[str, Any]]:
    fields: Dict[str, List[str]] = {}
    key: Optional[str] = None
    for raw_line in lines:
        # Models bold the labels in several ways (``**Severity:** high`` and
        # ``**Severity**: high``); dropping the emphasis normalises all of them.
        line = raw_line.replace("**", "")
        match = _FIELD_RE.match(line)
        if match:
            raw_key = match.group(1).lower()
            key = _FIELD_ALIASES.get(raw_key, raw_key)
            fields.setdefault(key, []).append(match.group(2).strip())
            continue
        if key and line.strip() and not line.strip().startswith("#"):
            fields[key].append(line.strip())
    if not fields:
        return None
    finding: Dict[str, Any] = {
        "severity": _normalise_severity(_join(fields.get("severity"))),
        "file": _normalise_path(_join(fields.get("file"))),
        "line": _join(fields.get("line")) or "n/a",
        "category": (_join(fields.get("category")) or "general").lower(),
        "problem": _join(fields.get("problem")),
        "impact": _join(fields.get("impact")),
        "evidence": _join(fields.get("evidence")),
        "recommended_fix": _join(fields.get("recommended_fix")),
    }
    if not finding["problem"] and not finding["evidence"]:
        return None
    return finding


def _join(values: Optional[List[str]]) -> str:
    if not values:
        return ""
    return " ".join(part for part in (value.strip() for value in values) if part).strip()


def _normalise_severity(value: str) -> str:
    lowered = (value or "").strip().lower()
    for severity in SEVERITIES:
        if lowered.startswith(severity):
            return severity
    if lowered in ("blocker", "critical/high"):
        return "critical"
    if lowered in ("major",):
        return "high"
    if lowered in ("minor", "nit", "nitpick", "info", "style"):
        return "low"
    return "medium"


def _normalise_path(value: str) -> str:
    path = (value or "").strip().strip("`").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


# --------------------------------------------------------------------------- consolidation

_WORD_RE = re.compile(r"[a-z0-9]+")


def _fingerprint(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _line_number(value: str) -> Optional[int]:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else None


def _same_locus(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if left["file"] != right["file"]:
        return False
    lnum, rnum = _line_number(left.get("line", "")), _line_number(right.get("line", ""))
    if lnum is None or rnum is None:
        return True
    return abs(lnum - rnum) <= 10


def are_duplicates(left: Dict[str, Any], right: Dict[str, Any], threshold: float = 0.72) -> bool:
    """Two findings are the same issue when they sit at the same place and say the same thing."""
    if not _same_locus(left, right):
        return False
    left_text = _fingerprint("%s %s" % (left.get("problem", ""), left.get("recommended_fix", "")))
    right_text = _fingerprint("%s %s" % (right.get("problem", ""), right.get("recommended_fix", "")))
    if not left_text or not right_text:
        return False
    if left.get("category") == right.get("category") and left_text == right_text:
        return True
    return difflib.SequenceMatcher(None, left_text, right_text).ratio() >= threshold


def consolidate_findings(findings: Sequence[Dict[str, Any]], threshold: float = 0.72) -> List[Dict[str, Any]]:
    """Merge duplicate findings across reviewers, keeping the strongest wording."""
    merged: List[Dict[str, Any]] = []
    for finding in sorted(findings, key=lambda f: SEVERITY_RANK.get(f.get("severity", "medium"), 2)):
        for existing in merged:
            if are_duplicates(existing, finding, threshold):
                _absorb(existing, finding)
                break
        else:
            entry = dict(finding)
            entry["reported_by"] = [finding.get("reviewer", "unknown")]
            entry["duplicate_count"] = 1
            entry.pop("reviewer", None)
            merged.append(entry)

    merged.sort(key=lambda f: (SEVERITY_RANK.get(f.get("severity", "medium"), 2), f.get("file", "")))
    for index, entry in enumerate(merged, 1):
        entry["id"] = "F%d" % index
        entry.setdefault("triage", "needs-triage")
        entry.setdefault("triage_note", "")
    return merged


def _absorb(target: Dict[str, Any], other: Dict[str, Any]) -> None:
    reviewer = other.get("reviewer", "unknown")
    if reviewer not in target["reported_by"]:
        target["reported_by"].append(reviewer)
    target["duplicate_count"] = target.get("duplicate_count", 1) + 1
    if SEVERITY_RANK.get(other.get("severity", "medium"), 2) < SEVERITY_RANK.get(
        target.get("severity", "medium"), 2
    ):
        target["severity"] = other["severity"]
    for field in ("problem", "impact", "evidence", "recommended_fix"):
        if len(other.get(field, "")) > len(target.get(field, "")):
            target[field] = other[field]
    if target.get("line") in ("", "n/a") and other.get("line") not in ("", "n/a"):
        target["line"] = other["line"]


def collect_reports(workspace: ws.Workspace, reviewer_ids: Sequence[str]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for reviewer_id in reviewer_ids:
        path = workspace.reviewer_report_path(reviewer_id)
        if not os.path.isfile(path):
            continue
        findings.extend(parse_findings(ws.read_text(path), reviewer_id))
    return findings


def build_consolidation(
    workspace: ws.Workspace,
    runs: Sequence[Dict[str, Any]],
    findings: Sequence[Dict[str, Any]],
    iteration: int = 1,
) -> Dict[str, Any]:
    meta = ws.read_json(workspace.snapshot_meta_path, {}) or {}
    previous = {
        entry.get("id"): entry
        for entry in (ws.read_json(workspace.consolidated_json_path, {}) or {}).get("findings", [])
    }
    consolidated = consolidate_findings(findings)
    for entry in consolidated:
        old = previous.get(entry["id"])
        if old and _fingerprint(old.get("problem", "")) == _fingerprint(entry.get("problem", "")):
            entry["triage"] = old.get("triage", entry["triage"])
            entry["triage_note"] = old.get("triage_note", "")
    return {
        "generated_at": ws.utcnow(),
        "iteration": iteration,
        "snapshot": {"sha256": meta.get("sha256"), "files": meta.get("files", [])},
        "reviewers": list(runs),
        "counts": _counts(consolidated, runs),
        "findings": consolidated,
    }


def _counts(findings: Sequence[Dict[str, Any]], runs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    by_severity = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        by_severity[finding.get("severity", "medium")] = (
            by_severity.get(finding.get("severity", "medium"), 0) + 1
        )
    return {
        "reviewers_total": len(runs),
        "reviewers_ok": sum(1 for run in runs if run.get("status") == "ok"),
        "reviewers_failed": sum(1 for run in runs if run.get("status") != "ok"),
        "findings_total": len(findings),
        "by_severity": by_severity,
        "duplicates_merged": sum(max(0, f.get("duplicate_count", 1) - 1) for f in findings),
    }


def render_consolidation(data: Dict[str, Any]) -> str:
    counts = data.get("counts", {})
    lines = [
        "# Consolidated review",
        "",
        "- Generated: %s" % data.get("generated_at"),
        "- Iteration: %s" % data.get("iteration"),
        "- Snapshot: %s" % str(data.get("snapshot", {}).get("sha256", ""))[:12],
        "- Reviewers: %s ok / %s total" % (counts.get("reviewers_ok"), counts.get("reviewers_total")),
        "- Findings: %s (%s duplicate report(s) merged)"
        % (counts.get("findings_total"), counts.get("duplicates_merged")),
        "",
        "## Reviewers",
        "",
    ]
    for run in data.get("reviewers", []):
        mark = "ok" if run.get("status") == "ok" else "FAILED"
        lines.append(
            "- %s [%s] %s / %s / %s -- %s"
            % (
                mark,
                run.get("role"),
                run.get("id"),
                run.get("provider"),
                run.get("model") or "unknown model",
                (run.get("error") or "%s finding(s)" % run.get("findings", 0)),
            )
        )
    lines += ["", "## Findings", ""]
    if not data.get("findings"):
        lines.append("No findings were reported.")
    for finding in data.get("findings", []):
        lines += [
            "### %s [%s] %s"
            % (finding["id"], finding["severity"].upper(), finding.get("category", "general")),
            "",
            "- Location: `%s`:%s" % (finding.get("file", "?"), finding.get("line", "n/a")),
            "- Reported by: %s" % ", ".join(finding.get("reported_by", [])),
            "- Triage: %s%s"
            % (finding.get("triage", "needs-triage"), _note_suffix(finding.get("triage_note", ""))),
            "- Problem: %s" % finding.get("problem", ""),
            "- Impact: %s" % finding.get("impact", ""),
            "- Evidence: %s" % finding.get("evidence", ""),
            "- Recommended fix: %s" % finding.get("recommended_fix", ""),
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _note_suffix(note: str) -> str:
    return " (%s)" % note if note else ""


def set_triage(data: Dict[str, Any], finding_id: str, status: str, note: str = "") -> Dict[str, Any]:
    if status not in TRIAGE_STATUSES:
        raise ReviewError(
            "unknown triage status %r (expected one of %s)" % (status, ", ".join(TRIAGE_STATUSES))
        )
    for finding in data.get("findings", []):
        if finding.get("id") == finding_id:
            finding["triage"] = status
            finding["triage_note"] = note
            return data
    raise ReviewError("no finding with id %r" % finding_id)


def accepted_findings(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [f for f in data.get("findings", []) if f.get("triage") == "accepted"]


def unresolved_blocking(
    data: Dict[str, Any], severities: Sequence[str] = ("critical", "high")
) -> List[Dict[str, Any]]:
    """Findings that should block completion: accepted or untriaged and severe."""
    blocking = []
    for finding in data.get("findings", []):
        if finding.get("severity") not in severities:
            continue
        if finding.get("triage") in ("rejected", "duplicate"):
            continue
        blocking.append(finding)
    return blocking


def render_fix_brief(data: Dict[str, Any]) -> str:
    """The prompt payload handed to the Review Fixer: accepted findings only."""
    findings = accepted_findings(data)
    if not findings:
        return "No accepted findings. Nothing to fix.\n"
    lines = ["# Accepted review findings to fix", ""]
    for finding in findings:
        lines += [
            "## %s [%s] %s:%s"
            % (
                finding["id"],
                finding["severity"].upper(),
                finding.get("file", "?"),
                finding.get("line", "n/a"),
            ),
            "",
            "- Category: %s" % finding.get("category", "general"),
            "- Reported by: %s" % ", ".join(finding.get("reported_by", [])),
            "- Problem: %s" % finding.get("problem", ""),
            "- Impact: %s" % finding.get("impact", ""),
            "- Evidence: %s" % finding.get("evidence", ""),
            "- Recommended fix: %s" % finding.get("recommended_fix", ""),
            "",
        ]
    return "\n".join(lines)


def summarise_runs(runs: Sequence[ReviewerRun]) -> Tuple[int, int]:
    ok = sum(1 for run in runs if run.status == "ok")
    return ok, len(runs) - ok
