#!/usr/bin/env python3
"""Turn a YouTube video (or a local audio/video file) into a clean, edited transcript.

Pipeline: yt-dlp (best audio -> FLAC) -> Spokenly CLI (local NVIDIA Parakeet, word timestamps)
          -> chunked editing tasks for LLM sub-agents -> merged, QA-checked Markdown.

  yt_transcript.py doctor
  yt_transcript.py prepare  SOURCE  [--out-dir DIR] [--chunk-words N] [--keep-audio] [--force] ...
  yt_transcript.py finalize WORKDIR

`doctor` checks the environment. `prepare` and `finalize` print a compact JSON summary to stdout;
progress goes to stderr. Standard library only, Python 3.9+. macOS only: Spokenly's CLI and local server
exist only in its macOS direct-download build (Spokenly for Windows/Linux has no documented CLI).
External tools: yt-dlp, ffmpeg, deno, Spokenly.app 2.29+ (its bundled CLI).
"""
from __future__ import annotations

import argparse
import bisect
import datetime
import difflib
import json
import math
import platform
import plistlib
import re
import shlex
import shutil
import socket
import string
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TASK_TEMPLATE = SKILL_DIR / "assets" / "editor-task-template.md"
PLUGIN_MANIFEST = SKILL_DIR.parent.parent / ".claude-plugin" / "plugin.json"
SPOKENLY_CLI = Path.home() / "Library" / "Application Support" / "Spokenly" / "spokenly"
SPOKENLY_PORT = 51089
MIN_SPOKENLY_VERSION = (2, 29)
YTDLP_MAX_AGE_DAYS = 60
DEFAULT_OUT_DIR = "youtube-transcripts"

# NVIDIA Parakeet TDT 0.6B v3 transcribes these 25 European languages (auto-detected).
PARAKEET_V3_LANGS = set("bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl es sv ru uk".split())
LANG_NAMES = {"ru": "Russian", "uk": "Ukrainian", "en": "English", "de": "German", "fr": "French",
              "es": "Spanish", "it": "Italian", "pt": "Portuguese", "pl": "Polish", "nl": "Dutch"}
MODEL_NAMES = {"parakeetTDT06": "Parakeet TDT 0.6B v3"}
INFO_KEYS = ("id", "title", "channel", "channel_url", "uploader", "upload_date", "duration",
             "duration_string", "webpage_url", "language", "description", "tags", "chapters", "extractor_key")

SENTENCE_END = re.compile(r"[.!?…][\"'»”’)\]]*$")
HEADING = re.compile(r"^#{1,6}\s")
MARKER = re.compile(r"\[(?:music|applause|laughter|inaudible|noise|silence|музыка|аплодисменты|смех|"
                    r"неразборчиво|шум|тишина)[^\]]*\]", re.I)

YTDLP_HINTS = """How to fix, in this order:
  1. Re-run: YouTube sometimes answers 403 once and works on the next try (this script already retried).
  2. Update yt-dlp: `brew upgrade yt-dlp` (or `yt-dlp -U` if it was not installed with Homebrew).
     YouTube changes constantly; an outdated yt-dlp is the most common cause of 403 / missing formats.
  3. Check the JavaScript runtime yt-dlp needs for YouTube: `deno --version` (else `brew install deno`).
  4. Age-restricted / members-only / private video: pass --cookies-from-browser chrome (or safari, firefox)."""

# Editing QA thresholds (normalized words, raw vs edited). Careful edits measured on real videos
# land at 99-100% length and ~0.96 similarity, so these leave a wide margin for legitimate fixes.
MIN_LENGTH_RATIO, MAX_LENGTH_RATIO, MIN_SIMILARITY = 0.90, 1.10, 0.80


class PipelineError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[yt-transcript] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- helpers

def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_date(upload_date: str | None) -> str | None:
    if upload_date and re.fullmatch(r"\d{8}", upload_date):
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    return None


