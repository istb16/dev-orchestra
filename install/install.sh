#!/usr/bin/env sh
# Install the AI Development Orchestrator skill.
#
#   ./install/install.sh                     link into ~/.claude/skills/
#   ./install/install.sh --copy              copy instead of symlink
#   ./install/install.sh --project /path     into <path>/.claude/skills/
#   ./install/install.sh --codex             add an AGENTS.md pointer for Codex
#   ./install/install.sh --codex --project /path
#   ./install/install.sh --antigravity       link into ~/.gemini/config/plugins/
#   ./install/install.sh --antigravity --project /path
#                                            into <path>/.agents/plugins/
#   (--gemini is the same as --antigravity; restart Antigravity afterwards)
#
# SKILL.md is never duplicated: the Claude and Antigravity installs link (or
# copy) this checkout, and the Codex install writes a pointer to it.
set -eu

SKILL_NAME=dev-orchestra
here=$(cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname -- "$here")

mode=claude
target_project=""
use_copy=0
saw_codex=0
saw_antigravity=0

while [ $# -gt 0 ]; do
  case $1 in
    --codex) mode=codex; saw_codex=1 ;;
    --claude) mode=claude ;;
    --antigravity|--gemini) mode=antigravity; saw_antigravity=1 ;;
    --copy) use_copy=1 ;;
    --project)
      shift
      [ $# -gt 0 ] || { echo "--project needs a path" >&2; exit 2; }
      target_project=$1
      ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$saw_codex" -eq 1 ] && [ "$saw_antigravity" -eq 1 ]; then
  echo "--antigravity and --codex cannot be combined" >&2
  exit 2
fi

# What a copy install carries: the plugin payload, not .git, tests or CI.
PAYLOAD="plugin.json skills .claude-plugin .codex-plugin README.md LICENSE references scripts bin agents examples"

# Written into every copy the installer makes, so that a later run can tell a
# directory it made from one it did not.
SENTINEL=.dev-orchestra-install

# The project's .git/info/exclude gets the entry below this comment, and the
# uninstaller removes the entry only when the comment is right above it.
EXCLUDE_MARKER="# added by dev-orchestra install --antigravity"

# The pointer block tells the host how to run the CLI, so it has to name an
# interpreter this machine actually has. Distributions that ship Python 3 only
# as `python3` are common enough that a hardcoded `python` sends the agent to a
# command that is not there. A name that runs a Python older than 3.11 is
# passed over, the same as bin/dev-orchestra does.
python_cmd=python
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    python_cmd=$candidate
    break
  fi
done

exclude_from_project_git() {
  dest=$1
  [ -n "$target_project" ] || return 0
  git_dir="$target_project/.git"
  [ -d "$git_dir" ] || return 0

  entry=$(printf '%s' "${dest#"$target_project"/}")
  exclude_file="$git_dir/info/exclude"
  mkdir -p "$git_dir/info"
  [ -f "$exclude_file" ] || : > "$exclude_file"
  if ! grep -qxF "/$entry" "$exclude_file" 2>/dev/null; then
    printf '/%s\n' "$entry" >> "$exclude_file"
    printf 'Excluded /%s via .git/info/exclude (local only)\n' "$entry"
  fi
}

copy_payload() {
  mkdir -p "$1"
  for item in $PAYLOAD; do
    if [ -e "$root/$item" ]; then
      cp -R "$root/$item" "$1/"
    fi
  done
}

# $1 in single quotes, so that a printed command can be pasted as it is.
shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# Clear the way for an install at $1, or stop. A link is removed only when it
# resolves to this checkout, a directory only when this installer wrote it
# (the sentinel is there, or it is an older Claude copy, and it is not a
# clone or the checkout itself), and anything else is left where it is.
release_destination() {
  dest=$1
  if [ -L "$dest" ]; then
    target=$(cd -P -- "$dest" 2>/dev/null && pwd) || target=""
    if [ -n "$target" ] && [ "$target" = "$(cd -P -- "$root" && pwd)" ]; then
      rm -- "$dest"
      return 0
    fi
    printf '%s is a link to %s, not to this checkout; the installer did not make it.\n' \
      "$dest" "${target:-a path that does not exist}" >&2
    printf 'Remove it by hand if it is no longer wanted:\n    rm -- %s\n' "$(shell_quote "$dest")" >&2
    exit 1
  fi
  # The checkout itself, reached by its own path or through a link above it:
  # removing it would remove the checkout, and the skill is already there.
  if [ -d "$dest" ] && [ "$(cd -P -- "$dest" 2>/dev/null && pwd)" = "$(cd -P -- "$root" && pwd)" ]; then
    printf '%s is this checkout itself; replacing it would delete the checkout.\n' "$dest" >&2
    printf 'Nothing to install: it is already in place. To link or copy it, run the installer from a checkout somewhere else.\n' >&2
    exit 1
  fi
  if [ -d "$dest" ] && [ ! -e "$dest/.git" ] && [ ! -L "$dest/.git" ]; then
    if [ -f "$dest/$SENTINEL" ] || { [ "$mode" = claude ] && is_unmarked_copy "$dest"; }; then
      rm -rf -- "$dest"
      return 0
    fi
  fi
  if [ -e "$dest" ]; then
    printf '%s exists and the installer did not write it; it was left in place.\n' "$dest" >&2
    printf 'Remove it by hand if it is no longer wanted, then re-run.\n' >&2
    exit 1
  fi
}

# A Claude copy made before the installer wrote the sentinel: the skill and
# its CLI are there, and nothing at the top that a copy does not carry, so
# removing it loses nothing the checkout does not have.
is_unmarked_copy() {
  [ -f "$1/skills/$SKILL_NAME/SKILL.md" ] && [ -f "$1/scripts/orchestrator/__init__.py" ] || return 1
  for entry in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    case " $PAYLOAD " in
      *" ${entry##*/} "*) ;;
      *) return 1 ;;
    esac
  done
}

