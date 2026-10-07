# Troubleshooting

Start with the environment check. It tests every dependency and prints the fix for each problem:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/yt_transcript.py doctor
```

Then read the error printed by `prepare` or `finalize`: it names the failing tool and usually the fix.

## yt-dlp (download / metadata)

| Symptom | Fix |
|---|---|
| `HTTP Error 403: Forbidden` | Often transient: the script already retried 3 times, so run `prepare` once more. If it persists, **update yt-dlp**: `brew upgrade yt-dlp`. YouTube changes constantly and an old yt-dlp is the most common cause. `doctor` warns when yt-dlp is more than 60 days old. |
| `Requested format is not available`, empty format list | Update yt-dlp (as above). The script already falls back from `ba` (best audio) to `b` (best combined). |
| Warning about a missing JavaScript runtime, or formats missing | yt-dlp needs a JavaScript runtime for YouTube: `brew install deno`. |
| `Sign in to confirm you're not a bot`, age-restricted, members-only, private | `prepare … --cookies-from-browser chrome` (or `safari`, `firefox`). This uses the user's own browser session, so mention it to them. |
| Still failing after an update | Search the exact error at https://github.com/yt-dlp/yt-dlp/issues. Fixes sometimes land in nightly builds before a Homebrew release. |
| Playlist URL | Run `prepare` per video; list them with `yt-dlp --flat-playlist --print url "<playlist-url>"`. |
| Live stream / premiere | It can only be transcribed after the broadcast has ended and YouTube has processed it. |

Updating tools changes the user's system, so ask before running `brew upgrade` or `brew install` yourself.

## Spokenly (transcription)

| Symptom | Fix |
|---|---|
| `Spokenly.app is not installed` | Install it from https://spokenly.app. It must be version 2.29+ and the direct-download build: the App Store build has no local server or CLI. |
| `nothing listens on localhost:51089` | Open Spokenly.app manually and make sure it has finished starting (and isn't stuck in onboarding), then retry. |
| `Spokenly CLI not found` | The app writes `~/Library/Application Support/Spokenly/spokenly` when it starts. Update Spokenly and launch it once. |
| Warning: model is not Parakeet | The file-transcription model in Spokenly's settings isn't Parakeet. Tell the user. Parakeet TDT 0.6B v3 is the intended model for the 25 European languages it supports. |
| Warning: language not supported by Parakeet v3 | Parakeet v3 covers bg cs da de el en es et fi fr hr hu it lt lv mt nl pl pt ro ru sk sl sv uk. For anything else (ja, zh, ko, ar, hi, tr, …) ask the user to pick a Whisper model for file transcription in Spokenly, then run `prepare --force`. |
| Recurring misrecognized name across many videos | Spokenly's word replacements apply to every transcription: `"$HOME/Library/Application Support/Spokenly/spokenly" replacements add "<heard>" "<correct>"`. This changes the user's global Spokenly settings, so only do it at their request. |

Manual check: `"$HOME/Library/Application Support/Spokenly/spokenly" transcribe /path/audio.flac --format text`.

The CLI waits up to 10 minutes per file. Parakeet runs ~100–150× faster than real time on Apple Silicon (a 23-minute video takes about 10 s), so only absurdly long files would hit that limit.

## Editing (subagents and finalize)

| Symptom | Fix |
|---|---|
| `Agent type 'youtube-transcript:transcript-editor' not found` | The plugin was installed or updated during this session. Use `general-purpose` with `model: "sonnet"` and the same prompt, and tell the user that `/reload-plugins` or a restart brings back the faster editor. |
| Editors take minutes per chunk | They are probably `general-purpose` agents, which inherit the session's extended thinking and effort and spend minutes reasoning before writing. In tests: 190 s vs 22 s for the bundled `transcript-editor`. Use the bundled editor (after `/reload-plugins` if needed). Also check that `chunks/NN.task.md` still has the "Work efficiently" section (re-run `prepare` after editing the template), and pass a smaller `--chunk-words` if you need more parallelism. |
| `finalize`: edited chunk(s) missing | That editor failed or wrote elsewhere. Spawn it again for that task file. |
| Length ratio / similarity warning | Delete `chunks/NN.edited.md`, re-run that editor once, and finalize again. If it's flagged again, look at `changes.md` for that chunk and tell the user. |
| Heading count mismatch | Re-run the chunk, or restore the `## …` line in `NN.edited.md` by hand (any text works: `finalize` rewrites headings in order). |
| Editor "fixed" a correct word | Add the right spelling via `prepare --context "…"` and re-run `prepare`. Task files are regenerated; existing edits are kept only if the chunk plan didn't change. Then re-run the affected chunks. |
