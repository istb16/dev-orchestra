"""First-run setup wizard.

Also reachable non-interactively (``--defaults``) so CI and the skill's own
tests never block on a prompt. Everything it writes is a family + version
policy; concrete model ids stay out of the saved config unless the user
explicitly pins one.

What ``run`` returns is only what it asked about: a value nobody was asked for
is not a decision, and writing it down would pin today's default forever. What
it offers as the recommended answer comes from ``base``, which the caller
supplies -- only the caller knows which layer is being edited and therefore
what that layer would inherit.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config as config_mod
from .providers import (
    ModelResolutionError,
    adapter_failure,
    available_providers,
    describe_exception,
    describe_origin,
    get_provider,
)

#: Per-role recommended defaults, expressed as families only.
RECOMMENDED = {
    "orchestrator": ("claude", "sonnet"),
    "architect": ("claude", "fable"),
    "implementer": ("claude", "opus"),
    "review_fixer": ("claude", "opus"),
}
RECOMMENDED_REVIEWERS = (
    ("claude", "opus", "general"),
    ("codex", "recommended-coding", "general"),
)
ROLE_TITLES = (
    ("orchestrator", "Orchestrator"),
    ("architect", "Architect"),
    ("implementer", "Implementer"),
    ("review_fixer", "Review Fixer"),
)


def _say(text: str) -> None:
    """Print through the CLI's writer rather than through ``print``.

    ``cli._out`` degrades a character the console cannot encode instead of
    letting it kill the message, and a console that cannot encode one is
    exactly where the wizard is asked to echo config values and paths. Imported
    late because ``cli`` imports this module.
    """
    from . import cli

    cli._out(text)


class Prompter:
    """Console I/O, isolated so tests can drive the wizard with scripted answers."""

    def __init__(
        self,
        reader: Optional[Callable[[str], str]] = None,
        writer: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._read = reader or input
        self._write = writer or _say

    def say(self, text: str = "") -> None:
        self._write(text)

    def ask_text(self, question: str, default: str = "") -> str:
        suffix = " [%s]" % default if default else ""
        answer = self._read("%s%s: " % (question, suffix)).strip()
        return answer or default

    def ask_yes_no(self, question: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self._read("%s %s: " % (question, suffix)).strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Please answer y or n.")

    def ask_choice(self, question: str, options: Sequence[str], default_index: int = 0) -> int:
        self.say(question)
        for index, option in enumerate(options, 1):
            marker = " (recommended)" if index - 1 == default_index else ""
            self.say("  %d) %s%s" % (index, option, marker))
        while True:
            raw = self._read("Choice [%d]: " % (default_index + 1)).strip()
            if not raw:
                return default_index
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            self.say("Enter a number between 1 and %d." % len(options))

    def ask_int(self, question: str, default: int, minimum: int = 0, maximum: int = 20) -> int:
        while True:
            raw = self._read("%s [%d]: " % (question, default)).strip()
            if not raw:
                return default
            if raw.isdigit() and minimum <= int(raw) <= maximum:
                return int(raw)
            self.say("Enter a number between %d and %d." % (minimum, maximum))


def selectable_providers(
    include_missing: bool = True,
    failures: Optional[List[Tuple[str, str]]] = None,
) -> List[Tuple[str, str, bool]]:
    """(name, label, installed) for every registered adapter except the mock.

    An adapter that raises while being detected is left out rather than
    offered, and recorded in ``failures`` as (name, reason) when given one.
    """
    entries: List[Tuple[str, str, bool]] = []
    for name in available_providers():
        if name == "mock":
            continue
        try:
            provider = get_provider(name)
            detection = provider.detect()
        except Exception as exc:
            if failures is not None:
                reason = "adapter failed (%s): %s" % (describe_origin(name), describe_exception(exc))
                failures.append((name, reason))
            continue
        if not detection.installed and not include_missing:
            continue
        label = "%s (%s)" % (
            provider.display_name,
            ("%s" % detection.version) if detection.installed else "not installed",
        )
        entries.append((name, label, detection.installed))
    return entries


def default_reviewer_config() -> List[Dict[str, Any]]:
    reviewers = []
    for provider, family, role in RECOMMENDED_REVIEWERS:
        reviewers.append(config_mod.make_reviewer("%s-%s" % (provider, role), provider, family, role))
    return reviewers


def run(
    prompter: Prompter,
    existing: Optional[Dict[str, Any]] = None,
    base: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Drive the wizard. Returns (layer, save?).

    ``base`` is what would be in force without the layer being edited; it falls
    back to the built-in defaults, which is what the global layer inherits.
    Setting up a project layer over a global one that chose sonnet has to offer
    sonnet, or pressing enter through the wizard would quietly overrule the
    global choice with a built-in default nobody asked for.
    """
    base = base or config_mod.default_config()
    effective = config_mod.deep_merge(base, existing or {})
    data: Dict[str, Any] = copy.deepcopy(existing or {})
    # Whatever else the layer keeps, it keeps its own version -- an invalid one
    # included, for `validate` to report rather than for this to paper over.
    data.setdefault("version", config_mod.CONFIG_VERSION)

    failures: List[Tuple[str, str]] = []
    providers = selectable_providers(failures=failures)
    prompter.say("AI Development Orchestrator setup")
    prompter.say("")
    prompter.say("Detected CLIs:")
    for name, label, installed in providers:
        prompter.say("  %-8s %s" % (name + ":", "installed" if installed else "not found"))
        del label
    for name, reason in failures:
        prompter.say("  %-8s %s" % (name + ":", reason))
    prompter.say("")
    prompter.say("Roles below are saved as a model family plus a version policy, so they")
    prompter.say("keep following the latest model in that family.")
    prompter.say("")

    step = 1
    for key, title in ROLE_TITLES:
        prompter.say("%d. %s" % (step, title))
        current = effective.get(key) or {}
        rec_provider, rec_family = RECOMMENDED[key]
        spec = _ask_role(
            prompter,
            providers,
            default_provider=str(current.get("provider") or rec_provider),
            default_family=str((current.get("model") or {}).get("family") or rec_family),
        )
        data[key] = spec
        prompter.say("")
        step += 1

    prompter.say("%d. External Reviewers" % step)
    data["reviewers"] = _ask_reviewers(prompter, providers, effective)
    prompter.say("")

    # Summarised over the base, so what is shown before saving is what `load()`
    # will resolve afterwards -- the layer alone would report a design review
    # as off while the global layer has it on.
    prompter.say(render_summary(config_mod.deep_merge(base, data)))
    save = prompter.ask_yes_no("Save configuration?", True)
    return data, save


