#!/usr/bin/env python3
"""Turn a `codex exec --json` event stream into milestone lines.

One line per milestone on stdout, line-buffered, so a harness Monitor (or a
human with a terminal) can watch a long /sol run without reading raw JSON.

    python3 sol-watch.py "$SCRATCHPAD/sol-events.jsonl"

Follows the file like `tail -F`: it is fine to start this before codex has
created it. Exits 0 when the turn completes, 1 when it fails.

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
    def __init__(self, emit=print) -> None:
        self.emit_line = emit
        self.started = time.monotonic()
        self.steps = 0
        self.files: list[str] = []
        self.said_plan = False
        self.errors = 0
        self.notices = 0
        self.exit_code = 0
        self.finished = False

    # -- output -----------------------------------------------------------
    def emit(self, kind: str, message: str) -> None:
        stamp = human_duration(time.monotonic() - self.started)
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
            paths = extract_paths(item)
            for path in paths:
                if path not in self.files:
                    self.files.append(path)
            self.emit("changed", ", ".join(paths) if paths else "(unnamed file)")
            return

        if item_type == "agent_message" and not self.said_plan:
            self.said_plan = True
            text = item.get("text") or ""
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

        if VERIFY_RE.search(command):
            self.emit("ran", f"{shorten(command, 80)} -> exit {exit_code}")
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
            bits.append(f"{len(self.files)} file{'s' if len(self.files) != 1 else ''} changed")
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


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    once = "--once" in argv[1:]
    if not args:
        print(__doc__.strip().split("\n\n")[0], file=sys.stderr)
        print("usage: sol-watch.py <events.jsonl> [--once]", file=sys.stderr)
        return 2

    path = args[0]
    watcher = Watcher()
    if once:
        return replay(path, watcher)
    return follow(path, watcher)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
