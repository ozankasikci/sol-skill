# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-07-29

First public release.

### Added

- `/sol` skill: strict planner/implementer role split — Claude plans, briefs, reviews
  the diff and re-runs checks; GPT-5.6 Sol (`xhigh`) makes all code changes via Codex CLI.
- Five-phase flow with a hard cap of 2 correction rounds, and a fresh-eyes
  `codex exec review` pass for high-risk changes (auth, payments, migrations, concurrency).
- Research mode, opt-in only when the user names Sol explicitly, pinned to the
  `read-only` sandbox with source-attribution and injection-safety rules.
- `skills/sol/references/brief-template.md` — fill-in XML brief contract for
  implementation briefs, research briefs, and correction messages.
- `scripts/check-codex.sh` — read-only preflight for CLI presence, `codex exec`
  availability, authentication, model resolution, and git-tree cleanliness.
- Claude Code plugin and marketplace manifests for one-command install.

### Fixed

- Widened `allowed-tools` from `Bash(codex exec:*)` to `Bash` (plus `Write`). The
  narrow form contradicted the skill body: phase 2 writes the brief to a scratchpad
  file, and phase 3 requires the reviewer to run `git diff` and re-run the project's
  test/lint/typecheck commands itself — none of which are `codex exec` calls, so all
  of them were blocked.

[1.0.0]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.0.0
