#!/usr/bin/env bash
# Fan-out launcher for the /sol skill's parallel mode. Runs one `codex exec`
# worker per brief, each in its own git worktree and branch.
#
# Usage:
#   sol-parallel.sh [--workers N] <run-dir>     launch and wait
#   sol-parallel.sh --dry-run   <run-dir>       create and bootstrap worktrees, then
#                                                stop before launching any Codex session
#                                                — inspect the setup before spending runs
#   sol-parallel.sh --wait      <run-dir>       re-attach to a running batch
#   sol-parallel.sh --resume    <run-dir>       send correction briefs
#   sol-parallel.sh --cleanup   <run-dir>       remove merged worktrees/branches
#
# Exit: 0 all ok · 1 a worker failed · 2 precondition/usage · 75 still running
#
# Env: SOL_MAX_WORKERS (default 3)   ceiling on --workers
#      SOL_WORKTREE_SETUP            command run in each fresh worktree
#      SOL_WORKER_TIMEOUT (1800)     absolute per-worker wall-clock cap, seconds
#      SOL_FIRST_EVENT_TIMEOUT (900) kill a worker whose event log still holds
#                                    nothing but thread/turn bookkeeping after
#                                    this many seconds (codex hangs at high
#                                    reasoning effort emit exactly that shape)
#      SOL_IDLE_TIMEOUT (600)        kill a worker whose event log has gone
#                                    this many seconds without a new event
#      SOL_STALL_RETRIES (1)         automatic relaunches of a stalled worker,
#                                    each one reasoning-effort step lower

set -uo pipefail

MODEL="${SOL_MODEL:-gpt-5.6-sol}"
EFFORT="${SOL_EFFORT:-xhigh}"
WORKER_TIMEOUT="${SOL_WORKER_TIMEOUT:-1800}"
FIRST_EVENT_TIMEOUT="${SOL_FIRST_EVENT_TIMEOUT:-900}"
IDLE_TIMEOUT="${SOL_IDLE_TIMEOUT:-600}"
STALL_RETRIES="${SOL_STALL_RETRIES:-1}"

die() { printf 'sol-parallel: %s\n' "$1" >&2; exit "${2:-2}"; }

# BSD stat (macOS) then GNU stat; 0 for a missing file so age math never
# explodes — callers treat 0 as "no heartbeat yet".
mtime_of() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

# One reasoning-effort step down. Stalls are empirically effort-correlated
# (openai/codex#24260, #23807), so a stalled worker retries lower, never equal.
next_effort() {
  case "$1" in
    xhigh)  echo high ;;
    high)   echo medium ;;
    medium) echo low ;;
    *)      echo "$1" ;;
  esac
}

# thread.*/turn.* (and session bookkeeping) arrive before codex does any real
# work; a log holding only those is a worker that has not started. Anything
# else — item events, commands, even errors — is evidence of life.
has_substantive_event() {
  [ -s "$1" ] || return 1
  grep -qvE '"type"[[:space:]]*:[[:space:]]*"(thread\.|turn\.|session)' "$1"
}

# True when the log's last state includes a command execution or MCP tool call
# that started and has not completed: codex emits nothing while a command runs,
# so this silence is a build in progress, not a hang. The idle budget must not
# apply — a command hung forever is the absolute cap's job. Matching is by item
# id, so interleaved items resolve correctly.
in_flight_item() {
  [ -s "$1" ] || return 1
  python3 - "$1" <<'PY'
import json, sys
started, done = set(), set()
with open(sys.argv[1]) as fh:
    for line in fh:
        try:
            e = json.loads(line)
        except Exception:
            continue
        item = e.get("item") or {}
        if item.get("type") not in ("command_execution", "mcp_tool_call"):
            continue
        t = e.get("type")
        if t == "item.started":
            started.add(item.get("id"))
        elif t == "item.completed":
            done.add(item.get("id"))
sys.exit(0 if started - done else 1)
PY
}

