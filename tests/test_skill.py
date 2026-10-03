"""The skill package itself: frontmatter, docs, installers, portability."""

from __future__ import annotations

import ast
import importlib
import os
import pkgutil
import re
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

from helpers import (
    REPO_ROOT,
    SCRIPTS_DIR,
    USER_ADAPTER_SOURCE,
    IsolatedCase,
    is_link,
    make_dir_link,
    present,
    remove_link,
    user_adapter_source,
)

import orchestrator
from orchestrator import miniyaml
from orchestrator.providers import USER_MODULE_PREFIX

# Imported by name rather than with a plain `import`: it lives in scripts/,
# which only goes on sys.path when helpers is imported above. An import
# sorter would happily move a normal import statement ahead of that.
validate_skill = importlib.import_module("validate_skill")

# Assembled at run time so this file does not trip its own check.
_LOCAL_PATH_RE = re.compile(r"[Cc]:[\\/]" + "Use" + r"rs[\\/]|/ho" + r"me/[a-z]")


def _has_local_path(content):
    return bool(_LOCAL_PATH_RE.search(content))


def read(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


_UNWALKED = (".git", "__pycache__", ".ai", ".tmpcfg", ".venv")


def _walked_dirs(dirpath, dirnames):
    """The subdirectories a walk of the repository goes into.

    Not a link: that is a project install made in the checkout, which leads
    back into it -- a junction on Windows, which Python 3.11 walks into.
    """
    return [d for d in dirnames if d not in _UNWALKED and not is_link(os.path.join(dirpath, d))]


class TestValidator(IsolatedCase):
    def test_the_shipped_skill_validates(self):
        self.assertEqual(validate_skill.check(), [])

    def test_validator_rejects_missing_frontmatter(self):
        with self.assertRaises(ValueError):
            validate_skill.parse_frontmatter("# No frontmatter here\n")

    def test_validator_rejects_unterminated_frontmatter(self):
        with self.assertRaises(ValueError):
            validate_skill.parse_frontmatter("---\nname: x\n")


class TestSkillDocument(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.text = read(validate_skill.SKILL_PATH)
        self.front, self.body = validate_skill.parse_frontmatter(self.text)

    def test_frontmatter_fields(self):
        self.assertEqual(self.front["name"], "dev-orchestra")
        self.assertEqual(self.front["license"], "MIT")
        self.assertTrue(self.front["description"])

    def test_description_covers_the_intended_triggers(self):
        description = self.front["description"].lower()
        for phrase in ("implement", "investigate", "review", "orchestrat", "configur", "model"):
            self.assertIn(phrase, description)

    def test_description_scopes_out_casual_questions(self):
        self.assertIn("not for", self.front["description"].lower())

    def test_stays_under_the_size_budget(self):
        self.assertLess(len(self.text.splitlines()), validate_skill.MAX_SKILL_LINES)

    def test_stays_under_the_token_budget(self):
        """Lines are a poor proxy for cost -- a table row and a paragraph are
        one line each and price very differently -- so the real ceiling is in
        characters. This document is resident in every session."""
        self.assertLess(len(self.text), validate_skill.MAX_SKILL_CHARS)

    def test_the_pipeline_can_be_run_without_opening_a_reference(self):
        """The document was compressed by cutting words, not steps. A reader
        who has to open references/workflow.md to find the next command has
        not saved anything: that file costs more than this one."""
        for command in (
            "run architect",
            "run implementer",
            "review snapshot",
            "review run",
            "review run --design",
            "design approve",
            "review triage",
            "review fix-brief",
            "run review_fixer",
            "review status",
            "budget consume",
            "progress record",
            "doctor",
            "config set",
            "reviewer add",
            "tokens show",
        ):
            self.assertIn(command, self.body, command)

    def test_the_helper_invocation_is_spelled_out_once(self):
        """Commands are written bare to avoid repeating a 46-character prefix
        fourteen times, which only works if the expansion is stated."""
        self.assertIn('python "PLUGIN_ROOT/scripts/dev_orchestra.py" <command>', self.body)
        self.assertIn("Commands below are written bare", self.body)

    def test_the_review_invariants_survive_compression(self):
        """These are the rules that make a multi-model review worth running;
        they are also the easiest sentences to lose while shortening prose."""
        body = self.body.lower()
        for rule in (
            "no reviewer sees another",
            "never re-snapshot mid-round",
            "unparsed",
            "partial",
            "not reviewed in full",
            "triage before re-snapshotting",
            "read-only",
        ):
            self.assertIn(rule, body)

    def test_the_stage_order_is_still_stated_as_an_order(self):
        self.assertIn("their **order is not**", self.body)
        self.assertIn("Never review before tests, never", self.body)

    def test_states_the_non_negotiable_rules(self):
        body = self.body.lower()
        for rule in (
            "never hard-code a dated model id",
            "reviewers are read-only",
            "fix only triaged-accepted findings",
            "never print or store credentials",
            "approval is the user's",
        ):
            self.assertIn(rule, body)

    def test_says_what_holds_reviewers_to_reading(self):
        """Read-only is a claim about what the CLI enforces, and the one
        guarantee Codex does not have -- external side effects -- has to stay
        said."""
        body = self.body.lower()
        self.assertIn("enforced by the cli", body)
        self.assertIn("not examined", body)

    def test_documents_stage_selection_both_ways(self):
        self.assertIn("Skip", self.body)
        self.assertIn("**Run**", self.body)

    def test_every_referenced_document_exists(self):
        for match in re.findall(r"`(references/[a-z0-9_.-]+\.md)`", self.body):
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, match)), match)

    def test_no_dated_model_ids_in_the_skill(self):
        for candidate in validate_skill.SNAPSHOT_RE.findall(self.text):
            self.assertFalse(candidate.startswith(("claude-", "gpt-")), candidate)


