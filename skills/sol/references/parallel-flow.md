# Parallel flow

Followed instead of phases 2–5 when `SKILL.md`'s parallel routing rule fires. Runs N
`codex exec` workers concurrently via `scripts/sol-parallel.sh`, each in its own git
worktree on branch `sol/<slug>`, then integrates and re-verifies the result on one
merged branch. Phase 1 (plan) still applies unchanged; this replaces phases 2–5 with
the twelve steps below.

## 1. When this applies

Only when the user names a worker count: `--workers N`, or plain language like "use 3
workers". Never inferred from a request that merely looks like several independent
tasks — the trigger is a number the user typed, not a judgment about the work. If the
user says `--workers 1` (or "use 1 worker"), that is *not* parallel mode: follow
`SKILL.md` phases 1–5 as normal.

## 2. Resolve the ceiling

The ceiling is `SOL_MAX_WORKERS` if set, else 3 — this is `sol-parallel.sh`'s own
default and it is a hard cap, not a suggestion. If the user asks for more workers than
the ceiling, refuse in one line naming both numbers, e.g.: "You asked for 5 workers;
the ceiling is 3 (`SOL_MAX_WORKERS`). Raise `SOL_MAX_WORKERS` or ask for fewer." The
script itself refuses the same way (exit 2) if you launch anyway — do not silently
clamp the requested count down to the ceiling and proceed.

If there are more tasks than the ceiling, run waves: one `sol-parallel.sh` invocation
per wave. Write each new wave's briefs only after integrating and re-verifying the
previous wave (steps 7–10), so the new wave branches from that merged, checked result
— never from the stale pre-integration base.

## 3. Split and confirm

Print the numbered split before writing anything: task number, slug, one-line goal,
expected file scope. Call out any overlap between two tasks' file scopes explicitly and
propose either merging them into one task or moving one to a later wave. **Wait for the
user's confirmation before writing briefs or invoking the script** — this split is a
plan the user hasn't seen yet, and deserves the same confirmation bar as any other plan.

## 4. Write briefs

One brief per task at `<run-dir>/tasks/NN-<slug>.md` (two-digit `NN`, lowercase-hyphen
`<slug>`), using the "Parallel brief additions" in `brief-template.md` layered on the
normal Implementation brief. `<run-dir>` is a scratch directory you choose, e.g.
`$SCRATCHPAD/sol-run` — create `<run-dir>/tasks/` yourself before writing into it.

`sol-parallel.sh` derives each worker's branch name from this filename: it strips a
leading `NN-`, lowercases, and collapses every character that isn't `a-z0-9` to `-`.
Whatever slug you announced in the split, use the same text in the filename, or the
resulting branch name (`sol/<slug>`) won't match what you told the user.

## 5. Launch

Checkpoint first: `sol-parallel.sh` refuses to launch against a dirty working tree
(every worker worktrees off `HEAD`, so uncommitted changes on the current branch would
leak into all of them). Commit or stash before launching.

One blocking call:

```bash
bash <skill-dir>/scripts/sol-parallel.sh --workers N "$SCRATCHPAD/sol-run"
```

This creates one git worktree and branch (`sol/<slug>`) per brief under
`../.sol-worktrees/<repo-name>/<slug>` (a sibling of the repo, not inside it), runs
`SOL_WORKTREE_SETUP` in each fresh worktree if set, launches one `codex exec` per
worker, and **blocks until every worker finishes or is force-killed** by the
inactivity watchdog — so this single call can run far longer than one tool-call budget.
There is deliberately no default cap on how long a worker may take: a task's duration is
not predictable, so any constant kills productive workers. What is bounded is silence.

If the tool call itself times out before the script returns, the workers are still
running unattended — that is not a failure, just an interrupted wait. Re-attach:

```bash
bash <skill-dir>/scripts/sol-parallel.sh --wait "$SCRATCHPAD/sol-run"
```

