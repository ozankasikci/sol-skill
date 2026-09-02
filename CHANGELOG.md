# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.8.1] — 2026-09-02

### Fixed

- **An `--in-place` run could not be relaunched, resumed or cleaned up.** Only
  the launch knew about `--in-place`; every path after it recomputed the
  workspace as `$WORKTREE_ROOT/<slug>`, a directory an in-place run never
  creates. The stall relaunch therefore ran `codex exec -C <nonexistent>`, died
  in about two seconds with a bare "No such file or directory", and recorded
  `failed-launch`, exit 1, elapsed 2 — a stall recovery that reads as a broken
  invocation, on precisely the runs the watchdog exists to save. Seen in
  production: an `xhigh` worker stalled at 900s and its `high` retry never ran.
  `--resume` failed the same way (the wrapper's `cd` fell straight through to
  its `exit 2`), and `--cleanup` pointed at a worktree that never existed.

  In-place mode is now remembered in the run directory rather than inferred from
  the command line. Each worker's workspace is written to
  `<run-dir>/workers/<slug>/worktree` at launch, next to `brief`, and read back
  instead of recomputed; `<run-dir>/in-place` marks the run itself, so `--wait`,
  `--resume` and `--cleanup` re-attach correctly without the caller retyping
  `--in-place` — which nothing in the output ever prompted for.

- The stall relaunch and `--resume` now check the workspace exists before
  launching and say `workspace <path> missing` with the slug, rather than
  leaving codex to fail with an opaque OS error two seconds later.

- `--cleanup` on an in-place run prints one line saying there are no worktrees
  or branches to remove and exits 0 without touching anything. The workspace
  there is the user's own checkout, which may itself be a linked worktree, so a
  path that reached `git worktree remove` would have deleted it.

## [1.8.0] — 2026-08-23

### Added

- `SOL_SANDBOX` (default `workspace-write`) sets the `-s` policy for every codex
  invocation. It exists because `SOL_CODEX_CONFIG` structurally cannot reach the
  sandbox mode: an explicit `-s` flag beats `-c sandbox_mode=`, so setting it
  through the config channel is silently ignored — verified with both on one
  command, which still failed. Any non-default value warns on stderr, since
  `danger-full-access` removes all confinement rather than one restriction.
- SKILL.md now covers Docker specifically. Its daemon socket is a unix socket
  connect, which neither `network_access` nor `writable_roots` unblocks —
  verified, both leave `docker ps` at `connect: operation not permitted`.
  Verified end to end through the launcher: the same brief gets permission
  denied under the default sandbox and lists real containers under
  `SOL_SANDBOX=danger-full-access`.

## [1.7.0] — 2026-08-23

### Added

- `SOL_CODEX_CONFIG`: extra `-c key=value` overrides, space separated, applied
  to all three codex invocations the launcher makes (launch, stall relaunch,
  and resume). Unset by default — relaxing the sandbox is the caller's call per
  repo, never the script's. Verified end to end against real codex: the same
  probe brief reports HTTP `000` without it and `200` with
  `sandbox_workspace_write.network_access=true`.
- SKILL.md **Sandboxed toolchains**: how to recognise a repo whose build cannot
  run under `workspace-write` at all, and why it matters more than it looks — a
  worker that cannot compile verifies by grep instead, reports green on grep
  evidence, and silently turns the role split into "Sol implements blind."
  Covers the worktree case specifically: a worktree's git dir lives outside the
  write root, so `git commit` fails deterministically, not intermittently.

## [1.6.0] — 2026-08-19

### Added

- Reference images for workers. A brief may carry a sidecar,
  `<run-dir>/tasks/NN-<slug>.images`, one path per line with `#` comments; each
  entry resolves against the repo root and is passed to that worker as
  `codex exec -i`. A path that does not resolve fails the run at preflight,
  before any worktree exists. Verified end to end against real codex.
- `--in-place`: run a single brief in the repo itself — no worktree, no branch,
  no commit, changes left in the working tree exactly as a bare `codex exec`
  leaves them — but supervised by the inactivity watchdog, with stall retries,
  a `summary.json` status and a real exit code.

### Changed