class TestDocumentation(IsolatedCase):
    def test_readme_covers_every_required_section(self):
        readme = read("README.md")
        for heading in (
            "Why this exists",
            "Architecture",
            "Requirements",
            "Installation",
            "Initial setup",
            "Usage",
            "Configuration",
            "Model selection",
            "Reviewers",
            "Example workflows",
            "Troubleshooting",
            "Security",
            "Supported platforms",
            "Upgrading",
            "Uninstalling",
            "Versioning",
            "Contributing",
            "License",
        ):
            self.assertIn(heading, readme, heading)

    def test_both_readmes_document_the_recommended_lineup(self):
        """The point of the tool is several models with different jobs."""
        for relative in ("README.md", "README.ja.md"):
            text = read(relative)
            for token in (
                "orchestrator",
                "architect",
                "implementer",
                "review_fixer",
                "dev-orchestra model list",
            ):
                self.assertIn(token, text, "%s: %s" % (relative, token))

    def test_both_readmes_explain_stage_by_stage_use(self):
        for relative in ("README.md", "README.ja.md"):
            text = read(relative)
            for command in (
                "dev-orchestra run architect",
                "dev-orchestra run implementer",
                "dev-orchestra review snapshot",
                "dev-orchestra run review_fixer",
            ):
                self.assertIn(command, text, "%s: %s" % (relative, command))

    def test_both_readmes_cover_bulk_input(self):
        """Issue #2: show how to hand a large corpus to one model first."""
        for relative in ("README.md", "README.ja.md"):
            text = read(relative)
            self.assertIn("dev-orchestra run orchestrator", text, relative)
            self.assertIn("--output .ai/analysis.md", text, relative)

    def test_readme_has_an_architecture_diagram(self):
        self.assertIn("```mermaid", read("README.md"))

    def test_example_config_parses_and_is_valid(self):
        from orchestrator import config as config_mod

        data = miniyaml.loads(read("examples/config.example.yaml"))
        self.assertEqual(config_mod.validate(data), [])

    def test_example_project_override_parses(self):
        data = miniyaml.loads(read("examples/project-override.example.yaml"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["reviewers"]), 3)

    def test_agent_manifest_parses_and_points_at_skill_md(self):
        data = miniyaml.loads(read("agents/openai.yaml"))
        self.assertEqual(data["name"], "dev-orchestra")
        self.assertEqual(data["instructions"]["file"], "../" + validate_skill.SKILL_PATH)

    def test_changelog_documents_the_current_version(self):
        front, _ = validate_skill.parse_frontmatter(read(validate_skill.SKILL_PATH))
        self.assertIn(str(front["version"]), read("CHANGELOG.md"))

    def test_cli_reference_documents_every_top_level_command(self):
        from orchestrator import cli

        reference = read("references/cli.md")
        parser = cli.build_parser()
        actions = [action for action in parser._actions if hasattr(action, "choices") and action.choices]
        commands = [c for action in actions for c in present(action.choices)]
        for command in commands:
            self.assertIn(command, reference, command)