`--wait` exits 75 while any worker is still running, 0 once all finished cleanly, 1 if
at least one worker landed in a non-`ok`/`no-changes` state. Keep re-invoking `--wait`
until it stops returning 75. **Never launch `codex` directly in parallel mode** — the
script owns branch, worktree, and session bookkeeping that a hand-rolled command would
break.

(Optional: `sol-parallel.sh --dry-run <run-dir>` creates and bootstraps the worktrees —
including running `SOL_WORKTREE_SETUP` — and stops before launching any codex session,
if you want to sanity-check the setup before spending a run.)

## 6. Read `summary.json`, never the raw logs

Once the launch call (or the last `--wait`) returns 0 or 1, read
`<run-dir>/summary.json` — never `events.jsonl` or `report.md` directly except as
pointed to below. It has one entry per worker in its `workers` array: `slug`, `branch`
(`sol/<slug>`), `worktree` (absolute path), `status`, `exit_code`, `session_id`,
`commit`, `files_changed`, `elapsed_seconds`, `effort_used`, `stall_retries`,
`stall_reason`, and the paths to that worker's `report.md` / `events.jsonl` /
`stderr.txt`.

`status` is one of: `ok` · `no-changes` · `failed-launch` · `failed-run` ·
`failed-setup` · `failed-commit` · `timed-out` · `stalled`.

- `failed-launch` means codex itself never produced an event (bad invocation, binary
  missing) — read the *tail* of that worker's `stderr.txt` (path from `summary.json`),
  not the whole file.
- `failed-setup` means `SOL_WORKTREE_SETUP` failed before codex ever ran — check
  `<run-dir>/workers/<slug>/setup.log`.
- `stalled` means the inactivity watchdog killed the worker: no substantive event
  within `SOL_FIRST_EVENT_TIMEOUT` of launch, or no event at all for
  `SOL_IDLE_TIMEOUT` after work had started (`stall_reason` says which). Silence
  while a command execution or MCP tool call is in flight (an `item.started`
  with no matching `item.completed`) never counts against the idle budget — a
  quiet 15-minute build is not a stall. That exemption is bounded by
  `SOL_COMMAND_TIMEOUT` (default 1800s), so a command that never returns is still
  caught, as a stall rather than a timeout. `timed-out` appears only if you opt
  into an absolute cap with `SOL_WORKER_TIMEOUT`, which is off by default. The
  launcher already retried it `SOL_STALL_RETRIES` times, each attempt a fresh
  session one reasoning-effort step lower — `effort_used` and `stall_retries`
  record what happened, and each prior attempt's logs are archived as
  `events-attempt-N.jsonl` beside the final ones. A worker still `stalled` in the
  summary exhausted its retries: report it to the user rather than relaunching by
  hand, and note that a retried worker's worktree keeps whatever the stalled
  attempt had already edited (briefs describe an end state, so the retry builds on
  it).

`files_changed` is the branch's whole diff since `base_sha` for an `ok` worker. For a
non-`ok` worker it is a snapshot of everything its worktree holds that the base does
not — committed, staged, unstaged, or untracked — because a `failed-commit`,
`failed-run`, `timed-out`, or `stalled` worker can leave real work sitting there uncommitted, and
this is the only place the report tells the user where to find it. An empty list for a
non-`ok` worker means that worker genuinely produced nothing.

Every non-`ok` worker is named in the final report with its status — a task that
produced nothing (`no-changes`) is reported as such, never silently omitted.

## 7. Review in task order

For each `ok` worker, in the order the tasks were assigned:

```bash
git diff --stat <base_sha>..sol/<slug>
```

