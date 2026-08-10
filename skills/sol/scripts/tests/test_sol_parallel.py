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
    run_dir = repo / "run"

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
