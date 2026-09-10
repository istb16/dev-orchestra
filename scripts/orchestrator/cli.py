"""``dev-orchestra`` command line interface.

Only the parts that benefit from being deterministic and reusable live here:
configuration, environment diagnosis, provider invocation, and the mechanical
half of the review pipeline (snapshot, fan-out, parse, dedupe). Judgement calls
-- which stages to run, whether a finding is real -- stay with the orchestrating
agent, which drives these commands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config as config_mod
from . import doctor as doctor_mod
from . import jobs as jobs_mod
from . import ledger as ledger_mod
from . import review as review_mod
from . import wizard as wizard_mod
from . import workspace as ws
from .providers import (
    MODE_IMPLEMENT,
    MODE_PLAN,
    MODE_REVIEW,
    MODES,
    ModelResolutionError,
    available_providers,
    get_provider,
)

__version__ = "0.1.0"

DEFAULT_MODES = {
    "orchestrator": MODE_PLAN,
    "architect": MODE_PLAN,
    "implementer": MODE_IMPLEMENT,
    "review_fixer": MODE_IMPLEMENT,
}


# --------------------------------------------------------------------------- helpers


def _out(text: str = "") -> None:
    sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))


def _err(text: str) -> None:
    sys.stderr.write(text.rstrip() + "\n")


def _emit_json(data: Any) -> None:
    _out(json.dumps(data, indent=2, ensure_ascii=False))


def _resolve_scope(requested: Optional[str], start: Optional[str] = None) -> str:
    if requested in ("global", "project"):
        return requested
    return "project" if config_mod.find_project_config(start) else "global"


def _layer_path(scope: str, start: Optional[str] = None) -> str:
    return config_mod.global_config_path() if scope == "global" else config_mod.project_config_path(start)


def _read_layer(scope: str, start: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    path = _layer_path(scope, start)
    if os.path.isfile(path):
        return path, config_mod.read_config_file(path)
    seed: Dict[str, Any] = (
        config_mod.default_config() if scope == "global" else {"version": config_mod.CONFIG_VERSION}
    )
    return path, seed


def _seed_reviewers(layer: Dict[str, Any], start: Optional[str] = None) -> None:
    """A project layer editing reviewers must start from the effective list."""
    if "reviewers" in layer:
        return
    effective = config_mod.load(start, validate_result=False)
    layer["reviewers"] = [dict(reviewer) for reviewer in effective.reviewers()]


def _workspace(args: argparse.Namespace) -> ws.Workspace:
    root = ws.repo_root(getattr(args, "cwd", None) or os.getcwd())
    loaded = config_mod.load(root, validate_result=False)
    return ws.Workspace(root, loaded.workspace_dir(root)).ensure()


def _load_or_die(start: Optional[str] = None) -> config_mod.LoadedConfig:
    try:
        return config_mod.load(start)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        _err("Run `dev-orchestra config setup` to rebuild the configuration.")
        raise SystemExit(2) from exc


# --------------------------------------------------------------------------- config


def cmd_config_show(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    if args.scope == "global":
        path, data = _read_layer("global", args.cwd)
        source = path if os.path.isfile(path) else "%s (not created yet)" % path
    elif args.scope == "project":
        path = config_mod.project_config_path(args.cwd)
        data = config_mod.read_config_file(path) if os.path.isfile(path) else {}
        source = path if os.path.isfile(path) else "no project override"
    else:
        data = loaded.data
        source = "effective (project: %s, global: %s)" % (
            loaded.project_path or "none",
            loaded.global_path or "none",
        )

    if args.json:
        _emit_json({"source": source, "config": data})
        return 0
    _out("Source: %s" % source)
    if loaded.used_defaults and args.scope not in ("global", "project"):
        _out("No config file found yet -- showing built-in defaults.")
    _out("")
    _out(wizard_mod.render_summary(data) if "orchestrator" in data else "(empty layer)")
    problems = config_mod.validate(loaded.data)
    if problems:
        _out("Problems:")
        for problem in problems:
            _out("  - %s" % problem)
    return 0


def cmd_config_path(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    _out("global:  %s%s" % (config_mod.global_config_path(), "" if loaded.global_path else "  (not created)"))
    _out(
        "project: %s"
        % (loaded.project_path or "none (would be %s)" % config_mod.project_config_path(args.cwd))
    )
    return 0


def cmd_config_setup(args: argparse.Namespace) -> int:
    scope = args.scope or "global"
    path = _layer_path(scope, args.cwd)
    existing = config_mod.read_config_file(path) if os.path.isfile(path) else None

    if args.defaults:
        data = config_mod.default_config()
        if scope == "project":
            data = {"version": config_mod.CONFIG_VERSION, "reviewers": data["reviewers"]}
            data.update({key: config_mod.default_config()[key] for key, _ in wizard_mod.ROLE_TITLES})
        save = True
    else:
        if not sys.stdin.isatty() and not args.force:
            _err("config setup needs an interactive terminal; use --defaults for the recommended setup.")
            return 2
        try:
            data, save = wizard_mod.run(wizard_mod.Prompter(), existing)
        except EOFError:
            _err("input ended before setup finished; nothing was saved. Try --defaults instead.")
            return 2

    if not save:
        _out("Not saved.")
        return 1
    problems = config_mod.validate(data)
    if problems:
        # Writing this would leave every workflow command failing with
        # "invalid configuration" straight after a successful-looking setup.
        _err("not saved -- the configuration is invalid:")
        for problem in problems:
            _err("  - %s" % problem)
        return 2
    config_mod.write_config_file(path, data)
    _out("Saved %s configuration to %s" % (scope, path))
    return 0


def cmd_config_reset(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path = _layer_path(scope, args.cwd)
    if args.delete:
        if os.path.isfile(path):
            os.remove(path)
            _out("Removed %s" % path)
        else:
            _out("Nothing to remove at %s" % path)
        return 0
    data = config_mod.default_config()
    if scope == "project":
        _out("Note: resetting a project override writes the full recommended config to it.")
    config_mod.write_config_file(path, data)
    _out("Reset %s configuration to recommended defaults: %s" % (scope, path))
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    if args.path.startswith("reviewers"):
        _seed_reviewers(layer, args.cwd)
    value = args.value if args.raw else config_mod.coerce_scalar(args.value)
    try:
        config_mod.set_path(layer, args.path, value)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    config_mod.write_config_file(path, layer)
    _out("%s = %r  (%s: %s)" % (args.path, value, scope, path))
    effective = config_mod.load(args.cwd, validate_result=False).data
    for problem in config_mod.validate(effective):
        _err("warning: %s" % problem)
    _warn_unresolvable(effective, args.path.split(".")[0])
    return 0


def _warn_unresolvable(effective: Dict[str, Any], role_key: str) -> None:
    """Changing a provider can strand a model family that only the old CLI knew."""
    spec = effective.get(role_key)
    if not isinstance(spec, dict) or not spec.get("provider"):
        return
    try:
        get_provider(str(spec["provider"])).resolve_model(spec.get("model"))
    except ModelResolutionError as exc:
        _err("warning: %s" % exc)
    except ValueError:
        pass


def cmd_config_validate(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    problems = config_mod.validate(loaded.data)
    if args.json:
        _emit_json({"valid": not problems, "problems": problems})
    elif problems:
        _out("Invalid configuration:")
        for problem in problems:
            _out("  - %s" % problem)
    else:
        _out("Configuration is valid.")
    return 1 if problems else 0


# --------------------------------------------------------------------------- models


def cmd_model_list(args: argparse.Namespace) -> int:
    names = [args.provider] if args.provider else available_providers()
    payload: Dict[str, Any] = {}
    for name in names:
        try:
            provider = get_provider(name)
        except ValueError as exc:
            _err(str(exc))
            return 2
        detection = provider.detect()
        entry: Dict[str, Any] = {
            "installed": detection.installed,
            "version": detection.version,
            "models": [],
            "fallback_updated": provider.fallback_updated,
        }
        if detection.installed:
            entry["models"] = [candidate.to_dict() for candidate in provider.list_models()]
        payload[name] = entry

    if args.json:
        _emit_json(payload)
        return 0
    for name, entry in payload.items():
        _out("%s: %s" % (name, "installed" if entry["installed"] else "not installed"))
        if not entry["installed"]:
            continue
        for model in entry["models"]:
            _out("  %-28s family=%-20s source=%s" % (model["label"], model["family"], model["source"]))
        if all(m["source"] == "builtin-fallback" for m in entry["models"]) and entry["models"]:
            _out("  (built-in fallback list, last reviewed %s)" % entry["fallback_updated"])
    return 0


# --------------------------------------------------------------------------- reviewers


def cmd_reviewer_list(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    reviewers = loaded.reviewers()
    if args.json:
        _emit_json(reviewers)
        return 0
    if not reviewers:
        _out("No reviewers configured.")
        return 0
    for index, reviewer in enumerate(reviewers, 1):
        model = reviewer.get("model") or {}
        _out(
            "%d. %-18s %-8s %-18s %-8s %s"
            % (
                index,
                reviewer.get("id"),
                reviewer.get("provider"),
                model.get("family", "default"),
                model.get("version", "latest"),
                reviewer.get("role", "general"),
            )
        )
    return 0


def cmd_reviewer_add(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    _seed_reviewers(layer, args.cwd)
    role = args.role or "general"
    reviewer_id = args.id or config_mod.suggest_reviewer_id(layer, args.provider, role)
    family = args.model
    if family is None:
        family = "opus" if args.provider == "claude" else "recommended-coding"
    reviewer = config_mod.make_reviewer(
        reviewer_id,
        args.provider,
        family,
        role,
        version="pinned" if args.pin else "latest",
        model_id=args.pin,
    )
    try:
        config_mod.add_reviewer(layer, reviewer)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    problems = config_mod.validate(config_mod.deep_merge(config_mod.default_config(), layer))
    blocking = [p for p in problems if p.startswith("reviewers")]
    if blocking:
        for problem in blocking:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer)
    _out("Added reviewer %s (%s / %s / %s) to %s" % (reviewer_id, args.provider, family, role, path))
    return 0


def cmd_reviewer_remove(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    _seed_reviewers(layer, args.cwd)
    try:
        _, removed = config_mod.remove_reviewer(layer, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    config_mod.write_config_file(path, layer)
    _out("Removed reviewer %s from %s" % (removed.get("id"), path))
    return 0


def cmd_reviewer_set(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args.scope, args.cwd)
    path, layer = _read_layer(scope, args.cwd)
    _seed_reviewers(layer, args.cwd)
    try:
        index, reviewer = config_mod.find_reviewer(layer, args.selector)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    if args.provider:
        reviewer["provider"] = args.provider
    if args.role:
        reviewer["role"] = args.role
    if args.model or args.pin:
        model: Dict[str, Any] = {"family": args.model or reviewer.get("model", {}).get("family")}
        model["version"] = "pinned" if args.pin else "latest"
        if args.pin:
            model["id"] = args.pin
        reviewer["model"] = model
    if args.id:
        reviewer["id"] = args.id
    layer["reviewers"][index] = reviewer
    problems = [
        p
        for p in config_mod.validate(config_mod.deep_merge(config_mod.default_config(), layer))
        if p.startswith("reviewers")
    ]
    if problems:
        for problem in problems:
            _err(problem)
        return 2
    config_mod.write_config_file(path, layer)
    _out("Updated reviewer %s in %s" % (reviewer.get("id"), path))
    return 0


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_mod.collect(args.cwd, probe_models=not args.fast)
    if args.json:
        _emit_json(report)
    else:
        _out(doctor_mod.render(report))
    return doctor_mod.exit_code(report) if args.strict else 0


# --------------------------------------------------------------------------- run


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        if args.prompt_file == "-":
            return sys.stdin.read()
        return ws.read_text(args.prompt_file)
    if args.prompt:
        return args.prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("no prompt supplied: use --prompt, --prompt-file, or pipe one in")


def cmd_run(args: argparse.Namespace) -> int:
    loaded = _load_or_die(args.cwd)
    role = args.role
    try:
        spec = loaded.role(role) if role in config_mod.KNOWN_ROLES else _reviewer_spec(loaded, role)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2

    mode = args.mode or DEFAULT_MODES.get(role, MODE_REVIEW)
    workspace = _workspace(args)
    provider_name = str(spec.get("provider"))
    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        _err(str(exc))
        return 2

    if args.print_command:
        try:
            resolved = provider.resolve_model(spec.get("model"))
        except ModelResolutionError as exc:
            _err(str(exc))
            return 2
        _out(
            " ".join(
                provider.build_command(mode, resolved, workspace.root, args.extra or [], spec.get("options"))
            )
        )
        return 0

    prompt = _read_prompt(args)
    settings = loaded.review_settings()
    timeout = args.timeout or int(settings.get("timeout_seconds", 1800))
    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = settings.get("idle_timeout_seconds")

    book = _ledger(args, workspace)
    book.clear_stalls()
    refusal = _refuse_if_exhausted(book, role, args.force)
    if refusal is not None:
        return refusal
    try:
        if not args.job_file:
            book.consume(role, force=args.force)
    except ledger_mod.BudgetExhausted as exc:
        _err(str(exc))
        return ledger_mod.EXIT_BUDGET_EXHAUSTED

    if args.detach:
        # Hand the work to a detached worker so this call cannot block. The
        # budget was already consumed above, so the worker must not do it again.
        passthrough = _detached_argv(args, role)
        job = jobs_mod.start(workspace, role, passthrough, prompt=prompt, timeout=timeout)
        if args.json:
            _emit_json(job)
        else:
            _out("started %s as job %s" % (role, job["id"]))
            _out("follow it with: dev-orchestra jobs wait %s" % job["id"])
        return 0 if job.get("status") != "failed" else 1

    # Written down before the call, so a stall is visible from outside this
    # process and survives it dying.
    if args.job_file:
        jobs_mod.claim(args.job_file)
    token = book.begin(
        role,
        {"mode": mode, "provider": provider_name, "command": provider.executable, "job": args.job_file},
        deadline=timeout,
    )
    try:
        result = provider.run(
            prompt,
            mode,
            workspace.root,
            spec.get("model"),
            timeout=timeout,
            extra_args=args.extra or [],
            options=spec.get("options"),
            idle_timeout=idle_timeout,
        )
    except ModelResolutionError as exc:
        book.end(token, "failed", {"error": str(exc)})
        _err(str(exc))
        return 2

    if args.job_file:
        jobs_mod.finish(
            args.job_file,
            "succeeded" if result.ok else "failed",
            output=result.stdout,
            error="" if result.ok else (result.stderr or "").strip()[:2000],
            detail={
                "exit_code": result.exit_code,
                "stalled": result.stalled,
                "timed_out": result.timed_out,
                "duration_seconds": round(result.duration, 2),
                "model": result.resolved.display if result.resolved else None,
            },
        )
    if args.output:
        ws.write_text(args.output, result.stdout)
    elif not args.job_file:
        _out(result.stdout)
    if result.stalled:
        _err(
            "%s produced no output for %.0fs and was treated as stalled (not merely slow)."
            % (role, result.idle_for)
        )
    elif result.timed_out:
        _err("%s hit its %ss deadline and was killed." % (role, timeout))
    elif not result.ok:
        _err("%s failed (exit %s): %s" % (role, result.exit_code, result.stderr.strip()[:500]))
    if result.orphans_possible:
        _err("warning: %s's process group may have left orphans; check for stray processes." % role)

    # Recorded whatever the outcome: a failed run still spent what it spent.
    book.record_usage(role, result.usage.to_dict())
    book.end(
        token,
        "ok" if result.ok else ("stalled" if result.stalled else "failed"),
        {
            "mode": mode,
            "provider": provider_name,
            "model": result.resolved.display if result.resolved else None,
            "model_source": result.resolved.source if result.resolved else None,
            "duration_seconds": round(result.duration, 2),
            "timed_out": result.timed_out,
            "stalled": result.stalled,
            "output": args.output,
            "billed_tokens": result.usage.billed_tokens,
        },
    )
    return 0 if result.ok else 1


def _reviewer_spec(loaded: config_mod.LoadedConfig, selector: str) -> Dict[str, Any]:
    _, reviewer = config_mod.find_reviewer(loaded.data, selector)
    return reviewer


# --------------------------------------------------------------------------- review


def _ledger(args: argparse.Namespace, workspace: Optional[ws.Workspace] = None) -> ledger_mod.Ledger:
    workspace = workspace or _workspace(args)
    loaded = config_mod.load(getattr(args, "cwd", None), validate_result=False)
    return ledger_mod.Ledger(workspace, ledger_mod.budget_settings(loaded.data))


def _refuse_if_exhausted(book: ledger_mod.Ledger, stage: str, force: bool) -> Optional[int]:
    """Stop a loop at the action, not with advice from a query command."""
    if force:
        return None
    reasons = book.check(stage)
    if not reasons:
        return None
    _err("refusing to run %s:" % stage)
    for reason in reasons:
        _err("  - %s" % reason)
    _err("Report what is unresolved instead of retrying, or pass --force to override.")
    return ledger_mod.EXIT_BUDGET_EXHAUSTED


def _iteration(args: argparse.Namespace, workspace: ws.Workspace) -> int:
    """An explicit --iteration wins; otherwise derive it from what is on disk."""
    given = getattr(args, "iteration", None)
    return int(given) if given is not None else review_mod.next_iteration(workspace)


def _merge_runs(workspace: ws.Workspace, run_dicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep reviewers that did not run this time in the reviewer table."""
    previous = (ws.read_json(workspace.consolidated_json_path, {}) or {}).get("reviewers", [])
    fresh = {entry.get("id") for entry in run_dicts}
    kept = [
        dict(entry, status=entry.get("status", "ok")) for entry in previous if entry.get("id") not in fresh
    ]
    return run_dicts + kept


