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
    --workers) WORKERS="${2:-}"; shift 2 ;;
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