def safe_component(text: str, max_bytes: int) -> str:
    """Filesystem-safe name that keeps Unicode (Cyrillic titles stay readable)."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    while len(text.encode()) > max_bytes:
        text = text[:-1]
    return text.rstrip(" .") or "untitled"


def is_sentence_end(word: str) -> bool:
    return bool(SENTENCE_END.search(word))


def gap_before(words: list[dict], j: int) -> float:
    return max(0.0, words[j]["start"] - words[j - 1]["end"]) if 0 < j < len(words) else 0.0


def norm_words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def strip_headings(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not HEADING.match(line))


# ---------------------------------------------------------------- yt-dlp

def run_ytdlp(args: list[str], what: str, attempts: int = 3) -> str:
    if not shutil.which("yt-dlp"):
        raise PipelineError("yt-dlp is not installed: brew install yt-dlp ffmpeg deno")
    tail = ""
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(["yt-dlp", *args], capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        tail = "\n".join(lines[-4:])
        log(f"yt-dlp could not {what} (attempt {attempt}/{attempts}): {lines[-1] if lines else proc.returncode}")
        if attempt < attempts:
            time.sleep(4 * attempt)
    raise PipelineError(f"yt-dlp could not {what}.\n{tail}\n\n{YTDLP_HINTS}")


def fetch_info(url: str, extra: list[str]) -> dict:
    info: dict = {}
    for attempt in range(3):  # YouTube occasionally omits fields (e.g. channel) in one response
        data = json.loads(run_ytdlp(["-J", "--no-playlist", *extra, url], what="read the video metadata"))
        if data.get("_type") == "playlist":
            raise PipelineError("This URL is a playlist. Run `prepare` once per video URL.")
        info = {key: info.get(key) if info.get(key) is not None else data.get(key) for key in INFO_KEYS}
        if info.get("title") and (info.get("channel") or info.get("uploader")):
            break
        log("metadata came back incomplete, fetching it again")
    return info


def download_audio(url: str, workdir: Path, extra: list[str]) -> Path:
    audio = workdir / "audio.flac"
    if audio.exists():
        log("audio.flac already present, skipping download")
        return audio
    if not shutil.which("ffmpeg"):
        raise PipelineError("ffmpeg is not installed: brew install ffmpeg")
    log("downloading best audio and converting it to FLAC (yt-dlp + ffmpeg)")
    started = time.time()
    template = str(workdir).replace("%", "%%") + "/audio.%(ext)s"  # output template is %-formatted
    run_ytdlp(["-f", "ba/b", "-x", "--audio-format", "flac", "--no-playlist", "--no-overwrites",
               "--no-progress", "-o", template, *extra, url], what="download the audio")
    if not audio.exists():
        raise PipelineError(f"yt-dlp finished but {audio} is missing")
    log(f"audio ready in {time.time() - started:.0f}s ({audio.stat().st_size / 1e6:.0f} MB)")
    return audio


# ---------------------------------------------------------------- Spokenly

def spokenly_listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", SPOKENLY_PORT), timeout=1):
            return True
    except OSError:
        return False


def ensure_spokenly() -> None:
    if not spokenly_listening():
        log("Spokenly is not running, launching it in the background")
        if subprocess.run(["open", "-g", "-a", "Spokenly"], capture_output=True).returncode != 0:
            raise PipelineError("Spokenly.app is not installed: brew install --cask spokenly (or download it from "
                                "https://spokenly.app; version 2.29+, the direct-download build, not the App Store one).")
        for _ in range(60):
            if spokenly_listening():
                break
            time.sleep(1)
        else:
            raise PipelineError(f"Spokenly started but nothing listens on localhost:{SPOKENLY_PORT}. "
                                "Open Spokenly.app, make sure it finished loading, and retry.")
    if not SPOKENLY_CLI.exists():
        raise PipelineError(f"Spokenly CLI not found at {SPOKENLY_CLI}. Update Spokenly to 2.29+ and launch it once.")


def transcribe(audio: Path) -> dict:
    ensure_spokenly()
    log(f"transcribing locally with Spokenly: {audio.name}")
    started = time.time()
    proc = subprocess.run([str(SPOKENLY_CLI), "transcribe", str(audio), "--format", "json"],
                          capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise PipelineError(f"Spokenly transcription failed: {(proc.stderr or proc.stdout).strip()}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise PipelineError(f"Spokenly returned non-JSON output: {proc.stdout[:300]!r}")
    if not data.get("segments"):
        raise PipelineError("Spokenly returned an empty transcript (no speech detected?)")
    log(f"{len(data['segments'])} words in {time.time() - started:.0f}s, model {data.get('modelId')}")
    return data


# ---------------------------------------------------------------- structure: chapters, chunks, paragraphs

def snap_to_boundary(words: list[dict], starts: list[float], t: float, window: float = 10.0) -> int:
    """Index of the word that should open a chapter starting near time t (prefer sentence starts after pauses)."""
    lo = max(1, bisect.bisect_left(starts, t - window))
    hi = bisect.bisect_right(starts, t + window)
    candidates = list(range(lo, hi))
    if not candidates:
        return min(bisect.bisect_left(starts, t), len(words))
    pool = [j for j in candidates if is_sentence_end(words[j - 1]["text"])] or candidates
    return min(pool, key=lambda j: abs(words[j]["start"] - t) - 3 * min(gap_before(words, j), 2.0))


def build_sections(words: list[dict], chapters: list[dict] | None) -> list[tuple[int, int, dict | None]]:
    """Split the word list into (start, end, chapter) sections; chapter is None for text without a heading."""
    n, starts, opened = len(words), [w["start"] for w in words], []
    for chapter in chapters or []:
        t = float(chapter.get("start_time") or 0)
        idx = 0 if t <= 1 else snap_to_boundary(words, starts, t)
        if opened and idx <= opened[-1][0]:
            idx = opened[-1][0] + 1
        if idx >= n:
            break
        opened.append((idx, chapter))
    if not opened:
        return [(0, n, None)]
    sections = [(0, opened[0][0], None)] if opened[0][0] > 0 else []
    for k, (idx, chapter) in enumerate(opened):
        sections.append((idx, opened[k + 1][0] if k + 1 < len(opened) else n, chapter))
    return sections


def plan_chunks(words: list[dict], sections: list, target: int, max_chunks: int) -> list[tuple[int, int]]:
    """Balanced chunks for parallel editing; cut at chapter starts, else at sentence ends after long pauses."""
    n = len(words)
    k = max(1, min(max_chunks, math.ceil(n / target)))
    if k == 1:
        return [(0, n)]
    size = n / k
    chapter_cuts = [s for s, _, chapter in sections if s > 0 and chapter]
    sentence_cuts = [j for j in range(1, n) if is_sentence_end(words[j - 1]["text"])]
    cuts: list[int] = []
    for c in range(1, k):
        ideal = c * size
        near_chapter = [j for j in chapter_cuts if abs(j - ideal) <= 0.35 * size]
        near_sentence = [j for j in sentence_cuts if abs(j - ideal) <= 0.15 * size]
        if near_chapter:
            j = min(near_chapter, key=lambda j: abs(j - ideal))
        elif near_sentence:
            # among pauses within 0.15 s of the longest one, take the cut closest to the ideal position
            pause = {j: min(gap_before(words, j), 3.0) for j in near_sentence}
            longest = max(pause.values())
            j = min((j for j in near_sentence if pause[j] >= longest - 0.15), key=lambda j: abs(j - ideal))
        elif sentence_cuts:
            j = min(sentence_cuts, key=lambda j: abs(j - ideal))
        else:
            j = round(ideal)
        if 0 < j < n and (not cuts or j > cuts[-1]):
            cuts.append(j)
    bounds = [0, *cuts, n]
    return list(zip(bounds[:-1], bounds[1:]))


def paragraphs(words: list[dict], a: int, b: int, min_words: int = 35, max_words: int = 110) -> list[str]:
    """Rough paragraphs: break at a sentence end followed by a pause, or when a paragraph gets long."""
    out, current = [], []
    for i in range(a, b):
        current.append(words[i]["text"])
        if is_sentence_end(words[i]["text"]):
            pause = gap_before(words, i + 1)
            if (len(current) >= min_words and pause >= 0.9) or len(current) >= max_words:
                out.append(" ".join(current))
                current = []
    if current:
        out.append(" ".join(current))
    return out


def render_range(words: list[dict], sections: list, a: int, b: int, heading) -> str:
    parts = []
    for s, e, chapter in sections:
        lo, hi = max(s, a), min(e, b)
        if lo >= hi:
            continue
        if chapter and s >= a:
            parts.append(heading(chapter))
        parts.extend(paragraphs(words, lo, hi))
    return "\n\n".join(parts)


def plain_heading(chapter: dict) -> str:
    return f"## {chapter['title']}"


def linked_heading(info: dict, chapter: dict) -> str:
    t = int(float(chapter.get("start_time") or 0))
    video_id, extractor = info.get("id"), (info.get("extractor_key") or "").lower()
    if video_id and extractor.startswith("youtube"):
        return f"## {chapter['title']} · [{fmt_ts(t)}](https://youtu.be/{video_id}?t={t})"
    return f"## {chapter['title']} · {fmt_ts(t)}"


def document_header(info: dict, model_id: str | None, word_count: int, edited: bool) -> str:
    lines = [f"# {info.get('title') or 'Transcript'}", ""]
    channel = info.get("channel") or info.get("uploader")
    if channel:
        lines.append(f"- **Channel:** [{channel}]({info['channel_url']})" if info.get("channel_url")
                     else f"- **Channel:** {channel}")
    date = fmt_date(info.get("upload_date"))
    duration = fmt_ts(info["duration"]) if info.get("duration") else info.get("duration_string")
    when = " · ".join(x for x in (date and f"**Published:** {date}", duration and f"**Duration:** {duration}") if x)
    if when:
        lines.append(f"- {when}")
    if info.get("webpage_url"):
        lines.append(f"- **Video:** {info['webpage_url']}")
    model = MODEL_NAMES.get(model_id or "", model_id or "unknown model")
    status = "edited by Claude" if edited else "raw ASR output, not edited"
    lines.append(f"- **Transcript:** Spokenly · {model} (local) · {status} · {word_count:,} words")
    return "\n".join(lines) + "\n\n---\n\n"


# ---------------------------------------------------------------- editing tasks

def reference_context(info: dict) -> str:
    channel = info.get("channel") or info.get("uploader")
    lines = [f"Title: {info['title']}"] if info.get("title") else []
    lines += [f"Channel: {channel}"] if channel else []
    if info.get("chapters"):
        lines.append("Chapters: " + " | ".join(c["title"] for c in info["chapters"]))
    if info.get("tags"):
        lines.append("Tags: " + ", ".join(info["tags"]))
    if info.get("user_context"):
        lines.append("Notes from the user: " + info["user_context"])
    description = re.sub(r"https?://\S+", "", info.get("description") or "")
    description = re.sub(r"[ \t]+\n", "\n", description)
    description = re.sub(r"\n{3,}", "\n\n", description).strip()
    if description:
        lines.append("Description:\n" + (description[:3000].rstrip() + " …" if len(description) > 3000 else description))
    return "\n".join(lines) or "(none)"


def language_rule(language: str | None) -> str:
    code = (language or "").split("-")[0].lower()
    hint = f" (video metadata says: {LANG_NAMES.get(code, code)})" if code else ""
    return f"keep the text in the language it is spoken in{hint}; never translate it."


def write_tasks(workdir: Path, info: dict, words: list[dict], sections: list, plan: list) -> list[dict]:
    chunks_dir = workdir / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    manifest_path = chunks_dir / "manifest.json"
    if manifest_path.exists():  # edits from a previous run stay valid only if the chunk plan is unchanged
        old = json.loads(manifest_path.read_text())
        if [[c["first_word"], c["end_word"]] for c in old.get("chunks", [])] != [list(p) for p in plan]:
            for stale in chunks_dir.glob("*.md"):
                stale.unlink()
            log("chunk plan changed: removed previous chunk files")
    template = string.Template(TASK_TEMPLATE.read_text())
    context, total, chunks = reference_context(info), len(plan), []
    for no, (a, b) in enumerate(plan, 1):
        raw_path, task_path = chunks_dir / f"{no:02d}.raw.md", chunks_dir / f"{no:02d}.task.md"
        out_path = chunks_dir / f"{no:02d}.edited.md"
        text = render_range(words, sections, a, b, plain_heading)
        raw_path.write_text(text + "\n")
        fragment = ("this is the complete transcript." if total == 1 else
                    f"this is chunk {no} of {total} of one continuous transcript, cut at a sentence boundary. "
                    "It may begin or end mid-topic: do not add introductions, transitions or conclusions, "
                    "and keep its first and last sentences.")
        task_path.write_text(template.safe_substitute(
            chunk_no=no, chunk_total=total, language_rule=language_rule(info.get("language")),
            fragment_rule=fragment, output_path=str(out_path), reference_context=context, transcript=text))
        chunks.append({"no": no, "first_word": a, "end_word": b, "words": b - a,
                       "start": fmt_ts(words[a]["start"]), "end": fmt_ts(words[b - 1]["end"]),
                       "headings": [linked_heading(info, ch) for s, _, ch in sections if ch and a <= s < b],
                       "raw": str(raw_path), "task": str(task_path), "output": str(out_path)})
    manifest_path.write_text(json.dumps({"chunks": chunks}, ensure_ascii=False, indent=2))
    return chunks


# ---------------------------------------------------------------- doctor

def plugin_version() -> str:
    try:
        return json.loads(PLUGIN_MANIFEST.read_text()).get("version", "dev")
    except (OSError, ValueError):
        return "dev"


def tool_output(cmd: list[str]) -> str | None:
    """First line of a tool's output, or None when it is missing or fails."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (proc.stdout or proc.stderr).strip().splitlines()
    return lines[0].strip() if proc.returncode == 0 and lines else None


