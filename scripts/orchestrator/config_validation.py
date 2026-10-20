"""Checking a configuration: ``validate``, and the keys a file holds that nothing reads.

``config`` re-exports its public names.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import optimization as opt_mod
from .clocks import MAX_SECONDS
from .config_common import (
    CONFIG_VERSION,
    KNOWN_ROLES,
    STALE_NOTICE_MAX_DAYS,
    default_config,
    is_valid_reviewer_id,
    normalise_language_tag,
    whole_seconds,
)
from .config_keys import get_path
from .config_paths import user_providers_hint
from .config_reviewers import CODE_PANEL, DESIGN_PANEL, PanelKeys, ReviewerOrigin
from .config_seats import merge_tier
from .review_common import SEVERITIES

# Two imports stay inside the functions that need them, and each one points
# back here. The provider registry: importing it runs its bootstrap, which asks
# ``config_paths`` for ``user_providers_dir`` and imports every user adapter, and
# configuration must stay loadable without the provider registry. Presets:
# ``presets`` imports ``config``, which imports this module, so it can only be
# imported once both are complete.


#: How a deadline too long to wait for is refused.
_TOO_LONG = "must be %d or less" % MAX_SECONDS

#: Tier names are typed on a command line and read in a report, so they are
#: kept to the shape of a word rather than allowed to be a sentence.
_TIER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _int_at_least(value: Any, minimum: int) -> bool:
    """An int, not a bool, and at least ``minimum``."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def validate(
    data: Dict[str, Any],
    known_providers: Optional[List[str]] = None,
    project_layer: Optional[Dict[str, Any]] = None,
    global_layer: Optional[Dict[str, Any]] = None,
    origins: Optional[Sequence[ReviewerOrigin]] = None,
    design_origins: Optional[Sequence[ReviewerOrigin]] = None,
) -> List[str]:
    """Return a list of human-readable problems; empty means valid.

    ``project_layer`` is the project file on its own, for the one rule merged
    data cannot express: only the global file can name a preset. It and
    ``global_layer`` also have their ``reviewers_extra`` checked, entry by
    entry, whether or not the extras made it into the panel. ``origins`` is
    parallel to the folded ``reviewers``: an extra's entry is checked there
    and not again here, while the panel-wide rules cover it.
    ``design_origins`` is the same for ``review.design.reviewers``, whose
    design extras are checked per file too.
    """
    # lazy: presets and the provider registry; see the note at the top of this module
    from . import presets
    from .providers import available_providers

    providers = known_providers if known_providers is not None else available_providers()
    problems = _validate_version_and_preset(data, project_layer, presets.NAMES)
    problems.extend(_validate_roles(data, providers))
    problems.extend(_validate_reviewers(data, providers, origins, global_layer, project_layer))
    problems.extend(_validate_design_panel(data, providers, design_origins, global_layer, project_layer))
    for key, check in (
        ("run", _validate_run),
        ("review", _validate_review),
        ("design", _validate_design),
        ("optimization", _validate_optimization),
        ("budgets", _validate_budgets),
        ("workspace", _validate_workspace),
        ("language", _validate_language),
    ):
        block = data.get(key)
        if block is not None:
            problems.extend(check(block))
    return problems


#: A mapping whose keys are the user's own: provider options, which the
#: adapter validates, tier names, budgets, and run timeouts, whose keys
#: ``validate`` already checks as roles.
_FREE = "free"


def _key_schema() -> Dict[str, Any]:
    """The keys a file may hold, as nested mappings; None is a leaf, ``_FREE`` any keys.

    A list of reviewer entries is ``[schema]``. Built from ``default_config``,
    so a key added there is known here without a second list to keep.
    """

    def shape(value: Any) -> Any:
        return {key: shape(item) for key, item in value.items()} if isinstance(value, dict) else None

    model = {"family": None, "version": None, "id": None}
    role = {"provider": None, "model": model, "options": _FREE, "model_tiers": _FREE}
    # when is a leaf: its keys are validate's to refuse. high_risk_model is a
    # model block; the provider and options validate refuses in it are known
    # here, so they are reported once.
    reviewer = {
        "id": None,
        "provider": None,
        "model": model,
        "options": _FREE,
        "role": None,
        "high_risk_model": dict(model, provider=None, options=None),
        "when": None,
        "relevance": None,
    }
    schema = shape(default_config())
    schema.update(dict.fromkeys(KNOWN_ROLES, role))
    schema.update({"preset": None, "reviewers": [reviewer], "reviewers_extra": [reviewer], "budgets": _FREE})
    schema["run"]["timeout_seconds"] = _FREE
    schema["review"]["design"].update({"reviewers": [reviewer], "reviewers_extra": [reviewer]})
    return schema


