"""The reply-language hooks in the user's Claude Code settings.

dev-orchestra does not ship hooks in its plugin. It writes three entries into
the user's ``settings.json`` (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``) in
exec form: the absolute path of the Python that runs dev-orchestra, and a
small relay script in dev-orchestra's own config directory. The relay reads
the record beside it, which names the plugin checkout to run, so the entries
stay valid across plugin updates; a dev-orchestra command run inside Claude
Code from a checkout Claude Code installed moves the record to that checkout
(``refresh``).

The settings file is the user's: only entries shaped as ours and naming a
relay are changed, every other key keeps its place, the original bytes are kept in
one backup beside it before each write, and a file this does not understand
is refused rather than rewritten. Standard library plus ``config``.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import sys
import tempfile
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from . import config as config_mod
from . import reply_language
from .execution import DELEGATED_ENV

#: Moves Claude Code's user settings directory when set.
CLAUDE_CONFIG_ENV = "CLAUDE_CONFIG_DIR"
#: Set by Claude Code in the shell its Bash tool runs; ``workflow`` reads it too.
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"

SETTINGS_NAME = "settings.json"
BACKUP_SUFFIX = ".dev-orchestra-backup"
RELAY_NAME = "dev_orchestra_hook.py"
RECORD_NAME = "plugin.json"
RECORD_VERSION = 1
RELAY_VERSION = 1
HOOK_TIMEOUT = 10

#: ``(settings event, matcher, relay argument)`` for each hook, in the order written.
HOOK_EVENTS: Tuple[Tuple[str, Optional[str], str], ...] = (
    (reply_language.EVENTS["prompt"], None, "prompt"),
    (reply_language.EVENTS["session-start"], "compact|resume", "session-start"),
    (reply_language.EVENTS["stop"], None, "stop"),
)
_ARGUMENTS = frozenset(argument for _event, _matcher, argument in HOOK_EVENTS)

#: The relay's first line starts with this; an entry naming a file that does not is not ours.
RELAY_HEADER = "# dev-orchestra hook relay"

RELAY_TEXT = """\
%s (relay-version: %d). Written by `dev-orchestra hooks install`
# and refreshed by dev-orchestra itself: edits are overwritten. Runs the reply-language
# hook of the plugin checkout named in plugin.json beside it; prints nothing and exits 0
# when anything is missing or fails.
import json, os, runpy, sys


def trusted(path):
    # A file another user owns or can write could be swapped for their code.
    if not hasattr(os, "getuid"):
        return True
    info = os.stat(path)
    if info.st_uid not in (os.getuid(), 0) or info.st_mode & 0o002:
        return False
    return not info.st_mode & 0o020 or info.st_gid == os.getgid()


def main():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "plugin.json"), encoding="utf-8") as handle:
            record = json.load(handle)
        root = record["plugin_root"]
        scripts = os.path.join(root, "scripts")
        script = os.path.join(scripts, "hooks", "reply_language.py")
        if not os.path.isfile(script):
            return
        package = os.path.join(scripts, "orchestrator")
        for path in (root, scripts, os.path.dirname(script), script, package):
            if os.path.exists(path) and not trusted(path):
                return
        os.environ["DEV_ORCHESTRA_HOME"] = record["home"]
        os.environ["DEV_ORCHESTRA_CONFIG"] = record["config"]
        sys.argv = [script] + sys.argv[1:2]
        runpy.run_path(script, run_name="__main__")
    except BaseException:
        pass


