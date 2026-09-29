"""`doctor` on an Antigravity install that is this checkout.

The installer refuses to link a checkout whose root holds something
Antigravity loads on its own, but only when it links. `doctor` makes the same
judgement later, when a checkout has added one.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import IsolatedCase, has_git, make_dir_link, remove_link

from orchestrator import cli, doctor
from orchestrator import config as config_mod


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def same(path):
    return os.path.normcase(os.path.realpath(path))


def posix_non_root():
    return os.name != "nt" and os.geteuid() != 0


class TestDoctorAntigravity(IsolatedCase):
    def setUp(self):
        super().setUp()
        # Every role on mock, so the baseline has no problems and --strict
        # exits 0 even with no CLI installed.
        role = {"provider": "mock", "model": {"family": "small", "version": "latest"}}
        data = config_mod.default_config()
        data.update(orchestrator=role, architect=role, implementer=role, review_fixer=role)
        data["reviewers"] = [config_mod.make_reviewer("gen", "mock", "small")]
        config_mod.write_config_file(config_mod.global_config_path(), data)
        self.root = self.make_root(os.path.join(self.tmp, "checkout"))
        self.use_root(self.root)
        self.links = []

    def tearDown(self):
        # Before the temp tree goes: removing the tree must not follow a link.
        for link in reversed(self.links):
            if os.path.lexists(link):
                remove_link(link)
        super().tearDown()

    def make_root(self, path, skill=True):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "plugin.json"), "w", encoding="utf-8") as handle:
            handle.write('{"name": "dev-orchestra"}\n')
        if skill:
            skill_dir = os.path.join(path, "skills", "dev-orchestra")
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nname: dev-orchestra\n---\n")
        return path

    def use_root(self, path):
        original = doctor.PLUGIN_ROOT
        setattr(doctor, "PLUGIN_ROOT", path)
        self.addCleanup(setattr, doctor, "PLUGIN_ROOT", original)

    def touch(self, relative, root=None, directory=False):
        path = os.path.join(root or self.root, relative)
        if directory:
            os.makedirs(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{}\n")

    def project_location(self, base=None):
        return os.path.join(base or self.project, ".agents", "plugins", "dev-orchestra")

    def global_location(self):
        return os.path.join(self.os_home, ".gemini", "config", "plugins", "dev-orchestra")

    def link(self, location, target=None):
        os.makedirs(os.path.dirname(location), exist_ok=True)
        make_dir_link(location, target or self.root)
        self.links.append(location)
        return location

    def report(self):
        code, out, err = run_cli("doctor", "--fast", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def strict(self):
        code, out, _ = run_cli("doctor", "--fast", "--strict")
        return code, out

    def antigravity_problems(self, payload):
        return [
            problem for problem in payload["problems"] if "Antigravity" in problem or "agents/" in problem
        ]

    def assert_live(self, payload, *locations):
        self.assertEqual(
            [same(path) for path in payload["antigravity"]["live"]], [same(p) for p in locations]
        )

    def test_nothing_installed_is_no_problem(self):
        code, out = self.strict()
        self.assertEqual(code, 0, out)
        self.assertIn("No problems found.", out)
        payload = self.report()
        self.assertEqual(payload["antigravity"]["live"], [])
        self.assertEqual(payload["antigravity"]["autoload"], [])

    def test_a_clean_linked_checkout_is_no_problem(self):
        location = self.link(self.project_location())
        code, out = self.strict()
        self.assertEqual(code, 0, out)
        self.assertIn("No problems found.", out)
        payload = self.report()
        self.assert_live(payload, location)
        self.assertEqual(payload["antigravity"]["autoload"], [])

    def test_what_a_linked_checkout_would_load_is_a_problem(self):
        location = self.link(self.project_location())
        self.touch("hooks.json")
        self.touch("rules", directory=True)
        self.touch("agents/x.md")
        self.touch("agents/y.MD")
        self.touch("agents/openai.yaml")
        payload = self.report()
        problems = self.antigravity_problems(payload)
        self.assertEqual(len(problems), 1, payload["problems"])
        for name in ("hooks.json", "rules/", "agents/x.md", "agents/y.MD"):
            self.assertIn(name, problems[0])
        self.assertNotIn("openai.yaml", problems[0])
        self.assertIn(os.path.join(".agents", "plugins", "dev-orchestra"), problems[0])
        self.assert_live(payload, location)
        self.assertEqual(
            payload["antigravity"]["autoload"], ["hooks.json", "rules", "agents/x.md", "agents/y.MD"]
        )
        code, out = self.strict()
        self.assertEqual(code, 1)
        self.assertIn("\nProblems\n", out)
        self.assertIn("would also load hooks.json", out)

    def test_the_global_location_is_checked(self):
        location = self.link(self.global_location())
        self.touch("mcp_config.json")
        payload = self.report()
        self.assert_live(payload, location)
        problems = self.antigravity_problems(payload)
        self.assertEqual(len(problems), 1, payload["problems"])
        self.assertIn(location, problems[0])
        self.assertIn("mcp_config.json", problems[0])

    def test_a_checkout_sitting_at_the_location_is_checked(self):
        """A clone there is as live as a link to one."""
        location = self.project_location()
        self.make_root(location)
        self.use_root(location)
        self.touch("hooks.json", root=location)
        payload = self.report()
        self.assert_live(payload, location)
        self.assertEqual(len(self.antigravity_problems(payload)), 1, payload["problems"])

    def test_a_copy_install_is_not_this_checkout(self):
        location = self.project_location()
        os.makedirs(location)
        self.touch(".dev-orchestra-install", root=location)
        self.touch("hooks.json")
        payload = self.report()
        self.assertEqual(payload["antigravity"]["live"], [])
        self.assertEqual(self.antigravity_problems(payload), [])

    def test_a_link_elsewhere_or_a_dangling_one_is_ignored(self):
        self.touch("hooks.json")
        other = self.make_root(os.path.join(self.tmp, "other"))
        self.link(self.project_location(), other)
        missing = os.path.join(self.tmp, "gone")
        os.makedirs(missing)
        self.link(self.global_location(), missing)
        shutil.rmtree(missing)
        code, out = self.strict()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.report()["antigravity"]["live"], [])

    @unittest.skipUnless(has_git(), "git is not installed")
    def test_the_project_location_is_at_the_repository_root(self):
        self.init_git_repo()
        subdirectory = os.path.join(self.project, "src")
        os.makedirs(subdirectory)
        location = self.link(self.project_location())
        self.touch("hooks.json")
        os.chdir(subdirectory)
        payload = self.report()
        self.assert_live(payload, location)
        self.assertEqual(len(self.antigravity_problems(payload)), 1, payload["problems"])

    def test_a_root_without_the_skill_is_still_checked(self):
        bare = self.make_root(os.path.join(self.tmp, "bare"), skill=False)
        self.use_root(bare)
        self.link(self.project_location(), bare)
        self.touch("hooks.json", root=bare)
        self.assertEqual(len(self.antigravity_problems(self.report())), 1)

    @unittest.skipUnless(posix_non_root(), "needs a POSIX user that mode 000 stops")
    def test_an_unreadable_agents_directory_is_a_problem(self):
        self.link(self.project_location())
        self.touch("hooks.json")
        agents = os.path.join(self.root, "agents")
        os.makedirs(agents)
        os.chmod(agents, 0)
        try:
            payload = self.report()
            self.assertTrue(
                any(problem.startswith("cannot list agents/ in") for problem in payload["problems"]),
                payload["problems"],
            )
            self.assertTrue(any("would also load hooks.json" in p for p in payload["problems"]))
            code, _ = self.strict()
            self.assertEqual(code, 1)
        finally:
            # Before tearDown removes the tree.
            os.chmod(agents, 0o755)


if __name__ == "__main__":
    unittest.main()