class TestPortability(IsolatedCase):
    def test_skill_md_is_not_duplicated_anywhere(self):
        """Installers must point at SKILL.md, never copy its content."""
        marker = "You are the **Orchestrator**"
        hits = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = _walked_dirs(dirpath, dirnames)
            for name in filenames:
                if not name.endswith((".md", ".yaml", ".yml", ".sh", ".ps1")):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    if marker in handle.read():
                        hits.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(hits, [validate_skill.SKILL_PATH.replace("/", os.sep)])

    def test_installers_exist_for_both_hosts_and_platforms(self):
        for relative in (
            "install/install.sh",
            "install/install.ps1",
            "install/uninstall.sh",
            "install/uninstall.ps1",
            "bin/dev-orchestra",
            "bin/dev-orchestra.ps1",
        ):
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, relative)), relative)

    def test_posix_installer_handles_both_hosts(self):
        script = read("install/install.sh")
        self.assertIn("--codex", script)
        self.assertIn(".claude/skills", script)
        self.assertIn("BEGIN", script)  # idempotent marked block

    def test_installers_keep_a_project_install_out_of_the_host_repo(self):
        for relative in ("install/install.sh", "install/install.ps1"):
            self.assertIn("info/exclude", read(relative), relative)

    def test_every_script_has_the_antigravity_mode(self):
        """Either uninstaller has to undo either installer, so the names match."""
        flags = {".sh": ("--antigravity|--gemini",), ".ps1": ("[switch]$Antigravity", "[Alias('Gemini')]")}
        for relative in (
            "install/install.sh",
            "install/uninstall.sh",
            "install/install.ps1",
            "install/uninstall.ps1",
        ):
            script = read(relative)
            for token in (
                *flags[os.path.splitext(relative)[1]],
                ".gemini/config/plugins",
                ".agents/plugins",
                "# added by dev-orchestra install --antigravity",
                ".dev-orchestra-install",
                "Restart Antigravity",
            ):
                self.assertIn(token, script, "%s: %s" % (relative, token))

    def test_the_installers_refuse_what_the_validator_refuses(self):
        """The auto-load list is defined once in scripts/orchestrator/hosts.py and spelled
        out in both installers; a new entry goes in all three places."""
        from orchestrator import hosts

        self.assertIs(validate_skill.ANTIGRAVITY_AUTOLOAD, hosts.ANTIGRAVITY_AUTOLOAD)
        entries = hosts.ANTIGRAVITY_AUTOLOAD
        self.assertIn("for entry in %s; do" % " ".join(entries), read("install/install.sh"))
        self.assertIn('"$root"/agents/*.md', read("install/install.sh"))
        self.assertIn("@(%s)" % ", ".join("'%s'" % entry for entry in entries), read("install/install.ps1"))
        self.assertIn("-Filter '*.md'", read("install/install.ps1"))
        for relative in ("install/install.sh", "install/install.ps1"):
            self.assertIn(
                "Kept in step with ANTIGRAVITY_AUTOLOAD in scripts/orchestrator/hosts.py",
                read(relative),
                relative,
            )

    def test_the_claude_exclude_line_is_written_after_the_install(self):
        """A failed link or copy must leave the project's exclude file alone."""
        script = read("install/install.ps1")
        start = script.index("function Install-ClaudeSkill")
        body = script[start : script.index("\nfunction ", start + 1)]
        call = body.index("Add-ProjectGitExclude")
        self.assertGreater(call, body.index("Copy-Payload -Destination $dest"))
        self.assertGreater(call, body.index("New-Item -ItemType SymbolicLink"))

        script = read("install/install.sh")
        start = script.index("install_claude() {")
        body = script[start : script.index("\n}\n", start)]
        self.assertGreater(
            body.index('exclude_from_project_git "$dest"'), body.index('ln -s "$root" "$dest"')
        )

    def test_doctor_never_sees_the_real_home_in_a_test(self):
        from orchestrator import doctor

        home = doctor.user_home()
        self.assertTrue(os.path.abspath(home).startswith(os.path.abspath(self.tmp)), home)
        self.assertNotEqual(home, os.path.expanduser("~"))


