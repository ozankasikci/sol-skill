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
check("tests/test_auth.py" in body, "reports changed files from real `changes` shape")
check("python3 test_calc.py" in body, "reports bare-script test run (real-run regression)")
check("rg lockout" not in body, "still suppresses rg even when it mentions tests/")
check("cat /nonexistent" in body, "reports non-zero exit of an unrecognised command")
check("some_future_type" not in body, "ignores unknown item types silently")
check("plan:" in body, "reports the opening plan once")
check(body.count("plan:") == 1, "reports the plan only once")
check("1204 output tokens" in body, "reports token usage in the summary")
check("2 files changed (src/auth/lockout.py, tests/test_auth.py)" in body, "done line names the changed files")
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
for cmd in ("python3 test_calc.py", "./run_tests.sh", "node spec/all.js", "bash scripts/tests/run.sh"):
    check(bool(sol_watch.TEST_HINT_RE.search(cmd)), f"TEST_HINT_RE matches {cmd!r}")
for cmd in ("python3 setup.py", "ls -la src/", "git checkout main"):
    check(not sol_watch.TEST_HINT_RE.search(cmd), f"TEST_HINT_RE ignores {cmd!r}")
for cmd in ("rg foo tests/", "cat tests/test_a.py", "git diff tests/"):
    check(bool(sol_watch.ROUTINE_RE.match(cmd)), f"ROUTINE_RE matches {cmd!r}")
check(not sol_watch.ROUTINE_RE.match("python3 test_calc.py"), "ROUTINE_RE ignores script runs")
import os as _os
check(sol_watch.relativize(_os.getcwd() + "/src/a.py") == "src/a.py", "relativize strips cwd")
check(sol_watch.relativize("/somewhere/else/a.py") == "/somewhere/else/a.py", "relativize keeps foreign paths")

import json, os, tempfile, threading, time


def collect(events: list[dict]) -> list[str]:
    lines: list[str] = []
    watcher = sol_watch.Watcher(emit=lambda line, **kw: lines.append(line))
    for event in events:
        watcher.handle(event)
    return lines


def cmd(command: str, exit_code: int) -> dict:
    return {"type": "item.completed",
            "item": {"type": "command_execution", "command": command, "exit_code": exit_code}}


print("classification: what a Monitor should not be woken for")
out = "\n".join(collect([cmd("/bin/zsh -lc 'rg -n lockout src/'", 1)]))
check(out == "", f"a routine read that finds nothing is not a failure line (got {out!r})")
out = "\n".join(collect([cmd("cat > notes.py <<'PY'\nrun black and the tests later\nPY", 0)]))
check(out == "", f"a heredoc body mentioning a test runner is not a verification run (got {out!r})")
out = "\n".join(collect([cmd("python3 build.py", 3)]))
check("failed" in out and "exit 3" in out, "a real command that fails is still reported")
out = "\n".join(collect([{"type": "item.completed",
                          "item": {"type": "agent_message", "text": '{"status": "done"}'}}]))
check(out == "", "a JSON final report is not announced as the plan")

print("--run: follows a launcher run directory")
sol_watch.POLL_INTERVAL = 0.02
sol_watch.IDLE_GRACE = 0.2


def make_run(root: pathlib.Path, *slugs: str) -> pathlib.Path:
    run_dir = root / "run"
    for slug in slugs:
        (run_dir / "workers" / slug).mkdir(parents=True)
        (run_dir / "workers" / slug / "started-at").write_text(f"{int(time.time())}\n")
    (run_dir / "roster").write_text("".join(f"{s}\n" for s in slugs))
    return run_dir


def start(run_dir: pathlib.Path):
    lines: list[str] = []
    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.update(code=sol_watch.follow_run(
            str(run_dir), emit=lambda line, **kw: lines.append(line))),
        daemon=True)
    thread.start()
    return lines, result, thread


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def write_summary(run_dir: pathlib.Path, **status: str) -> None:
    (run_dir / "summary.json").write_text(json.dumps({"workers": [
        {"slug": slug, "status": st, "report_status": "done" if st == "ok" else "",
         "files_changed": ["a.py"] if st == "ok" else [], "stall_retries": 0,
         "elapsed_seconds": 75, "error": "" if st == "ok" else "quota exceeded"}
        for slug, st in status.items()]}))


