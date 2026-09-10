#!/usr/bin/env sh
# Install the AI Development Orchestrator skill.
#
#   ./install/install.sh                     link into ~/.claude/skills/
#   ./install/install.sh --copy              copy instead of symlink
#   ./install/install.sh --project /path     into <path>/.claude/skills/
#   ./install/install.sh --codex             add an AGENTS.md pointer for Codex
#   ./install/install.sh --codex --project /path
#
# SKILL.md is never duplicated: the Claude install links (or copies) this
# checkout, and the Codex install writes a pointer to it.
set -eu

SKILL_NAME=ai-dev-orchestrator
here=$(cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname -- "$here")

mode=claude
target_project=""
use_copy=0

while [ $# -gt 0 ]; do
  case $1 in
    --codex) mode=codex ;;
    --claude) mode=claude ;;
    --copy) use_copy=1 ;;
    --project)
      shift
      [ $# -gt 0 ] || { echo "--project needs a path" >&2; exit 2; }
      target_project=$1
      ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
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

install_claude() {
  if [ -n "$target_project" ]; then
    skills_dir="$target_project/.claude/skills"
  else
    skills_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
  fi
  mkdir -p "$skills_dir"
  dest="$skills_dir/$SKILL_NAME"

  if [ -e "$dest" ] || [ -L "$dest" ]; then
    printf 'Replacing existing install at %s\n' "$dest"
    rm -rf "$dest"
  fi

  # A per-project install drops a directory (usually a symlink to this git
  # checkout) inside someone else's repository. Left alone, `git add -A` there
  # fails with "does not have a commit checked out". Exclude it locally, which
  # touches neither their .gitignore nor their history.
  exclude_from_project_git "$dest"

  if [ "$use_copy" -eq 1 ]; then
    mkdir -p "$dest"
    # Copy the skill payload only -- not .git, tests, or CI config.
    for item in SKILL.md README.md LICENSE references scripts bin agents examples; do
      [ -e "$root/$item" ] && cp -R "$root/$item" "$dest/"
    done
    printf 'Copied the skill to %s\n' "$dest"
    printf 'Re-run this installer after `git pull` to upgrade.\n'
  elif ln -s "$root" "$dest" 2>/dev/null; then
    printf 'Linked %s -> %s\n' "$dest" "$root"
    printf '`git pull` in the checkout now upgrades the skill in place.\n'
  else
    echo "Could not create a symlink; re-run with --copy." >&2
    exit 1
  fi
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
    printf '    %s/SKILL.md\n\n' "$root"
    printf 'Its helper CLI is:\n\n'
    printf '    python %s/scripts/ai_orchestrator.py <command>\n\n' "$root"
    printf 'That file is the single source of truth; do not rely on a copy of it.\n'
    printf '%s\n' "$end"
  } >> "$agents_file"

  printf 'Added the pointer block to %s\n' "$agents_file"
}

case $mode in
  claude) install_claude ;;
  codex) install_codex ;;
esac

printf '\nVerify with:\n    %s/bin/ai-orchestrator doctor\n' "$root"
