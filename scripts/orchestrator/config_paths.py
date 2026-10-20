"""Where the configuration files are, and where Windows really keeps them.

``config`` re-exports its public names, except the three a test replaces:
``on_windows``, ``real_location`` and ``stored_elsewhere`` are looked up here,
so a test patches them here.
"""

from __future__ import annotations

import os
import sys
from typing import Any, List, Optional

# One import stays inside the function that needs it. The provider registry:
# importing it runs its bootstrap, which asks this module for
# ``user_providers_dir`` and imports every user adapter, and configuration
# must stay loadable without the provider registry.

APP_DIR_NAME = "dev-orchestra"
PROJECT_CONFIG_NAMES = (
    ".dev-orchestra.yaml",
    ".dev-orchestra.yml",
    ".dev-orchestra.json",
)


def global_config_dir() -> str:
    override = os.environ.get("DEV_ORCHESTRA_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(os.path.abspath(os.path.expanduser(base)), APP_DIR_NAME)


def user_providers_dir() -> str:
    """Where a user's own provider adapters live; loaded by the provider registry.

    Under the config directory, which ``DEV_ORCHESTRA_HOME`` moves and
    ``DEV_ORCHESTRA_CONFIG`` does not: that one names a file, which may sit in
    a project checkout, and code beside it is not the user's to import.
    """
    return os.path.join(global_config_dir(), "providers")


def user_providers_hint() -> str:
    """Where a missing provider's adapter would come from, and whether it can."""
    # lazy: the provider registry; see the note at the top of this module
    from .providers import USER_PROVIDERS_DISABLED_ENV, user_providers_disabled

    hint = "user adapters load from %s" % shown_location(user_providers_dir())
    if user_providers_disabled():
        hint += " (disabled by %s)" % USER_PROVIDERS_DISABLED_ENV
    return hint


def global_config_path() -> str:
    explicit = os.environ.get("DEV_ORCHESTRA_CONFIG")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(global_config_dir(), "config.yaml")


def on_windows() -> bool:
    """Whether this process runs on Windows; tests replace it."""
    return sys.platform.startswith("win")


def real_location(path: str) -> str:
    """Where ``path`` really is on disk; tests replace it."""
    return os.path.realpath(path)


def _path_key(path: str) -> str:
    # Windows paths compare case-insensitively and either separator works;
    # spelled out rather than ``normcase`` so it does not depend on the host.
    return path.lower().replace("/", "\\")


_PACKAGES_KEY = "\\appdata\\local\\packages\\"


def stored_elsewhere(path: str) -> Optional[str]:
    """The real location of ``path`` when Windows keeps it somewhere else.

    A Microsoft Store Python has the files it writes under the user's AppData
    quietly redirected into its package folder, so the path we built is only
    right from inside this process. ``None`` off Windows and whenever the file
    is where it seems to be. Links that merely rename a folder above the file,
    such as 8.3 short names, cancel out: the comparison is against the real
    location of an anchor -- the user profile, which is never redirected, or
    the file's own folder when it sits outside the profile.
    """
    if not on_windows():
        return None
    home = os.path.expanduser("~")
    try:
        rel = os.path.relpath(path, home)
    except ValueError:  # another drive
        rel = os.pardir
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        anchor = os.path.dirname(path)
        try:
            rel = os.path.relpath(path, anchor)
        except ValueError:
            rel = os.path.basename(path)
    else:
        anchor = home
    expected = real_location(anchor) if rel == os.curdir else os.path.join(real_location(anchor), rel)
    real = real_location(path)
    if _path_key(real) != _path_key(expected):
        return real
    if _PACKAGES_KEY in _path_key(real) and _PACKAGES_KEY not in _path_key(path):
        return real
    return None


def package_redirected(path: str) -> Optional[str]:
    """``stored_elsewhere``, only when a Microsoft Store package is what moves it.

    A link the user made leaves the real location elsewhere too, but a write
    through it lands where they meant it to.
    """
    real = stored_elsewhere(path)
    if real is not None and _PACKAGES_KEY in _path_key(real) and _PACKAGES_KEY not in _path_key(path):
        return real
    return None


def shown_location(path: str) -> str:
    """``path`` as printed: with its real location when that differs."""
    return describe_location(path, stored_elsewhere(path))


def describe_location(path: Any, real: Optional[str]) -> str:
    """``path``, and ``real`` beside it when there is one; for a renderer that
    reads a location ``stored_elsewhere`` already found."""
    if real is None:
        return str(path)
    return "%s (stored at %s)" % (path, real)


def find_project_config(start: Optional[str] = None) -> Optional[str]:
    """Walk up from ``start`` looking for a project override file.

    The walk stops at the repository root: any ``.git`` entry, so a
    worktree's or a submodule's ``.git`` file ends it too, rather than
    letting a parent directory's file configure the checkout.
    """
    for current in _up_to_repository_root(start):
        for name in PROJECT_CONFIG_NAMES:
            candidate = os.path.join(current, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _up_to_repository_root(start: Optional[str] = None) -> List[str]:
    """``start`` and each parent up to the first holding a ``.git`` entry, or to the
    filesystem root when none does. A walk rather than ``git rev-parse``: a
    reply-language hook runs it on every prompt."""
    current = os.path.abspath(start or os.getcwd())
    walked = [current]
    while not os.path.lexists(os.path.join(current, ".git")):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        walked.append(current)
    return walked


def repository_root(start: Optional[str] = None) -> Optional[str]:
    """The nearest directory up from ``start`` holding a ``.git`` entry, as
    ``find_project_config`` stops at; None outside a repository."""
    top = _up_to_repository_root(start)[-1]
    return top if os.path.lexists(os.path.join(top, ".git")) else None


def project_config_path(start: Optional[str] = None) -> str:
    """Where a *new* project override would be written."""
    existing = find_project_config(start)
    if existing:
        return existing
    return os.path.join(os.path.abspath(start or os.getcwd()), PROJECT_CONFIG_NAMES[0])