def unknown_keys(layer: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
    """Each key in ``layer`` nothing reads, dotted, with the known key it most resembles.

    A typo such as ``reveiw`` or ``review.timout_seconds`` is otherwise valid
    and silently ignored. Reported, never refused: a key a later release adds
    must not make an older one reject the file. Only mappings are walked; a
    value of the wrong type is ``validate``'s to report.
    """
    found: List[Tuple[str, Optional[str]]] = []

    def walk(node: Any, schema: Any, path: str) -> None:
        if isinstance(schema, list) and isinstance(node, list):
            for index, entry in enumerate(node):
                walk(entry, schema[0], "%s[%d]" % (path, index))
            return
        if not isinstance(schema, dict) or not isinstance(node, dict):
            return
        for key, value in node.items():
            name = "%s.%s" % (path, key) if path else str(key)
            if key in schema:
                walk(value, schema[key], name)
                continue
            close = difflib.get_close_matches(str(key), [str(known) for known in schema], n=1, cutoff=0.75)
            found.append((name, ("%s.%s" % (path, close[0]) if path else close[0]) if close else None))

    walk(layer, _key_schema(), "")
    return found


def unknown_key_warnings(global_layer: Dict[str, Any], project_layer: Dict[str, Any]) -> List[str]:
    """``unknown_keys`` of each file, one line apiece, naming the file."""
    warnings: List[str] = []
    for name, layer in (("global", global_layer), ("project", project_layer)):
        for key, close in unknown_keys(layer):
            hint = " (did you mean %s?)" % close if close else ""
            warnings.append("%s in the %s file: unknown key, ignored%s" % (key, name, hint))
    return warnings


def _validate_version_and_preset(
    data: Dict[str, Any], project_layer: Optional[Dict[str, Any]], preset_names: Sequence[str]
) -> List[str]:
    problems: List[str] = []
    version = data.get("version")
    if version != CONFIG_VERSION:
        problems.append("version must be %d (got %r)" % (CONFIG_VERSION, version))

    preset = data.get("preset")
    if preset is not None and not (isinstance(preset, str) and preset in preset_names):
        problems.append("preset: unknown %r (known: %s)" % (preset, ", ".join(preset_names)))
    if project_layer and project_layer.get("preset") is not None:
        problems.append("preset: only the global file can name a preset for now")
    return problems


def _validate_roles(data: Dict[str, Any], providers: List[str]) -> List[str]:
    problems: List[str] = []
    for role in KNOWN_ROLES:
        spec = data.get(role)
        if not isinstance(spec, dict):
            problems.append("%s: missing role definition" % role)
            continue
        problems.extend("%s: %s" % (role, msg) for msg in _validate_role(spec, providers))
        problems.extend(_validate_tiers(spec, role, providers))
    return problems


def _validate_reviewers(
    data: Dict[str, Any],
    providers: List[str],
    origins: Optional[Sequence[ReviewerOrigin]],
    global_layer: Optional[Dict[str, Any]],
    project_layer: Optional[Dict[str, Any]],
) -> List[str]:
    problems: List[str] = []
    reviewers = data.get("reviewers")
    if reviewers is None:
        reviewers = []
    if not isinstance(reviewers, list):
        problems.append("reviewers: must be a list (use [] for none)")
    else:
        problems.extend(_validate_panel_entries(reviewers, origins, providers, CODE_PANEL))
        problems.extend(_validate_conditions(reviewers, data.get("optimization"), origins))
    for name, layer in (("global", global_layer), ("project", project_layer)):
        if layer:
            problems.extend(_validate_extras_file(layer, name, providers))
    return problems


def _entry_identity(reviewer: Any, label: str, seen: Set[str], own: bool = True) -> Tuple[bool, List[str]]:
    """``(is_mapping, problems)``: an entry's shape and id, its id added to ``seen``.

    A duplicate id is always reported; a missing mapping or a malformed id
    only for an ``own`` entry.
    """
    if not isinstance(reviewer, dict):
        return False, ["%s: must be a mapping" % label] if own else []
    problems: List[str] = []
    rid = reviewer.get("id")
    if not is_valid_reviewer_id(rid):
        if own:
            problems.append("%s: id must match [a-z0-9][a-z0-9._-]* (got %r)" % (label, rid))
    elif rid in seen:
        problems.append("%s: duplicate reviewer id %r" % (label, rid))
    else:
        seen.add(rid)
    return True, problems


def _origin_label(origin: ReviewerOrigin, index: int) -> str:
    """How a problem names a panel entry: its file for an extra, else ``reviewers[i]``."""
    if origin.extra:
        return "%s[%d] in the %s file" % (origin.key, origin.position, origin.layer)
    if origin.design:
        return "%s[%d]" % (DESIGN_PANEL.reviewers, index)
    return "reviewers[%d]" % index


def _validate_panel_entries(
    reviewers: List[Any],
    origins: Optional[Sequence[ReviewerOrigin]],
    providers: List[str],
    keys: PanelKeys,
) -> List[str]:
    """Each entry of a composed panel: its shape, its id, and the entry itself.

    Every id counts toward the duplicates. Only the panel's own entries are
    checked further, by their origin: an extra is checked in its file, and a
    design panel's seat copied from the code panel was checked with the code
    panel. Without origins -- data that was never composed -- every entry is
    the panel's own, as a file wrote it.
    """
    design = keys == DESIGN_PANEL
    problems: List[str] = []
    seen: Set[str] = set()
    for index, reviewer in enumerate(reviewers):
        origin = origins[index] if origins is not None and index < len(origins) else None
        own = origin is None or (not origin.extra and origin.design == design)
        if origin is not None and origin.extra:
            label = _origin_label(origin, index)
        else:
            label = "%s[%d]" % (keys.reviewers, index)
        mapping, identity = _entry_identity(reviewer, label, seen, own)
        problems.extend(identity)
        if not mapping:
            continue
        if own:
            problems.extend(_validate_reviewer_entry(reviewer, label, label + ".", providers, design=design))
    return problems


def _validate_design_panel(
    data: Dict[str, Any],
    providers: List[str],
    origins: Optional[Sequence[ReviewerOrigin]],
    global_layer: Optional[Dict[str, Any]],
    project_layer: Optional[Dict[str, Any]],
) -> List[str]:
    """The design panel, when a file sets one: what ``_validate_reviewers`` checks of the code panel.

    Its own entries are checked here; a seat it copied from the code panel was
    checked there, and a design extra in its file -- both known by their
    origin (``_validate_panel_entries``). A plan has no changed paths, so
    ``when: paths`` is refused; and one seat must always run.
    """
    problems: List[str] = []
    review = data.get("review")
    design = review.get("design") if isinstance(review, dict) else None
    reviewers = design.get("reviewers") if isinstance(design, dict) else None
    # A design panel that is not a list is ``_validate_review_design``'s to report.
    if isinstance(reviewers, list):
        problems.extend(_validate_panel_entries(reviewers, origins, providers, DESIGN_PANEL))
        problems.extend(_validate_conditions(reviewers, data.get("optimization"), origins, design=True))
    for name, layer in (("global", global_layer), ("project", project_layer)):
        if layer:
            problems.extend(_validate_extras_file(layer, name, providers, DESIGN_PANEL))
    return problems


def _validate_run(run: Any) -> List[str]:
    if not isinstance(run, dict):
        return ["run: must be a mapping"]
    timeouts = run.get("timeout_seconds", {})
    if not isinstance(timeouts, dict):
        return ["run.timeout_seconds: must be a mapping of role to seconds"]
    problems: List[str] = []
    for role, value in timeouts.items():
        if role not in KNOWN_ROLES:
            problems.append(
                "run.timeout_seconds.%s: unknown role (known: %s)" % (role, ", ".join(KNOWN_ROLES))
            )
        elif not _int_at_least(value, 1):
            problems.append("run.timeout_seconds.%s: must be a positive integer" % role)
        elif not whole_seconds(value):
            problems.append("run.timeout_seconds.%s: %s" % (role, _TOO_LONG))
    return problems


def _validate_review(review: Any) -> List[str]:
    if not isinstance(review, dict):
        return ["review: must be a mapping"]
    problems: List[str] = []
    iterations = review.get("max_review_iterations", 2)
    if not _int_at_least(iterations, 0):
        problems.append("review.max_review_iterations: must be a non-negative integer")
    parallel = review.get("parallel")
    if parallel is not None and not isinstance(parallel, bool):
        problems.append("review.parallel: must be true or false")
    problems.extend(_validate_severities(review.get("re_review_severities")))
    timeout = review.get("timeout_seconds", 1800)
    if not _int_at_least(timeout, 1):
        problems.append("review.timeout_seconds: must be a positive integer")
    elif not whole_seconds(timeout):
        problems.append("review.timeout_seconds: %s" % _TOO_LONG)
    idle = review.get("idle_timeout_seconds")
    if idle is not None and not _int_at_least(idle, 1):
        problems.append("review.idle_timeout_seconds: must be a positive integer or null")
    elif idle is not None and not whole_seconds(idle):
        problems.append("review.idle_timeout_seconds: %s" % _TOO_LONG)
    incremental = review.get("incremental_rounds")
    if incremental is not None and not isinstance(incremental, bool):
        problems.append("review.incremental_rounds: must be true or false")
    findings_cap = review.get("max_findings")
    if findings_cap is not None and not _int_at_least(findings_cap, 0):
        problems.append("review.max_findings: must be a non-negative integer (0 = no cap)")
    design = review.get("design")
    if design is not None:
        problems.extend(_validate_review_design(design))
    context = review.get("context")
    if context is not None:
        problems.extend(_validate_review_context(context))
    exclude = review.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, list):
            problems.append("review.exclude: must be a list of glob patterns (use [] for none)")
        else:
            for index, pattern in enumerate(exclude):
                if not isinstance(pattern, str) or not pattern.strip():
                    problems.append(
                        "review.exclude[%d]: must be a non-empty string (got %r)" % (index, pattern)
                    )
    return problems


