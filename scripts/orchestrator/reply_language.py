"""The reply-language hooks the Claude Code plugin ships (``language.reply``).

``scripts/hooks/reply_language.py`` is the entry point; everything it does is
here, so it can be tested without a process per case. Three events:

- ``prompt`` (UserPromptSubmit) and ``session-start`` (SessionStart, after a
  compaction or a resume) add a short reminder naming the language;
- ``stop`` (Stop) reads the reply just written and, when it is clearly in
  another language, blocks once and asks for it to be written again.

Nothing is fixed to one language: the language name, the script judged and
every word of the reminder and the reason come from the configured tag.

Each acts only when ``language.reply`` is set, only in a session that used
dev-orchestra, and never inside a run this tool delegated. Standard library
only, and nothing here imports the provider registry, so a user's adapters
are never loaded by a hook. The caller turns any exception into silence.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from . import config as config_mod
from . import workflow
from .execution import DELEGATED_ENV

#: The command-line event name, and the ``hook_event_name`` it must arrive with.
EVENTS = {
    "prompt": "UserPromptSubmit",
    "session-start": "SessionStart",
    "stop": "Stop",
}

# --------------------------------------------------------------------------- languages

JAPANESE = "japanese"  # kana and ideographs together
HAN = "han"
HANGUL = "hangul"
CYRILLIC = "cyrillic"
GREEK = "greek"
ARABIC = "arabic"
HEBREW = "hebrew"
THAI = "thai"
DEVANAGARI = "devanagari"
LATIN = "latin"

#: Which letters count as written in each target script.
_TARGET_LETTERS = {
    JAPANESE: ("kana", HAN),
    HAN: (HAN,),
    HANGUL: (HANGUL,),
    CYRILLIC: (CYRILLIC,),
    GREEK: (GREEK,),
    ARABIC: (ARABIC,),
    HEBREW: (HEBREW,),
    THAI: (THAI,),
    DEVANAGARI: (DEVANAGARI,),
}

#: Scripts where one character carries about as much as three Latin letters.
_DENSE_SCRIPTS = frozenset((JAPANESE, HAN, HANGUL))
_DENSE_LETTERS = frozenset(("kana", HAN, HANGUL))


class Language(NamedTuple):
    name: str  # in English, for the instructions
    native: str  # the language's own name for itself; empty when it is the English one
    script: str


#: Primary subtag -> the language. A Latin-script entry in ``_FUNCTION_WORDS``
#: is also told from the others there by its common words; the rest are
#: listed for their names only, and judged only against a reply that is
#: mostly in another script.
_LANGUAGES = {
    "ja": Language("Japanese", "日本語", JAPANESE),
    "zh": Language("Chinese", "中文", HAN),
    "ko": Language("Korean", "한국어", HANGUL),
    "ru": Language("Russian", "русский", CYRILLIC),
    "uk": Language("Ukrainian", "українська", CYRILLIC),
    "bg": Language("Bulgarian", "български", CYRILLIC),
    "sr": Language("Serbian", "српски", CYRILLIC),
    "mk": Language("Macedonian", "македонски", CYRILLIC),
    "be": Language("Belarusian", "беларуская", CYRILLIC),
    "kk": Language("Kazakh", "қазақ тілі", CYRILLIC),
    "mn": Language("Mongolian", "монгол", CYRILLIC),
    "el": Language("Greek", "Ελληνικά", GREEK),
    "ar": Language("Arabic", "العربية", ARABIC),
    "fa": Language("Persian", "فارسی", ARABIC),
    "ur": Language("Urdu", "اردو", ARABIC),
    "he": Language("Hebrew", "עברית", HEBREW),
    "th": Language("Thai", "ไทย", THAI),
    "hi": Language("Hindi", "हिन्दी", DEVANAGARI),
    "mr": Language("Marathi", "मराठी", DEVANAGARI),
    "ne": Language("Nepali", "नेपाली", DEVANAGARI),
    "en": Language("English", "", LATIN),
    "fr": Language("French", "français", LATIN),
    "de": Language("German", "Deutsch", LATIN),
    "es": Language("Spanish", "español", LATIN),
    "it": Language("Italian", "italiano", LATIN),
    "pt": Language("Portuguese", "português", LATIN),
    "nl": Language("Dutch", "Nederlands", LATIN),
    "sv": Language("Swedish", "svenska", LATIN),
    "da": Language("Danish", "dansk", LATIN),
    "no": Language("Norwegian", "norsk", LATIN),
    "nb": Language("Norwegian Bokmål", "norsk bokmål", LATIN),
    "nn": Language("Norwegian Nynorsk", "norsk nynorsk", LATIN),
    "fi": Language("Finnish", "suomi", LATIN),
    "pl": Language("Polish", "polski", LATIN),
    "cs": Language("Czech", "čeština", LATIN),
    "sk": Language("Slovak", "slovenčina", LATIN),
    "hu": Language("Hungarian", "magyar", LATIN),
    "ro": Language("Romanian", "română", LATIN),
    "tr": Language("Turkish", "Türkçe", LATIN),
    "vi": Language("Vietnamese", "Tiếng Việt", LATIN),
    "id": Language("Indonesian", "Bahasa Indonesia", LATIN),
    "ms": Language("Malay", "Bahasa Melayu", LATIN),
    "ca": Language("Catalan", "català", LATIN),
    "hr": Language("Croatian", "hrvatski", LATIN),
    "sl": Language("Slovenian", "slovenščina", LATIN),
    "et": Language("Estonian", "eesti", LATIN),
    "lv": Language("Latvian", "latviešu", LATIN),
    "lt": Language("Lithuanian", "lietuvių", LATIN),
}

#: A script subtag overrides the language's usual script (``sr-Latn``).
_SCRIPT_SUBTAGS = {
    "latn": LATIN,
    "cyrl": CYRILLIC,
    "hans": HAN,
    "hant": HAN,
    "jpan": JAPANESE,
    "kore": HANGUL,
    "hang": HANGUL,
    "grek": GREEK,
    "arab": ARABIC,
    "hebr": HEBREW,
    "thai": THAI,
    "deva": DEVANAGARI,
}

_TRADITIONAL_CHINESE = Language("Traditional Chinese", "繁體中文", HAN)
_SIMPLIFIED_CHINESE = Language("Simplified Chinese", "简体中文", HAN)


def language_of(tag: str) -> Optional[Language]:
    """The language ``tag`` names, or None for a tag this module does not know.

    An unknown language with a known script subtag is known by its script
    alone, and named by its tag.
    """
    subtags = tag.lower().split("-")
    primary, rest = subtags[0], subtags[1:]
    script = next((_SCRIPT_SUBTAGS[part] for part in rest if part in _SCRIPT_SUBTAGS), None)
    known = _LANGUAGES.get(primary)
    if primary == "zh":
        if "hant" in rest or any(part in ("tw", "hk", "mo") for part in rest):
            known = _TRADITIONAL_CHINESE
        elif "hans" in rest or any(part in ("cn", "sg") for part in rest):
            known = _SIMPLIFIED_CHINESE
    if known is None:
        return Language(tag, "", script) if script else None
    if script and script != known.script:
        # The native name is written in the usual script; drop it rather than mislabel.
        return Language(known.name, "", script)
    return known


def language_name(tag: str) -> str:
    """How the instructions name the language: ``Japanese (日本語)``."""
    language = language_of(tag)
    if language is None or language.name == tag:
        return 'the language tagged "%s"' % tag
    if language.native:
        return "%s (%s)" % (language.name, language.native)
    return language.name


def _short_name(tag: str) -> str:
    language = language_of(tag)
    if language is None or language.name == tag:
        return language_name(tag)
    return language.name


def _word_list_of(tag: str) -> Optional[str]:
    """The ``_FUNCTION_WORDS`` key a Latin-script ``tag`` is judged by, if any."""
    language = language_of(tag)
    primary = tag.lower().split("-")[0]
    if language is None or language.script != LATIN or primary not in _FUNCTION_WORDS:
        return None
    return primary


def check_kind(tag: Optional[str]) -> str:
    """``script``, ``words``, ``latin`` or ``none``: what the Stop check can judge for ``tag``."""
    if not tag:
        return "none"
    language = language_of(tag)
    if language is None:
        return "none"
    if language.script != LATIN:
        return "script"
    return "words" if _word_list_of(tag) else "latin"


def word_languages() -> List[str]:
    """The Latin-script languages told apart by their common words, by name."""
    return [_LANGUAGES[key].name for key in _FUNCTION_WORDS]


def reminder(tag: str) -> str:
    """The reminder added before each prompt, and after a compaction or a resume."""
    return (
        "dev-orchestra: the user set language.reply: %s. Write every message to the user — progress, "
        "questions, approvals, findings, the final report and tool-call descriptions — in %s, however "
        "much text in other languages you have just read. Ids, paths, commands, code and quoted text "
        "stay as written. Agent prompts and .ai/ stay English." % (tag, language_name(tag))
    )


def rewrite_reason(tag: str) -> str:
    """Why the Stop hook blocked, which is also what it asks for."""
    short = _short_name(tag)
    return (
        "dev-orchestra language check: your last reply was not in %s, the reply language the user set "
        "(language.reply: %s). Write that same reply again, in full, in %s: the same content, every "
        "finding and risk, nothing dropped or softened. Ids, severities, paths, commands, code and "
        "quoted text stay as written — put code in backticks and quoted text in a > quote. Do not run "
        "tools or redo any work for this. If the reply already was in %s, end your turn without "
        "repeating it." % (language_name(tag), tag, short, short)
    )


# --------------------------------------------------------------------------- judgement

#: Fewer Latin words than this and a reply passes whatever else it holds: a
#: short answer made of identifiers is still an answer in the right language.
MIN_WORDS_TO_JUDGE = 20
#: Below this share of target-script letters, the reply as a whole fails.
MIN_SHARE = 0.30
#: One paragraph of at least this many Latin words...
PARAGRAPH_MIN_WORDS = 40
#: ...with less than this share of the target script fails the reply.
PARAGRAPH_MIN_SHARE = 0.10
#: Japanese only: this many ideographs with no kana at all is Chinese.
MIN_IDEOGRAPHS_WITHOUT_KANA = 50
#: Chinese only: at least this many kana, making up at least this share of
#: the kana and ideographs, is Japanese. Japanese prose runs well above half
#: kana; a Chinese reply holds none outside a quoted name.
MIN_KANA_FOR_JAPANESE = 20
MIN_KANA_SHARE_FOR_JAPANESE = 0.15
#: Weight of one Latin letter against one letter of a dense script.
DENSE_LATIN_WEIGHT = 1 / 3
#: Any target fails on at least this many letters of scripts other than its
#: own (Latin aside, for a non-Latin target), making up more than this
#: weighted share of them and its own.
MIN_FOREIGN_LETTERS = 60
MAX_FOREIGN_SHARE = 0.70
#: A Latin-script language in ``_FUNCTION_WORDS``: fewer common words of all
#: the listed languages together than this, and the reply passes.
MIN_FUNCTION_WORDS_TO_JUDGE = 20
#: Otherwise it fails when, against one other listed language, the words
#: only that one has number at least this many...
OTHER_LANGUAGE_MIN_HITS = 15
#: ...and at least this many times the words only the target has.
OTHER_LANGUAGE_RATIO = 2.0

#: Common words per Latin-script language, lower case with accents. Two
#: languages are compared on the words one has and the other does not, so a
#: word on both lists (``de``, ``en``, ``a``, ``no``...) never counts between them.
_FUNCTION_WORDS = {
    "en": """
        the and of to a in is that it i for on with as was were be are this these by not or have
        from at which but an they you we will can has if there their been would should when what
        all so do does no its our your into than after now only because still here
    """,
    "es": """
        de la que el en y a los del se las por un para con no una su al lo como más pero sus le ya
        o este sí porque esta entre cuando muy sin sobre también me hasta hay donde quien desde
        todo nos durante todos uno les ni contra otros ese eso ante ellos e esto mí antes algunos
        qué unos yo otro otras otra él tanto esa estos mucho quienes nada muchos cual poco ella
        estar estas algunas algo nosotros es son está están era si solo ahora después
    """,
    "fr": """
        le la les de des du un une et est en que qui dans pour pas sur au aux avec ce cette ces il
        elle ils sont ou mais par plus ne se son sa ses nous vous leur été être aussi comme tout
        tous très sans après avant encore déjà je on y à a était ont fait peut maintenant rien
        entre me ni si
    """,
    "de": """
        der die das und ist nicht ein eine einen einem einer zu den dem des mit sich auf für von
        im in es sie ich wir auch als an bei aus wird werden wurde sind hat haben oder aber noch
        nach wie nur so dass kein keine durch über um vor schon jetzt alle man diese dieser dieses
        wenn
    """,
    "pt": """
        o os a as do da dos das em no na nos nas um uma uns umas que e é de para com não por mais
        mas ao aos pelo pela se seu sua seus suas ele ela eles elas isso isto este esta esse essa
        foi são está estão tem têm também já ainda quando muito sem sobre depois agora como só há
        você nós ou nem então porque até pode nada todo todos entre antes desde algo estar estas
    """,
    "it": """
        il lo la i gli le di da del della dei delle dello nel nella al alla che è e non un una uno
        per con su sul sulla si ma come anche più questo questa sono ha ho hanno era stato ci ne
        se già ancora dopo prima tutti tutto molto quando perché poi ora solo senza tra fra o in
        poco
    """,
}

_WORD_SETS = {key: frozenset(value.split()) for key, value in _FUNCTION_WORDS.items()}
_ALL_WORDS = frozenset(word for words in _WORD_SETS.values() for word in words)

_RE_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[`~]*[ \t]*$|\Z)", re.M | re.S)
_RE_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_RE_QUOTE_LINE = re.compile(r"^[ \t]*>.*$", re.M)
_RE_INLINE_CODE = re.compile(r"(`+)[^\n]*?\1")
_RE_LINK_TARGET = re.compile(r"\]\([^)\n]*\)")
_RE_AUTOLINK = re.compile(r"<(?:https?|ftp|file|mailto):[^>\s]*>", re.I)
_RE_URL = re.compile(r"(?<![A-Za-z0-9])(?:(?:https?|ftp|file)://|www\.)[!-~]+", re.I)
# Each starts only where its leading run starts: tried at every position of a
# long run with no ``@`` or slash, they would take time quadratic in its length.
_RE_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_RE_PATH = re.compile(r"(?<![A-Za-z0-9_.~%+@:-])[A-Za-z0-9_.~%+@:-]*[/\\][A-Za-z0-9_.~%+@:/\\-]*")
_RE_COMMAND_LINE = re.compile(r"^[ \t]*(?:\$|PS[^>\n]*>)[ \t].*$", re.M)
_RE_FLAG = re.compile(r"(?<![A-Za-z0-9_-])--[A-Za-z0-9][A-Za-z0-9_-]*")
_RE_DOUBLE_QUOTED = re.compile(r'"[^"\n]*"')
_RE_TOKEN = re.compile(r"[A-Za-z0-9_.:=#@-]+")
_RE_MIXED_CASE = re.compile(r"(?<![A-Za-z])[A-Za-z]*[a-z][A-Z][A-Za-z]*")
_RE_ALL_CAPS = re.compile(r"(?<![A-Za-z])[A-Z]{2,}s?(?![A-Za-z])")
_RE_TABLE_SEPARATOR = re.compile(r"^[ \t]*\|?[ \t:|-]*-[ \t:|-]*$", re.M)
_RE_LATIN_WORD = re.compile(r"[A-Za-zÀ-ɏ]{2,}")
_RE_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_RE_WORD = re.compile(r"[^\W\d_]+")


def _drop_identifier(match: re.Match[str]) -> str:
    token = match.group(0)
    core = token.rstrip(".:")
    if re.search(r"[\d_=#@.:]", core):
        return " "
    return token


def strip_allowed(text: str) -> str:
    """``text`` without what rule 11 lets stay as written, in the order the plan gives."""
    text = _RE_FENCE.sub("", text)
    text = _RE_HTML_COMMENT.sub("", text)
    text = _RE_QUOTE_LINE.sub("", text)
    text = _RE_INLINE_CODE.sub(" ", text)
    text = _RE_LINK_TARGET.sub("] ", text)
    text = _RE_AUTOLINK.sub(" ", text)
    text = _RE_URL.sub(" ", text)
    text = _RE_EMAIL.sub(" ", text)
    text = _RE_PATH.sub(" ", text)
    text = _RE_COMMAND_LINE.sub("", text)
    text = _RE_FLAG.sub(" ", text)
    text = _RE_DOUBLE_QUOTED.sub(" ", text)
    text = _RE_TOKEN.sub(_drop_identifier, text)
    text = _RE_MIXED_CASE.sub(" ", text)
    text = _RE_ALL_CAPS.sub(" ", text)
    text = _RE_TABLE_SEPARATOR.sub("", text)
    return text


def _script_of(char: str) -> Optional[str]:
    """The script of one letter, or None for anything that is not a letter."""
    code = ord(char)
    if code < 0x80:
        return LATIN if char.isalpha() else None
    if 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF or 0xFF66 <= code <= 0xFF9D:
        return "kana"
    if (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FFFF
        or 0x3005 <= code <= 0x3007
    ):
        return HAN
    if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return HANGUL
    if 0x0400 <= code <= 0x052F:
        return CYRILLIC
    if 0x0370 <= code <= 0x03FF or 0x1F00 <= code <= 0x1FFF:
        return GREEK
    if 0x0590 <= code <= 0x05FF:
        return HEBREW
    if (
        0x0600 <= code <= 0x06FF
        or 0x0750 <= code <= 0x077F
        or 0x08A0 <= code <= 0x08FF
        or 0xFB50 <= code <= 0xFDFF
        or 0xFE70 <= code <= 0xFEFF
    ):
        return ARABIC
    if 0x0900 <= code <= 0x097F:
        return DEVANAGARI
    if 0x0E00 <= code <= 0x0E7F:
        return THAI
    if not char.isalpha():
        return None
    if code < 0x0250 or 0x1E00 <= code <= 0x1EFF:
        return LATIN
    return "other"


class Counts(NamedTuple):
    letters: Dict[str, int]
    words: int  # Latin words of two or more letters

    def of(self, scripts: Iterable[str]) -> int:
        return sum(self.letters.get(script, 0) for script in scripts)


def count(text: str) -> Counts:
    letters: Dict[str, int] = {}
    for char in text:
        script = _script_of(char)
        if script is not None:
            letters[script] = letters.get(script, 0) + 1
    return Counts(letters, len(_RE_LATIN_WORD.findall(text)))


def target_share(counts: Counts, script: str) -> Optional[float]:
    """The share of ``script``'s letters against them and Latin ones, Latin weighted
    down for a dense script; None with neither."""
    target = counts.of(_TARGET_LETTERS[script])
    weight = DENSE_LATIN_WEIGHT if script in _DENSE_SCRIPTS else 1.0
    total = target + weight * counts.letters.get(LATIN, 0)
    return target / total if total else None


def _mostly_foreign(counts: Counts, own: Iterable[str], ignored: Iterable[str] = ()) -> bool:
    """Whether letters of scripts other than ``own`` and ``ignored`` outweigh ``own`` clearly."""
    own_scripts = frozenset(own)
    skipped = own_scripts | frozenset(ignored)

    def weight(script: str) -> float:
        return 1 / DENSE_LATIN_WEIGHT if script in _DENSE_LETTERS else 1.0

    foreign = {script: value for script, value in counts.letters.items() if script not in skipped}
    if sum(foreign.values()) < MIN_FOREIGN_LETTERS:
        return False
    weighted = sum(value * weight(script) for script, value in foreign.items())
    native = sum(counts.letters.get(script, 0) * weight(script) for script in own_scripts)
    return weighted / (weighted + native) > MAX_FOREIGN_SHARE


def _fails_script_target(text: str, script: str) -> bool:
    whole = count(text)
    if script == JAPANESE and whole.letters.get(HAN, 0) >= MIN_IDEOGRAPHS_WITHOUT_KANA:
        if not whole.letters.get("kana"):
            return True
    if script == HAN:
        kana = whole.letters.get("kana", 0)
        cjk = whole.of(("kana", HAN))
        if kana >= MIN_KANA_FOR_JAPANESE and kana >= MIN_KANA_SHARE_FOR_JAPANESE * cjk:
            return True
    # Hangul in a Japanese or Chinese reply, kana or ideographs in a Korean one...
    if _mostly_foreign(whole, _TARGET_LETTERS[script], ignored=(LATIN,)):
        return True
    if whole.words < MIN_WORDS_TO_JUDGE:
        return False
    share = target_share(whole, script)
    if share is not None and share < MIN_SHARE:
        return True
    for paragraph in _RE_PARAGRAPH_BREAK.split(text):
        counts = count(paragraph)
        if counts.words < PARAGRAPH_MIN_WORDS:
            continue
        share = target_share(counts, script)
        if share is not None and share < PARAGRAPH_MIN_SHARE:
            return True
    return False


def function_words(text: str) -> Counter[str]:
    """The listed common words ``text`` holds, lower case, with how often each comes."""
    return Counter(word for word in _RE_WORD.findall(text.lower()) if word in _ALL_WORDS)


def word_hits(words: Counter[str], key: str, other: str) -> int:
    """How many of ``words`` are on ``key``'s list and not on ``other``'s."""
    own, theirs = _WORD_SETS[key], _WORD_SETS[other]
    return sum(value for word, value in words.items() if word in own and word not in theirs)


def _fails_word_target(text: str, key: str) -> bool:
    words = function_words(text)
    if sum(words.values()) < MIN_FUNCTION_WORDS_TO_JUDGE:
        return False
    for other in _WORD_SETS:
        if other == key:
            continue
        theirs, ours = word_hits(words, other, key), word_hits(words, key, other)
        if theirs >= OTHER_LANGUAGE_MIN_HITS and theirs >= OTHER_LANGUAGE_RATIO * ours:
            return True
    return False


def reply_fails(text: str, tag: str) -> bool:
    """Whether ``text`` is clearly not written in the language ``tag`` names.

    Leans towards passing: a wrong block costs the user a rewritten reply they
    did not need, a missed one only what rule 11 already risked.
    """
    language = language_of(tag)
    if language is None:
        return False
    stripped = strip_allowed(text)
    if language.script == LATIN:
        if _mostly_foreign(count(stripped), (LATIN,)):
            return True
        key = _word_list_of(tag)
        return key is not None and _fails_word_target(stripped, key)
    return _fails_script_target(stripped, language.script)


# --------------------------------------------------------------------------- transcript

#: How much of the transcript the scope check reads, from the end.
SCOPE_SCAN_BYTES = 64 * 1024 * 1024
#: The tail the last reply is looked for in, and how far that may grow.
REPLY_TAIL_BYTES = 256 * 1024
REPLY_TAIL_MAX_BYTES = 4 * 1024 * 1024

_MARKERS = (b"dev-orchestra", b"dev_orchestra")
#: A line holding a marker is parsed only if it also has a shape
#: ``_marks_session`` accepts: every entry carries ``cwd``, so in a checkout
#: whose path names the tool the bare name is on every line.
_RE_CANDIDATE_LINE = re.compile(
    rb'"Skill"|dev_orchestra\.py|bin(?:/|\\\\)dev-orchestra'
    rb'|(?:"|<command-name>)(?:\s|\\[nrt])*/dev-orchestra'
)
_RE_TYPED_COMMAND = re.compile(r"(?:\A\s*|<command-name>\s*)/dev-orchestra(?=$|[\s:<])")
_SHELL_TOOLS = ("Bash", "PowerShell")
_CLI = r"(?:dev_orchestra\.py|bin[/\\]dev-orchestra)"
_INTERPRETER = r"(?:python[\d.]*|py|sh|bash|zsh|pwsh|powershell)(?:\.exe)?"
#: The CLI as the command a shell runs: first in the line or after ``;``,
#: ``&``, ``|`` or ``(``, past any ``NAME=value`` and an interpreter with its
#: options. Merely naming the file (``cat``, ``git diff``, ``rg``) is not a run.
_RE_CLI_RUN = re.compile(
    (
        r"""(?:^|[;&|(])\s*(?:\w+=\S*\s+)*"""
        r"""(?:(?:"[^"\n]*?%(i)s"|'[^'\n]*?%(i)s'|[^\s"';&|()]*?%(i)s)\s+(?:-\S+\s+)*)?"""
        r"""(?:"[^"\n]*?%(c)s[^"\n]*"|'[^'\n]*?%(c)s[^'\n]*'|[^\s"';&|()]*?%(c)s)"""
    )
    % {"i": _INTERPRETER, "c": _CLI},
    re.M | re.I,
)