# Entries at the checkout root that Antigravity would load along with the
# skill. Kept in step with ANTIGRAVITY_AUTOLOAD in scripts/orchestrator/hosts.py.
autoload_entries() {
  for entry in hooks.json mcp_config.json plugins.json rules; do
    if [ -e "$root/$entry" ] || [ -L "$root/$entry" ]; then
      printf '%s\n' "$entry"
    fi
  done
  for entry in "$root"/agents/*.md; do
    if [ -e "$entry" ]; then
      printf 'agents/%s\n' "${entry##*/}"
    fi
  done
}

exclude_marked() {
  [ -n "$target_project" ] || return 0
  git_dir="$target_project/.git"
  [ -d "$git_dir" ] || return 0

  entry="/.agents/plugins/$SKILL_NAME"
  exclude_file="$git_dir/info/exclude"
  mkdir -p "$git_dir/info"
  [ -f "$exclude_file" ] || : > "$exclude_file"
  # tr: install.ps1 writes the file with CRLF line endings.
  if ! tr -d '\r' < "$exclude_file" | grep -qxF "$entry"; then
    # A last line without a newline would otherwise run into the marker.
    if [ -s "$exclude_file" ] && [ -n "$(tail -c 1 "$exclude_file")" ]; then
      printf '\n' >> "$exclude_file"
    fi
    printf '%s\n%s\n' "$EXCLUDE_MARKER" "$entry" >> "$exclude_file"
    printf 'Excluded %s via .git/info/exclude (local only)\n' "$entry"
  fi
}

install_claude() {
  if [ -n "$target_project" ]; then
    skills_dir="$target_project/.claude/skills"
  else
    skills_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
  fi
  mkdir -p "$skills_dir"
  dest="$skills_dir/$SKILL_NAME"

  if [ -e "$dest" ] || [ -L "$dest" ]; then
    release_destination "$dest"
    printf 'Replaced the existing install at %s\n' "$dest"
  fi

  if [ "$use_copy" -eq 1 ]; then
    copy_payload "$dest"
    printf 'Installed by install/install.sh from %s\n' "$root" > "$dest/$SENTINEL"
    printf 'Copied the skill to %s\n' "$dest"
    printf 'Re-run this installer after `git pull` to upgrade.\n'
  elif ln -s "$root" "$dest" 2>/dev/null; then
    printf 'Linked %s -> %s\n' "$dest" "$root"
    printf '`git pull` in the checkout now upgrades the skill in place.\n'
  else
    echo "Could not create a symlink; re-run with --copy." >&2
    exit 1
  fi

  # A per-project install drops a directory (usually a symlink to this git
  # checkout) inside someone else's repository. Left alone, `git add -A` there
  # fails with "does not have a commit checked out". Exclude it locally, which
  # touches neither their .gitignore nor their history. Only now: a failed
  # install leaves the exclude file as it was.
  exclude_from_project_git "$dest"
}

