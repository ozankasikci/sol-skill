#!/usr/bin/env bash
# Preflight for the /sol skill. Read-only: checks that the Codex CLI is installed,
# authenticated, and pointed at a usable model. Runs no research and edits nothing.
#
# Usage: bash scripts/check-codex.sh [model]
#   model  defaults to gpt-5.6-sol

set -uo pipefail

MODEL="${1:-gpt-5.6-sol}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
fail=0
warn=0

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail + 1)); }
note() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; warn=$((warn + 1)); }
hint() { printf '        %s\n' "$1"; }

printf '\n/sol preflight\n\n'

# 1. Codex CLI on PATH
if command -v codex >/dev/null 2>&1; then
  ok "codex on PATH — $(command -v codex)"
  if version=$(codex --version 2>/dev/null | head -1); then
    ok "version — ${version}"
  else
    note "could not read 'codex --version'"
  fi
else
  bad "codex not found on PATH"
  hint "install: npm i -g @openai/codex   (see https://github.com/openai/codex)"
  hint "then re-run this script"
  printf '\n%s check(s) failed.\n\n' "$fail"
  exit 1
fi

# 2. Non-interactive exec subcommand exists
if codex exec --help >/dev/null 2>&1; then
  ok "'codex exec' available (non-interactive mode)"
else
  bad "'codex exec' not available — CLI too old for this skill"
  hint "update: npm i -g @openai/codex@latest"
fi

# 3. Authentication
if [ -f "${CODEX_HOME}/auth.json" ]; then
  ok "authenticated — ${CODEX_HOME}/auth.json present"
else
  bad "not authenticated — no ${CODEX_HOME}/auth.json"
  hint "run: codex login"
fi

# 4. Model reachability. 'codex exec' is the only honest probe, and it costs a
#    request, so this only reports what is configured and whether the slug is
#    known to the local model cache.
config="${CODEX_HOME}/config.toml"
if [ -f "$config" ]; then
  configured=$(grep -E '^[[:space:]]*model[[:space:]]*=' "$config" 2>/dev/null | head -1 | sed 's/.*=[[:space:]]*//; s/"//g')
  effort=$(grep -E '^[[:space:]]*model_reasoning_effort[[:space:]]*=' "$config" 2>/dev/null | head -1 | sed 's/.*=[[:space:]]*//; s/"//g')
  if [ -n "${configured:-}" ]; then
    ok "config default model — ${configured}"
  else
    note "no default model in config.toml"
  fi
  if [ -n "${effort:-}" ]; then
    ok "config default effort — ${effort}"
  fi
else
  note "no ${config} — the skill passes -m/-c explicitly, so this is not fatal"
fi

cache="${CODEX_HOME}/models_cache.json"
if [ -f "$cache" ]; then
  if grep -q "$MODEL" "$cache" 2>/dev/null; then
    ok "target model '${MODEL}' present in local model cache"
  else
    note "target model '${MODEL}' not in local model cache"
    hint "the cache may just be stale; the skill will surface a real error if the slug is wrong"
    hint "override the model per-run: /sol uses -m, so edit the SKILL.md command or pass your own"
  fi
else
  note "no model cache yet — run codex once interactively to populate it"
fi

# 5. Git repo. Not required by Codex, but the skill's review step diffs the tree.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  ok "inside a git work tree — diff-based review will work"
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    note "working tree is dirty"
    hint "commit or stash first, so 'git diff' isolates exactly Sol's changes"
  else
    ok "working tree clean — Sol's diff will be isolated"
  fi
else
  note "not a git repo — the review phase cannot diff; commit history won't isolate changes"
fi

printf '\n'
if [ "$fail" -gt 0 ]; then
  printf '%s check(s) failed, %s warning(s). Fix the failures before running /sol.\n\n' "$fail" "$warn"
  exit 1
fi
printf 'Ready. %s warning(s).\n\n' "$warn"
