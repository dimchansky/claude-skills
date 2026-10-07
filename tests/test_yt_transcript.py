"""Unit tests for the youtube-transcript pipeline script.

Only pure logic is tested, on synthetic data: no network, yt-dlp or Spokenly needed, so the suite
also runs on Linux CI. Run with: python3 -m unittest discover -s tests -v
"""
import argparse
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugins" / "youtube-transcript" / "skills" / "youtube-transcript" / "scripts" / "yt_transcript.py"
_spec = importlib.util.spec_from_file_location("yt_transcript", SCRIPT)
yt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(yt)


def sentences(count, size=10):
    return [[f"w{i}x{j}" for j in range(size)] for i in range(count)]


def make_words(sents, long_pause_after=()):
    """Spokenly-like word segments; each sentence ends with a period, long pauses follow chosen sentences."""
    words, t = [], 0.0
    for s, sentence in enumerate(sents):
        for k, word in enumerate(sentence):
            text = word + ("." if k == len(sentence) - 1 else "")
            words.append({"start": round(t, 3), "end": round(t + 0.3, 3), "text": text})
            t += 0.35
        t += 1.5 if s in long_pause_after else 0.3
    return words


class FormattingTests(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(yt.fmt_ts(5), "00:05")
        self.assertEqual(yt.fmt_ts(3725), "1:02:05")

    def test_upload_date(self):
        self.assertEqual(yt.fmt_date("20261006"), "2026-10-06")
        self.assertIsNone(yt.fmt_date(None))
        self.assertIsNone(yt.fmt_date("2026"))

    def test_safe_component_keeps_unicode_and_drops_unsafe_characters(self):
        self.assertEqual(yt.safe_component('Это: видео / тест?', 150), "Это видео тест")

    def test_safe_component_limits_bytes(self):
        name = yt.safe_component("Ж" * 100, 51)
        self.assertLessEqual(len(name.encode()), 51)
        self.assertEqual(name, "Ж" * 25)

    def test_sentence_end(self):
        for word in ("конец.", "вопрос?»", "wow!", "итак…"):
            self.assertTrue(yt.is_sentence_end(word), word)
        for word in ("word,", "слово", "e.g"):
            self.assertFalse(yt.is_sentence_end(word), word)

    def test_linked_heading(self):
        chapter = {"title": "Заглянем в финал", "start_time": 66}
        self.assertEqual(yt.linked_heading({"id": "abc", "extractor_key": "Youtube"}, chapter),
                         "## Заглянем в финал · [01:06](https://youtu.be/abc?t=66)")
        self.assertEqual(yt.linked_heading({"id": "abc", "extractor_key": "Vimeo"}, chapter),
                         "## Заглянем в финал · 01:06")


class StructureTests(unittest.TestCase):
    def test_chapter_snaps_to_sentence_start_after_long_pause(self):
        words = make_words(sentences(6), long_pause_after={2})
        late_marker = words[30]["start"] + 2.0  # creator set the chapter 2 s after the sentence began
        sections = yt.build_sections(words, [{"start_time": 0, "title": "A"}, {"start_time": late_marker, "title": "B"}])
        self.assertEqual([(s, e, c["title"]) for s, e, c in sections], [(0, 30, "A"), (30, 60, "B")])

    def test_text_before_first_chapter_has_no_heading(self):
        words = make_words(sentences(4))
        sections = yt.build_sections(words, [{"start_time": words[20]["start"], "title": "Late start"}])
        self.assertEqual([(s, e, c and c["title"]) for s, e, c in sections], [(0, 20, None), (20, 40, "Late start")])

    def test_no_chapters(self):
        words = make_words(sentences(3))
        self.assertEqual(yt.build_sections(words, None), [(0, 30, None)])

    def test_balanced_chunks_cut_at_sentence_starts(self):
        words = make_words(sentences(40))
        plan = yt.plan_chunks(words, [(0, 400, None)], target=100, max_chunks=16)
        self.assertEqual(plan, [(0, 100), (100, 200), (200, 300), (300, 400)])

    def test_cut_prefers_long_pause(self):
        words = make_words(sentences(40), long_pause_after={10})  # sentence 11 starts at word 110
        plan = yt.plan_chunks(words, [(0, 400, None)], target=100, max_chunks=16)
        self.assertEqual(plan[0], (0, 110))

    def test_cut_prefers_chapter_start(self):
        words = make_words(sentences(40))
        sections = [(0, 130, {"title": "A"}), (130, 400, {"title": "B"})]
        plan = yt.plan_chunks(words, sections, target=100, max_chunks=16)
        self.assertEqual(plan[0], (0, 130))

    def test_single_chunk_and_chunk_cap(self):
        words = make_words(sentences(5))
        self.assertEqual(yt.plan_chunks(words, [(0, 50, None)], target=100, max_chunks=16), [(0, 50)])
        words = make_words(sentences(40))
        self.assertEqual(len(yt.plan_chunks(words, [(0, 400, None)], target=10, max_chunks=3)), 3)

    def test_paragraphs_break_at_pauses_and_length(self):
        words = make_words(sentences(10), long_pause_after={3, 7})
        self.assertEqual([len(p.split()) for p in yt.paragraphs(words, 0, len(words))], [40, 40, 20])
        words = make_words(sentences(30))
        self.assertEqual([len(p.split()) for p in yt.paragraphs(words, 0, len(words))], [110, 110, 80])


class EditorIoTests(unittest.TestCase):
    def test_clean_editor_output(self):
        self.assertEqual(yt.clean_editor_output("```markdown\nText\n\n\n\nMore\n```"), "Text\n\nMore")
        self.assertEqual(yt.clean_editor_output('<transcript chunk="1/2">\nText\n</transcript>'), "Text")

    def test_word_changes_ignore_case_punctuation_and_headings(self):
        raw = "## H\n\nКлод код это круто."
        edited = "## H\n\nClaude Code — это круто!"
        self.assertEqual(yt.word_changes(raw, edited), ["Клод код → Claude Code"])

    def test_reference_context(self):
        info = {"title": "T", "uploader": "U", "tags": ["x", "y"], "user_context": "Speaker: Ann",
                "chapters": [{"title": "A"}, {"title": "B"}],
                "description": "Links: https://example.com/a\n" + "d" * 4000}
        context = yt.reference_context(info)
        self.assertIn("Channel: U", context)
        self.assertIn("Chapters: A | B", context)
        self.assertIn("Tags: x, y", context)
        self.assertIn("Notes from the user: Speaker: Ann", context)
        self.assertNotIn("https://", context)
        self.assertTrue(context.endswith(" …"))

    def test_language_rule_never_asks_to_translate(self):
        self.assertIn("Russian", yt.language_rule("ru-RU"))
        self.assertIn("never translate", yt.language_rule(None))


class YtDlpTests(unittest.TestCase):
    def test_retries_transient_failures(self):
        failure = subprocess.CompletedProcess([], 1, "", "ERROR: HTTP Error 403: Forbidden")
        success = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(yt.shutil, "which", return_value="/usr/bin/yt-dlp"), \
                mock.patch.object(yt.subprocess, "run", side_effect=[failure, failure, success]) as run, \
                mock.patch.object(yt.time, "sleep"), mock.patch.object(yt, "log"):
            self.assertEqual(yt.run_ytdlp(["-J", "url"], what="read"), "ok")
        self.assertEqual(run.call_count, 3)

    def test_gives_up_with_upgrade_hint(self):
        failure = subprocess.CompletedProcess([], 1, "", "ERROR: HTTP Error 403: Forbidden")
        with mock.patch.object(yt.shutil, "which", return_value="/usr/bin/yt-dlp"), \
                mock.patch.object(yt.subprocess, "run", return_value=failure), \
                mock.patch.object(yt.time, "sleep"), mock.patch.object(yt, "log"):
            with self.assertRaisesRegex(yt.PipelineError, "brew upgrade yt-dlp"):
                yt.run_ytdlp(["-J", "url"], what="read")

    def test_refetches_incomplete_metadata(self):
        responses = [json.dumps({"id": "x", "title": "T", "channel": None, "channel_url": "u"}),
                     json.dumps({"id": "x", "title": "T", "channel": "C", "channel_url": None})]
        with mock.patch.object(yt, "run_ytdlp", side_effect=responses), mock.patch.object(yt, "log"):
            info = yt.fetch_info("https://youtu.be/x", [])
        self.assertEqual((info["channel"], info["channel_url"]), ("C", "u"))

    def test_rejects_playlists(self):
        with mock.patch.object(yt, "run_ytdlp", return_value=json.dumps({"_type": "playlist"})):
            with self.assertRaisesRegex(yt.PipelineError, "playlist"):
                yt.fetch_info("https://www.youtube.com/playlist?list=x", [])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name) / "video"
        self.workdir.mkdir()
        self.words = make_words(sentences(30), long_pause_after={9, 19})
        self.info = {"id": "abc123", "title": "Test video", "channel": "Chan", "extractor_key": "Youtube",
                     "upload_date": "20261006", "duration": 125, "webpage_url": "https://www.youtube.com/watch?v=abc123",
                     "chapters": [{"start_time": 0, "title": "Intro"},
                                  {"start_time": self.words[100]["start"], "title": "Middle"},
                                  {"start_time": self.words[200]["start"], "title": "End"}]}
        (self.workdir / "info.json").write_text(json.dumps(self.info))
        (self.workdir / "raw.json").write_text(json.dumps({"modelId": "parakeetTDT06", "segments": self.words}))
        self.sections = yt.build_sections(self.words, self.info["chapters"])
        self.plan = yt.plan_chunks(self.words, self.sections, target=100, max_chunks=16)
        self.chunks = yt.write_tasks(self.workdir, self.info, self.words, self.sections, self.plan)

    def tearDown(self):
        self.tmp.cleanup()

    def edit_all(self, transform=lambda text: text.replace("w0x1 ", "zz0x1 ")):
        for chunk in self.chunks:
            Path(chunk["output"]).write_text(transform(Path(chunk["raw"]).read_text()))

    def finalize(self):
        return yt.cmd_finalize(argparse.Namespace(workdir=str(self.workdir)))

    def test_task_files_carry_rules_context_and_output_path(self):
        self.assertEqual([(c["first_word"], c["end_word"]) for c in self.chunks], [(0, 100), (100, 200), (200, 300)])
        task = Path(self.chunks[1]["task"]).read_text()
        self.assertIn("Preserve the original meaning and word order.", task)
        self.assertIn(self.chunks[1]["output"], task)
        self.assertIn('<transcript chunk="2/3">', task)
        self.assertIn("## Middle", task)
        self.assertIn("Chapters: Intro | Middle | End", task)

    def test_finalize_merges_and_links_chapters(self):
        self.edit_all()
        result = self.finalize()
        self.assertEqual(result["warnings"], [])
        transcript = (self.workdir / "transcript.md").read_text()
        self.assertTrue(transcript.startswith("# Test video\n"))
        self.assertIn("- **Published:** 2026-10-06 · **Duration:** 02:05", transcript)
        self.assertIn("## Middle · [00:", transcript)
        self.assertIn("](https://youtu.be/abc123?t=", transcript)
        self.assertIn("- w0x1 → zz0x1", (self.workdir / "changes.md").read_text())
        self.assertEqual(result["edited_words"], 300)

    def test_raw_and_edited_word_counts_use_the_same_tokenizer(self):
        segments = json.loads((self.workdir / "raw.json").read_text())["segments"]
        segments[0]["text"] = "MCP-сервер"  # one recognizer segment, two words for the QA tokenizer
        (self.workdir / "raw.json").write_text(json.dumps({"modelId": "parakeetTDT06", "segments": segments}))
        self.edit_all(lambda text: text)
        self.assertEqual(self.finalize()["raw_words"], 301)

    def test_shortened_chunk_is_flagged(self):
        self.edit_all(lambda text: text[: len(text) // 2])
        warnings = " ".join(self.finalize()["warnings"])
        self.assertIn("possible shortening", warnings)

    def test_lost_heading_and_leftover_marker_are_flagged(self):
        self.edit_all(lambda text: text.replace("## Middle\n", "").replace("w15x0", "[Music] w15x0"))
        warnings = " ".join(self.finalize()["warnings"])
        self.assertIn("expected 1 chapter headings, found 0", warnings)
        self.assertIn("technical markers", warnings)

    def test_missing_chunk_stops_finalize(self):
        self.edit_all()
        Path(self.chunks[2]["output"]).unlink()
        with self.assertRaisesRegex(yt.PipelineError, r"missing or empty: \[3\]"):
            self.finalize()

    def test_edits_survive_rerun_only_if_chunk_plan_is_unchanged(self):
        self.edit_all()
        yt.write_tasks(self.workdir, self.info, self.words, self.sections, self.plan)
        self.assertTrue(all(Path(c["output"]).exists() for c in self.chunks))
        with mock.patch.object(yt, "log"):
            yt.write_tasks(self.workdir, self.info, self.words, self.sections, [(0, 300)])
        self.assertFalse(any(Path(c["output"]).exists() for c in self.chunks))


class PrepareLocalFileTests(unittest.TestCase):
    def test_reuses_existing_transcription_and_keeps_the_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "talk.m4a"
            media.write_bytes(b"")
            out = Path(tmp) / "out"
            (out / "talk").mkdir(parents=True)
            words = make_words(sentences(12))
            (out / "talk" / "raw.json").write_text(json.dumps({"modelId": "parakeetTDT06", "segments": words}))
            args = argparse.Namespace(source=str(media), out_dir=str(out), chunk_words=50, max_chunks=16,
                                      context="Speaker: Ann", keep_audio=False, force=False,
                                      cookies_from_browser=None, ytdlp_args=None)
            with mock.patch.object(yt.sys, "platform", "darwin"), mock.patch.object(yt, "log"):
                result = yt.cmd_prepare(args)
            self.assertEqual((result["words"], len(result["chunks"]), result["warnings"]), (120, 3, []))
            self.assertTrue(media.exists())
            self.assertIn("Notes from the user: Speaker: Ann", Path(result["chunks"][0]["task"]).read_text())
            raw_md = Path(result["raw_transcript"]).read_text()
            self.assertIn("raw ASR output, not edited · 120 words", raw_md)


if __name__ == "__main__":
    unittest.main()