- **Phase 2 now launches through the script in single-worker mode too.** The
  skill used to end its stall guidance with "parallel mode does all of this
  automatically; single-worker mode is your job", which is the one thing this
  project has repeatedly established does not work: a flow depending on the
  model reliably doing something degrades to silence when it doesn't, and that
  is what the 1.2.0 revert was about.

  It cost hours in practice. A visual brief could not use the launcher, because
  workers could not take reference images, so the run went out on the
  unsupervised path and hung at `xhigh` with `thread.started`, `turn.started`
  and 101 bytes — silent for nearly three hours. `SOL_FIRST_EVENT_TIMEOUT` would
  have killed and retried it in fifteen minutes; it was simply unreachable from
  where the work had to run. The images sidecar removed that reason, and
  `--in-place` removes the remaining one, so the unsupervised path is now an
  explicitly-documented fallback rather than the default.

## [1.5.0] — 2026-08-19

### Added

- Image input. The brief command can now take `-i <file>` (repeatable) to attach
  screenshots, mockups, or a failing chart, and the brief template shows how to
  reference the attachment from `<task>`. Verified against a real run: Sol reads
  the image and describes its actual content.
- Guidance for asset-producing briefs. Codex ships a built-in `image_gen` tool —
  a model-side tool, invisible in `codex exec --help`, which is why this went
  unnoticed — so a brief may ask for a raster asset and get AI-generated pixels.
  Sol routes to code on its own when the visual is code-native: asked for a solid
  red circle it rendered with ImageMagick and said so; asked for a painterly
  title screen it used `image_gen`. Acceptance criteria for an asset cannot be a
  test command, so the template shows the checkable form instead.

### Changed

- Phase 3 now covers binary artifacts. `git diff` reports only `Binary files
  differ`, so an image is reviewed by opening it and judging its content against
  the brief — the review phase exists precisely so the model that produced the
  artifact does not certify it, and that argument does not stop applying at the
  file-format boundary.

### Fixed

- Phase 2 said a silent in-flight command was backstopped by "the 30-minute
  absolute cap", which 1.4.1 turned off by default. `SOL_COMMAND_TIMEOUT` is the
  backstop now, and the text says so.

## [1.4.2] — 2026-08-17

### Fixed

- **The inactivity watchdog never worked on Linux.** `mtime_of` tried BSD
  `stat -f %m` first and fell back to GNU `stat -c %Y`, but GNU's `-f` means
  `--file-system`: it prints a filesystem dump and **exits 0**, so the fallback
  never fired. The dump then reached `[ "$last" -gt 0 ]` as a syntax error, and
  every stall check — first-event, idle, and the new command budget — was dead
  on every Linux host from 1.4.0 onward. macOS was unaffected, which is why the
  local suite passed while CI failed. `mtime_of` now tries GNU first and
  validates the result is numeric before returning it.

  This mattered most on 1.4.1, which turned the absolute cap off and left Linux
  workers with no watchdog at all. Linux users on 1.4.0 or 1.4.1 should upgrade.

- A unit test now asserts `mtime_of` returns an epoch. The behavioural stall
  tests only exercise the platform they run on, so a BSD-first ordering passed
  every macOS run while being broken everywhere else; this asserts the contract
  itself, so either platform catches a regression.

## [1.4.1] — 2026-08-17

### Changed

- The absolute per-worker cap `SOL_WORKER_TIMEOUT` is **off by default** (was
  1800s). It asked a question nobody can answer in advance — "has this taken too
  long?" — which requires knowing how long a task should take, the very thing you
  delegate because you don't know. Any constant is too short for a scaffolding
  brief and too long for a wedged one, so tuning it only trades false kills
  against slower detection. Observed in the wild: a worker with 25 events and a
  just-completed command execution, killed mid-task purely for elapsed time. The
  inactivity watchdog added in 1.4.0 asks the answerable question instead — has
  this stopped producing evidence of progress? — and is now the only automatic
  killer. Set `SOL_WORKER_TIMEOUT` to a positive number if you want a hard budget.

### Added