def _validate_severities(severities: Any) -> List[str]:
    """``review.re_review_severities``: a non-empty list drawn from ``SEVERITIES``, in any case.

    ``[]`` is refused rather than read as "block nothing": a review that
    blocks on nothing is not one somebody should get from an empty list.
    """
    if severities is None:
        return []
    choices = ", ".join(SEVERITIES)
    if not isinstance(severities, list):
        return ["review.re_review_severities: must be a list of %s (got %r)" % (choices, severities)]
    if not severities:
        return ["review.re_review_severities: must name at least one severity; [] would block nothing"]
    return [
        "review.re_review_severities[%d]: must be one of %s (got %r)" % (index, choices, name)
        for index, name in enumerate(severities)
        if not isinstance(name, str) or name.strip().lower() not in SEVERITIES
    ]


def _validate_review_design(design: Any) -> List[str]:
    if not isinstance(design, dict):
        return ["review.design: must be a mapping"]
    problems: List[str] = []
    enabled = design.get("enabled")
    if isinstance(enabled, str):
        known = enabled.strip().lower() == "auto"
    else:
        known = enabled is None or isinstance(enabled, bool)
    if not known:
        problems.append("review.design.enabled: must be true, false or auto")
    rounds = design.get("max_iterations")
    if rounds is not None and not _int_at_least(rounds, 0):
        problems.append("review.design.max_iterations: must be a non-negative integer")
    # The entries are checked with the panel; here only the shape, for data
    # validated without being composed.
    for key in ("reviewers", "reviewers_extra"):
        value = design.get(key)
        if value is not None and not isinstance(value, list):
            problems.append("review.design.%s: must be a list (use [] for none)" % key)
    return problems


