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
[ -n "${FAKE_ARGLOG:-}" ] && printf '%s\n' "$*" >> "$FAKE_ARGLOG"
cat >/dev/null                      # consume the brief
slug="$(basename "$(dirname "$out_file")")"
# Each worker stamps its own start and end when FAKE_STAMPS is set, so a test
# can assert concurrency directly (do the intervals overlap?) instead of racing
# a wall clock. `date +%s.%N` is GNU-only -- BSD date prints a literal N -- so
# go through python3, which every other part of this suite already needs.
# Short O_APPEND writes from concurrent processes do not interleave.
stamp() {
  [ -n "${FAKE_STAMPS:-}" ] || return 0
  python3 -c 'import sys,time; print(sys.argv[1], sys.argv[2], time.time())' \
    "$slug" "$1" >> "$FAKE_STAMPS"
}
stamp start
# Stall simulation for the watchdog tests. FAKE_STALL_MODE emits the exact
# event shape of a hung real worker -- prelude bookkeeping (and optionally one
# substantive item), then silence -- and sleeps until killed. FAKE_STALL_EFFORTS
# stalls only when the invocation's reasoning effort is in the list, so a
# downgraded retry proceeds normally.
effort=""
for arg in "$@"; do
  case "$arg" in model_reasoning_effort=*) effort="${arg#model_reasoning_effort=}" ;; esac
done
want_stall=""
[ -n "${FAKE_STALL_MODE:-}" ] && want_stall=1
if [ -n "${FAKE_STALL_EFFORTS:-}" ]; then
  case " $FAKE_STALL_EFFORTS " in
    *" $effort "*) want_stall=1 ;;
    *) want_stall="" ;;
  esac
fi
if [ -n "$want_stall" ]; then
  printf '{"type":"thread.started","thread_id":"%s"}\n' "${FAKE_THREAD:-019f-$slug}"
  printf '{"type":"turn.started"}\n'
  if [ "${FAKE_STALL_MODE:-prelude}" = "after-item" ]; then
    printf '{"type":"item.completed","item":{"id":"item_0","type":"command_execution"}}\n'
  fi
  if [ "${FAKE_STALL_MODE:-prelude}" = "in-command" ]; then
    # A command that started and never completes: silence here is a running
    # (possibly hung) command, never a codex transport stall.
    printf '{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"slow-build"}}\n'
  fi
  sleep "${FAKE_STALL_SLEEP:-600}"
  exit 0
fi
# Quiet-command mode: a real build in miniature -- the command starts, runs
# silently for FAKE_QUIET_CMD_SECS, completes, and the worker finishes ok.
if [ -n "${FAKE_QUIET_CMD_SECS:-}" ]; then
  printf '{"type":"thread.started","thread_id":"%s"}\n' "${FAKE_THREAD:-019f-$slug}"
  printf '{"type":"turn.started"}\n'
  printf '{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"quiet-build"}}\n'
  sleep "$FAKE_QUIET_CMD_SECS"
  printf '{"type":"item.completed","item":{"id":"item_0","type":"command_execution","exit_code":0}}\n'
fi
# Heartbeat mode: a slow but healthy worker that proves liveness by emitting a
# substantive event every second before finishing normally.
if [ -n "${FAKE_HEARTBEAT_SECS:-}" ]; then
  printf '{"type":"thread.started","thread_id":"%s"}\n' "${FAKE_THREAD:-019f-$slug}"
  printf '{"type":"turn.started"}\n'
  hb=0
  while [ "$hb" -lt "$FAKE_HEARTBEAT_SECS" ]; do
    printf '{"type":"item.completed","item":{"type":"reasoning","beat":%d}}\n' "$hb"
    sleep 1
    hb=$((hb + 1))
  done
fi
[ -n "${FAKE_SLEEP:-}" ] && sleep "$FAKE_SLEEP"
stamp end
if [ -z "${FAKE_EMPTY:-}" ]; then
  printf '{"type":"thread.started","thread_id":"%s"}\n' "${FAKE_THREAD:-019f-$slug}"
  printf '{"type":"turn.completed"}\n'
fi
[ -n "$out_file" ] && printf 'fake report for %s\n' "$slug" > "$out_file"
if [ -z "${FAKE_NOCHANGE:-}" ]; then
  printf 'touched by %s\n' "$slug" > "$cd_dir/${FAKE_FILENAME:-$slug.txt}"
fi
exit "${FAKE_EXIT:-0}"
"""


def install_fake_codex(bin_dir: pathlib.Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "codex"
    shim.write_text(FAKE_CODEX)
    shim.chmod(0o755)


# A test seam, not a mock of anything the script cares about. sol-parallel.sh
# shells out to `awk` in exactly one place -- pid_of() -- which runs *between*
# wait_for_workers' `[ -s exit-code ]` test and its `kill -0 "$pid"`. That gap is
# the race window. Blocking inside it until the worker has written its exit code
# and exited puts the loop into the window deterministically, on every run,
# instead of hoping to land in it under load. The worker itself is untouched: it
# succeeds exactly as it otherwise would.
FAKE_AWK = r"""#!/usr/bin/env bash
set -uo pipefail
out="$(__REAL_AWK__ "$@")"
pid="$(printf '%s' "$out" | head -1 | tr -d '[:space:]')"
case "$pid" in
  ''|*[!0-9]*) printf '%s\n' "$out"; exit 0 ;;
esac
i=0
while [ "$i" -lt 300 ] && kill -0 "$pid" 2>/dev/null; do
  sleep 0.1; i=$((i + 1))
done
if [ -n "${FAKE_AWK_LOG:-}" ]; then
  if kill -0 "$pid" 2>/dev/null; then
    printf 'alive %s\n' "$pid" >> "$FAKE_AWK_LOG"
  else
    printf 'dead %s\n' "$pid" >> "$FAKE_AWK_LOG"
  fi