main()
sys.exit(0)
""" % (RELAY_HEADER, RELAY_VERSION)

STATUS_INSTALLED = "installed"
STATUS_STALE = "stale"
STATUS_NOT_INSTALLED = "not-installed"
STATUS_UNREADABLE = "unreadable"

FIX_COMMAND = "dev-orchestra hooks install"
UNINSTALL_COMMAND = "dev-orchestra hooks uninstall"


class HooksError(Exception):
    """The settings file was not changed, and why."""


class Change(NamedTuple):
    """What ``install`` or ``uninstall`` did, or with ``dry_run`` would do."""

    settings_path: str
    added: List[str]
    removed: List[str]
    #: Whether the settings file changed (or, in a dry run, would change).
    changed: bool
    #: Where the original was copied, when it was and there was one.
    backup: Optional[str]
    #: The relay and record files written or removed.
    relay_files: List[str]
    dry_run: bool


# --------------------------------------------------------------------------- paths


def settings_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    environ = os.environ if environ is None else environ
    override = environ.get(CLAUDE_CONFIG_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".claude")


def settings_path() -> str:
    """Claude Code's user settings file; never a project's ``.claude/settings*.json``."""
    return os.path.join(settings_dir(), SETTINGS_NAME)


def plugins_dir() -> str:
    """Where Claude Code keeps the plugins it installed."""
    return os.path.join(settings_dir(), "plugins")


def hooks_dir() -> str:
    """Beside ``providers/``; the provider loader imports only ``providers/*.py``."""
    return os.path.join(config_mod.global_config_dir(), "hooks")


def relay_path() -> str:
    return os.path.join(hooks_dir(), RELAY_NAME)


def record_path() -> str:
    return os.path.join(hooks_dir(), RECORD_NAME)


def _slashes(path: str) -> str:
    return path.replace("\\", "/")


def _same_path(left: Any, right: Any) -> bool:
    """Equal paths, case-insensitively on Windows and with either separator."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    left, right = _slashes(os.path.normpath(left)), _slashes(os.path.normpath(right))
    if config_mod.on_windows():
        return left.lower() == right.lower()
    return left == right


def _within(path: str, directory: str) -> bool:
    """Whether ``path`` is ``directory`` or under it, once both are resolved."""
    path, directory = os.path.realpath(path), os.path.realpath(directory)
    if config_mod.on_windows():
        path, directory = path.lower(), directory.lower()
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:  # another drive
        return False


def in_virtual_environment() -> bool:
    return sys.prefix != sys.base_prefix


def python_command() -> str:
    """The Python running dev-orchestra, absolute but not resolved through links.

    A Microsoft Store Python's ``sys.executable`` is its app-execution alias;
    started through that alias the hook keeps the package identity, so its
    AppData reads are redirected exactly as dev-orchestra's own were. The real
    binary would read the real AppData. Unresolved, a Homebrew or pyenv path
    also stays the stable one rather than a versioned cellar directory.

    In a virtual environment it is the Python the environment was made from:
    the hooks run in every project, and an environment belongs to one, which
    may not be trusted (its ``.pth`` files run at each start, ``-I`` or not)
    and may be deleted. Raises ``HooksError`` when that Python is not found.
    """
    executable = _base_python() if in_virtual_environment() else sys.executable
    return _slashes(os.path.abspath(executable))


def _base_python() -> str:
    base = getattr(sys, "_base_executable", None)
    if isinstance(base, str) and base and not _same_path(base, sys.executable) and os.path.isfile(base):
        return base
    if config_mod.on_windows():
        names = ["python.exe"]
    else:
        names = [os.path.join("bin", "python3"), os.path.join("bin", "python")]
    for name in names:
        candidate = os.path.join(sys.base_prefix, name)
        if os.path.isfile(candidate):
            return candidate
    raise HooksError(
        "dev-orchestra runs in the virtual environment %s, and the Python it was made from was not found "
        "in %s; run dev-orchestra with a Python outside a virtual environment" % (sys.prefix, sys.base_prefix)
    )


# --------------------------------------------------------------------------- entries


def _group(matcher: Optional[str], python: Any, relay: Any, argument: str) -> Dict[str, Any]:
    hook = {
        "type": "command",
        "command": python,
        "args": ["-I", relay, argument],
        "timeout": HOOK_TIMEOUT,
    }
    group: Dict[str, Any] = {"matcher": matcher} if matcher else {}
    group["hooks"] = [hook]
    return group


def desired_entries(python: str, relay: str) -> Dict[str, Dict[str, Any]]:
    """The one matcher group of ours for each event."""
    relay = _slashes(relay)
    return {event: _group(matcher, python, relay, argument) for event, matcher, argument in HOOK_EVENTS}


def _names_relay(arg: Any) -> bool:
    """Whether ``arg`` is a relay, in whichever config directory.

    A ``hooks/dev_orchestra_hook.py`` that is this relay, holds the relay's
    header, or is gone -- an entry left behind when the config directory
    moved. A file of that name holding anything else is someone else's.
    """
    if not isinstance(arg, str):
        return False
    parts = _slashes(arg).lower().rsplit("/", 2)
    if len(parts) < 3 or parts[-1] != RELAY_NAME or parts[-2] != "hooks":
        return False
    if _same_path(arg, relay_path()):
        return True
    try:
        with open(arg, "rb") as handle:
            return handle.read(len(RELAY_HEADER)) == RELAY_HEADER.encode("utf-8")
    except FileNotFoundError:
        return True
    except OSError:
        return False


def is_ours(hook: Any) -> bool:
    """A command hook running ``-I <relay> <event argument>``, wherever the relay lives.

    Not tied to the config directory, so an install after ``DEV_ORCHESTRA_HOME``
    moved replaces the old entries instead of adding a second set; nor to the
    command, so one naming a Python since removed is replaced too.
    """
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    args = hook.get("args")
    return (
        isinstance(args, list)
        and len(args) == 3
        and args[0] == "-I"
        and isinstance(args[2], str)
        and args[2] in _ARGUMENTS
        and _names_relay(args[1])
    )


def describe(event: str, hook: Dict[str, Any]) -> str:
    """``Stop: <python> -I <relay> stop``."""
    raw = hook.get("args")
    args = raw if isinstance(raw, list) else []
    return "%s: %s" % (event, " ".join(str(part) for part in [hook.get("command"), *args]))


def _ours_in(groups: List[Any]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        found += [hook for hook in hooks if is_ours(hook)] if isinstance(hooks, list) else []
    return found


def _without_ours(groups: List[Any]) -> List[Any]:
    """``groups`` with our hooks taken out, and a group emptied by that dropped."""
    kept: List[Any] = []
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list) or not any(is_ours(hook) for hook in hooks):
            kept.append(group)
            continue
        remaining = [hook for hook in hooks if not is_ours(hook)]
        if remaining:
            kept.append(dict(group, hooks=remaining))
    return kept


def merged(
    settings: Dict[str, Any], install: bool, python: str = "", relay: str = ""
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """``settings`` with our entries installed or removed, and the entries added and removed.

    An event already holding exactly our group is left where it is, so a
    second install changes nothing. Every other key keeps its place.
    """
    data = copy.deepcopy(settings)
    hooks = data.get("hooks")
    events: Dict[str, Any] = hooks if isinstance(hooks, dict) else {}
    desired = desired_entries(python, relay) if install else {}
    added: List[str] = []
    removed: List[str] = []
    for event in list(events):
        groups = events[event]
        ours = _ours_in(groups)
        if not ours:
            continue
        wanted = desired.get(event)
        if wanted is not None and ours == wanted["hooks"] and wanted in groups:
            continue
        removed += [describe(event, hook) for hook in ours]
        groups = _without_ours(groups)
        if groups or (install and event in desired):
            events[event] = groups
        else:
            del events[event]
    for event, group in desired.items():
        groups = events.setdefault(event, [])
        if group in groups:
            continue
        groups.append(group)
        added += [describe(event, hook) for hook in group["hooks"]]
    if events:
        data["hooks"] = events
    elif removed:
        data.pop("hooks", None)  # emptied by us; an empty object the user wrote stays
    # An entry put back as it was is shown once, as added.
    removed = [entry for entry in removed if entry not in added]
    return data, added, removed


# --------------------------------------------------------------------------- the settings file


def _refusal(path: str, problem: str) -> HooksError:
    return HooksError("%s %s; it was not changed" % (path, problem))


def _read(path: str) -> Tuple[Dict[str, Any], Optional[bytes]]:
    """The settings and their bytes, or ``({}, None)`` when there is no file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        raise _refusal(path, "could not be read (%s)" % exc) from None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _refusal(path, "is not valid JSON (%s)" % exc) from None
    if not isinstance(data, dict):
        raise _refusal(path, "does not hold a JSON object")
    if "hooks" not in data:
        return data, raw
    hooks = data["hooks"]
    if not isinstance(hooks, dict):
        raise _refusal(path, "has a hooks value that is not an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise _refusal(path, "has a hooks.%s value that is not a list" % event)
        if not all(isinstance(group, dict) for group in groups):
            raise _refusal(path, "has a hooks.%s entry that is not an object" % event)
        for group in groups:
            inner = group.get("hooks")
            if not isinstance(inner, list) or not all(isinstance(hook, dict) for hook in inner):
                raise _refusal(path, "has a hooks.%s entry whose hooks is not a list of objects" % event)
    return data, raw


def read_settings(path: str) -> Dict[str, Any]:
    """The settings as Claude Code would read them; raises ``HooksError`` on anything unexpected."""
    return _read(path)[0]


def _current_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


class _Raced(Exception):
    """The file changed between being read and being replaced."""


#: ``_atomic_write``'s ``expected`` when the file is not compared before the move.
_UNCHECKED: Any = object()


def _write_target(path: str) -> str:
    """Where writing ``path`` lands: a linked file's target, so the link stays a link."""
    return os.path.realpath(path) if os.path.islink(path) else path


def _atomic_write(path: str, content: bytes, expected: object = _UNCHECKED) -> None:
    """Write ``content`` beside ``path`` and move it into place.

    With ``expected`` (the bytes read earlier, or None for no file) the file
    is read once more just before the move, and ``_Raced`` raised if it
    changed in between. A ``path`` that is a link -- a dotfile manager's, say
    -- has its target replaced, and the temporary file is made beside that.
    """
    path = _write_target(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    prefix = "." + os.path.basename(path) + "."
    handle, temporary = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass  # a new file keeps what mkstemp gave it
        if expected is not _UNCHECKED and _current_bytes(path) != expected:
            raise _Raced(path)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def write_settings(path: str, data: Dict[str, Any], original: Optional[bytes]) -> Optional[str]:
    """Replace the settings file with ``data``; the backup's path, if one was made.

    The original is copied byte for byte first: the rewrite normalises the
    file's formatting, and the backup is the only way back from that.
    """
    backup = None
    if original is not None:
        backup = _write_target(path) + BACKUP_SUFFIX
        _atomic_write(backup, original)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write(path, text.encode("utf-8"), expected=original)
    return backup


def _refuse_if_stored_elsewhere(path: str) -> None:
    real = config_mod.package_redirected(path)
    if real is not None:
        raise HooksError(
            "%s would be written to %s, where Claude Code does not read it (a Microsoft Store Python "
            "redirects it); it was not changed. Run dev-orchestra with a python.org Python, or add the "
            "entries `dev-orchestra hooks install --dry-run` prints by hand" % (path, real)
        )


_Edited = Tuple[str, List[str], List[str], bool, Optional[str]]


def _edit(install: bool, dry_run: bool, python: str = "", relay: str = "") -> _Edited:
    """Read, merge and write the settings, starting over once if another writer got in between."""
    path = settings_path()
    _refuse_if_stored_elsewhere(path)
    for attempt in range(2):
        data, original = _read(path)
        result, added, removed = merged(data, install, python, relay)
        changed = result != data
        if not changed or dry_run:
            return path, added, removed, changed, None
        try:
            backup = write_settings(path, result, original)
        except _Raced:
            if attempt:
                message = "%s changed while it was being edited; it was not changed. Try again" % path
                raise HooksError(message) from None
            continue
        except OSError as exc:
            raise HooksError("%s could not be written (%s)" % (path, exc)) from None
        return path, added, removed, True, backup
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- the relay


def desired_record(plugin_root: str, kept: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The record for ``plugin_root``: the config home and file in force, or ``kept``'s."""
    record = {
        "version": RECORD_VERSION,
        "plugin_root": _slashes(os.path.abspath(plugin_root)),
        "home": _slashes(config_mod.global_config_dir()),
        "config": _slashes(config_mod.global_config_path()),
    }
    if kept is not None and isinstance(kept.get("home"), str) and isinstance(kept.get("config"), str):
        record["home"], record["config"] = kept["home"], kept["config"]
    return record


def _record_bytes(plugin_root: str, kept: Optional[Dict[str, Any]]) -> bytes:
    record = desired_record(plugin_root, kept)
    return (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _relay_files(plugin_root: str, kept: Optional[Dict[str, Any]]) -> List[Tuple[str, bytes]]:
    return [(relay_path(), RELAY_TEXT.encode("utf-8")), (record_path(), _record_bytes(plugin_root, kept))]


def write_relay(plugin_root: str, dry_run: bool = False, kept: Optional[Dict[str, Any]] = None) -> List[str]:
    """Write the relay and its record where they differ; the paths written (or that would be).

    ``kept`` is a record whose config home and file the new one keeps.
    """
    written: List[str] = []
    for path, content in _relay_files(plugin_root, kept):
        if _current_bytes(path) == content:
            continue
        if not dry_run:
            _atomic_write(path, content)
        written.append(path)
    return written


def remove_relay(dry_run: bool = False) -> List[str]:
    removed: List[str] = []
    for path in (relay_path(), record_path()):
        if not os.path.lexists(path):
            continue
        if not dry_run:
            os.remove(path)
        removed.append(path)
    if not dry_run:
        try:
            os.rmdir(hooks_dir())
        except OSError:
            pass  # not empty, or already gone
    return removed


def _put_back(previous: List[Tuple[str, Optional[bytes]]]) -> None:
    """Restore the relay files to the bytes they held, removing those that did not exist."""
    for path, content in previous:
        try:
            if content is not None:
                _atomic_write(path, content)
            elif os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(hooks_dir())
    except OSError:
        pass  # not empty, or already gone


def refresh(plugin_root: str, environ: Mapping[str, str]) -> None:
    """Point an installed relay at this checkout; run before the commands but ``hooks``.

    Only inside Claude Code and never in a delegated run, so another host's
    checkout of another version does not keep moving the record back and
    forth. Only a checkout Claude Code installed (under its plugins
    directory), or the one already recorded: a command run once from a fork,
    a pull request's branch or a clone in a shared directory must not make
    that code the hook of every later session. Only the checkout moves; the
    config home and file stay those the record holds, so a one-off
    ``DEV_ORCHESTRA_CONFIG`` does not repoint the hooks. Nothing is created
    where nothing is installed, and no error reaches the command.
    """
    if not environ.get(SESSION_ENV) or environ.get(DELEGATED_ENV):
        return
    try:
        if not os.path.isfile(relay_path()):
            return
        record = _read_record()
        recorded = record is not None and _same_path(record.get("plugin_root"), os.path.abspath(plugin_root))
        if recorded or _within(plugin_root, plugins_dir()):
            write_relay(plugin_root, kept=record)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- status


def _read_record() -> Optional[Dict[str, Any]]:
    try:
        with open(record_path(), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def status(plugin_root: str) -> Dict[str, Any]:
    """Whether our hooks are in the settings file and would run this checkout."""
    path = settings_path()
    report: Dict[str, Any] = {
        "settings_path": path,
        "status": STATUS_NOT_INSTALLED,
        "reasons": [],
        "python": None,
        "relay": relay_path(),
        "plugin_root": None,
        "events": [],
        "hooks_disabled": False,
        "error": None,
    }
    try:
        settings = read_settings(path)
    except HooksError as exc:
        report["status"] = STATUS_UNREADABLE
        report["error"] = str(exc)
        return report
    report["hooks_disabled"] = settings.get("disableAllHooks") is True
    events: Dict[str, Any] = settings.get("hooks") or {}
    found = {event: _ours_in(groups) for event, groups in events.items()}
    found = {event: hooks for event, hooks in found.items() if hooks}
    report["events"] = sorted(found)
    record = _read_record()
    if record is not None and isinstance(record.get("plugin_root"), str):
        report["plugin_root"] = record["plugin_root"]
    if not found:
        return report
    hooks = [hook for event_hooks in found.values() for hook in event_hooks]
    commands = [hook.get("command") for hook in hooks]
    report["python"] = commands[0] if isinstance(commands[0], str) else None
    try:
        python: Optional[str] = python_command()
    except HooksError:
        python = None
    reasons: List[str] = []
    if any(not _same_path(command, python) for command in commands):
        reasons.append("python-differs")
    if any(not isinstance(command, str) or not os.path.lexists(command) for command in commands):
        # lexists: os.stat can fail on a Store app-execution alias that runs fine.
        reasons.append("python-missing")
    relays = [hook["args"][1] for hook in hooks]
    if not os.path.isfile(relay_path()) or any(not _same_path(arg, relay_path()) for arg in relays):
        reasons.append("relay-missing")
    elif _current_bytes(relay_path()) != RELAY_TEXT.encode("utf-8"):
        reasons.append("relay-outdated")
    wanted = desired_record(plugin_root)
    if record is not None and not _same_path(record.get("plugin_root"), wanted["plugin_root"]):
        reasons.append("plugin-root-differs")
    if record is None or any(not _same_path(record.get(key), wanted[key]) for key in ("home", "config")):
        reasons.append("record-differs")
    reasons += _entry_reasons(events, found)
    report["reasons"] = reasons
    report["status"] = STATUS_STALE if reasons else STATUS_INSTALLED
    return report


def _entry_reasons(events: Dict[str, Any], found: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    """``events-missing`` unless each event holds one hook of ours; ``entries-differ``
    unless each sits alone in a group shaped as ``install`` writes it.

    The command and the relay are left to their own reasons: the group is
    compared with one built from the hook's own.
    """
    if any(event not in found or len(found[event]) != 1 for event, _matcher, _argument in HOOK_EVENTS):
        return ["events-missing"]
    if set(found) - {event for event, _matcher, _argument in HOOK_EVENTS}:
        return ["entries-differ"]
    for event, matcher, argument in HOOK_EVENTS:
        (hook,) = found[event]
        holders = [group for group in events[event] if _ours_in([group])]
        if holders != [_group(matcher, hook.get("command"), hook["args"][1], argument)]:
            return ["entries-differ"]
    return []


def installed(report: Dict[str, Any]) -> bool:
    """Whether ``status`` found any entry of ours, current or not."""
    return report.get("status") in (STATUS_INSTALLED, STATUS_STALE)


# --------------------------------------------------------------------------- install


def install(plugin_root: str, dry_run: bool = False) -> Change:
    """Write the relay and its record, then merge our entries into the settings file.

    A settings file that would be refused is found before the relay is written.
    The relay comes first because the entries run it, and a hook whose script
    is missing exits with an error; when the settings are then not edited,
    the relay files are put back as they were.
    """
    _refuse_if_stored_elsewhere(settings_path())
    _read(settings_path())
    python = python_command()
    previous: List[Tuple[str, Optional[bytes]]] = []
    try:
        if not dry_run:
            previous = [(path, _current_bytes(path)) for path in (relay_path(), record_path())]
        relay_files = write_relay(plugin_root, dry_run)
        path, added, removed, changed, backup = _edit(True, dry_run, python, _slashes(relay_path()))
    except BaseException as exc:
        if not dry_run:
            _put_back(previous)
        if isinstance(exc, OSError):
            raise HooksError("the hook relay could not be written to %s (%s)" % (hooks_dir(), exc)) from None
        raise
    return Change(path, added, removed, changed, backup, relay_files, dry_run)


def uninstall(dry_run: bool = False) -> Change:
    """Take our entries out of the settings file, then delete the relay and its record."""
    path, added, removed, changed, backup = _edit(False, dry_run)
    try:
        relay_files = remove_relay(dry_run)
    except OSError as exc:
        raise HooksError("the hook relay in %s could not be removed (%s)" % (hooks_dir(), exc)) from None
    return Change(path, added, removed, changed, backup, relay_files, dry_run)


# --------------------------------------------------------------------------- the configured language


def configured_reply(cwd: Optional[str]) -> Tuple[bool, Optional[str]]:
    """``(readable, tag)``: ``language.reply`` from the global file with this project's over it.

    The files are read as the hook reads them. ``readable`` is False when one
    does not parse.
    """
    try:
        data = reply_language.file_settings(cwd or os.getcwd())
    except (config_mod.ConfigError, OSError):
        return False, None
    return True, config_mod.language_settings_of(data)["reply"]


class Layers(NamedTuple):
    """``language.reply`` as each file the hook reads holds it, read one by one rather than merged."""

    readable: bool
    global_tag: Optional[str] = None
    project_tag: Optional[str] = None

    def tag(self, scope: str) -> Optional[str]:
        return self.global_tag if scope == "global" else self.project_tag

    def any_tag(self) -> bool:
        return bool(self.global_tag or self.project_tag)


def configured_layers(cwd: Optional[str]) -> Layers:
    """Each file's own ``language.reply``; ``readable`` is False when one does not parse."""
    tags: List[Optional[str]] = []
    try:
        for path in reply_language.layer_paths(cwd or os.getcwd()):
            data = config_mod.read_config_file(path) if path and os.path.isfile(path) else {}
            tags.append(config_mod.language_settings_of(data)["reply"])
    except (config_mod.ConfigError, OSError):
        return Layers(False)
    return Layers(True, tags[0], tags[1])


SYNC_NOTHING = "nothing"
SYNC_INSTALLED = "installed"
SYNC_UNINSTALLED = "uninstalled"
#: A language newly set where Claude Code has no settings directory: nothing is created.
SYNC_NO_SETTINGS = "no-settings"
#: A language changed in a file that already set one while the hooks were not installed.
SYNC_LEFT_OUT = "left-out"


class Synced(NamedTuple):
    action: str
    change: Optional[Change] = None


def sync(scope: str, before: Layers, after: Layers, plugin_root: str) -> Synced:
    """Install or remove the hooks after a command wrote ``scope``'s file.

    Installs only when the command set a language in that file where it held
    none, or repairs hooks already there but out of date. A language from
    another file -- a project's, which a clone brings along -- is no consent
    to edit the user's Claude Code settings, and a file that already set one
    while the hooks were missing had them left out on purpose (``--no-hooks``,
    ``hooks uninstall``). Only where Claude Code's settings directory exists.
    Removes them only when the command took the language out of that file and
    neither the global file nor this project's sets one any more. Nothing when
    a file does not parse. ``HooksError`` from the install or the removal
    passes through: the configuration is saved either way.
    """
    if not before.readable or not after.readable:
        return Synced(SYNC_NOTHING)
    was, now = before.tag(scope), after.tag(scope)
    if now:
        state = status(plugin_root)["status"]
        if state == STATUS_INSTALLED:
            return Synced(SYNC_NOTHING)
        if was and state != STATUS_STALE:
            return Synced(SYNC_LEFT_OUT if now != was else SYNC_NOTHING)
        if not os.path.isdir(settings_dir()):
            return Synced(SYNC_NO_SETTINGS)
        return Synced(SYNC_INSTALLED, install(plugin_root))
    if was and not after.any_tag() and installed(status(plugin_root)):
        return Synced(SYNC_UNINSTALLED, uninstall())
    return Synced(SYNC_NOTHING)