class TestLinksInTheTree(IsolatedCase):
    """A project install made in the checkout puts a link to it inside it."""

    def setUp(self):
        super().setUp()
        self.link = ""

    def tearDown(self):
        # Before the temp tree goes, so that removing it cannot follow the link.
        if self.link and os.path.lexists(self.link):
            remove_link(self.link)
        super().tearDown()

    def make_tree(self):
        tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(tree, "real"))
        self.link = os.path.join(tree, "loop")
        make_dir_link(self.link, tree)
        return tree, self.link

    def test_is_link_sees_the_link_and_not_the_directory(self):
        tree, link = self.make_tree()
        self.assertTrue(is_link(link))
        self.assertFalse(is_link(os.path.join(tree, "real")))

    def test_the_walks_do_not_follow_it(self):
        tree, _ = self.make_tree()
        seen = []
        for dirpath, dirnames, _ in os.walk(tree):
            dirnames[:] = _walked_dirs(dirpath, dirnames)
            seen.append(os.path.relpath(dirpath, tree))
        self.assertEqual(sorted(seen), [".", "real"])

    def test_entry_point_needs_no_third_party_packages(self):
        """The whole package must import with only the standard library."""
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import orchestrator.cli, orchestrator.review, orchestrator.wizard,"
            "orchestrator.doctor, orchestrator.providers;"
            "print('ok')" % os.path.join(REPO_ROOT, "scripts")
        )
        result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gitignore_keeps_local_config_and_artifacts_out(self):
        ignore = read(".gitignore")
        for pattern in (".ai/", ".dev-orchestra.yaml", "__pycache__/"):
            self.assertIn(pattern, ignore)

    def test_repository_contains_no_absolute_local_paths(self):
        """Nothing machine-specific should be committable."""
        offenders = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            # `.venv` is the development environment CONTRIBUTING sets up: ignored by
            # git, and full of the machine it was made on.
            dirnames[:] = _walked_dirs(dirpath, dirnames)
            for name in filenames:
                if not name.endswith((".py", ".md", ".yaml", ".yml", ".sh", ".ps1", ".json")):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
                if _has_local_path(content):
                    offenders.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(offenders, [])


ORCHESTRATOR_DIR = os.path.join(SCRIPTS_DIR, "orchestrator")

#: Modules that must import without loading the provider registry, whose
#: bootstrap runs the user's adapters.
_REGISTRY_FREE = (
    "activity",
    "workspace",
    "optimization",
    "review_common",
    "config",
    "execution",
    "presets",
    "verified",
    "context",
    "review_snapshot",
    "review_coverage",
    "summary",
)

#: Imports allowed inside a function, as ``(file, outermost function, module
#: as written)``. Each needs a reason comment.
_ALLOWED = {
    # Windows only.
    ("clocks.py", "_windows_awake_clock", "ctypes"),
    ("execution.py", "pid_alive", "ctypes"),
    ("workspace.py", "_read_shared", "ctypes"),
    ("workspace.py", "_read_shared", "ctypes.wintypes"),
    ("workspace.py", "_read_shared", "msvcrt"),
    # Plugin loading.
    ("providers/__init__.py", "_bootstrap", ".agy"),
    ("providers/__init__.py", "_bootstrap", ".claude"),
    ("providers/__init__.py", "_bootstrap", ".codex"),
    ("providers/__init__.py", "_bootstrap", ".mock"),
    # The registry and presets both need config complete before they load.
    ("config.py", "user_providers_hint", ".providers"),
    ("config.py", "compose", ".presets"),
    ("config.py", "load", ".presets"),
    ("config.py", "validate", ".presets"),
    ("config.py", "validate", ".providers"),
    ("config.py", "_validate_role_options", ".providers"),
    # Initialisation order: the registry runs the user adapters.
    ("presets.py", "user_fit", ".providers"),
    ("presets.py", "_named_fit", ".providers"),
    ("presets.py", "installed_providers", ".providers"),
    ("presets.py", "suggestion_provider", ".providers"),
    # The policy loads the registry; summary must import without it.
    ("summary.py", "render_summary", ".config_policy"),
}


def _orchestrator_sources():
    """``(path relative to the package, parsed tree, lines)`` for every module."""
    for dirpath, dirnames, filenames in os.walk(ORCHESTRATOR_DIR):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            relative = os.path.relpath(path, ORCHESTRATOR_DIR).replace(os.sep, "/")
            yield relative, ast.parse(text, path), text.splitlines()


