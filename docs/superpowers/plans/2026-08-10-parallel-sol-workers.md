# Parallel Sol Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `/sol` run several independent implementation tasks concurrently — one Codex session and one git worktree per worker — while the default single-worker flow stays byte-for-byte unchanged.

**Architecture:** One new bash script, `skills/sol/scripts/sol-parallel.sh`, owns everything mechanical: preconditions, worktree creation and bootstrap, the fan-out launch, waiting, per-worker bookkeeping, and `summary.json`. Claude does only what requires judgment — splitting the request, writing briefs, reviewing diffs, cherry-picking approved branches, and re-running the checks on the merged result. The script exists specifically so the planner model never composes a fan-out itself, which is the 1.2.0 regression this design must not repeat.

**Tech Stack:** bash (matching `check-codex.sh` conventions), git worktrees, `codex exec`, Python 3 stdlib for tests.

**Spec:** [`docs/superpowers/specs/2026-08-10-parallel-sol-workers-design.md`](../specs/2026-08-10-parallel-sol-workers-design.md)

## Global Constraints

- **Bash style matches `check-codex.sh`:** `set -uo pipefail` (never `set -e` — non-zero worker exits must be captured, not fatal), `#!/usr/bin/env bash`, a usage comment block at the top.
- **CI gates every script:** `bash -n` must pass, the file must be `chmod +x`, and `shellcheck --severity=error skills/sol/scripts/*.sh` must be clean. Run all three locally before every commit.
- **Tests are stdlib-only Python run directly:** `python3 skills/sol/scripts/tests/test_sol_parallel.py`. No pytest, no third-party imports, no network. Follow `test_sol_watch.py`: a module-level `failures: list[str]`, a `check(condition, label)` helper printing `  ok    <label>` / `  FAIL  <label>`, and a final block that exits 1 listing failures or prints `all checks passed`.
- **No new runtime dependencies.** `git`, `bash`, `python3`, `codex` only. No `timeout(1)` — it is absent on stock macOS; the backstop is hand-rolled.
- **Version 1.3.0** must be set in **both** `.claude-plugin/plugin.json` and the `version:` field of `skills/sol/SKILL.md`. CI fails the build if they disagree.
- **Exit codes are contract:** `0` all workers ok · `1` at least one worker failed · `2` precondition or usage error · `75` still running (`--wait` only).
- **Never pass `-s`, `--color`, or `-C` to `codex exec resume`** — `codex-cli 0.144.6` rejects all three at parse time, exits 2 instantly, and leaves an empty event log that is invisible under redirection.
- **Never use `codex exec resume --last`** anywhere in this feature. With N sessions in flight it resumes an arbitrary worker.

### Two refinements to the spec, adopted here

1. **The spec's acceptance criterion 8 says `python3 -m pytest`.** The repo has no pytest dependency and CI runs test files directly. Corrected to `python3 skills/sol/scripts/tests/test_sol_parallel.py`.
2. **Waves are the caller's job, not the script's.** The spec requires each wave to branch from the previous wave's *merged* result, which needs Claude's review in between. So the script **refuses when the brief count exceeds the worker count** (exit 2) rather than running a rolling concurrency window, and Claude invokes it once per wave with a fresh run directory. This removes the concurrency-limiting loop entirely: launch all, wait all.

---

## File Structure

| File | Responsibility |
|---|---|
| `skills/sol/scripts/sol-parallel.sh` | **New.** The only executable surface. Modes: default (launch), `--wait`, `--resume`, `--cleanup`. |
| `skills/sol/scripts/tests/test_sol_parallel.py` | **New.** Self-contained: helpers, a fake `codex` shim generator, and all checks. |
| `skills/sol/references/parallel-flow.md` | **New.** The parallel phases in prose, so `SKILL.md` stays short. |
| `skills/sol/SKILL.md` | Opt-in routing rule, config table, pointer to the reference. Single-worker flow untouched. |
| `skills/sol/references/brief-template.md` | File-scope fence + "do not commit / do not branch" for parallel briefs. |
| `skills/sol/scripts/check-codex.sh` | One added check: worktree root creatable. |
| `.github/workflows/validate.yml` | Run the new test file. |
| `README.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json` | Docs and 1.3.0. |

### Run directory layout (the script's data contract)

```
<run-dir>/
  tasks/01-<slug>.md        # briefs, written by Claude — inputs
  pids                      # "<slug>\t<pid>" per line, written before any waiting
  base                      # "<branch>\t<sha>" of the checkout at launch
  summary.json              # written after every worker is post-processed
  workers/<slug>/
    events.jsonl  report.md  stderr.txt
    exit-code               # written by the child wrapper the instant codex exits
    session-id              # thread_id, extracted during post-processing
    started-at              # epoch seconds, for the 1800s backstop
    correction.md           # optional, consumed by --resume
```

---

## Task 1: Script skeleton, preconditions, and the test harness

**Files:**
- Create: `skills/sol/scripts/sol-parallel.sh`
- Create: `skills/sol/scripts/tests/test_sol_parallel.py`
- Modify: `.github/workflows/validate.yml` (add a test step after the `sol-watch tests` step)

**Interfaces:**
- Consumes: nothing.
- Produces: `sol-parallel.sh [--workers N] [--wait|--resume|--cleanup] <run-dir>`; exit codes 0/1/2/75; the run-directory layout above. Test helpers `make_repo(path)`, `install_fake_codex(bin_dir, **behavior)`, `run(args, cwd, env)`, `check(cond, label)`.

- [ ] **Step 1: Write the failing test**

Create `skills/sol/scripts/tests/test_sol_parallel.py`:

```python
#!/usr/bin/env python3
"""Tests for sol-parallel.sh. No dependencies: `python3 test_sol_parallel.py`.

Every test builds a real throwaway git repo and puts a fake `codex` first on
PATH, so nothing here touches the network or a real Codex session.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "sol-parallel.sh"

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


def git(repo: pathlib.Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    return out.stdout.strip()


def make_repo(path: pathlib.Path) -> pathlib.Path:
    """A git repo with one commit on branch 'main' and a gitignored .env."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    (path / ".gitignore").write_text(".env\n.env.local\n.env.sample\n")
    (path / ".env").write_text("SECRET=1\n")
    (path / ".env.local").write_text("SECRET=2\n")
    (path / ".env.sample").write_text("SECRET=\n")   # ignored AND excluded by name
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


FAKE_CODEX = r"""#!/usr/bin/env bash
# Fake codex for tests. Honours -C <dir> and -o <file>, reads the brief on stdin.
set -uo pipefail
cd_dir="."
out_file=""
prev=""
for arg in "$@"; do
  case "$prev" in
    -C) cd_dir="$arg" ;;
    -o) out_file="$arg" ;;
  esac
  prev="$arg"
done
cat >/dev/null                      # consume the brief
slug="$(basename "$(dirname "$out_file")")"
[ -n "${FAKE_SLEEP:-}" ] && sleep "$FAKE_SLEEP"
if [ -z "${FAKE_EMPTY:-}" ]; then
  printf '{"type":"thread.started","thread_id":"%s"}\n' "${FAKE_THREAD:-019f-$slug}"
  printf '{"type":"turn.completed"}\n'
fi
[ -n "$out_file" ] && printf 'fake report for %s\n' "$slug" > "$out_file"
if [ -z "${FAKE_NOCHANGE:-}" ]; then
  printf 'touched by %s\n' "$slug" > "$cd_dir/$slug.txt"
fi
exit "${FAKE_EXIT:-0}"
"""


def install_fake_codex(bin_dir: pathlib.Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "codex"
    shim.write_text(FAKE_CODEX)
    shim.chmod(0o755)


def write_tasks(run_dir: pathlib.Path, *slugs: str) -> None:
    tasks = run_dir / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    for i, slug in enumerate(slugs, start=1):
        (tasks / f"{i:02d}-{slug}.md").write_text(f"<task>do {slug}</task>\n")


def run(repo: pathlib.Path, bin_dir: pathlib.Path, *args: str):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("SOL_MAX_WORKERS", None)
    env.pop("SOL_WORKTREE_SETUP", None)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo, capture_output=True, text=True, check=False, env=env,
    )


print("preconditions")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)

    # not a git repo
    plain = root / "plain"
    plain.mkdir()
    (plain / "run" / "tasks").mkdir(parents=True)
    (plain / "run" / "tasks" / "01-a.md").write_text("x")
    r = run(plain, bin_dir, "--workers", "1", str(plain / "run"))
    check(r.returncode == 2, "exit 2 outside a git work tree")

    repo = make_repo(root / "repo")
    run_dir = root / "run"

    # missing tasks dir
    run_dir.mkdir()
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 2, "exit 2 when <run-dir>/tasks is missing")

    write_tasks(run_dir, "alpha", "beta")

    # more briefs than workers
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 2, "exit 2 when brief count exceeds worker count")
    check("wave" in r.stderr.lower() or "exceed" in r.stderr.lower(),
          "explains the brief/worker mismatch")

    # above the ceiling
    r = run(repo, bin_dir, "--workers", "9", str(run_dir))
    check(r.returncode == 2, "exit 2 when requested workers exceed the ceiling")
    check("SOL_MAX_WORKERS" in r.stderr, "names SOL_MAX_WORKERS when refusing")

    # dirty tree
    (repo / "dirty.txt").write_text("x")
    r = run(repo, bin_dir, "--workers", "2", str(run_dir))
    check(r.returncode == 2, "exit 2 on a dirty working tree")
    (repo / "dirty.txt").unlink()

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 skills/sol/scripts/tests/test_sol_parallel.py`
Expected: FAIL — every check fails because `sol-parallel.sh` does not exist yet (bash exits 127, not 2).