def _tail(path: str, size: int) -> Tuple[List[bytes], bool]:
    """The complete lines in the last ``size`` bytes, and whether that is the whole file."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        length = handle.tell()
        start = max(0, length - size)
        handle.seek(start)
        data = handle.read()
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]  # the first one starts mid-line
    return lines, start == 0


def _entries(lines: Iterable[bytes]) -> List[Dict[str, Any]]:
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _content(entry: Dict[str, Any]) -> Any:
    message = entry.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _walk_back(entries: List[Dict[str, Any]]) -> Tuple[Optional[str], bool]:
    """The text of the last reply, and whether its start was found.

    ``None`` when the last entry is not the assistant's: its reply is not
    written yet, and there is nothing to judge.
    """
    parts: List[str] = []
    seen = False
    for entry in reversed(entries):
        if entry.get("isSidechain"):
            continue
        kind = entry.get("type")
        if kind == "user":
            return ("\n\n".join(reversed(parts)) if seen else None), True
        if kind != "assistant":
            continue
        seen = True
        content = _content(entry)
        if isinstance(content, str):
            parts.append(content)
            continue
        for block in reversed(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                return "\n\n".join(reversed(parts)), True
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return ("\n\n".join(reversed(parts)) if seen else None), False


def last_reply(path: str) -> Optional[str]:
    """The text the assistant wrote after its last tool call, read from the transcript's tail."""
    size = REPLY_TAIL_BYTES
    while True:
        lines, whole = _tail(path, size)
        text, complete = _walk_back(_entries(lines))
        if complete or whole or size >= REPLY_TAIL_MAX_BYTES:
            return text
        size = min(size * 4, REPLY_TAIL_MAX_BYTES)