def _written(node):
    """The modules an import names, as written: ``from . import x`` names ``.x``."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    prefix = "." * node.level
    if node.module:
        return [prefix + node.module]
    return [prefix + alias.name for alias in node.names]


def _local_imports(tree):
    """``(outermost function, import node)`` for each import inside a function."""
    found = []

    def visit(node, outer):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, outer or child.name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                if outer:
                    found.append((outer, child))
            else:
                visit(child, outer)

    visit(tree, None)
    return found


def _has_reason(lines, lineno):
    """A comment on the import's line, or directly above it or above the run
    of imports it belongs to."""
    comment = lines[lineno - 1].partition("#")[2]
    if comment.strip():
        return True
    index = lineno - 2
    while index >= 0 and lines[index].strip().startswith(("import ", "from ")):
        index -= 1
    return index >= 0 and lines[index].strip().startswith("#")


def _import_problems(sources=None):
    """What breaks the rules for lazy and late imports, and every key seen.

    ``sources`` is what :func:`_orchestrator_sources` yields; the package by default.
    """
    problems = []
    seen = set()
    for relative, tree, lines in _orchestrator_sources() if sources is None else sources:
        for outer, node in _local_imports(tree):
            where = "%s:%d" % (relative, node.lineno)
            for module in _written(node):
                key = (relative, outer, module)
                seen.add(key)
                if key not in _ALLOWED:
                    problems.append("%s: %s imported inside %s" % (where, module, outer))
                elif not _has_reason(lines, node.lineno):
                    problems.append("%s: no reason given for importing %s here" % (where, module))
        for number, line in enumerate(lines, 1):
            if "noqa: E402" in line:
                problems.append("%s:%d: a late import" % (relative, number))
    return problems, seen


def _module_name(relative):
    """``providers/base.py`` -> ``orchestrator.providers.base``; a package is its ``__init__``."""
    parts = relative[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["orchestrator", *parts])


def _is_type_checking(test):
    """``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _run_on_import(body):
    """The imports a module runs when it is imported: its top level and the
    ``if`` and ``try`` blocks there, not an ``if TYPE_CHECKING:`` block or a
    function."""
    found = []
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        elif isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                found.extend(_run_on_import(node.body))
            found.extend(_run_on_import(node.orelse))
        elif isinstance(node, ast.Try):
            handlers = [handler.body for handler in node.handlers]
            for block in (node.body, *handlers, node.orelse, node.finalbody):
                found.extend(_run_on_import(block))
    return found


def _imported_modules(importer, is_package, node, modules):
    """The package's modules an import statement in ``importer`` names."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names if alias.name.split(".")[0] == "orchestrator"]
    if not node.level:
        name = node.module or ""
        return [name] if name.split(".")[0] == "orchestrator" else []
    base = importer if is_package else importer.rpartition(".")[0]
    for _ in range(node.level - 1):
        base = base.rpartition(".")[0]
    if node.module:
        return [base + "." + node.module]
    # ``from . import x`` names the module x when there is one, else the package.
    names = [base + "." + alias.name for alias in node.names]
    return [name if name in modules else base for name in names]


def _import_graph():
    """``{module: modules it imports when it is imported}`` over the package.

    Importing a submodule runs its package first, so an import also reaches
    each package above its target, other than the importer and those above it.
    """
    sources = []
    for relative, tree, _ in _orchestrator_sources():
        sources.append((_module_name(relative), relative.endswith("__init__.py"), tree))
    modules = {name for name, _, _ in sources}
    graph = {}
    for importer, is_package, tree in sources:
        parts = importer.split(".")
        own = {".".join(parts[:index]) for index in range(1, len(parts) + 1)}
        edges = set()
        for node in _run_on_import(tree.body):
            for target in _imported_modules(importer, is_package, node, modules):
                pieces = target.split(".")
                edges.update(".".join(pieces[:index]) for index in range(1, len(pieces) + 1))
        graph[importer] = sorted(edges - own)
    return graph


def _find_cycle(graph):
    """One cycle in ``graph`` as a path that ends where it starts, or None."""
    done = set()
    path = []

    def visit(node):
        path.append(node)
        for target in graph.get(node, ()):
            if target in path:
                return [*path[path.index(target) :], target]
            if target not in done:
                found = visit(target)
                if found:
                    return found
        path.pop()
        done.add(node)
        return None

    for node in sorted(graph):
        if node not in done:
            found = visit(node)
            if found:
                return found
    return None


def _python(code, env):
    return subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, env=env)


#: Run in a child for each module; prints whether the registry got loaded.
_IMPORT_ALONE = (
    "import sys; sys.path.insert(0, %r); import %s; print('orchestrator.providers' in sys.modules)"
)

_IMPORTED_ALONE = {}


def _imported_alone():
    """Each module imported first in a fresh interpreter with no user adapters,
    as ``{module: completed process}``. Run once and shared by the tests."""
    if not _IMPORTED_ALONE:
        names = [info.name for info in pkgutil.walk_packages(orchestrator.__path__, "orchestrator.")]
        env = {**os.environ, "DEV_ORCHESTRA_NO_USER_PROVIDERS": "1"}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = pool.map(lambda name: _python(_IMPORT_ALONE % (SCRIPTS_DIR, name), env), names)
            _IMPORTED_ALONE.update(zip(names, results, strict=True))
    return _IMPORTED_ALONE


_FUTURE = "from __future__ import annotations\n"

#: Modules a user adapter may import at its top level, each used once. Goes
#: after the ``__future__`` import, which has to stay the first statement.
_ADAPTER_IMPORTS = """
from orchestrator import config, optimization, presets, review_common