- [ ] **Step 3: Write minimal implementation**

Create `skills/sol/scripts/sol-parallel.sh`:

```bash
#!/usr/bin/env bash
# Fan-out launcher for the /sol skill's parallel mode. Runs one `codex exec`
# worker per brief, each in its own git worktree and branch.
#
# Usage:
#   sol-parallel.sh [--workers N] <run-dir>     launch and wait
#   sol-parallel.sh --dry-run   <run-dir>       create worktrees only, do not launch
#   sol-parallel.sh --wait      <run-dir>       re-attach to a running batch
#   sol-parallel.sh --resume    <run-dir>       send correction briefs
#   sol-parallel.sh --cleanup   <run-dir>       remove merged worktrees/branches
#
# Exit: 0 all ok · 1 a worker failed · 2 precondition/usage · 75 still running
#
# Env: SOL_MAX_WORKERS (default 3)   ceiling on --workers
#      SOL_WORKTREE_SETUP            command run in each fresh worktree

set -uo pipefail

MODEL="${SOL_MODEL:-gpt-5.6-sol}"
EFFORT="${SOL_EFFORT:-xhigh}"
WORKER_TIMEOUT=1800

die() { printf 'sol-parallel: %s\n' "$1" >&2; exit "${2:-2}"; }

MODE="launch"
WORKERS=""
RUN_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workers) [ $# -ge 2 ] || die "--workers requires a value"
               WORKERS="$2"; shift 2 ;;
    --wait)    MODE="wait";    shift ;;
    --resume)  MODE="resume";  shift ;;
    --cleanup) MODE="cleanup"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    -*)        die "unknown option: $1" ;;
    *)         RUN_DIR="$1"; shift ;;
  esac
done

[ -n "$RUN_DIR" ] || die "usage: sol-parallel.sh [--workers N] <run-dir>"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "not inside a git work tree"

REPO_ROOT="$(git rev-parse --show-toplevel)"
REPO_NAME="$(basename "$REPO_ROOT")"
WORKTREE_ROOT="$(cd "$REPO_ROOT/.." && pwd)/.sol-worktrees/$REPO_NAME"
TASKS_DIR="$RUN_DIR/tasks"
OUT_DIR="$RUN_DIR/workers"

preflight_launch() {
  local ceiling="${SOL_MAX_WORKERS:-3}"
  [ -d "$TASKS_DIR" ] || die "no briefs: $TASKS_DIR does not exist"

  local briefs=()
  local f
  for f in "$TASKS_DIR"/*.md; do
    [ -e "$f" ] && briefs+=("$f")
  done
  [ "${#briefs[@]}" -gt 0 ] || die "no briefs: $TASKS_DIR/*.md matched nothing"

  [ -n "$WORKERS" ] || WORKERS="$ceiling"
  case "$WORKERS" in ''|*[!0-9]*) die "--workers must be a positive integer" ;; esac
  [ "$WORKERS" -ge 1 ] || die "--workers must be at least 1"

  if [ "$WORKERS" -gt "$ceiling" ]; then
    die "requested $WORKERS workers but the ceiling is $ceiling; raise it with SOL_MAX_WORKERS"
  fi
  if [ "${#briefs[@]}" -gt "$WORKERS" ]; then
    die "${#briefs[@]} briefs exceed $WORKERS workers; run one wave per batch, merging between waves"
  fi

  command -v codex >/dev/null 2>&1 || die "codex not found on PATH"
  [ -z "$(git status --porcelain)" ] \
    || die "working tree is dirty; commit or stash so each worker branches from a clean HEAD"

  BRIEFS=("${briefs[@]}")
}

case "$MODE" in
  launch)  preflight_launch ;;
  *)       [ -d "$OUT_DIR" ] || die "no run directory to $MODE: $OUT_DIR" ;;
esac

exit 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
chmod +x skills/sol/scripts/sol-parallel.sh
bash -n skills/sol/scripts/sol-parallel.sh
shellcheck --severity=error skills/sol/scripts/sol-parallel.sh
python3 skills/sol/scripts/tests/test_sol_parallel.py
```
Expected: all four clean; the test prints `all checks passed`.

- [ ] **Step 5: Wire the test into CI**

In `.github/workflows/validate.yml`, immediately after the `sol-watch tests` step:

```yaml
      - name: sol-parallel tests
        run: python3 skills/sol/scripts/tests/test_sol_parallel.py
```

- [ ] **Step 6: Commit**

```bash
git add skills/sol/scripts/sol-parallel.sh skills/sol/scripts/tests/test_sol_parallel.py .github/workflows/validate.yml
git commit -m "Add sol-parallel skeleton with preconditions and test harness"
```

---

## Task 2: Worktree creation and bootstrap

**Files:**
- Modify: `skills/sol/scripts/sol-parallel.sh` (add `slug_for`, `create_worktrees`)
- Modify: `skills/sol/scripts/tests/test_sol_parallel.py` (append a `worktrees` section)

**Interfaces:**
- Consumes: `BRIEFS` array, `WORKTREE_ROOT`, `OUT_DIR` from Task 1.
- Produces: `slug_for <brief-path>` → sanitized slug (strips a leading `NN-` and the `.md`); `create_worktrees` → populates parallel arrays `SLUGS`, `WORKTREES`, `BRIEF_OF` and creates `<run-dir>/base`. Branch naming is `sol/<slug>`.

- [ ] **Step 1: Write the failing test**

Append to `test_sol_parallel.py`, before the final `if failures:` block:

```python
print("worktrees")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha", "beta")

    wt_root = root / ".sol-worktrees" / "repo"

    r = run(repo, bin_dir, "--workers", "2", "--dry-run", str(run_dir))
    check(r.returncode == 0, f"--dry-run exits 0 (stderr: {r.stderr[:200]})")
    check((wt_root / "alpha").is_dir(), "creates a worktree per brief")
    check((wt_root / "beta").is_dir(), "creates the second worktree")
    check("sol/alpha" in git(repo, "branch", "--list", "sol/alpha"),
          "creates branch sol/<slug>")
    check((wt_root / "alpha" / ".env").is_file(), "copies .env into the worktree")
    check(not (wt_root / "alpha" / ".env").is_symlink(), "copies .env rather than symlinking")
    check((wt_root / "alpha" / ".env.local").is_file(), "copies .env.* variants too")
    check(not (wt_root / "alpha" / ".env.sample").exists(),
          "never copies .env.sample / .env.example")
    base = (run_dir / "base").read_text().split("\t")
    check(base[0] == "main", "records the base branch")

    # a pre-existing branch is a hard stop. Remove both worktrees, keep branch
    # sol/beta and delete sol/alpha, so the batch collides on its SECOND brief
    # while `alpha` is the one that must never be created.
    #
    # The ordering carries the whole test. `git worktree add -b` fails on its
    # own when the branch exists, so a collision on the FIRST brief aborts at
    # iteration 1 with or without the pre-check — the assertions below would
    # pass against an implementation that has no pre-check at all. Colliding on
    # the SECOND brief is what discriminates: without a check across the whole
    # batch before any worktree is created, `alpha` gets created and is left
    # behind when `beta` fails.
    shutil.rmtree(wt_root)
    git(repo, "worktree", "prune")
    git(repo, "branch", "-D", "sol/alpha")
    r = run(repo, bin_dir, "--workers", "2", "--dry-run", str(run_dir))
    check(r.returncode == 2, "exit 2 when sol/<slug> already exists")
    check("sol/beta" in r.stderr, "names the colliding branch")
    check(not (wt_root / "alpha").exists(),
          "a collision anywhere aborts before creating any worktree, "
          "even an earlier non-colliding one")
    check(git(repo, "branch", "--list", "sol/alpha") == "",
          "a collision anywhere creates no branch, even for an earlier "
          "non-colliding brief")

print("worktree setup hook")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SOL_WORKTREE_SETUP"] = "echo ran > setup-marker.txt"
    r = subprocess.run(
        ["bash", str(SCRIPT), "--workers", "1", "--dry-run", str(run_dir)],
        cwd=repo, capture_output=True, text=True, check=False, env=env,
    )
    marker = root / ".sol-worktrees" / "repo" / "alpha" / "setup-marker.txt"
    check(r.returncode == 0, "setup hook run exits 0")
    check(marker.is_file(), "SOL_WORKTREE_SETUP runs inside the worktree")

    env["SOL_WORKTREE_SETUP"] = "exit 3"
    run_dir2 = root / "run2"
    write_tasks(run_dir2, "gamma")
    r = subprocess.run(
        ["bash", str(SCRIPT), "--workers", "1", "--dry-run", str(run_dir2)],
        cwd=repo, capture_output=True, text=True, check=False, env=env,
    )
    check(r.returncode == 1, "a failing setup hook fails that worker")
    check("failed-setup" in (r.stdout + r.stderr), "reports failed-setup")
```

`--dry-run` creates the worktrees and bootstraps them, then stops before launching any
Codex session — useful on its own for inspecting the setup before spending real runs,
and it is what keeps this task independently testable before any launching exists.
Document it in the script's usage block as a first-class mode, not a test hook.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 skills/sol/scripts/tests/test_sol_parallel.py`
Expected: FAIL — `--dry-run` is an unknown option, so the script exits 2.

- [ ] **Step 3: Write minimal implementation**

In the option loop, add `--dry-run` alongside the other flags:

```bash
    --dry-run) DRY_RUN=1; shift ;;
```

and initialise `DRY_RUN=0` next to `MODE="launch"`. Then add, after `preflight_launch`:

```bash
slug_for() {
  local base
  base="$(basename "$1" .md)"
  base="${base#[0-9][0-9]-}"
  base="$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-')"
  base="${base#-}"; base="${base%-}"
  base="${base:0:32}"
  # A brief name that reduces to nothing must not yield the branch `sol/`.
  [ -n "$base" ] || base="task"
  printf '%s' "$base"
}

bootstrap_worktree() {
  local wt="$1" slug="$2" f
  for f in "$REPO_ROOT"/.env "$REPO_ROOT"/.env.*; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in .env.example|.env.sample) continue ;; esac
    git -C "$REPO_ROOT" check-ignore -q "$f" || continue
    cp "$f" "$wt/$(basename "$f")" || return 1
  done
  if [ -n "${SOL_WORKTREE_SETUP:-}" ]; then
    ( cd "$wt" && eval "$SOL_WORKTREE_SETUP" ) \
      >"$OUT_DIR/$slug/setup.log" 2>&1 || return 1
  fi
  return 0
}

create_worktrees() {
  local branch sha brief slug wt
  branch="$(git rev-parse --abbrev-ref HEAD)"
  sha="$(git rev-parse HEAD)"
  mkdir -p "$OUT_DIR" "$WORKTREE_ROOT"
  printf '%s\t%s\n' "$branch" "$sha" > "$RUN_DIR/base"

  SLUGS=(); WORKTREES=(); BRIEF_OF=()
  for brief in "${BRIEFS[@]}"; do
    local stem candidate n=2
    stem="$(slug_for "$brief")"
    candidate="$stem"
    # "${SLUGS[@]:-}" on an empty array substitutes one empty word, which would
    # match an empty candidate; guard on length instead.
    while [ "${#SLUGS[@]}" -gt 0 ] && printf '%s\n' "${SLUGS[@]}" | grep -qx "$candidate"; do
      candidate="$stem-$n"; n=$((n + 1))
    done
    slug="$candidate"
    git show-ref --verify --quiet "refs/heads/sol/$slug" \
      && die "branch sol/$slug already exists; delete it or rename the brief"
    SLUGS+=("$slug"); BRIEF_OF+=("$brief")
    WORKTREES+=("$WORKTREE_ROOT/$slug")
  done

  local i status=0
  for i in "${!SLUGS[@]}"; do
    slug="${SLUGS[i]}"; wt="${WORKTREES[i]}"
    mkdir -p "$OUT_DIR/$slug"
    # Record the brief now. Recovering it later by globbing `*-<slug>.md` is
    # ambiguous: brief `01-add-auth.md` also matches slug `auth`.
    printf '%s\n' "${BRIEF_OF[i]}" > "$OUT_DIR/$slug/brief"
    git worktree add -q -b "sol/$slug" "$wt" HEAD \
      || die "could not create worktree for $slug"
    if ! bootstrap_worktree "$wt" "$slug"; then
      printf 'failed-setup\n' > "$OUT_DIR/$slug/status"
      printf 'sol-parallel: %s: failed-setup (see %s)\n' \
        "$slug" "$OUT_DIR/$slug/setup.log" >&2
      status=1
    fi
  done
  return "$status"
}
```

Replace the trailing `exit 0` with:

```bash
if [ "$MODE" = "launch" ]; then
  create_worktrees; setup_status=$?
  if [ "$DRY_RUN" -eq 1 ]; then
    exit "$setup_status"
  fi
fi

exit 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
bash -n skills/sol/scripts/sol-parallel.sh && shellcheck --severity=error skills/sol/scripts/sol-parallel.sh
python3 skills/sol/scripts/tests/test_sol_parallel.py
```
Expected: PASS, `all checks passed`.

- [ ] **Step 5: Commit**

```bash
git add skills/sol/scripts/sol-parallel.sh skills/sol/scripts/tests/test_sol_parallel.py
git commit -m "Create a worktree and branch per brief, with env copying and a setup hook"
```

---

## Task 3: Fan-out launch, PID recording, and the wait loop

**Files:**
- Modify: `skills/sol/scripts/sol-parallel.sh` (add `launch_workers`, `wait_for_workers`)
- Modify: `skills/sol/scripts/tests/test_sol_parallel.py` (append a `launch` section)

**Interfaces:**
- Consumes: `SLUGS`, `WORKTREES`, `BRIEF_OF`, `OUT_DIR` from Task 2.
- Produces: `<run-dir>/pids` (`<slug>\t<pid>` per line, written before any waiting); `<run-dir>/workers/<slug>/exit-code`, `events.jsonl`, `report.md`, `stderr.txt`, `started-at`. `wait_for_workers <block>` where `block=1` blocks until every worker has an `exit-code`, `block=0` returns 75 if any is still live.

- [ ] **Step 1: Write the failing test**

Append to `test_sol_parallel.py`:

```python
print("launch")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha", "beta")

    r = run(repo, bin_dir, "--workers", "2", str(run_dir))
    check(r.returncode == 0, f"a clean 2-worker run exits 0 (stderr: {r.stderr[:200]})")

    pids = (run_dir / "pids").read_text().strip().splitlines()
    check(len(pids) == 2, "records one pid per worker")
    check(all("\t" in line for line in pids), "pids file is slug<TAB>pid")

    for slug in ("alpha", "beta"):
        w = run_dir / "workers" / slug
        check((w / "events.jsonl").stat().st_size > 0, f"{slug}: event log is non-empty")
        check((w / "exit-code").read_text().strip() == "0", f"{slug}: records exit code 0")
        check((w / "report.md").is_file(), f"{slug}: report written")
        wt = root / ".sol-worktrees" / "repo" / slug
        check((wt / f"{slug}.txt").is_file(), f"{slug}: worker wrote into its own worktree")

    # workers really are concurrent: two 2s workers finish in well under 4s
    repo2 = make_repo(root / "repo2")
    run_dir2 = root / "run2"
    write_tasks(run_dir2, "one", "two")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_SLEEP"] = "2"
    import time
    t0 = time.time()
    r = subprocess.run(
        ["bash", str(SCRIPT), "--workers", "2", str(run_dir2)],
        cwd=repo2, capture_output=True, text=True, check=False, env=env,
    )
    elapsed = time.time() - t0
    check(r.returncode == 0, "concurrent run exits 0")
    check(elapsed < 3.5, f"two 2s workers ran concurrently (took {elapsed:.1f}s)")

print("launch failures")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_EMPTY"] = "1"
    env["FAKE_EXIT"] = "2"
    r = subprocess.run(
        ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
        cwd=repo, capture_output=True, text=True, check=False, env=env,
    )
    check(r.returncode == 1, "a failed worker makes the run exit 1")
    check((run_dir / "workers" / "alpha" / "exit-code").read_text().strip() == "2",
          "records the worker's real exit code")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 skills/sol/scripts/tests/test_sol_parallel.py`
Expected: FAIL — no `pids` file is written; the run exits 0 without launching anything.

- [ ] **Step 3: Write minimal implementation**

Add before the trailing dispatch:

```bash
launch_workers() {
  local i slug wt brief w pid
  # Job control puts each background job in its own process group, so the
  # timeout backstop can signal the whole group. Without it, killing the
  # wrapper orphans the `codex` process it forked and the backstop is a no-op.
  set -m
  : > "$RUN_DIR/pids"
  for i in "${!SLUGS[@]}"; do
    slug="${SLUGS[i]}"; wt="${WORKTREES[i]}"; brief="${BRIEF_OF[i]}"
    w="$OUT_DIR/$slug"
    [ -f "$w/status" ] && continue        # failed-setup: never launched
    date +%s > "$w/started-at"
    nohup bash -c '
      codex exec --json -m "$1" -c model_reasoning_effort="$2" \
        -s workspace-write --color never -C "$3" \
        -o "$4/report.md" - < "$5" \
        > "$4/events.jsonl" 2> "$4/stderr.txt"
      printf "%s\n" "$?" > "$4/exit-code"
    ' _ "$MODEL" "$EFFORT" "$wt" "$w" "$brief" >/dev/null 2>&1 &
    pid=$!
    disown "$pid" 2>/dev/null
    printf '%s\t%s\n' "$slug" "$pid" >> "$RUN_DIR/pids"
  done
}

worker_slugs() { cut -f1 "$RUN_DIR/pids"; }
pid_of() { awk -F'\t' -v s="$1" '$1 == s { print $2 }' "$RUN_DIR/pids"; }

wait_for_workers() {
  local block="$1" slug pid started now live
  while :; do
    live=0
    while read -r slug; do
      [ -n "$slug" ] || continue
      [ -f "$OUT_DIR/$slug/exit-code" ] && continue
      pid="$(pid_of "$slug")"
      if kill -0 "$pid" 2>/dev/null; then
        started="$(cat "$OUT_DIR/$slug/started-at" 2>/dev/null || echo 0)"
        now="$(date +%s)"
        if [ "$started" -gt 0 ] && [ $((now - started)) -gt "$WORKER_TIMEOUT" ]; then
          # Signal the whole process group: the wrapper forked `codex`, so
          # killing the wrapper alone leaves the real worker running.
          kill -9 -- -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null
          printf '124\n' > "$OUT_DIR/$slug/exit-code"
          continue
        fi
        live=$((live + 1))
      else
        # Gone without recording an exit code: killed or interrupted. The
        # wrapper may have been killed alone (an operator's `kill -9`, an OOM
        # kill), leaving `codex` orphaned — reap the group for the same reason
        # the timeout branch does. Residual risk: if the pid has been recycled
        # since we recorded it, this signals an unrelated group; the window is
        # small and the alternative is a worker that runs unobserved forever.
        kill -9 -- -"$pid" 2>/dev/null
        printf '137\n' > "$OUT_DIR/$slug/exit-code"
      fi
    done < <(worker_slugs)
    [ "$live" -eq 0 ] && return 0
    [ "$block" -eq 1 ] || return 75
    sleep 2
  done
}
```

Replace the trailing dispatch with:

```bash
if [ "$MODE" = "launch" ]; then
  create_worktrees; setup_status=$?
  [ "$DRY_RUN" -eq 1 ] && exit "$setup_status"
  launch_workers
  wait_for_workers 1
fi

# Seed from create_worktrees: a worker whose bootstrap failed is never launched
# and so never appears in `pids`, but the run still failed. Starting at 0 here
# silently reported success whenever setup failed outside --dry-run.
run_status="${setup_status:-0}"
while read -r slug; do
  [ -n "$slug" ] || continue
  # Classify from `status`, not from the worker's own exit code. A worker whose
  # event log is empty exits 0 while having accomplished nothing — reading
  # exit-code here reported the run as a success for precisely the failure this
  # script exists to catch.
  case "$(cat "$OUT_DIR/$slug/status" 2>/dev/null || echo failed-launch)" in
    ok|no-changes) ;;
    *) run_status=1 ;;
  esac
done < <(roster)
exit "$run_status"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
bash -n skills/sol/scripts/sol-parallel.sh && shellcheck --severity=error skills/sol/scripts/sol-parallel.sh
python3 skills/sol/scripts/tests/test_sol_parallel.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/sol/scripts/sol-parallel.sh skills/sol/scripts/tests/test_sol_parallel.py
git commit -m "Launch one codex worker per brief and wait on all of them"
```

---

## Task 4: Post-processing — empty-log assertion, session id, commit, summary.json

**Files:**
- Modify: `skills/sol/scripts/sol-parallel.sh` (add `post_process`, `write_summary`)
- Modify: `skills/sol/scripts/tests/test_sol_parallel.py` (append a `summary` section)

**Interfaces:**
- Consumes: `exit-code`, `events.jsonl`, worktree paths from Task 3.
- Produces: `<run-dir>/workers/<slug>/{session-id,status}`; `<run-dir>/summary.json` with the exact schema in the spec. `status` ∈ `ok` · `no-changes` · `failed-launch` · `failed-run` · `failed-setup` · `failed-commit` · `timed-out`.

- [ ] **Step 1: Write the failing test**

Append to `test_sol_parallel.py`:

```python
print("summary")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha", "beta")

    r = run(repo, bin_dir, "--workers", "2", str(run_dir))
    check(r.returncode == 0, "run exits 0")

    summary = json.loads((run_dir / "summary.json").read_text())
    check(summary["base_branch"] == "main", "summary records base_branch")
    check(len(summary["workers"]) == 2, "summary has one entry per worker")
    check([w["slug"] for w in summary["workers"]] == ["alpha", "beta"],
          "workers are in task order, not completion order")

    alpha = summary["workers"][0]
    check(alpha["status"] == "ok", "clean worker is status ok")
    check(alpha["branch"] == "sol/alpha", "records the branch")
    check(alpha["session_id"] == "019f-alpha", "extracts thread_id as session_id")
    check(alpha["files_changed"] == ["alpha.txt"], "records changed files")
    check(len(alpha["commit"]) >= 7, "records the commit sha")

    # exactly one commit on the branch, and the base branch is untouched
    log = git(repo, "log", "--oneline", "main..sol/alpha")
    check(len(log.splitlines()) == 1, "exactly one commit per branch")
    check(git(repo, "rev-parse", "main") == summary["base_sha"], "base branch untouched")
    check((run_dir / "workers" / "alpha" / "session-id").read_text().strip() == "019f-alpha",
          "session-id file written for --resume")

print("summary: failure statuses")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "empty")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_EMPTY"] = "1"
    subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
                   cwd=repo, capture_output=True, text=True, check=False, env=env)
    s = json.loads((run_dir / "summary.json").read_text())
    check(s["workers"][0]["status"] == "failed-launch",
          "empty event log is failed-launch, not success")

    repo2 = make_repo(root / "repo2")
    run_dir2 = root / "run2"
    write_tasks(run_dir2, "quiet")
    env2 = dict(os.environ)
    env2["PATH"] = f"{bin_dir}{os.pathsep}{env2['PATH']}"
    env2["FAKE_NOCHANGE"] = "1"
    subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir2)],
                   cwd=repo2, capture_output=True, text=True, check=False, env=env2)
    s = json.loads((run_dir2 / "summary.json").read_text())
    check(s["workers"][0]["status"] == "no-changes", "a worker that changed nothing is no-changes")
    check(s["workers"][0]["commit"] == "", "no-changes worker has no commit")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 skills/sol/scripts/tests/test_sol_parallel.py`
