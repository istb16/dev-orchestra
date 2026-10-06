"""The reply-language hooks: judgement, transcript reading, scope and failing open (#254).

Nothing here is fixed to Japanese: each language is the configured tag's, and
the cases cover dense scripts (ja, zh, ko), an alphabetic one (Cyrillic),
Latin-script targets told apart by common words (es, en, ...) or not (nl),
and a tag the hook does not know.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional
from unittest import mock

from helpers import REPO_ROOT, SCRIPTS_DIR, IsolatedCase

from orchestrator import cli, doctor, hosts, workflow
from orchestrator import config as config_mod
from orchestrator import reply_language as rl

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "reply_language")
HOOK_SCRIPT = os.path.join(SCRIPTS_DIR, "hooks", "reply_language.py")
SESSION = "5f0c3a52-0000-4000-8000-000000000254"

#: Run in a child under -I: which ``orchestrator`` modules a Stop hook imports.
IMPORT_PROBE = """\
import json, sys
sys.path.insert(0, %r)
from orchestrator import reply_language as rl
out = rl.run("stop", sys.stdin.buffer.read(), {})
names = sorted(name for name in sys.modules if name.startswith("orchestrator"))
print(json.dumps([bool(out), names]))
"""


def fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def assistant(*blocks: Dict[str, Any], sidechain: bool = False) -> Dict[str, Any]:
    message = {"role": "assistant", "content": list(blocks)}
    return {"type": "assistant", "isSidechain": sidechain, "message": message}


def user(content: Any, sidechain: bool = False) -> Dict[str, Any]:
    message = {"role": "user", "content": content}
    return {"type": "user", "isSidechain": sidechain, "message": message}


def text(value: str) -> Dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(name: str, **tool_input: Any) -> Dict[str, Any]:
    return {"type": "tool_use", "id": "t", "name": name, "input": tool_input}


SKILL_LOAD = assistant(tool_use("Skill", skill="dev-orchestra:dev-orchestra"))


def english_turn() -> List[Dict[str, Any]]:
    """A session that loaded the skill and then answered in English."""
    return [user("go"), SKILL_LOAD, assistant(text(fixture("en_reply.md")))]


def hook_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.pop(rl.DELEGATED_ENV, None)
    return env


def run_hook_process(event: str, stdin: bytes, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", HOOK_SCRIPT, event],
        input=stdin,
        capture_output=True,
        cwd=cwd,
        env=hook_env(),
        timeout=60,
    )


class TestJudgement(unittest.TestCase):
    """``reply_fails``: True means the reply is clearly in another language."""

    def test_japanese_reply_full_of_identifiers_passes(self):
        self.assertFalse(rl.reply_fails(fixture("ja_identifiers.md"), "ja"))

    def test_short_japanese_reply_passes(self):
        reply = "了解しました。`config set language.reply ja` を実行しました。"
        self.assertFalse(rl.reply_fails(reply, "ja"))
        self.assertFalse(rl.reply_fails("OK, F3 と F4 は直しました。", "ja"))

    def test_english_reply_fails_for_ja(self):
        self.assertTrue(rl.reply_fails(fixture("en_reply.md"), "ja"))

    def test_english_body_under_a_japanese_opening_line_fails(self):
        self.assertTrue(rl.reply_fails(fixture("ja_opening_en_body.md"), "ja"))

    def test_long_english_paragraph_in_a_japanese_reply_fails(self):
        reply = fixture("ja_long_en_paragraph.md")
        self.assertTrue(rl.reply_fails(reply, "ja"))
        # The overall share alone would pass it: the paragraph is what fails.
        counts = rl.count(rl.strip_allowed(reply))
        self.assertGreaterEqual(rl.target_share(counts, rl.JAPANESE) or 0, rl.MIN_SHARE)
        # The same paragraph as a quote is quoted text, which stays as written.
        quoted = reply.replace("The rejected finding", "> The rejected finding")
        self.assertFalse(rl.reply_fails(quoted, "ja"))

    def test_code_fences_inline_code_urls_paths_quotes_are_ignored(self):
        reply = fixture("ja_with_code.md")
        self.assertFalse(rl.reply_fails(reply, "ja"))
        stripped = rl.strip_allowed(reply)
        for gone in (
            "Everything else",
            "configuration reference",
            "github.com",
            "config.yaml",
            "--fast",
            "git status",
            "Reply language",
            "reviewers never see",
        ):
            self.assertNotIn(gone, stripped)
        self.assertIn("設定を変えました", stripped)

    def test_identifier_tokens_are_dropped_and_words_kept(self):
        stripped = rl.strip_allowed("F3 snake_case file.py a=b #12 user@host GitHub README PRs Note: done.")
        for gone in ("F3", "snake_case", "file.py", "a=b", "#12", "user@host", "GitHub", "README", "PRs"):
            self.assertNotIn(gone, stripped)
        self.assertIn("Note:", stripped)
        self.assertIn("done.", stripped)

    def test_final_report_table_in_a_fence_passes(self):
        self.assertFalse(rl.reply_fails(fixture("ja_report_table.md"), "ja"))

    def test_chinese_reply_fails_for_ja(self):
        self.assertTrue(rl.reply_fails(fixture("zh_reply.md"), "ja"))
        self.assertTrue(rl.reply_fails(fixture("zh_tw_reply.md"), "ja"))
        self.assertFalse(rl.reply_fails(fixture("zh_reply.md"), "zh"))
        self.assertFalse(rl.reply_fails(fixture("zh_reply.md"), "zh-TW"))
        self.assertTrue(rl.reply_fails(fixture("en_reply.md"), "zh-Hans"))

    def test_chinese_target_passes_both_scripts(self):
        for tag in ("zh", "zh-CN", "zh-TW", "zh-Hans", "zh-Hant", "zh-HK"):
            self.assertFalse(rl.reply_fails(fixture("zh_reply.md"), tag), tag)
            self.assertFalse(rl.reply_fails(fixture("zh_tw_reply.md"), tag), tag)

    def test_japanese_reply_fails_for_zh(self):
        for name in ("ja_identifiers.md", "ja_with_code.md"):
            for tag in ("zh", "zh-TW", "zh-Hans"):
                self.assertTrue(rl.reply_fails(fixture(name), tag), (name, tag))
        # Too few kana to judge: a one-line Japanese report passes.
        self.assertFalse(rl.reply_fails(fixture("ja_report_table.md"), "zh"))
        counts = rl.count(rl.strip_allowed(fixture("ja_identifiers.md")))
        self.assertGreaterEqual(counts.letters["kana"], rl.MIN_KANA_FOR_JAPANESE)
        # Kana never count as Chinese: a reply in kana alone is not one.
        self.assertTrue(rl.reply_fails("これはテストです。" * 10, "zh"))

    def test_a_few_kana_in_a_chinese_reply_pass(self):
        reply = fixture("zh_reply.md") + "\n用户提到的工具叫作「ドクター」。"
        self.assertFalse(rl.reply_fails(reply, "zh"))
        # Many kana, but a small share of a long Chinese reply, pass too.
        reply = (fixture("zh_reply.md") * 6) + "\n" + "ドクター、" * 5
        counts = rl.count(rl.strip_allowed(reply))
        self.assertGreaterEqual(counts.letters["kana"], rl.MIN_KANA_FOR_JAPANESE)
        self.assertFalse(rl.reply_fails(reply, "zh"))

    def test_korean_target_passes_hangul_fails_english(self):
        self.assertFalse(rl.reply_fails(fixture("ko_reply.md"), "ko"))
        self.assertTrue(rl.reply_fails(fixture("en_reply.md"), "ko"))
        self.assertTrue(rl.reply_fails(fixture("ja_opening_en_body.md"), "ko"))

    def test_hangul_kana_and_ideographs_never_count_for_each_other(self):
        for tag in ("ja", "zh", "zh-TW"):
            self.assertTrue(rl.reply_fails(fixture("ko_reply.md"), tag), tag)
        for name in ("ja_identifiers.md", "zh_reply.md", "zh_tw_reply.md"):
            self.assertTrue(rl.reply_fails(fixture(name), "ko"), name)

    def test_cyrillic_target(self):
        for tag in ("ru", "uk", "sr"):
            self.assertFalse(rl.reply_fails(fixture("ru_reply.md"), tag), tag)
            self.assertTrue(rl.reply_fails(fixture("en_reply.md"), tag), tag)
        # A script subtag moves the language to another script.
        self.assertEqual(rl.check_kind("sr-Latn"), "latin")
        self.assertFalse(rl.reply_fails(fixture("en_reply.md"), "sr-Latn"))

    def test_latin_target_flags_replies_in_another_script(self):
        for tag in ("en", "fr", "en-GB", "nl", "sv"):
            for other in ("ja_identifiers.md", "ko_reply.md", "ru_reply.md", "zh_reply.md"):
                self.assertTrue(rl.reply_fails(fixture(other), tag), (tag, other))
        # A few words of another script are not a reply in it.
        self.assertFalse(rl.reply_fails(fixture("en_reply.md") + "\n日本語の補足。", "en"))

    def test_listed_latin_languages_are_told_apart_by_common_words(self):
        replies = {
            "en": "en_reply.md",
            "es": "es_reply.md",
            "fr": "fr_reply.md",
            "de": "de_reply.md",
            "pt": "pt_reply.md",
            "it": "it_reply.md",
        }
        self.assertEqual(
            sorted(rl.word_languages()), ["English", "French", "German", "Italian", "Portuguese", "Spanish"]
        )
        for tag in replies:
            self.assertEqual(rl.check_kind(tag), "words", tag)
            for language, name in replies.items():
                self.assertEqual(rl.reply_fails(fixture(name), tag), language != tag, (tag, name))

    def test_spanish_target(self):
        for tag in ("es", "es-ES", "es-419", "es-MX"):
            self.assertFalse(rl.reply_fails(fixture("es_reply.md"), tag), tag)
            self.assertFalse(rl.reply_fails(fixture("es_identifiers.md"), tag), tag)
            self.assertTrue(rl.reply_fails(fixture("en_reply.md"), tag), tag)
            self.assertTrue(rl.reply_fails(fixture("ja_identifiers.md"), tag), tag)
        for tag in ("en", "en-US"):
            self.assertTrue(rl.reply_fails(fixture("es_reply.md"), tag), tag)

    def test_short_latin_replies_pass(self):
        self.assertFalse(rl.reply_fails("Done. All tests pass and the branch is ready.", "es"))
        self.assertFalse(rl.reply_fails("Listo. Todas las pruebas pasan y la rama está lista.", "en"))

    def test_shared_words_count_for_neither_language(self):
        words = rl.function_words("de la no a e o en el the")
        self.assertEqual(rl.word_hits(words, "es", "en"), 6)  # de la e o en el
        self.assertEqual(rl.word_hits(words, "en", "es"), 1)  # the
        self.assertEqual(rl.word_hits(words, "es", "pt"), 3)  # la en el

    def test_identifiers_in_a_spanish_reply_are_not_english(self):
        stripped = rl.strip_allowed(fixture("es_identifiers.md"))
        words = rl.function_words(stripped)
        self.assertEqual(rl.word_hits(words, "en", "es"), 0)

    def test_unlisted_latin_target_keeps_the_script_check_only(self):
        for tag in ("nl", "sv", "pl", "sr-Latn"):
            self.assertEqual(rl.check_kind(tag), "latin", tag)
            for name in ("en_reply.md", "es_reply.md", "fr_reply.md"):
                self.assertFalse(rl.reply_fails(fixture(name), tag), (tag, name))

    def test_unknown_tag_never_fails(self):
        for tag in ("tlh", "qaa", "xyz-Latx"):
            self.assertEqual(rl.check_kind(tag), "none", tag)
            for name in ("en_reply.md", "ja_identifiers.md", "ko_reply.md", "ru_reply.md"):
                self.assertFalse(rl.reply_fails(fixture(name), tag), (tag, name))

    def test_every_alphabetic_script_is_judged_by_its_own_letters(self):
        """A reply in each script passes for its languages and fails for the others and English."""
        replies = {
            "el": "Οι αλλαγές ολοκληρώθηκαν και όλες οι δοκιμές περνούν. Ο κλάδος είναι έτοιμος για έλεγχο "
            "και δεν απομένουν ανοιχτά ζητήματα.",
            "ar": "اكتملت التغييرات ونجحت جميع الاختبارات. الفرع جاهز للمراجعة ولا توجد مشكلات مفتوحة "
            "متبقية في هذه المرحلة.",
            "fa": "تغییرات کامل شد و همه آزمون‌ها با موفقیت اجرا شدند. شاخه برای بازبینی آماده است و هیچ "
            "مشکل بازی باقی نمانده است.",
            "he": "השינויים הושלמו וכל הבדיקות עוברות. הענף מוכן לסקירה ולא נותרו בעיות פתוחות בשלב הזה "
            "של העבודה.",
            "th": "การเปลี่ยนแปลงเสร็จสมบูรณ์แล้วและการทดสอบทั้งหมดผ่าน สาขานี้พร้อมสำหรับการตรวจสอบและไม่มีปัญหาที่ค้างอยู่",
            "hi": "बदलाव पूरे हो गए हैं और सभी परीक्षण सफल रहे। शाखा समीक्षा के लिए तैयार है और कोई खुली "
            "समस्या बाकी नहीं है।",
            "mr": "बदल पूर्ण झाले आहेत आणि सर्व चाचण्या यशस्वी झाल्या. शाखा पुनरावलोकनासाठी तयार आहे आणि "
            "कोणतीही समस्या उरलेली नाही.",
            "ne": "परिवर्तनहरू पूरा भए र सबै परीक्षणहरू सफल भए। शाखा समीक्षाका लागि तयार छ र कुनै समस्या बाँकी छैन।",
        }
        english = fixture("en_reply.md")
        for tag, native in replies.items():
            self.assertEqual(rl.check_kind(tag), "script", tag)
            self.assertFalse(rl.reply_fails(native, tag), tag)
            self.assertFalse(rl.reply_fails(native + " F3, `tests/x.py`, README.", tag), tag)
            self.assertTrue(rl.reply_fails(english, tag), tag)
            script = rl.language_of(tag)
            for other, reply in replies.items():
                other_script = rl.language_of(other)
                if script and other_script and other_script.script != script.script:
                    self.assertTrue(rl.reply_fails(reply, tag), (tag, other))

    def test_a_script_subtag_overrides_a_chinese_region(self):
        self.assertEqual(rl.check_kind("zh-Latn-TW"), "latin")
        self.assertEqual(rl.check_kind("zh-Latn-CN"), "latin")
        self.assertEqual(rl.language_name("zh-Latn-TW"), "Traditional Chinese")
        self.assertFalse(rl.reply_fails(fixture("en_reply.md"), "zh-Latn-TW"))
        self.assertTrue(rl.reply_fails(fixture("en_reply.md"), "zh-TW"))

    def test_long_runs_without_an_at_sign_or_slash_are_stripped_in_linear_time(self):
        for run in ("a" * 200_000, "deadbeef" * 25_000, "x.y-z" * 40_000):
            started = time.monotonic()
            rl.strip_allowed(run)
            self.assertLess(time.monotonic() - started, 5.0, run[:10])
        stripped = rl.strip_allowed("mail me at dev@example.com or see a/b/c and ~/x for it")
        for gone in ("dev@example.com", "a/b/c", "~/x"):
            self.assertNotIn(gone, stripped)

    def test_check_kind_follows_the_tag(self):
        for tag in ("ja", "zh-TW", "ko", "ru", "el", "ar", "he", "th", "hi"):
            self.assertEqual(rl.check_kind(tag), "script", tag)
        for tag in ("en", "es-419", "fr", "de-CH", "pt-BR", "it"):
            self.assertEqual(rl.check_kind(tag), "words", tag)
        for tag in ("nl", "sv", "sr-Latn"):
            self.assertEqual(rl.check_kind(tag), "latin", tag)
        for tag in ("tlh", "", None):
            self.assertEqual(rl.check_kind(tag), "none", tag)


class TestMessages(unittest.TestCase):
    """Every word naming the language comes from the configured tag."""

    def test_language_names_come_from_the_tag(self):
        self.assertEqual(rl.language_name("ja"), "Japanese (日本語)")
        self.assertEqual(rl.language_name("KO"), "Korean (한국어)")
        self.assertEqual(rl.language_name("ru"), "Russian (русский)")
        self.assertEqual(rl.language_name("zh-TW"), "Traditional Chinese (繁體中文)")
        self.assertEqual(rl.language_name("en"), "English")
        self.assertEqual(rl.language_name("tlh"), 'the language tagged "tlh"')

    def test_reminder_names_the_configured_language(self):
        for tag, name, others in (
            ("ja", "Japanese (日本語)", ("Korean", "한국어")),
            ("ko", "Korean (한국어)", ("Japanese", "日本語")),
            ("fr", "French (français)", ("Japanese", "Korean")),
            ("es", "Spanish (español)", ("Japanese", "Korean")),
        ):
            reminder = rl.reminder(tag)
            self.assertIn("language.reply: %s." % tag, reminder)
            self.assertIn("in %s," % name, reminder)
            for other in others:
                self.assertNotIn(other, reminder)

    def test_reason_names_language_and_code(self):
        for tag, name, short, others in (
            ("ja", "Japanese (日本語)", "Japanese", ("Korean", "한국어")),
            ("ko", "Korean (한국어)", "Korean", ("Japanese", "日本語")),
            ("es", "Spanish (español)", "Spanish", ("Japanese", "English")),
            ("zh-TW", "Traditional Chinese (繁體中文)", "Traditional Chinese", ("Japanese", "日本語")),
        ):
            reason = rl.rewrite_reason(tag)
            self.assertIn("not in %s," % name, reason)
            self.assertIn("(language.reply: %s)" % tag, reason)
            self.assertIn("in full, in %s:" % short, reason)
            self.assertIn("already was in %s," % short, reason)
            self.assertIn("Do not run tools", reason)
            for other in others:
                self.assertNotIn(other, reason)

    def test_an_unknown_tag_is_named_by_the_tag(self):
        self.assertIn('in the language tagged "tlh"', rl.reminder("tlh"))


class TestTranscript(IsolatedCase):
    def write_transcript(self, entries: List[Dict[str, Any]], name: str = "t.jsonl") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    def test_last_message_joins_text_blocks_after_last_tool_use(self):
        path = os.path.join(FIXTURES, "last_reply.jsonl")
        self.assertEqual(rl.last_reply(path), "First part of the reply.\n\nSecond part of the reply.")
        reply = assistant(text("before"), tool_use("Bash", command="ls"), text("after"))
        self.assertEqual(rl.last_reply(self.write_transcript([user("go"), reply])), "after")

    def test_partial_first_line_in_tail_is_skipped(self):
        reply = assistant(text("done"))
        path = self.write_transcript([user("x" * 2000), reply])
        tail = len(json.dumps(reply)) + 40
        with mock.patch.object(rl, "REPLY_TAIL_BYTES", tail):
            with mock.patch.object(rl, "REPLY_TAIL_MAX_BYTES", 8192):
                self.assertEqual(rl.last_reply(path), "done")

    def test_last_entry_not_assistant_passes(self):
        path = self.write_transcript([assistant(text("An English reply.")), user("next question")])
        self.assertIsNone(rl.last_reply(path))

    def test_sidechain_entries_ignored(self):
        subagent = assistant(text("English"), sidechain=True)
        path = self.write_transcript([user("go"), assistant(text("本当の返答です。")), subagent])
        self.assertEqual(rl.last_reply(path), "本当の返答です。")
        skill = assistant(tool_use("Skill", skill="dev-orchestra"), sidechain=True)
        self.assertFalse(rl.transcript_marks_session(self.write_transcript([user("go"), skill])))

    def test_a_missing_transcript_marks_nothing(self):
        self.assertFalse(rl.transcript_marks_session(os.path.join(self.tmp, "absent.jsonl")))

    def test_a_reply_that_ends_in_a_tool_call_has_no_text(self):
        reply = assistant(text("checking"), tool_use("Bash", command="ls"))
        self.assertEqual(rl.last_reply(self.write_transcript([user("go"), reply])), "")

    def test_plain_string_content_is_the_reply(self):
        entry = {"type": "assistant", "message": {"role": "assistant", "content": "A plain reply."}}
        self.assertEqual(rl.last_reply(self.write_transcript([user("go"), entry])), "A plain reply.")

    def test_the_scope_scan_reads_only_the_last_window(self):
        filler = assistant(text("x" * 500))
        path = self.write_transcript([user("go"), SKILL_LOAD] + [filler] * 20)
        self.assertTrue(rl.transcript_marks_session(path))
        with mock.patch.object(rl, "SCOPE_SCAN_BYTES", len(json.dumps(filler)) * 5):
            self.assertFalse(rl.transcript_marks_session(path))

    def test_lines_that_only_carry_the_name_in_cwd_are_not_parsed(self):
        cwd = "/" + "/".join(("home", "me", "src", "dev-orchestra"))
        entries = [dict(user("hello"), cwd=cwd), dict(assistant(text("dev-orchestra is a plugin.")), cwd=cwd)]
        path = self.write_transcript(entries * 50)
        with mock.patch.object(rl.json, "loads", wraps=json.loads) as loads:
            self.assertFalse(rl.transcript_marks_session(path))
        self.assertEqual(loads.call_count, 0)
        path = self.write_transcript(entries * 50 + [dict(SKILL_LOAD, cwd=cwd)])
        self.assertTrue(rl.transcript_marks_session(path))

    def test_typed_command_after_leading_whitespace_marks_the_session(self):
        for content in ("  /dev-orchestra go", "\n/dev-orchestra:dev-orchestra go", [text("/dev-orchestra")]):
            self.assertTrue(rl.transcript_marks_session(self.write_transcript([user(content)])), content)


class HookCase(IsolatedCase):
    """A project with a config file and a transcript, and the hook run in-process."""

    def setUp(self) -> None:
        super().setUp()
        os.makedirs(os.path.join(self.project, ".git"))
        self.transcript = os.path.join(self.tmp, "session.jsonl")
        self.set_transcript([])

    def configure(self, reply: Any = "ja", rewrite: Any = None, scope: str = "project") -> None:
        language: Dict[str, Any] = {"reply": reply}
        if rewrite is not None:
            language["rewrite"] = rewrite
        if scope == "global":
            path = config_mod.global_config_path()
        else:
            path = os.path.join(self.project, ".dev-orchestra.yaml")
        config_mod.write_config_file(path, {"version": 1, "language": language}, scope)

    def set_transcript(self, entries: List[Dict[str, Any]]) -> None:
        with open(self.transcript, "w", encoding="utf-8", newline="\n") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def payload(self, event: str, **extra: Any) -> Dict[str, Any]:
        payload = {
            "hook_event_name": rl.EVENTS[event],
            "session_id": SESSION,
            "transcript_path": self.transcript,
            "cwd": self.project,
        }
        payload.update(extra)
        return payload

    def hook(self, event: str, environ: Optional[Dict[str, str]] = None, **extra: Any) -> Any:
        raw = json.dumps(self.payload(event, **extra)).encode("utf-8")
        output = rl.run(event, raw, environ or {})
        return json.loads(output) if output else None

    def workflow_dir(self) -> str:
        """Where a dev-orchestra command run in this session puts its workflow."""
        return workflow.workflow_dir(os.path.join(self.project, ".ai"), workflow.from_session(SESSION))


class TestScope(HookCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure("ja")

    def test_skill_load_marks_session(self):
        self.set_transcript([user("doctor を見て"), SKILL_LOAD])
        self.assertIsNotNone(self.hook("prompt", prompt="続けて"))

    def test_dev_orchestra_command_marks_session(self):
        script = tool_use("Bash", command='python "/p/scripts/dev_orchestra.py" doctor')
        wrapper = tool_use("PowerShell", command="& C:\\p\\bin\\dev-orchestra.ps1 status")
        for entries in (
            [user("<command-name>/dev-orchestra:dev-orchestra</command-name>")],
            [user("/dev-orchestra implement #254")],
            [user("go"), assistant(script)],
            [user("go"), assistant(wrapper)],
        ):
            self.set_transcript(entries)
            self.assertIsNotNone(self.hook("prompt", prompt="next"), entries)
        # Typed in this very prompt, before the transcript has it.
        self.set_transcript([])
        self.assertIsNotNone(self.hook("prompt", prompt="/dev-orchestra:dev-orchestra fix the bug"))

    def test_running_the_cli_marks_the_session_however_it_is_started(self):
        for command in (
            "cd /r && python3 -I scripts/dev_orchestra.py status",
            '& "C:\\Program Files\\p\\bin\\dev-orchestra.ps1" doctor',
            "DEV_ORCHESTRA_WORKFLOW=x bin/dev-orchestra doctor",
            'py -3 "C:\\p\\scripts\\dev_orchestra.py" doctor',
            "git status; /usr/bin/python3.12 /p/scripts/dev_orchestra.py review run",
            "/p/.venv/bin/python /p/scripts/dev_orchestra.py doctor 2>&1 | tail -5",
        ):
            self.set_transcript([user("go"), assistant(tool_use("Bash", command=command))])
            self.assertIsNotNone(self.hook("prompt", prompt="next"), command)

    def test_naming_the_cli_without_running_it_is_not_a_marker(self):
        for command in (
            "cat bin/dev-orchestra",
            "git diff scripts/dev_orchestra.py",
            "rg dev_orchestra.py",
            "grep -n main scripts/dev_orchestra.py",
            "ruff check scripts/dev_orchestra.py && git add bin/dev-orchestra",
            'echo "see bin/dev-orchestra"',
        ):
            self.set_transcript([user("go"), assistant(tool_use("Bash", command=command))])
            self.assertIsNone(self.hook("prompt", prompt="next"), command)

    def test_only_the_dev_orchestra_skill_marks_the_session(self):
        for skill, marks in (
            ("dev-orchestra", True),
            ("/dev-orchestra:dev-orchestra", True),
            ("x-dev-orchestra", False),
            ("my-plugin:not-dev-orchestra", False),
        ):
            self.set_transcript([user("go"), assistant(tool_use("Skill", skill=skill))])
            self.assertEqual(self.hook("prompt", prompt="next") is not None, marks, skill)

    def test_workflow_dir_for_session_hash_marks_session(self):
        os.makedirs(self.workflow_dir())
        self.assertIsNotNone(self.hook("prompt", prompt="next"))
        self.assertIsNone(self.hook("prompt", prompt="next", session_id="another-session"))

    def test_workflow_dir_follows_workspace_dir(self):
        config_mod.write_config_file(
            os.path.join(self.project, ".dev-orchestra.yaml"),
            {"version": 1, "language": {"reply": "ja"}, "workspace": {"dir": "work"}},
            "project",
        )
        os.makedirs(self.workflow_dir())  # under .ai/, which is no longer the container
        self.assertIsNone(self.hook("prompt", prompt="next"))
        digest = os.path.basename(self.workflow_dir())
        os.makedirs(os.path.join(self.project, "work", "workflows", digest))
        self.assertIsNotNone(self.hook("prompt", prompt="next"))

    def test_session_without_any_marker_is_out_of_scope(self):
        question = user("dev-orchestra って何?")
        self.set_transcript([question, assistant(text("dev-orchestra is a plugin."))])
        self.assertIsNone(self.hook("prompt", prompt="ありがとう"))
        self.assertIsNone(self.hook("stop"))

    def test_ai_dir_alone_is_not_scope(self):
        os.makedirs(os.path.join(self.project, ".ai", "workflows", "0123456789ab"))
        self.assertIsNone(self.hook("prompt", prompt="next"))

    def test_delegated_env_exits_silently(self):
        self.set_transcript(english_turn())
        self.assertIsNotNone(self.hook("stop"))
        for event in rl.EVENTS:
            self.assertIsNone(self.hook(event, environ={rl.DELEGATED_ENV: "1"}, prompt="x"), event)


class TestHookOutput(HookCase):
    def test_block_json_shape(self):
        self.configure("ja")
        self.set_transcript(english_turn())
        self.assertEqual(self.hook("stop"), {"decision": "block", "reason": rl.rewrite_reason("ja")})

    def test_a_reply_in_the_configured_language_is_not_blocked(self):
        for tag, name in (("ja", "ja_identifiers.md"), ("ko", "ko_reply.md"), ("ru", "ru_reply.md")):
            self.configure(tag)
            self.set_transcript([user("go"), SKILL_LOAD, assistant(text(fixture(name)))])
            self.assertIsNone(self.hook("stop"), tag)

    def test_additional_context_shape_per_event(self):
        self.configure("ko", scope="global")
        self.set_transcript([user("go"), SKILL_LOAD])
        for event, name in (("prompt", "UserPromptSubmit"), ("session-start", "SessionStart")):
            expected = {"hookEventName": name, "additionalContext": rl.reminder("ko")}
            output = self.hook(event, prompt="next", source="compact")
            self.assertEqual(output, {"hookSpecificOutput": expected})

    def test_the_event_must_match_the_command_line(self):
        self.configure("ja")
        self.set_transcript([user("go"), SKILL_LOAD])
        raw = json.dumps(self.payload("stop")).encode("utf-8")
        self.assertEqual(rl.run("prompt", raw, {}), "")
        self.assertEqual(rl.run("unknown", raw, {}), "")

    def test_the_project_file_wins_over_the_global_one(self):
        self.configure("ko", scope="global")
        self.configure("ja", scope="project")
        self.set_transcript([user("go"), SKILL_LOAD])
        output = self.hook("prompt", prompt="x")
        self.assertIn("Japanese (日本語)", output["hookSpecificOutput"]["additionalContext"])

    def test_a_project_null_silences_a_global_reply_language(self):
        self.configure("ja", scope="global")
        self.set_transcript(english_turn())
        self.assertIsNotNone(self.hook("stop"))
        self.configure(None, scope="project")
        for event in rl.EVENTS:
            self.assertIsNone(self.hook(event, prompt="x"), event)

    def test_last_assistant_message_field_preferred(self):
        self.configure("ja")
        japanese, english = fixture("ja_identifiers.md"), fixture("en_reply.md")
        self.set_transcript([user("go"), SKILL_LOAD, assistant(text(japanese))])
        self.assertIsNotNone(self.hook("stop", last_assistant_message=english))
        self.set_transcript(english_turn())
        self.assertIsNone(self.hook("stop", last_assistant_message=japanese))


class TestFailingOpen(HookCase):
    def test_stop_hook_active_never_blocks(self):
        self.configure("ja")
        self.set_transcript(english_turn())
        self.assertIsNone(self.hook("stop", stop_hook_active=True))

    def test_bad_json_stdin_silent_exit_0(self):
        self.configure("ja")
        for stdin in (b"{not json", b"[1, 2]", b"", b"\xff"):
            result = run_hook_process("stop", stdin)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), stdin)

    def test_missing_transcript_silent(self):
        self.configure("ja")
        os.makedirs(self.workflow_dir())
        self.assertIsNone(self.hook("stop", transcript_path=os.path.join(self.tmp, "absent.jsonl")))
        self.assertIsNone(self.hook("stop", transcript_path=None))

    def test_bad_config_file_silent(self):
        """The same run blocks with a valid file; the bad file is the only difference."""
        self.set_transcript(english_turn())
        self.configure("ja")
        payload = json.dumps(self.payload("stop")).encode("utf-8")
        result = run_hook_process("stop", payload, cwd=self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")
        self.write(".dev-orchestra.yaml", "- a list\n- is not a configuration\n")
        result = run_hook_process("stop", payload, cwd=self.project)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_an_invalid_setting_is_unset(self):
        self.set_transcript([user("go"), SKILL_LOAD])
        for value in ("japanese", "日本語", 1, True):
            self.configure(value)
            self.assertIsNone(self.hook("prompt", prompt="x"), value)

    def test_unset_language_prints_nothing_for_every_event(self):
        self.set_transcript(english_turn())
        os.makedirs(self.workflow_dir())
        for event in rl.EVENTS:
            self.assertIsNone(self.hook(event, prompt="x"), event)
        self.configure(None)
        for event in rl.EVENTS:
            self.assertIsNone(self.hook(event, prompt="x"), event)

    def test_rewrite_false_keeps_reminder_drops_stop(self):
        self.configure("ja", rewrite=False)
        self.set_transcript(english_turn())
        self.assertIsNotNone(self.hook("prompt", prompt="x"))
        self.assertIsNotNone(self.hook("session-start", source="resume"))
        self.assertIsNone(self.hook("stop"))

    def test_hook_does_not_import_providers(self):
        self.configure("ja")
        self.set_transcript(english_turn())
        result = subprocess.run(
            [sys.executable, "-I", "-c", IMPORT_PROBE % SCRIPTS_DIR],
            input=json.dumps(self.payload("stop")).encode("utf-8"),
            capture_output=True,
            env=hook_env(),
            check=True,
        )
        blocked, modules = json.loads(result.stdout)
        self.assertTrue(blocked)
        self.assertEqual([name for name in modules if name.startswith("orchestrator.providers")], [])

    def test_end_to_end_subprocess_within_the_hook_timeout(self):
        """A 20 MB transcript with the only marker near its end: the whole scan, then the tail.

        Timed against the hooks' own timeout, with room to spare, rather than
        a tight limit a cold start or a slow runner could break.
        """
        self.configure("ja")
        filler = json.dumps(assistant(text("x" * 1000))) + "\n"
        with open(self.transcript, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(user("go")) + "\n")
            handle.write(filler * (20 * 1024 * 1024 // len(filler)))
            for entry in [user("again"), *english_turn()]:
                handle.write(json.dumps(entry) + "\n")
        self.assertGreater(os.path.getsize(self.transcript), 20 * 1024 * 1024)
        payload = json.dumps(self.payload("stop")).encode("utf-8")
        started = time.monotonic()
        result = run_hook_process("stop", payload, cwd=self.project)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")
        with open(os.path.join(REPO_ROOT, "hooks", "claude-code.json"), encoding="utf-8") as handle:
            timeout = json.load(handle)["hooks"]["Stop"][0]["hooks"][0]["timeout"]
        self.assertLess(elapsed, timeout / 2)


class TestDoctorLanguage(IsolatedCase):
    def set_language(self, path: str, value: str) -> None:
        code, _, err = run_cli("config", "set", path, value, "--scope", "global")
        self.assertEqual(code, 0, err)

    def test_doctor_reports_language_and_hosts(self):
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("Reply language: not set", out)
        self.set_language("language.reply", "ko")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn(
            "Reply language: ko (global) -- Claude Code: Stop-hook rewrite + reminder; "
            "Codex, Antigravity: rule 11 only",
            out,
        )
        self.set_language("language.rewrite", "false")
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("Reply language: ko (global) -- Claude Code: reminder only;", out)

    def test_doctor_json_language_block(self):
        self.set_language("language.reply", "fr")
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        hosts = {
            "claude": {"status": "hook-shipped"},
            "codex": {"status": "not-enforced"},
            "agy": {"status": "not-enforced"},
        }
        expected = {"reply": "fr", "rewrite": True, "layer": "global", "check": "words", "hosts": hosts}
        self.assertEqual(report["language"], expected)
        notes = " ".join(report["notes"])
        self.assertIn("tells English, Spanish, French, German, Portuguese, Italian apart", notes)
        self.set_language("language.reply", "nl")
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        self.assertEqual(report["language"]["check"], "latin")
        self.assertIn("cannot tell one Latin-script language", " ".join(report["notes"]))
        self.set_language("language.reply", "tlh")
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        self.assertEqual(report["language"]["check"], "none")
        self.assertIn("tlh is not a language the Stop-hook check knows", " ".join(report["notes"]))

    def test_doctor_reads_claude_settings_best_effort(self):
        settings_dir = os.path.join(self.os_home, ".claude")
        os.makedirs(settings_dir)
        settings_file = os.path.join(settings_dir, "settings.json")
        with open(settings_file, "w", encoding="utf-8") as handle:
            json.dump({"enabledPlugins": {hosts.CLAUDE_PLUGIN_KEY: False}, "disableAllHooks": False}, handle)
        self.set_language("language.reply", "ja")
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        expected = {"status": "hook-shipped", "plugin_enabled": False, "hooks_disabled": False}
        self.assertEqual(report["language"]["hosts"]["claude"], expected)
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("Claude Code: rule 11 only (the plugin is disabled)", out)
        with open(settings_file, "w", encoding="utf-8") as handle:
            json.dump({"enabledPlugins": {hosts.CLAUDE_PLUGIN_KEY: True}, "disableAllHooks": True}, handle)
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("Claude Code: rule 11 only (disableAllHooks is set);", out)
        with open(settings_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        self.assertEqual(report["language"]["hosts"]["claude"], {"status": "hook-shipped"})

    def test_language_line_explains_each_unenforced_case(self):
        base = {"reply": "ja", "rewrite": True, "layer": "project", "check": "script"}

        def line(**claude: Any) -> str:
            return doctor._language_line(dict(base, hosts={"claude": claude}))

        tail = "; Codex, Antigravity: rule 11 only"
        self.assertEqual(
            line(status="not-shipped"),
            "ja (project) -- Claude Code: rule 11 only (this install carries no hooks)" + tail,
        )
        self.assertEqual(
            line(status="hook-shipped", hooks_disabled=True),
            "ja (project) -- Claude Code: rule 11 only (disableAllHooks is set)" + tail,
        )
        self.assertEqual(
            line(status="hook-shipped", plugin_enabled=False),
            "ja (project) -- Claude Code: rule 11 only (the plugin is disabled)" + tail,
        )
        self.assertEqual(
            line(status="hook-shipped"), "ja (project) -- Claude Code: Stop-hook rewrite + reminder" + tail
        )

    def test_a_project_setting_is_reported_as_the_project_layer(self):
        self.set_language("language.reply", "ko")
        code, _, err = run_cli("config", "set", "language.reply", "ja", "--scope", "project")
        self.assertEqual(code, 0, err)
        _, out, _ = run_cli("doctor", "--fast")
        self.assertIn("Reply language: ja (project) -- Claude Code:", out)
        code, _, err = run_cli("config", "set", "language.reply", "null", "--scope", "project")
        self.assertEqual(code, 0, err)
        report = json.loads(run_cli("doctor", "--fast", "--json")[1])
        self.assertEqual((report["language"]["reply"], report["language"]["layer"]), (None, "project"))

    def copy_install(self, hooks_key: bool, hooks_file: bool) -> str:
        """A plugin root with the Claude Code manifest, with or without its hooks."""
        root = os.path.join(self.tmp, "copy-%s-%s" % (hooks_key, hooks_file))
        os.makedirs(os.path.join(root, ".claude-plugin"))
        with open(os.path.join(REPO_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not hooks_key:
            del manifest["hooks"]
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        if hooks_file:
            os.makedirs(os.path.join(root, "hooks"))
            shutil.copy(os.path.join(REPO_ROOT, "hooks", "claude-code.json"), os.path.join(root, "hooks"))
        return root

    def test_an_install_without_the_hooks_says_so(self):
        self.assertTrue(hosts.claude_hooks_shipped(REPO_ROOT))
        self.assertTrue(hosts.claude_hooks_shipped(self.copy_install(hooks_key=True, hooks_file=True)))
        self.set_language("language.reply", "ja")
        for hooks_key, hooks_file in ((True, False), (False, True)):
            root = self.copy_install(hooks_key, hooks_file)
            self.assertFalse(hosts.claude_hooks_shipped(root), (hooks_key, hooks_file))
            with mock.patch.object(doctor, "PLUGIN_ROOT", root):
                _, out, _ = run_cli("doctor", "--fast")
            expected = (
                "Reply language: ja (global) -- Claude Code: rule 11 only (this install carries no hooks);"
            )
            self.assertIn(expected, out)

    def test_config_show_names_the_reply_language(self):
        _, out, _ = run_cli("config", "show")
        self.assertIn("Reply language: not set  (language.reply)", out)
        self.set_language("language.reply", "zh-TW")
        _, out, _ = run_cli("config", "show")
        self.assertIn("Reply language: zh-TW  (language.reply)", out)


if __name__ == "__main__":
    unittest.main()
