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
- `--json` mode on the preflight: single machine-readable object with `ready`,
  `failures`, `warnings`, `model`, and per-check `{name, status, detail}`, for CI and
  for agents deciding whether delegation is available. Human output is unchanged and
  exit codes are shared between modes. Implemented via `/sol` itself — the run is
  documented in the README.
- Claude Code plugin manifest. Distribution goes through the existing
  [ozankasikci/claude-plugins](https://github.com/ozankasikci/claude-plugins) marketplace
  (`ozankasikci-plugins`) rather than a per-repo marketplace, so one
  `/plugin marketplace add` covers every plugin in the collection.

### Fixed

- Widened `allowed-tools` from `Bash(codex exec:*)` to `Bash` (plus `Write`). The
  narrow form contradicted the skill body: phase 2 writes the brief to a scratchpad
  file, and phase 3 requires the reviewer to run `git diff` and re-run the project's
  test/lint/typecheck commands itself — none of which are `codex exec` calls, so all
  of them were blocked.

[1.0.0]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.0.0

## [1.1.0] — 2026-07-29

### Added

- Live progress visibility for long runs. The run now emits a `codex exec --json`
  event stream and `skills/sol/scripts/sol-watch.py` turns it into milestone
  lines: opening plan, each file changed, each test/lint/typecheck command with
  its exit code, genuine errors, stalls, and a closing summary with token usage.
  Routine reads and greps are suppressed, keeping a long run to ~5-15 lines.
  Works as a harness Monitor or standalone in a terminal.
- Test suite for the watcher (43 checks) over one captured real event stream and
  two synthetic fixtures, wired into CI.

### Changed

- Implementation and correction runs are backgrounded by default. A foreground
  call blocks the planner from reporting anything, which is what made runs feel
  opaque.
- stderr now goes to `sol-stderr.txt` instead of being folded into stdout with
  `2>&1`, which would corrupt the JSONL stream.
- `sol-log.txt` is now `sol-events.jsonl` for implementation and correction runs.
  Research mode keeps a plain-text log.

### Fixed

- codex reports some informational notices as `error` items — the "skill
  descriptions were shortened" one fires on every single run. Left unfiltered
  that meant a false ERROR notification every time, so the feed would stop being
  worth reading. Filtered by a narrow explicit pattern; anything unmatched is
  still treated as a real error.
