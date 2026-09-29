# AI Development Orchestrator

**English** | [日本語](README.ja.md)

A general-purpose [Agent Skill](https://code.claude.com/docs/en/skills) that
orchestrates a full software-development workflow across **multiple AI coding
CLIs** — design with one model, implement with another, then have several
models review the result independently before anything is called done. It
works with any codebase, and runs only the stages a request needs: a typo fix
skips straight to the edit; a schema change gets the full pipeline.

## Why this exists

**A model reviewing its own work is a weak reviewer.** It shares the
assumptions that produced the bug. Two or three *different* models, each seeing
the same diff with no knowledge of the others' opinions, disagree in useful
ways — and the disagreements are where the real bugs are.

**Raw review output is not a fix list.** Multiple reviewers duplicate each
other, some findings are false positives, and forwarding all of it to a fixer
produces churn. So findings are deduplicated mechanically and then *triaged* by
the orchestrator; only accepted findings reach the fixer. And no model name is
ever guessed: configuration names a *family* plus `version: latest`, and an
adapter that cannot verify it raises an error.

## Who does what

| Stage | Role in the config | What it does |
| --- | --- | --- |
| Direction | `orchestrator` | Decides which stages a request needs, digests bulk input, merges the results |
| Design | `architect` | Investigates and writes the plan, read-only; nothing is implemented until you approve it |
| Implementation | `implementer` | Writes the code and the tests from the plan |
| Review | reviewers | Two or more models read the same frozen diff, read-only, without seeing each other |
| Fix | `review_fixer` | Fixes only the findings the orchestrator accepted |

```mermaid
flowchart LR
    U[User] --> O[Orchestrator]
    O --> A[Architect<br/>read-only]
    A --> I[Implementer]
    I --> T[Tests]
    T --> S[[Frozen snapshot]]
    S --> R1[Reviewer 1]
    S --> R2[Reviewer 2]
    S --> R3[Reviewer N]
    R1 --> C[Consolidate + dedupe]
    R2 --> C
    R3 --> C
    C --> TR[Triage]
    TR -->|accepted only| F[Review Fixer]
    F --> T2[Re-test] --> REP[Report]
```

The shape that matters: **one vendor designs, another reviews.** Two models
from the same family share the blind spot that produced the bug.

## Requirements

- **Python 3.11+** — standard library only (PyYAML is used if present). Run
  `python3` where that is its only name, or `bin/dev-orchestra[.ps1]`, which finds it.
- **git** — required for review snapshots.
- **At least one supported CLI**, already authenticated:
  - [Claude Code](https://claude.com/claude-code) (`claude`)
  - [Codex CLI](https://developers.openai.com/codex/cli) (`codex`)

The skill runs as a plugin in Claude Code, Codex or Antigravity; the CLIs above
are what it drives, not where it runs.

The skill uses **your existing CLI logins**. It never asks for an API key, never
stores credentials, and never prints them.

## Installation

Install it as a plugin from this repository (it is on no official marketplace).
Claude Code and Codex each run their own copy from their cache
(`~/.claude/plugins/cache/…`, `~/.codex/plugins/cache/…`), available from the
next session. Antigravity loads the directory placed in its `plugins/` folder.

### Claude Code plugin

```bash
claude plugin marketplace add istb16/dev-orchestra
claude plugin install dev-orchestra@dev-orchestra
```

Inside Claude Code: `/plugin marketplace add istb16/dev-orchestra`, then
`/plugin install dev-orchestra@dev-orchestra`.

### Codex plugin

```bash
codex plugin marketplace add istb16/dev-orchestra
codex plugin add dev-orchestra@dev-orchestra
```

`codex plugin list` shows what is installed.

### Antigravity plugin

Clone the repository and link it into Antigravity's plugins folder:

```bash
git clone https://github.com/istb16/dev-orchestra.git
cd dev-orchestra
./install/install.sh --antigravity    # links into ~/.gemini/config/plugins/
```

```powershell
.\install\install.ps1 -Antigravity    # Windows
```

By hand it is one link: `ln -s "$PWD" ~/.gemini/config/plugins/dev-orchestra`.
`--project <path>` installs into `<path>/.agents/plugins/` instead. Restart
Antigravity afterwards: a new plugin directory is only discovered on startup.
A linked install loads whatever branch the checkout has, so look at an
untrusted branch with `--copy` or from a separate worktree. The Antigravity
CLI can also stage a copy with `agy plugin install <path>`, which copies the
checkout as it is, and a `plugins.json` entry can point at the folder that
contains the checkout, which loads it live; both carry the same
untrusted-branch caution, and neither is guarded by the installer or checked
by `doctor` ([details](references/workflow.md#installing-from-a-skill-checkout)).
The Marketplace is curated by Google and takes no user-added entries.

The installers from before the plugin still work ([Installing from a skill checkout](references/workflow.md#installing-from-a-skill-checkout));
running the plugin from a clone is in `CONTRIBUTING.md`.

## Initial setup

On first use the skill notices there is no configuration and runs the wizard:
a CLI and a model family for the `orchestrator`, `architect`, `implementer`
and `review_fixer` roles and for each reviewer. To run it yourself, or to take
the recommended values without questions:

```bash
dev-orchestra config setup
dev-orchestra config setup --defaults
dev-orchestra model list        # the families your installed CLIs offer
```

The recommended lineup: a Claude `fable` architect, Claude `opus` for
implementation and fixes, one Claude and one Codex reviewer. Use the families
`dev-orchestra model list` prints. The file keeps only what you chose. See
[the wizard](references/configuration.md#the-wizard) and a
[two-vendor lineup](references/configuration.md#worked-examples).

## Usage

Talk to your agent normally — "Implement this issue using the configured
workflow", "Investigate and fix the checkout timeout", "Run a multi-model
review of this branch against main", "Add a security reviewer using Codex".
One sentence runs as much of the pipeline as the request needs. To drive a
stage yourself, these are the commands the orchestrator issues underneath
([workflow](references/workflow.md) explains each one):

```bash
dev-orchestra run architect --prompt-file .ai/request.md --output .ai/plan.md
dev-orchestra design approve            # after you have read the plan
dev-orchestra run implementer --prompt-file .ai/plan.md
# run the project's own tests, then record the outcome
dev-orchestra state record test ok
dev-orchestra review snapshot --base main
dev-orchestra review run
dev-orchestra review triage F1 --status accepted --note "confirmed"
dev-orchestra review fix-brief --output .ai/fix-brief.md
dev-orchestra run review_fixer --prompt-file .ai/fix-brief.md
# the same tests again, and record the outcome
dev-orchestra state record test ok
dev-orchestra review status             # another round, or report
```

For logs or a long spec, digest the bulk first and design from the summary
([Feeding it a lot of text](references/limits.md#feeding-it-a-lot-of-text)):

```bash
dev-orchestra run orchestrator --prompt-file .ai/analysis-request.md --output .ai/analysis.md
```

## Configuration

Precedence is **project → global → built-in defaults**:
`<repo>/.dev-orchestra.yaml`, then `~/.config/dev-orchestra/config.yaml`
(`%APPDATA%\dev-orchestra\config.yaml` on Windows). `dev-orchestra config show`
prints the result; `config set` changes one value. The schema, every field and
worked examples: [configuration](references/configuration.md).

## Model selection

A role stores a family and `version: latest` (or `pinned` with an exact id),
resolved against the installed CLI on every run
([model families](references/configuration.md#model-families-and-version-policy));
`model_tiers` gives one role a cheaper or second-opinion model on request
([model tiers](references/configuration.md#model-tiers)).

## Reviewers

Zero or more; two or more recommended. `dev-orchestra reviewer add --provider codex --role security`
adds one. A second round reviews only the fix, and lockfiles and bundles are
withheld. Roles, the snapshot, dedup and triage: [reviews](references/reviews.md).

## Example workflows

A Rails feature, an API change, a typo, a review on its own, and a dry run with
the mock provider: [Example workflows](references/workflow.md#example-workflows).

## Troubleshooting

`dev-orchestra doctor` checks the CLIs, the configuration and the model
families; `doctor --json` gives the same machine-readable. Symptoms and fixes:
[Troubleshooting](references/cli.md#troubleshooting).

## Security

Credentials are never requested, stored or printed. The architect and every
reviewer run read-only, enforced by the CLI rather than the prompt; the skill
has no network access of its own. Details, including what `--restricted` means
for your `permissions.deny` rules: [Security](references/architecture.md#security).

## Supported platforms

Linux, macOS and native Windows (`bin\dev-orchestra.ps1`) are CI-tested; WSL
works as Linux and is not required. Everything is pure Python plus `git`. The
Antigravity install on Windows makes a junction, which needs no Developer Mode.

## Upgrading

```bash
/plugin marketplace update                     # Claude Code, inside a session
codex plugin marketplace upgrade               # Codex: re-fetches the snapshot only,
codex plugin add dev-orchestra@dev-orchestra   # so add it again to install it
```

A checkout install upgrades with `git pull` ([details](references/workflow.md#installing-from-a-skill-checkout)).
Antigravity: `git pull` in the checkout, then restart Antigravity.
Configuration is forward-compatible within a major version, and `CHANGELOG.md`
calls out anything that needs action.

## Uninstalling

```bash
claude plugin uninstall dev-orchestra
codex plugin remove dev-orchestra@dev-orchestra
dev-orchestra config reset --scope global --delete   # optional: your configuration
```

Configuration stays unless you remove it; delete `.ai/` in a project to drop
its artifacts. A checkout install has `install/uninstall.sh` (`.ps1` on Windows).
Antigravity: `./install/uninstall.sh --antigravity` (`-Antigravity` on Windows), then restart it.

## Versioning and changelog

[Semantic versioning](https://semver.org/) over the config schema, the CLI
commands and flags, and the `.ai/` artifact formats: major for a breaking
change, minor for new commands, providers, roles or fields, patch for fixes and
documentation. `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/).

## Architecture

Judgement stays in the model, mechanics in code: `skills/dev-orchestra/SKILL.md`
says what to run, `scripts/dev_orchestra.py` does the deterministic parts, and
only the providers know CLI flags and model names — your own adapter goes in
`<config dir>/providers/` ([providers](references/providers.md)).
[architecture](references/architecture.md) has the long version.

## Reference documents

The detail behind this README. They are written for the orchestrating model
as much as for you, which is why they are written in English. A Japanese
translation of each, for people to read, is in [docs/ja/references/](docs/ja/references/).

| Document | What it answers |
| --- | --- |
| [references/workflow.md](references/workflow.md) | What each stage does, the prompt templates, the `.ai/` artifacts, example workflows, installing from a checkout |
| [references/configuration.md](references/configuration.md) | The schema, layering, every field, model families and tiers, the wizard, worked examples |
| [references/providers.md](references/providers.md) | The adapter interface, Claude and Codex, adding a CLI |
| [references/reviews.md](references/reviews.md) | The snapshot, withheld files, the fix-only second round, output schema, dedup, triage |
| [references/architecture.md](references/architecture.md) | How the pieces fit, why, and the security model |
| [references/limits.md](references/limits.md) | Stalls, timeouts, budgets, bulk input, what a run costs, the optimization level |
| [references/cli.md](references/cli.md) | Every command and flag, and troubleshooting |

## Contributing

Issues and pull requests welcome — see `CONTRIBUTING.md`, which also says how
to report a security problem. Adapter fixes for a CLI that changed its flags or
model names are the most valuable contribution.

## Documentation language

The skill and `references/` are English, which AI models read best; the README
is in [English](README.md) and [日本語](README.ja.md). The translations in
`docs/ja/references/` are for people, the English stays authoritative, and the
tests fail when the English moves on without them (each names its sha256).

## License

MIT — see `LICENSE`.