fi
printf '%s\n' "$out"
"""


def install_fake_awk(bin_dir: pathlib.Path) -> None:
    real = shutil.which("awk")
    assert real, "no real awk on PATH"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "awk"
    shim.write_text(FAKE_AWK.replace("__REAL_AWK__", real))
    shim.chmod(0o755)


def write_tasks(run_dir: pathlib.Path, *slugs: str) -> None:
    tasks = run_dir / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    for i, slug in enumerate(slugs, start=1):
        (tasks / f"{i:02d}-{slug}.md").write_text(f"<task>do {slug}</task>\n")


def run(repo: pathlib.Path, bin_dir: pathlib.Path, *args: str, timeout: float | None = None):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("SOL_MAX_WORKERS", None)
    env.pop("SOL_WORKTREE_SETUP", None)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo, capture_output=True, text=True, check=False, env=env,
        timeout=timeout,
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
    # run_dir lives outside the repo (as it does in real use, e.g. a scratchpad
    # dir) so writing briefs into it never dirties the repo's working tree.
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

    # --workers with no value must not hang: `shift 2` silently no-ops when
    # only one arg remains (no `set -e`), so a naive parser loops forever.
    # A `timeout=` here means a regression fails this suite instead of
    # wedging it.
    try:
        r = run(repo, bin_dir, "--workers", timeout=10)
        check(r.returncode == 2, "exit 2 when --workers has no value")
    except subprocess.TimeoutExpired:
        check(False, "exit 2 when --workers has no value (timed out instead of exiting)")

    # clean tree: since run_dir is outside the repo, `git status --porcelain`
    # is empty here and the script should get past the dirty-tree gate.
    r = run(repo, bin_dir, "--workers", "2", str(run_dir))
    check(r.returncode == 0, "clean tree passes precondition checks")

    # dirty tree
    (repo / "dirty.txt").write_text("x")
    r = run(repo, bin_dir, "--workers", "2", str(run_dir))
    check(r.returncode == 2, "exit 2 on a dirty working tree")
    (repo / "dirty.txt").unlink()

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

    # a pre-existing branch is a hard stop. Remove both worktrees but keep
    # branch sol/beta and delete sol/alpha, so the batch collides on its
    # SECOND brief while `alpha` (first, non-colliding) is the one that must
    # never be created.
    #
    # This ordering is deliberate, not incidental: `git worktree add -b` also
    # fails on its own when a branch already exists, so if the collision were
    # on the FIRST brief, a naive implementation relying solely on that
    # failure would abort on iteration 1 anyway, before ever touching the
    # second brief -- masking the missing pre-check. Putting the collision on
    # the SECOND brief is what actually exercises it: without an explicit
    # check across the whole batch *before* any worktree is created, the
    # naive path creates `alpha` (which doesn't collide) first, then only
    # fails once it reaches `beta`.
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

print("empty slug names")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "!!!", "???")

    wt_root = root / ".sol-worktrees" / "repo"

    r = run(repo, bin_dir, "--workers", "2", "--dry-run", str(run_dir))
    check(r.returncode == 0,
          f"brief names that reduce to nothing still succeed (stderr: {r.stderr[:200]})")
    check((wt_root / "task").is_dir(), "an empty slug falls back to 'task'")
    check((wt_root / "task-2").is_dir(),
          "a second empty slug becomes 'task-2', not a duplicate or 'sol/'")
    check("sol/task" in git(repo, "branch", "--list", "sol/task"),
          "creates branch sol/task, never the invalid sol/")
    check("sol/task-2" in git(repo, "branch", "--list", "sol/task-2"),
          "creates branch sol/task-2")

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

    # Same failing hook, but without --dry-run: create_worktrees's failure
    # must not be dropped once the run proceeds past worktree setup. The
    # failed worker never lands in `pids` (launch_workers skips it), so the
    # final status loop -- which only walks `pids` -- has nothing to see it
    # in; the run's exit code has to come from create_worktrees's own status.
    run_dir3 = root / "run3"
    write_tasks(run_dir3, "delta")
    r = subprocess.run(
        ["bash", str(SCRIPT), "--workers", "1", str(run_dir3)],
        cwd=repo, capture_output=True, text=True, check=False, env=env,
    )
    check(r.returncode == 1,
          "a failing setup hook fails the whole run even without --dry-run")
    check("failed-setup" in (r.stdout + r.stderr), "reports failed-setup (non-dry-run)")

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

    # Workers really are concurrent. Asserted from the workers' OWN start/end
    # stamps, not from the launcher's wall clock: the run's wall time also
    # carries worktree creation, launch, and a tail of up to one 2s poll after
    # the last worker exits, so no wall-clock bound both fits an idle machine
    # and survives a loaded one. Overlapping intervals prove concurrency
    # directly and cannot flake under load -- a serialised launcher produces
    # disjoint intervals no matter how slow or fast the box is.
    repo2 = make_repo(root / "repo2")
    run_dir2 = root / "run2"
    write_tasks(run_dir2, "one", "two")
    stamps = root / "stamps.txt"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_SLEEP"] = "2"
    env["FAKE_STAMPS"] = str(stamps)
    r = subprocess.run(
        ["bash", str(SCRIPT), "--workers", "2", str(run_dir2)],
        cwd=repo2, capture_output=True, text=True, check=False, env=env,
    )
    check(r.returncode == 0, "concurrent run exits 0")

    spans: dict[str, dict[str, float]] = {}
    for line in stamps.read_text().splitlines() if stamps.exists() else []:
        parts = line.split()
        if len(parts) == 3:
            spans.setdefault(parts[0], {})[parts[1]] = float(parts[2])
    check(
        all(slug in spans and {"start", "end"} <= set(spans[slug]) for slug in ("one", "two")),
        f"both workers recorded a start and an end stamp (got {spans!r})",
    )
    if all(slug in spans and {"start", "end"} <= set(spans[slug]) for slug in ("one", "two")):
        one, two = spans["one"], spans["two"]
        overlap = min(one["end"], two["end"]) - max(one["start"], two["start"])
        check(
            overlap > 0,
            f"the two workers' run intervals overlap, i.e. they really ran at the "
            f"same time rather than one after the other "
            f"(overlap {overlap:.2f}s; one=[{one['start']:.2f},{one['end']:.2f}] "
            f"two=[{two['start']:.2f},{two['end']:.2f}])",
        )
    else:
        check(False, "the two workers' run intervals overlap (no stamps to compare)")

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

print("mtime_of is portable")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    target = root / "probe.txt"
    target.write_text("x")
    # Extract the helper and call it directly. The behavioural stall tests only
    # catch a broken mtime_of on the platform they happen to run on: BSD-first
    # ordering passed every macOS run while returning a filesystem dump on
    # Linux, which reached `[ -gt ]` as a syntax error and disabled the whole
    # inactivity watchdog there. This asserts the contract itself — an epoch —
    # so either platform catches a regression.
    src = SCRIPT.read_text()
    start = src.index("mtime_of() {")
    end = src.index("\n}", start) + 2
    helper = root / "mtime_of.sh"
    helper.write_text(src[start:end] + '\nmtime_of "$1"\n')
    out = subprocess.run(["bash", str(helper), str(target)],
                         capture_output=True, text=True, check=False)
    val = out.stdout.strip()
    check(val.isdigit() and int(val) > 0,
          f"mtime_of returns an epoch, not a filesystem dump (got {val[:60]!r})")
    check(out.stderr == "", f"mtime_of is silent on stderr (got {out.stderr[:60]!r})")
    check(abs(int(val) - int(target.stat().st_mtime)) <= 1,
          "mtime_of matches the file's real mtime")

print("image sidecar")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "visual", "plain")
    shot = root / "mockup.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    rel = repo / "ref.png"
    rel.write_bytes(b"\x89PNG\r\n\x1a\n")
    # Committed, not just written: an uncommitted file would trip the clean-tree
    # precondition, which is correct behaviour and not what this test is about.
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "add reference image")
    # Absolute path, a repo-relative path, a comment and a blank line.
    (run_dir / "tasks" / "01-visual.images").write_text(
        f"# reference images\n{shot}\n\nref.png\n")
    arglog = root / "args.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    for k in ("SOL_MAX_WORKERS", "SOL_WORKTREE_SETUP", "SOL_WORKER_TIMEOUT"):
        env.pop(k, None)
    env["FAKE_ARGLOG"] = str(arglog)
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "2", str(run_dir)],
                       cwd=repo, capture_output=True, text=True, check=False, env=env,
                       timeout=120)
    check(r.returncode == 0, f"a run with an image sidecar exits 0 (stderr: {r.stderr[:200]})")
    args = arglog.read_text()
    visual = [l for l in args.splitlines() if "visual" in l]
    plain = [l for l in args.splitlines() if "plain" in l]
    check(len(visual) == 1 and f"-i {shot}" in visual[0],
          "the absolute sidecar path is passed to that worker as -i")
    # Compare on the resolved suffix, not the literal fixture path: the script
    # resolves relative entries against `git rev-parse --show-toplevel`, which on
    # macOS returns /private/var/... where the fixture says /var/... . Asserting
    # the raw path passes on Linux and fails here for a reason unrelated to the
    # behaviour under test.
    check(len(visual) == 1 and visual[0].count(" -i ") == 2
          and "/ref.png" in visual[0],
          "a repo-relative sidecar path is resolved and passed as -i")
    check("#" not in " ".join(visual), "comment lines are not passed as images")
    check(len(plain) == 1 and " -i " not in f" {plain[0]} ",
          "a worker with no sidecar gets no -i at all")

print("image sidecar: a missing image fails before anything is created")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "visual")
    (run_dir / "tasks" / "01-visual.images").write_text("does-not-exist.png\n")
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 2, f"exit 2 when a sidecar names a missing image (got {r.returncode})")
    check("does-not-exist.png" in r.stderr, "names the missing image")
    check(not (root / ".sol-worktrees").exists(),
          "fails before any worktree is created")
    check(git(repo, "branch", "--list", "sol/visual") == "",
          "fails before any branch is created")

print("--in-place")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "inplace")
    head_before = git(repo, "rev-parse", "HEAD")
    r = run(repo, bin_dir, "--workers", "1", "--in-place", str(run_dir))
    check(r.returncode == 0, f"--in-place exits 0 (stderr: {r.stderr[:200]})")
    check(not (root / ".sol-worktrees").exists(), "--in-place creates no worktree")
    check(git(repo, "branch", "--list", "sol/inplace") == "", "--in-place creates no branch")
    check(git(repo, "rev-parse", "HEAD") == head_before, "--in-place makes no commit")
    # The work lands in the user's tree, exactly as a plain single-worker run.
    check((repo / "inplace.txt").is_file(), "the worker's changes land in the repo itself")
    check("inplace.txt" in git(repo, "status", "--porcelain"),
          "changes are left uncommitted for review")
    s = json.loads((run_dir / "summary.json").read_text())
    check(s["workers"][0]["status"] == "ok", "--in-place classifies ok")
    check(s["workers"][0]["files_changed"] == ["inplace.txt"],
          f"files_changed comes from the working tree (got {s['workers'][0]['files_changed']})")
    check(s["workers"][0]["commit"] == "", "no commit sha is recorded in-place")

print("--in-place refuses what it cannot isolate")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "a", "b")
    r = run(repo, bin_dir, "--workers", "2", "--in-place", str(run_dir))
    check(r.returncode == 2, "--in-place with 2 workers exits 2")
    check("one worker" in r.stderr, "explains that in-place is single-worker only")
    run_dir2 = root / "run2"
    write_tasks(run_dir2, "a", "b")
    r = run(repo, bin_dir, "--workers", "1", "--in-place", str(run_dir2))
    check(r.returncode == 2, "--in-place with 2 briefs exits 2")

print("SOL_SANDBOX")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "sbx")
    arglog = root / "args.log"
    base_env = dict(os.environ)
    base_env["PATH"] = f"{bin_dir}{os.pathsep}{base_env['PATH']}"
    for k in ("SOL_MAX_WORKERS", "SOL_WORKTREE_SETUP", "SOL_WORKER_TIMEOUT",
              "SOL_CODEX_CONFIG", "SOL_SANDBOX"):
        base_env.pop(k, None)
    base_env["FAKE_ARGLOG"] = str(arglog)

    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", "--in-place", str(run_dir)],
                       cwd=repo, capture_output=True, text=True, check=False,
                       env=base_env, timeout=120)
    check(r.returncode == 0, f"default run exits 0 (stderr: {r.stderr[:150]})")
    check("-s workspace-write" in arglog.read_text(),
          "defaults to -s workspace-write")
    check("sandbox is" not in r.stderr, "no warning on the default sandbox")

    # An explicit -s beats -c sandbox_mode=, so this must replace the flag
    # rather than be appended as config, or it is silently ignored.
    arglog.write_text("")
    run_dir2 = root / "run2"
    write_tasks(run_dir2, "sbx2")
    # A fresh repo: the in-place run above left its worker's file uncommitted,
    # which correctly trips the clean-tree precondition on any second run here.
    repo2 = make_repo(root / "repo2")
    env = dict(base_env); env["SOL_SANDBOX"] = "danger-full-access"
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", "--in-place", str(run_dir2)],
                       cwd=repo2, capture_output=True, text=True, check=False,
                       env=env, timeout=120)
    check(r.returncode == 0, f"SOL_SANDBOX run exits 0 (stderr: {r.stderr[:150]})")
    args = arglog.read_text()
    check("-s danger-full-access" in args, "SOL_SANDBOX replaces the -s flag")
    check("-s workspace-write" not in args, "the hardcoded default is gone, not duplicated")
    check("danger-full-access" in r.stderr and "write outside" in r.stderr,
          "warns on stderr when confinement is dropped")

print("timeout backstop")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "slow")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("SOL_MAX_WORKERS", None)
    env.pop("SOL_WORKTREE_SETUP", None)
    env["FAKE_SLEEP"] = "30"
    env["SOL_WORKER_TIMEOUT"] = "3"

    import time
    t0 = time.time()
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=20,
        )
        elapsed = time.time() - t0
        # Bound at 25, not 15: what this proves is that the cap killed the
        # worker rather than the 30s sleep running to completion. A tighter
        # bound measures machine load instead, and fails at load average 221
        # while the kill worked perfectly — the same wall-clock-guessing
        # mistake this release removes from the product.
        check(elapsed < 25,
              f"run terminates via the cap instead of waiting out the 30s sleep (took {elapsed:.1f}s)")
        check(r.returncode == 1, "a timed-out worker makes the run exit 1")
        check((run_dir / "workers" / "slow" / "exit-code").read_text().strip() == "124",
              "records exit code 124 for a timed-out worker")
        # The worker is killed mid-sleep, before it emits anything, so its event
        # log is empty. Classified on the empty log alone it reads
        # `failed-launch`, which the flow doc defines as a bad invocation or a
        # missing binary — sending the reviewer after the wrong thing entirely.
        st = (run_dir / "workers" / "slow" / "status").read_text().strip()
        check(st == "timed-out",
              f"a capped worker killed before its first event is timed-out, "
              f"not failed-launch (got {st!r})")
    except subprocess.TimeoutExpired:
        check(False, "run terminates promptly instead of waiting out the 30s sleep (timed out instead)")
        check(False, "a timed-out worker makes the run exit 1 (timed out instead)")
        check(False, "records exit code 124 for a timed-out worker (timed out instead)")

    # Give any orphaned descendant a moment to show up, then confirm nothing
    # from this run's process group survives the timeout kill. The marker is
    # this test's own throwaway tmp dir -- unique per run and present in the
    # fake codex process's own argv (its -C/-o paths live under it) -- so it
    # cannot match an unrelated process on the machine.
    time.sleep(1)
    stray = subprocess.run(
        ["pgrep", "-f", str(root)], capture_output=True, text=True, check=False
    )
    check(stray.returncode == 1,
          f"no stray codex/sleep descendant survives the timeout kill "
          f"(pgrep exit {stray.returncode}, found: {stray.stdout.strip()!r})")

print("worker killed without recording an exit code")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "zombie")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("SOL_MAX_WORKERS", None)
    env.pop("SOL_WORKTREE_SETUP", None)
    env["FAKE_SLEEP"] = "30"

    import signal
    import time

    # Launch in the background so we can reach in and kill only the wrapper
    # pid ourselves -- reproducing an operator's `kill -9` or an OOM kill that
    # lands on just that one process, leaving codex an orphan unless the
    # "gone without an exit-code" branch reaps its whole process group.
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        pids_file = run_dir / "pids"
        deadline = time.time() + 10
        while time.time() < deadline and not (
            pids_file.exists() and pids_file.stat().st_size > 0
        ):
            time.sleep(0.1)
        check(pids_file.exists() and pids_file.stat().st_size > 0,
              "pids file appears before we intervene")
        wrapper_pid = int(pids_file.read_text().strip().splitlines()[0].split("\t")[1])

        os.kill(wrapper_pid, signal.SIGKILL)   # the wrapper only, not its group

        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            check(False, "a worker killed without recording an exit code makes the run exit 1 (timed out instead)")
            check(False, "records exit code 137 for a worker killed without an exit code (timed out instead)")
            check(False, "no stray codex/sleep descendant survives an externally killed wrapper (timed out instead)")
        else:
            check(proc.returncode == 1,
                  "a worker killed without recording an exit code makes the run exit 1")
            check((run_dir / "workers" / "zombie" / "exit-code").read_text().strip() == "137",
                  "records exit code 137 for a worker killed without an exit code")
            time.sleep(1)
            stray = subprocess.run(
                ["pgrep", "-f", str(root)], capture_output=True, text=True, check=False
            )
            check(stray.returncode == 1,
                  f"no stray codex/sleep descendant survives an externally killed wrapper "
                  f"(pgrep exit {stray.returncode}, found: {stray.stdout.strip()!r})")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

print("worker that finishes inside the wait loop's own race window")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    install_fake_awk(bin_dir)          # widens the window; see FAKE_AWK
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "racer")
    awk_log = root / "awk.log"

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("SOL_MAX_WORKERS", None)
    env.pop("SOL_WORKTREE_SETUP", None)
    env["FAKE_SLEEP"] = "1"
    env["FAKE_AWK_LOG"] = str(awk_log)

    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        r = None
        for label in ("the race seam really fired",
                      "a worker that finishes mid-poll keeps its real exit code",
                      "...is classified ok, not failed-run",
                      "...has its work committed, not stranded in the worktree",
                      "...does not fail the run"):
            check(False, f"{label} (run timed out)")

    if r is not None:
        # Prove the seam did what it claims: the wrapper really was already gone
        # by the time the loop got its pid. Without this the test could pass for
        # the wrong reason -- never entering the window at all.
        seen = awk_log.read_text().strip() if awk_log.is_file() else "<no awk log>"
        check("dead" in seen,
              f"the race seam really fired: the wait loop got the pid only after the "
              f"worker had exited (awk log: {seen!r})")

        w = run_dir / "workers" / "racer"
        code = (w / "exit-code").read_text().strip()
        status = (w / "status").read_text().strip()
        check(code == "0",
              f"a worker that finishes between the exit-code check and the liveness "
              f"check keeps its real exit code, instead of having it overwritten with "
              f"137 by a reap of an already-dead process group (got {code!r})")
        check(status == "ok",
              f"...and is classified ok, not failed-run (got {status!r})")
        check((w / "commit").read_text().strip() != "",
              "...and its work is committed, not left stranded uncommitted in "
              "the worktree")
        wt = root / ".sol-worktrees" / "repo" / "racer"
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                               capture_output=True, text=True, check=False).stdout
        check(dirty.strip() == "",
              f"...leaving nothing uncommitted behind (porcelain: {dirty.strip()!r})")
        check(r.returncode == 0,
              f"...and does not fail the run (got {r.returncode}, stderr: {r.stderr[:200]})")

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
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env)
    s = json.loads((run_dir / "summary.json").read_text())
    check(s["workers"][0]["status"] == "failed-launch",
          "empty event log is failed-launch, not success")
    check(r.returncode == 1, "a failed-launch worker makes the run exit 1")

    repo2 = make_repo(root / "repo2")
    run_dir2 = root / "run2"
    write_tasks(run_dir2, "quiet")
    env2 = dict(os.environ)
    env2["PATH"] = f"{bin_dir}{os.pathsep}{env2['PATH']}"
    env2["FAKE_NOCHANGE"] = "1"
    r2 = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir2)],
                         cwd=repo2, capture_output=True, text=True, check=False, env=env2)
    s = json.loads((run_dir2 / "summary.json").read_text())
    check(s["workers"][0]["status"] == "no-changes", "a worker that changed nothing is no-changes")
    check(s["workers"][0]["commit"] == "", "no-changes worker has no commit")
    check(r2.returncode == 0, "a no-changes worker still makes the run exit 0")

print("summary: rejected commit")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "gamma")

    # Worktrees share the main repo's .git/hooks (no core.hooksPath override),
    # so a hook installed here applies to every worktree the script creates.
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    pre_commit = hooks / "pre-commit"
    pre_commit.write_text("#!/usr/bin/env bash\nexit 1\n")
    pre_commit.chmod(0o755)

    base_sha = git(repo, "rev-parse", "main")
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 1, "a rejected commit makes the run exit 1")

    s = json.loads((run_dir / "summary.json").read_text())
    gamma = s["workers"][0]
    check(gamma["status"] == "failed-commit",
          "a hook-rejected commit is failed-commit, not ok with the base sha")
    check(gamma["commit"] == "", "a rejected commit records no commit sha")
    check(git(repo, "rev-parse", "main") == base_sha, "base branch still untouched")
    # The work is real, staged, and uncommitted. summary.json is the only place
    # the user is told where to look for it; an empty files_changed here is the
    # difference between "recoverable" and "gone".
    check(gamma["files_changed"] == ["gamma.txt"],
          f"a failed-commit worker still reports the work it stranded in its "
          f"worktree (got {gamma['files_changed']!r})")

print("summary: a non-ok worker's stranded work is still reported")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "broken")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_EXIT"] = "1"          # failed-run, but it wrote a file first
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
                       cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 1, f"a failed-run launch exits 1 (stderr: {r.stderr[:200]})")
    s = json.loads((run_dir / "summary.json").read_text())
    broken = s["workers"][0]
    check(broken["status"] == "failed-run", "sanity: the worker really is failed-run")
    # failed-run and timed-out never reach `git add`, so the work sits in the
    # worktree as untracked files. Reporting [] for them stranded it exactly the
    # way failed-commit did.
    check(broken["files_changed"] == ["broken.txt"],
          f"a failed-run worker reports the uncommitted work left in its worktree "
          f"(got {broken['files_changed']!r})")

    # The other direction: a non-ok worker that genuinely did nothing must not
    # be given invented files.
    repo2 = make_repo(root / "repo2")
    run_dir2 = root / "run2"
    write_tasks(run_dir2, "sterile")
    env2 = dict(os.environ)
    env2["PATH"] = f"{bin_dir}{os.pathsep}{env2['PATH']}"
    env2["FAKE_EXIT"] = "1"
    env2["FAKE_NOCHANGE"] = "1"     # failed, and touched nothing
    r2 = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir2)],
                        cwd=repo2, capture_output=True, text=True, check=False, env=env2)
    check(r2.returncode == 1, f"a no-op failed-run launch exits 1 (stderr: {r2.stderr[:200]})")
    s2 = json.loads((run_dir2 / "summary.json").read_text())
    check(s2["workers"][0]["status"] == "failed-run", "sanity: also failed-run")
    check(s2["workers"][0]["files_changed"] == [],
          f"a failed-run worker with a clean worktree reports no files, not the "
          f"copied .env or other noise (got {s2['workers'][0]['files_changed']!r})")

print("--wait / --resume keep a failed-setup worker on the roster")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "good", "bad")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("SOL_MAX_WORKERS", None)
    # Fail bootstrap for exactly one of the two workers. The hook runs with the
    # worktree as cwd, and the worktree is named after the slug.
    env["SOL_WORKTREE_SETUP"] = 'case "$PWD" in */bad) exit 3 ;; esac'

    r = subprocess.run(["bash", str(SCRIPT), "--workers", "2", str(run_dir)],
                       cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 1, f"launch exits 1 when one worker's setup fails "
          f"(stderr: {r.stderr[:200]})")
    s = json.loads((run_dir / "summary.json").read_text())
    check([w["slug"] for w in s["workers"]] == ["good", "bad"],
          f"sanity: launch's summary lists both workers in task order "
          f"(got {[w['slug'] for w in s['workers']]!r})")
    check([w["status"] for w in s["workers"]] == ["ok", "failed-setup"],
          f"sanity: bad really is failed-setup "
          f"(got {[w['status'] for w in s['workers']]!r})")
    check("bad" not in (run_dir / "pids.all").read_text(),
          "sanity: pids.all really does exclude the never-launched worker")

    # parallel-flow.md §5 says the launch call routinely outruns its tool-call
    # budget and Claude re-attaches with --wait, so this is the normal path.
    # Rehydrating from pids.all erased `bad` from summary.json and turned a run
    # that had correctly exited 1 into a silent exit 0 -- the worker's own
    # `status` file on disk still saying failed-setup the whole time.
    r = run(repo, bin_dir, "--wait", str(run_dir))
    check(r.returncode == 1,
          f"--wait still exits 1 for a failed-setup worker it never launched "
          f"(got {r.returncode})")
    s = json.loads((run_dir / "summary.json").read_text())
    check([w["slug"] for w in s["workers"]] == ["good", "bad"],
          f"--wait keeps the failed-setup worker in summary.json, in task order "
          f"(got {[w['slug'] for w in s['workers']]!r})")
    by_slug = {w["slug"]: w for w in s["workers"]}
    check(by_slug.get("bad", {}).get("status") == "failed-setup",
          f"--wait reports the failed-setup worker's real status "
          f"(got {by_slug.get('bad', {}).get('status')!r})")

    # Same erasure on the --resume path.
    (run_dir / "workers" / "good" / "correction.md").write_text("good.txt:1 — tweak.\n")
    r = run(repo, bin_dir, "--resume", str(run_dir))
    check(r.returncode == 1,
          f"--resume still exits 1 for a bystander failed-setup worker (got {r.returncode})")
    s = json.loads((run_dir / "summary.json").read_text())
    check([w["slug"] for w in s["workers"]] == ["good", "bad"],
          f"--resume keeps the failed-setup worker in summary.json, in task order "
          f"(got {[w['slug'] for w in s['workers']]!r})")

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

print("--wait rehydrates the correct brief per slug (suffix-colliding briefs)")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    # "02-auth.md" is a glob suffix match of "01-add-auth.md": both end in
    # "-auth.md". A rehydration that recovers the brief by globbing
    # `*-<slug>.md` instead of reading a recorded path would resolve slug
    # "auth" to the wrong file.
    write_tasks(run_dir, "add-auth", "auth")

    r = run(repo, bin_dir, "--workers", "2", str(run_dir))
    check(r.returncode == 0, f"launch exits 0 (stderr: {r.stderr[:200]})")

    r = run(repo, bin_dir, "--wait", str(run_dir))
    check(r.returncode == 0, "--wait after completion exits 0")

    s = json.loads((run_dir / "summary.json").read_text())
    by_slug = {w["slug"]: w for w in s["workers"]}
    check(by_slug["add-auth"]["brief"].endswith("01-add-auth.md"),
          "add-auth worker keeps its own brief after --wait rehydration")
    check(by_slug["auth"]["brief"].endswith("02-auth.md"),
          "auth worker (suffix of add-auth) gets its own brief, not add-auth's, "
          f"after --wait rehydration (got {by_slug['auth']['brief']!r})")

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

print("--resume: roster coupling (a bystander failure must still fail the run)")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha", "beta")
    r = run(repo, bin_dir, "--workers", "2", str(run_dir))
    check(r.returncode == 0, f"initial launch exits 0 (stderr: {r.stderr[:200]})")

    # Simulate alpha having failed in the first round (a real failed-run from
    # a bad launch) while beta succeeded, without needing to reproduce a
    # genuine per-worker failure through the shared fake codex shim -- only
    # beta is corrected and resumed; alpha is left exactly as a first-round
    # failure would leave it.
    (run_dir / "workers" / "alpha" / "status").write_text("failed-run\n")

    (run_dir / "workers" / "beta" / "correction.md").write_text(
        "beta.txt:1 — wrong value.\n")
    r = run(repo, bin_dir, "--resume", str(run_dir))
    check(r.returncode == 1,
          f"a resume exits 1 when a bystander worker is still failed-run (got {r.returncode})")

    s = json.loads((run_dir / "summary.json").read_text())
    check(len(s["workers"]) == 2, "summary.json still has both workers after a partial resume")
    by_slug = {w["slug"]: w for w in s["workers"]}
    check(by_slug["alpha"]["status"] == "failed-run",
          "the bystander's failed-run status is preserved, not silently dropped")
    check(by_slug["beta"]["status"] == "ok", "the resumed worker is reclassified ok")

print("--resume: clears terminal state so a failed worker can be reclassified")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_EXIT"] = "1"
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 1, "initial launch fails (worker exits 1)")
    check((run_dir / "workers" / "alpha" / "status").read_text().strip() == "failed-run",
          "alpha is failed-run after round 1")

    (run_dir / "workers" / "alpha" / "correction.md").write_text(
        "alpha.txt:1 — fix the failure.\n")

    env2 = dict(os.environ)
    env2["PATH"] = f"{bin_dir}{os.pathsep}{env2['PATH']}"   # no FAKE_EXIT this round
    r = subprocess.run(["bash", str(SCRIPT), "--resume", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env2)
    check(r.returncode == 0, f"resume with a successful correction exits 0 (stderr: {r.stderr[:200]})")
    check((run_dir / "workers" / "alpha" / "status").read_text().strip() == "ok",
          "a previously failed-run worker is reclassified ok after a successful resume, "
          "not left stuck at its old status")

print("--resume: a resumed no-op still reports files_changed, computed from the commit")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha")
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 0, f"initial launch exits 0 (stderr: {r.stderr[:200]})")

    (run_dir / "workers" / "alpha" / "correction.md").write_text(
        "alpha.txt:1 — double-check this, nothing to change.\n")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    r = subprocess.run(["bash", str(SCRIPT), "--resume", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 0, f"a resumed no-op exits 0 (stderr: {r.stderr[:200]})")

    s = json.loads((run_dir / "summary.json").read_text())
    alpha = s["workers"][0]
    check(alpha["status"] == "ok", "resumed no-op is still ok")
    check(alpha["files_changed"] == ["alpha.txt"],
          "a resumed no-op still reports the branch's real files_changed, computed from "
          f"the commit rather than the (empty) pre-commit porcelain (got {alpha['files_changed']!r})")

print("--resume: files_changed records the bare filename, even with a double quote in it")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_FILENAME"] = 'weird"name.txt'
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 0, f"launch with a quoted filename exits 0 (stderr: {r.stderr[:200]})")
    s = json.loads((run_dir / "summary.json").read_text())
    check(s["workers"][0]["files_changed"] == ['weird"name.txt'],
          "the bare filename is recorded, not git's porcelain-quoted form "
          f"(got {s['workers'][0]['files_changed']!r})")

print("--resume: skips a worker with no recorded session id rather than resuming blind")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "alpha", "beta")
    r = run(repo, bin_dir, "--workers", "2", str(run_dir))
    check(r.returncode == 0, f"initial launch exits 0 (stderr: {r.stderr[:200]})")

    # Simulate alpha never having recorded a session (failed-setup, or a round
    # whose event log was empty) without reproducing the whole failure path --
    # only the missing session-id file matters for this guard.
    (run_dir / "workers" / "alpha" / "session-id").unlink()

    (run_dir / "workers" / "alpha" / "correction.md").write_text("alpha.txt:1 — fix.\n")
    (run_dir / "workers" / "beta" / "correction.md").write_text("beta.txt:1 — fix.\n")

    arglog = root / "args.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_ARGLOG"] = str(arglog)
    r = subprocess.run(["bash", str(SCRIPT), "--resume", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 0,
          f"resume exits 0 when the other worker resumes cleanly (stderr: {r.stderr[:300]})")
    check("alpha" in r.stderr and "no session id" in r.stderr,
          f"reports the skipped worker by name (stderr: {r.stderr[:300]!r})")
    check((run_dir / "workers" / "alpha" / "correction.md").exists(),
          "a skipped worker's correction.md is not consumed")
    args = arglog.read_text() if arglog.exists() else ""
    check("beta" in args, "the other worker with a real session id still resumes")

print("--cleanup")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "merged", "kept")
    run(repo, bin_dir, "--workers", "2", str(run_dir))

    # Simulate Claude integrating one branch. Amend afterwards so the integrated
    # commit has a DIFFERENT sha with the same patch — which is what a real
    # cherry-pick minutes after the original produces. Without the amend this
    # test only passes when both commits land in the same second.
    git(repo, "cherry-pick", "sol/merged")
    git(repo, "commit", "--amend", "--no-edit", "-m", "integrated: merged")
    check(git(repo, "rev-parse", "HEAD") != git(repo, "rev-parse", "sol/merged"),
          "the integrated commit has a different sha than the branch")

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
    check((run_dir / "workers" / "kept" / "status").read_text().strip() in r.stdout,
          f"names the surviving worker's status in the kept line (stdout: {r.stdout!r})")

    # Running --cleanup again must not misreport the branch it just removed.
    # `git merge-base --is-ancestor` on a branch that no longer exists also
    # returns non-zero (same as "not merged"), which -- without an explicit
    # existence check -- would fall into the "kept" path and print a
    # worktree path that was already deleted above.
    r2 = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r2.returncode == 0, f"a second --cleanup exits 0 (stderr: {r2.stderr[:200]})")
    check("sol/merged" not in r2.stdout,
          f"a second --cleanup does not phantom-report the already-removed branch "
          f"(stdout: {r2.stdout!r})")
    check("sol/kept" in r2.stdout,
          "a second --cleanup still names the still-unmerged survivor")

print("--cleanup: a no-changes worker (no commit, trivially merged) is removed")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "quiet")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_NOCHANGE"] = "1"
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 0, f"a no-changes launch exits 0 (stderr: {r.stderr[:200]})")

    branch_sha = git(repo, "rev-parse", "sol/quiet")
    base_sha = git(repo, "rev-parse", "main")
    check(branch_sha == base_sha,
          "sanity: the no-changes branch really made no commit, so it is "
          "trivially an ancestor of base")

    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup on a no-changes run exits 0 (stderr: {r.stderr[:200]})")
    check(not (root / ".sol-worktrees" / "repo" / "quiet").exists(),
          "removes the worktree of a no-changes (trivially merged) branch")
    check(git(repo, "branch", "--list", "sol/quiet") == "",
          "deletes the no-changes (trivially merged) branch")
    check("sol/quiet" not in r.stdout, "does not report the removed no-changes branch as kept")

print("--cleanup: a removal that fails is reported, not silently swallowed")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "locked")
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 0, f"launch exits 0 (stderr: {r.stderr[:200]})")

    # Fast-forward, not cherry-pick: a cherry-pick's committer timestamp is
    # "now", so it only reproduces the *exact same commit sha* as sol/locked
    # (and thus registers as an ancestor) when both land in the same
    # wall-clock second -- flaky under load. `merge --ff-only` moves main to
    # sol/locked's own sha, so the ancestor relationship is exact and timing-independent.
    git(repo, "merge", "-q", "--ff-only", "sol/locked")

    wt = root / ".sol-worktrees" / "repo" / "locked"
    lock_out = subprocess.run(["git", "worktree", "lock", str(wt)],
                               cwd=repo, capture_output=True, text=True, check=False)
    check(lock_out.returncode == 0, f"test setup: locking the worktree succeeds "
          f"(stderr: {lock_out.stderr[:200]})")

    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup still exits 0 when a removal fails "
          f"(stderr: {r.stderr[:200]})")
    check("locked" in r.stderr and ("incomplete" in r.stderr or "inconsistent" in r.stderr),
          f"reports the failed removal by slug name on stderr, instead of the "
          f"silent 2>/dev/null default (stderr: {r.stderr!r})")
    check(wt.is_dir(), "the locked worktree is still on disk, not silently lost")
    check(git(repo, "branch", "--list", "sol/locked") != "",
          "the branch backing a failed removal is not silently deleted either")
    check("sol/locked" in r.stdout,
          "a branch that failed to be removed is still named on stdout, so it "
          "isn't stranded silently")

    subprocess.run(["git", "worktree", "unlock", str(wt)], cwd=repo, check=False)

print("--cleanup: a failed-setup worker is not left invisible to --cleanup")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "badsetup")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SOL_WORKTREE_SETUP"] = "exit 3"
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 1, f"a failing setup hook fails the launch (stderr: {r.stderr[:200]})")
    check((run_dir / "workers" / "badsetup" / "status").read_text().strip() == "failed-setup",
          "sanity: the worker really is failed-setup")

    # create_worktrees creates the branch and worktree for a failed-setup
    # worker BEFORE bootstrap runs, so both are real -- but launch_workers
    # never adds a failed-setup slug to pids/pids.all (it was never
    # launched), so roster() alone would never see it. Without walking
    # OUT_DIR directly, --cleanup would silently skip this worker forever:
    # no "kept" line, no removal, just invisible.
    check(git(repo, "branch", "--list", "sol/badsetup") != "",
          "sanity: the failed-setup worker really does have a real branch")
    wt = root / ".sol-worktrees" / "repo" / "badsetup"
    check(wt.is_dir(), "sanity: the failed-setup worker really does have a real worktree")
    pids_all = (run_dir / "pids.all").read_text()
    check("badsetup" not in pids_all,
          "sanity: pids.all really does exclude the failed-setup worker "
          f"(pids.all: {pids_all!r})")

    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup exits 0 on a failed-setup-only run "
          f"(stderr: {r.stderr[:200]})")
    # A failed-setup worker never ran codex and so never committed: its branch
    # is exactly base, trivially "integrated" by ancestry alone. But integration
    # is necessary, not sufficient -- removability also requires a terminal
    # status that means success (ok/no-changes). "failed-setup" is neither, so
    # the conservative, honest outcome is to keep and name it, same as any
    # other non-terminal-success status, not to assume there was nothing to lose.
    check(wt.is_dir(),
          "keeps the worktree of a failed-setup worker (status isn't ok/no-changes)")
    check(git(repo, "branch", "--list", "sol/badsetup") != "",
          "keeps the branch of a failed-setup worker")
    check("sol/badsetup" in r.stdout and str(wt) in r.stdout,
          f"names the failed-setup worker's branch and worktree on stdout, now that "
          f"it is visible to --cleanup at all (stdout: {r.stdout!r})")

print("--cleanup: a failed-commit worker's uncommitted work survives --cleanup")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "gamma")

    # Same rejecting pre-commit hook as the "summary: rejected commit" test:
    # worktrees share the main repo's .git/hooks, so installing it here makes
    # every worker's auto-commit attempt fail.
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    pre_commit = hooks / "pre-commit"
    pre_commit.write_text("#!/usr/bin/env bash\nexit 1\n")
    pre_commit.chmod(0o755)

    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 1, f"a rejected commit fails the run (stderr: {r.stderr[:200]})")
    check((run_dir / "workers" / "gamma" / "status").read_text().strip() == "failed-commit",
          "sanity: the worker really is failed-commit")

    wt = root / ".sol-worktrees" / "repo" / "gamma"
    worked_file = wt / "gamma.txt"
    check(worked_file.is_file(),
          "sanity: the worker's real (staged, uncommitted) file is really there")
    # The branch itself has no commit -- git add -A staged the file but the
    # hook rejected the commit -- so it sits exactly at base and would look
    # trivially "integrated" by ancestry alone.
    check(git(repo, "rev-parse", "sol/gamma") == git(repo, "rev-parse", "main"),
          "sanity: the failed-commit branch really sits exactly at base "
          "(nothing was ever committed)")

    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup exits 0 (stderr: {r.stderr[:200]})")
    check(wt.is_dir(),
          "does NOT remove the worktree of a failed-commit worker -- "
          "its real work is staged-but-uncommitted there")
    check(git(repo, "branch", "--list", "sol/gamma") != "",
          "does NOT delete the failed-commit worker's branch")
    check(worked_file.is_file(),
          "the failed-commit worker's uncommitted file is still on disk, not destroyed")
    check("sol/gamma" in r.stdout and str(wt) in r.stdout,
          f"names the failed-commit worker's branch and worktree on stdout "
          f"(stdout: {r.stdout!r})")
    check("uncommitted" in r.stdout,
          f"the kept line says the work is uncommitted, not just 'failed-commit' "
          f"(stdout: {r.stdout!r})")

print("--cleanup: an integrated worker with unrelated uncommitted edits is kept, not force-removed")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "solo")
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 0, f"launch exits 0 (stderr: {r.stderr[:200]})")
    check((run_dir / "workers" / "solo" / "status").read_text().strip() == "ok",
          "sanity: the worker really is ok")

    git(repo, "merge", "-q", "--ff-only", "sol/solo")   # deterministic integration

    wt = root / ".sol-worktrees" / "repo" / "solo"
    # Unrelated uncommitted edit left in the worktree -- e.g. a reviewer poking
    # around, or a leftover build artifact -- with nothing to do with the
    # worker's own committed work.
    (wt / "scratch-notes.txt").write_text("someone was looking at this\n")
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                            capture_output=True, text=True, check=False).stdout
    check(dirty.strip() != "", "sanity: the worktree really is dirty before --cleanup runs")

    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup exits 0 (stderr: {r.stderr[:200]})")
    check(wt.is_dir(),
          "does NOT force-remove an integrated worker's worktree while it has "
          "unrelated uncommitted edits")
    check(git(repo, "branch", "--list", "sol/solo") != "",
          "does NOT delete an integrated worker's branch while its worktree is dirty")
    check("sol/solo" in r.stdout,
          f"names the dirty-but-integrated worker on stdout (stdout: {r.stdout!r})")

print("--cleanup: a failed-run worker with a CLEAN worktree is still kept -- status, "
      "not just worktree cleanliness, gates removal")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "broken")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_EXIT"] = "1"        # codex itself fails
    env["FAKE_NOCHANGE"] = "1"    # ...and never touches the worktree
    r = subprocess.run(["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
                        cwd=repo, capture_output=True, text=True, check=False, env=env)
    check(r.returncode == 1, f"a failing worker fails the run (stderr: {r.stderr[:200]})")
    check((run_dir / "workers" / "broken" / "status").read_text().strip() == "failed-run",
          "sanity: the worker really is failed-run")

    wt = root / ".sol-worktrees" / "repo" / "broken"
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                            capture_output=True, text=True, check=False).stdout
    check(dirty.strip() == "",
          "sanity: the worktree really is clean (codex never wrote anything) -- "
          "so only the status gate, not worktree dirtiness, can protect this worker")
    check(git(repo, "rev-parse", "sol/broken") == git(repo, "rev-parse", "main"),
          "sanity: the branch sits exactly at base, same as a failed-commit worker, "
          "so it also looks trivially integrated by ancestry alone")

    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup exits 0 (stderr: {r.stderr[:200]})")
    check(wt.is_dir(),
          "does NOT remove a failed-run worker's worktree just because it "
          "happens to be clean and trivially integrated")
    check(git(repo, "branch", "--list", "sol/broken") != "",
          "does NOT delete a failed-run worker's branch just because its "
          "worktree happens to be clean")
    check("sol/broken" in r.stdout,
          f"names the failed-run worker on stdout (stdout: {r.stdout!r})")

print("--cleanup: an unverifiable base keeps everything instead of guessing 'integrated'")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "rejected")
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 0, f"launch exits 0 (stderr: {r.stderr[:200]})")
    check((run_dir / "workers" / "rejected" / "status").read_text().strip() == "ok",
          "sanity: the worker is ok and its worktree is clean, so nothing but the "
          "integration check itself stands between it and removal")

    # This worker was reviewed and REJECTED: its patch was never integrated. Then
    # the base branch is renamed -- an ordinary merged-and-pruned action. Now
    # `git cherry main sol/rejected` fails outright: it prints nothing, `grep -c`
    # dutifully reports 0 unmerged commits, and a check that reads only the
    # output concludes "fully integrated" from a command that never ran.
    git(repo, "branch", "-m", "main", "trunk")
    check(git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "trunk",
          "sanity: the base branch really was renamed out from under the run")
    probe = subprocess.run(["git", "cherry", "main", "sol/rejected"], cwd=repo,
                           capture_output=True, text=True, check=False)
    check(probe.returncode != 0 and probe.stdout.strip() == "",
          f"sanity: git cherry really does fail with empty stdout here "
          f"(rc {probe.returncode}, stdout {probe.stdout!r})")

    wt = root / ".sol-worktrees" / "repo" / "rejected"
    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup exits 0 (stderr: {r.stderr[:200]})")
    check(wt.is_dir(),
          "does NOT remove the worktree of un-integrated work just because the "
          "integration check could not run")
    check(git(repo, "branch", "--list", "sol/rejected") != "",
          "does NOT delete the branch either")
    check("sol/rejected" in r.stdout,
          f"names the kept branch on stdout rather than saying nothing at all "
          f"(stdout: {r.stdout!r})")
    check("main" in r.stderr and "resolve" in r.stderr,
          f"says on stderr that the base ref no longer resolves, so the operator "
          f"knows why nothing was cleaned up (stderr: {r.stderr!r})")

print("--cleanup: a kept line never prints a worktree path that no longer exists")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "vanished")
    r = run(repo, bin_dir, "--workers", "1", str(run_dir))
    check(r.returncode == 0, f"launch exits 0 (stderr: {r.stderr[:200]})")

    wt = root / ".sol-worktrees" / "repo" / "vanished"
    shutil.rmtree(wt)   # deleted by hand, bypassing git -- branch sol/vanished survives

    r = run(repo, bin_dir, "--cleanup", str(run_dir))
    check(r.returncode == 0, f"--cleanup exits 0 (stderr: {r.stderr[:200]})")
    check(str(wt) not in r.stdout,
          f"never prints the path of a worktree that was removed by hand "
          f"(stdout: {r.stdout!r})")
    check("sol/vanished" in r.stdout,
          f"still names the branch even though its worktree is gone "
          f"(stdout: {r.stdout!r})")
    check("already removed" in r.stdout,
          f"says the worktree is already gone rather than staying silent about it "
          f"(stdout: {r.stdout!r})")

def stall_env(bin_dir: pathlib.Path, **overrides: str) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    for k in ("SOL_MAX_WORKERS", "SOL_WORKTREE_SETUP", "SOL_WORKER_TIMEOUT",
              "SOL_FIRST_EVENT_TIMEOUT", "SOL_IDLE_TIMEOUT", "SOL_STALL_RETRIES",
              "SOL_COMMAND_TIMEOUT", "SOL_EFFORT"):
        env.pop(k, None)
    env.update(overrides)
    return env


print("stall watchdog: prelude-only worker is killed and classed stalled")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "hung")
    env = stall_env(bin_dir, FAKE_STALL_MODE="prelude",
                    SOL_FIRST_EVENT_TIMEOUT="3", SOL_STALL_RETRIES="0")

    import time
    t0 = time.time()
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=25,
        )
        elapsed = time.time() - t0
        w = run_dir / "workers" / "hung"
        check(elapsed < 15,
              f"stalled worker is killed by the first-event budget, not the 600s sleep (took {elapsed:.1f}s)")
        check(r.returncode == 1, "a stalled worker makes the run exit 1")
        check((w / "exit-code").read_text().strip() == "125",
              "records exit code 125 for a stalled worker")
        check((w / "status").read_text().strip() == "stalled",
              f"classifies the worker `stalled` (got {(w / 'status').read_text().strip()!r})")
        reason = (w / "stall-reason").read_text().strip() if (w / "stall-reason").exists() else ""
        check("no substantive event" in reason,
              f"stall-reason names the first-event budget (got {reason!r})")
        summary = json.loads((run_dir / "summary.json").read_text())
        check(summary["workers"][0]["status"] == "stalled"
              and summary["workers"][0]["stall_reason"] != "",
              "summary.json carries status=stalled and the stall reason")
    except subprocess.TimeoutExpired:
        for label in ("stalled worker killed promptly", "stalled run exits 1",
                      "exit code 125", "status stalled", "stall reason recorded",
                      "summary carries stalled"):
            check(False, f"{label} (run timed out instead)")

    time.sleep(1)
    stray = subprocess.run(
        ["pgrep", "-f", str(root)], capture_output=True, text=True, check=False
    )
    check(stray.returncode == 1,
          f"no stray codex/sleep descendant survives the stall kill "
          f"(pgrep: {stray.stdout!r})")

print("stall watchdog: silence after a substantive event trips the idle budget")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "quiet")
    env = stall_env(bin_dir, FAKE_STALL_MODE="after-item",
                    SOL_FIRST_EVENT_TIMEOUT="60", SOL_IDLE_TIMEOUT="3",
                    SOL_STALL_RETRIES="0")
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=25,
        )
        w = run_dir / "workers" / "quiet"
        check(r.returncode == 1, "an idle-stalled worker makes the run exit 1")
        check((w / "status").read_text().strip() == "stalled",
              "idle silence after a substantive event is classed stalled")
        reason = (w / "stall-reason").read_text().strip() if (w / "stall-reason").exists() else ""
        check("idle budget" in reason,
              f"stall-reason names the idle budget (got {reason!r})")
    except subprocess.TimeoutExpired:
        for label in ("idle-stalled run exits 1", "idle stall classed stalled",
                      "idle stall reason"):
            check(False, f"{label} (run timed out instead)")

print("stall retry ladder: relaunch one effort lower recovers the worker")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "flaky")
    arglog = root / "args.log"
    env = stall_env(bin_dir, FAKE_STALL_EFFORTS="xhigh", FAKE_ARGLOG=str(arglog),
                    SOL_FIRST_EVENT_TIMEOUT="3", SOL_STALL_RETRIES="1")
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=40,
        )
        w = run_dir / "workers" / "flaky"
        check(r.returncode == 0,
              f"run recovers to exit 0 after the downgraded retry (stderr: {r.stderr[:200]})")
        check((w / "status").read_text().strip() == "ok",
              "relaunched worker finishes ok")
        check((w / "events-attempt-1.jsonl").exists(),
              "the stalled attempt's event log is archived, not overwritten")
        args = arglog.read_text()
        check("model_reasoning_effort=xhigh" in args
              and "model_reasoning_effort=high" in args,
              f"codex was invoked at xhigh then relaunched at high (args: {args!r})")
        summary = json.loads((run_dir / "summary.json").read_text())
        wk = summary["workers"][0]
        check(wk["effort_used"] == "high" and wk["stall_retries"] == 1,
              f"summary records effort_used=high, stall_retries=1 "
              f"(got {wk['effort_used']!r}, {wk['stall_retries']})")
        check(wk["status"] == "ok" and wk["commit"],
              "summary shows the recovered worker committed real work")
    except subprocess.TimeoutExpired:
        for label in ("retry run exits 0", "relaunched worker ok",
                      "attempt log archived", "xhigh then high",
                      "summary effort/retries", "recovered commit"):
            check(False, f"{label} (run timed out instead)")

print("stall watchdog: a quiet command outlasting the idle budget is not a stall")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "builder")
    env = stall_env(bin_dir, FAKE_QUIET_CMD_SECS="6",
                    SOL_FIRST_EVENT_TIMEOUT="60", SOL_IDLE_TIMEOUT="3",
                    SOL_STALL_RETRIES="0")
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=40,
        )
        w = run_dir / "workers" / "builder"
        check(r.returncode == 0,
              f"a 6s-silent command survives a 3s idle budget because it is in flight "
              f"(stderr: {r.stderr[:200]})")
        check((w / "status").read_text().strip() == "ok",
              "quiet-build worker finishes ok, never stall-killed mid-command")
    except subprocess.TimeoutExpired:
        for label in ("quiet command survives idle budget", "quiet-build worker ok"):
            check(False, f"{label} (run timed out instead)")

print("absolute cap is opt-in: 0 disables it rather than killing instantly")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "unhurried")
    # 0 is the shipped default. Without the `[ "$WORKER_TIMEOUT" -gt 0 ]` guard,
    # `now - started > 0` is true on the first poll, so every worker would be
    # killed with 124 the instant it launched. This is the check that catches
    # that, and it is why the guard exists rather than just changing a number.
    env = stall_env(bin_dir, SOL_WORKER_TIMEOUT="0", FAKE_SLEEP="4",
                    SOL_IDLE_TIMEOUT="60", SOL_FIRST_EVENT_TIMEOUT="60")
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=60,
        )
        w = run_dir / "workers" / "unhurried"
        code = (w / "exit-code").read_text().strip()
        check(code == "0", f"a worker outliving no cap is not killed (exit-code {code!r})")
        check((w / "status").read_text().strip() == "ok",
              "worker classifies ok, not timed-out")
        check(r.returncode == 0, "run exits 0 with the absolute cap disabled")
    except subprocess.TimeoutExpired:
        for label in ("a worker outliving no cap is not killed",
                      "worker classifies ok, not timed-out",
                      "run exits 0 with the absolute cap disabled"):
            check(False, f"{label} (run timed out instead)")

print("stall watchdog: a command hung forever is caught by the command budget")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "wedged")
    # No SOL_WORKER_TIMEOUT: the absolute cap is off by default now, so if the
    # command budget does not fire, nothing kills this worker and the run hangs
    # until the subprocess timeout below — which is the failure this asserts.
    env = stall_env(bin_dir, FAKE_STALL_MODE="in-command",
                    SOL_FIRST_EVENT_TIMEOUT="60", SOL_IDLE_TIMEOUT="3",
                    SOL_COMMAND_TIMEOUT="8", SOL_STALL_RETRIES="0")
    import time
    t0 = time.time()
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=30,
        )
        elapsed = time.time() - t0
        w = run_dir / "workers" / "wedged"
        check(elapsed >= 8,
              f"in-flight command is exempt from the 3s idle budget (killed at {elapsed:.1f}s)")
        check((w / "exit-code").read_text().strip() == "125",
              f"hung command is killed by the command budget as a stall "
              f"(exit-code {(w / 'exit-code').read_text().strip()!r})")
        check((w / "status").read_text().strip() == "stalled",
              "hung command classifies stalled")
        check("command in flight" in (w / "stall-reason").read_text(),
              "stall reason names the command budget")
    except subprocess.TimeoutExpired:
        for label in ("in-flight exempt from idle budget", "hung command exit 125",
                      "hung command stalled", "stall reason names the command budget"):
            check(False, f"{label} (run timed out instead — nothing killed the worker)")

print("stall watchdog: heartbeating worker is never killed")
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bin_dir = root / "bin"
    install_fake_codex(bin_dir)
    repo = make_repo(root / "repo")
    run_dir = root / "run"
    write_tasks(run_dir, "steady")
    env = stall_env(bin_dir, FAKE_HEARTBEAT_SECS="5",
                    SOL_FIRST_EVENT_TIMEOUT="3", SOL_IDLE_TIMEOUT="3",
                    SOL_STALL_RETRIES="0")
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--workers", "1", str(run_dir)],
            cwd=repo, capture_output=True, text=True, check=False, env=env,
            timeout=40,
        )
        w = run_dir / "workers" / "steady"
        check(r.returncode == 0,
              f"a slow worker emitting steady events outlives budgets shorter than its runtime "
              f"(stderr: {r.stderr[:200]})")
        check((w / "status").read_text().strip() == "ok",
              "heartbeating worker finishes ok, never stall-killed")
    except subprocess.TimeoutExpired:
        for label in ("heartbeating worker survives", "heartbeating worker ok"):
            check(False, f"{label} (run timed out instead)")

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
