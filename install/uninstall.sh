#!/usr/bin/env sh
# Remove the AI Development Orchestrator skill.
#
#   ./install/uninstall.sh                   remove from ~/.claude/skills/
#   ./install/uninstall.sh --project /path   remove from <path>/.claude/skills/
#   ./install/uninstall.sh --codex           remove the AGENTS.md pointer block
#
# Your configuration is left alone. To remove it too:
#   ai-orchestrator config reset --scope global --delete
set -eu

SKILL_NAME=ai-dev-orchestrator
here=$(cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname -- "$here")

mode=claude
target_project=""

while [ $# -gt 0 ]; do
  case $1 in
    --codex) mode=codex ;;
    --claude) mode=claude ;;
    --project)
      shift
      [ $# -gt 0 ] || { echo "--project needs a path" >&2; exit 2; }
      target_project=$1
      ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$mode" = claude ]; then
  if [ -n "$target_project" ]; then
    dest="$target_project/.claude/skills/$SKILL_NAME"
  else
    dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/$SKILL_NAME"
  fi
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    rm -rf "$dest"
    printf 'Removed %s\n' "$dest"
  else
    printf 'Nothing installed at %s\n' "$dest"
  fi
else
  if [ -n "$target_project" ]; then
    agents_file="$target_project/AGENTS.md"
  else
    agents_file="${CODEX_HOME:-$HOME/.codex}/AGENTS.md"
  fi
  begin="<!-- BEGIN $SKILL_NAME -->"
  end="<!-- END $SKILL_NAME -->"
  if [ -f "$agents_file" ] && grep -qF "$begin" "$agents_file"; then
    tmp="$agents_file.tmp.$$"
    awk -v b="$begin" -v e="$end" '
      index($0, b) { skip = 1 }
      !skip { print }
      index($0, e) { skip = 0 }
    ' "$agents_file" > "$tmp"
    mv "$tmp" "$agents_file"
    printf 'Removed the pointer block from %s\n' "$agents_file"
  else
    printf 'No pointer block found in %s\n' "$agents_file"
  fi
fi

printf 'The checkout at %s was left in place.\n' "$root"