# Signal the whole process group: the wrapper forked `codex`, so killing the
# wrapper alone leaves the real worker running. Single-pid fallback for shells
# that reject the group form.
kill_worker_group() { kill -9 -- -"$1" 2>/dev/null || kill -9 "$1" 2>/dev/null; }

MODE="launch"
WORKERS=""
RUN_DIR=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --workers) [ $# -ge 2 ] || die "--workers requires a value"
               WORKERS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --wait)    MODE="wait";    shift ;;
    --resume)  MODE="resume";  shift ;;
    --cleanup) MODE="cleanup"; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
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

  # The authoritative roster of the run, in task order, written before anything
  # can fail. `pids.all` is not a substitute: launch_workers only records
  # workers it actually launched, so a failed-setup worker never appears there
  # and every re-attach path that rehydrated from it dropped the worker --
  # and with it the run's failure -- entirely.
  printf '%s\n' "${SLUGS[@]}" > "$RUN_DIR/roster"

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
  cp "$RUN_DIR/pids" "$RUN_DIR/pids.all"
}

# A stalled worker (exit-code 125, no status yet) is relaunched with the same
# brief in the same worktree — fresh session, one reasoning-effort step lower.
# Fresh session, not `resume`: the stalled session's transport state is exactly
# what cannot be trusted. The worktree is left as the stalled attempt left it;
# briefs describe an end state, so a partial attempt is a head start, not a
# hazard. Prior attempt logs are kept as *-attempt-N files.
#
# Returns 0 if anything was relaunched.
relaunch_stalled() {
  local round="$1" slug w wt brief pid n eff relaunched=0 stalled=()
  # Scan before touching anything: truncating `pids` on a run with no stalls
  # would erase the just-finished workers' records, which --wait re-attach and
  # the pid-per-worker bookkeeping still depend on.
  while read -r slug; do
    [ -n "$slug" ] || continue
    w="$OUT_DIR/$slug"
    [ -f "$w/status" ] && continue                       # already classified
    [ "$(cat "$w/exit-code" 2>/dev/null)" = "125" ] || continue
    stalled+=("$slug")
  done < <(roster)
  [ "${#stalled[@]}" -gt 0 ] || return 1

  : > "$RUN_DIR/pids"
  for slug in "${stalled[@]}"; do
    w="$OUT_DIR/$slug"
    wt="$WORKTREE_ROOT/$slug"
    brief="$(cat "$w/brief" 2>/dev/null)"
    [ -f "$brief" ] || { printf 'sol-parallel: %s: brief missing, cannot relaunch\n' "$slug" >&2; continue; }

    n=1
    while [ -e "$w/events-attempt-$n.jsonl" ]; do n=$((n + 1)); done
    mv "$w/events.jsonl" "$w/events-attempt-$n.jsonl" 2>/dev/null
    mv "$w/stderr.txt"   "$w/stderr-attempt-$n.txt"   2>/dev/null
    mv "$w/report.md"    "$w/report-attempt-$n.md"    2>/dev/null
    mv "$w/stall-reason" "$w/stall-reason-attempt-$n" 2>/dev/null

    eff="$(next_effort "$(cat "$w/effort" 2>/dev/null || echo "$EFFORT")")"
    printf '%s\n' "$eff" > "$w/effort"
    printf '%s\n' "$round" > "$w/stall-retries"
    printf 'sol-parallel: %s: relaunching after stall (attempt %d, effort %s)\n' \
      "$slug" $((n + 1)) "$eff" >&2

    rm -f "$w/exit-code"
    date +%s > "$w/started-at"
    nohup bash -c '
      codex exec --json -m "$1" -c model_reasoning_effort="$2" \
        -s workspace-write --color never -C "$3" \
        -o "$4/report.md" - < "$5" \
        > "$4/events.jsonl" 2> "$4/stderr.txt"
      printf "%s\n" "$?" > "$4/exit-code"
    ' _ "$MODEL" "$eff" "$wt" "$w" "$brief" >/dev/null 2>&1 &
    pid=$!
    disown "$pid" 2>/dev/null
    printf '%s\t%s\n' "$slug" "$pid" >> "$RUN_DIR/pids"
    relaunched=1
  done
  [ "$relaunched" -eq 1 ]
}

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
      # </dev/null: codex exec hangs forever on an open pipe stdin with no
      # writer (openai/codex#20919); the launch path is safe because it reads
      # the brief from stdin, but resume passes the prompt as an argument.
      codex exec resume "$6" --json -m "$1" -c model_reasoning_effort="$2" \
        -o "$4/report.md" "$(cat "$5")" \
        > "$4/events.jsonl" 2> "$4/stderr.txt" < /dev/null
      printf "%s\n" "$?" > "$4/exit-code"
    ' _ "$MODEL" "$EFFORT" "$WORKTREE_ROOT/$slug" "$w" "$w/correction-$n.md" \
        "$(cat "$w/session-id")" >/dev/null 2>&1 &
    pid=$!
    disown "$pid" 2>/dev/null
    printf '%s\t%s\n' "$slug" "$pid" >> "$RUN_DIR/pids"
  done < <(roster)
  [ "$pending" -gt 0 ] || die "no correction.md found in $OUT_DIR/*/"
}

