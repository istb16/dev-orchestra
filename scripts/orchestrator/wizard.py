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
from . import config_policy as policy_mod
from . import presets as presets_mod
from .cli_common import _out
from .config_layers import _compose_preview, _reviewers_hold_design_panel
from .optimization import RELEVANCE_ALWAYS, WHEN_HIGH_RISK, reviewer_condition, risk_patterns
from .providers import (
    ModelResolutionError,
    adapter_failure,
    available_providers,
    describe_exception,
    describe_origin,
    get_provider,
    warned_provider,
)
from .summary import ROLE_TITLES, render_summary

#: Per-role recommended defaults, expressed as families only: the ``standard``
#: preset as written, so the wizard and the preset cannot disagree.
RECOMMENDED = {role: ("claude", family) for role, family in presets_mod.PRESETS["standard"].roles.items()}

#: The preset question's menu, in the order it is offered.
PRESET_CHOICES = (
    ("quality", "quality  -- strongest models, Codex beside Claude on both panels, design review auto"),
    (
        "standard",
        "standard -- the built-in defaults: general on Claude and Codex, security on sonnet "
        "(opus on high-risk changes), test on sonnet",
    ),
    (
        "fast",
        "fast     -- lighter models, one reviewer plus a sonnet security one on high-risk changes; "
        "design review off",
    ),
)
CUSTOMISE = "customise each role"

LANGUAGE_QUESTION = "Reply language (a tag such as ja, ko, zh-TW; blank: the language you write in)"
#: Typed to the language question, these drop a ``language.reply`` the file held.
LANGUAGE_CLEAR = ("none", "null", "-")


def _say(text: str) -> None:
    """Print through the CLI's writer rather than through ``print``.

    ``_out`` degrades a character the console cannot encode instead of
    letting it kill the message, and a console that cannot encode one is
    exactly where the wizard is asked to echo config values and paths.
    """
    _out(text)


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
    """The built-in panel, which is ``standard`` expanded with nothing installed:
    unique ids and ``when`` included, and PATH never read."""
    return copy.deepcopy(config_mod.default_config()["reviewers"])