def _validate_review_context(context: Any) -> List[str]:
    if not isinstance(context, dict):
        return ["review.context: must be a mapping"]
    problems: List[str] = []
    # No cross-check between the two. `inline_chars` above
    # `max_chars` only says "nothing is ever handed over as a
    # file except a forced round", and below it says "I will
    # not pay for a prompt that large" -- both are things
    # somebody means, and neither is a mistake to warn about.
    for key in ("max_chars", "inline_chars", "surrounding_chars"):
        value = context.get(key)
        if value is not None and not _int_at_least(value, 1):
            problems.append("review.context.%s: must be a positive integer" % key)
    # `off` is read as false by YAML, so false means none too;
    # true names no mode and is refused rather than guessed at.
    surrounding = context.get("surrounding")
    if isinstance(surrounding, str):
        known = surrounding.strip().lower() in ("none", "enclosing")
    else:
        known = surrounding is None or surrounding is False
    if not known:
        problems.append("review.context.surrounding: must be one of enclosing, none")
    return problems


def _validate_design(design: Any) -> List[str]:
    if not isinstance(design, dict):
        return ["design: must be a mapping"]
    problems: List[str] = []
    approval = design.get("require_approval")
    if approval is not None and not isinstance(approval, bool):
        problems.append("design.require_approval: must be true or false")
    ask = design.get("ask_questions")
    if isinstance(ask, str):
        known = ask.strip().lower() == "auto"
    else:
        known = ask is None or isinstance(ask, bool)
    if not known:
        problems.append("design.ask_questions: must be true, false or auto")
    rounds = design.get("max_question_rounds")
    if rounds is not None and not _int_at_least(rounds, 1):
        problems.append("design.max_question_rounds: must be a positive integer")
    resume = design.get("resume")
    if resume is not None:
        if not isinstance(resume, dict):
            problems.append("design.resume: must be a mapping")
        else:
            age = resume.get("max_age_seconds")
            if age is not None and not _int_at_least(age, 0):
                problems.append("design.resume.max_age_seconds: must be a non-negative integer")
            cap = resume.get("max_context_tokens")
            if cap is not None and not _int_at_least(cap, 1):
                problems.append("design.resume.max_context_tokens: must be a positive integer or null")
    return problems