- `SOL_COMMAND_TIMEOUT` (default 1800s) bounds the in-flight-command exemption.
  1.4.0 exempted a running command from the idle budget so a quiet 15-minute
  build would not be mistaken for a hang, which left a command that never returns
  catchable only by the absolute cap. With that cap now off by default, this
  budget closes the hole: a single command's runtime is predictable in a way a
  whole task's is not. Such a kill classifies `stalled`, not `timed-out`, so it
  feeds the existing effort-downgrade retry ladder.

### Fixed

- A worker killed by an opted-in `SOL_WORKER_TIMEOUT` before emitting its first
  event was classified `failed-launch` rather than `timed-out`, because the
  empty-event-log rung was evaluated before the exit-code rung. `failed-launch`
  is documented as a bad invocation or a missing binary, so the report sent the
  reviewer after the wrong thing. Exit code 124 is now classified alongside 125
  ahead of that rung, as stall kills already were.

## [1.4.0] — 2026-08-16

### Added

- Inactivity watchdog for parallel workers. The absolute `SOL_WORKER_TIMEOUT`
  could not tell a worker deep in silent reasoning from one that hung after
  `turn.started` and would never speak again — a known codex failure shape at
  high reasoning effort (openai/codex#24260, #23807) that burned a full
  45-minute budget doing nothing. The wait loop now uses the worker's
  `events.jsonl` as a heartbeat with two budgets: `SOL_FIRST_EVENT_TIMEOUT`
  (default 900s) until the first substantive event, and `SOL_IDLE_TIMEOUT`
  (default 600s) between events thereafter. A tripped watchdog kills the
  worker's process group and records the new `stalled` status (exit code 125)
  with a `stall-reason`, distinct from `timed-out`. Silence while a command
  execution or MCP tool call is in flight (`item.started` with no matching
  `item.completed`) is exempt from the idle budget — codex emits nothing while
  a command runs, so that silence is a build in progress, not a hang; a command
  hung forever falls to the absolute cap as `timed-out`.
- Stall retry ladder. A stalled worker is relaunched with the same brief in the
  same worktree — fresh session, one reasoning-effort step lower (`xhigh → high
  → medium → low`) — up to `SOL_STALL_RETRIES` times (default 1). Prior
  attempts' logs are archived as `*-attempt-N` files; `summary.json` gains
  `effort_used`, `stall_retries`, and `stall_reason` per worker.
- Effort guidance in SKILL.md: `high` for mechanical briefs (moves,
  scaffolding, renames), `xhigh` only for algorithmically hard ones; and a
  single-worker instruction to treat a silent event log as a stall, not
  progress.

### Fixed

- `codex exec resume` in parallel mode now redirects stdin from `/dev/null`;
  codex hangs forever on an open pipe stdin with no writer (openai/codex#20919).

## [1.3.0] — 2026-08-10

### Added

- Parallel workers, opt-in per run: `/sol --workers N` runs N independent tasks at
  once, one `codex exec` session and one git worktree per task. Claude splits the
  request, shows the split with each task's file scope and waits for confirmation,
  then reviews each branch and cherry-picks the approved ones onto your branch — one
  commit per task.
- `skills/sol/scripts/sol-parallel.sh` owns everything mechanical: worktree creation
  and bootstrap, the fan-out launch, waiting, per-worker bookkeeping, `summary.json`,
  correction batches, and cleanup. The planner never composes a fan-out itself, which
  is what made the 1.1.0 background flow degrade to silence.
- `SOL_MAX_WORKERS` (default 3) and `SOL_WORKTREE_SETUP`. Asking for more workers than
  the ceiling is refused with a reason, never silently clamped.
- After integration the project's checks are re-run **once on the merged branch**, and
  that result leads the report. N green isolated runs are not a green integrated run.

### Changed

- Parallel mode requires a clean working tree (single-worker mode still only warns):
  every worker branches from `HEAD`, so a dirty `HEAD` puts changes nobody made into
  every worker's diff.
- `check-codex.sh` gains a `worktree_root` check.

### Fixed

- Corrections in parallel mode resume by explicit session id read from the worker's own
  event log, never `codex exec resume --last`, which with N sessions in flight would
  silently correct an arbitrary worker.

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

[1.3.0]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.3.0
[1.2.1]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.2.1
[1.2.0]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.2.0
[1.1.2]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.1.2
[1.1.1]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.1.1
[1.1.0]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.1.0
