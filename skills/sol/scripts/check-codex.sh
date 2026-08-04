#!/usr/bin/env bash
# Preflight for the /sol skill. Read-only: checks that the Codex CLI is installed,
# authenticated, and pointed at a usable model. Runs no research and edits nothing.
#
# Usage: bash scripts/check-codex.sh [--json] [model]
#   model  defaults to gpt-5.6-sol

set -uo pipefail

json=0
MODEL="gpt-5.6-sol"
model_set=0
for arg in "$@"; do
  if [ "$arg" = "--json" ]; then
    json=1
  elif [ "$model_set" -eq 0 ]; then
    MODEL="$arg"
    model_set=1
  fi
done

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
fail=0
warn=0
check_names=()
check_statuses=()
check_details=()

record_check() {
  local index="${#check_names[@]}"
  check_names[index]="$1"
  check_statuses[index]="$2"
  check_details[index]="$3"
}

append_hint() {
  local index=$((${#check_details[@]} - 1))
  if [ "$index" -ge 0 ]; then
    check_details[index]+=$'\n'
    check_details[index]+="$1"
  fi
}

json_escape() {
  local LC_ALL=C
  local value="$1"
  local escaped=""
  local character
  local code
  local sequence
  local i

  for ((i = 0; i < ${#value}; i++)); do
    character="${value:i:1}"
    case "$character" in
      '"') escaped+='\"' ;;
      \\)  escaped+='\\' ;;
      *)
        printf -v code '%d' "'$character"
        if [ "$code" -lt 0 ]; then
          code=$((code + 256))
        fi
        if [ "$code" -lt 32 ]; then
          printf -v sequence '\\u%04x' "$code"
          escaped+="$sequence"
        else
          escaped+="$character"
        fi
        ;;
    esac
  done

  JSON_ESCAPED="$escaped"
}

print_json() {
  local ready=true
  local i
  local name
  local status
  local detail

  if [ "$fail" -gt 0 ]; then
    ready=false
  fi

  json_escape "$MODEL"
  printf '{"ready":%s,"failures":%s,"warnings":%s,"model":"%s","checks":[' \
    "$ready" "$fail" "$warn" "$JSON_ESCAPED"

  for ((i = 0; i < ${#check_names[@]}; i++)); do
    if [ "$i" -gt 0 ]; then
      printf ','
    fi
    json_escape "${check_names[i]}"
    name="$JSON_ESCAPED"
    json_escape "${check_statuses[i]}"
    status="$JSON_ESCAPED"
    json_escape "${check_details[i]}"
    detail="$JSON_ESCAPED"
    printf '{"name":"%s","status":"%s","detail":"%s"}' "$name" "$status" "$detail"
  done

  printf ']}\n'
}

ok() {
  if [ "$json" -eq 1 ]; then
    record_check "$2" "ok" "$1"
  else
    printf '  \033[32mok\033[0m    %s\n' "$1"
  fi
}

bad() {
  if [ "$json" -eq 1 ]; then
    record_check "$2" "fail" "$1"
  else
    printf '  \033[31mFAIL\033[0m  %s\n' "$1"
  fi
  fail=$((fail + 1))
}

note() {
  if [ "$json" -eq 1 ]; then
    record_check "$2" "warn" "$1"
  else
    printf '  \033[33mwarn\033[0m  %s\n' "$1"
  fi
  warn=$((warn + 1))
}

hint() {
  if [ "$json" -eq 1 ]; then
    append_hint "$1"
  else
    printf '        %s\n' "$1"
  fi
}

if [ "$json" -eq 0 ]; then
  printf '\n/sol preflight\n\n'
fi

# 1. Codex CLI on PATH
if command -v codex >/dev/null 2>&1; then
  ok "codex on PATH — $(command -v codex)" "codex_on_path"
  if version=$(codex --version 2>/dev/null | head -1); then
    ok "version — ${version}" "codex_version"
  else
    note "could not read 'codex --version'" "codex_version"
  fi
else
  bad "codex not found on PATH" "codex_on_path"
  hint "install: npm i -g @openai/codex   (see https://github.com/openai/codex)"
  hint "then re-run this script"
  if [ "$json" -eq 1 ]; then
    print_json
  else
    printf '\n%s check(s) failed.\n\n' "$fail"
  fi
  exit 1
fi

# 2. Non-interactive exec subcommand exists
if codex exec --help >/dev/null 2>&1; then
  ok "'codex exec' available (non-interactive mode)" "codex_exec"
else
  bad "'codex exec' not available — CLI too old for this skill" "codex_exec"
  hint "update: npm i -g @openai/codex@latest"
fi

# 3. Authentication
if [ -f "${CODEX_HOME}/auth.json" ]; then
  ok "authenticated — ${CODEX_HOME}/auth.json present" "authentication"
else
  bad "not authenticated — no ${CODEX_HOME}/auth.json" "authentication"
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
    ok "config default model — ${configured}" "config_model"
  else
    note "no default model in config.toml" "config_model"
  fi
  if [ -n "${effort:-}" ]; then
    ok "config default effort — ${effort}" "config_effort"
  fi
else
  note "no ${config} — the skill passes -m/-c explicitly, so this is not fatal" "config_model"
fi

cache="${CODEX_HOME}/models_cache.json"
if [ -f "$cache" ]; then
  if grep -q "$MODEL" "$cache" 2>/dev/null; then
    ok "target model '${MODEL}' present in local model cache" "model_cache"
  else
    note "target model '${MODEL}' not in local model cache" "model_cache"
    hint "the cache may just be stale; the skill will surface a real error if the slug is wrong"
    hint "override the model per-run: /sol uses -m, so edit the SKILL.md command or pass your own"
  fi
else
  note "no model cache yet — run codex once interactively to populate it" "model_cache"
fi

# 5. Git repo. Not required by Codex, but the skill's review step diffs the tree.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  ok "inside a git work tree — diff-based review will work" "git_work_tree"
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    note "working tree is dirty" "git_tree_state"
    hint "commit or stash first, so 'git diff' isolates exactly Sol's changes"
  else
    ok "working tree clean — Sol's diff will be isolated" "git_tree_state"
  fi
else
  note "not a git repo — the review phase cannot diff; commit history won't isolate changes" "git_work_tree"
fi

if [ "$json" -eq 1 ]; then
  print_json
  if [ "$fail" -gt 0 ]; then
    exit 1
  fi
  exit 0
fi

printf '\n'
if [ "$fail" -gt 0 ]; then
  printf '%s check(s) failed, %s warning(s). Fix the failures before running /sol.\n\n' "$fail" "$warn"
  exit 1
fi
printf 'Ready. %s warning(s).\n\n' "$warn"