Expected: FAIL — `summary.json` does not exist, raising `FileNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```bash
json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")'
}

post_process() {
  local i slug wt w code status session commit files
  for i in "${!SLUGS[@]}"; do
    slug="${SLUGS[i]}"; wt="${WORKTREES[i]}"; w="$OUT_DIR/$slug"

    if [ -f "$w/status" ] && [ "$(cat "$w/status")" = "failed-setup" ]; then
      continue
    fi

    code="$(cat "$w/exit-code" 2>/dev/null || echo 1)"
    session=""; commit=""; files=""

    if [ ! -s "$w/events.jsonl" ]; then
      status="failed-launch"
    else
      session="$(head -1 "$w/events.jsonl" \
        | python3 -c 'import json,sys; print(json.loads(sys.stdin.readline() or "{}").get("thread_id",""))' \
        2>/dev/null)"
      printf '%s\n' "$session" > "$w/session-id"
      if [ "$code" = "124" ]; then
        status="timed-out"
      elif [ "$code" != "0" ]; then
        status="failed-run"
      elif [ -z "$(git -C "$wt" status --porcelain)" ]; then
        status="no-changes"
      else
        status="ok"
      fi
    fi

    if [ "$status" = "ok" ] && [ -z "$commit" ]; then
      git -C "$wt" add -A >/dev/null 2>&1
      if git -C "$wt" commit -q -m "sol: $slug" >/dev/null 2>&1; then
        commit="$(git -C "$wt" rev-parse HEAD)"
      else
        # `rev-parse HEAD` still succeeds when the commit was rejected (a hook,
        # a bad identity), returning the BASE sha — which looks like a real
        # commit and reported the worker as ok with its work stranded uncommitted.
        status="failed-commit"
        commit=""
      fi
    fi

    # `status` last: the re-attach idempotency guard treats its presence as
    # "already processed", so an interruption after it but before these two
    # would strand the worker with a correct status and empty metadata.
    printf '%s\n' "$files" > "$w/files-changed"
    printf '%s\n' "$commit" > "$w/commit"
    printf '%s\n' "$status" > "$w/status"
  done
}

write_summary() {
  RUN_DIR="$RUN_DIR" OUT_DIR="$OUT_DIR" \
  SLUG_LIST="$(printf '%s\n' "${SLUGS[@]}")" \
  WT_LIST="$(printf '%s\n' "${WORKTREES[@]}")" \
  BRIEF_LIST="$(printf '%s\n' "${BRIEF_OF[@]}")" \
  python3 - <<'PY'
import json, os, pathlib

run_dir = pathlib.Path(os.environ["RUN_DIR"])
out_dir = pathlib.Path(os.environ["OUT_DIR"])
slugs = os.environ["SLUG_LIST"].split("\n")
wts = os.environ["WT_LIST"].split("\n")
briefs = os.environ["BRIEF_LIST"].split("\n")

def read(p, default=""):
    try:
        return p.read_text().strip()
    except OSError:
        return default

base_branch, _, base_sha = read(run_dir / "base").partition("\t")
workers = []
for slug, wt, brief in zip(slugs, wts, briefs):
    w = out_dir / slug
    started = read(w / "started-at")
    files = [f for f in read(w / "files-changed").split("\n") if f]
    workers.append({
        "slug": slug,
        "branch": f"sol/{slug}",
        "worktree": wt,
        "brief": brief,
        "status": read(w / "status", "failed-launch") or "failed-launch",
        "exit_code": int(read(w / "exit-code") or 1),
        "session_id": read(w / "session-id"),
        "commit": read(w / "commit"),
        "files_changed": files,
        "elapsed_seconds": int(read(w / "elapsed") or 0),
        "events_path": str(w / "events.jsonl"),
        "report_path": str(w / "report.md"),
        "stderr_path": str(w / "stderr.txt"),
    })

(run_dir / "summary.json").write_text(json.dumps(
    {"base_branch": base_branch, "base_sha": base_sha, "workers": workers},
    indent=2) + "\n")
PY
}
```

Record elapsed at the end of `post_process`, per worker, before writing status:

```bash
    # Cumulative since base, computed from the commit rather than from the
    # pre-commit porcelain: the resumed-no-op rung never touches the worktree,
    # and porcelain quotes filenames containing a double quote. This is the
    # branch's whole diff, which is what a reviewer of it wants.
    # `-z`, not `-c core.quotePath=false`: that setting only stops quoting for
    # non-ASCII bytes, while git always backslash-escapes a literal double quote
    # in its default text output. `-z` gives unquoted NUL-delimited names.
    if [ "$status" = "ok" ]; then
      files="$(git -C "$wt" diff --name-only -z \
        "$(cut -f2 "$RUN_DIR/base")" HEAD | tr '\0' '\n')"
    fi

    if [ -f "$w/started-at" ]; then
      printf '%s\n' "$(( $(date +%s) - $(cat "$w/started-at") ))" > "$w/elapsed"
    fi
```

Call both in the dispatch, after `wait_for_workers 1`:

```bash
  post_process
  write_summary