def _marks_session(entry: Dict[str, Any]) -> bool:
    """Whether one transcript entry shows dev-orchestra in use in the main session."""
    if entry.get("isSidechain"):
        return False
    content = _content(entry)
    kind = entry.get("type")
    if kind == "user":
        texts = [content] if isinstance(content, str) else []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
        return any(isinstance(text, str) and _RE_TYPED_COMMAND.search(text) for text in texts)
    if kind != "assistant" or not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        name = block.get("name")
        if name == "Skill":
            skill = tool_input.get("skill") or tool_input.get("command") or tool_input.get("name")
            if isinstance(skill, str) and skill.strip().lstrip("/").split(":")[-1] == "dev-orchestra":
                return True
        elif name in _SHELL_TOOLS:
            command = tool_input.get("command")
            if isinstance(command, str) and _RE_CLI_RUN.search(command):
                return True
    return False


def transcript_marks_session(path: str) -> bool:
    """Whether the transcript shows dev-orchestra in use; only candidate lines are parsed."""
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        start = max(0, handle.tell() - SCOPE_SCAN_BYTES)
        handle.seek(start)
        if start:
            handle.readline()  # the first one starts mid-line
        for line in handle:
            if not any(marker in line for marker in _MARKERS) or not _RE_CANDIDATE_LINE.search(line):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and _marks_session(entry):
                return True
    return False