install_codex() {
  if [ -n "$target_project" ]; then
    agents_file="$target_project/AGENTS.md"
  else
    agents_file="${CODEX_HOME:-$HOME/.codex}/AGENTS.md"
  fi
  mkdir -p "$(dirname -- "$agents_file")"
  [ -f "$agents_file" ] || : > "$agents_file"

  begin="<!-- BEGIN $SKILL_NAME -->"
  end="<!-- END $SKILL_NAME -->"

  # Idempotent: drop any previous block before appending the current one.
  if grep -qF "$begin" "$agents_file" 2>/dev/null; then
    tmp="$agents_file.tmp.$$"
    awk -v b="$begin" -v e="$end" '
      index($0, b) { skip = 1 }
      !skip { print }
      index($0, e) { skip = 0 }
    ' "$agents_file" > "$tmp"
    mv "$tmp" "$agents_file"
  fi

  {
    printf '%s\n' "$begin"
    printf '## AI Development Orchestrator\n\n'
    printf 'When the request is to implement a feature or issue, investigate and fix a bug,\n'
    printf 'run a multi-model code review, orchestrate development across AI CLIs, or change\n'
    printf 'the development agent configuration, read and follow:\n\n'
    printf '    %s/skills/dev-orchestra/SKILL.md\n\n' "$root"
    printf 'Its helper CLI is:\n\n'
    printf '    %s %s/scripts/dev_orchestra.py <command>\n\n' "$python_cmd" "$root"
    printf 'That file is the single source of truth; do not rely on a copy of it.\n'
    printf '%s\n' "$end"
  } >> "$agents_file"

  printf 'Added the pointer block to %s\n' "$agents_file"
}

copy_for_antigravity() {
  copy_payload "$1"
  # Antigravity would load an agents/*.md as an agent definition; the payload
  # needs only the rest of agents/.
  for agent_file in "$1"/agents/*.md; do
    if [ -e "$agent_file" ] || [ -L "$agent_file" ]; then
      rm -f -- "$agent_file"
    fi
  done
  printf 'Installed by install/install.sh from %s\n' "$root" > "$1/$SENTINEL"
  printf 'Copied the plugin to %s\n' "$1"
  printf 'Re-run this installer after `git pull` to upgrade.\n'
}

install_antigravity() {
  if [ -n "$target_project" ]; then
    plugins_dir="$target_project/.agents/plugins"
  else
    plugins_dir="$HOME/.gemini/config/plugins"
  fi
  # First, so that a fresh home or a project path that does not exist yet
  # works, and before anything below resolves a path.
  mkdir -p "$plugins_dir"
  dest="$plugins_dir/$SKILL_NAME"

  # A link exposes the whole working tree, and Antigravity loads more than
  # skills/ from a plugin root. A copy carries only the payload, without
  # agents/*.md. Checked before anything is removed, so that a refused run
  # leaves the existing install in place.
  if [ "$use_copy" -eq 0 ]; then
    loaded=$(autoload_entries)
    if [ -n "$loaded" ]; then
      printf 'Refusing to link: Antigravity would also load these from the checkout:\n' >&2
      printf '%s\n' "$loaded" | sed 's/^/    /' >&2
      printf 'Remove them, re-run with --copy, or install from a clean worktree.\n' >&2
      exit 1
    fi
  fi

  if [ -e "$dest" ] || [ -L "$dest" ]; then
    release_destination "$dest"
    printf 'Replaced the existing install at %s\n' "$dest"
  fi

  if [ "$use_copy" -eq 1 ]; then
    copy_for_antigravity "$dest"
  elif ln_error=$(ln -s "$root" "$dest" 2>&1) && [ -L "$dest" ]; then
    printf 'Linked (symlink) %s -> %s\n' "$dest" "$root"
    printf '`git pull` in the checkout now upgrades the plugin in place.\n'
  else
    # Git Bash on Windows answers `ln -s` with a full copy of the checkout,
    # .git included, and success. The destination was empty a moment ago, so
    # what is there now is that copy: replace it with the payload.
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
      rm -rf "$dest"
      ln_error="ln -s made a copy, not a link"
    fi
    printf 'warning: could not create a symlink (%s); copying instead.\n' "${ln_error:-ln -s failed}" >&2
    copy_for_antigravity "$dest"
  fi

  # Only now: a failed install leaves the exclude file as it was.
  exclude_marked
  printf 'Restart Antigravity: a newly installed plugin directory is only discovered on startup.\n'
}

case $mode in
  claude) install_claude ;;
  codex) install_codex ;;
  antigravity) install_antigravity ;;
esac

printf '\nVerify with:\n    %s/bin/dev-orchestra doctor\n    %s/bin/dev-orchestra config setup --preset standard\n' "$root" "$root"