```

Delete the now-unused `json_escape` helper if shellcheck flags it — the summary is
written by Python, which handles escaping correctly.

- [ ] **Step 4: Run test to verify it passes**

```bash
bash -n skills/sol/scripts/sol-parallel.sh && shellcheck --severity=error skills/sol/scripts/sol-parallel.sh
python3 skills/sol/scripts/tests/test_sol_parallel.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/sol/scripts/sol-parallel.sh skills/sol/scripts/tests/test_sol_parallel.py
git commit -m "Post-process workers into one commit per branch and a summary.json"
```

---

## Task 5: `--wait` re-attach mode

**Files:**
- Modify: `skills/sol/scripts/sol-parallel.sh`
- Modify: `skills/sol/scripts/tests/test_sol_parallel.py`

**Interfaces:**
- Consumes: `<run-dir>/pids`, `base`, and the `workers/<slug>/` tree from Tasks 3–4.
- Produces: `--wait <run-dir>` → 75 while any worker lives; otherwise post-processes, rewrites `summary.json`, and returns 0/1. Rebuilds `SLUGS`/`WORKTREES`/`BRIEF_OF` from disk, since it runs in a fresh process.

- [ ] **Step 1: Write the failing test**

```python
print("--wait re-attach")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "slow")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_SLEEP"] = "6"
    launcher = subprocess.Popen(
        ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
        cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    import time
    for _ in range(50):                       # wait for the pids file to appear
        if (run_dir / "pids").is_file():
            break
        time.sleep(0.1)

    r = run(repo, bin_dir, "--wait", str(run_dir))
    check(r.returncode == 75, f"--wait returns 75 while a worker is live (got {r.returncode})")

    launcher.wait(timeout=60)
    r = run(repo, bin_dir, "--wait", str(run_dir))
    check(r.returncode == 0, "--wait returns 0 once every worker has finished")
    s = json.loads((run_dir / "summary.json").read_text())
    check(s["workers"][0]["status"] == "ok", "--wait leaves a correct summary")

    r = run(repo, bin_dir, "--wait", str(root / "nope"))
    check(r.returncode == 2, "--wait on an unknown run dir exits 2")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 skills/sol/scripts/tests/test_sol_parallel.py`
Expected: FAIL — `--wait` currently falls through and exits 0/1 immediately, never 75.

- [ ] **Step 3: Write minimal implementation**

```bash
# The full roster of the run, as opposed to worker_slugs() which is only the
# batch currently being waited on. These differ under --resume, where `pids` is
# rewritten to just the corrected workers; rehydrating from `pids` there would
# silently drop every other worker from summary.json.
roster() {
  if [ -f "$RUN_DIR/pids.all" ]; then cut -f1 "$RUN_DIR/pids.all"; else worker_slugs; fi
}

rehydrate() {
  local slug
  SLUGS=(); WORKTREES=(); BRIEF_OF=()
  while read -r slug; do
    [ -n "$slug" ] || continue
    SLUGS+=("$slug")
    WORKTREES+=("$WORKTREE_ROOT/$slug")
    BRIEF_OF+=("$(cat "$OUT_DIR/$slug/brief" 2>/dev/null)")
  done < <(roster)
}
```

Because `roster` reads `pids.all`, `launch_workers` must write that file — Task 6 adds
the `cp` that does so. Until then `roster` falls back to `pids`, which is identical
during a launch, so Task 5's tests pass either way.

In the dispatch, before the launch branch:

```bash
if [ "$MODE" = "wait" ]; then
  [ -f "$RUN_DIR/pids" ] || die "no pids file in $RUN_DIR"
  rehydrate
  wait_for_workers 0 || exit 75
  post_process
  write_summary
fi
```

- [ ] **Step 4: Run test to verify it passes**

```bash
bash -n skills/sol/scripts/sol-parallel.sh && shellcheck --severity=error skills/sol/scripts/sol-parallel.sh
python3 skills/sol/scripts/tests/test_sol_parallel.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/sol/scripts/sol-parallel.sh skills/sol/scripts/tests/test_sol_parallel.py
git commit -m "Add --wait re-attach so a run can outlive the caller's timeout"
```

---

## Task 6: `--resume` correction batches

**Files:**
- Modify: `skills/sol/scripts/sol-parallel.sh`
- Modify: `skills/sol/scripts/tests/test_sol_parallel.py`

**Interfaces:**
- Consumes: `session-id` per worker and `<run-dir>/workers/<slug>/correction.md` written by Claude.
- Produces: `--resume <run-dir>` → resumes only workers with a `correction.md`, in parallel, by explicit session id, with the worktree as cwd. Consumes each `correction.md` (renamed to `correction-<n>.md`) so a re-run cannot re-send it.

- [ ] **Step 1: Write the failing test**

The fake codex must record the flags it was given so the test can assert none of the
forbidden ones appear. Extend `FAKE_CODEX`, just after the arg loop:

```bash
[ -n "${FAKE_ARGLOG:-}" ] && printf '%s\n' "$*" >> "$FAKE_ARGLOG"
```

```python
print("--resume")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha", "beta")
    run(repo, bin_dir, "--workers", "2", str(run_dir))

    (run_dir / "workers" / "alpha" / "correction.md").write_text(
        "alpha.txt:1 — wrong value. Required: 2. `test` must pass.\n")

    arglog = root / "args.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_ARGLOG"] = str(arglog)
    r = subprocess.run(["bash", str(SCRIPT), "--resume", str(run_dir)],
                       cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 0, f"--resume exits 0 (stderr: {r.stderr[:200]})")

    args = arglog.read_text()
    check("resume" in args, "invokes codex exec resume")
    check("019f-alpha" in args, "resumes by explicit session id")
    check("--last" not in args, "never uses --last")
    check(" -s " not in f" {args} ", "passes no -s to resume")
    check("--color" not in args, "passes no --color to resume")
    check(" -C " not in f" {args} ", "passes no -C to resume")
    check("beta" not in args, "does not resume workers without a correction")
    check(not (run_dir / "workers" / "alpha" / "correction.md").exists(),
          "consumes correction.md so it cannot be re-sent")

    r = subprocess.run(["bash", str(SCRIPT), "--resume", str(run_dir)],
                       cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 2, "exit 2 when no corrections are pending")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 skills/sol/scripts/tests/test_sol_parallel.py`
Expected: FAIL — `--resume` falls through without invoking codex; `args.log` is missing.

- [ ] **Step 3: Write minimal implementation**

```bash
resume_workers() {
  local slug w pid n pending=0
  : > "$RUN_DIR/pids"
  while read -r slug; do
    [ -n "$slug" ] || continue
    w="$OUT_DIR/$slug"
    [ -f "$w/correction.md" ] || continue
    if [ ! -s "$w/session-id" ]; then
      # Never launched (failed-setup) or its event log was empty, so there is
      # no session to resume. `codex exec resume ""` would be nonsense.
      printf 'sol-parallel: %s: no session id, cannot resume\n' "$slug" >&2
      continue
    fi
    pending=$((pending + 1))
    n=1
    while [ -f "$w/correction-$n.md" ]; do n=$((n + 1)); done
    mv "$w/correction.md" "$w/correction-$n.md"
    date +%s > "$w/started-at"
    # Clear the terminal state. `post_process` skips any worker that already
    # has a `status` file — the guard that stops a re-attach from downgrading a
    # committed worker to `no-changes` — so a resumed worker that kept its old
    # status would be skipped forever and never reclassified.
    rm -f "$w/exit-code" "$w/status" "$w/files-changed" "$w/commit"
    nohup bash -c '
      cd "$3" || exit 2
      codex exec resume "$6" --json -m "$1" -c model_reasoning_effort="$2" \
        -o "$4/report.md" "$(cat "$5")" \
        > "$4/events.jsonl" 2> "$4/stderr.txt"
      printf "%s\n" "$?" > "$4/exit-code"
    ' _ "$MODEL" "$EFFORT" "$WORKTREE_ROOT/$slug" "$w" "$w/correction-$n.md" \
        "$(cat "$w/session-id")" >/dev/null 2>&1 &
    pid=$!
    disown "$pid" 2>/dev/null
    printf '%s\t%s\n' "$slug" "$pid" >> "$RUN_DIR/pids"
  done < <(roster)
  [ "$pending" -gt 0 ] || die "no correction.md found in $OUT_DIR/*/"
}
```

`resume_workers` reads the roster from `pids.all` (via `roster`) but rewrites `pids` to
only the resumed workers, so `wait_for_workers` blocks on exactly those while
`rehydrate`/`post_process`/`write_summary` still cover every worker in the run.

The final exit-code classification loop must iterate `roster` too, for the same reason:
after a resume, `pids` holds only the corrected workers, so classifying from it would
let a run exit 0 while a *different* worker sat in `failed-run`. That is the same
detected-but-not-propagated failure this plan has already shipped three times.

`resume_workers` rewrites `pids` to only the resumed workers, so `wait_for_workers`
blocks on exactly those. It reads the full roster from `pids.all`, which the launch
path must now also write — add to `launch_workers`, right after its loop:

```bash
  cp "$RUN_DIR/pids" "$RUN_DIR/pids.all"
```

Note the worktree is passed as the child's **cwd** (`cd "$3"`), because
`codex exec resume` accepts no `-C`. In the dispatch:

```bash
if [ "$MODE" = "resume" ]; then
  [ -f "$RUN_DIR/pids.all" ] || die "no completed run in $RUN_DIR"
  resume_workers
  wait_for_workers 1
  rehydrate
  post_process
  write_summary
fi
```

`post_process` must not skip an already-committed worktree: change its `no-changes`
branch to compare against the branch tip rather than assuming an uncommitted diff — a
resumed worker whose worktree is clean after committing is still `ok` if its branch has
moved. Replace the status ladder's clean-tree test with:

```bash
      elif [ -z "$(git -C "$wt" status --porcelain)" ] \
        && [ "$(git -C "$wt" rev-parse HEAD)" = "$(cut -f2 "$RUN_DIR/base")" ]; then
        status="no-changes"
      elif [ -z "$(git -C "$wt" status --porcelain)" ]; then
        # Clean tree but the branch has already moved past base: a resumed
        # worker that committed earlier and made no further edits this round.
        # Committing anyway finds "nothing to commit" and misreports a real
        # success as failed-commit.
        status="ok"
        commit="$(git -C "$wt" rev-parse HEAD)"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