def find_spokenly_app() -> Path | None:
    for app in (Path("/Applications/Spokenly.app"), Path.home() / "Applications" / "Spokenly.app"):
        if app.exists():
            return app
    found = tool_output(["mdfind", "kMDItemCFBundleIdentifier == 'app.spokenly'"])
    return Path(found) if found and found.endswith(".app") else None


def spokenly_settings() -> tuple[str | None, bool | None]:
    """(model id used for file transcription, local-only mode) from Spokenly's preferences."""
    proc = subprocess.run(["defaults", "export", "app.spokenly", "-"], capture_output=True)
    if proc.returncode != 0:
        return None, None
    prefs = plistlib.loads(proc.stdout)
    raw = prefs.get("fileTranscriptionVoiceModelID") or prefs.get("transcriptionModelID")
    if isinstance(raw, bytes):  # stored as a JSON-encoded string
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = raw.decode(errors="replace")
    local = prefs.get("localOnlyMode")
    return (raw if isinstance(raw, str) else None), (None if local is None else bool(local))


def cmd_doctor(_args: argparse.Namespace) -> int:
    rows: list[tuple[str, str, str | None]] = []  # (level, message, fix)

    def check(level: str, message: str, fix: str | None = None) -> None:
        rows.append((level, message, fix))

    if sys.platform != "darwin":
        check("fail", f"macOS required, this is {platform.system()}",
              "Spokenly runs on Windows and Linux too, but the CLI and local server this skill uses are "
              "documented only for its macOS direct-download build (see the plugin README, Platform support).")
    else:
        arch = platform.machine()
        check("ok" if arch == "arm64" else "warn", f"macOS {platform.mac_ver()[0]} on {arch}",
              None if arch == "arm64" else "Apple Silicon is recommended: Parakeet runs on the Neural Engine.")
    version = ".".join(map(str, sys.version_info[:3]))
    check("ok" if sys.version_info >= (3, 9) else "fail", f"Python {version}",
          None if sys.version_info >= (3, 9) else "Python 3.9+ is required: xcode-select --install")

    ytdlp = tool_output(["yt-dlp", "--version"])
    if not ytdlp:
        check("fail", "yt-dlp not found", "brew install yt-dlp")
    else:
        match = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", ytdlp)
        age = (datetime.date.today() - datetime.date(*map(int, match.groups()))).days if match else 0
        upgrade = "brew upgrade yt-dlp" if "homebrew" in (shutil.which("yt-dlp") or "").lower() else "yt-dlp -U"
        if age > YTDLP_MAX_AGE_DAYS:
            check("warn", f"yt-dlp {ytdlp} is {age} days old; YouTube breaks old versions", upgrade)
        else:
            check("ok", f"yt-dlp {ytdlp}")
    ffmpeg = tool_output(["ffmpeg", "-hide_banner", "-version"])
    check("ok", ffmpeg.split(" Copyright")[0]) if ffmpeg else check("fail", "ffmpeg not found", "brew install ffmpeg")
    deno = tool_output(["deno", "--version"])
    if deno:
        check("ok", f"{deno.split(' (')[0]} (JavaScript runtime yt-dlp needs for YouTube)")
    else:
        check("fail", "deno not found: yt-dlp needs a JavaScript runtime for YouTube", "brew install deno")

    app = find_spokenly_app() if sys.platform == "darwin" else None
    if not app:
        check("fail", "Spokenly.app not found",
              "brew install --cask spokenly (or the direct-download build from https://spokenly.app)")
    else:
        try:
            app_version = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get("CFBundleShortVersionString", "0")
        except (OSError, plistlib.InvalidFileException):
            app_version = "0"
        parts = tuple(int(x) for x in re.findall(r"\d+", app_version)[:2])
        if (app / "Contents" / "_MASReceipt").exists():
            check("fail", f"Spokenly {app_version} is the App Store build, which has no local server or CLI",
                  "brew install --cask spokenly (or the direct-download build from https://spokenly.app)")
        elif parts < MIN_SPOKENLY_VERSION:
            check("fail", f"Spokenly {app_version} is too old (needs 2.29+ for its CLI)", "Update Spokenly")
        else:
            check("ok", f"Spokenly {app_version} ({app})")
        if spokenly_listening():
            check("ok", f"Spokenly server is running on localhost:{SPOKENLY_PORT}")
        else:
            check("warn", "Spokenly is not running", "Nothing to do: `prepare` starts it in the background")
        if SPOKENLY_CLI.exists():
            check("ok", f"Spokenly CLI ({SPOKENLY_CLI})")
        else:
            check("warn", "Spokenly CLI not created yet", "Launch Spokenly once; it writes the CLI on start")
        model, local_only = spokenly_settings()
        if model and "parakeet" in model.lower():
            mode = {True: ", local-only mode", False: ", local-only mode off", None: ""}[local_only]
            check("ok", f"File-transcription model: {MODEL_NAMES.get(model, model)}{mode}")
        elif model:
            check("warn", f"File-transcription model is '{model}', not Parakeet",
                  "Spokenly > Settings: choose Parakeet TDT 0.6B v3 for file transcription")
        else:
            check("warn", "Could not read Spokenly's file-transcription model",
                  "Open Spokenly and choose Parakeet TDT 0.6B v3 for file transcription")

    icons = {"ok": "✓", "warn": "!", "fail": "✗"}
    print(f"youtube-transcript {plugin_version()}: environment check")
    for level, message, fix in rows:
        print(f"  {icons[level]} {message}")
        if fix:
            print(f"      fix: {fix}")
    failures = sum(level == "fail" for level, _, _ in rows)
    warnings = sum(level == "warn" for level, _, _ in rows)
    print(f"\n{'Ready.' if not failures else f'{failures} problem(s) to fix.'}"
          + (f" {warnings} warning(s)." if warnings else ""))
    return 1 if failures else 0


