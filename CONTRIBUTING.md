# Contributing

Thanks for helping. This project has a small surface and a few firm rules —
most of them exist to keep it working as the underlying CLIs change.

## Getting set up

```bash
git clone https://github.com/istb16/dev-orchestra.git
cd dev-orchestra
python -m unittest discover -s tests -t tests
python scripts/validate_skill.py
```

No dependencies to install. Python 3.11+ and `git` are enough. Commands here
are written `python`; read it as `python3` where that is the only name the
interpreter has. Linting uses [ruff](https://docs.astral.sh/ruff/) if you have
it (`ruff check .`, then `ruff format .`); CI runs it but will not block on its
absence locally.

CI runs the same tests with `python tests/run_parallel.py -v`, which hands one
test class at a time to a pool of worker processes (`-j N` or
`DEV_ORCHESTRA_TEST_JOBS` sets how many; the default is the CPU count up to
4). It is optional locally and the command above stays the reference. Classes
from different modules run at the same time, so a test class that does not
derive from `IsolatedCase` must not write outside its own temporary
directory, change the working directory, or leave the environment changed.

The tools for working on the code -- ruff and
[Pyright](https://microsoft.github.io/pyright/) -- are pinned in
`requirements-dev.txt`. Nothing at runtime needs them; set them up once in a
virtual environment, which `.gitignore` already leaves out:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip
```

Pyright's settings are in `pyproject.toml`: it checks `scripts/` and `tests/`
against Python 3.11 for every platform at once, so a Unix-only branch is checked on
Windows too. Run `pyright` from the repository root; it has to report no
errors. The Node.js it runs on comes with it, from its `nodejs` extra.

In a test, a value that may be `None` but that the test has just made sure of
is narrowed where it is named: `assert job is not None` right after
`job = ...`. Where it has no name -- a call used in place --
`present(value)` from `tests/helpers.py` does the same inline. A
test that passes a wrong type on purpose -- `None` where a string goes, to
check what happens -- says why on the line above and silences that one line
with `# pyright: ignore[<rule>]`. A test that swaps a function out uses
`setattr()`, which ruff is told not to flag in `tests/`.

When the checker cannot follow a narrowing the code relies on, prefer making
it followable -- read the value into a variable before testing its type --
over `cast()`, and keep a `cast()` to the places where the reason fits in a
one-line comment beside it. Neither may change what the code does.

CI installs exactly these versions, so a formatter or checker release cannot
turn every PR red. Bumping one is a deliberate, manual change: update
`requirements-dev.txt`, run `ruff format .` and `pyright` locally with the new
version, and commit the result separately from any behaviour change.

### Running the plugin from a clone

Point any of the three hosts at the clone instead of at GitHub:

```bash
claude plugin validate .                    # manifest check, --strict in CI
claude plugin marketplace add "$PWD"
claude plugin install dev-orchestra@dev-orchestra

codex plugin marketplace add "$PWD"
codex plugin add dev-orchestra@dev-orchestra

./install/install.sh --antigravity --project .   # Antigravity, for this repository only
./install/install.sh --antigravity               # or for every workspace
```

Every host runs the same skill from its own plugin directory, and everything
the skill needs — `scripts/`, `references/`, `bin/` — is inside it. Claude
Code and Codex copy the plugin into their cache (`~/.claude/plugins/cache/…`,
`~/.codex/plugins/cache/…`), so no path points back at the clone and a change
needs a reinstall. Antigravity loads the link itself, so a change is live at
its next restart. `--project .` puts the link in `.agents/plugins/` inside
this repository and keeps it out of `git status` through `.git/info/exclude`;
`./install/uninstall.sh --antigravity --project .` removes both. Because the
link exposes the working tree, a branch you check out is what Antigravity
loads: look at an untrusted branch with `--copy` or from a separate worktree.
Antigravity can also load a checkout kept elsewhere, live, through a
`plugins.json` in a customization root (`~/.gemini/config/plugins.json`, or
`.agents/plugins.json` in a project). Its entry names the parent directory,
with `include_only` naming the plugin directory:
`{"entries":[{"path":"C:/Projects","include_only":["dev-orchestra"]}]}`. The
installers do not write it and `doctor` does not read it, so neither the
installer's auto-load refusal nor the `doctor` check covers that route. A
`plugins.json` at this repository's root is still refused, because it would
turn the plugin root into a customization root.

`claude plugin details dev-orchestra` lists what was actually loaded. Re-run
`python scripts/validate_skill.py` after touching a manifest: it checks every
host's manifests against the skill they ship.

## The rules that matter

**1. Never hard-code a dated model id.** Not in defaults, not in the wizard, not
in a fallback list, not in a test fixture that looks like real config. Families
and aliases only. `tests/test_config.py` enforces this for the defaults.

**2. Never write a CLI flag from memory.** Run the CLI's `--help` and check.
When you touch an adapter, update the version you verified against in its module
docstring, e.g. *"Verified against `codex` 0.154.x"*.

**3. Tests must not invoke a real CLI, or depend on one being installed.** Use
the `mock` provider, or patch `_capture` / `which`. The suite has to pass on a
machine with neither `claude` nor `codex` installed — that is what CI runs on.

Your machine probably has them, and the suite acts as if it did not:
`tests/helpers.py` hides `claude`, `codex` and `agy` from every `IsolatedCase`,
and `tests/cli_guard/` refuses to start any of them, with the
`FileNotFoundError` a machine without them gives. It finds where they are
installed once (each hit on `PATH`, where its links lead, and for an npm shim
the script and package it runs), and refuses a command that names one, runs one
of those scripts through `node`, or reaches one through `cmd /c`, `sh -c`, `pwsh
-Command`, `env`, `npx` or a `&&` list. It guards `subprocess`, `os.system`,
`os.spawn*`, `os.posix_spawn*`, `os.exec*` and `os.startfile` in the test
processes, and, as their `sitecustomize`, in the Python processes they start. It
does not reach a launch through `_winapi.CreateProcess` or another direct system
call, nor a Python process started with `-I`, `-E` or `-S` or with an
environment that drops `PYTHONPATH`; a test that starts one of those must not
let it run a CLI.

So plain `python -m unittest discover -s tests -t tests` is exactly what CI
runs; `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI` is still accepted and changes nothing.
A test that needs a CLI to answer writes a fake, names it by its path in the
command, and passes that path to `IsolatedCase.allow_cli()`; a bare `claude`
stays refused. The `mock` provider has affordances for the awkward paths: always
"installed", with `DEV_ORCHESTRA_MOCK_FAIL` for run failures and the
`unresolvable` model family for resolution failures.

Which leaves a gap, and it is worth naming: **nothing in the suite has ever run
a real CLI.** That is not a hypothetical cost. `CodexProvider.run` raised
`TypeError` on every call for weeks -- `idle_timeout` was added to
`Provider.run` and not to the override -- while 654 tests stayed green, because
the mock overrides `run` outright and exercises no adapter's signature but its
own. A configured `sandbox: read-only` was being dropped just as quietly, which
is worse: a crash gets fixed.

So there is a second script, outside the suite, that does start the real
things:

```bash
python scripts/smoke_live.py            # every installed CLI
python scripts/smoke_live.py --provider codex
python scripts/smoke_live.py --model agy=<id agy models lists>
```

It spends a small number of real tokens on the questions only a real process
can answer: does the command line still work, does the CLI still report what a
run cost in a shape the parser reads, and does a read-only mode still actually
refuse to write -- checked by asking for a file and then looking for it, not by
believing what the agent said about itself -- and, for an adapter that declares
its read-only runs confined (Claude), does a run still stay inside its working
directory while the widening arguments still widen it. Which checks an adapter
is asked is declared on the adapter, not listed in the script; see "Taking part
in the live check" in `references/providers.md`.

`--model <provider>=<model>` runs that provider's checks on a model you name
instead of the CLI default -- a Claude model inside agy, say -- while every
other provider in the run uses its default. The model is checked before any
token is spent only as strictly as the adapter resolves it: claude passes any
`claude-*` id through, so a typo there fails at the first run. A provider named
in `--model` writes no live-check record and no resume pass, since both vouch
for the CLI's default model; a resume failure is still recorded, because a
breach is evidence whatever the model.

Run it before a release, after touching an adapter, and after bumping a CLI. A
failure there is the adapter and the CLI having drifted apart: read the CLI's
`--help` before changing anything. A `SKIP` is a check that could not be run --
the symlink check on Windows without the symlink privilege -- and also exits 1:
run it where symlinks can be created before calling the run clean.

Each run records, per provider, which CLI version it checked and which checks
failed or were skipped (names only) in `verified/<provider>-smoke.json` in the
config directory. `doctor` shows it on a `Live check:` line and adds a note
when the installed version has not been through the script on this machine --
which is exactly when a CLI update can have drifted. It is not scheduled in CI,
which has no API keys.

**4. Reviewers stay read-only and independent.** If a change could let a
reviewer edit files or see another reviewer's output, it needs a very good
reason and a test proving the boundary still holds, and the built command
still carries the allowlist and `--restricted`.

**5. Never print or persist a credential.** New output paths go through
`redact()`. `doctor` reports credential presence, never values.

## Where things go

| Change | Location |
| --- | --- |
| Orchestration policy (when to run a stage) | `skills/dev-orchestra/SKILL.md` |
| Long-form explanation | `references/` — keep the skill under 500 lines |
| Plugin packaging | `.claude-plugin/`, `.codex-plugin/`, `.agents/plugins/`, and the root `plugin.json` for Antigravity |
| A new CLI | `scripts/orchestrator/providers/` + `register()` — see `references/providers.md` (a user's own adapter goes in `<config dir>/providers/` instead, without a change here) |
| Config schema | `config.py` (defaults **and** `validate`) + `references/configuration.md` |
| Read-only seat / write-role refusals and enforcement warnings | `config_policy.py` |
| A command | its `cli_*.py` module (`cli_review.py` for `review …`, `cli_state.py` for `state`/`budget`/`tokens`, …), and its arguments in `cli.py` |
| The wording of `optimization report` | `optimization_render.py`, which builds the lines; the figures come from `optimization_report.py`, and `cli_state.py` reads the run logs and prints |
| Review snapshot, fan-out, parsing, consolidation, coverage wording | `review_snapshot.py`, `review_fanout.py`, `review_parsing.py`, `review_consolidation.py`, `review_coverage.py`; constants and prompt templates in `review_common.py` |

`cli.py` re-exports every name its command modules define, so `cli.name`
keeps working. `review.py` re-exports the public names of the `review_*`
modules, so `review.name` keeps working for those; a test imports an
underscore helper from its own module: `review_snapshot._diff`, not
`review._diff`. A test that replaces a function has to replace it where it is
looked up: `cli_run._out`, not `cli._out`.

Imports go at the top of the module. `tests/test_skill.py` fails on an import
inside a function unless it is on its allow-list with a comment saying why
(Windows-only modules, the provider registry), and on any import cycle at
module level. When two modules need the same helper, move it down to a module
both can import.

`skills/dev-orchestra/SKILL.md` is the single source of truth for skill content.
Installers point at it; they never copy it. It sits under `skills/` because
that is the only place Codex looks — a `SKILL.md` at the repository root would
be invisible there and a second copy of the skill for Claude Code.

The repository is also the plugin *and* its marketplace: `.claude-plugin/` and
`.codex-plugin/` describe the same package for Claude Code and Codex, and both
marketplace manifests source it from `./`. For Antigravity the repository root
is the plugin directory, marked by the root `plugin.json`, which carries only
`$schema`, `name` and `description`, the fields its published schema allows,
and no version. `python scripts/validate_skill.py` checks that
the manifests agree with each other and with the skill, and that the root holds
nothing else Antigravity would load; `claude plugin validate . --strict`
checks the Claude manifests against the host's own schema.

## Pull requests

- One logical change per PR.
- Include tests. Bug fixes should include a test that fails before the fix.
- Update the docs in the same PR — an undocumented flag does not exist.
- A change to `references/*.md` changes its Japanese translation in
  `docs/ja/references/` too: bring the translation up to date, then run
  `python scripts/stamp_translation.py docs/ja/references/<name>.md`. The
  English stays authoritative, and `tests/test_docs.py` fails until the
  translation's header names the English as it is now. If you cannot write
  the Japanese yourself, say so in the PR so someone else can; do not stamp a
  translation you did not update.
- The long references (`reviews`, `cli`, `configuration`, `limits`,
  `workflow`, `providers`) and their translations open with a table of
  contents. After adding, renaming or removing a `##` or `###` heading in one,
  run `python scripts/doc_contents.py <file>` on it and on its translation;
  `tests/test_docs.py` fails while the contents are out of date.
- Add a `CHANGELOG.md` entry under `## [Unreleased]`. Keep it to a few lines
  for someone upgrading: what changed, anything they have to do, and what it
  means for compatibility (a key, flag or file that is new, or behaviour that
  differs), ending with the issue number. The reasons and the detail go in the
  reference that covers the topic, not in the entry; name that reference
  instead of repeating it. Entries in released sections stay as they were
  written.
- Say which platform you tested on, and which CLI versions if you touched an
  adapter.

CI runs lint, the test suite, and skill validation on Linux, macOS and Windows.

## Commit messages

Short imperative subject, body explaining *why* when it is not obvious:

```
Resolve Codex models by omitting -m instead of guessing

The Codex CLI has no model-list command, so any name we synthesised
was a guess. Omitting the flag lets the CLI use its own current
default, which is what "recommended-coding, latest" actually means.
```

## Releases

[Semantic versioning](https://semver.org/) over the surface listed under
"Compatibility" in `README.md`. The `.ai/` artifacts change by addition only;
the rule, and what a change that breaks it needs, is under "How the formats
change" in `references/workflow.md`. A change to the `Provider` base class is
not covered: it may ship in a minor version, and its entry begins
**User adapters**.

1. Run `python scripts/smoke_live.py` against the installed CLIs. The suite
   cannot tell you an adapter has drifted; this can.
2. If that run recorded a claude or codex version it had not recorded before
   (it says so on its `resume verified` line and prints the entry), copy the
   entry into `VERIFIED_RESUME` in that adapter's
   `scripts/orchestrator/providers/<name>.py`: the version string, the date,
   the read-only mechanism, the check names, and in `source` the date and the
   environment it was run in. Entries come only from runs on the CLI's default
   model, the only runs that write a resume pass: a model that declines to read
   or write passes "not read" and "refused to write" on the filesystem alone.
   Never add a version that failed. A user on a
   version older than every entry has every `--resume` run fresh until they run
   the script themselves; one on a newer version resumes on trust.
3. Changing a value in `default_config()` is a change to the effective
   configuration of everyone who never set it, so record it under `Changed`
   with the old and the new value.
4. Move `Unreleased` entries under a new version heading with a date, and fix
   the reference links at the foot of the file: point `[Unreleased]` at
   `compare/vX.Y.Z...HEAD` and add `[X.Y.Z]: .../compare/vW...vX.Y.Z`. Without
   the definition the new heading renders as literal `[X.Y.Z]` on GitHub.
5. Bump the version everywhere it is written down:
   `skills/dev-orchestra/SKILL.md`, `agents/openai.yaml`, the two plugin
   manifests (`.claude-plugin/` and `.codex-plugin/`), both entries in the
   Claude marketplace file, and `__version__` in
   `scripts/orchestrator/__init__.py` and `cli.py`. Seven files;
   `python scripts/validate_skill.py` refuses if any of them disagree, so run
   it rather than counting.
6. Tag `vX.Y.Z`.

## Reporting a security issue

Please do **not** open a public issue. Use GitHub's private vulnerability
reporting on this repository, or contact a maintainer directly. Include the
version, the CLIs involved, and a reproduction if you have one.

Especially interested in: any path where a credential could reach `.ai/`, a
report, or a log; and any way a reviewer could gain write access to the working
tree.

## Code of conduct

Be straightforward and civil. Assume the other person read the code and
disagreed for a reason; ask what it was.