_USED = (config.KNOWN_ROLES, presets.DEFAULT, optimization.DEFAULT_LEVEL, review_common.SEVERITIES)
"""

#: Run in a child for each entry module, with user adapters enabled.
_LOAD_USER_ADAPTER = """\
import sys; sys.path.insert(0, %r)
import orchestrator.%s
from orchestrator import providers
assert "mycli" in providers.available_providers(), providers.user_provider_report()
assert not providers.user_provider_report()["errors"]
"""

#: Run in a child with user adapters enabled: ``summary`` imports without the registry.
_SUMMARY_ALONE = """\
import sys; sys.path.insert(0, %r)
import orchestrator.summary
assert "orchestrator.providers" not in sys.modules
assert not [name for name in sys.modules if name.startswith(%r)]
"""

#: Run in a child with user adapters enabled: ``presets`` imports without the
#: registry, and ``suggestion_provider`` reaches it only when asked about a user
#: adapter -- never for no candidate or a fitted one.
_PRESETS_ALONE = """\
import sys; sys.path.insert(0, %r)
from orchestrator import presets
assert "orchestrator.providers" not in sys.modules
assert presets.suggestion_provider(["claude"]) == "claude"
assert presets.suggestion_provider([]) is None
assert presets.suggestion_provider(["agy"]) is None
assert "orchestrator.providers" not in sys.modules
assert presets.suggestion_provider(["seated"]) == "seated"
assert "orchestrator.providers" in sys.modules
assert "orchestrator.config_policy" not in sys.modules
"""


class TestImportLayering(IsolatedCase):
    """Which module may import which, and when."""

    def test_every_module_imports_first(self):
        alone = _imported_alone()
        expected = ("orchestrator.summary", "orchestrator.optimization_report", "orchestrator.providers.agy")
        for name in expected:
            self.assertIn(name, alone)
        for name, result in alone.items():
            self.assertEqual(result.returncode, 0, "%s: %s" % (name, result.stderr))
            self.assertEqual(result.stderr, "", name)

    def test_the_registry_stays_unloaded(self):
        """Importing the registry runs the user's adapters; these must not."""
        alone = _imported_alone()
        for name in _REGISTRY_FREE:
            self.assertEqual(alone["orchestrator." + name].stdout.strip(), "False", name)

    def test_a_user_adapter_loads_on_every_entry_path(self):
        """The registry first loads while an entry module is half-built; an
        adapter importing the modules below it must still find them complete."""
        source = USER_ADAPTER_SOURCE.replace(_FUTURE, _FUTURE + _ADAPTER_IMPORTS, 1)
        self.assertIn("_USED", source)
        self.write_user_provider("layering", source)
        env = self.user_adapter_env()
        for entry in ("cli", "doctor", "wizard", "cli_config", "cli_run", "cli_review"):
            result = _python(_LOAD_USER_ADAPTER % (SCRIPTS_DIR, entry), env)
            self.assertEqual(result.returncode, 0, "%s: %s" % (entry, result.stderr))

    def user_adapter_env(self):
        """The environment of a child that loads the user adapters in the config home."""
        env = {key: value for key, value in os.environ.items() if key != "DEV_ORCHESTRA_NO_USER_PROVIDERS"}
        env["DEV_ORCHESTRA_HOME"] = self.config_home
        return env

    def test_summary_leaves_the_user_adapters_alone(self):
        self.write_user_provider("layering")
        result = _python(_SUMMARY_ALONE % (SCRIPTS_DIR, USER_MODULE_PREFIX), self.user_adapter_env())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_presets_reaches_the_policy_only_for_a_user_adapter(self):
        source = user_adapter_source("seated", 'preset_family = "big"', "verified")
        self.write_user_provider("seated", source)
        result = _python(_PRESETS_ALONE % SCRIPTS_DIR, self.user_adapter_env())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_cycle_at_module_level(self):
        graph = _import_graph()
        self.assertIn("orchestrator.providers.base", graph["orchestrator.providers"])
        cycle = _find_cycle(graph)
        self.assertIsNone(cycle, " -> ".join(cycle or ()))

    def test_lazy_and_late_imports_are_accounted_for(self):
        problems, seen = _import_problems()
        self.assertEqual(problems, [])
        self.assertEqual(sorted(_ALLOWED - seen), [], "listed, but no longer there")

    def test_moved_names_live_in_their_new_home(self):
        from orchestrator import (
            cli,
            cli_common,
            cli_review,
            cli_run,
            cli_state,
            cli_workflow,
            config,
            config_policy,
            optimization_render,
            providers,
            review,
            review_common,
            review_consolidation,
            review_coverage,
            review_fanout,
            review_snapshot,
            summary,
            wizard,
        )

        rendered = (
            "_revision_rows",
            "_counts",
            "_figure",
            "_usd",
            "_scorecard_counts",
            "_scorecard_spend",
            "_scorecard_per_accepted",
            "_scorecard_rates",
            "_scorecard_cost_row",
            "_scorecard_rows",
            "_paired_rows",
            "_panel_names",
            "_pair_exclusions",
            "_context_row",
            "_context_per_run_row",
            "_runs_row",
            "_tools_row",
        )
        homes = {
            cli_common: ("_ledger", "_refuse_if_exhausted", "_wrote_plan"),
            cli_review: (
                "_ran_since_last_round",
                "_reviewed_something",
                "_approved_as_recorded",
                "_design_final_pass",
                "_code_final_pass",
            ),
            cli_run: ("_detached_argv",),
            optimization_render: rendered,
        }
        for home, names in homes.items():
            for name in names:
                self.assertIs(getattr(cli, name), getattr(home, name), name)
                self.assertEqual(getattr(home, name).__module__, home.__name__, name)
        # Constants carry no __module__, so only where cli finds them is checked.
        constants = (
            "_OPT_ROW",
            "_SCORECARD_OUTCOMES",
            "_SCORECARD_BIAS",
            "_SCORECARD_FLOORS",
            "_SCORECARD_PAIRS",
            "_SCORECARD_ALONE",
            "_SCORECARD_EFFORT",
            "_SCORECARD_TOTAL",
        )
        for name in constants:
            self.assertIs(getattr(cli, name), getattr(optimization_render, name), name)
        self.assertIs(cli._REFUSAL_CAUSE, cli_workflow._REFUSAL_CAUSE)
        for name in ("_REFUSAL_CAUSE", "_detached_argv", *rendered, *constants):
            self.assertFalse(hasattr(cli_state, name), name)
        self.assertEqual(review_common.accepted_findings.__module__, "orchestrator.review_common")
        self.assertIs(review.accepted_findings, review_common.accepted_findings)
        self.assertIs(wizard.render_summary, summary.render_summary)
        self.assertIs(cli_run._describe_spec, summary._describe)
        for name in ("_describe", "merged"):
            self.assertFalse(hasattr(wizard, name), name)
        moved = {
            review_coverage: (
                "snapshot_reviewers",
                "coverage_state",
                "unverified_phrase",
                "coverage_headline",
                "coverage_advice",
            ),
            review_snapshot: ("snapshot_stamp", "current_snapshot_stamp"),
        }
        for home, names in moved.items():
            for name in names:
                self.assertIs(getattr(review, name), getattr(home, name), name)
                self.assertEqual(getattr(home, name).__module__, home.__name__, name)
        self.assertFalse(hasattr(cli, "_coverage_advice"))
        self.assertFalse(hasattr(cli_review, "_coverage_advice"))
        for name in ("_coverage_line", "snapshot_reviewers", "current_snapshot_stamp"):
            self.assertFalse(hasattr(review_consolidation, name), name)
        for name in ("_stamp", "_snapshot_sha"):
            self.assertFalse(hasattr(review_fanout, name), name)
        policy = (
            "RawArgs",
            "_string_args",
            "read_only_raw_args",
            "_tier_provider_path",
            "_project_refused",
            "_project_seat_refused",
            "_project_provider_seats",
            "project_provider_refusals",
            "reviewer_provider_refusals",
            "project_seat_refusal",
            "project_reviewer_refusal",
            "_seat_provider_path",
            "_all_refused",
            "_by_key",
            "project_raw_arg_refusals",
            "reviewer_raw_arg_refusals",
            "read_only_enforcement_warnings",
            "_enforcement_warned_seats",
            "reviewer_enforcement_warnings",
            "project_write_refusals",
            "read_only_arg_warnings",
        )
        for name in policy:
            self.assertEqual(getattr(config_policy, name).__module__, "orchestrator.config_policy", name)
            self.assertFalse(hasattr(config, name), name)
        # The seat walk is configuration shape, so it stays with ``merge_tier``.
        for name in ("SeatRun", "role_seats", "_reviewer_seats", "_read_only_seats"):
            self.assertEqual(getattr(config, name).__module__, "orchestrator.config", name)
        self.assertEqual(config.WRITE_ROLES, ("implementer", "review_fixer"))
        # The warned-provider predicate reads only the registry.
        for name in ("_warned_provider", "warned_provider"):
            self.assertEqual(getattr(providers, name).__module__, "orchestrator.providers", name)
            self.assertFalse(hasattr(config, name), name)
        self.assertFalse(hasattr(config_policy, "warned_provider"))
        self.assertEqual(config.default_reviewer_family.__module__, "orchestrator.config")
        self.assertFalse(hasattr(config_policy, "default_reviewer_family"))