worker_slugs() { cut -f1 "$RUN_DIR/pids"; }
pid_of() { awk -F'\t' -v s="$1" '$1 == s { print $2 }' "$RUN_DIR/pids"; }

# The full roster of the run, as opposed to worker_slugs() which is only the
# batch currently being waited on. These differ under --resume, where `pids` is
# rewritten to just the corrected workers; rehydrating from `pids` there would
# silently drop every other worker from summary.json.
#
# Prefer `roster`, written by create_worktrees. `pids.all` holds only the
# workers that were actually LAUNCHED, so a failed-setup worker is missing from
# it: --wait and --resume rehydrated a short roster, wrote a summary.json with
# the failed worker erased, and exited 0 after a launch that had correctly
# exited 1. Per parallel-flow.md §5 the re-attach path is the normal one, so
# that was the common case, not an edge case. Fall back to `pids.all` and then
# `pids` for a run directory created before `roster` existed.
#
# Deliberately NOT cleanup_slugs(): that walks OUT_DIR and sorts, which loses
# task order. summary.json's worker array is contractually in task order.
roster() {
  if   [ -s "$RUN_DIR/roster" ];   then cat "$RUN_DIR/roster"
  elif [ -f "$RUN_DIR/pids.all" ]; then cut -f1 "$RUN_DIR/pids.all"
  else worker_slugs; fi
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

post_process() {
  local i slug wt w code status session commit files
  for i in "${!SLUGS[@]}"; do
    slug="${SLUGS[i]}"; wt="${WORKTREES[i]}"; w="$OUT_DIR/$slug"

    # Idempotent: a worker already carries a status either from failed-setup
    # (create_worktrees, before any launch) or from a prior post_process pass.
    # The latter matters for --wait: if the original launcher wasn't actually
    # killed by its caller's timeout and ran to completion on its own, it
    # already classified and committed this worker. Re-running the commit
    # logic here would find a clean worktree (already committed) and
    # downgrade a real "ok" to "no-changes".
    if [ -f "$w/status" ]; then
      continue
    fi

    code="$(cat "$w/exit-code" 2>/dev/null || echo 1)"
    session=""; commit=""; files=""

    if [ "$code" = "125" ]; then
      # Stall-killed by the watchdog. Classified before the empty-events check:
      # a worker stalled before its very first event (e.g. the codex stdin
      # hang, openai/codex#20919) has an empty log but is a stall, not a
      # failed launch — the distinction drives the retry ladder and report.
      status="stalled"
      if [ -s "$w/events.jsonl" ]; then
        session="$(head -1 "$w/events.jsonl" \
          | python3 -c 'import json,sys; print(json.loads(sys.stdin.readline() or "{}").get("thread_id",""))' \
          2>/dev/null)"
        printf '%s\n' "$session" > "$w/session-id"
      fi
    elif [ ! -s "$w/events.jsonl" ]; then
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
      elif [ -z "$(git -C "$wt" status --porcelain)" ] \
        && [ "$(git -C "$wt" rev-parse HEAD)" = "$(cut -f2 "$RUN_DIR/base")" ]; then
        status="no-changes"
      elif [ -z "$(git -C "$wt" status --porcelain)" ]; then
        # Clean tree but the branch has already moved past base: a resumed
        # worker that committed earlier and made no further edits this round.
        # There is nothing new to add/commit -- doing so anyway would find
        # "nothing to commit" and misreport a real "ok" as failed-commit.
        status="ok"
        commit="$(git -C "$wt" rev-parse HEAD)"
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

    # Cumulative since base, computed from the commit rather than from the
    # pre-commit porcelain: the resumed-no-op rung never touches the worktree,
    # and porcelain quotes "unusual" filenames -- notably one containing a
    # double quote, which `-c core.quotePath=false` alone does NOT unquote;
    # that setting only stops quoting for non-ASCII bytes, while git always
    # backslash-escapes literal double quotes in its default text output. `-z`
    # gives NUL-delimited, unquoted names regardless of content; translating
    # NUL to newline matches how files-changed is already stored and parsed.
    # This is the branch's whole diff, which is what a reviewer of it wants.
    if [ "$status" = "ok" ]; then
      files="$(git -C "$wt" diff --name-only -z \
        "$(cut -f2 "$RUN_DIR/base")" HEAD | tr '\0' '\n')"
    else
      # A non-ok worker can still have produced real work, and reporting nothing
      # for it is how that work went missing: failed-commit stages everything and
      # then has its commit rejected, failed-run and timed-out leave it in the
      # worktree untouched. With files_changed empty, summary.json gave the user
      # no way to find any of it -- detected, then never reported. Snapshot
      # everything the worktree holds that base does not: committed since base,
      # staged or unstaged against HEAD, and untracked. `sort -u` because the
      # three sources overlap; `-z | tr` for the same bare-filename reasons as
      # the ok path above.
      files="$( { git -C "$wt" diff --name-only -z \
                    "$(cut -f2 "$RUN_DIR/base")" HEAD 2>/dev/null
                  git -C "$wt" diff --name-only -z HEAD 2>/dev/null
                  git -C "$wt" ls-files -o --exclude-standard -z 2>/dev/null
                } | tr '\0' '\n' | LC_ALL=C sort -u)"
    fi

    if [ -f "$w/started-at" ]; then
      printf '%s\n' "$(( $(date +%s) - $(cat "$w/started-at") ))" > "$w/elapsed"
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
  EFFORT_DEFAULT="$EFFORT" \
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
        "effort_used": read(w / "effort") or os.environ.get("EFFORT_DEFAULT", ""),
        "stall_retries": int(read(w / "stall-retries") or 0),
        "stall_reason": read(w / "stall-reason"),
        "events_path": str(w / "events.jsonl"),
        "report_path": str(w / "report.md"),
        "stderr_path": str(w / "stderr.txt"),
    })