def _validate_budgets(budgets: Any) -> List[str]:
    if not isinstance(budgets, dict):
        return ["budgets: must be a mapping"]
    problems: List[str] = []
    for key, value in budgets.items():
        if value is None:
            continue
        if not _int_at_least(value, 0):
            problems.append("budgets.%s: must be a non-negative integer or null" % key)
    return problems


def _validate_workspace(workspace: Any) -> List[str]:
    if not isinstance(workspace, dict):
        return ["workspace: must be a mapping"]
    problems: List[str] = []
    directory = workspace.get("dir")
    if directory is not None and not (isinstance(directory, str) and directory.strip()):
        problems.append("workspace.dir: must be a non-empty string (got %r)" % (directory,))
    days = workspace.get("stale_notice_days")
    if days is not None:
        if not _int_at_least(days, 0):
            problems.append("workspace.stale_notice_days: must be a non-negative integer (0 = off)")
        elif days > STALE_NOTICE_MAX_DAYS:
            problems.append(
                "workspace.stale_notice_days: must be %d or less (100 years)" % STALE_NOTICE_MAX_DAYS
            )
    return problems


def _validate_language(language: Any) -> List[str]:
    if not isinstance(language, dict):
        return ["language: must be a mapping"]
    problems: List[str] = []
    reply = language.get("reply")
    if reply is not None and normalise_language_tag(reply) is None:
        message = "language.reply: must be a language tag such as ja, zh-TW, ko or en, or null"
        if isinstance(reply, bool):
            # YAML reads a bare no, yes, on or off as a boolean; `no` is Norwegian.
            message += ' (got %s: quote a tag such as "no" to keep it a string)' % str(reply).lower()
        problems.append(message)
    rewrite = language.get("rewrite")
    if rewrite is not None and not isinstance(rewrite, bool):
        problems.append("language.rewrite: must be true or false")
    return problems


