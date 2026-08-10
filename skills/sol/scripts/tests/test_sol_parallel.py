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
        check(elapsed < 15,
              f"run terminates promptly instead of waiting out the 30s sleep (took {elapsed:.1f}s)")
        check(r.returncode == 1, "a timed-out worker makes the run exit 1")
        check((run_dir / "workers" / "slow" / "exit-code").read_text().strip() == "124",
              "records exit code 124 for a timed-out worker")
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

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