(run_dir / "summary.json").write_text(json.dumps(
    {"base_branch": base_branch, "base_sha": base_sha, "workers": workers},
    indent=2) + "\n")
PY
}

wait_for_workers() {
  local block="$1" slug pid started now live stall_reason ev last
  while :; do
    live=0
    while read -r slug; do
      [ -n "$slug" ] || continue
      # `-s`, not `-f`: the wrapper creates this file by redirection and fills it
      # an instant later, so an empty one means "not finished", not "exit 0".
      [ -s "$OUT_DIR/$slug/exit-code" ] && continue
      pid="$(pid_of "$slug")"
      if kill -0 "$pid" 2>/dev/null; then
        started="$(cat "$OUT_DIR/$slug/started-at" 2>/dev/null || echo 0)"
        now="$(date +%s)"
        if [ "$started" -gt 0 ] && [ $((now - started)) -gt "$WORKER_TIMEOUT" ]; then
          kill_worker_group "$pid"
          # Same shape as the re-stat below: a worker that finished in the
          # instant between the liveness check and this kill has already
          # recorded its real result, and 124 would overwrite it with a
          # fabricated timeout.
          [ -s "$OUT_DIR/$slug/exit-code" ] \
            || printf '124\n' > "$OUT_DIR/$slug/exit-code"
          continue
        fi
        # Stall watchdog. The absolute cap above cannot tell a worker deep in
        # productive silence from one that hung after `turn.started` and will
        # never speak again (a known codex failure shape at high reasoning
        # effort: openai/codex#24260, #23807 — its internal stream retries can
        # sit silent for many minutes). The event log is the heartbeat:
        #   - nothing substantive yet → allow FIRST_EVENT_TIMEOUT from launch
        #     (long: xhigh legitimately thinks silently before its first item)
        #   - substantive events exist → allow IDLE_TIMEOUT since the last
        #     write of any kind (mtime advances with every event)
        # Exit code 125 marks the kill as a stall so post_process can class it
        # `stalled` and the retry ladder can tell it apart from `timed-out`.
        stall_reason=""
        ev="$OUT_DIR/$slug/events.jsonl"
        if has_substantive_event "$ev"; then
          last="$(mtime_of "$ev")"
          if [ "$last" -gt 0 ] && [ $((now - last)) -gt "$IDLE_TIMEOUT" ] \
             && ! in_flight_item "$ev"; then
            stall_reason="no events for $((now - last))s (idle budget ${IDLE_TIMEOUT}s)"
          fi
        elif [ "$started" -gt 0 ] && [ $((now - started)) -gt "$FIRST_EVENT_TIMEOUT" ]; then
          stall_reason="no substantive event $((now - started))s after launch (budget ${FIRST_EVENT_TIMEOUT}s)"
        fi
        if [ -n "$stall_reason" ]; then
          kill_worker_group "$pid"
          if [ ! -s "$OUT_DIR/$slug/exit-code" ]; then
            printf '%s\n' "$stall_reason" > "$OUT_DIR/$slug/stall-reason"
            printf '125\n' > "$OUT_DIR/$slug/exit-code"
            printf 'sol-parallel: %s: stalled — %s\n' "$slug" "$stall_reason" >&2
          fi
          continue
        fi
        live=$((live + 1))
      else
        # Re-stat before concluding the worker vanished. The wrapper writes
        # `exit-code` and only THEN exits, so a worker that finished between the
        # check at the top of this iteration and the `kill -0` just above is not
        # gone-without-a-result — it is done. Forking `pid_of` in between widens
        # that window enough to land in it routinely under load. Treating it as
        # a kill overwrote a real exit code with 137, which post_process then
        # classified `failed-run`: a fully successful worker reported as failed,
        # its work never committed and left stranded in the worktree, and the
        # run exiting 1. Cheap stat, and the only thing standing between a
        # finished worker and a fabricated failure.
        if [ -s "$OUT_DIR/$slug/exit-code" ]; then
          continue
        fi
        # Genuinely gone without recording an exit code: killed or interrupted.
        # The wrapper may have been killed alone (an operator's `kill -9`, an OOM
        # kill), leaving `codex` orphaned — reap the group for the same reason
        # the timeout branch does, with the same single-pid fallback for a shell
        # that rejects the group form. Residual risk: if the pid has been
        # recycled since we recorded it, this signals an unrelated group; the
        # window is small and the alternative is a worker that runs unobserved
        # forever.
        kill -9 -- -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null
        printf '137\n' > "$OUT_DIR/$slug/exit-code"
      fi
    done < <(worker_slugs)
    [ "$live" -eq 0 ] && return 0
    [ "$block" -eq 1 ] || return 75
    sleep 2
  done
}

