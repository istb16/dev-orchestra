"""What a plugin host loads from the plugin root, and where it is installed.

Shared by ``scripts/validate_skill.py`` (what is committed), ``doctor`` (what
a live install would load now) and, by hand, the two installers (what they
refuse to link). Standard library only.

Named ``hosts`` rather than ``antigravity``: the standard library has an
``antigravity`` module, and importing that one opens a browser.
"""

from __future__ import annotations

import os
from typing import List, NamedTuple, Tuple

PLUGIN_NAME = "dev-orchestra"

#: The directory two levels above skills/dev-orchestra/SKILL.md, which is what
#: Antigravity loads. realpath: launched through a link, this is the checkout.
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

#: Entries at a plugin root that Antigravity loads on its own when the plugin
#: is enabled, and ``plugins.json``, which would make the root a customization
#: root. A linked checkout exposes its working tree, so none may exist here.
#: ``agents/*.md`` is checked separately. This is the definition: the
#: validator reads it, and the two installers spell out the same list.
ANTIGRAVITY_AUTOLOAD = ("hooks.json", "mcp_config.json", "plugins.json", "rules")

#: The parents the installers put the plugin in, as path segments: under the
#: home directory, and under a project with ``--project``.
ANTIGRAVITY_GLOBAL_PLUGINS = (".gemini", "config", "plugins")
ANTIGRAVITY_PROJECT_PLUGINS = (".agents", "plugins")


class AutoloadScan(NamedTuple):
    entries: List[str]
    errors: List[str]


def antigravity_autoload(root: str) -> AutoloadScan:
    """What Antigravity would load from ``root`` besides the skill.

    Never raises: a directory that cannot be listed is reported in
    ``errors``, since a caller that must always produce a report cannot say
    the root is clean.
    """
    entries = [entry for entry in ANTIGRAVITY_AUTOLOAD if os.path.lexists(os.path.join(root, entry))]
    errors: List[str] = []
    agents_dir = os.path.join(root, "agents")
    if os.path.isdir(agents_dir):
        try:
            names = sorted(os.listdir(agents_dir))
        except OSError as exc:
            errors.append("cannot list agents/ in %s: %s" % (root, exc.strerror or exc))
        else:
            # Case-insensitive: the PowerShell installer's -Filter '*.md' is on
            # Windows, and this must not be narrower than what it refuses.
            entries.extend("agents/%s" % name for name in names if name.lower().endswith(".md"))
    return AutoloadScan(entries, errors)


def resolves_to(path: str, root: str) -> bool:
    """Whether ``path`` is ``root``, directly or through a link or junction.

    ``realpath`` resolves a symlink and, on Windows, a junction; a dangling
    link resolves to something other than ``root``.
    """
    try:
        return os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(root))
    except OSError:
        return False


def antigravity_install_locations(project_root: str, home: str) -> List[Tuple[str, str]]:
    """``(scope, path)`` for each place the installers put the plugin."""
    return [
        ("global", os.path.join(home, *ANTIGRAVITY_GLOBAL_PLUGINS, PLUGIN_NAME)),
        ("project", os.path.join(project_root, *ANTIGRAVITY_PROJECT_PLUGINS, PLUGIN_NAME)),
    ]