def _ask_role(
    prompter: Prompter,
    providers: Sequence[Tuple[str, str, bool]],
    default_provider: str,
    default_family: str,
) -> Dict[str, Any]:
    provider_name, model = _ask_cli_and_model(
        prompter, providers, "   CLI:", default_provider, lambda _name: default_family, note_missing=True
    )
    return {"provider": provider_name, "model": model}


def _ask_cli_and_model(
    prompter: Prompter,
    providers: Sequence[Tuple[str, str, bool]],
    question: str,
    default_provider: str,
    family_for: Callable[[str], str],
    note_missing: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Pick a CLI, then its model; a CLI whose adapter raises is dropped and
    the question asked again over the rest."""
    remaining = list(providers)
    while True:
        names = [entry[0] for entry in remaining]
        labels = [entry[1] for entry in remaining]
        default_index = names.index(default_provider) if default_provider in names else 0
        chosen = prompter.ask_choice(question, labels, default_index)
        provider_name = names[chosen]
        if note_missing and not remaining[chosen][2]:
            prompter.say(
                "   Note: %s is not installed. The config will be saved, but this role "
                "will fail until the CLI is available." % provider_name
            )
        default_family = family_for(provider_name)
        model = _ask_model(prompter, provider_name, default_family)
        if model is not None:
            return provider_name, model
        remaining = [entry for entry in remaining if entry[0] != provider_name]
        if not remaining:
            prompter.say(
                "   No other CLI to choose; keeping %s with model family %r."
                % (provider_name, default_family)
            )
            return provider_name, {"family": default_family, "version": "latest"}
        prompter.say("   Choose another CLI.")


def _ask_model(prompter: Prompter, provider_name: str, default_family: str) -> Optional[Dict[str, Any]]:
    """The model for ``provider_name``, or None when its adapter raised."""
    try:
        provider = get_provider(provider_name)
        candidates = provider.list_models()
    except Exception as exc:
        _say_adapter_failed(prompter, provider_name, exc)
        return None
    options = ["%s [%s]" % (c.label, c.source) for c in candidates]
    families = [c.family for c in candidates]
    options.append("custom (type a family or exact model id)")

    default_index = families.index(default_family) if default_family in families else 0
    chosen = prompter.ask_choice("   Model:", options, default_index)
    if chosen < len(candidates):
        return {"family": candidates[chosen].family, "version": "latest"}

    raw = prompter.ask_text("   Model family or id", default_family)
    try:
        provider.resolve_model({"family": raw, "version": "latest"})
    except ModelResolutionError as exc:
        prompter.say("   %s" % exc)
        if prompter.ask_yes_no("   Pin this exact model id instead?", False):
            return {"family": raw, "version": "pinned", "id": raw}
        return {"family": default_family, "version": "latest"}
    except Exception as exc:
        _say_adapter_failed(prompter, provider_name, exc)
        return None
    return {"family": raw, "version": "latest"}


def _say_adapter_failed(prompter: Prompter, provider_name: str, exc: BaseException) -> None:
    prompter.say("   %s" % adapter_failure(provider_name, exc))


def _ask_reviewers(
    prompter: Prompter,
    providers: Sequence[Tuple[str, str, bool]],
    data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    # An empty panel is a decision -- somebody chose to run no independent
    # review -- and only an absent one means nobody has chosen yet. Reading the
    # two the same way turned a global `reviewers: []` back into two reviewers
    # for anyone who pressed enter through project setup.
    existing = data.get("reviewers")
    if not isinstance(existing, list):
        existing = default_reviewer_config()
    count = prompter.ask_int("   How many reviewers?", len(existing), 0, 10)
    reviewers: List[Dict[str, Any]] = []
    index = 0
    while True:
        while index < count:
            prompter.say("   reviewer #%d" % (index + 1))
            template = existing[index] if index < len(existing) else {}
            reviewers.append(_ask_reviewer(prompter, providers, template, {"reviewers": reviewers}))
            index += 1
        if count == 0:
            prompter.say(
                "   No reviewers configured. The independent-review stage will be skipped;"
                " two or more reviewers are recommended."
            )
            break
        if not prompter.ask_yes_no("   Add another reviewer?", False):
            break
        count += 1
    return reviewers


def _ask_reviewer(
    prompter: Prompter,
    providers: Sequence[Tuple[str, str, bool]],
    template: Dict[str, Any],
    scratch: Dict[str, Any],
) -> Dict[str, Any]:
    default_provider = str(template.get("provider") or providers[0][0])
    template_family = str((template.get("model") or {}).get("family") or "")

    def family_for(provider_name: str) -> str:
        if template_family:
            return template_family
        return "opus" if provider_name == "claude" else "recommended-coding"

    provider_name, model = _ask_cli_and_model(prompter, providers, "     CLI:", default_provider, family_for)

    role_options = [*list(config_mod.BUILTIN_ROLES), "custom role"]
    default_role = str(template.get("role") or "general")
    role_index = role_options.index(default_role) if default_role in role_options else 0
    picked = prompter.ask_choice("     Review role:", role_options, role_index)
    role = role_options[picked]
    if picked == len(role_options) - 1:
        role = prompter.ask_text("     Custom role name", "general") or "general"

    suggested = config_mod.suggest_reviewer_id(scratch, provider_name, role)
    default_id = str(template.get("id") or suggested)
    if any(r.get("id") == default_id for r in scratch.get("reviewers") or []):
        default_id = suggested
    taken = {r.get("id") for r in scratch.get("reviewers") or []}
    while True:
        reviewer_id = prompter.ask_text("     Reviewer id", default_id)
        if reviewer_id in taken:
            # Saving a duplicate id produces a config that every later command
            # rejects, so catch it while the user is still here to fix it.
            prompter.say("     %r is already used by another reviewer." % reviewer_id)
            continue
        if not config_mod.is_valid_reviewer_id(reviewer_id):
            prompter.say("     Ids must look like %s (lowercase, digits, . _ -)." % suggested)
            continue
        break
    return {"id": reviewer_id, "provider": provider_name, "model": model, "role": role}


def render_summary(data: Dict[str, Any]) -> str:
    lines = ["Configuration", ""]
    for key, title in ROLE_TITLES:
        spec = data.get(key) or {}
        lines.append("  %s" % title)
        lines.append("    %s" % _describe(spec))
        # Shown because a tier is invisible until somebody routes work to it,
        # and an unused tier is usually one nobody remembered was there.
        tiers = spec.get("model_tiers") if isinstance(spec, dict) else None
        for name in sorted(tiers) if isinstance(tiers, dict) else []:
            entry = tiers[name]
            described = _describe(merged(spec, entry)) if isinstance(entry, dict) else "(invalid)"
            lines.append("      --tier %-10s %s" % (name, described))
    reviewers = data.get("reviewers") or []
    lines.append("  Reviews")
    if not reviewers:
        lines.append("    (none configured)")
    for index, reviewer in enumerate(reviewers, 1):
        lines.append(
            "    %d. %s / %s / %s"
            % (index, _describe(reviewer), reviewer.get("role", "general"), reviewer.get("id"))
        )
    # Shown rather than asked: the wizard settles who does which job, and this
    # is a behaviour knob like `max_review_iterations`. But it decides whether
    # a whole stage runs, so leaving it out of the summary entirely would make
    # it the one stage nobody can see the state of.
    lines.append(
        "    design review: %s  (review.design.enabled)" % ("on" if _design_review_enabled(data) else "off")
    )
    lines.append("")
    return "\n".join(lines)


def _design_review_enabled(data: Dict[str, Any]) -> bool:
    """Falls back to the built-in default: a layer may name no `review` at all."""
    review = data.get("review")
    design = review.get("design") if isinstance(review, dict) else None
    if isinstance(design, dict) and "enabled" in design:
        return bool(design["enabled"])
    return bool(config_mod.default_config()["review"]["design"]["enabled"])


def merged(spec: Dict[str, Any], tier: Dict[str, Any]) -> Dict[str, Any]:
    """Late import: the config module imports this one for its prompts."""
    from .config import merge_tier

    return merge_tier(spec, tier)


def _describe(spec: Dict[str, Any]) -> str:
    model = spec.get("model") or {}
    version = model.get("version", "latest")
    family = model.get("id") if version == "pinned" else model.get("family", "default")
    return "%s / %s / %s" % (spec.get("provider", "?"), family or "default", version)
