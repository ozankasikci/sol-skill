# Parallel Sol workers — design

**Date:** 2026-08-10
**Status:** Approved, ready for implementation planning
**Target release:** 1.3.0

## Goal

Let `/sol` run several independent implementation tasks concurrently, each in its own
Codex session and its own git worktree, with Claude reviewing and integrating the
results one branch at a time.

Parallelism is **opt-in per run**. The default path — one task, one worker, in the
user's working tree — is unchanged.

## Non-goals

- **No task decomposition of a single cohesive task.** Splitting one task into N slices
  by file scope is a different feature with a different risk profile (bad splits produce
  merge conflicts that cost more than the parallelism saves). Out of scope.
- **No best-of-N racing.** Running N workers on the identical brief and judging the
  candidates is also out of scope.
- **No auto-detection of parallelism.** Claude never infers that a request "looks like
  three tasks". See Routing.
- **No background/polling run mode.** Reverted in 1.2.0; not reintroduced. See §3.
- **No changes to Research mode.**

---

## 1. Routing and configuration

### The opt-in rule

Parallel mode engages **only when the user names a worker count** — either the
`--workers N` flag or an explicit natural-language request ("use 3 workers", "run these
with 3 workers"). Absent that, `/sol` behaves exactly as it does today: one task, one
worker, changes land in the user's working tree.

This is deliberate. The 1.2.0 revert was caused by the planner model choosing wrong
between two similar-looking launch mechanisms, and a flow that degrades to silence when
the model picks wrong is worse than the simpler flow it replaced. "Did the user name a
number" is a fact the model reads off the input, not a judgment it forms — so the
discriminator cannot misfire.

`--workers 1` is legal and routes to the normal single-worker flow.

### Precedence and the ceiling

```
--workers N   (per run)   >   SOL_MAX_WORKERS   (shell env)   >   3   (built-in)
```

The resolved value is a **ceiling on what may be requested**, not a default worker
count. The default worker count is always 1.

If the user asks for more workers than the ceiling allows, `/sol` **refuses with a
one-line reason naming the ceiling and how to raise it**. It does not silently clamp.
A silent clamp means the user believes five tasks are in flight when three are, and
discovers the difference from a report that is missing two tasks.

If the task count exceeds the ceiling, tasks run in **waves** of at most `ceiling`
workers. Each wave branches from the previous wave's *merged* result, so later tasks
build on earlier integrated work.

### Configuration surface

Exactly two environment variables and one flag. The README's "there is no config file"
claim is amended to name these, since editing `SKILL.md` does not survive a skill
update.

| Setting | Default | Meaning |
|---|---|---|
| `--workers N` | — | Worker ceiling for this run. Presence of this flag (or an equivalent NL request) is what engages parallel mode. |
| `SOL_MAX_WORKERS` | `3` | Worker ceiling when `--workers` is absent. |
| `SOL_WORKTREE_SETUP` | unset | Shell command run once inside each freshly created worktree (`npm ci`, `uv sync`, …). |

A per-worker runaway backstop of **1800 seconds** is a constant in the launcher, not
configuration.

### The split confirmation gate

When parallel mode engages, Claude splits the request into exactly N tasks and
**prints the split, then waits for confirmation before creating anything**:

```
3 workers requested. Split:

  1  upload-rate-limit   — rate-limit the upload endpoint    scope: src/api/upload.*, tests/api/
  2  preflight-json      — add --json to check-codex.sh      scope: skills/sol/scripts/check-codex.sh
  3  changelog-1-3-0     — changelog entry for 1.3.0         scope: CHANGELOG.md

Proceed?
```

If two tasks' expected file scopes overlap, Claude says so and proposes merging them
into one task or sequencing them, rather than launching. The premise of this feature is
that the tasks are independent; that premise is the user's, and it can be wrong.

---

## 2. Isolation

### Worktree and branch per worker

```bash
git worktree add -b "sol/<slug>" "$WORKTREE_ROOT/<slug>" HEAD
```

- **`WORKTREE_ROOT`** = `<repo-parent>/.sol-worktrees/<repo-basename>/`. A sibling
  directory: it requires no `.gitignore` edit in the user's project, stays on the same
  filesystem as the repo, and keeps copied `.env` files out of `/tmp`.
- **Slug** derives from the task's short title: lowercased, non-alphanumerics collapsed
  to `-`, trimmed to 32 characters. Collisions within a run get a `-2`, `-3` suffix.
  If branch `sol/<slug>` already exists in the repo, the launcher **fails fast naming
  the branch** rather than reusing or force-creating it.
- **Clean tree is a hard precondition** in parallel mode, not the warning it is today.
  Every worker branches from `HEAD`; branching from a dirty `HEAD` makes each worker's
  diff include changes it did not make.

Two workers in one tree corrupt each other's writes, and two workers on one branch is
refused by git and would tangle history if it weren't. Worktrees are not an
optimization here; they are the only correct arrangement.

### Bootstrapping a fresh worktree

A new worktree contains no gitignored files — no `.env`, no `node_modules`, no
`.venv`. This is where parallel setups quietly fail: the worker's verification loop
fails for reasons unrelated to its task, and burns correction rounds on it.

The launcher, for each worktree, in order:

1. Copies root-level `.env` and `.env.*` files that exist and are gitignored, excluding
   `.env.example` / `.env.sample`. **Copy, never symlink** — a symlink lets one worker's
   edit propagate to every other worker.
2. Runs `SOL_WORKTREE_SETUP` if set, with the worktree as cwd. A non-zero exit aborts
   that worker before its Codex session starts, and is recorded as a setup failure.

---

## 3. Launch: `sol-parallel.sh`

### Why a script rather than instructions in Markdown

If `SKILL.md` tells the model "launch N background jobs and wait for all of them", the
model must compose that fan-out correctly on every run, and the failure mode is silent
— exactly the 1.2.0 regression. A script makes it one deterministic foreground command.

Claude writes one brief per task into a **run directory** and makes a single call. The
run directory is the script's only positional argument, and it holds both the inputs and
every output of the run:

```
$SCRATCHPAD/sol-run/
  tasks/01-<slug>.md          # briefs, written by Claude — the inputs
  pids                        # written at launch, before any waiting
  summary.json                # written after every worker is post-processed
  workers/<slug>/
    events.jsonl  report.md  stderr.txt  session-id
```

```bash
bash <skill-dir>/scripts/sol-parallel.sh --workers 3 "$SCRATCHPAD/sol-run"
```

Throughout this document `$out` is `<run-dir>/workers` and `$BASE` is the branch that
was checked out when the run started (recorded as `base_branch` in `summary.json`).

### Per-worker brief additions

Each parallel brief carries, in addition to today's blocks:

- A **file-scope fence** inside `<non_goals>`: the worker touches only the paths named
  in its scope.
- **"Do not commit, do not create or switch branches."** The launcher commits on the
  worker's behalf so that each branch carries exactly one commit representing exactly
  one task.

### What the script does

1. **Preconditions**, all fail-fast with exit 2: inside a git work tree; working tree
   clean; `codex` on `PATH`; `<run-dir>/tasks/` exists and is non-empty; requested worker
   count ≤ ceiling; no pre-existing `sol/<slug>` branches. The ceiling is enforced here
   as a backstop even though Claude refuses first (§1), so a hand-run of the script
   cannot exceed it either.
2. **Per task**: create branch + worktree, bootstrap it (§2).
3. **Launch** up to N concurrently, each child:
   ```bash
   codex exec --json -m gpt-5.6-sol -c model_reasoning_effort=xhigh \
     -s workspace-write --color never -C "$wt" \
     -o "$out/$slug/report.md" - < "$brief" \
     > "$out/$slug/events.jsonl" 2> "$out/$slug/stderr.txt" &
   ```
   PIDs are recorded to `<run-dir>/pids` immediately, before any waiting.
4. **Wait** on all children with a concurrency limit of N, recording each exit code.
   Kill and mark any worker exceeding the 1800s backstop.
5. **Per worker, after it exits**:
   - Assert `events.jsonl` is **non-empty**; an empty log means the codex invocation
     itself failed and the tail of `stderr.txt` is the diagnosis. This generalizes the
     95d04d1 lesson from something the model must remember into something the script
     enforces, per worker.
   - Extract `thread_id` from the first line (`thread.started`) into
     `$out/$slug/session-id`. This is what corrections resume by (§4).
   - `git -C "$wt" add -A && git -C "$wt" commit` with a deterministic subject derived
     from the task title, producing exactly one commit on the branch. A worker that
     changed nothing is recorded as `no-changes` and produces no commit.
6. **Write `<run-dir>/summary.json`**.
7. **Exit** 0 if every worker exited 0 with a non-empty event log; 1 if any worker
   failed; 2 on a precondition error.

### `summary.json`

```json
{
  "base_branch": "main",
  "base_sha": "95d04d1...",
  "workers": [
    {
      "slug": "upload-rate-limit",
      "branch": "sol/upload-rate-limit",
      "worktree": "/path/to/.sol-worktrees/sol-skill/upload-rate-limit",
      "brief": "/path/to/01-upload-rate-limit.md",
      "status": "ok",
      "exit_code": 0,
      "session_id": "019fcd80-...",
      "commit": "abc1234",
      "files_changed": ["src/api/upload.py", "tests/api/test_upload.py"],
      "elapsed_seconds": 512,
      "events_path": "...", "report_path": "...", "stderr_path": "..."
    }
  ]
}
```

`status` is one of `ok`, `no-changes`, `failed-launch` (empty event log), `failed-run`
(non-zero exit), `failed-setup`, `timed-out`.

### Overrunning the tool timeout

Wall clock is the slowest worker, not the sum, but `xhigh` still overruns a 10-minute
Bash tool ceiling. Re-invoking the same script re-attaches:

```bash
bash <skill-dir>/scripts/sol-parallel.sh --wait "$SCRATCHPAD/sol-run"
```

`--wait` reads `<run-dir>/pids`, blocks on any still-live worker, and **exits 75 for "still
running"**, 0/1 once every worker has finished and been post-processed. One mechanism,
called repeatedly, with an explicit not-done code — so there is no second launch path
for the model to pick wrong, and "not finished" can never be mistaken for "finished
quietly". This is portable to hosts without a background-task facility.

---

## 4. Review, correction, integration

### Review

Per worker, **in task order** (not completion order — a deterministic order makes the
report reproducible):

1. Scope with `git diff --stat "$BASE".."sol/<slug>"`.
2. Read the full branch diff as the primary substrate, opening whole files only where
   hunks lack the context to judge correctness.
3. Apply today's phase-3 standard: correctness against the acceptance criteria,
   regressions, edge cases, security, missing tests, out-of-scope changes — plus a
   parallel-specific check that the worker stayed inside its declared file scope.

Sol's per-worker `report.md` is read for what it claims, never as verification.

### Integration — sequential, one branch at a time

Approved branches are **cherry-picked onto the base branch one at a time**, in task
order. Each branch carries a single commit, so the result is linear history with one
commit per task and no merge commits.

After each cherry-pick, every **not-yet-reviewed** worktree is rebased onto the new
base head:

```bash
git -C "$wt" rebase "$BASE"
```

so each subsequent review happens against current reality. Conflicts therefore surface
one branch at a time rather than all at once at the end. **A rebase or cherry-pick
conflict is evidence the "independent tasks" premise was wrong for that pair** — it is
reported as such, and resolution is either a correction round scoped to the conflict or
a hand-back to the user, never an automatic conflict resolution.

### Corrections

Per failing worker, capped at **2 rounds counted independently per worker**:

```bash
cd "$wt" && codex exec resume "$SESSION_ID" --json \
  -m gpt-5.6-sol -c model_reasoning_effort=xhigh \
  -o "$out/$slug/report.md" \
  "<file:line — observed problem, required behavior, check that must pass>" \
  > "$out/$slug/events.jsonl" 2> "$out/$slug/stderr.txt"
```

Two constraints verified against `codex-cli 0.144.6`:

- **Never `--last`.** With N sessions in flight, "the most recent recorded session" is a
  race that would silently apply a correction to the wrong worker. Resume by the
  explicit `session_id` recorded in `summary.json`.
- **`codex exec resume` accepts no `-C/--cd`, no `-s`, no `--color`** — only `-c`, `-m`,
  `-i`, `--json`, `-o`, `--last`, the session id and the prompt. The worktree must
  therefore be the *cwd*, which is why corrections run through the launcher
  (`--resume`) rather than as an inline command. Passing an unsupported flag makes
  codex exit 2 instantly with an empty event log, which is invisible under redirection
  — the same trap fixed in 95d04d1.

The launcher's `--resume <dir>` mode resumes every worker that has a pending correction
brief, in parallel, under the same ceiling and the same non-empty-event-log assertion.

A worker still failing after 2 rounds **leaves its branch in place, named in the
report**. Everything that passed is already merged; a stubborn task never holds
finished work hostage.

### The combined-state re-run — the load-bearing check

Each worker verified green against a base that did not contain the other workers'
changes. **Green × N does not imply green combined.**

So after integration, Claude re-runs the project's full test/lint/typecheck commands
**once on the merged base branch**, and *that* result is what the report leads with.
Per-worker results are reported as supporting detail only. Reporting N green isolated
runs as if they were a green integrated run would reintroduce, one level up, exactly the
self-assessment problem this skill exists to eliminate.

If the combined re-run fails, the failure is attributed to the task its diff points at
and handled as a correction round for that worker; if it cannot be attributed, it is
reported to the user with the failing output.

### Cleanup

After integration: `git worktree remove` each worktree, `git branch -d` each merged
branch, `git worktree prune`. Branches for failed or unreviewed workers survive and are
**named in the report along with their worktree paths**, so nothing is stranded silently.

---

## 5. Report

Success requires all of: every task's acceptance criteria met, the **combined** checks
passing under Claude's own re-run, every diff reviewed, and no unexplained out-of-scope
changes.

The final message contains:

- **Per task**: title, branch, verdict, `git diff --stat` table with one clause per file
  saying what changed in it, and the correction rounds it needed.
- **Combined**: the full check re-run on the merged branch with actual command output,
  the list of commits now on the base branch, and the wall-clock time.
- **Survivors and risks**: any branch or worktree left behind and why, tasks that never
  launched (e.g. rate-limited), and remaining risks.

A task that never started is reported as **unstarted**, never omitted.

---

## 6. Failure modes this design owns explicitly

| Failure | Handling |
|---|---|
| Empty event log for a worker (codex itself failed) | Script asserts non-empty per worker; status `failed-launch`; stderr tail surfaced |
| 429 / rate limit under N concurrent sessions | That worker's non-zero exit is recorded; its task is reported **unstarted**, never silently dropped |
| Wall clock exceeds the tool timeout | `--wait` re-attach, exit 75 for "still running" |
| Dirty tree at launch | Hard stop, exit 2 |
| Not a git repository | Hard stop, exit 2 |
| Pre-existing `sol/<slug>` branch | Hard stop naming the branch; never force-created |
| Requested workers > ceiling | Refusal naming the ceiling and how to raise it; never a silent clamp |
| Rebase / cherry-pick conflict between workers | Reported as a violated independence premise; correction or hand-back, never auto-resolved |
| Worker exceeds 1800s | Killed, status `timed-out`, branch preserved |
| `SOL_WORKTREE_SETUP` fails | Worker aborted before its Codex session starts, status `failed-setup` |
| Worker produced no changes | Status `no-changes`, no commit, reported |
| All workers green individually, combined state red | Mandatory combined re-run catches it; report leads with the combined result |

---

## 7. Files

| File | Change |
|---|---|
| `skills/sol/scripts/sol-parallel.sh` | **New.** Preconditions, worktree creation and bootstrap, fan-out launch, `--wait` re-attach, `--resume` correction batches, per-worker post-processing, `summary.json` |
| `skills/sol/references/parallel-flow.md` | **New.** The parallel phases in full, keeping `SKILL.md` readable |
| `skills/sol/references/brief-template.md` | Add the file-scope fence and the "do not commit / do not branch" clause for parallel briefs |
| `skills/sol/SKILL.md` | The opt-in routing rule, the config table, the split-confirmation gate, and a pointer to the reference. Single-worker flow untouched |
| `skills/sol/scripts/check-codex.sh` | One added check: `git worktree` usable and `WORKTREE_ROOT` writable |
| `skills/sol/scripts/tests/test_sol_parallel.py` | **New.** See Testing |
| `README.md` | Parallel-workers section; amend "there is no config file" to name the two env vars |
| `CHANGELOG.md`, `.claude-plugin/plugin.json` | 1.3.0 |

## 8. Testing

Python driving the bash script via `subprocess`, matching the existing
`test_sol_watch.py` convention. Each test builds a real throwaway git repo in a temp
directory and puts a **fake `codex` shim** first on `PATH`. The shim emits canned JSONL
(a `thread.started` line with a known `thread_id`, then `item.completed` /
`turn.completed`), writes a report file, and touches a file in its cwd so there is a
diff to commit. No network, no real Codex, deterministic.

Cases: N workers produce N branches with exactly one commit each; task order preserved
in `summary.json`; `session_id` extracted correctly; empty-event-log detection; each
precondition failure exits 2; ceiling refusal; `--wait` returns 75 while a worker lives
and 0 after; `.env` copied but `.env.example` not; `SOL_WORKTREE_SETUP` runs in the
worktree and its failure aborts that worker only; a no-change worker yields
`no-changes` and no commit; slug collision within a run is disambiguated; a
pre-existing branch is a hard stop.

## 9. Acceptance criteria

1. `/sol <task>` with no worker count produces byte-for-byte today's single-worker
   behavior — no worktree created, no branch created, changes in the user's tree.
2. `/sol --workers 3 <three tasks>` prints the split and creates nothing until confirmed.
3. Requesting more workers than the ceiling refuses with a message naming the ceiling;
   no Codex session starts.
4. After a successful 3-worker run, the base branch has exactly 3 new commits in task
   order, the working tree is clean, and no `sol/*` branch or worktree remains.
5. A worker whose event log is empty is reported `failed-launch` with its stderr tail,
   and the other workers still merge.
6. A correction resumes by explicit session id from `summary.json`, run with the
   worktree as cwd, and passes no `-s`, `--color`, or `-C` to `codex exec resume`.
7. The report's headline check result comes from a re-run on the merged base branch, not
   from any per-worker run.
8. `python3 -m pytest skills/sol/scripts/tests/` passes with no network access.

---

## Sources

- [Running Multiple Codex Agent Instances: Parallel Orchestration Patterns](https://codex.danielvaughan.com/2026/04/18/running-multiple-codex-agents-parallel-orchestration/) — 3–5 concurrent sweet spot; distinct file ownership per agent; per-agent budgets and iteration caps
- [Coding Agent Orchestration — Tembo](https://www.tembo.io/blog/coding-agent-orchestration) — sequential merge with rebase-onto-newest-main
- [Git Worktrees for Parallel AI Agent Execution — Augment Code](https://www.augmentcode.com/guides/git-worktrees-parallel-ai-agent-execution) — worktree-per-agent isolation, clean test baseline before handoff
- [Using git worktree for parallel AI agent development — DEV](https://dev.to/sonim1/using-git-worktree-for-parallel-ai-agent-development-44nb) — untracked-file gap, copy `.env` rather than symlink
- [Parallel Agentic Development With Git Worktrees — MindStudio](https://www.mindstudio.ai/blog/parallel-agentic-development-git-worktrees) — one file one owner; PR/branch as integration checkpoint
- [parallel-worktrees SKILL.md — SpillwaveSolutions](https://github.com/SpillwaveSolutions/parallel-worktrees/blob/main/SKILL.md) — worktree naming, status files, cleanup protocol
- [Orchestrating Multiple Parallel Agents — Developer Toolkit](https://developertoolkit.ai/en/codex/productivity-patterns/multi-agent-workflows/) — shared rate limits across parallel Codex sessions
- Local verification against `codex-cli 0.144.6`: `codex exec -C/--cd` exists; `codex exec resume` accepts only `-c`, `-m`, `-i`, `--json`, `-o`, `--last`; `thread.started` carries `thread_id`
