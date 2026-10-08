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

# The same names install.sh writes, and what its copy carries.
SENTINEL=.dev-orchestra-install
PAYLOAD="plugin.json skills .claude-plugin .codex-plugin README.md LICENSE references scripts bin agents examples"
EXCLUDE_MARKER="# added by dev-orchestra install --antigravity"
CLAUDE_EXCLUDE_MARKER="# added by dev-orchestra install --claude"

# A full copy of a checkout, .git included, where the Claude install goes:
# what install.sh left under Git Bash, whose `ln -s` copies, before it
# checked for a link. It cannot be told from a clone, so it is explained,
# never removed.
explain_full_copy() {
  [ "$mode" = claude ] || return 0
  [ -e "$1/.git" ] && [ -f "$1/skills/$SKILL_NAME/SKILL.md" ] || return 0
  printf 'If it is a full copy of a checkout, .git included, that an earlier install.sh\n' >&2
  printf 'made under Git Bash, not a clone you work in, remove it once you have checked\n' >&2
  printf 'it holds nothing of yours:\n    rm -rf -- %s\n' "$(shell_quote "$1")" >&2
}

# $1 in single quotes, so that a printed command can be pasted as it is.
shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# Remove the install at $1, or stop. A link is removed only when it resolves
# to this checkout, a directory only when the installer wrote it (the
# sentinel is there, or it is an older Claude copy, and it is not a clone or
# the checkout itself), and anything else is left where it is.
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
  # The checkout itself, reached by its own path or through a link above it.
  if [ -d "$dest" ] && [ "$(cd -P -- "$dest" 2>/dev/null && pwd)" = "$(cd -P -- "$root" && pwd)" ]; then
    printf '%s is this checkout itself; removing it would delete the checkout.\n' "$dest" >&2
    printf 'The installer did not put it there. Delete the checkout by hand if it is no longer wanted.\n' >&2
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
    printf 'Remove it by hand if it is no longer wanted.\n' >&2
    explain_full_copy "$dest"
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

# Drop the entry $1 the installer added, and its marker $2. An entry without
# the marker right above it was there before and stays.
unexclude_marked() {
  [ -n "$target_project" ] || return 0
  exclude_file="$target_project/.git/info/exclude"
  [ -f "$exclude_file" ] || return 0

  # No grep first: install.ps1 writes CRLF line endings, which awk strips for
  # the comparison and `grep -x` does not. BINMODE keeps them in what is
  # written back (Git Bash's awk would drop them). awk exits non-zero when it
  # removed nothing.
  entry=$1
  tmp="$exclude_file.tmp.$$"
  awk -v BINMODE=3 -v m="$2" -v e="$entry" '
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
  # The Claude entry without a marker: what installers before the marker
  # wrote, or the user's own line. Either way it is not removed, only named.
  if [ "$mode" = claude ] && tr -d '\r' < "$exclude_file" | grep -qxF "$entry"; then
    printf 'Left %s in .git/info/exclude: the installer did not mark it as its own.\n' "$entry"
    printf 'An older installer may have added it; remove the line by hand if it is no longer wanted.\n'
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
  unexclude_marked "/.agents/plugins/$SKILL_NAME" "$EXCLUDE_MARKER"
  printf 'Restart Antigravity so that it stops loading the plugin.\n'
elif [ "$mode" = claude ]; then
  if [ -n "$target_project" ]; then
    dest="$target_project/.claude/skills/$SKILL_NAME"
  else
    dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/$SKILL_NAME"
  fi
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    release_destination "$dest"
    printf 'Removed %s\n' "$dest"
  else
    printf 'Nothing installed at %s\n' "$dest"
  fi
  unexclude_marked "/.claude/skills/$SKILL_NAME" "$CLAUDE_EXCLUDE_MARKER"
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
    # BINMODE: Git Bash's awk would otherwise drop the CR of every CRLF line.
    # The result ends with a newline only when the file did (eol).
    eol=1
    if [ -s "$agents_file" ] && [ -n "$(tail -c 1 "$agents_file")" ]; then eol=0; fi
    awk -v BINMODE=3 -v b="$begin" -v e="$end" -v eol="$eol" '
      index($0, b) { skip = 1 }
      !skip { printf "%s%s", sep, $0; sep = "\n" }
      index($0, e) { skip = 0 }
      END { if (eol && sep != "") printf "\n" }
    ' "$agents_file" > "$tmp"
    mv "$tmp" "$agents_file"
    printf 'Removed the pointer block from %s\n' "$agents_file"
  else
    printf 'No pointer block found in %s\n' "$agents_file"
  fi
fi

printf 'The checkout at %s was left in place.\n' "$root"