# ---------------------------------------------------------------- commands

def cmd_prepare(args: argparse.Namespace) -> dict:
    if sys.platform != "darwin":
        raise PipelineError("This skill needs macOS: it drives Spokenly through the CLI and local server that only "
                            "Spokenly's macOS direct-download build provides.")
    extra = shlex.split(args.ytdlp_args or "")
    if args.cookies_from_browser:
        extra += ["--cookies-from-browser", args.cookies_from_browser]
    source = Path(args.source).expanduser()
    local = source.is_file()
    if local:
        info = {"title": source.stem, "source_file": str(source.resolve())}
        name = safe_component(source.stem, 150)
    else:
        log("reading video metadata")
        info = fetch_info(args.source, extra)
        date = fmt_date(info.get("upload_date"))
        title = safe_component(info.get("title") or "untitled", 150)
        name = f"{date + ' - ' if date else ''}{title} [{info.get('id')}]"
    if args.context:
        info["user_context"] = args.context

    workdir = (Path(args.out_dir).expanduser() / name).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    warnings = []

    raw_json = workdir / "raw.json"
    if raw_json.exists() and not args.force:
        log("raw.json exists, reusing the previous transcription (use --force to redo)")
        data = json.loads(raw_json.read_text())
    else:
        audio = source.resolve() if local else download_audio(args.source, workdir, extra)
        data = transcribe(audio)
        raw_json.write_text(json.dumps(data, ensure_ascii=False))
        if not local and not args.keep_audio:
            audio.unlink()

    words = [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
             for s in data["segments"] if s.get("text", "").strip()]
    (workdir / "raw.txt").write_text(" ".join(w["text"] for w in words) + "\n")
    if local and not info.get("duration"):
        info["duration"] = round(words[-1]["end"])
        (workdir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))

    model_id = data.get("modelId")
    if model_id and "parakeet" not in model_id.lower():
        warnings.append(f"Spokenly used model '{model_id}', not Parakeet: check Settings > File Transcription in Spokenly.")
    language = (info.get("language") or "").split("-")[0].lower()
    if language and language not in PARAKEET_V3_LANGS and "parakeet" in (model_id or "").lower():
        warnings.append(f"Video language '{language}' is not supported by Parakeet v3: expect a poor transcript. "
                        "Switch Spokenly's file-transcription model to Whisper, or tell the user.")

    sections = build_sections(words, info.get("chapters"))
    plan = plan_chunks(words, sections, args.chunk_words, args.max_chunks)
    raw_md = workdir / "transcript.raw.md"
    raw_md.write_text(document_header(info, model_id, len(words), edited=False)
                      + render_range(words, sections, 0, len(words), lambda ch: linked_heading(info, ch)) + "\n")
    chunks = write_tasks(workdir, info, words, sections, plan)

    return {"workdir": str(workdir), "title": info.get("title"),
            "duration": fmt_ts(words[-1]["end"]), "language": info.get("language"), "model": model_id,
            "words": len(words), "chapters": len(info.get("chapters") or []), "raw_transcript": str(raw_md),
            "chunks": [{"no": c["no"], "words": c["words"], "span": f"{c['start']}-{c['end']}",
                        "task": c["task"], "output": c["output"],
                        "already_edited": Path(c["output"]).exists()} for c in chunks],
            "warnings": warnings}