def cmd_review_snapshot(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    exclude = () if args.no_exclude else loaded.review_settings().get("exclude")
    try:
        meta = review_mod.create_snapshot(
            workspace,
            args.base,
            include_untracked=not args.no_untracked,
            exclude=exclude,
        )
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    if args.json:
        _emit_json(meta)
        return 0
    _out("Snapshot: %s" % workspace.relative(workspace.snapshot_path))
    _out("  strategy: %s" % meta["strategy"])
    _out("  files:    %d" % len(meta["files"]))
    _out("  size:     %d bytes (sha256 %s)" % (meta["bytes"], meta["sha256"][:12]))
    withheld = meta.get("withheld") or []
    if withheld:
        # Named, not merely counted: an exclusion nobody can see is an
        # exclusion nobody can correct.
        lines = review_mod.withheld_lines(withheld)
        _out("  withheld: %d file(s), %s changed line(s) not sent to reviewers" % (len(withheld), lines))
        for entry in withheld:
            _out("    %s (%s)" % (entry["path"], entry["pattern"]))
        _out("    reviewers are told these changed; --no-exclude sends them in full")
    if meta["empty"]:
        if withheld:
            _out("  WARNING: every changed file was withheld -- re-run with --no-exclude to review them.")
        else:
            _out("  WARNING: the snapshot is empty -- there is nothing to review.")
        return 1
    return 0


def cmd_review_run(args: argparse.Namespace) -> int:
    loaded = _load_or_die(args.cwd)
    workspace = _workspace(args)
    configured = loaded.reviewers()
    reviewers = configured
    if args.only:
        wanted = set(args.only)
        reviewers = [r for r in configured if r.get("id") in wanted or r.get("role") in wanted]
    if not reviewers and not args.only:
        _out("No reviewers configured -- skipping the independent-review stage.")
        data = review_mod.build_consolidation(workspace, [], [], _iteration(args, workspace))
        ws.write_json(workspace.consolidated_json_path, data)
        ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
        return 0
    if not reviewers:
        _err("--only %s matched no configured reviewer" % " ".join(args.only))
        return 2

    settings = loaded.review_settings()
    if not os.path.isfile(workspace.snapshot_path):
        try:
            review_mod.create_snapshot(workspace, args.base, exclude=settings.get("exclude"))
        except review_mod.ReviewError as exc:
            _err(str(exc))
            return 2
    iteration = _iteration(args, workspace)
    max_iterations = int(settings.get("max_review_iterations", 2))
    if iteration > max_iterations and not args.force:
        _err(
            "refusing to run review round %d: the budget is %d rounds "
            "(review.max_review_iterations)." % (iteration, max_iterations)
        )
        _err("Report the remaining findings instead of looping, or pass --force to override.")
        return ledger_mod.EXIT_BUDGET_EXHAUSTED

    book = _ledger(args, workspace)
    book.clear_stalls()
    batch_timeout = args.timeout or int(settings.get("timeout_seconds", 1800))
    token = book.begin(
        "review",
        {"iteration": iteration, "reviewers": [str(r.get("id")) for r in reviewers]},
        deadline=batch_timeout,
    )

    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        idle_timeout = settings.get("idle_timeout_seconds")
    try:
        runs = review_mod.run_reviews(
            reviewers,
            workspace,
            parallel=not args.sequential and bool(settings.get("parallel", True)),
            timeout=batch_timeout,
            extra_context=args.context or "",
            idle_timeout=idle_timeout,
        )
    except review_mod.ReviewError as exc:
        book.end(token, "failed", {"error": str(exc)})
        _err(str(exc))
        return 2

    for run in runs:
        book.record_usage("review", run.usage.to_dict(), label=str(run.reviewer.get("id") or "reviewer"))
    run_dicts = [run.to_dict() for run in runs]
    # Consolidate from every configured reviewer's report, not only the ones
    # that just ran: with --only that would otherwise overwrite the report with
    # a subset and discard the other reviewers' findings and triage. Reports
    # from an earlier snapshot are skipped rather than mixed in.
    stamp = review_mod.current_snapshot_stamp(workspace)
    findings, stale = review_mod.read_reports(workspace, [str(r.get("id")) for r in configured], stamp)
    data = review_mod.build_consolidation(workspace, _merge_runs(workspace, run_dicts), findings, iteration)
    ws.write_json(workspace.consolidated_json_path, data)
    ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
    repeats = book.register_signature("review", review_mod.findings_signature(data))
    book.end(
        token,
        "ok",
        {
            "iteration": iteration,
            "reviewers": run_dicts,
            "findings": data["counts"].get("findings_total"),
            "identical_rounds": repeats,
        },
    )
    if repeats > 1:
        _err(
            "note: round %d produced the same findings as the previous round -- "
            "the last fix changed nothing that the reviewers can see." % iteration
        )
    for reviewer_id in stale:
        _err("note: %s has no report for this snapshot; its earlier report was ignored" % reviewer_id)

    ok, failed = review_mod.summarise_runs(runs)
    if args.json:
        _emit_json({"ok": ok, "failed": failed, "reviewers": run_dicts, "counts": data["counts"]})
    else:
        for run in runs:
            status = "ok" if run.status == "ok" else "FAILED"
            _out(
                "%-6s %-18s %-8s %-18s %s"
                % (
                    status,
                    run.reviewer.get("id"),
                    run.reviewer.get("provider"),
                    run.model_display or "?",
                    run.error or "%d finding(s)" % run.findings,
                )
            )
        _out("")
        _out("%d successful, %d failed" % (ok, failed))
        _out("Consolidated: %s" % workspace.relative(workspace.consolidated_md_path))
    if ok == 0 and failed:
        return 1
    return 0


def cmd_review_consolidate(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    reviewer_ids = [str(r.get("id")) for r in loaded.reviewers()]
    stamp = review_mod.current_snapshot_stamp(workspace)
    findings, stale = review_mod.read_reports(workspace, reviewer_ids, stamp)
    previous = ws.read_json(workspace.consolidated_json_path, {}) or {}
    data = review_mod.build_consolidation(
        workspace, previous.get("reviewers", []), findings, _iteration(args, workspace)
    )
    for reviewer_id in stale:
        _err("note: %s's report predates the current snapshot and was ignored" % reviewer_id)
    ws.write_json(workspace.consolidated_json_path, data)
    ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
    if args.json:
        _emit_json(data)
    else:
        _out(review_mod.render_consolidation(data))
    return 0


def cmd_review_show(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not data:
        _err("no consolidated review found -- run `review run` first")
        return 2
    if args.accepted:
        data = dict(data, findings=review_mod.accepted_findings(data))
    if args.json:
        _emit_json(data)
    else:
        _out(review_mod.render_consolidation(data))
    return 0


def cmd_review_triage(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    if not data:
        _err("no consolidated review found -- run `review run` first")
        return 2
    try:
        for finding_id in args.ids:
            review_mod.set_triage(data, finding_id, args.status, args.note or "")
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    ws.write_json(workspace.consolidated_json_path, data)
    ws.write_text(workspace.consolidated_md_path, review_mod.render_consolidation(data))
    _out("Triaged %s as %s" % (", ".join(args.ids), args.status))
    return 0


def cmd_review_fix_brief(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    brief = review_mod.render_fix_brief(data)
    if args.output:
        ws.write_text(args.output, brief)
        _out("Wrote %s" % args.output)
    else:
        _out(brief)
    return 0


def cmd_review_status(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    settings = loaded.review_settings()
    severities = tuple(settings.get("re_review_severities") or ("critical", "high"))
    blocking = review_mod.unresolved_blocking(data, severities)
    iteration = int(data.get("iteration", 0) or 0)
    max_iterations = int(settings.get("max_review_iterations", 2))
    payload = {
        "iteration": iteration,
        "max_review_iterations": max_iterations,
        "blocking": [f["id"] for f in blocking],
        "blocking_count": len(blocking),
        "accepted_count": len(review_mod.accepted_findings(data)),
        "re_review_recommended": bool(blocking) and iteration < max_iterations,
        "iteration_budget_exhausted": iteration >= max_iterations,
        "counts": data.get("counts", {}),
    }
    if args.json:
        _emit_json(payload)
    else:
        _out("iteration %d/%d" % (iteration, max_iterations))
        _out("accepted findings: %d" % payload["accepted_count"])
        _out("blocking (%s): %d %s" % ("/".join(severities), len(blocking), ", ".join(payload["blocking"])))
        _out("re-review recommended: %s" % ("yes" if payload["re_review_recommended"] else "no"))
        if payload["iteration_budget_exhausted"] and blocking:
            _out("iteration budget exhausted -- report the remaining findings instead of looping")
    return 0


# --------------------------------------------------------------------------- state


def _detached_argv(args: argparse.Namespace, role: str) -> List[str]:
    """Rebuild this invocation for the worker, prompt now coming from a file."""
    argv = ["run", role, "--prompt-file", "-", "--force"]
    if args.mode:
        argv += ["--mode", args.mode]
    if args.output:
        argv += ["--output", os.path.abspath(args.output)]
    if args.timeout:
        argv += ["--timeout", str(args.timeout)]
    if args.idle_timeout is not None:
        argv += ["--idle-timeout", str(args.idle_timeout)]
    if args.extra:
        argv += ["--extra", *args.extra]
    return argv


def cmd_jobs_list(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    found = jobs_mod.list_jobs(workspace)
    if args.json:
        _emit_json(found)
        return 0
    if not found:
        _out("No jobs recorded.")
        return 0
    for job in found:
        _out(
            "%-34s %-10s %-14s %s"
            % (job.get("id"), job.get("status"), job.get("stage"), job.get("started_at"))
        )
    return 0


def cmd_jobs_show(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    job = jobs_mod.read_job(workspace, args.job_id)
    if job is None:
        _err("no such job: %s" % args.job_id)
        return 2
    if args.json:
        _emit_json(job)
        return 0
    _out(jobs_mod.render(job))
    if args.output and job.get("output_file"):
        _out("")
        _out(ws.read_text(str(job["output_file"])))
    return 0


def cmd_jobs_wait(args: argparse.Namespace) -> int:
    """Bounded wait: returning while the job runs is an outcome, not an error."""
    workspace = _workspace(args)
    if jobs_mod.read_job(workspace, args.job_id) is None:
        _err("no such job: %s" % args.job_id)
        return 2
    job = jobs_mod.wait(workspace, args.job_id, timeout=args.timeout, poll=args.poll)
    if args.json:
        _emit_json(job)
    else:
        _out(jobs_mod.render(job))
        if job.get("status") == "succeeded" and job.get("output_file"):
            _out("")
            _out(ws.read_text(str(job["output_file"])))
    if job.get("waited_out"):
        return 4
    return 0 if job.get("status") == "succeeded" else 1


def cmd_jobs_cancel(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    try:
        job = jobs_mod.cancel(workspace, args.job_id)
    except KeyError:
        _err("no such job: %s" % args.job_id)
        return 2
    _out(jobs_mod.render(job))
    return 0


def cmd_budget_show(args: argparse.Namespace) -> int:
    book = _ledger(args)
    summary = book.summary()
    if args.json:
        _emit_json(summary)
        return 0
    _out("Workflow started: %s" % summary["started_at"])
    for stage, entry in summary["budgets"].items():
        _out("  %-14s %d/%d used" % (stage, entry["used"], entry["limit"]))
    total = summary["total_delegated_runs"]
    _out("  %-14s %s/%s used" % ("delegated runs", total["used"], total["limit"]))
    if summary["runtime_remaining_seconds"] is not None:
        _out("  %-14s %ss left" % ("runtime", summary["runtime_remaining_seconds"]))
    for stage, repeats in (summary["signatures"] or {}).items():
        if repeats and int(repeats) > 1:
            _out("  %-14s same outcome %s times in a row" % (stage, repeats))
    return 0


def cmd_budget_consume(args: argparse.Namespace) -> int:
    """Claim an attempt at a stage the orchestrator runs itself, such as tests."""
    book = _ledger(args)
    try:
        book.consume(args.stage, force=args.force)
    except ledger_mod.BudgetExhausted as exc:
        _err("refusing another %s attempt: %s" % (args.stage, exc))
        _err("Report what is still failing instead of retrying.")
        return ledger_mod.EXIT_BUDGET_EXHAUSTED
    remaining = book.remaining(args.stage)
    suffix = "" if remaining is None else " (%d left)" % remaining
    _out("%s attempt recorded%s" % (args.stage, suffix))
    return 0


def cmd_budget_reset(args: argparse.Namespace) -> int:
    _ledger(args).reset()
    _out("Budgets reset; this is now a fresh workflow.")
    return 0


_TOKEN_ROW = "  %-18s %5s %9s %9s %9s %9s %9s"


def _token_row(name: str, account: Dict[str, Any]) -> str:
    def num(field: str) -> str:
        value = int(account.get(field) or 0)
        return "{:,}".format(value) if value else "-"

    runs = "%s/%s" % (account.get("measured_runs") or 0, account.get("runs") or 0)
    cost = float(account.get("cost_usd") or 0.0)
    return _TOKEN_ROW % (
        name,
        runs,
        num("input_tokens"),
        num("output_tokens"),
        num("total_tokens"),
        num("billed_tokens"),
        ("$%.4f" % cost) if cost else "-",
    )


def cmd_tokens_show(args: argparse.Namespace) -> int:
    """What the workflow has spent. Accounting only -- it refuses nothing."""
    book = _ledger(args)
    report = book.token_report()
    if args.json:
        _emit_json(report)
        return 0

    totals = report["totals"]
    if not totals["runs"]:
        _out("No delegated runs recorded yet.")
        return 0

    _out(_TOKEN_ROW % ("stage", "meas.", "input", "output", "total", "billed", "cost"))
    for stage, account in sorted(report["by_stage"].items()):
        _out(_token_row(stage, account))
    _out(_token_row("ALL", totals))
    if report["by_label"]:
        _out("")
        _out("Per reviewer:")
        for label, account in sorted(report["by_label"].items()):
            _out(_token_row(label, account))

    _out("")
    chars = int(totals.get("prompt_chars") or 0)
    if chars:
        _out(
            "Prompt text this repo composed: %s chars over %d run(s). "
            "That is the part it can shorten." % ("{:,}".format(chars), totals["runs"])
        )
    if not report["complete"]:
        silent = int(totals["runs"]) - int(totals["measured_runs"])
        _out(
            "%d of %d run(s) reported no usage, so every total above is a floor, "
            "not a total." % (silent, totals["runs"])
        )
    return 0


def cmd_progress_record(args: argparse.Namespace) -> int:
    """Record a stage outcome so a loop that achieves nothing can be stopped."""
    book = _ledger(args)
    repeats = book.register_signature(args.stage, args.signature)
    allowed = int(book.settings.get("max_repeats_without_progress") or 0)
    stop = bool(allowed and repeats >= allowed)
    if args.json:
        _emit_json({"stage": args.stage, "repeats": repeats, "stop": stop})
    elif stop:
        _out(
            "%s has produced the same outcome %d times: stop and report, since "
            "retrying is not making progress." % (args.stage, repeats)
        )
    else:
        _out("%s outcome recorded (seen %d time(s))." % (args.stage, repeats))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """One verdict the orchestrator can act on: continue, or stop and report."""
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    book = _ledger(args, workspace)
    abandoned = book.clear_stalls()

    review_data = ws.read_json(workspace.consolidated_json_path, {}) or {}
    settings = loaded.review_settings()
    severities = tuple(settings.get("re_review_severities") or ("critical", "high"))
    blocking = review_mod.unresolved_blocking(review_data, severities)
    iteration = int(review_data.get("iteration", 0) or 0)
    max_iterations = int(settings.get("max_review_iterations", 2))
    summary = book.summary()

    reasons: List[str] = []
    if blocking and iteration >= max_iterations:
        reasons.append(
            "review budget spent (%d/%d rounds) with %d finding(s) still open"
            % (iteration, max_iterations, len(blocking))
        )
    if int((summary["signatures"] or {}).get("review") or 0) > 1:
        reasons.append("the last review round found exactly what the previous one found")
    for stage, entry in summary["budgets"].items():
        if entry["remaining"] == 0:
            reasons.append("%s has no attempts left" % stage)
    total = summary["total_delegated_runs"]
    if total["limit"] and total["used"] >= int(total["limit"]):
        reasons.append("no delegated runs left in this workflow")
    if summary["runtime_remaining_seconds"] == 0:
        reasons.append("the workflow runtime budget is spent")

    payload = {
        "verdict": "stop-and-report" if reasons else "continue",
        "reasons": reasons,
        "stalls": summary["stalls"],
        "abandoned_stages": abandoned,
        "in_flight": summary["in_flight"],
        "review": {
            "iteration": iteration,
            "max_review_iterations": max_iterations,
            "blocking": [f["id"] for f in blocking],
            "accepted": len(review_mod.accepted_findings(review_data)),
        },
        "budgets": summary["budgets"],
        "total_delegated_runs": total,
        "runtime_remaining_seconds": summary["runtime_remaining_seconds"],
        # Reported, never enforced: no verdict here turns on what a run cost.
        "tokens": summary["tokens"],
    }
    if args.json:
        _emit_json(payload)
        return 0

    _out("Verdict: %s" % payload["verdict"].upper())
    for reason in reasons:
        _out("  - %s" % reason)
    if summary["stalls"]:
        _out("")
        _out("Stalled stages:")
        for stall in summary["stalls"]:
            _out(
                "  %s started %s (%.0fs ago) -- %s"
                % (stall["stage"], stall["started_at"], stall["elapsed_seconds"], stall["reason"])
            )
    if abandoned:
        _out("")
        _out("Cleared %d stage(s) whose process is gone: %s" % (len(abandoned), ", ".join(abandoned)))
    if summary["in_flight"]:
        _out("")
        _out("In flight:")
        for token, entry in summary["in_flight"].items():
            _out("  %s (%s) since %s" % (entry.get("stage"), token, entry.get("started_at")))
    _out("")
    _out(
        "Review: round %d/%d, %d accepted, %d blocking"
        % (iteration, max_iterations, payload["review"]["accepted"], len(blocking))
    )
    tokens = summary["tokens"]["totals"]
    if tokens["runs"]:
        _out(
            "Tokens: %s billed over %d run(s)%s (dev-orchestra tokens show)"
            % (
                "{:,}".format(int(tokens["billed_tokens"] or 0)) or "0",
                tokens["runs"],
                "" if summary["tokens"]["complete"] else ", partially reported",
            )
        )
    return 0


def cmd_state_show(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    state = workspace.read_state()
    if args.json:
        _emit_json(state)
        return 0
    events = state.get("events", [])
    if not events:
        _out("No recorded stages yet.")
        return 0
    for event in events:
        _out(
            "%-14s %-8s %s %s"
            % (event.get("stage"), event.get("status"), event.get("at"), event.get("model") or "")
        )
    return 0


def cmd_state_record(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    detail: Dict[str, Any] = {}
    for item in args.detail or []:
        key, _, value = item.partition("=")
        detail[key] = config_mod.coerce_scalar(value)
    workspace.record_event(args.stage, args.status, detail)
    _out("recorded %s=%s" % (args.stage, args.status))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    loaded = config_mod.load(args.cwd, validate_result=False)
    workspace = _workspace(args)
    state = workspace.read_state()
    review_data = ws.read_json(workspace.consolidated_json_path, {}) or {}

    lines = ["Workflow:"]
    seen: Dict[str, str] = {}
    for event in state.get("events", []):
        seen[str(event.get("stage"))] = str(event.get("status"))
    for stage in ("architect", "implementer", "test", "review", "review_fixer", "re-test"):
        if stage in seen:
            lines.append("  %-14s %s" % (stage, "OK" if seen[stage] == "ok" else seen[stage].upper()))
    counts = review_data.get("counts", {})
    if counts:
        lines.append(
            "  %-14s %s/%s ok" % ("reviews", counts.get("reviewers_ok"), counts.get("reviewers_total"))
        )
    lines.append("")
    lines.append("Models:")
    for key, title in wizard_mod.ROLE_TITLES:
        spec = loaded.data.get(key) or {}
        model = spec.get("model") or {}
        lines.append(
            "  %-14s %s / %s / %s"
            % (title, spec.get("provider"), model.get("family", "default"), model.get("version", "latest"))
        )
    lines.append("Review:")
    # Prefer the models actually resolved during the run; fall back to config.
    for reviewer in review_data.get("reviewers") or loaded.reviewers():
        model = reviewer.get("model")
        if isinstance(model, dict):
            model = model.get("family", "default")
        lines.append(
            "  %-14s %s / %s%s"
            % (
                reviewer.get("id"),
                reviewer.get("provider"),
                model or "default",
                "" if reviewer.get("status", "ok") == "ok" else " (FAILED)",
            )
        )
    book = _ledger(args, workspace)
    report = book.token_report()
    if report["totals"]["runs"]:
        lines.append("")
        lines.append("Tokens:")
        for stage, account in sorted(report["by_stage"].items()):
            lines.append("  %-14s %s billed" % (stage, "{:,}".format(int(account.get("billed_tokens") or 0))))
        total = report["totals"]
        cost = float(total.get("cost_usd") or 0.0)
        lines.append(
            "  %-14s %s billed over %d run(s)%s%s"
            % (
                "total",
                "{:,}".format(int(total.get("billed_tokens") or 0)),
                total["runs"],
                (" -- $%.4f" % cost) if cost else "",
                "" if report["complete"] else " (partially reported: a floor)",
            )
        )
    if args.json:
        _emit_json({"stages": seen, "counts": counts, "tokens": report})
        return 0
    _out("\n".join(lines))
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev-orchestra",
        description="Configuration, diagnostics and review plumbing for the"
        " AI Development Orchestrator skill.",
    )
    parser.add_argument("--version", action="version", version="dev-orchestra %s" % __version__)
    parser.add_argument("--cwd", default=None, help="operate as if run from this directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # config -----------------------------------------------------------------
    config_parser = subparsers.add_parser("config", help="show and edit configuration")
    config_sub = config_parser.add_subparsers(dest="subcommand", required=True)

    show = config_sub.add_parser("show", help="show the effective configuration")
    show.add_argument("--scope", choices=["global", "project", "effective"], default="effective")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_config_show)

    path_parser = config_sub.add_parser("path", help="print config file locations")
    path_parser.set_defaults(func=cmd_config_path)

    setup = config_sub.add_parser("setup", help="run the setup wizard")
    setup.add_argument("--scope", choices=["global", "project"], default="global")
    setup.add_argument(
        "--defaults", action="store_true", help="write the recommended config without prompting"
    )
    setup.add_argument("--force", action="store_true", help="prompt even without a TTY")
    setup.set_defaults(func=cmd_config_setup)

    reset = config_sub.add_parser("reset", help="restore recommended defaults")
    reset.add_argument("--scope", choices=["global", "project"], default=None)
    reset.add_argument("--delete", action="store_true", help="delete the config file instead of rewriting it")
    reset.set_defaults(func=cmd_config_reset)

    set_parser = config_sub.add_parser("set", help="set one value, e.g. implementer.model.family opus")
    set_parser.add_argument("path")
    set_parser.add_argument("value")
    set_parser.add_argument("--scope", choices=["global", "project"], default=None)
    set_parser.add_argument("--raw", action="store_true", help="keep the value as a string")
    set_parser.set_defaults(func=cmd_config_set)

    validate = config_sub.add_parser("validate", help="validate the effective configuration")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_config_validate)

    # model ------------------------------------------------------------------
    model_parser = subparsers.add_parser("model", help="inspect available models")
    model_sub = model_parser.add_subparsers(dest="subcommand", required=True)
    model_list = model_sub.add_parser("list", help="list models the installed CLIs advertise")
    model_list.add_argument("--provider", choices=available_providers(), default=None)
    model_list.add_argument("--json", action="store_true")
    model_list.set_defaults(func=cmd_model_list)

    # reviewer ---------------------------------------------------------------
    reviewer_parser = subparsers.add_parser("reviewer", help="manage the review panel")
    reviewer_sub = reviewer_parser.add_subparsers(dest="subcommand", required=True)

    r_list = reviewer_sub.add_parser("list", help="list configured reviewers")
    r_list.add_argument("--json", action="store_true")
    r_list.set_defaults(func=cmd_reviewer_list)

    r_add = reviewer_sub.add_parser("add", help="add a reviewer")
    r_add.add_argument("--provider", required=True, choices=available_providers())
    r_add.add_argument("--model", default=None, help="model family (default: provider's recommended)")
    r_add.add_argument("--role", default="general")
    r_add.add_argument("--id", default=None)
    r_add.add_argument("--pin", default=None, help="pin an exact model id instead of tracking latest")
    r_add.add_argument("--scope", choices=["global", "project"], default=None)
    r_add.set_defaults(func=cmd_reviewer_add)

    r_remove = reviewer_sub.add_parser("remove", help="remove a reviewer by id, role, or position")
    r_remove.add_argument("selector")
    r_remove.add_argument("--scope", choices=["global", "project"], default=None)
    r_remove.set_defaults(func=cmd_reviewer_remove)

    r_set = reviewer_sub.add_parser("set", help="change an existing reviewer")
    r_set.add_argument("selector")
    r_set.add_argument("--provider", choices=available_providers(), default=None)
    r_set.add_argument("--model", default=None)
    r_set.add_argument("--role", default=None)
    r_set.add_argument("--id", default=None)
    r_set.add_argument("--pin", default=None)
    r_set.add_argument("--scope", choices=["global", "project"], default=None)
    r_set.set_defaults(func=cmd_reviewer_set)

    # doctor -----------------------------------------------------------------
    doctor_parser = subparsers.add_parser("doctor", help="diagnose CLIs, auth and configuration")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--fast", action="store_true", help="skip model discovery")
    doctor_parser.add_argument("--strict", action="store_true", help="exit non-zero when problems are found")
    doctor_parser.set_defaults(func=cmd_doctor)

    # run --------------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="run one configured role against a prompt")
    run_parser.add_argument(
        "role", help="orchestrator | architect | implementer | review_fixer | <reviewer id>"
    )
    run_parser.add_argument("--prompt", default=None)
    run_parser.add_argument("--prompt-file", default=None, help="path, or - for stdin")
    run_parser.add_argument("--mode", choices=list(MODES), default=None)
    run_parser.add_argument("--output", default=None, help="write the response here instead of stdout")
    run_parser.add_argument("--timeout", type=int, default=None, help="total deadline in seconds")
    run_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="treat as stalled after this long with no output (streaming providers only)",
    )
    run_parser.add_argument("--force", action="store_true", help="run even though a budget is spent")
    run_parser.add_argument(
        "--detach",
        action="store_true",
        help="start the run in its own process and return a job id immediately",
    )
    run_parser.add_argument("--json", action="store_true", help="machine-readable output")
    run_parser.add_argument("--job-file", default=None, help=argparse.SUPPRESS)
    run_parser.add_argument("--print-command", action="store_true", help="print the CLI invocation and exit")
    run_parser.add_argument("--extra", nargs=argparse.REMAINDER, help="extra args passed to the provider CLI")
    run_parser.set_defaults(func=cmd_run)

    # review -----------------------------------------------------------------
    review_parser = subparsers.add_parser("review", help="independent multi-model review pipeline")
    review_sub = review_parser.add_subparsers(dest="subcommand", required=True)

    snapshot = review_sub.add_parser("snapshot", help="freeze the change under review")
    snapshot.add_argument("--base", default=None, help="revision to diff against (default: HEAD)")
    snapshot.add_argument("--no-untracked", action="store_true")
    snapshot.add_argument(
        "--no-exclude",
        action="store_true",
        help="send every changed file, including generated and vendored ones",
    )
    snapshot.add_argument("--json", action="store_true")
    snapshot.set_defaults(func=cmd_review_snapshot)

    review_run = review_sub.add_parser("run", help="run every reviewer against the frozen snapshot")
    review_run.add_argument(
        "--iteration",
        type=int,
        default=None,
        help="review round; derived from the snapshot when omitted",
    )
    review_run.add_argument("--sequential", action="store_true")
    review_run.add_argument("--only", nargs="*", default=None, help="reviewer ids or roles to run")
    review_run.add_argument("--context", default=None, help="extra context for reviewers")
    review_run.add_argument("--base", default=None)
    review_run.add_argument("--timeout", type=int, default=None)
    review_run.add_argument("--idle-timeout", type=float, default=None)
    review_run.add_argument("--force", action="store_true", help="run even past the review iteration budget")
    review_run.add_argument("--json", action="store_true")
    review_run.set_defaults(func=cmd_review_run)

    consolidate = review_sub.add_parser("consolidate", help="re-parse reports and dedupe findings")
    consolidate.add_argument("--iteration", type=int, default=None)
    consolidate.add_argument("--json", action="store_true")
    consolidate.set_defaults(func=cmd_review_consolidate)

    review_show = review_sub.add_parser("show", help="show the consolidated review")
    review_show.add_argument("--accepted", action="store_true", help="only accepted findings")
    review_show.add_argument("--json", action="store_true")
    review_show.set_defaults(func=cmd_review_show)

    triage = review_sub.add_parser("triage", help="record a triage decision for findings")
    triage.add_argument("ids", nargs="+")
    triage.add_argument("--status", required=True, choices=list(review_mod.TRIAGE_STATUSES))
    triage.add_argument("--note", default=None)
    triage.set_defaults(func=cmd_review_triage)

    fix_brief = review_sub.add_parser("fix-brief", help="emit the accepted-findings brief for the fixer")
    fix_brief.add_argument("--output", default=None)
    fix_brief.set_defaults(func=cmd_review_fix_brief)

    status = review_sub.add_parser("status", help="report whether a re-review is warranted")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_review_status)

    # state ------------------------------------------------------------------
    state_parser = subparsers.add_parser("state", help="inspect or append run state")
    state_sub = state_parser.add_subparsers(dest="subcommand", required=True)
    state_show = state_sub.add_parser("show")
    state_show.add_argument("--json", action="store_true")
    state_show.set_defaults(func=cmd_state_show)
    state_record = state_sub.add_parser("record")
    state_record.add_argument("stage")
    state_record.add_argument("status")
    state_record.add_argument("--detail", nargs="*", default=None, help="key=value pairs")
    state_record.set_defaults(func=cmd_state_record)

    jobs_parser = subparsers.add_parser("jobs", help="detached runs, so no call blocks forever")
    jobs_sub = jobs_parser.add_subparsers(dest="subcommand", required=True)
    jobs_list = jobs_sub.add_parser("list", help="every recorded job, newest first")
    jobs_list.add_argument("--json", action="store_true")
    jobs_list.set_defaults(func=cmd_jobs_list)
    jobs_show = jobs_sub.add_parser("show", help="one job")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--output", action="store_true", help="also print its output")
    jobs_show.add_argument("--json", action="store_true")
    jobs_show.set_defaults(func=cmd_jobs_show)
    jobs_wait = jobs_sub.add_parser("wait", help="wait for a job, with a deadline of your own")
    jobs_wait.add_argument("job_id")
    jobs_wait.add_argument("--timeout", type=float, default=60.0)
    jobs_wait.add_argument("--poll", type=float, default=1.0)
    jobs_wait.add_argument("--json", action="store_true")
    jobs_wait.set_defaults(func=cmd_jobs_wait)
    jobs_cancel = jobs_sub.add_parser("cancel", help="stop a running job")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.set_defaults(func=cmd_jobs_cancel)

    budget_parser = subparsers.add_parser("budget", help="attempt budgets that stop runaway loops")
    budget_sub = budget_parser.add_subparsers(dest="subcommand", required=True)
    budget_show = budget_sub.add_parser("show", help="what has been spent")
    budget_show.add_argument("--json", action="store_true")
    budget_show.set_defaults(func=cmd_budget_show)
    budget_consume = budget_sub.add_parser("consume", help="claim an attempt at a stage")
    budget_consume.add_argument("stage", help="a stage the orchestrator runs itself, e.g. test")
    budget_consume.add_argument("--force", action="store_true")
    budget_consume.set_defaults(func=cmd_budget_consume)
    budget_reset = budget_sub.add_parser("reset", help="start a fresh workflow")
    budget_reset.set_defaults(func=cmd_budget_reset)

    # Separate from `budget` on purpose: attempts are enforced, tokens are only
    # counted, and putting them under one command invites reading one as the
    # other.
    tokens_parser = subparsers.add_parser("tokens", help="what the workflow has spent, per stage")
    tokens_sub = tokens_parser.add_subparsers(dest="subcommand", required=True)
    tokens_show = tokens_sub.add_parser("show", help="the token account (reported, never enforced)")
    tokens_show.add_argument("--json", action="store_true")
    tokens_show.set_defaults(func=cmd_tokens_show)

    progress_parser = subparsers.add_parser("progress", help="detect a loop that is going nowhere")
    progress_sub = progress_parser.add_subparsers(dest="subcommand", required=True)
    progress_record = progress_sub.add_parser("record", help="record a stage outcome signature")
    progress_record.add_argument("stage")
    progress_record.add_argument("--signature", required=True, help="e.g. the failing test summary")
    progress_record.add_argument("--json", action="store_true")
    progress_record.set_defaults(func=cmd_progress_record)

    status_parser = subparsers.add_parser(
        "status", help="continue or stop: budgets, stalls and open findings in one verdict"
    )
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    summary = subparsers.add_parser("summary", help="print the end-of-run summary")
    summary.add_argument("--json", action="store_true")
    summary.set_defaults(func=cmd_summary)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.cwd:
        # Resolve before chdir and keep the absolute form: commands also use
        # args.cwd as a search-start path, and a relative value would otherwise
        # be applied a second time against the directory we just moved into.
        args.cwd = os.path.abspath(args.cwd)
        os.chdir(args.cwd)
    try:
        return int(args.func(args) or 0)
    except config_mod.ConfigError as exc:
        _err(str(exc))
        return 2
    except review_mod.ReviewError as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        _err("interrupted")
        return 130