# --------------------------------------------------------------------------- scope


def workflow_marker(cwd: str, session: str, data: Dict[str, Any]) -> str:
    """Where this session's workflow directory is, had it run a dev-orchestra command."""
    workspace = data.get("workspace")
    container = (workspace.get("dir") if isinstance(workspace, dict) else None) or ".ai"
    if not os.path.isabs(container):
        container = os.path.join(config_mod.repository_root(cwd) or os.path.abspath(cwd), container)
    return workflow.workflow_dir(container, workflow.from_session(session))


def in_scope(payload: Dict[str, Any], cwd: str, data: Dict[str, Any]) -> bool:
    """Whether this session used dev-orchestra: its workflow directory, then its transcript."""
    session = payload.get("session_id")
    if isinstance(session, str) and session and os.path.isdir(workflow_marker(cwd, session, data)):
        return True
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and _RE_TYPED_COMMAND.search(prompt):
        return True  # typed just now, not in the transcript yet
    transcript = payload.get("transcript_path")
    return isinstance(transcript, str) and bool(transcript) and transcript_marks_session(transcript)


def file_settings(cwd: str) -> Dict[str, Any]:
    """The global file with the project file over it, as read from disk.

    Not ``config.load``: that composes a preset, which imports the provider
    registry and with it the user's adapters.
    """
    data: Dict[str, Any] = {}
    for path in layer_paths(cwd):
        if path and os.path.isfile(path):
            data = config_mod.deep_merge(data, config_mod.read_config_file(path))
    return data