def _source(relative, text):
    """One entry shaped like what :func:`_orchestrator_sources` yields."""
    return relative, ast.parse(text), text.splitlines()


class TestImportCheckers(unittest.TestCase):
    """The helpers behind :class:`TestImportLayering`, on inputs with a known answer."""

    def test_an_unlisted_local_import_is_reported(self):
        text = "def helper():\n    # Lazy for speed.\n    import json\n"
        problems, seen = _import_problems([_source("clocks.py", text)])
        self.assertEqual(problems, ["clocks.py:3: json imported inside helper"])
        self.assertEqual(seen, {("clocks.py", "helper", "json")})

    def test_a_listed_import_without_a_reason_is_reported(self):
        text = "def _windows_awake_clock():\n    import ctypes\n"
        problems, _ = _import_problems([_source("clocks.py", text)])
        self.assertEqual(problems, ["clocks.py:2: no reason given for importing ctypes here"])

    def test_a_listed_import_with_a_reason_passes(self):
        text = "def _windows_awake_clock():\n    # Windows only.\n    import ctypes\n"
        problems, _ = _import_problems([_source("clocks.py", text)])
        self.assertEqual(problems, [])

    def test_imports_under_a_module_level_if_run_on_import(self):
        text = (
            "import a\n"
            "if sys.platform == 'win32':\n"
            "    import b\n"
            "elif x:\n"
            "    import c\n"
            "else:\n"
            "    import d\n"
            "if TYPE_CHECKING:\n"
            "    import e\n"
            "else:\n"
            "    import f\n"
            "if typing.TYPE_CHECKING:\n"
            "    import g\n"
            "try:\n"
            "    import h\n"
            "except ImportError:\n"
            "    import i\n"
            "def later():\n"
            "    import j\n"
        )
        found = _run_on_import(ast.parse(text).body)
        self.assertEqual([name for node in found for name in _written(node)], list("abcdfhi"))

    def _resolve(self, importer, statement, modules, is_package=False):
        node = ast.parse(statement).body[0]
        return _imported_modules(importer, is_package, node, modules)

    def test_relative_imports_resolve(self):
        modules = {"orchestrator", "orchestrator.x", "orchestrator.y", "orchestrator.providers.x"}
        self.assertEqual(self._resolve("orchestrator.a", "from . import x", modules), ["orchestrator.x"])
        # A name rather than a module is the package itself.
        self.assertEqual(self._resolve("orchestrator.a", "from . import name", modules), ["orchestrator"])
        self.assertEqual(
            self._resolve("orchestrator.providers.base", "from .. import y", modules), ["orchestrator.y"]
        )
        self.assertEqual(self._resolve("orchestrator.a", "from .x import y", modules), ["orchestrator.x"])
        self.assertEqual(
            self._resolve("orchestrator.providers", "from .x import y", modules, is_package=True),
            ["orchestrator.providers.x"],
        )
        self.assertEqual(self._resolve("orchestrator.a", "import json", modules), [])

    def test_find_cycle(self):
        self.assertEqual(_find_cycle({"a": ["b"], "b": ["a"]}), ["a", "b", "a"])
        self.assertEqual(_find_cycle({"a": ["b"], "b": ["c"], "c": ["b"]}), ["b", "c", "b"])
        self.assertIsNone(_find_cycle({"a": ["b", "c"], "b": ["c"], "c": []}))


if __name__ == "__main__":
    unittest.main()
