# Changelog

All notable changes to the plugins in this marketplace are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and each plugin is versioned with [Semantic Versioning](https://semver.org/).

## youtube-transcript

### [1.0.1] - 2026-10-07

#### Changed

- Documented the measured editing speed. The bundled `transcript-editor` edits a ~1,000-word chunk in ~22 s. A `general-purpose` fallback takes 3–4 minutes (~9× slower, with equal quality) because it inherits the session's thinking and effort settings. SKILL.md and the troubleshooting guide now say to run `/reload-plugins` when the bundled editor is missing right after installation.

### [1.0.0] - 2026-10-07

#### Added

- `prepare`: downloads the best audio with yt-dlp (3 retries; re-fetches incomplete metadata) and transcribes it locally with the Spokenly CLI, keeping word timestamps. It maps YouTube chapters to sentence boundaries, splits the text into balanced chunks for parallel editing, and writes self-contained editing tasks with a glossary built from the title, description, tags and chapters.
- `transcript.raw.md`: a readable raw transcript with chapter headings, available within seconds (fast path for summaries and Q&A).
- `transcript-editor` subagent: Read/Write only, Sonnet, with a prompt-injection guard. It edits in a single pass: a ~1,500-word chunk takes ~5 minutes instead of ~10, and benchmarks against Opus show no loss in quality.
- `finalize`: merges the edited chunks and restores chapter headings with timestamp links. It runs QA checks (length ratio, word similarity, headings, leftover `[Music]` markers) and writes `changes.md` with every word-level edit.
- `doctor`: checks macOS, Python, yt-dlp (including its age), ffmpeg, deno and Spokenly (version, build, server, CLI, selected model), and prints the fix for each problem.
- Local audio/video files as input, with `--context` for names and terms.