def layer_paths(cwd: str) -> List[Optional[str]]:
    """The global file, then the project file for ``cwd`` (None when there is none)."""
    return [config_mod.global_config_path(), config_mod.find_project_config(cwd)]


# --------------------------------------------------------------------------- hook


def handle(event: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The hook's JSON output for ``payload``, or None to print nothing."""
    if event == "stop" and payload.get("stop_hook_active"):
        return None  # this reply is already the rewrite: never block twice
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    data = file_settings(cwd)
    settings = config_mod.language_settings_of(data)
    tag = settings["reply"]
    if not tag or (event == "stop" and not settings["rewrite"]):
        return None
    if not in_scope(payload, cwd, data):
        return None
    if event != "stop":
        return {"hookSpecificOutput": {"hookEventName": EVENTS[event], "additionalContext": reminder(tag)}}
    text = payload.get("last_assistant_message")
    if not isinstance(text, str):
        transcript = payload.get("transcript_path")
        if not isinstance(transcript, str) or not os.path.isfile(transcript):
            return None
        text = last_reply(transcript)
    if not text or not reply_fails(text, tag):
        return None
    return {"decision": "block", "reason": rewrite_reason(tag)}


def run(event: str, raw: bytes, environ: Mapping[str, str]) -> str:
    """What the hook prints for ``raw`` stdin: JSON, or nothing."""
    if environ.get(DELEGATED_ENV):
        return ""
    expected = EVENTS.get(event)
    if expected is None:
        return ""
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("hook_event_name") != expected:
        return ""
    output = handle(event, payload)
    return json.dumps(output) if output else ""