bash -n skills/sol/scripts/sol-parallel.sh && shellcheck --severity=error skills/sol/scripts/sol-parallel.sh
python3 skills/sol/scripts/tests/test_sol_parallel.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/sol/scripts/sol-parallel.sh skills/sol/scripts/tests/test_sol_parallel.py
git commit -m "Add --resume correction batches, resuming by explicit session id"
```

---

## Task 7: `--cleanup`

**Files:**
- Modify: `skills/sol/scripts/sol-parallel.sh`
- Modify: `skills/sol/scripts/tests/test_sol_parallel.py`

**Interfaces:**
- Consumes: `summary.json`, `<run-dir>/base`.
- Produces: `--cleanup <run-dir>` → removes the worktree and deletes the branch for every worker whose branch is fully merged into the base branch; runs `git worktree prune`; prints one `kept: sol/<slug> <worktree-path> (<reason>)` line per survivor to stdout; exits 0.

- [ ] **Step 1: Write the failing test**

```python
print("--cleanup")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "merged", "kept")
    run(repo, bin_dir, "--workers", "2", str(run_dir))

    git(repo, "cherry-pick", "sol/merged")     # simulate Claude integrating one branch

    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup exits 0 (stderr: {r.stderr[:200]})")
    check(not (root / ".sol-worktrees" / "repo" / "merged").exists(),
          "removes the worktree of a merged branch")
    check(git(repo, "branch", "--list", "sol/merged") == "",
          "deletes the merged branch")
    check((root / ".sol-worktrees" / "repo" / "kept").is_dir(),
          "keeps the worktree of an unmerged branch")
    check("sol/kept" in r.stdout, "names the surviving branch on stdout")
    check(str(root / ".sol-worktrees" / "repo" / "kept") in r.stdout,
          "names the surviving worktree path")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 skills/sol/scripts/tests/test_sol_parallel.py`
Expected: FAIL — `--cleanup` does nothing; both worktrees survive and nothing is printed.

- [ ] **Step 3: Write minimal implementation**

```bash
cleanup_run() {
  local base slug wt
  base="$(cut -f1 "$RUN_DIR/base")"
  while read -r slug; do
    [ -n "$slug" ] || continue
    wt="$WORKTREE_ROOT/$slug"
    if git merge-base --is-ancestor "sol/$slug" "$base" 2>/dev/null; then
      git worktree remove --force "$wt" >/dev/null 2>&1
      git branch -q -D "sol/$slug" >/dev/null 2>&1
    else
      printf 'kept: sol/%s %s (%s)\n' \
        "$slug" "$wt" "$(cat "$OUT_DIR/$slug/status" 2>/dev/null || echo unmerged)"
    fi
  done < <(roster)
  git worktree prune
}
```

In the dispatch:

```bash
if [ "$MODE" = "cleanup" ]; then
  [ -f "$RUN_DIR/pids.all" ] || die "no completed run in $RUN_DIR"
  cleanup_run
  exit 0
fi
```

- [ ] **Step 4: Run test to verify it passes**

```bash
bash -n skills/sol/scripts/sol-parallel.sh && shellcheck --severity=error skills/sol/scripts/sol-parallel.sh
python3 skills/sol/scripts/tests/test_sol_parallel.py
```
Expected: PASS — and this is the last script task, so also confirm the whole file is
still `chmod +x`: `test -x skills/sol/scripts/sol-parallel.sh && echo executable`.

- [ ] **Step 5: Commit**

```bash
git add skills/sol/scripts/sol-parallel.sh skills/sol/scripts/tests/test_sol_parallel.py
git commit -m "Add --cleanup: remove merged worktrees, name every survivor"
```

---

## Task 8: Preflight check for the worktree root

**Files:**
- Modify: `skills/sol/scripts/check-codex.sh` (add a check after the existing `git_tree_state` check)

**Interfaces:**
- Consumes: the `ok`/`note`/`hint` helpers, whose signature is `ok "<detail>" "<check_name>"`.
- Produces: a new check named `worktree_root` in both human and `--json` output.

- [ ] **Step 1: Write the failing test**

No unit test — `check-codex.sh` is verified by running it. Record the expected output:

```bash
bash skills/sol/scripts/check-codex.sh --json | python3 -c \
  "import json,sys; names=[c['name'] for c in json.load(sys.stdin)['checks']]; \
   print(names); assert 'worktree_root' in names, 'missing worktree_root'"
```
Expected: `AssertionError: missing worktree_root`.

- [ ] **Step 2: Confirm it fails**

Run the command above. Expected: the assertion fires.

- [ ] **Step 3: Write minimal implementation**

Immediately after the existing working-tree-state check:

```bash
# Parallel mode: the sibling worktree root must be creatable
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  repo_root="$(git rev-parse --show-toplevel)"
  wt_root="$(cd "$repo_root/.." && pwd)/.sol-worktrees/$(basename "$repo_root")"
  if mkdir -p "$wt_root" 2>/dev/null; then
    ok "parallel worktree root writable — $wt_root" "worktree_root"
    rmdir "$wt_root" 2>/dev/null
    rmdir "$(dirname "$wt_root")" 2>/dev/null
  else
    note "cannot create $wt_root — parallel mode (--workers) will not run" "worktree_root"
    hint "single-worker /sol is unaffected"
  fi
fi
```

`rmdir` (not `rm -rf`) so an existing worktree root with live worktrees in it is never
deleted by a preflight check — `rmdir` refuses on a non-empty directory.

- [ ] **Step 4: Verify it passes**

```bash
bash -n skills/sol/scripts/check-codex.sh && shellcheck --severity=error skills/sol/scripts/check-codex.sh
bash skills/sol/scripts/check-codex.sh
bash skills/sol/scripts/check-codex.sh --json | python3 -c \
  "import json,sys; d=json.load(sys.stdin); names=[c['name'] for c in d['checks']]; \
   assert 'worktree_root' in names; print('ok', names)"
```
Expected: human output shows the new `ok` line; JSON mode still parses and now lists
`worktree_root`.

- [ ] **Step 5: Commit**

```bash
git add skills/sol/scripts/check-codex.sh
git commit -m "Preflight: check the parallel worktree root is writable"
```

---

## Task 9: Skill instructions — routing, the parallel flow, brief additions

**Files:**
- Create: `skills/sol/references/parallel-flow.md`
- Modify: `skills/sol/SKILL.md` (add a routing paragraph after the existing "Task routing" paragraph at line 21, and a config table)
- Modify: `skills/sol/references/brief-template.md` (add a parallel section after "Implementation brief")

**Interfaces:**
- Consumes: the CLI contract from Tasks 1–7.
- Produces: the prose contract Claude follows. No code.

- [ ] **Step 1: Write `references/parallel-flow.md`**

Create it with these sections, written as instructions to the planner:

1. **When this applies** — only when the user names a worker count (`--workers N`, or "use N workers"). Never inferred. `--workers 1` uses the normal single-worker flow.
2. **Resolve the ceiling** — `--workers` > `SOL_MAX_WORKERS` > 3. If the user asks for more than the ceiling, refuse in one line naming the ceiling and `SOL_MAX_WORKERS`; do not clamp. If there are more tasks than the ceiling, run waves: one script invocation per wave, each branching from the previous wave's merged result.
3. **Split and confirm** — print the numbered split with each task's slug, one-line goal, and expected file scope; state overlaps and propose merging or sequencing; **wait for confirmation before running anything**.
4. **Write briefs** — one per task at `<run-dir>/tasks/NN-<slug>.md`, using the parallel brief template. The slug in the filename becomes the branch name.
5. **Launch** — one blocking call:
   ```bash
   bash <skill-dir>/scripts/sol-parallel.sh --workers N "$SCRATCHPAD/sol-run"
   ```
   If it exits 75 or the tool times out, re-invoke `--wait "$SCRATCHPAD/sol-run"` until it returns 0 or 1. Never launch codex directly in parallel mode.
6. **Read `summary.json`, never the raw logs** — one entry per worker, `status` ∈ `ok` · `no-changes` · `failed-launch` · `failed-run` · `failed-setup` · `failed-commit` · `timed-out`. `failed-launch` means codex itself failed: read the tail of that worker's `stderr.txt`. Every non-`ok` worker is named in the report with its status — a task that produced nothing is reported as such, never omitted.
7. **Review in task order** — `git diff --stat "$BASE".."sol/<slug>"`, then the full branch diff, to today's phase-3 standard, plus a check that the worker stayed inside its declared file scope.
8. **Integrate one branch at a time** — `git cherry-pick sol/<slug>` onto the base, then `git -C <worktree> rebase "$BASE"` for every not-yet-reviewed worker. A conflict is evidence the independence premise was wrong for that pair: report it, never auto-resolve.
9. **Correct** — write the delta to `<run-dir>/workers/<slug>/correction.md` and run `--resume <run-dir>`. Two rounds per worker, counted independently. Never construct a `codex exec resume` command by hand in parallel mode.
10. **Re-run the checks on the merged branch** — mandatory, once, after integration. This result leads the report; per-worker results are supporting detail only. Green × N is not green combined.
11. **Clean up** — `--cleanup <run-dir>`; name every branch and worktree it kept.
12. **Report** — per task (title, branch, verdict, `--stat` with a clause per file, correction rounds) then combined (merged check output, commits added, wall clock) then survivors and risks.

- [ ] **Step 2: Add routing to `SKILL.md`**

After the existing "**Task routing:**" paragraph, insert:

```markdown
**Parallel routing:** If — and only if — the user names a worker count (`--workers N`,
or "use 3 workers"), follow `references/parallel-flow.md` instead of phases 2–5. Never
infer parallelism from a request that merely looks like several tasks; the trigger is
the number the user typed, not a judgment about the work. `--workers 1` is the normal
flow below.