with tempfile.TemporaryDirectory() as tmp:
    run_dir = make_run(pathlib.Path(tmp), "solo")
    log = run_dir / "workers" / "solo" / "events.jsonl"
    # This process stands in for the supervising launcher: alive for now.
    (run_dir / "supervisor").write_text(f"{os.getpid()}\n")
    (run_dir / "pids").write_text("solo\t999999\n")
    lines, result, thread = start(run_dir)

    log.write_text(json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n"
                   + json.dumps(cmd("pytest tests/", 1)) + "\n")
    check(wait_for(lambda: any("pytest tests/" in l for l in lines)),
          "milestones stream while the launcher is alive")
    check(thread.is_alive(), "a finished turn alone does not end the watch")

    # A stall relaunch: the launcher moves the log aside and starts a new one.
    (run_dir / "workers" / "solo" / "effort").write_text("medium\n")
    log.rename(log.with_name("events-attempt-1.jsonl"))
    log.write_text(json.dumps({"type": "thread.started", "thread_id": "t-2"}) + "\n"
                   + json.dumps(cmd("pytest tests/", 0)) + "\n")
    check(wait_for(lambda: any("relaunch" in l and "medium" in l for l in lines)),
          "a replaced log is announced as a relaunch at the new effort")
    check(wait_for(lambda: any("t-2" in l for l in lines))
          and wait_for(lambda: sum("pytest tests/" in l for l in lines) == 2),
          "and the new attempt is followed from its first line")
    check(all(not l.startswith("solo ") for l in lines),
          "a single worker's lines carry no slug prefix")

    # The launcher finishes: summary written, supervisor marker removed.
    write_summary(run_dir, solo="ok")
    (run_dir / "supervisor").unlink()
    thread.join(timeout=5)
    check(not thread.is_alive() and result.get("code") == 0,
          f"exits 0 once the launcher is done and every worker is ok (got {result})")
    check(lines[-1].startswith("run: solo — ok") and "report done" in lines[-1]
          and "1m15s" in lines[-1],
          f"closes with the launcher's own verdict (got {lines[-1]!r})")

with tempfile.TemporaryDirectory() as tmp:
    run_dir = make_run(pathlib.Path(tmp), "api", "ui")
    for slug in ("api", "ui"):
        (run_dir / "workers" / slug / "events.jsonl").write_text(
            json.dumps(cmd(f"pytest {slug}/", 0)) + "\n")
    (run_dir / "pids").write_text("api\t999998\nui\t999999\n")
    write_summary(run_dir, api="ok", ui="failed-run")
    lines, result, thread = start(run_dir)
    thread.join(timeout=5)
    check(any(l.startswith("api ") and "pytest api/" in l for l in lines)
          and any(l.startswith("ui ") and "pytest ui/" in l for l in lines),
          "with several workers each line names its worker")
    check(result.get("code") == 1 and any(l.startswith("run: ui — failed-run")
                                          and "quota exceeded" in l for l in lines),
          f"a failed worker makes the watch exit 1 and names the reason (got {lines[-2:]!r})")

with tempfile.TemporaryDirectory() as tmp:
    run_dir = make_run(pathlib.Path(tmp), "orphan")
    write_summary(run_dir, orphan="ok")                    # from an earlier batch
    time.sleep(0.05)
    (run_dir / "pids").write_text("orphan\t999999\n")       # this batch, launched after
    lines, result, thread = start(run_dir)
    thread.join(timeout=5)
    check(result.get("code") == 1 and "--wait" in lines[-1],
          f"a batch nobody classified is reported, not passed off with a stale summary "
          f"(got {lines[-1:]!r})")

with tempfile.TemporaryDirectory() as tmp:
    run_dir = pathlib.Path(tmp) / "run"
    (run_dir / "tasks").mkdir(parents=True)
    lines, result, thread = start(run_dir)
    thread.join(timeout=5)
    check(result.get("code") == 1 and "refused to start" in lines[-1],
          "a launcher that never started is said so, instead of watching forever")

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
