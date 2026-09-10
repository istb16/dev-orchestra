"""Provider registry.

Adding a CLI means dropping a module in this package and registering a factory
here -- nothing else in the skill needs to change.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

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
    redact,
)

ProviderFactory = Callable[[Optional[str]], Provider]

_REGISTRY: Dict[str, ProviderFactory] = {}


def register(name: str, factory: ProviderFactory) -> None:
    _REGISTRY[name] = factory


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


def _bootstrap() -> None:
    from . import claude, codex, mock

    register(claude.ClaudeProvider.name, claude.build_provider)
    register(codex.CodexProvider.name, codex.build_provider)
    register(mock.MockProvider.name, mock.build_provider)


_bootstrap()