def _validate_reviewer_entry(
    reviewer: Dict[str, Any], label: str, when_prefix: str, providers: List[str], design: bool = False
) -> List[str]:
    """One reviewer entry's role, provider, model, ``high_risk_model``, ``relevance`` and ``when``.

    Its id is the caller's to check, against the list it is unique in.
    ``when_prefix`` is what goes before the ``when`` key in a message.
    ``design`` is a design panel's entry, which may not be ``when: paths``.
    """
    problems: List[str] = []
    role_name = reviewer.get("role", "general")
    if not isinstance(role_name, str) or not role_name.strip():
        problems.append("%s: role must be a non-empty string" % label)
    problems.extend("%s: %s" % (label, msg) for msg in _validate_role(reviewer, providers))
    if "high_risk_model" in reviewer:
        found = _validate_high_risk_model(reviewer["high_risk_model"])
        problems.extend("%s: %s" % (label, msg) for msg in found)
    problems.extend(_validate_relevance(reviewer, when_prefix))
    when = reviewer.get("when")
    if design and opt_mod.is_paths_condition(when):
        problems.append(
            "%swhen: a design reviewer cannot be when: paths -- a plan has no changed paths "
            "(use when: high-risk, or list it under reviewers for the code review)" % when_prefix
        )
    else:
        problems.extend(_validate_when(when, when_prefix))
    return problems


def _validate_high_risk_model(model: Any) -> List[str]:
    """A seat's ``high_risk_model``: a model block, as ``model`` is, and nothing else.

    The provider never changes on a high-risk round and the options stay the
    seat's, so a ``provider`` or ``options`` inside it is refused rather than
    ignored.
    """
    if not isinstance(model, dict):
        return ["high_risk_model must be a mapping with 'family' and 'version'"]
    problems = [
        "high_risk_model.%s: not allowed -- the provider and options stay the seat's own" % key
        for key in ("provider", "options")
        if key in model
    ]
    problems.extend("high_risk_" + message for message in _validate_model(model))
    return problems


def _validate_relevance(reviewer: Dict[str, Any], prefix: str) -> List[str]:
    """A seat's ``relevance``: one of the rules or ``always``, and no rule on a general seat."""
    if "relevance" not in reviewer:
        return []
    value = reviewer.get("relevance")
    choices = (*opt_mod.RELEVANCE_RULES, opt_mod.RELEVANCE_ALWAYS)
    if not isinstance(value, str) or value.strip().lower() not in choices:
        return ["%srelevance: must be one of %s (got %r)" % (prefix, ", ".join(choices), value)]
    role = reviewer.get("role", "general")
    if value.strip().lower() != opt_mod.RELEVANCE_ALWAYS and str(role).strip().lower() == "general":
        return [
            "%srelevance: a general reviewer is never skipped, so it takes no relevance rule (got %s)"
            % (prefix, value)
        ]
    return []


def _validate_when(when: Any, prefix: str) -> List[str]:
    """A reviewer's ``when``: one of the strings, or a mapping with ``paths``.

    Refuses exactly the forms ``optimization.reviewer_condition`` reads as
    ``always``, by asking the same predicate, so a config that validates is
    never read differently from how it was written. ``prefix`` goes before
    the key in each message.
    """
    if when is None:
        return []
    if isinstance(when, str):
        if when.strip().lower() in opt_mod.REVIEWER_CONDITIONS:
            return []
    elif isinstance(when, dict):
        if opt_mod.is_paths_condition(when):
            return []
        if set(when) != {"paths"}:
            keys = ", ".join(sorted(str(key) for key in when)) or "none"
            return ["%swhen: a when mapping takes paths only (got keys: %s)" % (prefix, keys)]
        patterns = when.get("paths")
        if not isinstance(patterns, list) or not patterns:
            return ["%swhen.paths: must be a non-empty list of glob patterns" % prefix]
        return [
            "%swhen.paths[%d]: must be a non-empty string (got %r)" % (prefix, index, pattern)
            for index, pattern in enumerate(patterns)
            if not isinstance(pattern, str) or not pattern.strip()
        ]
    conditions = ", ".join(opt_mod.REVIEWER_CONDITIONS)
    return ["%swhen: must be one of %s, or a mapping with paths" % (prefix, conditions)]


