# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] — 2026-08-05

### Fixed

- The phase-4 correction command passed `-s workspace-write` and `--color never` to
  `codex exec resume`, which rejects both at parse time (the sandbox is inherited
  from the resumed session). With stderr redirected, the rejection was invisible:
  codex exited 2 instantly, the events file stayed empty, the tree stayed clean,
  and pre-existing green checks looked like a successful fix. Found in real use
  when a correction round silently no-opped. The command is fixed, and the skill
  now requires confirming the events file is non-empty after every codex launch
  before drawing any conclusion.

## [1.2.0] — 2026-08-05

### Changed

- Reverted the background-run + live-watcher flow introduced in 1.1.0, based on real
  use: when the planner armed the watcher through the wrong channel (a Bash
  background task instead of a streaming monitor), its output went to a file nobody
  read — leaving a "sol progress / no output" chip for the entire run. A flow that
  degrades to silent is worse than the simple one. Runs are foreground with a
  timeout again, as in 1.0.0.
- The watcher (`sol-watch.py`) stays, demoted to an optional summary tool: Claude
  uses `--once` to explain a failed or finished run instead of dumping raw JSON, and
  it works in a second terminal for anyone who wants a live view. All 1.1.x fixes
  (test-command detection, path relativization, benign-error filtering, named files
  in the summary) are retained.
- The run still writes the `--json` event log and split stderr, so post-hoc
  summaries work; the elaborated per-file report contract from 1.1.2 is unchanged.

## [1.1.2] — 2026-08-05

### Changed

- "N files changed, +355/-21" is a number, not a report. The watcher's closing
  summary now names the changed files (first 5, then "+N more"), and the skill's
  report phase requires a per-file breakdown — the `git diff --stat` table plus one
  clause per file on what changed in it — along with the checks' actual results and
  whether a commit was made.

## [1.1.1] — 2026-08-05

### Fixed

- The watcher suppressed the project's actual verification command when it wasn't a
  recognised runner. A real run whose brief said "run `python3 test_calc.py`" showed
  only the file-change line — the test result, the most important milestone, was
  silently dropped. Commands that look like test runs (`python3 test_*.py`,
  `./run_tests.sh`, `spec/`) now surface even off the runner list, while pure reads
  that merely mention a tests directory (`rg foo tests/`) stay suppressed.
- File paths in `file_change` events arrive absolute; they are now shown relative to
  the working directory. The refactor fixture now pins the real event shape
  (`changes: [{path, kind}]`) captured from codex-cli 0.144.6.

## [1.1.0] — 2026-08-04

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
- Moved `check-codex.sh` from the repo root into `skills/sol/scripts/`. `npx skills add`
  installs only the skill directory, so the preflight the README told those users to run
  was never actually reaching them. Everything the skill needs at runtime now lives under
  `skills/sol/`.

### Fixed

- codex reports some informational notices as `error` items — the "skill
  descriptions were shortened" one fires on every single run. Left unfiltered
  that meant a false ERROR notification every time, so the feed would stop being
  worth reading. Filtered by a narrow explicit pattern; anything unmatched is
  still treated as a real error.

## 1.0.0 — 2026-07-29

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

[1.2.1]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.2.1
[1.2.0]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.2.0
[1.1.2]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.1.2
[1.1.1]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.1.1
[1.1.0]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.1.0