def clean_editor_output(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    text = re.sub(r"^<transcript[^>]*>\s*|\s*</transcript>$", "", text).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def word_changes(raw: str, edited: str) -> list[str]:
    """Word-level substitutions/insertions/deletions, ignoring case and punctuation."""
    def tokens(text):
        shown = [w for w in strip_headings(text).split() if norm_words(w)]
        return shown, ["".join(norm_words(w)) for w in shown]
    a_shown, a = tokens(raw)
    b_shown, b = tokens(edited)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            out.append(f"{' '.join(a_shown[i1:i2]) or '∅'} → {' '.join(b_shown[j1:j2]) or '∅'}")
    return out


def cmd_finalize(args: argparse.Namespace) -> dict:
    workdir = Path(args.workdir).expanduser().resolve()
    info = json.loads((workdir / "info.json").read_text())
    data = json.loads((workdir / "raw.json").read_text())
    chunks = json.loads((workdir / "chunks" / "manifest.json").read_text())["chunks"]

    missing = [c["no"] for c in chunks
               if not Path(c["output"]).exists() or not Path(c["output"]).read_text().strip()]
    if missing:
        raise PipelineError(f"edited chunk(s) missing or empty: {missing}. Run the editor for them, then finalize again.")

    parts, warnings, stats, changes_md = [], [], [], []
    for c in chunks:
        raw = Path(c["raw"]).read_text()
        edited = clean_editor_output(Path(c["output"]).read_text())
        lines = edited.split("\n")
        found = [i for i, line in enumerate(lines) if HEADING.match(line)]
        if len(found) == len(c["headings"]):
            for i, heading in zip(found, c["headings"]):
                lines[i] = heading
            edited = "\n".join(lines)
        else:
            warnings.append(f"chunk {c['no']}: expected {len(c['headings'])} chapter headings, found {len(found)}")
        r, e = norm_words(strip_headings(raw)), norm_words(strip_headings(edited))
        ratio = len(e) / max(1, len(r))
        similarity = difflib.SequenceMatcher(None, r, e, autojunk=False).ratio()
        changes = word_changes(raw, edited)
        if not MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO:
            warnings.append(f"chunk {c['no']}: edited length is {ratio:.0%} of raw (expected 90-110%): "
                            "possible shortening/rewriting")
        if similarity < MIN_SIMILARITY:
            warnings.append(f"chunk {c['no']}: word similarity {similarity:.0%} < {MIN_SIMILARITY:.0%}: "
                            "editor changed too much (rewrite or translation?)")
        if MARKER.search(edited):
            warnings.append(f"chunk {c['no']}: technical markers left: {sorted(set(MARKER.findall(edited)))[:5]}")
        stats.append({"no": c["no"], "raw_words": len(r), "edited_words": len(e),
                      "length_ratio": round(ratio, 3), "similarity": round(similarity, 3), "word_changes": len(changes)})
        changes_md.append(f"## Chunk {c['no']} ({c['start']}-{c['end']}): {len(changes)} changes\n\n"
                          + "\n".join(f"- {x}" for x in changes))
        parts.append(edited)

    body = "\n\n".join(parts)
    edited_words = len(norm_words(strip_headings(body)))
    out = workdir / "transcript.md"
    out.write_text(document_header(info, data.get("modelId"), edited_words, edited=True) + body + "\n")
    changes_path = workdir / "changes.md"
    changes_path.write_text("# Editor changes (raw → edited, ignoring case and punctuation)\n\n"
                            + "\n\n".join(changes_md) + "\n")
    raw_words = len(norm_words(" ".join(s.get("text", "") for s in data["segments"])))  # same counting as edited
    return {"transcript": str(out), "raw_transcript": str(workdir / "transcript.raw.md"),
            "changes": str(changes_path), "raw_words": raw_words, "edited_words": edited_words,
            "chunks": stats, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"youtube-transcript {plugin_version()}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check that every required tool is installed and configured")
    p = sub.add_parser("prepare", help="download + transcribe + write editing tasks")
    p.add_argument("source", help="video URL (YouTube or any yt-dlp site) or a local audio/video file")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"parent folder for results (default: ./{DEFAULT_OUT_DIR})")
    p.add_argument("--chunk-words", type=int, default=1500, help="target words per editing chunk (default 1500)")
    p.add_argument("--max-chunks", type=int, default=16, help="upper bound on parallel editing chunks (default 16)")
    p.add_argument("--context", help="extra names/terms for the editor, e.g. 'speaker: Ivan Petrov; product: Foo'")
    p.add_argument("--keep-audio", action="store_true", help="keep audio.flac after transcription")
    p.add_argument("--force", action="store_true", help="re-download and re-transcribe even if raw.json exists")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="pass cookies to yt-dlp (chrome, safari, ...)")
    p.add_argument("--ytdlp-args", help="extra yt-dlp arguments as one quoted string")
    f = sub.add_parser("finalize", help="merge edited chunks, run QA, write transcript.md")
    f.add_argument("workdir")
    args = parser.parse_args()
    if args.command == "doctor":
        return cmd_doctor(args)
    try:
        result = cmd_prepare(args) if args.command == "prepare" else cmd_finalize(args)
    except PipelineError as err:
        log(f"ERROR: {err}")
        if args.command == "prepare":
            log(f"For a full environment check run: python3 {Path(__file__).resolve()} doctor")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