def run(
    prompter: Prompter,
    existing: Optional[Dict[str, Any]] = None,
    base: Optional[Dict[str, Any]] = None,
    scope: str = "global",
    reply: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Drive the wizard. Returns (layer, save?).

    The reply language is asked last, just before saving; ``reply`` is the
    answer offered, and without one the layer's own ``language.reply``.

    ``base`` is what would be in force without the layer being edited; it falls
    back to the built-in defaults, which is what the global layer inherits.
    Setting up a project layer over a global one that chose sonnet has to offer
    sonnet, or pressing enter through the wizard would quietly overrule the
    global choice with a built-in default nobody asked for.

    For the global layer a preset is asked for first. Saved as is, the layer
    names it and nothing else it governs; adjusted, the questions below start
    from its fit and only what differs from that fit is kept. Only the global
    file can name a preset, so a project layer is asked the questions alone.
    """
    base = base or config_mod.default_config()
    data: Dict[str, Any] = copy.deepcopy(existing or {})
    # Whatever else the layer keeps, it keeps its own version -- an invalid one
    # included, for `validate` to report rather than for this to paper over.
    data.setdefault("version", config_mod.CONFIG_VERSION)

    failures: List[Tuple[str, str]] = []
    providers = selectable_providers(failures=failures)
    prompter.say("AI Development Orchestrator setup")
    prompter.say("")
    prompter.say("Detected CLIs:")
    for name, _label, installed in providers:
        prompter.say("  %-8s %s" % (name + ":", "installed" if installed else "not found"))
    for name, reason in failures:
        prompter.say("  %-8s %s" % (name + ":", reason))
    prompter.say("")

    preset: Optional[str] = None
    on_path = presets_mod.installed_providers()
    if scope == "global":
        options = [label for _name, label in PRESET_CHOICES]
        options.append(CUSTOMISE)
        names = [name for name, _label in PRESET_CHOICES]
        picked = prompter.ask_choice(
            "Preset (fitted to the CLIs found above):", options, names.index(presets_mod.DEFAULT)
        )
        prompter.say("")
        if picked < len(names):
            preset = names[picked]
            data = presets_mod.with_preset(existing, preset)
            preview, fit, _name, _source = config_mod.compose(data, {}, on_path)
            prompter.say(render_summary(preview, fit.origins, fit.design_origins))
            notes = presets_mod.render_notes(fit)
            if notes:
                prompter.say(notes)
                prompter.say("")
            data = _ask_language(prompter, data, reply)
            if prompter.ask_yes_no("Save as is?", True):
                return data, True
            prompter.say("")
            base = config_mod.compose({"preset": preset}, {}, on_path)[0]
    effective = config_mod.deep_merge(base, data)

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
            seat=key if key in config_mod.READ_ONLY_ROLES else "",
            scope=scope,
        )
        # The answer is a provider and a model; the role's options and tiers
        # were not asked about, so they stay as the layer held them.
        held = data.get(key)
        data[key] = {**held, **spec} if isinstance(held, dict) else spec
        prompter.say("")
        step += 1

    prompter.say("%d. External Reviewers" % step)
    # Never asked about and never written: the layer keeps it as the file held it.
    extras = data.get("reviewers_extra")
    if isinstance(extras, list) and extras:
        ids = ", ".join(str(entry.get("id")) for entry in extras if isinstance(entry, dict))
        prompter.say(
            "   This file's reviewers_extra (%s) is kept as it is; reviewer add/remove manage it." % ids
        )
    # Nor is the design panel: these questions are about the code review's.
    design_keys = [key for key in config_mod.DESIGN_PANEL if config_mod.get_path(data, key) is not None]
    if design_keys:
        prompter.say(
            "   This file's %s is kept as it is; reviewer add/set/remove --design manage it."
            % " and ".join(design_keys)
        )
    data["reviewers"] = _ask_reviewers(prompter, providers, effective, scope)
    prompter.say("")
    if preset is None:
        # Asked already, before "Save as is?", when a preset was picked.
        data = _ask_language(prompter, data, reply)
        prompter.say("")

    if preset is not None:
        data = _differences_from_fit(data, base, preset)
        if _reviewers_hold_design_panel(scope, data):
            # A saved code panel takes the design panel with it: say so, as
            # the reviewer writers do, before the summary shows the result.
            prompter.say(
                "   note: the reviewers differ from preset %s's fit, so they are saved; design rounds "
                "then run them without when, not the preset's design panel" % preset
            )
        preview, fit, _name, _source = config_mod.compose(data, {}, on_path)
        prompter.say(render_summary(preview, fit.origins, fit.design_origins))
        notes = presets_mod.render_notes(fit)
        if notes:
            prompter.say(notes)
            prompter.say("")
    else:
        # Composed as `load()` will compose it once saved, so what is shown
        # before saving is what is in force afterwards -- the layer alone would
        # report a design review as off while the global layer has it on, and
        # would leave out the extras it keeps.
        preview, fit, _name, _source = _compose_preview(scope, data)
        prompter.say(render_summary(preview, fit.origins, fit.design_origins))
    save = prompter.ask_yes_no("Save configuration?", True)
    return data, save


def _differences_from_fit(data: Dict[str, Any], fit: Dict[str, Any], preset: str) -> Dict[str, Any]:
    """What an adjusted preset setup saves: only what differs from the fit.

    A role that differs is kept whole. Pruned field by field it could lose
    the provider that happens to equal this machine's fit, and a role a file
    names is never fitted -- so it would come back on the default provider.
    A panel is compared whole, as ``deep_merge`` replaces it.

    Only what a preset governs is compared. Everything else the file held is
    kept as it was, a value equal to a default included: the user wrote it.
    """
    roles = {
        role: data[role] for role in config_mod.KNOWN_ROLES if role in data and data[role] != fit.get(role)
    }
    missing = object()
    governed: Dict[str, Any] = {}
    for dotted in presets_mod.GOVERNED:
        value = config_mod.get_path(data, dotted, missing)
        if dotted not in config_mod.KNOWN_ROLES and value is not missing:
            config_mod.set_path(governed, dotted, copy.deepcopy(value))
    reference = {key: value for key, value in fit.items() if key != "preset"}
    pruned, _dropped = config_mod.prune_layer(governed, reference)
    pruned.pop("version")
    kept = config_mod.deep_merge(presets_mod.with_preset(data, preset), pruned)
    return {**kept, **roles}


def _ask_language(prompter: Prompter, data: Dict[str, Any], offered: Optional[str]) -> Dict[str, Any]:
    """``data`` with the answer to the reply-language question, asked until it is a tag or blank.

    The answer offered is ``offered``, or the layer's own value; blank keeps
    that, and with neither sets nothing.
    """
    current = offered or config_mod.normalise_language_tag(config_mod.get_path(data, "language.reply"))
    question = LANGUAGE_QUESTION
    if current:
        question = question[:-1] + "; none: clear it)"
    while True:
        answer = prompter.ask_text(question, current or "")
        if not answer:
            return data
        if answer.lower() in LANGUAGE_CLEAR:
            config_mod.pop_path(data, "language.reply")
            return data
        tag = config_mod.normalise_language_tag(answer)
        if tag is not None:
            config_mod.set_path(data, "language.reply", tag)
            return data
        prompter.say("   %r is not a language tag such as ja, ko, zh-TW or en." % answer)


def _ask_role(
    prompter: Prompter,
    providers: Sequence[Tuple[str, str, bool]],
    default_provider: str,
    default_family: str,
    seat: str = "",
    scope: str = "global",
) -> Dict[str, Any]:
    """``seat`` is the role's name when it is a read-only one: a CLI that
    cannot be held to reading is warned about there, and refused in a project
    layer, where the question is asked again."""
    while True:
        provider_name, model = _ask_cli_and_model(
            prompter, providers, "   CLI:", default_provider, lambda _name: default_family, note_missing=True
        )
        spec = {"provider": provider_name, "model": model}
        if seat and warned_provider(provider_name):
            if scope == "project":
                name = config_mod.PROJECT_CONFIG_NAMES[0]
                path = "%s.provider" % seat
                prompter.say("   %s" % policy_mod.project_seat_refusal(seat, provider_name, name, path))
                continue
            _say_unenforced(prompter, {seat: spec})
        return spec


def _say_unenforced(prompter: Prompter, data: Dict[str, Any]) -> None:
    for line in policy_mod.read_only_enforcement_warnings(data):
        prompter.say("   Warning: %s" % line)


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
    scope: str = "global",
) -> List[Dict[str, Any]]:
    # An empty panel is a decision -- somebody chose to run no independent
    # review -- and only an absent one means nobody has chosen yet. Reading the
    # two the same way turned a global `reviewers: []` back into two reviewers
    # for anyone who pressed enter through project setup.
    existing = data.get("reviewers")
    if not isinstance(existing, list):
        existing = default_reviewer_config()
    # Saved, a fitted or built-in high-risk seat becomes one the file wrote,
    # which validate() refuses while no risk pattern is in force.
    optimization = data.get("optimization")
    if isinstance(optimization, dict) and not risk_patterns(optimization):
        skipped = [r for r in existing if isinstance(r, dict) and reviewer_condition(r) == WHEN_HIGH_RISK]
        if skipped:
            existing = [r for r in existing if r not in skipped]
            prompter.say(
                "   Not offered: %s (when: high-risk); optimization.high_risk_paths "
                "has no pattern in force." % ", ".join(str(r.get("id")) for r in skipped)
            )
    count = prompter.ask_int("   How many reviewers?", len(existing), 0, 10)
    reviewers: List[Dict[str, Any]] = []
    index = 0
    while True:
        while index < count:
            prompter.say("   reviewer #%d" % (index + 1))
            template = existing[index] if index < len(existing) else {}
            reviewers.append(_ask_reviewer(prompter, providers, template, {"reviewers": reviewers}, scope))
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
    scope: str = "global",
) -> Dict[str, Any]:
    default_provider = str(template.get("provider") or _default_reviewer_provider(providers))
    template_family = str((template.get("model") or {}).get("family") or "")

    def family_for(provider_name: str) -> str:
        if template_family:
            return template_family
        return config_mod.default_reviewer_family(provider_name)

    while True:
        provider_name, model = _ask_cli_and_model(
            prompter, providers, "     CLI:", default_provider, family_for
        )
        if scope != "project" or not warned_provider(provider_name):
            break
        # Refused in a project layer, as a run of it would be; asked again.
        shown = template.get("id") or config_mod.suggest_reviewer_id(scratch, provider_name, "general")
        name = config_mod.PROJECT_CONFIG_NAMES[0]
        refusal = policy_mod.project_reviewer_refusal("reviewer %s" % shown, provider_name, name)
        prompter.say("     %s" % refusal)

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
    reviewer: Dict[str, Any] = {"id": reviewer_id, "provider": template.get("provider"), "model": model}
    # Not asked, so kept: a seat's high-risk model stays while it stays on the
    # CLI that model belongs to, and goes with a line when it moves, by the
    # rule `reviewer set --provider` follows.
    if template.get("high_risk_model") is not None:
        reviewer["high_risk_model"] = copy.deepcopy(template["high_risk_model"])
    if config_mod.move_reviewer_provider(reviewer, provider_name):
        prompter.say(
            "     note: provider is now %s; %s (reviewer set --high-risk-model sets another)"
            % (provider_name, config_mod.HIGH_RISK_MODEL_REMOVED)
        )
    reviewer["role"] = role
    # Likewise a relevance rule, while the seat keeps the role it was written
    # for: a rule is about one role's work, and a general seat takes none.
    # ``always`` holds for any role.
    relevance = template.get("relevance")
    if relevance is not None:
        if role == template.get("role", "general") or str(relevance).strip().lower() == RELEVANCE_ALWAYS:
            reviewer["relevance"] = copy.deepcopy(relevance)
        else:
            prompter.say(
                "     note: role is now %s; its relevance %s was removed "
                "(reviewer set --relevance sets another)" % (role, relevance)
            )
    # Likewise: a preset's high-risk seat stays high-risk when its other
    # answers are taken as offered.
    if template.get("when") is not None:
        reviewer["when"] = copy.deepcopy(template["when"])
    _say_unenforced(prompter, {"reviewers": [reviewer]})
    return reviewer


def _default_reviewer_provider(providers: Sequence[Tuple[str, str, bool]]) -> str:
    """The CLI a new reviewer is offered: a seat provider, installed if one is.

    Not the first of the menu, which is sorted: a CLI that cannot be held to
    reading would sort first and be the default.
    """
    names = [entry[0] for entry in providers]
    installed = [entry[0] for entry in providers if entry[2]]
    for pool in (installed, names):
        for name in presets_mod.SEAT_PROVIDERS:
            if name in pool:
                return name
    return providers[0][0]
