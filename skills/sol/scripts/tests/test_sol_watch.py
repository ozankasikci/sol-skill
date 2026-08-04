#!/usr/bin/env python3
"""Tests for sol-watch.py. No dependencies: `python3 test_sol_watch.py`.

The watcher's whole job is deciding what is worth interrupting a human for, so
these tests are mostly about what it stays quiet about.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
WATCHER = HERE.parent / "sol-watch.py"

spec = importlib.util.spec_from_file_location("sol_watch", WATCHER)
assert spec and spec.loader, f"cannot load {WATCHER}"
sol_watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sol_watch)

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


def run(fixture: str) -> tuple[list[str], int]:
    lines: list[str] = []
    watcher = sol_watch.Watcher(emit=lambda line, **kw: lines.append(line))
    code = sol_watch.replay(str(HERE / fixture), watcher)
    return lines, code


print("fixture-refactor.jsonl")
lines, code = run("fixture-refactor.jsonl")
body = "\n".join(lines)
print("\n".join(f"       {line}" for line in lines))

check(code == 0, "exits 0 on turn.completed")
check("thread 019fcd80-dead-beef-0000-000000000002" not in body, "does not leak other fixture")
check("019fcd80-dead-beef-0000-000000000001" in body, "reports thread id for resuming")
check("skills context budget" not in body.lower(), "suppresses the benign skills-budget notice")
check("descriptions were shortened" not in body.lower(), "suppresses benign notice text")
check(body.count("ERROR") == 1, "reports exactly the one genuine error")
check("sandbox denied write" in body, "reports the real sandbox error")
check("rg --files" not in body, "suppresses routine rg")
check("git status" not in body, "suppresses routine git status")
check(body.count("pytest tests/test_auth.py") == 2, "reports both pytest runs")
check("-> exit 1" in body and "-> exit 0" in body, "reports pytest exit codes")
check("/bin/zsh -lc" not in body, "strips the shell wrapper from commands")
check("src/auth/lockout.py" in body, "reports changed file from `path`")
check("tests/test_auth.py" in body, "reports changed files from `paths` list")
check("cat /nonexistent" in body, "reports non-zero exit of an unrecognised command")
check("some_future_type" not in body, "ignores unknown item types silently")
check("plan:" in body, "reports the opening plan once")
check(body.count("plan:") == 1, "reports the plan only once")
check("1204 output tokens" in body, "reports token usage in the summary")
check("2 files changed" in body, "counts unique changed files")
check("1 error" in body and "2 errors" not in body, "counts only genuine errors")

print("\nfixture-crash.jsonl")
lines, code = run("fixture-crash.jsonl")
body = "\n".join(lines)
print("\n".join(f"       {line}" for line in lines))

check(code == 1, "exits 1 on turn.failed")
check("FAILED" in body, "emits a FAILED summary line")
check("model stream disconnected" in body, "surfaces the failure reason")
check("alembic upgrade head" in body, "reports the non-zero command before the crash")
check("-> exit 2" in body, "reports its exit code")

print("\nunit checks")
check(sol_watch.strip_shell_wrapper("/bin/zsh -lc 'pytest -q'") == "pytest -q", "strip_shell_wrapper zsh")
check(sol_watch.strip_shell_wrapper('/bin/bash -c "npm test"') == "npm test", "strip_shell_wrapper bash")
check(sol_watch.strip_shell_wrapper("pytest -q") == "pytest -q", "strip_shell_wrapper passthrough")
check(sol_watch.human_duration(0) == "0m00s", "human_duration zero")
check(sol_watch.human_duration(482) == "8m02s", "human_duration 8m02s")
for cmd in ("pytest -q", "npm run typecheck", "cargo clippy", "go test ./...", "tsc --noEmit", "ruff check ."):
    check(bool(sol_watch.VERIFY_RE.search(cmd)), f"VERIFY_RE matches {cmd!r}")
for cmd in ("rg --files", "ls -la", "cat foo.py", "git diff"):
    check(not sol_watch.VERIFY_RE.search(cmd), f"VERIFY_RE ignores {cmd!r}")
check(bool(sol_watch.BENIGN_ERROR_RE.search("Skill descriptions were shortened")), "BENIGN_ERROR_RE matches notice")
check(not sol_watch.BENIGN_ERROR_RE.search("sandbox denied write"), "BENIGN_ERROR_RE ignores real errors")

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
