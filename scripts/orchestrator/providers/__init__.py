"""Provider registry.

Adding a CLI to the plugin means dropping a module in this package and
registering a factory here -- nothing else in the skill needs to change.

Adding one *without* editing the plugin means dropping a module in the user's
config directory (``<config dir>/providers/*.py``): it is imported after the
built-ins, and survives a plugin update because it lives outside the plugin.
Built-in names cannot be taken over, and the first file to claim a name keeps
it. A module that fails to load is recorded, never fatal, and ``doctor``
reports it. Those modules are trusted code running in this process, not a
sandbox: the checks here catch mistakes, not a module set on getting round them.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import re
import sys
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from .base import (  # noqa: F401 - re-exported as the adapter interface
    MODE_IMPLEMENT,
    MODE_PLAN,
    MODE_REVIEW,
    MODES,
    READ_ONLY_MODES,
    Detection,
    ModelCandidate,
    ModelResolutionError,
    Provider,
    ResolvedModel,
    RunResult,
    Usage,
    clear_discovery_cache,
    redact,
)

ProviderFactory = Callable[[Optional[str]], Provider]

#: Set to anything but "" or "0" to skip the user's adapter directory entirely.
USER_PROVIDERS_DISABLED_ENV = "DEV_ORCHESTRA_NO_USER_PROVIDERS"

#: User modules are imported under this prefix, outside the package, so a
#: relative import in one fails loudly instead of reaching into the plugin.
USER_MODULE_PREFIX = "dev_orchestra_user_providers."

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ProviderOrigin(NamedTuple):
    kind: str  # "builtin" | "user"
    path: Optional[str]  # the file, for user modules
    module: str  # sys.modules name


class ProviderRegistrationError(ValueError):
    """Raised when a registration would take over a name or bypass the loader."""


_REGISTRY: Dict[str, ProviderFactory] = {}
_ORIGINS: Dict[str, ProviderOrigin] = {}

#: True once the built-ins are in. From then on a registration has to say
#: where it comes from, so nothing registered later can pass for a built-in.
_BOOTSTRAPPED = False

#: The user module being loaded, while it is. ``register()`` refuses every
#: call in that window: user modules register through ``build_provider()``.
_EXECUTING_USER_MODULE: Optional[str] = None

#: True only around the loader's own ``register()`` call, the one way a
#: ``user`` origin gets in -- so a factory run later cannot add names of its own.
_LOADER_REGISTERING = False

_USER_REPORT: Optional[Dict[str, Any]] = None


def register(name: str, factory: ProviderFactory, origin: Optional[ProviderOrigin] = None) -> None:
    if _EXECUTING_USER_MODULE is not None or (
        origin is not None
        and origin.kind != "builtin"
        and not (origin.kind == "user" and _LOADER_REGISTERING)
    ):
        raise ProviderRegistrationError(
            "user provider modules register through build_provider(), not register()"
        )
    if origin is None or origin.kind == "builtin":
        if _BOOTSTRAPPED:
            raise ProviderRegistrationError(
                "cannot register %r as a built-in provider; add a user adapter under %s instead"
                % (name, _user_providers_dir())
            )
        origin = origin or ProviderOrigin("builtin", None, getattr(factory, "__module__", "") or "")
    existing = _ORIGINS.get(name)
    if name in _REGISTRY:
        if existing.kind == "builtin":
            raise ProviderRegistrationError("%r is a built-in provider; the built-in wins" % name)
        raise ProviderRegistrationError(
            "%r is already provided by %s; the first file wins" % (name, existing.path)
        )
    _REGISTRY[name] = factory
    _ORIGINS[name] = origin


def available_providers() -> List[str]:
    return sorted(_REGISTRY)


def get_provider(name: str, executable: Optional[str] = None) -> Provider:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise UnknownProviderError(
            "unknown provider %r (known: %s)" % (name, ", ".join(available_providers()))
        ) from None
    return factory(executable)


class UnknownProviderError(ValueError):
    """Raised when a config references a provider with no adapter."""


def provider_origin(name: str) -> Optional[ProviderOrigin]:
    return _ORIGINS.get(name)


def describe_origin(name: str) -> str:
    origin = _ORIGINS.get(name)
    if origin is None:
        return "unknown"
    if origin.kind == "user":
        return "user module %s" % origin.path
    return "built-in"


def origin_payload(name: str) -> Optional[Dict[str, Optional[str]]]:
    """The origin as ``--json`` output carries it."""
    origin = _ORIGINS.get(name)
    return {"kind": origin.kind, "path": origin.path} if origin else None


def describe_exception(exc: BaseException) -> str:
    return "%s: %s" % (type(exc).__name__, exc)


def adapter_failure(name: str, exc: BaseException) -> str:
    """One wording for an adapter that raised, wherever it is reported."""
    return "%s adapter failed (%s): %s" % (name, describe_origin(name), describe_exception(exc))


# --------------------------------------------------------------------------- user adapters


def _user_providers_dir() -> str:
    from .. import config as config_mod

    return config_mod.user_providers_dir()


def user_providers_disabled() -> bool:
    return os.environ.get(USER_PROVIDERS_DISABLED_ENV, "").strip() not in ("", "0")


def unload_user_providers() -> None:
    """Forget every user adapter, so the directory can be read again."""
    global _USER_REPORT
    for name in [name for name, origin in _ORIGINS.items() if origin.kind == "user"]:
        _REGISTRY.pop(name, None)
        _ORIGINS.pop(name, None)
    for module in [module for module in sys.modules if module.startswith(USER_MODULE_PREFIX)]:
        del sys.modules[module]
    # Discovery is memoised by module and class name, which an edited file
    # keeps: without this, the next load would be answered from the old one.
    clear_discovery_cache()
    _USER_REPORT = None


def user_provider_report() -> Dict[str, Any]:
    """What the last ``load_user_providers()`` imported and what it refused."""
    if _USER_REPORT is not None:
        return copy.deepcopy(_USER_REPORT)
    return _empty_report(_user_providers_dir())


def _empty_report(directory: str) -> Dict[str, Any]:
    return {
        "directory": directory,
        "enabled": not user_providers_disabled(),
        "present": os.path.isdir(directory),
        "loaded": [],
        "errors": [],
    }


def load_user_providers(directory: Optional[str] = None) -> Dict[str, Any]:
    """Import ``<config dir>/providers/*.py`` and register what each provides.

    Never raises for anything a user module does: each file is loaded in its
    own ``try`` and a failure is recorded against its path.
    """
    global _USER_REPORT
    unload_user_providers()
    if directory is None:
        directory = _user_providers_dir()
    report = _empty_report(directory)
    _USER_REPORT = report
    if not report["enabled"] or not report["present"]:
        return copy.deepcopy(report)
    try:
        entries = sorted(os.listdir(directory))
    except OSError as exc:
        report["errors"].append({"path": directory, "error": describe_exception(exc)})
        return copy.deepcopy(report)
    for entry in entries:
        path = os.path.join(directory, entry)
        if not entry.endswith(".py") or entry.startswith(("_", ".")) or not os.path.isfile(path):
            continue
        name, error = _load_user_module(path)
        if error is None:
            report["loaded"].append({"name": name, "path": path, "module": USER_MODULE_PREFIX + entry[:-3]})
        else:
            report["errors"].append({"path": path, "error": error})
    return copy.deepcopy(report)


def _load_user_module(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Load one file; returns ``(provider name, None)`` or ``(None, error)``."""
    global _EXECUTING_USER_MODULE, _LOADER_REGISTERING
    modname = USER_MODULE_PREFIX + os.path.basename(path)[:-3]
    # Everything the module does to the registry -- from its top level or from
    # the build_provider() call below -- is undone afterwards, so the one
    # registration a file gets is the one this loader makes.
    snapshot = (dict(_REGISTRY), dict(_ORIGINS))
    error: Optional[str] = None
    name: Optional[str] = None
    module: Any = None
    _EXECUTING_USER_MODULE = path
    try:
        spec = importlib.util.spec_from_file_location(modname, path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot import %s" % path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        name = _check_contract(module)
    except (Exception, SystemExit) as exc:
        error = describe_exception(exc)
        if isinstance(exc, ImportError) and (
            USER_MODULE_PREFIX.rstrip(".") in str(exc) or "relative import" in str(exc)
        ):
            error += " (user modules import from orchestrator.providers.base, not from .base)"
    finally:
        _EXECUTING_USER_MODULE = None
        changed = _restore(snapshot)
    if changed:
        restored = "changed the provider registry while loading (%s); restored" % ", ".join(changed)
        error = restored if error is None else "%s; %s" % (error, restored)
    if error is None:
        _LOADER_REGISTERING = True
        try:
            register(name, _guarded(module.build_provider, path), ProviderOrigin("user", path, modname))
        except ProviderRegistrationError as exc:
            error = describe_exception(exc)
        finally:
            _LOADER_REGISTERING = False
    if error is not None:
        sys.modules.pop(modname, None)
        return None, error
    return name, None


def _guarded(build: ProviderFactory, path: str) -> ProviderFactory:
    """``build`` as the registry stores it: every later call gets the same
    snapshot-and-restore the loader gives the first one."""

    def factory(executable: Optional[str] = None) -> Provider:
        snapshot = (dict(_REGISTRY), dict(_ORIGINS))
        try:
            return build(executable)
        finally:
            changed = _restore(snapshot)
            if changed:
                raise ProviderRegistrationError(
                    "%s changed the provider registry from build_provider() (%s); restored"
                    % (path, ", ".join(changed))
                )

    factory.__module__ = getattr(build, "__module__", factory.__module__)
    return factory


def _check_contract(module: Any) -> str:
    """The name to register under, read once: a second read is the adapter's to break."""
    build = getattr(module, "build_provider", None)
    if not callable(build):
        raise TypeError("defines no build_provider(executable=None) function")
    provider = build(None)
    if not isinstance(provider, Provider):
        raise TypeError(
            "build_provider(None) returned %s, not an orchestrator.providers.base.Provider"
            % type(provider).__name__
        )
    name = provider.name
    if not isinstance(name, str) or not _NAME_PATTERN.match(name) or name == "base":
        raise ValueError(
            "provider name %r is not usable (lowercase letters, digits, '.', '_', '-')" % (name,)
        )
    return name


def _restore(snapshot: Tuple[Dict[str, ProviderFactory], Dict[str, ProviderOrigin]]) -> List[str]:
    """Put the registry back as ``snapshot`` had it; returns the names that differed."""
    registry, origins = snapshot
    changed = sorted(
        name
        for name in set(registry) | set(_REGISTRY) | set(origins) | set(_ORIGINS)
        if registry.get(name) is not _REGISTRY.get(name) or origins.get(name) != _ORIGINS.get(name)
    )
    if changed:
        _REGISTRY.clear()
        _REGISTRY.update(registry)
        _ORIGINS.clear()
        _ORIGINS.update(origins)
    return changed


def _bootstrap() -> None:
    global _BOOTSTRAPPED
    from . import claude, codex, mock

    built_ins = ((claude, claude.ClaudeProvider), (codex, codex.CodexProvider), (mock, mock.MockProvider))
    for module, cls in built_ins:
        register(cls.name, module.build_provider, ProviderOrigin("builtin", None, module.__name__))
    _BOOTSTRAPPED = True
    load_user_providers()


_bootstrap()
