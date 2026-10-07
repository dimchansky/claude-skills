# Working on this repository

This repository is a Claude Code plugin marketplace named `dimchansky-skills`. Users install from it with `claude plugin marketplace add dimchansky/claude-skills`, then `claude plugin install <plugin>@dimchansky-skills`. Read [README.md](README.md) for the layout.

## Rules

- **Bump the version on every user-visible change to a plugin.** Installed copies update only when `version` in `plugins/<plugin>/.claude-plugin/plugin.json` changes. Use SemVer: a fix is a patch, a new option or behavior is a minor, a breaking change is a major. Add a matching entry to `CHANGELOG.md`. Keep `version` only in `plugin.json`, never in `marketplace.json`.
- **Paths inside plugins:** reference bundled files from SKILL.md and agents with `${CLAUDE_SKILL_DIR}` or `${CLAUDE_PLUGIN_ROOT}`. Never write `~/.claude/...` or other machine-specific paths: plugins run from `~/.claude/plugins/cache/...`.
- **Scripts:** Python 3.9+ standard library only, since `/usr/bin/python3` on macOS is 3.9. No pip dependencies.
- **Copyright:** never commit media, transcripts, `info.json` or other material from real videos. Tests use synthetic data.
- **Names:** plugin names must not start with `claude-` (`claude plugin validate` rejects it). The marketplace name `dimchansky-skills` must not change, because users' installs depend on it.
- **Skill descriptions:** keep the `description` in SKILL.md under 1,536 characters, and put "when to use" triggers there, not in the body.

## Before committing

```bash
claude plugin validate .
claude plugin validate plugins/youtube-transcript
python3 -m unittest discover -s tests -v
/usr/bin/python3 -m unittest discover -s tests -v   # the oldest supported Python
```

## Releasing

1. Bump `version` in `plugin.json` and update `CHANGELOG.md`.
2. Commit, then tag: `git tag youtube-transcript--v<version>`.
3. `git push --follow-tags`.
4. `gh release create youtube-transcript--v<version> --title "youtube-transcript v<version>" --notes "<changelog entry>"`.
