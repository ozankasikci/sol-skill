#!/usr/bin/env python3
"""Turn a `codex exec --json` event stream into milestone lines.

One line per milestone on stdout, line-buffered, so a harness Monitor (or a
human with a terminal) can watch a long /sol run without reading raw JSON.

    python3 sol-watch.py --run <run-dir>          # a whole launcher run, live
    python3 sol-watch.py <events.jsonl>           # one event log, live
    python3 sol-watch.py <events.jsonl> --once    # one event log, after the fact

`--run` is the one to hand a Monitor. It follows every worker of a
sol-parallel.sh run directory, survives the launcher replacing a worker's log
(a stall relaunch, a `--resume`), and exits once the launcher has written
`summary.json` -- with one closing line per worker and exit 0 only if every
worker ended `ok` or `no-changes`. Without it a backgrounded run is a single
opaque shell command with nothing to look at until it ends.

The single-file form follows the file like `tail -F`: it is fine to start it
before codex has created it. Exits 0 when the turn completes, 1 when it fails.

Design notes:

- Routine commands (rg, ls, cat, sed) are suppressed. Only verification
  commands, non-zero exits, file changes, and errors are milestones. Volume
  target is roughly 5-15 lines for an 8-minute run, because a Monitor that
  floods gets shut off.

- Silence must never look like success. A crashed codex emits no milestone at
  all, so a stall past STALE_AFTER is reported as its own line.

- Event schema is not part of any documented contract. These item types were
  observed on codex-cli 0.144.6:
      thread.started   -> thread_id
      turn.started
      item.started     -> item_type: command_execution
      item.completed   -> item_type: agent_message | command_execution | error
      turn.completed   -> usage{input_tokens, cached_input_tokens,
                                output_tokens, reasoning_output_tokens}
  file_change and turn.failed are handled defensively but were not observed.
  Anything unrecognised is ignored rather than fatal.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

POLL_INTERVAL = 0.4
STALE_AFTER = 300.0  # seconds of no events before reporting a stall
# --run: how long "no launcher and no worker alive" must hold before the run is
# taken to be over. Covers the instant between a Monitor starting and the
# launcher it was started alongside claiming the run directory.
IDLE_GRACE = 5.0

# Commands whose result is worth interrupting for: tests, linters, typecheckers,
# builds. Matched against the whole command string, so it catches
# `/bin/zsh -lc 'pytest tests/'` as well as a bare `pytest`.
VERIFY_RE = re.compile(
    r"\b("
    r"pytest|unittest|tox|nox"
    r"|jest|vitest|mocha|ava"
    r"|npm (?:run )?(?:test|lint|typecheck|build)|pnpm (?:run )?(?:test|lint|build)"
    r"|yarn (?:test|lint|build)"
    r"|cargo (?:test|clippy|build|check)"
    r"|go (?:test|build|vet)|golangci-lint"
    r"|tsc|mypy|pyright|ruff|flake8|black|eslint|prettier"
    r"|rspec|phpunit|bundle exec rspec"
    r"|make (?:test|check|lint|build)"
    r"|gradle|mvn|dotnet test|swift test|ctest"
    r")\b",
    re.IGNORECASE,
)

# A test run that doesn't use a recognised runner (`python3 test_calc.py`,
# `./run_tests.sh`, `node spec/all.js`) must still surface — suppressing the
# project's actual verification command was a real bug found by a real run.
# Applied only to commands that are not pure reads.
TEST_HINT_RE = re.compile(r"(?:^|[\s/_.-])(?:tests?|specs?)(?:$|[\s/_.\-:])", re.IGNORECASE)

ROUTINE_RE = re.compile(
    r"^(?:rg|ls|cat|sed|awk|head|tail|grep|find|pwd|echo|wc|which|file|stat|tree"
    r"|git\s+(?:status|diff|log|show|ls-files|rev-parse|branch))\b"
)

FILE_CHANGE_TYPES = {"file_change", "patch_apply", "apply_patch", "file_edit"}

# codex reports some purely informational notices as `error` items. They fire on
# every run, so surfacing them would mean a false ERROR notification every time
# and the feed would stop being worth reading. Observed on 0.144.6:
#   "Skill descriptions were shortened to fit the 2% skills context budget..."
# Kept deliberately narrow: anything not matched here is still treated as a real
# error, so a genuine failure is never silently swallowed.
BENIGN_ERROR_RE = re.compile(
    r"skills context budget|descriptions were shortened|context budget",
    re.IGNORECASE,
)


def human_duration(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}m{total % 60:02d}s"


def shorten(text: str, limit: int = 110) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def strip_shell_wrapper(command: str) -> str:
    """`/bin/zsh -lc 'pytest tests/'` reads better as `pytest tests/`."""
    match = re.match(r"""^\S*(?:sh|zsh|bash)\s+-\S*c\s+(['"])(.*)\1\s*$""", command.strip(), re.S)
    return match.group(2).strip() if match else command.strip()


def relativize(path: str) -> str:
    """Real events carry absolute paths; show them relative to cwd when under it."""
    try:
        cwd = os.getcwd().rstrip(os.sep) + os.sep
        if path.startswith(cwd):
            return path[len(cwd):]
    except OSError:
        pass
    return path


def extract_paths(item: dict) -> list[str]:
    """Pull file paths out of a change-ish item without knowing its exact shape."""
    for key in ("path", "file", "filename"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return [value]
    for key in ("paths", "files", "changes"):
        value = item.get(key)
        if isinstance(value, list):
            out = []
            for entry in value:
                if isinstance(entry, str):
                    out.append(entry)
                elif isinstance(entry, dict):
                    for inner in ("path", "file", "filename"):
                        if isinstance(entry.get(inner), str):
                            out.append(entry[inner])
                            break
            if out:
                return out
    return []


class Watcher:
    def __init__(self, emit=print, elapsed=None) -> None:
        self.emit_line = emit
        self.started = time.monotonic()
        # Optional clock for the stamp. --run passes the worker's own age, so a
        # watcher attached late still stamps lines on the run's clock, not its own.
        self.elapsed = elapsed
        self.steps = 0
        self.files: list[str] = []
        self.said_plan = False
        self.errors = 0
        self.notices = 0
        self.exit_code = 0
        self.finished = False

    # -- output -----------------------------------------------------------
    def emit(self, kind: str, message: str) -> None:
        stamp = human_duration(
            self.elapsed() if self.elapsed else time.monotonic() - self.started)
        self.emit_line(f"[{stamp}] step {self.steps:<3d}· {kind}: {message}", flush=True)

    # -- event handling ---------------------------------------------------
    def handle(self, event: dict) -> None:
        etype = event.get("type")

        if etype == "thread.started":
            thread = event.get("thread_id")
            if thread:
                self.emit("start", f"codex thread {thread}")
            return

        if etype in ("item.started", "item.completed"):
            self.handle_item(etype, event.get("item") or {})
            return

        if etype == "turn.completed":
            self.finish(event, ok=True)
            return

        if etype in ("turn.failed", "turn.aborted", "error"):
            message = event.get("message") or (event.get("error") or {}).get("message") or etype
            self.emit("ERROR", shorten(message))
            self.finish(event, ok=False)
            return

    def handle_item(self, etype: str, item: dict) -> None:
        item_type = item.get("item_type") or item.get("type")

        if item_type == "command_execution":
            if etype == "item.started":
                self.steps += 1
                return
            self.report_command(item)
            return

        if etype != "item.completed":
            return

        if item_type == "error":
            message = item.get("message") or "unknown error"
            if BENIGN_ERROR_RE.search(message):
                self.notices += 1
                return
            self.errors += 1
            self.emit("ERROR", shorten(message))
            return

        if item_type in FILE_CHANGE_TYPES:
            paths = [relativize(p) for p in extract_paths(item)]
            for path in paths:
                if path not in self.files:
                    self.files.append(path)
            self.emit("changed", ", ".join(paths) if paths else "(unnamed file)")
            return

        if item_type == "agent_message" and not self.said_plan:
            text = item.get("text") or ""
            # With the report schema on, the final message is a JSON object. A
            # worker that says nothing before it would otherwise have that
            # report printed, truncated, as its "plan".
            if text.lstrip().startswith("{"):
                return
            self.said_plan = True
            if text.strip():
                self.emit("plan", shorten(text))
            return

    def report_command(self, item: dict) -> None:
        raw = str(item.get("command") or "")
        command = strip_shell_wrapper(raw)
        exit_code = item.get("exit_code")

        # apply_patch arrives as a command on some codex versions
        if "apply_patch" in raw:
            self.emit("changed", shorten(command, 90))
            return

        # Classify on the first line only. A heredoc body is data, not the
        # command: `cat > notes.py <<EOF` followed by text mentioning "black" or
        # "tests" was being announced as a verification run.
        head = command.split("\n", 1)[0]
        if VERIFY_RE.search(head) or (
            TEST_HINT_RE.search(head) and not ROUTINE_RE.match(head)
        ):
            self.emit("ran", f"{shorten(command, 80)} -> exit {exit_code}")
            return

        # A search that exits 1 found nothing. That is an answer, not a failure,
        # and it happens dozens of times a run.
        if exit_code == 1 and re.match(r"^(?:rg|grep)\b", head):
            return

        if isinstance(exit_code, int) and exit_code != 0:
            self.emit("failed", f"{shorten(command, 80)} -> exit {exit_code}")

    def stalled(self, quiet_for: float) -> None:
        self.emit("stalled", f"no codex activity for {human_duration(quiet_for)}")

    def finish(self, event: dict, ok: bool) -> None:
        if self.finished:
            return
        self.finished = True
        usage = event.get("usage") or {}
        bits = [f"{self.steps} step{'s' if self.steps != 1 else ''}"]
        if self.files:
            # Name the files: "2 files changed" answers nothing; the names are
            # what lets the reader decide whether to go look at the diff.
            shown = ", ".join(self.files[:5])
            if len(self.files) > 5:
                shown += f", +{len(self.files) - 5} more"
            bits.append(
                f"{len(self.files)} file{'s' if len(self.files) != 1 else ''} changed ({shown})"
            )
        if self.errors:
            bits.append(f"{self.errors} error{'s' if self.errors != 1 else ''}")
        out_tokens = usage.get("output_tokens")
        if out_tokens is not None:
            bits.append(f"{out_tokens} output tokens")
        self.emit("done" if ok else "FAILED", ", ".join(bits))
        self.exit_code = 0 if ok else 1


def follow(path: str, watcher: Watcher) -> int:
    """Read `path` as it grows, dispatching complete JSON lines to `watcher`."""
    handle = None
    buffer = ""
    last_event = time.monotonic()
    reported_stall = False

    try:
        while True:
            if handle is None:
                if os.path.exists(path):
                    handle = open(path, "r", encoding="utf-8", errors="replace")
                else:
                    time.sleep(POLL_INTERVAL)
                    if not reported_stall and time.monotonic() - last_event > STALE_AFTER:
                        watcher.stalled(time.monotonic() - last_event)
                        reported_stall = True
                    continue

            chunk = handle.read()
            if chunk:
                buffer += chunk
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue  # banners, blank lines, partial junk
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    last_event = time.monotonic()
                    reported_stall = False
                    watcher.handle(event)
                    if watcher.finished:
                        return watcher.exit_code
                continue

            time.sleep(POLL_INTERVAL)
            quiet_for = time.monotonic() - last_event
            if not reported_stall and quiet_for > STALE_AFTER:
                watcher.stalled(quiet_for)
                reported_stall = True
    except KeyboardInterrupt:
        return 130
    finally:
        if handle is not None:
            handle.close()


def replay(path: str, watcher: Watcher) -> int:
    """Process an existing file once and stop. Used by the tests."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            watcher.handle(event)
            if watcher.finished:
                break
    return watcher.exit_code


def pid_alive(pid: object) -> bool:
    try:
        os.kill(int(str(pid).strip()), 0)
    except PermissionError:
        return True          # exists, just not ours
    except (OSError, ValueError):
        return False
    return True


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


class Tail:
    """One worker's event log inside a run directory.

    The launcher replaces this file under a running watcher: a stall relaunch
    moves it aside and starts a new one, a `--resume` truncates it. Both are
    detected (new inode, or shorter than what was already read) and treated as
    a fresh attempt rather than as the end of the story.
    """

    def __init__(self, run_dir: str, slug: str, emit) -> None:
        self.dir = os.path.join(run_dir, "workers", slug)
        self.path = os.path.join(self.dir, "events.jsonl")
        self.slug = slug
        self.emit_line = emit
        self.prefix = ""
        self.handle = None
        self.inode = None
        self.pos = 0
        self.buffer = b""
        self.attempts = 0
        self.watcher = self.new_watcher()

    def age(self) -> float:
        """Seconds since the launcher started this attempt."""
        try:
            return max(0.0, time.time() - float(read_text(os.path.join(self.dir, "started-at"))))
        except ValueError:
            return 0.0

    def new_watcher(self) -> Watcher:
        return Watcher(emit=lambda line, **kw: self.emit_line(self.prefix + line, **kw),
                       elapsed=self.age)

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def drain(self) -> None:
        chunk = self.handle.read() if self.handle is not None else b""
        if not chunk:
            return
        self.pos += len(chunk)
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self.watcher.handle(event)

    def poll(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            return           # not created yet, or mid-replacement
        if self.handle is None or st.st_ino != self.inode or st.st_size < self.pos:
            if self.handle is not None:
                self.drain()                      # whatever the old log still held
                self.close()
                self.watcher = self.new_watcher()
                effort = read_text(os.path.join(self.dir, "effort")).strip()
                self.watcher.emit(
                    "relaunch",
                    f"new attempt {self.attempts + 1}" + (f" at effort {effort}" if effort else ""),
                )
            try:
                self.handle = open(self.path, "rb")
            except OSError:
                self.handle = None
                return
            self.inode, self.pos, self.buffer = st.st_ino, 0, b""
            self.attempts += 1
        self.drain()


def run_slugs(run_dir: str) -> list[str]:
    """Workers of the run, in task order when the launcher's roster exists."""
    slugs = [s for s in read_text(os.path.join(run_dir, "roster")).split("\n") if s]
    if slugs:
        return slugs
    try:
        return sorted(
            d for d in os.listdir(os.path.join(run_dir, "workers"))
            if os.path.isdir(os.path.join(run_dir, "workers", d))
        )
    except OSError:
        return []


def run_active(run_dir: str) -> bool:
    """Is anything still alive: the supervising launcher, or any worker it started?"""
    if pid_alive(read_text(os.path.join(run_dir, "supervisor"))):
        return True
    for row in read_text(os.path.join(run_dir, "pids")).split("\n"):
        parts = row.split("\t")
        if len(parts) == 2 and pid_alive(parts[1]):
            return True
    return False


def conclude(run_dir: str, emit) -> int:
    """The closing lines, from the launcher's own verdict rather than ours."""
    summary_path = os.path.join(run_dir, "summary.json")
    try:
        summary_mtime = os.stat(summary_path).st_mtime
        with open(summary_path, "r", encoding="utf-8") as handle:
            workers = json.load(handle).get("workers") or []
    except (OSError, ValueError, AttributeError):
        emit(
            "run: the launcher is not running and wrote no summary.json — it most "
            "likely refused to start; read the launcher's own output",
            flush=True,
        )
        return 1
    try:
        batch_mtime = os.stat(os.path.join(run_dir, "pids")).st_mtime
    except OSError:
        batch_mtime = 0.0
    if summary_mtime < batch_mtime:
        # summary.json is from an earlier batch: this one was launched after it
        # and nothing has classified it since. The launcher was killed, and its
        # workers finished with nobody watching.
        emit(
            "run: the launcher is gone and no worker is running, but this batch was "
            f"never classified — run `sol-parallel.sh --wait {run_dir}`",
            flush=True,
        )
        return 1
    code = 0
    for w in workers:
        status = w.get("status") or "unknown"
        if status not in ("ok", "no-changes"):
            code = 1
        bits = [status]
        if w.get("report_status"):
            bits.append(f"report {w['report_status']}")
        files = w.get("files_changed") or []
        if files:
            bits.append(f"{len(files)} file{'s' if len(files) != 1 else ''}")
        if w.get("stall_retries"):
            bits.append(f"{w['stall_retries']} stall retr{'ies' if w['stall_retries'] != 1 else 'y'}")
        if w.get("elapsed_seconds"):
            bits.append(human_duration(w["elapsed_seconds"]))
        line = f"run: {w.get('slug', '?')} — " + " · ".join(bits)
        if w.get("error"):
            line += f" — {shorten(w['error'], 90)}"
        emit(line, flush=True)
    return code


def follow_run(run_dir: str, emit=print) -> int:
    """Follow every worker of a launcher run until the launcher is done with it."""
    tails: dict[str, Tail] = {}
    idle_since = None
    try:
        while True:
            slugs = run_slugs(run_dir)
            for slug in slugs:
                if slug not in tails:
                    tails[slug] = Tail(run_dir, slug, emit)
            for tail in tails.values():
                # Name the worker only when there is more than one to tell apart.
                tail.prefix = f"{tail.slug} " if len(tails) > 1 else ""
                tail.poll()

            if run_active(run_dir):
                idle_since = None
            else:
                now = time.monotonic()
                if idle_since is None:
                    idle_since = now
                if now - idle_since >= IDLE_GRACE:
                    for tail in tails.values():
                        tail.poll()               # anything written at the very end
                    return conclude(run_dir, emit)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        return 130
    finally:
        for tail in tails.values():
            tail.close()


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    once = "--once" in argv[1:]
    run = "--run" in argv[1:]
    if not args:
        print(__doc__.strip().split("\n\n")[0], file=sys.stderr)
        print("usage: sol-watch.py --run <run-dir> | <events.jsonl> [--once]", file=sys.stderr)
        return 2

    path = args[0]
    if run:
        if not os.path.isdir(path):
            print(f"sol-watch: not a run directory: {path}", file=sys.stderr)
            return 2
        return follow_run(path)
    watcher = Watcher()
    if once:
        return replay(path, watcher)
    return follow(path, watcher)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
