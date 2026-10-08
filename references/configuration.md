# Configuration

<!-- contents: start -->

**Contents**

- [Where it lives](#where-it-lives)
- [Precedence](#precedence)
- [Presets](#presets)
  - [Adding reviewers beside the panel (`reviewers_extra`)](#adding-reviewers-beside-the-panel-reviewers_extra)
  - [A design panel of its own (`review.design.reviewers`)](#a-design-panel-of-its-own-reviewdesignreviewers)
- [Schema (version 1)](#schema-version-1)
  - [Field reference](#field-reference)
  - [Role options](#role-options)
  - [What the project file may not loosen](#what-the-project-file-may-not-loosen)
- [Optimization level](#optimization-level)
  - [Reviewers that run only on high-risk changes](#reviewers-that-run-only-on-high-risk-changes)
- [Model tiers](#model-tiers)
- [Model families and version policy](#model-families-and-version-policy)
- [What the user asks for, and what to run](#what-the-user-asks-for-and-what-to-run)
- [Editing](#editing)
  - [The wizard](#the-wizard)
- [Worked examples](#worked-examples)
- [YAML dialect](#yaml-dialect)

<!-- contents: end -->

## Where it lives

| Layer | Path | Purpose |
| --- | --- | --- |
| Project | `<repo>/.dev-orchestra.yaml` | Per-repository override, committed or not as you prefer. Read-only roles do not take `options.args` from it ([below](#role-options)) |
| Global | see below | Your personal default for every project |
| Built-in | `scripts/orchestrator/config.py` | Recommended defaults, used when no file exists |

In the config directory, a `providers/` directory holds your own provider
adapters (`%APPDATA%\dev-orchestra\providers\` on Windows,
`~/.config/dev-orchestra/providers/` elsewhere, `$DEV_ORCHESTRA_HOME/providers/`
when that is set; `DEV_ORCHESTRA_CONFIG` does not move it). See
`references/providers.md`.

Beside it, `verified/<provider>-resume.json` records the CLI versions that
`python scripts/smoke_live.py` checked on this machine for resuming a session
(`run architect --resume`); only that script writes it. It is read only when
its real path is outside the workspace: point `DEV_ORCHESTRA_HOME` into the
checkout and the record is neither read nor written, and `doctor` and the
`--resume` note say so. A version newer than one that passed here or in the
adapter's table, and of the same major version, resumes on trust unless a
failure recorded here stands in between, so a newer CLI whose resumed session lost a restriction is resumed
until the script is run on it; `design.resume.max_age_seconds: 0` turns
resuming off.

Global config path by platform:

| Platform | Path |
| --- | --- |
| Linux / BSD | `$XDG_CONFIG_HOME/dev-orchestra/config.yaml`, else `~/.config/dev-orchestra/config.yaml` |
| macOS | `~/.config/dev-orchestra/config.yaml` |
| Windows | `%APPDATA%\dev-orchestra\config.yaml` |

**Microsoft Store Python.** The Python from the Microsoft Store has Windows
keep the files it writes under your AppData in its own package folder
(`%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.<version>_…\LocalCache\Roaming\dev-orchestra\`).
dev-orchestra still reads and writes the same files, but Explorer, editors and
other Pythons do not see them. Wherever a path is stored somewhere other than
where it is printed, it is followed by `(stored at <real path>)`: in
`config path`, `config show --scope global`, the messages of the commands that
write the global file, and `doctor`, which also says that the Store Python is
in use and adds a note on how to fix it (`--json` gains a `*_real` key beside
each such path, only then). Adapters and records in the package folder take
precedence over same-named files in the real `%APPDATA%`. To fix this, either
use a python.org Python (`py`), or set `DEV_ORCHESTRA_HOME` to a trusted
folder you control, outside `%USERPROFILE%\AppData` and outside any project
checkout. Then copy only `config.yaml` from the package folder. Review
`providers\*.py` before moving them, because they are code that is imported at
startup. Let `python scripts/smoke_live.py` regenerate the `verified\` records
instead of copying them.

Environment overrides:

- `DEV_ORCHESTRA_CONFIG` — use this exact file as the global layer.
- `DEV_ORCHESTRA_HOME` — use this directory instead of the platform default.
- `DEV_ORCHESTRA_NO_USER_PROVIDERS` — any value but empty or `0` skips the
  user adapter directory (`<config dir>/providers/`) entirely.

`dev-orchestra config path` prints both resolved locations.

The project file is found by walking up from the current directory and stopping
at the git root, so running the CLI from a subdirectory still finds it.
Every command starts that walk from the same place, the directory it was run
from (or `--cwd`), and a `run --detach` worker is handed it: a subdirectory
with a file of its own sets the roles, the budgets and `workspace.dir` alike,
foreground or detached. `.ai/` itself still sits at the git root.
Accepted names, in order: `.dev-orchestra.yaml`, `.dev-orchestra.yml`,
`.dev-orchestra.json`.
A command that writes to a `.json` file (`config set`, `reviewer add` and the
other writers) writes it back as JSON, without the header comment a YAML file
gets.

## Precedence

```
project config  →  global config  →  the global file's preset, fitted to the installed CLIs  →  built-in defaults
```

Mappings merge key by key, so a project file that only sets `implementer` keeps
your global architect. **Lists replace wholesale**: a project file that defines
`reviewers` defines the entire panel for that project. That is deliberate —
"this repo reviews with security + database only" must be expressible.

The preset layer reaches only the roles and the reviewer panels that no file
sets ([Presets](#presets)). With both Claude Code and Codex installed, or
none of the fitted CLIs, the default preset is the built-in defaults exactly,
plus its design panel.

## Presets

A preset is one choice on the spend axis: `quality`, `standard` or `fast`. It
is saved as one key in the global file and worked out every time the
configuration is loaded, against the CLIs this machine has on PATH, so a
machine without Codex never carries a Codex reviewer that fails every round.

```bash
dev-orchestra config setup --preset quality   # save it; nothing is asked
dev-orchestra config set preset fast          # switch, keeping your other settings (always the global file)
```

```yaml
version: 1
preset: quality
```

A global file that names no preset, and no file at all, run under `standard`.
There is no "none": the first `config set` on a fresh machine does not switch
the fit off.

What each one sets (families only, `version: latest` throughout; Codex is
`recommended-coding` in every slot, the one family its adapter vouches for
without running the CLI):

| Key | `quality` | `standard` | `fast` |
| --- | --- | --- | --- |
| `orchestrator` | opus | sonnet | sonnet |
| `architect` | fable | fable | opus |
| `implementer` | fable | opus | sonnet |
| `review_fixer` | fable | opus | sonnet |
| reviewer seats (role / Claude family) | general / fable [Claude], general [Codex], security / opus [Claude], security [Codex], architecture / opus [Claude], test / opus [Claude] | general / opus, general / sonnet, security / sonnet→opus [Claude], test / sonnet | general / opus; security / sonnet, `when: high-risk` |
| design reviewer seats (`review.design.reviewers`) | general / sonnet→opus [Claude], general [Codex], security / opus [Claude], test / sonnet [Claude], architecture / opus [Claude] | general / sonnet→opus [Claude], security / sonnet [Claude], test / sonnet [Claude] | general / sonnet [Claude] |
| `review.design.enabled` | `auto` | not set (`auto`) | `false` |
| `optimization.level` | `quality` | not set (`balanced`) | `aggressive` |

`→opus` is `high_risk_model: {family: opus}`: the seat runs opus on a
high-risk round and its own family on the rest. `[Claude]` and `[Codex]` mark
a seat placed on that vendor rather than dealt (step 2 below).

Those eight keys are the ones a preset governs. Everything else — budgets,
timeouts, `review.max_review_iterations`, `design.require_approval` — is the
built-in default unless a file sets it (`design.require_approval` only the global
file: [below](#what-the-project-file-may-not-loosen)). `standard`'s code panel is read from
the built-in defaults, so the two cannot drift apart; its design panel is the
preset's own, since the defaults have none.

No preset sets `relevance`, so every preset security seat runs on every round
it is in, the design ones included: `quality`'s opus design security seat runs
on each design round `auto` lets through. To have it sit out a plan with no
security-relevant token, opt it in: `reviewer set claude-security --design
--relevance security` (which copies the design panel into the file, below).

**Fitting.** `claude`, `codex` and `agy` take part, and a user adapter only
when it declares `preset_family` (see
[Taking part in preset fitting](providers.md#taking-part-in-preset-fitting));
any other user adapter runs only when a file names it, as before. Detection is
a PATH lookup, with no CLI started:

1. The orchestrator and the architect go to the first installed CLI of
   `claude`, `codex`; failing both, to the first user adapter eligible for a
   seat, by name; failing that, to agy. The implementer and the review fixer
   go to the first of `claude`, `codex`, `agy`; failing all three, to the
   first opted-in user adapter, by name. Claude gets the preset's family,
   Codex `recommended-coding`, agy `default`, a user adapter the family it
   declares. agy cannot be held to reading, so it gets a read-only role or a
   reviewer seat only on a machine with neither Claude, Codex nor an eligible
   user adapter. There the seat is warned wherever a global-file agy seat is,
   and its note ends with how to keep it off (`; agy cannot be held to reading
   -- set architect in the global file to keep it off agy`).
2. Reviewer seats are dealt round the same CLIs as the orchestrator's --
   `claude`, `codex`; else the eligible user adapters; else agy -- in turn,
   starting with the implementer's -- the provider a file puts it on, when a file sets it -- so
   with two vendors every panel holds both. Each panel, code and design, is
   fitted on its own the same way. Only full seats without a vendor are dealt
   and counted here; vendor seats are placed, and cheap seats are step 4. A
   panel with one dealt reviewer that always runs (`fast`'s code panel) starts
   with the other vendor: one always-running reviewer from the implementer's
   own vendor is not an independent review. A seat keeps its `when` wherever
   it lands. Between two eligible user adapters the round works the same way,
   so a project file that sets `implementer` to the second one gives it the
   first seat under `standard`, and gives the one always-running seat of
   `fast` to the first.

   A vendor seat is placed instead of dealt, so the implementer moves none of
   them. A Claude seat sits on Claude whenever Claude is installed, and is
   dealt with the untagged seats where it is not. A Codex seat sits on Codex,
   else on the first other CLI of the seat pool (an eligible user adapter, or
   agy), else it is not added: `codex not found on PATH: reviewer seat 2
   (general) was not added; it is a second vendor's opinion and nothing
   installed stands in for one`. A seat with a `high_risk_model` keeps it on
   Claude only, the one CLI whose second model can be named offline; one dealt
   elsewhere drops it, and its note ends `; no high-risk model on codex`.
   `standard`'s security seat carries one, so it is a Claude seat.
3. A seat that would repeat an earlier one exactly (provider, family,
   high-risk family, role, condition) is not added. Ids come from the provider
   and the role, so a second Claude seat of the same role is
   `claude-general-2`.
4. A cheap seat -- a Claude sonnet seat with no `high_risk_model`, the `test`
   seats here -- goes to Claude alone, the one CLI with a cheap model that can
   be named offline, and is not added where Claude is not installed (`claude
   not found on PATH: reviewer seat 4 (test) was not added; codex has no cheap
   model named offline`). Codex and user adapters therefore get no cheap seat.
   `standard`'s security seat is a full seat, so Codex alone keeps one; there
   it runs every round. Every seat of `standard` but a general one, `test` in
   `quality`'s code panel, and every design seat but a general one, is a held
   seat, never put on agy (`...; agy cannot be held to reading`), so agy alone
   keeps `standard` at `agy-general`, `quality`'s code panel at three seats and
   every design panel at `agy-general`. A skipped seat moves no other seat and
   changes no id. Design seat notes say `design reviewer seat`, and on agy end
   `-- list review.design.reviewers in the global file to keep them off agy`.
   When the files leave no risk pattern in force
   (`optimization.high_risk_paths: []` and no `extra_high_risk_paths`), a
   fitted `when: high-risk` seat stays and runs only on rounds declared with
   `review run --high-risk`; a `when: high-risk` reviewer a file writes, in
   `reviewers` or `reviewers_extra`, is still refused, and the wizard does not
   offer one a file holds. Only `fast` fits a `when: high-risk` seat.
5. With none of Claude, Codex, an opted-in user adapter or agy installed,
   every role and the panel expand as written; `doctor` reports the missing
   CLIs. With agy alone every role and seat goes to agy, each with a note
   (`claude, codex not found on PATH: implementer went to agy (default)`).

The code panel:

| Preset | Claude + Codex | Claude only | Codex only | agy only |
| --- | --- | --- | --- | --- |
| `quality` | claude-general fable, codex-general, claude-security opus, codex-security, claude-architecture opus, claude-test opus | claude-general fable, claude-security opus, claude-architecture opus, claude-test opus | codex-general, codex-security, codex-architecture, codex-test | agy-general, agy-security, agy-architecture |
| `standard` | claude-general opus, codex-general, claude-security sonnet→opus, claude-test sonnet (the built-in defaults) | claude-general opus, claude-general-2 sonnet, claude-security sonnet→opus, claude-test sonnet | codex-general, codex-security | agy-general |
| `fast` | codex-general; claude-security sonnet (high-risk) | claude-general opus; claude-security sonnet (high-risk) | codex-general; codex-security (high-risk) | agy-general; agy-security (high-risk) |

The design panel:

| Preset | Claude + Codex | Claude only | Codex only | agy only |
| --- | --- | --- | --- | --- |
| `quality` | claude-general sonnet→opus, codex-general, claude-security opus, claude-test sonnet, claude-architecture opus | as Claude + Codex without codex-general | codex-general, codex-security, codex-architecture | agy-general |
| `standard` | claude-general sonnet→opus, claude-security sonnet, claude-test sonnet | the same | codex-general, codex-security | agy-general |
| `fast` (`enabled: false`) | claude-general sonnet | the same | codex-general | agy-general |

With Claude and Codex, a project file that puts the implementer on Codex
deals `standard`'s code panel as codex-general, claude-general sonnet,
claude-security sonnet→opus, claude-test sonnet; the vendor seats of
`quality` and the design panels stay where they are.

agy beside Claude or Codex changes nothing: with Claude, Codex and agy
installed every preset is what it is with Claude and Codex, the built-in
defaults included; Claude + agy is Claude only, and Codex + agy is Codex only.
On a machine with agy alone, keep it off the read-only seats by setting the
orchestrator and the architect, or listing `reviewers`, in the global file: a
role a file sets, and a panel a file lists, are not fitted (below). A listed
`reviewers` takes the fitted design panel with it; `review.design.reviewers`
alone keeps only the design rounds off agy.

The fit is worked out again on every load: install Codex later and the next
command's panel has it. Reviewer ids can therefore differ between teammates'
machines, so a script that names `codex-security` in `--only` has to allow for
that. What was refitted, and why, is a line each under `Preset:` in
`config show`, under Notes in `doctor`, and under the summary `config setup`
prints: `codex not found on PATH: reviewer seat 2 (general) went to claude as
claude-general-2 (sonnet)`.

**Only what no file sets is fitted.** A role that any file sets any field of —
`provider`, `model`, `options`, `model_tiers` — gets nothing from the preset: it
resolves from the built-in defaults and the files exactly as it did before
presets existed, on the provider the files name. Set a role whole if you set it
at all; a `quality` user who sets only `implementer.options` gets the default
`opus` implementer, and `config show` says the role was not fitted. When that
provider's CLI is absent, `doctor` says to set `<role>.provider` to an installed
CLI or to remove the role from the file. The first `config set` that takes a
role out of the fit and so changes its provider says so.

The panel follows the same rule: a `reviewers` list in any file replaces the
fitted panel whole, and a reviewer you listed whose CLI is absent still fails
in every round, because you listed it. A file that lists `reviewers` also
takes the fitted design panel out, and its fit notes with it: its design
rounds run that list without `when`, as they did before presets had a design
panel. A `review.design.reviewers` list in any file replaces the fitted design
panel whole; `review.design.reviewers_extra` alone joins it
([A design panel of its own](#a-design-panel-of-its-own-reviewdesignreviewers)).

**`reviewer add` adds beside the panel; the other writers record it.** In a
file that lists no `reviewers`, `reviewer add` writes the new reviewer to that
file's `reviewers_extra` and copies nothing, so the panel keeps following the
fit -- or, in a project, the global file's list -- and the command says so:
`Added reviewer claude-security-2 (claude / opus / security) to <path> as an
extra; the panel still follows preset standard's fit`. In a file that lists
`reviewers` it appends there, as before ([below](#adding-reviewers-beside-the-panel-reviewers_extra)).
`reviewer remove` and `reviewer set` find their reviewer in the panel in force
-- the ids and positions `reviewer list` shows -- and edit one of the file's own
extras in place. For any other seat -- fitted, listed, or a global extra seen
from a project -- they and `config set reviewers[...]` start from this
machine's fitted panel, copy it into the file and edit it there, as they have
always started from the inherited list. From then on the file's list is the
panel, on this machine and any other that reads the file, and the command says
so once: `note: <path> now lists the reviewers; the panel no longer follows
preset standard's fit (recorded claude-general opus, claude-general-2 sonnet,
claude-security sonnet, claude-test sonnet)`.
Roles, the design review and the optimization level keep following the preset;
the fitted design panel does not, since the file now lists `reviewers`. When no
file lists `review.design.reviewers` either, the design rounds move with it, and
the note ends `; design rounds now run this list without when, not preset
standard's design panel`. For the same reason `config prune` keeps a
`reviewers` list equal to the fit when it is all that keeps the design rounds
off the preset's design panel, and says `Kept reviewers in <file>: dropping it
would move design reviews to the preset's design panel.`
In project scope, when the global file lists no reviewers -- so what is copied
is the fit and the global extras -- a seat on agy is left out of the copy,
since the project file may not hold it, and the note ends `; not copied into
.dev-orchestra.yaml: agy-general -- a reviewer on agy is taken only from the
global config`. An edit that names such a seat -- `reviewer set agy-general`,
or an index past the copied list -- fails with the same `note: not copied into
...` beside its error. A panel the global file lists is copied whole
([below](#role-options)). Each of these writers composes the configuration as
it would be saved, both files included, and refuses (exit 2, nothing written)
a `reviewers` problem the write would introduce -- leaving no reviewer that
always runs, for one; a problem the file already had does not stop a write.
`config reset` on the global file keeps `preset` and clears the list and the
extras, which restores the fit. An unknown preset name is cleared with the
rest, with a `note:`, and the file then runs under `standard`.

A reviewer added without `--model` gets its CLI's default family: `opus` on
Claude, `default` on agy, `recommended-coding` on Codex and on any other
adapter. `reviewer set --provider <other>` without `--model` or `--pin` writes
the new CLI's default family the same way, because a family is the old CLI's
name for a model and the new one would not resolve it; a `note:` line says
what it was (`note: model family reset from 'opus' to 'default' for provider
agy (--model picks another)`).

**Only the global file can name a preset, for now.** A `preset:` in a project
file fails validation with `preset: only the global file can name a preset for
now`, and workflow commands exit 2 until it is removed. `config set preset
<name>` therefore writes the global file even inside a project that has its own,
and refuses `--scope project` (exit 2) without writing.

**Security and unattended runs are additions, not presets.** For a security
reviewer on risky changes, `reviewer add --provider claude --role security
--when high-risk` plus `optimization.extra_high_risk_paths` for this
repository's own sensitive paths; that adds it beside the panel, as above. For runs
nobody watches, `config set design.require_approval false`; with
`preset: quality` beside it, that is "unattended quality".

An older dev-orchestra reading a global file with `preset:` ignores the key and
runs its built-in defaults; its `config validate` does not report it.

### Adding reviewers beside the panel (`reviewers_extra`)

A file can add reviewers to the panel it inherits instead of copying that
panel. The panel then keeps following the preset's fit or the global file's
list, and the additions stay:

```yaml
version: 1
reviewers_extra:
  - id: claude-security
    provider: claude
    model:
      family: opus
      version: latest
    role: security
    when: high-risk
```

`reviewer add` writes this key whenever the file lists no `reviewers`. Its
entries take the schema of `reviewers` entries; `null` and `[]` add nothing.
Either file may hold it, and the extras join the panel after it:

| The panel is | When |
| --- | --- |
| the project file's `reviewers`, then the project file's extras | the project file lists `reviewers` |
| the global file's `reviewers`, then the global extras, then the project extras | otherwise, when the global file lists `reviewers` |
| the preset's fit, then the global extras, then the project extras | otherwise |

`reviewers: []` counts as a list.

**Ids.** The panel the extras join keeps every id. An extra whose id is already
taken -- by the fit, a listed reviewer or an earlier extra -- runs under a new
one, with a note under `Preset:` in `config show` and under Notes in `doctor`:
`reviewers_extra[0] in the project file: id codex-general is taken by the
fitted panel; it runs as codex-general-2 (reviewer set codex-general-2 --id
<name> keeps a name)`. So `--only <fitted id>` always selects the fitted seat;
and since the fit follows what is installed, installing Codex can move an extra
to a new id. An extra is never dropped for anything else, so one that the fit
later covers runs twice until it is removed.

**What is checked.** Each file's extras are checked entry by entry -- the id,
the role, the provider and model, `when`, and ids unique within that file --
whether or not they join the panel, as `reviewers_extra[0] in the global file:
...`. The rules of the whole panel, at least one reviewer that always runs
among them, apply with the extras in it. `reviewer remove`, `reviewer set` and
`config set reviewers_extra[<n>].<key>` edit one of the file's own extras in
place, a renamed one found by the id it runs under, and copy nothing. Given a
renamed extra's old id, `reviewer remove` and `reviewer set` refuse (exit 2,
nothing written), since that id now selects another seat, and the message
names the id the extra runs under: `reviewer claude-security:
reviewers_extra[0] in the project file runs as claude-security-2, since
claude-security is taken; use claude-security-2 (select the other seat by its
position in reviewer list)`.

**Where each one came from.** A project extra meets every refusal a project
`reviewers` list does: one on agy, or one with `options.args`, is refused
([below](#role-options)). A global extra on agy runs, warned. `config show`
marks each extra (`(extra, project file)`) and its JSON adds
`reviewer_origins`, parallel to `config.reviewers`; `reviewer list` marks them
`(extra: project)`, and its JSON and `doctor`'s give each reviewer an `origin`:
`fit`, `global`, `project`, `global extra` or `project extra`. `config prune`
keeps extras, and `config reset` clears them with the other overrides and says
how many. When a small change runs a single reviewer, it is the first `general`
one in panel order, so an extra is chosen only when the panel it joins has no
`general` seat.

**A list that holds the inherited panel plus more** -- what `reviewer add` left
before this key existed -- gets a note in `doctor`: `reviewers in <file>: holds
the inherited panel plus <ids>; move <ids> to reviewers_extra and remove
reviewers to keep following it`. A design list that holds the inherited design
panel plus more, in a file that lists no `reviewers`, gets the same note for
`review.design.reviewers` (`holds the inherited design panel plus <ids>; move
<ids> to review.design.reviewers_extra ...`). The inherited design panel is the
one below the file, or -- in a project file under a global `reviewers` list --
that list without `when`. Files are never rewritten.

**Code extras and the preset's design panel.** While the design rounds run
the preset's design panel, `reviewers_extra` -- from `reviewer add` or
`suggest-roles --write` -- reviews code only; before, design rounds ran a copy
of the code panel and the extras came with it. `doctor` notes each one:
`reviewers_extra in <file>: <ids> review code only; design rounds run preset
standard's design panel (add them to review.design.reviewers_extra as well to
review designs too)`.

**A file that lists `reviewers` keeps exactly those seats.** When a preset
gains seats, a listed panel -- a copy a writer once seeded from an older fit
included -- gets none of them. `config show` prints the panel in force.

An older dev-orchestra ignores `reviewers_extra`: the panel runs without the
extras, and its `config validate` does not report the key.

### A design panel of its own (`review.design.reviewers`)

The design review runs the preset's fitted design panel ([Presets](#presets))
until a file gives it one of its own; a file that lists the code panel
(`reviewers`) runs that panel instead, with every `when` ignored.
`review.design.reviewers` replaces the inherited design panel for design
rounds only, and `review.design.reviewers_extra` adds to whichever design
panel is inherited, as `reviewers_extra` does for the code panel:

```yaml
review:
  design:
    reviewers:
      - id: claude-general
        provider: claude
        model:
          family: sonnet
          version: latest
        high_risk_model:           # opus on a high-risk plan
          family: opus
          version: latest
        role: general
      - id: claude-security
        provider: claude
        model:
          family: sonnet
          version: latest
        role: security
        relevance: always          # never judged by the role rules (a security seat's default)
    reviewers_extra: []
```

| The design panel is | When |
| --- | --- |
| none: design rounds run the code panel, `when` ignored | no file sets `review.design.reviewers` or `review.design.reviewers_extra`, and a file lists `reviewers` or the global file names an unknown preset |
| the project file's design list, then the project file's design extras | the project file lists `review.design.reviewers` |
| the global file's design list, then the global design extras, then the project design extras | otherwise, when the global file lists one |
| the preset's fitted design panel, then the global design extras, then the project design extras | otherwise, when no file lists `reviewers` |
| the code panel in force with every `when` removed, then the global design extras, then the project design extras | otherwise (a file lists `reviewers` and a file sets design extras only) |

The same id may name a seat in both panels (`claude-general`): rounds, reports
and usage labels are kept apart by stage, so its history stays continuous.
An extra whose id is taken is renamed with a note, as in the code panel.

**What a design seat may be.** The schema of a `reviewers` entry, checked the
same way (`review.design.reviewers[0]: ...`), with two differences: `when:
paths` is refused, since a plan has no changed paths; and `when: high-risk`
is honoured -- the seat joins a plan with a high-risk hit or a round declared
with `review run --design --high-risk` -- so one seat must still run always. A
project file's design seat on agy, or with `options.args`, is refused like a
project `reviewers` entry -- also one that repeats the id, provider and
options of a global code seat: a seat a file wrote under `review.design` is
always judged as its own. Only a seat copied from the code panel meets that
code seat's rule.

**Editing it.** `reviewer list|add|remove|set --design` act on the design
panel. `reviewer add --design` writes to the file's `review.design.reviewers`
when the file lists one, and otherwise to `review.design.reviewers_extra`,
saying what the design panel still follows (`preset standard's fit`, `the
code panel` or `the global file's design reviewers`). `reviewer set|remove
--design` on an inherited seat copy the design panel in force into the file
first; copied from the code panel, the seats lose their `when`, and the note
says so. Copied from the fit, the note says `copied from preset standard's
fit` and adds `; the design panel no longer follows preset standard's fit
(recorded claude-general sonnet, claude-security sonnet, claude-test sonnet)`,
in either file. In project scope
the copy follows the code panel's rule: a seat on agy is left out, with the
same `not copied into` note, unless the global file lists the panel it came
from -- its design list for a design seat, its `reviewers` for a seat of the
code panel -- in which case it is copied whole. `--when-paths` with
`--design` exits 2. `reviewer list --design` names the source on its first
line: `(design panel: the preset's fit)`, `(design panel: the code panel;
when conditions ignored)`, `(design panel: global file)` or `(design panel:
project file)`; `review status --design` names it the same way.

**Where it shows.** `config show` has a `Design reviews` block, and its JSON
adds `design_reviewer_origins` when there is a design panel (`fit design` for
a fitted seat); `doctor` reports `design_reviewers` and `design_panel_source`
(`fit`, `global`, `project` or `code`), prints `Design: 3 (the preset's fit)`
unless the design panel is only a copy of the code panel, and diagnoses each design seat
the fit or a file wrote as it does a code seat. On agy alone `agy-general` is
therefore warned twice, once per panel. `review status --design` lists the design
panel and the seats the next round would leave out. `review run --design
--only <id>` selects from the design panel; `run <reviewer id>` always runs
the code panel's seat.

**Model on a high-risk round.** Any seat, in either panel, may set
`high_risk_model`: a model block (`family` plus `version: latest`, or pinned
with an `id`) it runs instead of `model` on a round with a high-risk hit or
`--high-risk`. The provider and `options` stay the seat's own, so a
`provider` or `options` inside it is refused. `when: high-risk` adds a seat on
risk; `high_risk_model` changes a seat's model; the two can coexist. Each
switched seat prints `note: high-risk round (<path> matches <pattern>): <id>
runs opus instead of sonnet`, and its run record gains `model_slot:
high-risk` (a record without it is the usual slot), and `optimization
report` scores the two slots apart. `reviewer add|set
--high-risk-model FAMILY` sets it, `reviewer set --clear-high-risk-model`
removes it, and `reviewer set --provider` to another CLI removes it with a
note, with or without `--model`; `--high-risk-model` beside it sets the new
CLI's instead.

An older dev-orchestra ignores `review.design.reviewers`, `reviewers_extra`
under it, `high_risk_model` and `relevance`: design rounds run the code panel
and every seat its usual model. It also has no fitted design panel.

## Schema (version 1)

```yaml
version: 1

orchestrator:                 # decides stages, delegates, writes the report
  provider: claude
  model:
    family: sonnet
    version: latest

architect:                    # investigation + design, read-only
  provider: claude
  model:
    family: fable
    version: latest

implementer:                  # writes code and tests
  provider: claude
  model:
    family: opus
    version: latest

review_fixer:                 # fixes accepted findings
  provider: claude
  model:
    family: opus
    version: latest

reviewers:                    # 0..n independent reviewers; two or more recommended; listed, design rounds run it too
  - id: claude-general
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-general
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: general
  - id: claude-security        # security and test on sonnet, Claude's cheap model
    provider: claude
    model:
      family: sonnet
      version: latest
    high_risk_model:           # the full model on a high-risk change
      family: opus
      version: latest
    role: security
  - id: claude-test
    provider: claude
    model:
      family: sonnet
      version: latest
    role: test

run:
  timeout_seconds:                    # total deadline of one `run`, per role
    orchestrator: 1800
    architect: 1800
    implementer: 3600                 # implementers measured past 30 min
    review_fixer: 1800

review:
  max_review_iterations: 2            # hard stop on review→fix→re-review loops
  parallel: true                      # run reviewers concurrently
  re_review_severities: [critical, high]
  timeout_seconds: 1800               # reviewers only: `review run` and `run <reviewer-id>`
  idle_timeout_seconds: 300           # no output for this long: wedged; `run` too
  design:
    enabled: auto                     # review .ai/plan.md before implementing (true, false or auto)
    max_iterations: 2                 # design review -> revise -> re-review
    # reviewers: [...]                # a design panel of its own; unset, the preset's fitted one

optimization:
  skip_unneeded_roles: true           # a test/architecture seat sits out a round with nothing for it

design:
  require_approval: true              # implementer waits for the user's yes (design approve)
  resume:                             # run architect --resume
    max_age_seconds: 3600             # older than this, the revision runs fresh
    max_context_tokens: null          # no cap on the context a resumed session carries

workspace:
  dir: .ai                            # relative to the repo root, or absolute

language:
  reply: null                         # a language tag (ja, zh-TW, ko, en): the language to answer in
  rewrite: true                       # false: Claude Code reminds, but never asks for a rewrite
```

### Field reference

| Field | Type | Notes |
| --- | --- | --- |
| `version` | int | Must be `1`. |
| `<role>.provider` | string | A registered adapter: `agy`, `claude`, `codex`, `mock`, or a user adapter (see `references/providers.md`). A read-only role or reviewer on `agy` is taken only from the global config, or from the global preset's fit when no other installed CLI can take the seat ([below](#role-options)). |
| `<role>.model.family` | string | A family/alias the provider can resolve (`opus`, `sonnet`, `fable`, `recommended-coding`). Omit or use `default` to let the CLI choose. |
| `<role>.model.version` | `latest` \| `pinned` | `latest` re-resolves on every run. `pinned` requires `model.id`. |
| `<role>.model.id` | string | Exact model id, only with `version: pinned`. |
| `reviewers[].id` | string | Unique, matching `[a-z0-9][a-z0-9._-]*`. Names the report file. |
| `reviewers[].role` | string | Built-in or your own; see `references/reviews.md`. |
| `reviewers[].when` | `always` \| `high-risk` \| mapping with `paths` | When the reviewer runs on a **code** review round (default `always`). `high-risk` joins only the rounds judged high-risk; a mapping with `paths` joins only the rounds whose change matches one of its own patterns. A design round on the code panel ignores both and runs every reviewer; a design panel of its own honours `high-risk` and refuses `paths`. At least one reviewer must stay `always`. See [Reviewers that run only on high-risk changes](#reviewers-that-run-only-on-high-risk-changes) and [Reviewers scoped to paths](#reviewers-scoped-to-paths). |
| `reviewers[].high_risk_model` | mapping | The model block the seat runs instead of `model` on a high-risk round (a high-risk hit or `--high-risk`), in either panel: `family` and `version`, or `version: pinned` with an `id`. No `provider` or `options`: those stay the seat's. See [A design panel of its own](#a-design-panel-of-its-own-reviewdesignreviewers). |
| `reviewers[].relevance` | `security` \| `test` \| `architecture` \| `always` | The role rule that may leave the seat out of a round with nothing for it. Unset, a `test` or `architecture` seat is judged by its own role's rule and any other role -- `security` included, since its rule reads path names alone -- is never judged; `security` opts a security seat in. `always` keeps the seat out of the judgement. A rule on a `general` seat is refused: `general` is never left out. See `references/reviews.md` ("Roles a round does not need"). |
| `reviewers_extra` | list \| null | Reviewers added beside the panel the file inherits, with the schema of `reviewers` entries; in either file. Renamed when an id is taken, never dropped. See [Adding reviewers beside the panel](#adding-reviewers-beside-the-panel-reviewers_extra). |
| `review.design.reviewers` | list \| null | The design review's own panel, with the schema of `reviewers` entries (no `when: paths`). Unset in every file, design rounds run the preset's fitted design panel, or the code panel with `when` ignored when a file lists `reviewers`. A preset governs it: `config setup --preset` replaces it. See [A design panel of its own](#a-design-panel-of-its-own-reviewdesignreviewers). |
| `review.design.reviewers_extra` | list \| null | Design reviewers added beside the design panel the file inherits -- the fitted one, or the code panel without `when` when a file lists `reviewers`, when no file lists a design panel. |
| `review.max_review_iterations` | int ≥ 0 | Rounds per review, not per project: the count restarts on a new branch, a new `--base`, or `budget reset`. `0` disables re-review entirely. |
| `review.parallel` | bool | `false` runs reviewers one at a time (easier to debug). |
| `review.re_review_severities` | list | Severities that count as blocking: a non-empty list drawn from `critical`, `high`, `medium` and `low`, in any case (default `[critical, high]`). A single name not in a list, an unknown name and `[]` are refused; a command that reads the file without validating it blocks on the default instead. |
| `run.timeout_seconds.<role>` | int > 0 | The total deadline of one `run` of `orchestrator`, `architect`, `implementer` or `review_fixer` (default 3600 for the implementer, 1800 for the others). `--timeout` overrides it for one run. Any other key is refused. Not part of a role's block, so setting it never takes the role out of the preset's fit, and `config setup --preset` keeps it. A run killed at it says so, naming the key. |
| `review.timeout_seconds` | int > 0 | The total deadline of each reviewer: of a `review run` round's reviewers and of `run <reviewer-id>` (default 1800). It no longer bounds `run` of a role; that is `run.timeout_seconds.<role>`. A timeout is reported, not raised. |
| `review.idle_timeout_seconds` | int > 0 \| null | No output for this long, and the run is treated as wedged (default 300; streaming providers only). Shared by reviewers and `run` of every role: silence does not grow with the task. A role can override it with `options.idle_timeout`. |
| `review.exclude` | list | Glob patterns whose diff body is withheld from reviewers. Replaces the default list wholesale; `[]` reviews everything. |
| `review.incremental_rounds` | bool | `true` (default) makes a second round diff against what the first round reviewed, carrying the findings the fix was meant to address. `false` re-diffs the whole change every round. |
| `review.max_findings` | int \| null | How many findings each reviewer is asked for. `null` (default) lets `optimization.level` decide, `0` lifts the cap. Findings that come back over the cap are kept, never trimmed. |
| `review.context.max_chars` | int ≥ 1 \| null | The largest change body a review round will send at all: the diff, or the plan plus the request it answers (default 400,000 ≈ 100k tokens, four times the largest prompt recorded here — it refuses nothing anyone has run). Over it, `review run` exits 3 without reviewing anything; the ways under it are `--base`, `review.exclude`, splitting the change, or a shorter plan. `--force` is a human's override, and records the round as over budget. `null` means the default, as it does on `review.max_findings`; there is no value that switches the limit off. See `references/limits.md`. |
| `review.context.inline_chars` | int ≥ 1 \| null | How much of the change body goes into the reviewer's prompt (default 400,000, the same number as `max_chars`). At or under it the body is inlined and the round can be clean; over it the reviewer is handed the path of the frozen snapshot and the round is recorded `partial` — coverage unverified — whatever comes back. **Setting it below `max_chars` opens a band between the two where rounds run and are recorded `partial`**: that is the explicit choice of somebody who will not pay for very large prompts, and `partial` is what it costs. Setting it *above* `max_chars` is also allowed and is not a mistake — it means the body is only ever handed over as a file on a round a human forced. `null` means the default. Every round records the number it was measured against, so a `partial` round says which limit made it one. See `references/limits.md`. |
| `review.context.surrounding` | `none` \| `enclosing` | `enclosing` also hands every code reviewer the Python function, method or class enclosing each hunk, extracted from the snapshot's git tree when the snapshot is taken (default `none`: the diff alone). Off, because measured on one snapshot it did not make a review cheaper — `optimization report` compares rounds with and without it. `false` and `null` mean `none` (`off` reads as `false`); `true` is refused. See `references/reviews.md`. |
| `review.context.surrounding_chars` | int ≥ 1 \| null | The most surrounding context a round may add (default 15,000: measured on one snapshot, it left the cost per run where it was, while 60,000 added what it carried). Capped further by what the diff leaves under `max_chars` and `inline_chars`, so context never refuses a round or sends a diff over as a file. What does not fit is left out by name, in the prompt and in every report. `null` means the default. See `references/limits.md`. |
| `review.design.enabled` | bool \| `auto` \| null | Whether `.ai/plan.md` goes in front of the design panel (see `review.design.reviewers`) before implementation; the stage costs a reviewer run per panel member per round. `true` always, `false` never. `auto` (default) decides from the plan, and `status` prints the answer and its reason: every backticked token anywhere in the plan, and every path-shaped word under the plan's `Files to Modify` heading whether backticked or not (a fenced block there included), is checked against the high-risk patterns (`optimization.high_risk_paths` plus `extra_high_risk_paths`) ignoring case, and a hit means run — a name with a `/` and no extension, such as `db/migrate`, is also checked as a directory; the size counts the filename-shaped tokens (a `/` or a file extension) under `Files to Modify` outside fenced blocks, `docs/`, `references/`, `tests/` and `.md`, `.rst` and `.txt` files, without looking at the disk, and 6 or more means run; a glob, a directory, a name with a `/` and no extension, or a path through `..` there means run (code such as `payload["mode"]` is not read as a glob); an unreadable plan, no `Files to Modify` section or one that names no file means run; before a plan is written the answer is `auto -> run (once a plan is written)`; and once a design round has run for the workflow the answer stays run, so a revision cannot switch the loop off half way. `null` means the default, `auto`. |
| `review.design.max_iterations` | int ≥ 0 | Design review rounds (review → triage → revise), counted apart from `max_review_iterations` (default 2). The round that reaches the limit still gets its revision; the limit refuses only the re-review after it. `1`: one round, one revision, then ask. `0`: no design review. `budgets.architect` (default 3) covers the design plus one revision per round at the default; raise it with `max_iterations`, and by one more if changes asked for at approval are expected. |
| `design.require_approval` | bool | `true` (default) makes `run implementer` refuse (exit 5) while `.ai/plan.md` exists and the plan as it is now has not been approved with `design approve` -- after the user said yes. `false` is for runs nobody is watching (CI, batch), and restores the behaviour from before the gate existed. Top-level rather than under `review.design`: approval matters whether or not the panel reviewed the plan. `--force` does not bypass it; only this setting does. Taken only from the global config: in the project file it is ignored ([below](#what-the-project-file-may-not-loosen)). |
| `design.resume.max_age_seconds` | int ≥ 0 \| null | How old the last architect run may be for `run architect --resume` to continue its session (default 3600, how long the CLI kept its prompt cache when this was measured). Older, the revision runs fresh with the full prompt. `0` always runs fresh; `null` means the default. |
| `design.resume.max_context_tokens` | int > 0 \| null | The largest context, in tokens, a session may have ended with and still be continued (default `null`: no cap). Every run records its `context_tokens`, so a cap can be set from what was measured. Setting one of the two keeps the other's default. |
| `optimization.level` | `aggressive` \| `balanced` \| `quality` | How hard to try to be cheap. Default `balanced`. See below. |
| `optimization.high_risk_paths` | list | Globs that force `quality` for a change touching them. Replaces the default list wholesale. |
| `optimization.extra_high_risk_paths` | list | Globs added to `high_risk_paths` rather than replacing it (default `[]`). A hit escalates exactly as a `high_risk_paths` hit does. Like every list, a project value replaces a global one. |
| `optimization.low_risk_max_files` | int | Below `quality`, at most this many files still counts as a small change (default 5). |
| `optimization.low_risk_max_lines` | int | And at most this many changed lines (default 150). |
| `optimization.skip_unneeded_roles` | bool | `true` (default): a `test` or `architecture` seat that always runs sits out a round with nothing for it, in code review and design review and at every level, `quality` included -- and so does a `security` seat that opts in with `relevance: security`; one that does not always runs. `false` runs every such seat every round, as before. See `references/reviews.md` ("Roles a round does not need"). |
| `optimization.security_paths` | list | What the `security` rule looks for, for a seat with `relevance: security`, beside the high-risk patterns: request handling and input, file, URL, client, query and database access, execution and deserialisation, configuration and dependency manifests, plus every default high-risk pattern. Replaces the default list wholesale; `[]` leaves only the high-risk patterns (`doctor` reports that). |
| `optimization.extra_security_paths` | list | Globs added to `security_paths` (default `[]`). |
| `optimization.architecture_paths` | list | What the `architecture` rule looks for: contracts and schemas, module surface, configuration and record formats, the CLI, the build. Replaces the default list wholesale; `[]` leaves only the size and directory tests (`doctor` reports that). |
| `optimization.extra_architecture_paths` | list | Globs added to `architecture_paths` (default `[]`). |
| `workspace.dir` | string | Where `.ai/` artifacts go, the approval record included; a relative path is joined to the repository root. One outside the repository -- absolute, rooted, or climbing out with `..` -- is taken only from the global config: in the project file it is ignored and `.ai` (or the global value) is used ([below](#what-the-project-file-may-not-loosen)). Anything but a non-empty string is refused; a command that reads the file without validating it uses `.ai` instead. |
| `workspace.stale_notice_days` | int 0–36500 | When a new workflow starts, its first command notes, once and on stderr, the other workflows whose last activity (`updated_at`, else `started_at`, in `state.json`) is this many days old or more (default 30). The current workflow is left out, and so is any workflow with a stage in flight: that mark clears only when that workflow itself runs `status`, so a workflow abandoned mid-stage is never named here; `workflow list` shows it as `in flight`. A workflow with no usable timestamp, or an unreadable `state.json`, is not counted. Nothing is deleted: `workflow remove <id> --yes` is still the only thing that deletes one. `0` turns the note off; `null` means the default. The note never changes what the command does. |
| `language.reply` | string \| null | The language the orchestrator answers the user in, as a language tag: `ja`, `zh-TW`, `ko`, `ru`, `en`, `es`, `fr` and so on (a primary subtag of two or three letters, then any further subtags; the primary one is read in lower case). `null` (default) leaves the choice to SKILL.md rule 11: the language the user asked for, else the one they write in. Write Norwegian as `"no"` in a file: a bare `no`, like `yes`, `on` and `off`, is read as true or false and refused, while `config set language.reply no` keeps it a tag. Unlike other keys, a `null` in the project file is not inherited through: it undoes a tag the global file sets, for that project only. Set, `doctor` prints it as *Reply language* on every host, and in Claude Code three hooks remind the orchestrator of it before each prompt and, for a language they can judge (by its script, or for English, Spanish, French, German, Portuguese and Italian by their common words), ask once for a reply clearly in another language to be written again. Setting it with `config set` or `config setup --language` in a file that held none also adds those hooks to Claude Code's user settings (`~/.claude/settings.json`, or under `$CLAUDE_CONFIG_DIR`) when that directory exists -- a value only a project's file holds never does -- and clearing it so that neither the global file nor the project's sets one removes them; `--no-hooks` leaves the settings alone, and `hooks install` / `hooks uninstall` do it by hand. What each host does with it, how the hooks are written, and what the check can and cannot tell apart: `references/architecture.md` ("Reply-language hooks"). |
| `language.rewrite` | bool \| null | `true` (default): the Stop-hook check in Claude Code is on. `false` keeps the reminders and turns the check off, for a check that misjudges your replies. `null` means the default. |
| `<role>.options` | mapping | Provider-specific knobs; see below. |
| `<role>.model_tiers` | mapping | Named alternatives for this role's model; see below. Optional. |

### Role options

`options` is provider-specific on purpose -- there is no honest way to map
Claude's permission modes onto Codex's sandbox policies, so the adapter that
owns the CLI owns its own keys. The adapter validates them, so a typo is caught
by `config validate`, not at run time.

| Provider | Key | Values |
| --- | --- | --- |
| any | `args` | List of extra CLI arguments, appended verbatim |
| `claude` | `output_format` | `stream-json` (default), `text`, `json`. `text` disables stall detection |
| `claude` | `permission_mode` | Whatever the installed CLI advertises for `--permission-mode` (`dev-orchestra model list` aside, run `claude --help` to see them). On a write role, taken only from the global config (or `--extra`) |
| `codex` | `sandbox` | `read-only`, `workspace-write`, `danger-full-access`. On a write role, taken only from the global config (or `--extra`) |
| `codex` | `approve` | `true` (default) passes `--approve-for-me`; `false` omits it. On a write role, taken only from the global config (or `--extra`) |
| `agy` | `skip_permissions` | `true` passes `--dangerously-skip-permissions` on `implement` runs, so the implementer can run commands; default `false`. Taken only from the global config (or `--extra --dangerously-skip-permissions`) |
| any | `idle_timeout` | Override the no-output deadline for this role: a number of seconds above 0, or null. `0`, a negative, `true` or a string is refused by `config validate` |

```yaml
# In the global config: the project file's permission_mode and args are refused.
implementer:
  provider: claude
  model:
    family: opus
    version: latest
  options:
    # The default, acceptEdits, auto-approves file edits but not shell commands,
    # so an Implementer told to "run the tests" may be unable to. Loosen it here
    # if your environment makes that appropriate.
    permission_mode: bypassPermissions
    args: ["--add-dir", "../shared-lib"]
```

On a read-only role (the orchestrator, the architect, their tiers, every
reviewer) `--add-dir <path>` is the only raw argument Claude accepts, and Codex
accepts none. It is also taken only from the global config or from `--extra`:
the same `args` in the project file makes that role's runs refuse, whatever
they hold, because the project file can come with the branch under review, and
a branch that names its own reviewers' directories can widen what they read. An
implementer or review fixer, on a CLI other than `claude`, `codex` and `agy`,
takes `args` from either file. On those three, a write role takes neither its
permission options -- Claude's `permission_mode`, Codex's `sandbox` and
`approve`, agy's `skip_permissions` -- nor any `options.args` from the project
file: a project file that names one of them on the implementer, the review
fixer or one of their tiers has that role's `implement` runs refused, whatever
the value, and `config validate` and `doctor` say so. They come from the global
config, or from `--extra` for one run (`--extra --permission-mode
bypassPermissions`, `--extra --dangerously-skip-permissions`).

**A read-only seat on agy is taken only from the global config.** agy has no
read-only mode, so a plan or review run on it can modify the working tree,
`.ai/` (including the approval record), `.git/` and files outside the
repository, and nothing checks afterwards. Set in the global file -- or put
there by the global preset's fit, on a machine with neither Claude, Codex nor
a user adapter eligible for a seat ([Presets](#presets)) -- such a seat
-- the orchestrator, the architect, a tier of either, a reviewer -- runs, and
is warned about by `config set`, `reviewer add`, `reviewer set`, `config
validate`, the wizard, `doctor` (as a note; `--strict` still passes), `run`
and `review run`. The same seat from the project file is refused: `run` exits
2, the reviewer fails in its round while the others run, and `config validate`
and `doctor` report it (so `doctor --strict` fails). `config set --scope
project architect.provider agy` (and `orchestrator.provider`,
`<role>.model_tiers.<tier>.provider`, `reviewers[<n>].provider`,
`reviewers_extra[<n>].provider`), `reviewer add
--scope project --provider agy` and `reviewer set --scope project --provider
agy` exit 2 without writing. The refusal names the command that sets the seat
globally:

```bash
dev-orchestra config set --scope global architect.provider agy
dev-orchestra reviewer add --scope global --provider agy
```

A project file that sets any `reviewers[i].<key>` holds the whole panel (the
list is replaced whole), so a global agy reviewer copied into it becomes a
project one and is refused, with the same message. An agy seat that came from
the preset's fit, or a global extra on agy, with no reviewers in the global
file, is not copied: the writer leaves it out and says so ([Presets](#presets)).
A project file's `reviewers_extra` is refused the same way as its `reviewers`.

**Options that would loosen a read-only stage are ignored or refused.** The
orchestrator, the architect and every reviewer always run read-only, whatever
`permission_mode` or `sandbox` says -- that invariant is what makes an
independent review worth anything. `dev-orchestra doctor` lists any option it
is ignoring for that reason rather than dropping it silently. Any other raw
argument in `options.args` makes that role's runs refuse (exit 2, and a failed
reviewer in a review round); `config validate` and `doctor` warn about it
before a run does.

The alternative to loosening a permission mode is allow-listing the specific
commands in the CLI's own settings (for Claude Code, a `permissions.allow` entry
such as `Bash(pytest:*)` in `.claude/settings.json`). That is narrower, and it
lives with the project rather than with this skill.

Read-only Claude runs pass `--restricted`, so project and user `settings.json`
files are not read there at all. **`permissions.deny` is not read either**: a
deny rule such as `Read(./.env)` that keeps the model away from a secret in the
repository does not apply to architect or reviewer runs. Managed settings still
apply, so move such rules there. The implementer and the review fixer read
their settings files as before. Those read-only runs read only inside the
working directory and `--add-dir`: with the workspace container outside the
repository (`workspace.dir` set to an absolute path), add `--add-dir <container>`
to that role's `options.args` in the global config, or pass it with `--extra`.

`mock` is a real, registered provider: an offline adapter used by the tests and
useful for dry-running the pipeline without spending tokens. It is hidden from
the setup wizard.

### What the project file may not loosen

The project file can come with the branch under review, so the gates a branch
could use to wave itself through are treated apart.

**Plan approval and a workspace outside the repository come only from the
global config.** `design.require_approval` in the project file, whatever its
value, and a `workspace.dir` there that is absolute or resolves outside the
repository (`/srv/ai`, `C:ai`, `../ai`) are ignored: the global value, or the
default, is used. So is a relative one that a symlink or junction in the
repository takes outside it once followed (`dir: inner` with `inner` a link to
`/srv/ai`), since a branch can commit the link as easily as the file. A
`workspace.dir` holds the approval record, so a project file pointing it
elsewhere could bring an approval nobody gave. A relative `workspace.dir`
inside the repository is still the project's to choose, and a `null` approval
means the default, which is required. The global value and the default are
not checked for links: a `.ai` you link elsewhere keeps working. `config
validate` (as a warning) and `review run` (as a `warning:`) say what was
ignored and name the `--scope global` command. `doctor` reports it as a
problem, so `--strict` fails, when taking it would have loosened what is in
force -- a project `false` over a required approval, or a workspace other than
the one used -- and as a note otherwise, as for a project
`design.require_approval: true` over a required approval. A refused `run
implementer` adds a `note:` when the project file tried to turn approval off.
`config set design.require_approval false` writes the global file even inside
a project that has its own, as `preset` does, and so does `config set
workspace.dir <outside>`; `--scope project` with either, as the key itself or
inside a `design` or `workspace` block written whole, exits 2 and writes
nothing.

This keeps the record where your own commands put it; it cannot keep a branch
from bringing a record with it. The approval `run implementer` reads is the
`design_approval` entry of `state.json` in the workflow's directory under the
workspace, checked against the plan beside it and the last design review
round, so a branch that commits those files inside the workspace -- through a
committed `.ai` link as well, which is not checked -- brings an approval too.
The workspace's own `.gitignore` keeps dev-orchestra's files out of your
commits, not out of a branch that adds them; look at what a branch commits
under the workspace.

**The other review gates still take effect from the project file**, since a
repository may mean to review less, but when the project file makes one looser
than the configuration without it -- the global file, its preset's fit and the
defaults -- `config validate` warns, `doctor` notes it (`--strict` still
passes; `--json` lists them under `config.loosened`) and `review run` prints a
`warning:` before the round. Only a gate the project file writes is compared,
read as a run reads it. Looser means:

| Gate | Looser when the project file |
| --- | --- |
| `reviewers` | leaves out a reviewer id the code panel would otherwise have (`[]` leaves out all) |
| `review.design.reviewers` | leaves out a reviewer id the design panel would otherwise have; set by its own list, or by a `reviewers` list that takes the fitted design panel out |
| `review.max_review_iterations` | lowers it |
| `review.re_review_severities` | leaves out a severity, read as `review status` reads it: in any case, and a value `config validate` refuses (`[]`, a single name not in a list, an unknown name) as the default, `critical` and `high` |
| `review.exclude` | adds a pattern, withholding more of the diff |
| `review.max_findings` | asks each reviewer for fewer findings, counting `null` as the level's cap and `0` as none |
| `review.design.enabled` | moves it down `true` → `auto` → `false` |
| `review.design.max_iterations` | lowers it |
| `optimization.level` | moves it down `quality` → `balanced` → `aggressive` |
| `optimization.high_risk_paths` | leaves out a pattern, counting `extra_high_risk_paths` with it |
| `optimization.low_risk_max_files`, `optimization.low_risk_max_lines` | raises it |
| `optimization.skip_unneeded_roles` | turns it on over a `false` |
| `optimization.security_paths`, `optimization.architecture_paths` | leaves out a pattern, counting its `extra_` list with it |

A `reviewers_extra` only adds, so it is never one. A stricter value is not
reported, and neither is a panel dealt differently because the project file
sets the implementer.

## Optimization level

One dial over three savings. Default `balanced`.

| | `aggressive` | `balanced` | `quality` |
| --- | --- | --- | --- |
| Tests recorded as failing | refuse | refuse | review anyway |
| No test result recorded | warn, review | warn, review | review |
| Small, low-risk change | 1 reviewer | 1 reviewer | whole panel |
| Findings asked for | 4 | 6 | 10 |

`--force` gets past the refusal. `--only` overrides the reduced panel, because
that flag is someone naming the reviewers by hand.

**The gate reads a recorded result, it does not run anything.** This tool has
no way to know a project's test command -- the orchestrator discovers it from
the repository and runs it directly -- so the gate reads whatever the last
`dev-orchestra state record test ok|failed` wrote. That is three states, not
two: passed, failed, and never recorded. Only a recorded failure refuses;
never recorded warns and continues, so a workflow that has not adopted
`state record` keeps working exactly as before.

**A high-risk change escalates to `quality`, whatever is configured.** A
change touching authentication, secrets, payments, migrations, SQL, crypto or
deploy configuration gets the full panel and the full findings budget. The
patterns are configurable (`optimization.high_risk_paths`, `[]` to clear
them); the fact that a match escalates is not. The escalation is printed with
the file and pattern that caused it, so it can be checked rather than only
obeyed.

The patterns over-match on purpose. `authors_controller.rb` matches `*auth*`
and costs one extra reviewer; missing `auth_controller.rb` costs an
authorisation bug.

**`balanced` reduces the panel too, as of 0.4.2.** Restricting that to
`aggressive` made it unreachable in the repositories that most needed it: a
high-risk match escalates to `quality`, and `quality` is not `aggressive`, so
in an infrastructure repository where `*.tf` matches on most rounds the dial
could not fire at all. Measured over eleven real rounds at `balanced`: the
panel was reduced zero times, and no round came close to the old 2 file / 50
line thresholds either. Both were raised at the same time.

`quality` is now the only level that always pays for the whole panel, which is
what that level means -- the whole panel of roles this round has something
for: at every level, `quality` included, a `test` or `architecture` seat --
or a `security` seat that opts in with `relevance: security` -- with nothing
to read sits out unless the round is high-risk (`optimization.skip_unneeded_roles`; see `references/reviews.md`,
"Roles a round does not need").

**Size is never the only test.** The reduced panel needs the change to be
under both thresholds *and* to touch nothing high-risk, because one line in an
auth file is the exact shape an authorisation bug arrives in.

```yaml
optimization:
  level: aggressive
  low_risk_max_files: 3
  low_risk_max_lines: 80
```

### Reviewers that run only on high-risk changes

A specialist such as a `security` reviewer can be made to join the code
review only when the round is judged high-risk, and cost nothing otherwise:

```yaml
reviewers:
  - id: claude-general
    provider: claude
    role: general            # when: always (default)
  - id: claude-security
    provider: claude
    role: security
    when: high-risk          # always (default) | high-risk; code review, and a design panel of its own
optimization:
  extra_high_risk_paths: ["*/providers/*", "*/config.py"]   # added to high_risk_paths; default []
```

**A `when: high-risk` reviewer runs on a code round when the change matches a
high-risk path, when the orchestrator declares the round high-risk with
`review run --high-risk`, or when it must re-check its own open accepted
finding on an incremental round or a re-run of the same snapshot. Otherwise it
is left out.** A design round on the code panel always runs it; a seat of a
design panel of its own joins a plan by the same judgement, on the plan's
tokens ([above](#a-design-panel-of-its-own-reviewdesignreviewers)).
`--only` naming it runs it. The judgement is the one
that escalates a round to `quality`; every decision to add or leave out a
conditional reviewer is printed and recorded with its reason. See
`references/reviews.md` for how a round decides.

Two rules keep such a panel able to review anything, and `config validate`,
`doctor` and `review run` all refuse a configuration that breaks either:

- **At least one reviewer must run always.** A panel where every reviewer is
  `high-risk` would leave a quiet round with nobody to review it.
  `reviewer set --when high-risk` on the last unconditional reviewer, and
  `reviewer remove` of it, are refused.
- **A pattern must be in force.** `high_risk_paths: []` with no
  `extra_high_risk_paths` leaves a `high-risk` reviewer nothing to be judged by,
  so it would never run.

`extra_high_risk_paths` adds to whichever `high_risk_paths` list is in force, so
a repository can name the one path the defaults miss without copying the thirty
they already cover. **Every hit on an extra pattern escalates the round to
`quality` and lets a red tree through the gate, exactly as a `high_risk_paths`
hit does**, so add only paths whose changes deserve the whole panel. Between
config layers it replaces like every list: a project that sets it discards the
global value.

Roll it out in this order: add `extra_high_risk_paths` for the paths the
defaults miss first, then switch a reviewer to `when: high-risk`. The other way
round, the reviewer sits out the rounds the new patterns were meant to catch.
`doctor` notes a `high-risk` reviewer that is judged by the built-in patterns
alone, so a switch made without that first step does not go unnoticed.

```bash
dev-orchestra reviewer set claude-security --when high-risk
dev-orchestra reviewer set claude-security --when always      # back to every round
```

#### Reviewers scoped to paths

A domain specialist, such as a `database` reviewer, can be made to join the
code review only when the change touches the files it knows about:

```yaml
reviewers:
  - id: codex-database
    provider: codex
    role: database
    when:
      paths:
        - "*migration*/*"
        - "*migrate*/*"
        - "*.sql"
```

**A path-scoped reviewer joins a code round when a changed path matches one
of its own patterns, when it must re-check its own open accepted finding, or
when `--only` names it, and nothing else adds it.** Like any conditional
reviewer, it runs on every design review, it keeps the panel whole when it
joins, and every decision to add or leave it out is printed and recorded with
its reason. It counts as conditional for the "at least one reviewer must run
always" rule, and it needs no `high_risk_paths` pattern in force, since it
brings its own.

- **It is for domain specialists, not for roles that judge risk.** A database,
  frontend or docs reviewer is interested in a set of files; a security
  reviewer is interested in how risky the round is. Keep security and other
  risk-judging roles `when: high-risk` (or `always`): scoped to paths, a
  security reviewer sits out a change to `.env`, `*secret*`, `*.tf` or
  `.github/workflows/*` even though the round escalates. Membership that is
  both "on high-risk rounds" and "on these paths" is not offered. Do not get it
  by adding a specialist's path to `extra_high_risk_paths`: that changes the
  risk policy, escalating every such change to `quality` and letting a red
  tree through the gate for a path only one reviewer cares about.
- **A match never escalates.** The reviewer's patterns say what it knows, not
  how dangerous the change is, so a hit does not move the level or the gate.
  Escalation stays with `high_risk_paths` and `extra_high_risk_paths`. When a
  pattern is in both (the defaults include `*.sql`, `*migration*/*` and
  `*migrate*/*`), the round escalates through the high-risk list and the
  reviewer joins through its own, each with its own note.
- **A high-risk round does not add it.** A high-risk path hit and
  `review run --high-risk` add the `when: high-risk` reviewers only. When
  either happens and the path-scoped reviewer is left out, its note says so.
  `--only <ids>` naming every reviewer that should run includes it for one
  round; `reviewer set <id> --when high-risk` is the lasting fix.
- **It is matched against the change a reviewer sees**: the reviewed files,
  withheld files and rename sources, but not the orchestrator's own files or
  untracked files left out of an incremental round, which a reviewer is never
  shown.

The patterns are matched the way `high_risk_paths` is:

- against the whole path, and, for a pattern with no `/`, against the basename
  too, so `*.sql` matches at any depth;
- with no `**`: `db/**/*.sql` behaves as `db/*/*.sql`, and a directory pattern
  needs both forms (`migrations/*` and `*/migrations/*`), or `*migration*/*`;
  note that `*migration*/*` does not match `migrate`;
- **case-sensitively**, so list both cases when a repository mixes them;
- as written: a pattern that can never match is not reported.

Write the mapping in block form, as above, one pattern per line and every
pattern that starts with `*` in quotes. An unquoted `*.sql` is read as a YAML
alias by PyYAML and refused by the bundled parser used without PyYAML, which
also refuses an inline mapping and any pattern containing `[` inside a flow
list, quoted or not:

```yaml
when:
  paths:
    - "*.sql"
    - "*.SQL"
    - "*[Mm]igration*/*"
```

`reviewer add` and `reviewer set` write this form for you. Quote each pattern
on the command line too, so the shell does not expand it:

```bash
dev-orchestra reviewer add --provider codex --role database --when-paths "*migrate*/*" "*.sql"
dev-orchestra reviewer set codex-database --when-paths "*.sql"   # replaces the list
dev-orchestra reviewer set codex-database --when always          # back to every round
```

#### Suggesting path-scoped reviewers (`config suggest-roles`)

`config suggest-roles` reads the project's file names and proposes the
built-in specialists a set of paths can speak for: `database`, `frontend` and
`backend`, each with its own `when.paths`. It makes no model call and spends
no tokens. It writes nothing unless you pass `--write`:

```bash
dev-orchestra config suggest-roles            # what it would add, and why
dev-orchestra config suggest-roles --write    # add them to the project file's reviewers_extra
dev-orchestra config suggest-roles --json
```

For each proposal it prints the id, provider and family, the patterns, the
files that led to it, and how many listed files the patterns match, with how
many of those `review.exclude` withholds. Then it names every role it did not
propose, with the reason. `--provider` picks the CLI (default: the first
installed of `claude` and `codex`), and `--model` the family (default:
`sonnet` on Claude, `recommended-coding` on Codex).

**The files it reads.** Inside a git repository, only what `git ls-files`
lists: ignored and untracked files are not read. A `git ls-files` that fails,
times out (30 seconds) or prints more than 16 MiB is an error (exit 2), and
nothing is walked instead. Outside a git repository the directory is walked,
skipping dot-directories, the vendored and generated directories named below,
and anything more than 8 levels deep, and stopping at
20,000 files or after 10 seconds; the output then says the shares are
approximate. The only other file opened is the `package.json` at the listing
root, and only when it is tracked, a regular file, and at most 1 MiB.

**Two sets of files.** Match counts and shares are taken over every listed
file except the orchestrator's own (`workspace.dir` and the project file).
That includes the files `review.exclude` withholds, because a withheld file
still brings a path-scoped reviewer into a round. The evidence the rules read
leaves out files under a dot-directory, under `node_modules`, `vendor`,
`third_party`, `dist`, `build`, `target`, `venv`, `__pycache__`, `generated`,
`__generated__` or `gen`, under a test directory (`test`, `tests`,
`__tests__`, `spec`, `testdata`, `fixtures`), and files `review.exclude`
withholds. At most the first 20,000 evidence files are read; past that the
output says "evidence read from the first 20,000 files".

**The rules:**

| Role | Proposed when | Patterns |
| --- | --- | --- |
| `database` | A `migrations`, `migrate`, `alembic` or `prisma` directory holds at least 2 files, there are at least 2 `*.sql` files, or there is a `schema.prisma` | `<name>/*` and `*/<name>/*` per directory name, `*.sql`, and `schema.prisma` |
| `frontend` | At least 2 `*.tsx`, `*.jsx`, `*.vue` or `*.svelte` files | The extensions found |
| `backend` | An `api`, `server`, `backend`, `handlers`, `routes` or `controllers` directory holds at least 2 files | `<name>/*` and `*/<name>/*` per directory name, never a bare language extension |

- Names are found whatever their case, and each pattern is written in the case
  the repository uses, one pattern or pair per spelling: a `Migrations/`
  directory gives `Migrations/*` and `*/Migrations/*`.
- Frontend dependencies in `package.json` (react, vue, svelte,
  `@angular/core`, next, `@sveltejs/kit`, `@remix-run/*`) are evidence only;
  with no component files, `frontend` is skipped and the reason says so.
- A directory does not count for `backend` when at least half its files are
  components or SvelteKit `+` route files. `routes` never counts when
  `package.json` lists `@sveltejs/kit` or a `@remix-run/` package, or when
  `package.json` was not read and there is a frontend signal. When a name has
  directories that count and directories that do not, each one that counts is
  written by its full path (`backend/api/*`), so the pattern does not match
  the others.
- Each directory needs its 2 files on its own: two `migrations/` directories
  of one file each do not count.
- A role is skipped when it is already on the panel in force (fitted, listed
  or an extra, from either file), when it would need more than 12 patterns,
  or when its patterns match more than half the listed files: it would join
  most rounds, which keeps the panel whole.
- `general`, `security`, `architecture`, `performance` and `test` are never
  proposed: they judge risk or the whole change, not a set of paths.

**Where `--write` writes.** It appends to the `reviewers_extra` of the project
file in force from the current directory, or creates `.dev-orchestra.yaml` at
the listing root (the directory holding `.git`) when there is none, so the
panel keeps following what it followed. It refuses (exit 2, nothing written)
when the project file in force is not at the listing root, for example one in
the subdirectory you ran from; the dry run names the mismatch. It also
refuses when `reviewers_extra` is not a list, when the panel in force is not a
list, when no CLI a project reviewer can run on is installed and `--provider`
is not given, and, like every panel writer, before writing a panel problem it
would introduce, such as a `reviewers: []` that would leave only conditional
reviewers. `--provider agy` is refused with or without `--write`.

Each proposal runs in every code round touching its paths and in every design
review round, on the family above. Run it again after the repository changes:
a role already on the panel is skipped and named.

## Model tiers

The same role, the same prompt, a different model behind it. A one-line fix
handed to something cheap, a gnarly design handed to something expensive, a
second opinion handed to another vendor:

```yaml
implementer:
  provider: claude
  model:
    family: opus
    version: latest
  model_tiers:
    light:
      model:
        family: sonnet
        version: latest
    second-opinion:
      provider: codex
```

```bash
dev-orchestra run implementer --tier light --prompt-file .ai/execution/fix.md
```

A tier is a per-role override rather than a second role, because keeping two
definitions of what the implementer *is* would be the expensive part. Only
`provider`, `model` and `options` can be overridden; anything else is a
configuration error rather than a silently ignored key.

**Who picks is the caller.** Nothing infers a tier from the size of a diff.
The orchestrator is the only thing that knows how hard the task is, and a
wrong guess spends exactly what tiers exist to control.

**An unknown tier is an error, never a fallback.** A tier quietly ignored runs
the work on the default model and says nothing -- the expensive one when you
asked for cheap, or the cheap one when you asked for care.

**Each key is replaced whole, not merged into.** A tier naming a family over a
base pinned to an id would otherwise inherit the pin and run a model nobody
asked for. Changing `provider` drops the previous provider's `model` and
`options` with it: `opus` means nothing to Codex, and `permission_mode` is not
a thing it has. A tier that wants something specific on the new provider says
so.

Tiers are shown by `config show`, and a tiered run is recorded against its own
line in `tokens show`, so the question a tier raises -- did the cheaper one
actually cost less -- has an answer.

Reviewers have no tiers. The panel is already one model per reviewer, which is
the same routing by another name.

## Model families and version policy

Configuration stores **what kind of model you want**, not **which snapshot you
got**. `family: opus` + `version: latest` means "the newest Opus the installed
CLI offers", so the setup keeps working after a model release.

```yaml
implementer:            # tracks the latest Opus, whatever that is today
  model:
    family: opus
    version: latest

architect:              # frozen to one snapshot - only do this deliberately
  model:
    family: opus
    version: pinned
    id: claude-opus-5

review_fixer:           # let the CLI pick entirely
  model:
    family: default
    version: latest
```

Resolution happens at run time in the provider adapter, and an adapter that
cannot verify a family raises `ModelResolutionError` rather than sending a
guessed name to the CLI. The *resolved* id is recorded in `.ai/state.json` for
traceability; the config file keeps the family.

The adapter checks, in order:

1. What the installed CLI advertises — `claude --help` for aliases,
   `codex debug models` plus `$CODEX_HOME/config.toml` for Codex.
2. The provider's current aliases.
3. A built-in fallback list of **families only**, carrying the date it was last
   checked.

If none of those can verify the family, the run stops with an explanation.
`model list` shows what each source offers on this machine:

```bash
dev-orchestra model list
```

```
claude: installed
  fable    family=fable    source=cli-help
  opus     family=opus     source=cli-help
  sonnet   family=sonnet   source=cli-help
codex: installed
  CLI default (recommended coding model)  family=recommended-coding  source=cli-default
  gpt-6-astra (this CLI's configured model) family=gpt-6-astra       source=cli-config
  GPT-5.6-Terra                           family=gpt-5.6-terra       source=cli-catalog
```

`recommended-coding` resolves by *omitting* the `-m` flag — the CLI's own
current default is, by definition, current. Any other Codex family has to
appear in that listing: the adapter accepts the model the CLI is configured
with and the slugs its own catalogue publishes, and refuses everything else.
Adapter detail is in `references/providers.md`.

## What the user asks for, and what to run

The skill carries the command grammar; this is the phrasebook.

| The user says | Run |
| --- | --- |
| "show my configuration" | `config show` |
| "set this up" / "redo setup" | `config setup`, or `config setup --preset quality\|standard\|fast` |
| "best quality" / "spend less" | `config set preset quality`, `config set preset fast` ([Presets](#presets)) |
| "which models can I use?" | `model list` |
| "use Claude Opus for implementation" | `config set implementer.model.family opus` |
| "make the architect use Codex" | `config set architect.provider codex` **and** a family Codex accepts |
| "the implementer can't run the tests" | `config set --scope global implementer.options.permission_mode bypassPermissions` (on agy, `config set --scope global implementer.options.skip_permissions true`), or allow-list the command in that CLI's own settings |
| "review the design too" / "always review the design" | `config set review.design.enabled true` |
| "never review the design" | `config set review.design.enabled false` |
| "don't ask me to approve plans" / running in CI | `config set design.require_approval false` |
| "add a Codex security reviewer" | `reviewer add --provider codex --role security` |
| "run the security reviewer only on risky changes" | `reviewer set <id> --when high-risk` (see [above](#reviewers-that-run-only-on-high-risk-changes)) |
| "use opus on risky changes" | `reviewer set <id> --high-risk-model opus` (add `--design` for the design panel) |
| "review plans with a different panel" | `reviewer add --design …` (see [A design panel of its own](#a-design-panel-of-its-own-reviewdesignreviewers)), then `reviewer list --design` |
| "always run the security reviewer" | It does: a security seat sits out only with `relevance: security`, which `reviewer set <id> --relevance default` removes; any seat: `reviewer set <id> --relevance always`; every role every round: `config set optimization.skip_unneeded_roles false` |
| "make it three reviewers" | `reviewer add …` again, then `reviewer list` |
| "remove the performance reviewer" | `reviewer remove performance` |
| "change the second reviewer" | `reviewer set 2 --provider … --role …` |
| "just this project" | add `--scope project` to any write |
| "undo my own settings" | `config reset --scope global` (clears your overrides and keeps your preset; the preset fitted to this machine is what is left) |
| "drop this project's overrides" | `config reset --scope project` (the project then follows the global layer and its preset) |
| "my config is from an old version" | `config prune --dry-run`, then `config prune` |
| "check my environment" | `doctor` |
| "always reply in Japanese" (or any language) | `config set language.reply ja`, with that language's tag (`ko`, `zh-TW`, `fr`, ...) -- tell the user first that this adds hooks to their Claude Code user settings; `config set language.reply null` goes back to the language the user writes in and removes them |
| "stop asking for my replies to be rewritten" | `config set language.rewrite false` (the reminders stay) |

Show the resulting configuration after any write, so the user can confirm it.

## Editing

```bash
dev-orchestra config show                    # effective configuration
dev-orchestra config show --scope project    # just the project layer
dev-orchestra config setup                   # interactive wizard
dev-orchestra config setup --preset standard # non-interactive, a preset fitted to the installed CLIs
dev-orchestra config setup --defaults        # non-interactive, recommended values
dev-orchestra config set implementer.model.family sonnet
dev-orchestra config set --scope project architect.provider codex
dev-orchestra config set reviewers[1].role security
dev-orchestra config reset                   # clear this layer's overrides (the global file keeps its preset)
dev-orchestra config reset --delete          # remove the file entirely
dev-orchestra config prune                   # drop values equal to what is inherited
dev-orchestra config validate
```

**A saved config holds only what you set.** Everything else is resolved from
the layer below when the config is loaded, so a default improved in a later
release reaches your installation instead of being shadowed by the copy that
was current the day you ran setup. `config setup --defaults` therefore writes
`version: 1` and nothing else: choosing the recommended configuration is
choosing to override nothing, and such a file runs under preset `standard`.
`config setup --preset <name>` writes `version` and `preset`, and keeps what
the file already held beside the keys a preset governs; a
`review.design.reviewers` list goes, and `review.design.reviewers_extra`
stays. Of a role it removes
only `provider` and `model`: a role that still holds `options` or `model_tiers`
stays set, is therefore not fitted, and the printed notes say so.
`config show --scope global|project` prints the
layer exactly as it is on disk, and `config show` the configuration it resolves
to.

**Files written before 0.6.0 still hold every default**, which is how the
low-risk thresholds raised in 0.4.2 failed to reach anyone who had run setup
before them. `doctor` lists any setting whose value the built-in default has
moved off, with both numbers. `config prune` drops the values equal to what the
layer inherits, on request only -- a deliberate choice and an inherited default
look identical on disk, so this reads equality as evidence and says so:

```bash
dev-orchestra config prune --dry-run         # list what would be dropped
dev-orchestra config prune --scope project
```

A value is dropped only when the built-in defaults and the preset's fit on this
machine agree on it, so pruning never changes the configuration in force. A key
a preset governs that the file stopped setting would fall to the fit: under
`quality`, `optimization.level: balanced` or the default `opus` implementer
stays, and so does a role or a panel equal to this machine's fit alone. A role
is compared whole, since dropping its last field hands it to the fit.

A project file is pruned against *your* global layer, not against the built-in
defaults, so a value placed there to cancel a global one survives. The other
side of that: prune a `.dev-orchestra.yaml` the team shares only while your own
global layer overrides nothing, or the result will be shaped by your machine.

`config set` coerces values: `3` becomes an int, `true` a bool, `[a, b]` a list,
anything else a string. Use `--raw` to force a string.

Writes go to the project layer when one exists, otherwise the global layer;
`--scope global|project` decides explicitly. **Editing one entry of a list
writes the whole list**, because a list replaces the one below it wholesale:
`reviewers[1].role`, `review.exclude[0]` and `optimization.high_risk_paths[2]`
all copy the rest of the list from the layer below first -- the built-in
defaults and the preset's fit for the global layer, the global layer for a
project one. A project's panel therefore never ends up in your global file. An
index past the end of the list is an error (exit 2), not a new entry.
`reviewers_extra[0].role` is the exception: it edits the file's own extra and
copies nothing. A `reviewers[...]`, `reviewers_extra[...]` or
`review.design.reviewers...` path is checked before anything is written, and a
panel problem it would introduce is refused (exit 2) instead of written and
warned about.

### The wizard

On first use the skill notices there is no configuration and runs it:

```
AI Development Orchestrator setup

Detected CLIs:
  claude:  installed
  codex:   installed

Preset (fitted to the CLIs found above):
  1) quality  -- strongest models, Codex beside Claude on both panels, design review auto
  2) standard -- the built-in defaults: general on Claude and Codex, security on sonnet (opus on high-risk changes), test on sonnet (recommended)
  3) fast     -- lighter models, one reviewer plus a sonnet security one on high-risk changes; design review off
  4) customise each role
Choice [2]: 4

1. Orchestrator
   CLI:
     1) Claude Code (2.1.x) (recommended)
     2) Codex CLI (0.154.x)
   Model:
     1) sonnet [cli-help] (recommended)
     2) opus [cli-help]
     3) fable [cli-help]
     4) custom (type a family or exact model id)
...
5. External Reviewers
   How many reviewers? [4]
   reviewer #1  CLI / Model / Review role / id
   reviewer #2  CLI / Model / Review role / id
   reviewer #3  CLI / Model / Review role / id
   reviewer #4  CLI / Model / Review role / id
   Add another reviewer? [y/N]

Configuration
  Orchestrator    claude / sonnet / latest
  Architect       claude / fable  / latest
  Implementer     claude / opus   / latest
  Review Fixer    claude / opus   / latest
  Reviews
    1. claude / opus / latest / general / claude-general
    2. codex / recommended-coding / latest / general / codex-general
    3. claude / sonnet / latest / security / claude-security (opus when high-risk)
    4. claude / sonnet / latest / test / claude-test
    design review: auto  (review.design.enabled)
    optimization level: balanced  (optimization.level)
    skip unneeded roles: on  (optimization.skip_unneeded_roles)
    plan approval: required  (design.require_approval)
  Design reviews
    (the code panel; when conditions ignored)  (review.design.reviewers)

Save configuration? [Y/n]
```

The design panel there is the code panel's copy because the answers are saved
as `reviewers`; a preset saved as is shows its fitted design panel instead.

For the global file the first question is the preset. Choosing one shows the
configuration it resolves to on this machine, with its fitted design panel and
any notes on what was refitted, and asks `Save as is?`: yes saves `version`
and `preset` beside what the file held apart from the keys a preset governs,
`review.design.reviewers` included. No goes through the role and
reviewer questions starting from the preset's fit, and saves only what you
changed from it: a role you changed is saved whole, a panel you changed is
saved as a list, and everything you left as offered keeps following the preset.
A code panel saved as a list takes the design rounds off the preset's design
panel as well, and the wizard says so (`note: the reviewers differ from preset
standard's fit, so they are saved; design rounds then run them without when,
not the preset's design panel`).
A reviewer question keeps the seat's `high_risk_model` while you keep its CLI;
picking another CLI drops it with a line (`note: provider is now codex; its
high_risk_model was removed (reviewer set --high-risk-model sets another)`), as
`reviewer set --provider` does. It keeps the seat's `relevance` while you keep
its role (`always` whatever the role); picking another role drops it with a
line (`note: role is now general; its relevance security was removed (reviewer
set --relevance sets another)`), since a rule is about one role's work and a
general seat takes none.
The file's other settings are kept as they were, a value equal to a default
included.
`customise each role` is the wizard as it was before presets: every answer is
saved. A project file is never asked the preset question.

The wizard never asks about `reviewers_extra` and never writes it: the file
keeps the key as it held it, when a preset is chosen too, and a line before the
reviewer questions says so (`This file's reviewers_extra (<ids>) is kept as it
is; reviewer add/remove manage it.`). Nor does it ask about the design panel:
`review.design.reviewers` and its extras are kept as the file held them, and a
line says so (`This file's review.design.reviewers is kept as it is; reviewer
add/set/remove --design manage it.`). Every summary before saving is the
configuration `load()` will resolve, the extras marked `(extra, global file)`,
with the design panel under `Design reviews`; a seat with a high-risk model
shows `(opus when high-risk)`, and one with a `relevance` shows it.
Answers to the reviewer questions are still saved as `reviewers`, which the
panel then follows; to add a reviewer and keep following the inherited panel,
use `reviewer add`.

Files the wizard wrote before presets existed list every role and the panel, so
a `preset:` added to one reaches only what they do not set. `config reset`
clears them and keeps `preset`; `config prune` drops a value only when the
built-in defaults and the preset's fit agree on it.

`config setup` saves the answers you gave and nothing else. Its recommended
answers, and the summary you approve, come from whatever the layer you are
editing would inherit: setting up a project layer over a global one that chose
sonnet offers sonnet, and the summary shows the design review as on if your
global layer turned it on. So what you see before saving is what `config show`
reports afterwards.

An answer you accepted by pressing enter is still an answer, and is saved. For
the four roles that is visible later -- `doctor` reports a role whose family the
built-in default has moved off. **For `reviewers` it is not**: `doctor` never
compares a panel to the default one, because that would flag every installation
that added a reviewer. A panel written by the wizard therefore stays as it was
that day, silently. The two ways back are `config show --scope global|project`,
which shows the panel sitting in the layer, and `config prune`, which drops it
when it still equals the current default panel. If you want to override nothing
at all, `config setup --defaults`.

## Worked examples

**One vendor designs, another reviews** — a lineup that maps onto what the two
CLIs offered when this was written:

```yaml
version: 1

orchestrator:
  provider: codex
  model:
    family: gpt-5.6-sol
    version: latest

architect:
  provider: claude
  model:
    family: fable
    version: latest

implementer:
  provider: claude
  model:
    family: opus
    version: latest

review_fixer:
  provider: claude
  model:
    family: opus
    version: latest

reviewers:
  - id: claude-review
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-independent
    provider: codex
    model:
      family: gpt-5.6-terra
      version: latest
    role: general
```

Check those families with `dev-orchestra model list` before copying them: model
names change, and the two CLIs offer different ones per version and account.
Every adapter refuses a family the installed CLI does not vouch for instead of
guessing, so a stale name fails loudly during setup rather than quietly running
something else. The particular families matter less than the shape: two models
from the same family share the blind spot that produced the bug.

**A repo where the whole team should use the same panel** — commit
`.dev-orchestra.yaml`:

```yaml
version: 1
reviewers:
  - id: claude-general
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-security
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: security
  - id: codex-database
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: database
```

**Only one CLI installed** — drop the other provider everywhere:

```bash
dev-orchestra config set architect.provider claude
dev-orchestra reviewer remove codex-general
dev-orchestra reviewer add --provider claude --role security
```

**A cheap, fast loop for a small repo**:

```bash
dev-orchestra config set implementer.model.family sonnet
dev-orchestra config set review.max_review_iterations 1
dev-orchestra reviewer remove 2
```

**No reviews at all** (valid, and `doctor` will point it out):

```bash
dev-orchestra reviewer remove 1
dev-orchestra reviewer remove 1
```

## YAML dialect

Config files are parsed by PyYAML when it is installed, and otherwise by a
built-in parser covering block mappings, block sequences, inline empty
collections, inline scalar lists, comments, and quoted strings. Anchors,
aliases, tags, merge keys (`<<`), multi-document streams, and block scalars
(`|`, `>`) are rejected with a clear error, and so is an unquoted value that
starts with a character YAML reserves (`*.sql`, `&x`, `!x`, `@x`): quote it.
A sequence item may put any number of spaces after its `-`, as long as the
item's other lines line up with its first. JSON is always accepted.

Inside double quotes a backslash starts an escape, as in YAML: write a Windows
path as `"C:\\work\\new"` or in single quotes (`'C:\work\new'`). An escape YAML
does not define, such as the `\w` of `"C:\work"`, is refused rather than kept.
Indent with spaces; a tab in the indentation is refused, a tab inside a value
is kept. The commands that write a config quote a string that looks like a
number or a date (`"123"`, `"1.0"`, `"2026-10-06"`), so it reads back as text.