| Setting | Default | Meaning |
|---|---|---|
| `--workers N` | — | Worker ceiling for this run; its presence engages parallel mode |
| `SOL_MAX_WORKERS` | `3` | Ceiling when `--workers` is absent |
| `SOL_WORKTREE_SETUP` | unset | Command run in each fresh worktree (`npm ci`, `uv sync`) |
```

- [ ] **Step 3: Add the parallel brief block to `brief-template.md`**

After the "Implementation brief" section:

````markdown
## Parallel brief additions

In parallel mode every worker shares the repo, so each brief adds a scope fence to
`<non_goals>` and forbids git operations the launcher owns:

```xml
<non_goals>
- Touch only these paths: [the task's declared file scope]. Another worker owns the rest
  of this repo right now.
- Do not commit, do not create or switch branches, do not run git rebase or merge.
  The launcher commits your work on its own branch.
</non_goals>
```
````

- [ ] **Step 4: Verify**

```bash
grep -c 'SOL_MAX_WORKERS' skills/sol/SKILL.md               # expect 1
test -f skills/sol/references/parallel-flow.md && echo ok
grep -q 'do not create or switch branches' skills/sol/references/brief-template.md && echo ok
```
Expected: `1`, `ok`, `ok`.

- [ ] **Step 5: Commit**

```bash
git add skills/sol/SKILL.md skills/sol/references/parallel-flow.md skills/sol/references/brief-template.md
git commit -m "Document the opt-in parallel flow and its brief contract"
```

---

## Task 10: README, changelog, and the 1.3.0 bump

**Files:**
- Modify: `README.md` (a "Parallel workers" subsection after "Summarizing a run"; amend the "Changing the defaults" claim that there is no config file)
- Modify: `CHANGELOG.md` (a `## [1.3.0] — 2026-08-10` entry above `## [1.2.1]`, plus the link line at the bottom)
- Modify: `.claude-plugin/plugin.json` and `skills/sol/SKILL.md` (`version` → `1.3.0`)

**Interfaces:**
- Consumes: behavior from Tasks 1–9.
- Produces: user-facing docs. No code.

- [ ] **Step 1: Add the README section**

After "Summarizing a run", before "**5. Report.**":

```markdown
### Parallel workers

Several *independent* tasks can run at once, one Codex session and one git worktree each.
It is opt-in per run and never inferred — the trigger is a worker count you type:

```
/sol --workers 3 rate-limit the upload endpoint, add --json to the preflight, and write the 1.3.0 changelog
```

Claude shows you the split and the file scope of each task and waits before creating
anything. Then one blocking call fans out N `codex exec` workers, each in its own
worktree on branch `sol/<slug>`. Claude reviews each diff, cherry-picks the approved
branches onto your branch one at a time — one commit per task — and **re-runs your
checks once on the merged result.** That last part is the point: each worker verified
green against a base without the other workers' changes, so N green isolated runs are
not a green integrated run, and the report leads with the combined result.

A task that fails review gets the normal two correction rounds in its own worktree while
the others merge; anything still failing leaves its branch behind, named in the report.

Defaults: worker ceiling `3`, raise with `SOL_MAX_WORKERS`. Set `SOL_WORKTREE_SETUP` to
a command (`npm ci`, `uv sync`) if a fresh worktree needs bootstrapping — worktrees do
not inherit gitignored files, though `/sol` copies your `.env` files in for you.
```

Then amend "Changing the defaults" — the sentence "There's no config file; you change
behavior by editing it" becomes:

```markdown
The whole skill is one readable markdown file: [`skills/sol/SKILL.md`](skills/sol/SKILL.md).
Behavior changes by editing it, with two exceptions that survive a skill update:
`SOL_MAX_WORKERS` and `SOL_WORKTREE_SETUP` (see [Parallel workers](#parallel-workers)).
```

- [ ] **Step 2: Add the changelog entry**

```markdown
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
```

Add the link line at the bottom, above the `[1.2.1]` line:

```markdown
[1.3.0]: https://github.com/ozankasikci/sol-skill/releases/tag/v1.3.0
```

- [ ] **Step 3: Bump the version in both places**

```bash
python3 - <<'PY'
import json, pathlib, re
p = pathlib.Path(".claude-plugin/plugin.json")
d = json.loads(p.read_text()); d["version"] = "1.3.0"
p.write_text(json.dumps(d, indent=2) + "\n")
s = pathlib.Path("skills/sol/SKILL.md")
s.write_text(re.sub(r'^version:.*$', 'version: "1.3.0"', s.read_text(), count=1, flags=re.M))
PY
```

- [ ] **Step 4: Run the full validation suite**

```bash
python3 -c "import json; json.load(open('.claude-plugin/plugin.json'))"
plugin=$(python3 -c "import json; print(json.load(open('.claude-plugin/plugin.json'))['version'])")
skill=$(sed -n 's/^version:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' skills/sol/SKILL.md | head -1)
test "$plugin" = "$skill" && echo "version ok: $plugin"
for f in skills/sol/scripts/*.sh; do bash -n "$f" && test -x "$f" && echo "ok $f"; done
shellcheck --severity=error skills/sol/scripts/*.sh
python3 skills/sol/scripts/tests/test_sol_watch.py
python3 skills/sol/scripts/tests/test_sol_parallel.py
```
Expected: `version ok: 1.3.0`, every script `ok`, shellcheck silent, both test files
printing `all checks passed`.

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md .claude-plugin/plugin.json skills/sol/SKILL.md
git commit -m "Document parallel workers and release 1.3.0"
```

---

## Final verification against the spec's acceptance criteria

Run these after Task 10. Each maps to a numbered criterion in the spec.

- [ ] **AC1 — no regression.** `/sol <task>` with no worker count creates no worktree and
  no branch: `git worktree list` shows only the repo, `git branch --list 'sol/*'` is empty.
- [ ] **AC2 — confirmation gate.** Covered by `references/parallel-flow.md` step 3;
  verify by inspection that no script invocation precedes the confirmation.
- [ ] **AC3 — ceiling refusal.** `SOL_MAX_WORKERS=2 bash skills/sol/scripts/sol-parallel.sh --workers 5 /tmp/x`
  exits 2 naming `SOL_MAX_WORKERS`, and no `codex` process starts.
- [ ] **AC4 — clean integration.** Covered by the `summary` and `--cleanup` tests.
- [ ] **AC5 — empty log is a failure.** Covered by the `failed-launch` test.
- [ ] **AC6 — resume hygiene.** Covered by the `--resume` argument-log assertions.
- [ ] **AC7 — combined re-run leads.** Covered by `parallel-flow.md` step 10; verify by
  inspection that the report contract names the merged-branch result first.
- [ ] **AC8 — tests pass offline.** `python3 skills/sol/scripts/tests/test_sol_parallel.py`
  with no network. (Corrected from the spec's `pytest`.)
