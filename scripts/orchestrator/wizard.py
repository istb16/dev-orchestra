"""First-run setup wizard.

Also reachable non-interactively (``--defaults``) so CI and the skill's own
tests never block on a prompt. Everything it writes is a family + version
policy; concrete model ids stay out of the saved config unless the user
explicitly pins one.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config as config_mod
from .providers import ModelResolutionError, available_providers, get_provider

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


class Prompter:
    """Console I/O, isolated so tests can drive the wizard with scripted answers."""

    def __init__(
        self,
        reader: Optional[Callable[[str], str]] = None,
        writer: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._read = reader or input
        self._write = writer or (lambda text: print(text))

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


def selectable_providers(include_missing: bool = True) -> List[Tuple[str, str, bool]]:
    """(name, label, installed) for every registered adapter except the mock."""
    entries: List[Tuple[str, str, bool]] = []
    for name in available_providers():
        if name == "mock":
            continue
        provider = get_provider(name)
        detection = provider.detect()
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
) -> Tuple[Dict[str, Any], bool]:
    """Drive the wizard. Returns (config, save?)."""
    data = config_mod.default_config()
    if existing:
        data = config_mod.deep_merge(data, existing)

    providers = selectable_providers()
    prompter.say("AI Development Orchestrator setup")
    prompter.say("")
    prompter.say("Detected CLIs:")
    for name, label, installed in providers:
        prompter.say("  %-8s %s" % (name + ":", "installed" if installed else "not found"))
        del label
    prompter.say("")
    prompter.say("Roles below are saved as a model family plus a version policy, so they")
    prompter.say("keep following the latest model in that family.")
    prompter.say("")

    step = 1
    for key, title in ROLE_TITLES:
        prompter.say("%d. %s" % (step, title))
        current = data.get(key) or {}
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
    data["reviewers"] = _ask_reviewers(prompter, providers, data)
    prompter.say("")

    prompter.say(render_summary(data))
    save = prompter.ask_yes_no("Save configuration?", True)
    return data, save


def _ask_role(
    prompter: Prompter,
    providers: Sequence[Tuple[str, str, bool]],
    default_provider: str,
    default_family: str,
) -> Dict[str, Any]:
    names = [entry[0] for entry in providers]
    labels = [entry[1] for entry in providers]
    default_index = names.index(default_provider) if default_provider in names else 0
    chosen = prompter.ask_choice("   CLI:", labels, default_index)
    provider_name = names[chosen]
    if not providers[chosen][2]:
        prompter.say(
            "   Note: %s is not installed. The config will be saved, but this role "
            "will fail until the CLI is available." % provider_name
        )
    model = _ask_model(prompter, provider_name, default_family)
    return {"provider": provider_name, "model": model}


def _ask_model(prompter: Prompter, provider_name: str, default_family: str) -> Dict[str, Any]:
    provider = get_provider(provider_name)
    candidates = provider.list_models()
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
    return {"family": raw, "version": "latest"}


def _ask_reviewers(
    prompter: Prompter,
    providers: Sequence[Tuple[str, str, bool]],
    data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    existing = data.get("reviewers") or default_reviewer_config()
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
    names = [entry[0] for entry in providers]
    labels = [entry[1] for entry in providers]
    default_provider = str(template.get("provider") or names[0])
    default_index = names.index(default_provider) if default_provider in names else 0
    provider_name = names[prompter.ask_choice("     CLI:", labels, default_index)]

    default_family = str((template.get("model") or {}).get("family") or "")
    if not default_family:
        default_family = "opus" if provider_name == "claude" else "recommended-coding"
    model = _ask_model(prompter, provider_name, default_family)

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
    reviewers = data.get("reviewers") or []
    lines.append("  Reviews")
    if not reviewers:
        lines.append("    (none configured)")
    for index, reviewer in enumerate(reviewers, 1):
        lines.append(
            "    %d. %s / %s / %s"
            % (index, _describe(reviewer), reviewer.get("role", "general"), reviewer.get("id"))
        )
    lines.append("")
    return "\n".join(lines)


def _describe(spec: Dict[str, Any]) -> str:
    model = spec.get("model") or {}
    version = model.get("version", "latest")
    family = model.get("id") if version == "pinned" else model.get("family", "default")
    return "%s / %s / %s" % (spec.get("provider", "?"), family or "default", version)