def _validate_extras_file(
    layer: Dict[str, Any], name: str, providers: List[str], panel_keys: PanelKeys = CODE_PANEL
) -> List[str]:
    """One file's extras of one panel, entry by entry: the only check of an extra's entry.

    Run on each file whether or not its extras joined the panel, so a global
    list that a project ``reviewers`` list replaces is still checked.
    """
    extras = get_path(layer, panel_keys.extras)
    if extras is None:
        return []
    if not isinstance(extras, list):
        return ["%s in the %s file: must be a list (use [] for none)" % (panel_keys.extras, name)]
    design = panel_keys == DESIGN_PANEL
    problems: List[str] = []
    seen: Set[str] = set()
    for index, reviewer in enumerate(extras):
        label = "%s[%d] in the %s file" % (panel_keys.extras, index, name)
        mapping, identity = _entry_identity(reviewer, label, seen)
        problems.extend(identity)
        if not mapping:
            continue
        # The label names the file, so the key is said after it.
        problems.extend(_validate_reviewer_entry(reviewer, label, label + ": ", providers, design=design))
    return problems


def _validate_conditions(
    reviewers: List[Any],
    optimization: Any,
    origins: Optional[Sequence[ReviewerOrigin]] = None,
    design: bool = False,
) -> List[str]:
    """What a panel of conditional reviewers needs to be able to run at all.

    One reviewer that always runs, or a round that matched nothing would have
    nobody to review it; and a pattern for the high-risk ones to be judged
    by, or they would never run. A path-scoped reviewer brings its own
    patterns, so only the first rule applies to it. ``review run`` validates
    before it reads a snapshot, so either mistake stops there rather than
    inside a round.

    The second rule holds only for a reviewer a file wrote. A seat from the
    fit or the built-in panel stays without patterns and runs on the rounds
    declared with ``review run --high-risk``; without ``origins`` every
    reviewer counts as written. ``design`` checks a design panel, named as such.
    """
    entries = [(index, reviewer) for index, reviewer in enumerate(reviewers) if isinstance(reviewer, dict)]
    always = opt_mod.WHEN_ALWAYS
    conditional = [index for index, reviewer in entries if opt_mod.reviewer_condition(reviewer) != always]
    high_risk = [
        index
        for index, reviewer in entries
        if opt_mod.reviewer_condition(reviewer) == opt_mod.WHEN_HIGH_RISK
        and not (origins is not None and index < len(origins) and origins[index].layer == "default")
    ]
    problems: List[str] = []
    key = DESIGN_PANEL.reviewers if design else CODE_PANEL.reviewers
    if entries and len(conditional) == len(entries):
        problems.append(
            "%s: at least one reviewer must run always; every reviewer is conditional "
            "(when: high-risk or when: paths)" % key
        )
    if high_risk and isinstance(optimization, dict) and not opt_mod.risk_patterns(optimization):
        first = high_risk[0]
        origin = origins[first] if origins is not None and first < len(origins) else None
        if origin is not None and (origin.extra or not design):
            label = _origin_label(origin, first)
        else:
            label = "%s[%d]" % (key, first)
        problems.append(
            "optimization.high_risk_paths: no pattern in force, but %s is when: high-risk "
            "and would never run; add patterns to high_risk_paths or extra_high_risk_paths" % label
        )
    return problems


