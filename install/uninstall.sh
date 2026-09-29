#!/usr/bin/env sh
# Remove the AI Development Orchestrator skill.
#
#   ./install/uninstall.sh                   remove from ~/.claude/skills/
#   ./install/uninstall.sh --project /path   remove from <path>/.claude/skills/
#   ./install/uninstall.sh --codex           remove the AGENTS.md pointer block
#   ./install/uninstall.sh --antigravity     remove from ~/.gemini/config/plugins/
#   ./install/uninstall.sh --antigravity --project /path
#                                            remove from <path>/.agents/plugins/
#
# Your configuration is left alone. To remove it too:
#   dev-orchestra config reset --scope global --delete
set -eu

SKILL_NAME=dev-orchestra
here=$(cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname -- "$here")

mode=claude
target_project=""
saw_codex=0
saw_antigravity=0

while [ $# -gt 0 ]; do
  case $1 in
    --codex) mode=codex; saw_codex=1 ;;
    --claude) mode=claude ;;
    --antigravity|--gemini) mode=antigravity; saw_antigravity=1 ;;
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

if [ "$saw_codex" -eq 1 ] && [ "$saw_antigravity" -eq 1 ]; then
  echo "--antigravity and --codex cannot be combined" >&2
  exit 2
fi

# The same names install.sh writes.
SENTINEL=.dev-orchestra-install
EXCLUDE_MARKER="# added by dev-orchestra install --antigravity"

# $1 in single quotes, so that a printed command can be pasted as it is.
shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# Remove the install at $1, or stop. A link is removed only when it resolves
# to this checkout, a directory only when the installer wrote it (the
# sentinel is there and it is not a clone), and anything else is left where
# it is.
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
  if [ -d "$dest" ] && [ -f "$dest/$SENTINEL" ] && [ ! -e "$dest/.git" ] && [ ! -L "$dest/.git" ]; then
    rm -rf -- "$dest"
    return 0
  fi
  if [ -e "$dest" ]; then
    printf '%s exists and the installer did not write it; it was left in place.\n' "$dest" >&2
    printf 'Remove it by hand if it is no longer wanted.\n' >&2
    exit 1
  fi
}

# Drop the entry the installer added, and its marker. An entry without the
# marker right above it was there before and stays.
unexclude_marked() {
  [ -n "$target_project" ] || return 0
  exclude_file="$target_project/.git/info/exclude"
  [ -f "$exclude_file" ] || return 0

  # No grep first: install.ps1 writes CRLF line endings, which awk strips and
  # `grep -x` does not. awk exits non-zero when it removed nothing.
  entry="/.agents/plugins/$SKILL_NAME"
  tmp="$exclude_file.tmp.$$"
  awk -v m="$EXCLUDE_MARKER" -v e="$entry" '
    { line = $0; sub(/\r$/, "", line) }
    held { held = 0; if (line == e) { removed = 1; next } print m }
    line == m { held = 1; next }
    { print }
    END { if (held) print m; exit !removed }
  ' "$exclude_file" > "$tmp" && found=1 || found=0
  if [ "$found" -eq 1 ]; then
    mv "$tmp" "$exclude_file"
    printf 'Removed %s from .git/info/exclude\n' "$entry"
  else
    rm -f "$tmp"
  fi
}

if [ "$mode" = antigravity ]; then
  if [ -n "$target_project" ]; then
    dest="$target_project/.agents/plugins/$SKILL_NAME"
  else
    dest="$HOME/.gemini/config/plugins/$SKILL_NAME"
  fi
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    release_destination "$dest"
    printf 'Removed %s\n' "$dest"
  else
    printf 'Nothing installed at %s\n' "$dest"
  fi
  unexclude_marked
  printf 'Restart Antigravity so that it stops loading the plugin.\n'
elif [ "$mode" = claude ]; then
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