# Integration here means cherry-pick, which produces a commit with a NEW sha and
# an identical patch. Ancestry therefore reports genuinely integrated work as
# unmerged, and only coincides when the cherry-pick lands in the same second as
# the original commit — which is why a same-second test passed and real use would
# not have. `git cherry` marks '+' any commit whose patch is not upstream.
#
# Fails CLOSED. Every "I don't know" answer must come back as "not integrated",
# because the only caller uses a true answer to delete a branch and force-remove
# a worktree. `grep -c` always prints a number, so gating on the output alone
# turned a `git cherry` that never ran (base branch renamed or deleted after the
# run — an ordinary merged-and-pruned action) into "0 unmerged commits" and thus
# into a silent, unrecoverable removal of un-integrated work.
branch_integrated() {
  local base="$1" branch="$2" unmerged cherry rc
  git show-ref --verify --quiet "refs/heads/$branch" || return 1
  # If base no longer resolves, nothing below can judge anything.
  git rev-parse --verify --quiet "$base^{commit}" >/dev/null 2>&1 || return 1
  git merge-base --is-ancestor "$branch" "$base" 2>/dev/null && return 0
  # Capture `git cherry`'s own exit status, not just its (possibly empty) output.
  # It has to be read before anything else runs: reading PIPESTATUS after
  # `unmerged="$(... | grep -c ...)"` would report grep's status, not git's.
  cherry="$(git cherry "$base" "$branch" 2>/dev/null)"; rc=$?
  [ "$rc" -eq 0 ] || return 1
  unmerged="$(printf '%s\n' "$cherry" | grep -c '^+')"
  [ "$unmerged" = "0" ]
}