(`<base_sha>` is `summary.json`'s `base_sha`.) Then read the full branch diff. Hold it
to the same standard as the single-worker flow's phase 3 — correctness against the
brief's acceptance criteria, regressions, edge cases, security, missing tests,
out-of-scope changes.

Additionally check the worker stayed inside the file scope its brief declared —
compare against that worker's `files_changed` in `summary.json`.

## 8. Integrate one branch at a time

From the repo root, on the base branch (`summary.json`'s `base_branch` — check it out
first if you're not already on it), once a worker passes review:

```bash
git cherry-pick sol/<slug>
```

This lands the worker's patch as a *new* commit on the base — not a merge — so each
worker's branch still shows its own full diff independently.

Before moving to the next worker, catch cross-worker conflicts early by rebasing every
not-yet-integrated worker onto the base you just advanced:

```bash
git -C <worktree> rebase <base-branch>
```

(`<worktree>` from that worker's `summary.json` entry.) A conflict here is evidence the
"these tasks are independent" premise was wrong for that pair — report it to the user;
never resolve it yourself by picking one side.

## 9. Correct

Write the delta — same three parts as a single-worker correction (where, what's wrong
and required, what check must pass), not a restatement of the brief — to
`<run-dir>/workers/<slug>/correction.md`, then:

```bash
bash <skill-dir>/scripts/sol-parallel.sh --resume "$SCRATCHPAD/sol-run"
```

This resumes every worker that currently has a `correction.md` waiting, each **by its
own recorded session id** — never `--last`. With N sessions in flight, `--last` resumes
whichever session codex last touched, which may not be the worker you meant to correct,
silently applying your fix to the wrong worker's branch. `codex exec resume` also
accepts no `-C`, `-s`, or `--color` (verified against codex-cli 0.144.6); the script
already handles this. **Never construct a `codex exec resume` command by hand in
parallel mode.**

`--resume` blocks the same way `--workers` does in step 5 — if the tool call times out
before it returns, re-attach with the same `--wait "$SCRATCHPAD/sol-run"` call, then
re-read `summary.json` once it exits 0 or 1.

Each round's `correction.md` is renamed to `correction-1.md`, `correction-2.md`, ... as
it's consumed, so rounds are visible on disk per worker. Track rounds per worker
independently and stop after 2 rounds for that worker, same ceiling as single-worker
mode — after 2 rounds, stop and report the remaining issue to the user instead of
looping. Re-review after each round (back to step 7 for that worker).

## 10. Re-run the checks on the merged branch

Mandatory, once, after every worker in this wave is integrated: run the project's
actual test/lint/typecheck commands yourself, on the now-merged base branch. **This
result leads the report; per-worker results are supporting detail only.** Each worker
was verified green in isolation, against a base that did not contain the other
workers' changes — green × N is not green combined. Reporting N isolated green runs as
if they were a combined green run reintroduces, one level up, exactly the
self-assessment problem this skill exists to eliminate.

## 11. Clean up

```bash
bash <skill-dir>/scripts/sol-parallel.sh --cleanup "$SCRATCHPAD/sol-run"
```

Removes the worktree and branch for every worker whose status was `ok`/`no-changes`,
whose worktree is clean, and whose branch is fully integrated into the base (checked by
patch equivalence via `git cherry`, not ancestry — cherry-pick creates new commits, so
ancestry alone would miss genuinely-integrated work). Everything else is printed as a
`kept: sol/<slug> <path> (<reason>)` line. Name every one of them in your report —
nothing a worker produced is ever silently discarded.

The integration check fails closed: if it cannot get a trustworthy answer — the base
branch was renamed or deleted after the run, or `git cherry` itself errors — nothing is
removed, every worker is printed as `kept:`, and the reason is written to stderr. A
`--cleanup` that removes nothing and warns about the base ref is that guard firing, not
a no-op; re-point or restore the base branch and run it again.

## 12. Report

- **Per task**: title, branch (`sol/<slug>`), verdict, `git diff --stat` output with
  one clause per file, correction rounds used.
- **Combined**: the merged-branch check output from step 10, commits added to the base,
  wall clock for the run.
- **Survivors and risks**: every branch/worktree `--cleanup` kept and why (from its
  `kept:` line), plus anything unusual noticed across workers.
