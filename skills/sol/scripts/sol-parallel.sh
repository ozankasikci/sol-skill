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

set -uo pipefail

MODEL="${SOL_MODEL:-gpt-5.6-sol}"
EFFORT="${SOL_EFFORT:-xhigh}"
WORKER_TIMEOUT="${SOL_WORKER_TIMEOUT:-1800}"

die() { printf 'sol-parallel: %s\n' "$1" >&2; exit "${2:-2}"; }

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

worker_slugs() { cut -f1 "$RUN_DIR/pids"; }
pid_of() { awk -F'\t' -v s="$1" '$1 == s { print $2 }' "$RUN_DIR/pids"; }

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