# Every worker directory the run ever created, not just roster()'s
# pids.all-derived list. create_worktrees creates the worktree and branch for
# a failed-setup worker before bootstrap runs, but launch_workers never adds
# a failed-setup slug to pids/pids.all -- it was never launched. roster()
# alone would make that worker's real branch and worktree permanently
# invisible to --cleanup: not reported as kept, not removed, just silently
# unreachable forever. Every worker directory under OUT_DIR is a strict
# superset of roster(), so walking it covers both without touching roster()'s
# own contract (used elsewhere for --resume/--wait bookkeeping).
cleanup_slugs() {
  local d
  for d in "$OUT_DIR"/*/; do
    [ -d "$d" ] && basename "$d"
  done | sort -u
}

# Removes the worktree and branch for every worker whose branch is fully
# merged into base; prints one `kept:` line per survivor so nothing a worker
# produced is ever stranded without the caller being told it exists. When in
# doubt (branch missing, removal partially failing) this errs toward keeping
# and reporting rather than silently discarding.
cleanup_run() {
  local base slug wt rm_ok br_ok d wt_dirty removable
  base="$(cut -f1 "$RUN_DIR/base")"
  # Say so once, out loud. branch_integrated fails closed on an unresolvable
  # base, so everything is about to be kept — without this line the operator
  # sees a --cleanup that cleans nothing up and is told nothing about why.
  if ! git rev-parse --verify --quiet "$base^{commit}" >/dev/null 2>&1; then
    printf 'sol-parallel: base ref %s no longer resolves (renamed or deleted?); integration cannot be verified, so nothing will be removed\n' \
      "$base" >&2
  fi
  while read -r slug; do
    [ -n "$slug" ] || continue
    wt="$WORKTREE_ROOT/$slug"

    if ! git show-ref --verify --quiet "refs/heads/sol/$slug"; then
      # No branch to check merge-base against: already cleaned up by a prior
      # --cleanup run, or removed by hand. `git merge-base --is-ancestor` on a
      # missing branch also returns non-zero -- indistinguishable from "not
      # merged" -- which would otherwise fall into the "kept" branch below and
      # print a worktree path for a branch that no longer exists. Report only
      # if a worktree is still orphaned there; otherwise there is nothing left
      # to strand and nothing to say.
      if [ -d "$wt" ]; then
        printf 'kept: sol/%s %s (orphaned worktree; branch no longer exists)\n' \
          "$slug" "$wt"
      fi
      continue
    fi

    # Integration is necessary but NOT sufficient. A failed-commit worker's
    # branch sits at base because nothing was ever committed, so it looks
    # trivially integrated while its real work is staged-but-uncommitted in the
    # worktree. Removing it destroys that work silently and unrecoverably.
    # Require all three: a terminal status that means success, a clean
    # worktree, and proven integration. Anything else is kept and named.
    wt_dirty=0
    [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ] && wt_dirty=1
    case "$(cat "$OUT_DIR/$slug/status" 2>/dev/null || echo unknown)" in
      ok|no-changes) removable=1 ;;
      *)             removable=0 ;;
    esac

    if [ "$removable" -eq 1 ] && [ "$wt_dirty" -eq 0 ] \
       && branch_integrated "$base" "sol/$slug"; then
      rm_ok=1; br_ok=1
      git worktree remove --force "$wt" >/dev/null 2>&1 || rm_ok=0
      git branch -q -D "sol/$slug" >/dev/null 2>&1 || br_ok=0
      if [ "$rm_ok" -eq 0 ] || [ "$br_ok" -eq 0 ]; then
        # Both removals are run under 2>/dev/null so a partial failure (one
        # succeeds, the other doesn't) would otherwise say nothing and leave
        # inconsistent state -- a branch with no worktree, or vice versa.
        # Surface it on both channels: stderr for an operator watching the
        # run, stdout (as a survivor) so the branch is never dropped from the
        # printed account of what's left.
        printf 'sol-parallel: %s: cleanup incomplete (worktree removed: %s, branch deleted: %s)\n' \
          "$slug" "$([ "$rm_ok" -eq 1 ] && echo yes || echo no)" \
          "$([ "$br_ok" -eq 1 ] && echo yes || echo no)" >&2
        printf 'kept: sol/%s %s (merged but cleanup failed: worktree removed=%s branch deleted=%s)\n' \
          "$slug" "$wt" \
          "$([ "$rm_ok" -eq 1 ] && echo yes || echo no)" \
          "$([ "$br_ok" -eq 1 ] && echo yes || echo no)"
      fi
    else
      # Never print a path that is not there: the worktree may have been removed
      # by hand while the branch survived.
      printf 'kept: sol/%s %s (%s%s)\n' "$slug" \
        "$([ -d "$wt" ] && printf '%s' "$wt" || printf '(worktree already removed)')" \
        "$(cat "$OUT_DIR/$slug/status" 2>/dev/null || echo unmerged)" \
        "$([ "$wt_dirty" -eq 1 ] && printf ', uncommitted work in the worktree')"
    fi
  done < <(cleanup_slugs)
  git worktree prune
}

case "$MODE" in
  launch)  preflight_launch ;;
  *)       [ -d "$OUT_DIR" ] || die "no run directory to $MODE: $OUT_DIR" ;;
esac

if [ "$MODE" = "wait" ]; then
  [ -f "$RUN_DIR/pids" ] || die "no pids file in $RUN_DIR"
  rehydrate
  wait_for_workers 0 || exit 75
  post_process
  write_summary
fi

if [ "$MODE" = "launch" ]; then
  create_worktrees; setup_status=$?
  [ "$DRY_RUN" -eq 1 ] && exit "$setup_status"
  launch_workers
  wait_for_workers 1
  # Stall retry ladder: each round relaunches every stalled worker one effort
  # step lower, then waits again. Bounded by SOL_STALL_RETRIES; workers that
  # stall with no rounds left fall through to post_process as `stalled`.
  stall_round=0
  while [ "$stall_round" -lt "$STALL_RETRIES" ]; do
    stall_round=$((stall_round + 1))
    relaunch_stalled "$stall_round" || break
    wait_for_workers 1
  done
  post_process
  write_summary
fi

if [ "$MODE" = "resume" ]; then
  [ -f "$RUN_DIR/pids.all" ] || die "no completed run in $RUN_DIR"
  resume_workers
  wait_for_workers 1
  rehydrate
  post_process
  write_summary
fi

# --cleanup reports on a finished run rather than re-judging it, so it exits
# here instead of falling through to the shared status-classification loop
# below.
if [ "$MODE" = "cleanup" ]; then
  [ -f "$RUN_DIR/pids.all" ] || die "no completed run in $RUN_DIR"
  cleanup_run
  exit 0
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
