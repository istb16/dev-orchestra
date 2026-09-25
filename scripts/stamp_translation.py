"""Write the header of a Japanese reference translation.

    python scripts/stamp_translation.py docs/ja/references/cli.md [...]

The header names the English file the translation was made from and the
sha256 of that file as it is now. ``tests/test_docs.py`` fails once the
English file changes, so a translation cannot silently fall behind. Run this
only after the translation has been brought up to date with the English: the
stamp is the claim that it has.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
JA_DIR = REPO_ROOT / "docs" / "ja" / "references"

HEADER = re.compile(r"\A<!-- translated-from: (?P<source>\S+) sha256:(?P<digest>[0-9a-f]{64}) -->\n")
NOTE_LINES = 3


def source_of(translation: pathlib.Path) -> pathlib.Path:
    return REPO_ROOT / "references" / translation.name


def digest_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def header_for(translation: pathlib.Path) -> str:
    source = source_of(translation)
    relative = source.relative_to(REPO_ROOT).as_posix()
    return (
        "<!-- translated-from: %s sha256:%s -->\n"
        "\n"
        "> この文書は [%s](../../../%s) の日本語訳です。内容が食い違うときは英語版が正です。\n"
        "\n" % (relative, digest_of(source), relative, relative)
    )


def stamp(translation: pathlib.Path) -> None:
    text = translation.read_text(encoding="utf-8")
    if HEADER.match(text):
        # Drop the old header and the note after it, then write the new ones.
        text = "\n".join(text.split("\n")[1 + NOTE_LINES :])
    translation.write_text(header_for(translation) + text.lstrip("\n"), encoding="utf-8", newline="\n")


def main(argv: list) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    for name in argv:
        path = pathlib.Path(name).resolve()
        if path.parent != JA_DIR or not source_of(path).is_file():
            print("not a translation of a file in references/: %s" % name, file=sys.stderr)
            return 2
        stamp(path)
        print("stamped %s" % path.relative_to(REPO_ROOT).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
