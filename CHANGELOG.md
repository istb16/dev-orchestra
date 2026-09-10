# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The public surface covered by that promise is: the configuration schema, the
`dev-orchestra` commands and flags, and the `.ai/` artifact formats.

## [Unreleased]

### Changed

- Renamed every user-visible identifier to `dev-orchestra`: the skill name, the
  `dev-orchestra` command, the config directory, the `.dev-orchestra.yaml`
  project override, and the `DEV_ORCHESTRA_*` environment variables. Three
  competing names for one tool was one too many.

### Fixed

- Two tests silently depended on `claude` and `codex` being installed, so they
  passed locally and failed on every CI platform. Both now drive the `mock`
  provider instead, and `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI=1` reproduces the CI
  environment on a developer machine that has the real CLIs.
- `doctor` now reports options that a read-only role will ignore even when the
  provider CLI is missing: whether an option takes effect is a fact about the
  configuration, not about what happens to be installed.

### Added

- **Role options.** Each role can carry provider-specific `options`:
  `permission_mode` and `args` for Claude, `sandbox` / `approve` / `args` for
  Codex. The adapter validates them against what the installed CLI actually
  accepts, so `config validate` catches a typo instead of a run failing later.
  This exists because the default `acceptEdits` auto-approves file edits but
  not shell commands, which can stop an Implementer from running the tests it
  was told to run. Options that would loosen a read-only stage are ignored and
  reported by `doctor` — the architect and every reviewer stay read-only.
- **Duplicate candidates.** Findings from different reviewers that quote the
  same code are reported as possible duplicates, ranked by how rare the shared
  code token is, for the orchestrator to confirm as `duplicate` during triage.
  Auto-merge stays conservative: measured on real two-provider output over one
  diff, a confirmed duplicate pair scored 0.03 text similarity while an
  unrelated pair scored 0.29, so no threshold on wording can separate them —
  and a wrong merge hides a bug, while a missed one only costs redundant work.
- Japanese README (`README.ja.md`), linked from the English one and checked by
  skill validation.

## [0.1.0] - 2026-09-10

First release.

### Added

- **Orchestrated workflow skill** (`SKILL.md`) covering design, implementation,
  test, independent review, triage, fix and re-test — with stage selection left
  to the orchestrator rather than made mandatory.
- **Provider adapters** for Claude Code (`claude` 2.1.x) and Codex CLI
  (`codex` 0.154.x), plus an offline `mock` adapter for tests and dry runs.
  Adding a CLI is one module and one `register()` call.
- **Model handling without dated snapshot ids**: configuration stores a family
  plus `version: latest`, resolved at run time against the installed CLI.
  Claude aliases come from the CLI's own `--model` help; the Codex
  `recommended-coding` family resolves by omitting `-m`. Unverifiable families
  raise instead of being guessed.
- **Layered configuration**: project `.dev-orchestra.yaml` over an
  OS-appropriate global file over built-in defaults, with `DEV_ORCHESTRA_HOME`
  and `DEV_ORCHESTRA_CONFIG` overrides.
- **Setup wizard**, interactive or `--defaults`, covering all four roles and an
  arbitrary reviewer panel.
- **Independent review pipeline**: frozen git snapshot (including untracked
  files), parallel read-only reviewers, tolerant finding parser, locus+text
  deduplication, triage states, accepted-findings fix brief, and an iteration
  budget that stops review loops.
- **`dev-orchestra` CLI**: `config`, `model`, `reviewer`, `doctor`, `run`,
  `review`, `state`, `summary`.
- **Doctor** diagnostics for CLI presence, version, credential *presence*,
  model resolution per role, and configuration validity.
- **Credential redaction** on every captured stream, and read-only enforcement
  for planning and review runs.
- Installers for Claude Code (skills directory) and Codex CLI (marked
  `AGENTS.md` pointer), keeping `SKILL.md` as the single source of truth. A
  per-project install also excludes itself via the target repo's
  `.git/info/exclude`, so the nested checkout stays out of that repo.
- 157 tests covering config layering, validation, wizard flows, adapters,
  review parsing/dedup/triage, partial reviewer failure and secret redaction —
  none of which invoke a real CLI.
- CI on Linux, macOS and Windows: lint, tests, skill validation.

[Unreleased]: https://github.com/istb16/dev-orchestra/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/istb16/dev-orchestra/releases/tag/v0.1.0
