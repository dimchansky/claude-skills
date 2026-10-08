# claude-skills

[![CI](https://github.com/dimchansky/claude-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/dimchansky/claude-skills/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

My [Claude Code](https://code.claude.com) plugins and skills, published as a plugin marketplace named **`dimchansky-skills`**.

| Plugin | What it does | Platform |
|---|---|---|
| [youtube-transcript](plugins/youtube-transcript) | Clean, proofread Markdown transcripts of YouTube videos and local audio/video. Speech recognition runs locally with Parakeet in Spokenly; Claude proofreads in parallel and checks the result | macOS only |

## Quick start

**1. Install** (details below):

```bash
claude plugin marketplace add dimchansky/claude-skills
claude plugin install youtube-transcript@dimchansky-skills
```

**2. Check the requirements**: macOS, [Spokenly](https://spokenly.app) with the Parakeet model (`brew install --cask spokenly`), and `brew install yt-dlp ffmpeg deno`. Start Claude Code and ask:

```
> run the youtube-transcript doctor
```

**3. Use it.** Paste a link and say what you want, in any language:

```
> Сделай текст из этого видео: https://www.youtube.com/watch?v=XXXXXXXXXXX
> Transcribe https://youtu.be/XXXXXXXXXXX and save it to ~/Documents/talks
> What are the three main arguments in this talk? https://youtu.be/XXXXXXXXXXX
> Расшифруй ~/Downloads/interview.m4a, говорят Анна Петрова и Иван Сидоров
```

You can also call the skill explicitly: `/youtube-transcript:youtube-transcript <url> [speaker names, terms]`.

Claude downloads the audio, transcribes it locally in about 10 seconds, proofreads it with parallel editors, checks the result and tells you where the file is:

```
youtube-transcripts/2026-09-14 - Как мы собрали кофемашину своими руками [XXXXXXXXXXX]/
├── transcript.md       ← clean text with chapter headings that link to the video
├── transcript.raw.md   ← raw recognizer output (ready in ~20 s)
└── changes.md          ← every word the editors changed
```

`transcript.md` looks like this:

```markdown
# Как мы собрали кофемашину своими руками

- **Channel:** [Example Channel](https://www.youtube.com/@example)
- **Published:** 2026-09-14 · **Duration:** 18:42
- **Video:** https://www.youtube.com/watch?v=XXXXXXXXXXX
- **Transcript:** Spokenly · Parakeet TDT 0.6B v3 (local) · edited by Claude · 2,614 words

---

## Вступление · [00:00](https://youtu.be/XXXXXXXXXXX?t=0)

Всем привет! Сегодня расскажу, как мы за два выходных собрали кофемашину
из старого бойлера и Raspberry Pi, и что из этого получилось.

## Подбираем насос · [02:15](https://youtu.be/XXXXXXXXXXX?t=135)

…
```

For questions about a video ("what's the gist", "find the part about pricing"), Claude stops after the fast raw transcript and answers from it, without the editing step. More in the [plugin README](plugins/youtube-transcript/README.md#usage).

## Install

In a terminal:

```bash
claude plugin marketplace add dimchansky/claude-skills
claude plugin install youtube-transcript@dimchansky-skills
```

Or inside a Claude Code session:

```
/plugin marketplace add dimchansky/claude-skills
/plugin install youtube-transcript@dimchansky-skills
```

Then restart Claude Code, or run `/reload-plugins` in a session. Each plugin has its own requirements: for `youtube-transcript`, see its [README](plugins/youtube-transcript/README.md#requirements) and check them with `doctor`.

The repository is public, so no GitHub credentials are needed.

## Update

Plugins update when their `version` changes. To update by hand:

```bash
claude plugin marketplace update dimchansky-skills
claude plugin update youtube-transcript@dimchansky-skills
```

Then restart Claude Code.

To update automatically, open `/plugin` in Claude Code, go to **Marketplaces**, select `dimchansky-skills` and choose **Enable auto-update**. Claude Code then checks for new versions in the background when a session starts.

## Uninstall

```bash
claude plugin uninstall youtube-transcript@dimchansky-skills
claude plugin marketplace remove dimchansky-skills
```

## Repository layout

```
.claude-plugin/marketplace.json        marketplace catalog (name: dimchansky-skills)
plugins/
└── youtube-transcript/
    ├── .claude-plugin/plugin.json     plugin manifest; "version" drives updates
    ├── skills/youtube-transcript/     the skill: SKILL.md, scripts/, assets/, references/
    ├── agents/transcript-editor.md    Read/Write-only editor subagent the skill spawns
    └── README.md
tests/                                 unit tests (synthetic data, no network)
.github/workflows/ci.yml               tests on Python 3.9 and 3.13 + `claude plugin validate`
```

## Development

```bash
claude plugin validate .                              # marketplace catalog
claude plugin validate plugins/youtube-transcript     # plugin manifest and components
python3 -m unittest discover -s tests -v              # unit tests
```

To try local changes without publishing, disable the installed copy and load the working tree:

```bash
claude plugin disable youtube-transcript@dimchansky-skills
claude --plugin-dir ./plugins/youtube-transcript
```

When you're done, re-enable the installed copy with `claude plugin enable youtube-transcript@dimchansky-skills`.

**Releasing.** Installed copies only update when the plugin's `version` changes. For every release:

1. Bump `version` in the plugin's `.claude-plugin/plugin.json` (SemVer).
2. Add an entry to [CHANGELOG.md](CHANGELOG.md).
3. Commit, then tag `<plugin>--v<version>` (for example `youtube-transcript--v1.0.1`), push with tags, and create a GitHub release.

## License

[MIT](LICENSE) © Dmitrij Koniajev