def _validate_optimization(data: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["optimization: must be a mapping"]
    levels = opt_mod.LEVELS
    level = data.get("level")
    if level is not None and (not isinstance(level, str) or level.strip().lower() not in levels):
        problems.append("optimization.level: must be one of %s" % ", ".join(sorted(levels)))
    skip = data.get("skip_unneeded_roles")
    if skip is not None and not isinstance(skip, bool):
        problems.append("optimization.skip_unneeded_roles: must be true or false")
    for key in (
        "high_risk_paths",
        "extra_high_risk_paths",
        "security_paths",
        "extra_security_paths",
        "architecture_paths",
        "extra_architecture_paths",
    ):
        patterns = data.get(key)
        if patterns is None:
            continue
        if not isinstance(patterns, list):
            problems.append("optimization.%s: must be a list of glob patterns (use [] for none)" % key)
            continue
        for index, pattern in enumerate(patterns):
            if not isinstance(pattern, str) or not pattern.strip():
                problems.append(
                    "optimization.%s[%d]: must be a non-empty string (got %r)" % (key, index, pattern)
                )
    for key in ("low_risk_max_files", "low_risk_max_lines"):
        value = data.get(key)
        if value is not None and not _int_at_least(value, 0):
            problems.append("optimization.%s: must be a non-negative integer" % key)
    return problems


def _validate_tiers(spec: Dict[str, Any], label: str, providers: List[str]) -> List[str]:
    """Every tier is checked as the role it would become.

    A tier is only ever used by name, so a broken one fails at the moment
    somebody routes work to it -- which is the worst moment to find out. It
    is validated here as a whole merged role, by the same rules.
    """
    tiers = spec.get("model_tiers")
    if tiers is None:
        return []
    if not isinstance(tiers, dict):
        return ["%s.model_tiers: must be a mapping of tier name to overrides" % label]
    problems: List[str] = []
    for name, entry in tiers.items():
        if not isinstance(name, str) or not _TIER_NAME_RE.match(name):
            problems.append("%s.model_tiers: %r is not a usable tier name" % (label, name))
            continue
        if not isinstance(entry, dict):
            problems.append("%s.model_tiers.%s: must be a mapping" % (label, name))
            continue
        if not entry:
            problems.append("%s.model_tiers.%s: sets nothing, so it is not a tier" % (label, name))
            continue
        unknown = sorted(set(entry) - {"provider", "model", "options"})
        if unknown:
            problems.append(
                "%s.model_tiers.%s: only provider, model and options can be overridden (got %s)"
                % (label, name, ", ".join(unknown))
            )
            continue
        problems.extend(
            "%s.model_tiers.%s: %s" % (label, name, message)
            for message in _validate_role(merge_tier(spec, entry), providers)
        )
    return problems


def _validate_role(spec: Dict[str, Any], providers: List[str]) -> List[str]:
    problems: List[str] = []
    provider = spec.get("provider")
    if not isinstance(provider, str) or not provider:
        problems.append("provider is required")
    elif provider not in providers:
        problems.append(
            "unknown provider %r (known: %s; %s)" % (provider, ", ".join(providers), user_providers_hint())
        )

    # Options first, because the model checks below return early. A role may
    # legitimately omit `model` -- that is how you let a CLI pick its own --
    # and doing so used to skip option validation entirely, so a typo in a
    # sandbox policy passed `config validate` and was discovered at run time.
    problems.extend(_validate_role_options(spec, provider, providers))

    model = spec.get("model")
    if model is None:
        return problems
    if not isinstance(model, dict):
        problems.append("model must be a mapping with 'family' and 'version'")
        return problems
    problems.extend(_validate_model(model))
    return problems


def _validate_model(model: Dict[str, Any]) -> List[str]:
    """A model block's family and version; each message starts ``model``."""
    problems: List[str] = []
    family = model.get("family")
    if family is not None and (not isinstance(family, str) or not family.strip()):
        problems.append("model.family must be a non-empty string")
    version = model.get("version", "latest")
    if not isinstance(version, str) or version not in ("latest", "pinned"):
        problems.append("model.version must be 'latest' or 'pinned' (got %r)" % (version,))
    if version == "pinned" and not model.get("id"):
        problems.append("model.version is 'pinned' but model.id is missing")
    return problems


def _validate_role_options(spec: Dict[str, Any], provider: Any, providers: List[str]) -> List[str]:
    """Options are provider-specific, so the adapter validates them.

    Only consulted when a role actually sets ``options``: for the common case
    this keeps configuration checks from shelling out to a CLI at all.
    """
    if "options" not in spec:
        return []
    if not isinstance(provider, str) or provider not in providers:
        return []
    # lazy: the provider registry; see the note at the top of this module
    from .providers import get_provider

    try:
        return get_provider(provider).validate_options(spec.get("options"))
    except Exception as exc:  # a broken adapter must not hide the rest of the report
        return ["options could not be validated: %s" % exc]
