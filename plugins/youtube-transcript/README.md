# YouTube Transcript

A Claude Code plugin that turns a YouTube video (or anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) can download, or a local audio/video file) into a clean, proofread Markdown transcript. Chapter headings in the result link back to the right second of the video.

Speech recognition runs **locally** on your Mac, in [Spokenly](https://spokenly.app) with NVIDIA's Parakeet TDT 0.6B v3 model. Claude then proofreads the text with several editors working in parallel and checks that nothing was shortened, rewritten or translated.

> **macOS only for now.** Spokenly also exists for Windows and Linux, but the command-line interface the plugin uses to drive it is documented only for Spokenly's macOS direct-download build. See [Platform support](#platform-support).

## What you get

```
youtube-transcripts/2026-10-06 - <video title> [<video id>]/
├── transcript.md       ← the clean transcript: header, chapter headings with timestamp links, paragraphs
├── transcript.raw.md   ← raw recognizer output, ready ~20 s after you ask (no LLM involved)
├── changes.md          ← every word the editors changed, for spot checks
├── raw.json, raw.txt   ← word-level timestamps / plain text from Spokenly
├── info.json           ← video metadata
└── chunks/             ← editing tasks and results
```

## How it works

```
URL ─ yt-dlp ─▶ audio.flac ─ Spokenly · Parakeet (local) ─▶ words + timestamps
    ─ prepare ─▶ N task files ─ N parallel Claude editors ─▶ edited chunks ─ finalize ─▶ transcript.md
```

1. **prepare** fetches the metadata and audio with yt-dlp (with retries, because YouTube sometimes answers 403 once). It transcribes the audio with Spokenly's CLI, which takes about 10 s for 20 minutes of audio on Apple Silicon. Then it maps YouTube chapters to sentence boundaries and splits the text into ~1,500-word chunks. Each chunk becomes a self-contained editing task: the editing rules, a glossary built from the video's title, description, tags and chapters (so names like *DaVinci Resolve* or *Claude Code* come out right), and the text.
2. **Editors.** One `transcript-editor` subagent per chunk, all running at once. They can only read their task file and write their result. The rules: keep the meaning, word order and style; fix punctuation, spelling and misrecognized words; split into paragraphs; remove `[Music]`-style markers and recognizer hallucinations; never summarize or translate.
3. **finalize** merges the chunks, restores the chapter headings with timestamp links, and runs QA checks per chunk: length ratio, word-level similarity to the raw text, chapter headings, leftover markers. Careful edits keep ~99–100% of the words at ~0.96 similarity, so anything far off is flagged.

The main Claude session never reads the transcript itself. That keeps long videos fast and cheap.

**Timing** on an Apple Silicon Mac, for a 23-minute video: `prepare` takes ~20 s. Editing takes well under a minute: the bundled editor needed 22 s for a ~1,000-word chunk, and all chunks run in parallel, so longer videos take about as long. `finalize` takes under a second. If Claude falls back to a general-purpose subagent, for example right after installing before `/reload-plugins`, editing takes 3–4 minutes instead, because that agent inherits your session's thinking and effort settings.

**Model choice.** On the same chunks, Sonnet and Opus produced near-identical edits: 3–5 differing spots per ~1,500 words, with the same QA scores. The editor therefore runs on Sonnet.

## Requirements

| What | Version | Install |
|---|---|---|
| macOS, Apple Silicon recommended | — | — |
| [Claude Code](https://code.claude.com) | with plugin support | — |
| [Spokenly](https://spokenly.app) | 2.29+, **direct-download build** (the App Store build has no local server or CLI) | `brew install --cask spokenly`, or download it from the website |
| Parakeet TDT 0.6B v3 selected in Spokenly as the file-transcription model | — | Spokenly settings |
| yt-dlp, ffmpeg, deno (yt-dlp's JavaScript runtime for YouTube) | yt-dlp ≤ 60 days old | `brew install yt-dlp ffmpeg deno` |
| Python | 3.9+ (the `python3` that comes with Xcode Command Line Tools is enough) | `xcode-select --install` |

After installing, check everything at once. Just ask Claude *"run the youtube-transcript doctor"*, or run it yourself:

```bash
python3 "$(find ~/.claude/plugins/cache -path '*youtube-transcript/scripts/yt_transcript.py' | sort -V | tail -1)" doctor
```

```
youtube-transcript 1.0.0: environment check
  ✓ macOS 26.6.1 on arm64
  ✓ Python 3.14.8
  ✓ yt-dlp 2026.08.19
  ✓ ffmpeg version 9.0.2
  ✓ deno 2.9.7 (JavaScript runtime yt-dlp needs for YouTube)
  ✓ Spokenly 2.29.1 (/Applications/Spokenly.app)
  ✓ Spokenly server is running on localhost:51089
  ✓ Spokenly CLI (~/Library/Application Support/Spokenly/spokenly)
  ✓ File-transcription model: Parakeet TDT 0.6B v3, local-only mode

Ready.
```

Every failed check prints the exact command or setting that fixes it. yt-dlp in particular needs regular updates (`brew upgrade yt-dlp`), because YouTube keeps changing. `doctor` warns when yours is more than 60 days old.

## Installation

See the [repository README](../../README.md#install).

## Usage

Talk to Claude Code as usual. The skill triggers on its own:

- *Сделай текст из этого видео: https://youtu.be/…*
- *Transcribe https://www.youtube.com/watch?v=… and save it to ~/Documents/talks*
- *What are the main arguments in this talk? https://youtu.be/…* This takes the fast path: Claude answers from the raw transcript without the editing step.
- *Расшифруй ~/Downloads/interview.m4a, говорят Анна Петрова и Иван Сидоров*

Or invoke it explicitly: `/youtube-transcript:youtube-transcript <url> [names or terms]`.

A typical session:

```
> Сделай текст из этого видео: https://www.youtube.com/watch?v=XXXXXXXXXXX

  ⏺ prepare: metadata, audio, local transcription — 3,045 words, 12 chapters, 3 chunks (19 s)
  ⏺ 3 transcript-editor agents edit the chunks in parallel
  ⏺ finalize: 3/3 chunks pass QA (99–100% length, 0.96 similarity)

  Done: youtube-transcripts/2026-10-06 - … [XXXXXXXXXXX]/transcript.md
  22:50 · 3,038 words · 12 chapters. Unsure fragments: two short lines in noisy crosstalk at 04:12.
```

What the editors fix, on a made-up sentence. Raw Parakeet output:

```
сегодня мы посмотрим как клод код работает с давинчи резолв через эмсипи сервер и ну это работает
```

After editing, with the glossary taken from the video's tags:

```
Сегодня мы посмотрим, как Claude Code работает с DaVinci Resolve через MCP-сервер, и — ну — это работает.
```

Meaning, word order and the speaker's style stay as they were. Only punctuation, capitalization, misheard names and paragraph breaks change.

Results go to `./youtube-transcripts/` in the current directory unless you name another place. Useful things to tell Claude:

| Say | Effect (script option) |
|---|---|
| where to save | `--out-dir` |
| speaker names, product names, jargon | `--context` (goes into the editors' glossary) |
| "keep the audio" | `--keep-audio` (by default the FLAC is deleted after transcription) |
| "redo the transcription" | `--force` |
| private / age-restricted / members-only video | `--cookies-from-browser chrome` |

## Platform support

| Component | macOS | Windows | Linux |
|---|---|---|---|
| Claude Code | ✓ | ✓ (native or WSL) | ✓ |
| yt-dlp, ffmpeg, deno, Python 3.9+ | ✓ | ✓ | ✓ |
| Spokenly with local Parakeet/Whisper models and file transcription | ✓ | ✓ (Windows 10/11, x64) | ✓ (x86_64 only) |
| Spokenly CLI and local server (`spokenly transcribe`, `localhost:51089`), which the plugin drives | ✓ (direct-download build) | not documented | not documented |
| **This plugin** | **✓ tested on Apple Silicon**; Intel should work (Spokenly is a Universal app), not tested | ✗ not yet | ✗ not yet |

Everything except the automated transcription step already runs on all three systems. Spokenly itself transcribes files with the same Parakeet model on Windows and Linux, but only from its window. As of October 2026, its documentation describes the CLI and the local server only for the macOS direct-download build, so there is nothing for a script to call on those systems.

A port needs two things:

1. **A scriptable speech recognizer** behind the script's `transcribe()` function. That could be Spokenly's CLI if it ships for those systems, or another local engine: candidates are Parakeet through NVIDIA NeMo or an ONNX runtime such as sherpa-onnx, or Whisper through whisper.cpp or faster-whisper.
2. **Small script changes:**
   - The script launches Spokenly with the macOS `open` command.
   - `doctor` reads the app version and the selected model from macOS-specific places.
   - The skill calls `python3`, which on Windows is usually `python` or `py`.

## Privacy

- Audio is downloaded from the video site to your Mac. With Spokenly in local-only mode it is transcribed on-device and never uploaded.
- The transcript **text** is processed by Claude, through your Claude Code session and its editor subagents, like any other text you work on with Claude.
- Nothing else leaves your machine. The plugin has no telemetry and no servers.

## Troubleshooting

See [skills/youtube-transcript/references/troubleshooting.md](skills/youtube-transcript/references/troubleshooting.md). The most common fix is `brew upgrade yt-dlp`.

## Credits

[yt-dlp](https://github.com/yt-dlp/yt-dlp) · [Spokenly](https://spokenly.app) · [NVIDIA Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) · [FFmpeg](https://ffmpeg.org) · [Deno](https://deno.com)
