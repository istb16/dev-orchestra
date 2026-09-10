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

No dependencies to install. Python 3.9+ and `git` are enough. Linting uses
[ruff](https://docs.astral.sh/ruff/) if you have it (`ruff check .`, then
`ruff format .`); CI runs it but will not block on its absence locally.

CI pins an exact ruff version so a formatter release cannot turn every PR red.
Bumping it is a deliberate, manual change: update `.github/workflows/ci.yml`,
run `ruff format .` locally with the new version, and commit the result
separately from any behaviour change.

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

Your machine probably has them, so check before pushing:

```bash
DEV_ORCHESTRA_TEST_ASSUME_NO_CLI=1 python -m unittest discover -s tests -t tests
```

That hides both provider CLIs and is exactly what CI does. The `mock` provider
has affordances for the awkward paths: always "installed",
with `DEV_ORCHESTRA_MOCK_FAIL` for run failures and the `unresolvable` model
family for resolution failures.

**4. Reviewers stay read-only and independent.** If a change could let a
reviewer edit files or see another reviewer's output, it needs a very good
reason and a test proving the boundary still holds.

**5. Never print or persist a credential.** New output paths go through
`redact()`. `doctor` reports credential presence, never values.

## Where things go

| Change | Location |
| --- | --- |
| Orchestration policy (when to run a stage) | `SKILL.md` |
| Long-form explanation | `references/` — keep `SKILL.md` under 500 lines |
| A new CLI | `scripts/orchestrator/providers/` + `register()` — see `references/providers.md` |
| Config schema | `config.py` (defaults **and** `validate`) + `references/configuration.md` |
| Review parsing/dedup | `review.py` |

`SKILL.md` is the single source of truth for skill content. Installers point at
it; they never copy it.

## Pull requests

- One logical change per PR.
- Include tests. Bug fixes should include a test that fails before the fix.
- Update the docs in the same PR — an undocumented flag does not exist.
- Add a `CHANGELOG.md` entry under `## [Unreleased]`.
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

[Semantic versioning](https://semver.org/). The public surface is the config
schema, the CLI commands and flags, and the `.ai/` artifact formats.

1. Move `Unreleased` entries under a new version heading with a date.
2. Bump `version:` in `SKILL.md` and `__version__` in
   `scripts/orchestrator/__init__.py` and `cli.py` (validation checks they match).
3. Tag `vX.Y.Z`.

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
