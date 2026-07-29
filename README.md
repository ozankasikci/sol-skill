# /sol — Claude plans, Sol implements, Claude reviews

**Two frontier models, one job each. The model that wrote the diff never grades it.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-6f42c1?style=flat-square)](https://docs.claude.com/en/docs/claude-code/overview)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-000000?style=flat-square)](https://agentskills.io)

**Claude Code (recommended — auto-updates via marketplace):**
```
/plugin marketplace add ozankasikci/sol-skill
/plugin install sol
```

**Cursor, Copilot, Gemini CLI, or any of 50+ [Agent Skills](https://agentskills.io) hosts:**
```
npx skills add ozankasikci/sol-skill -g
```

Then, in any repo:
```
/sol add rate limiting to the upload endpoint
```

Requires the [Codex CLI](https://github.com/openai/codex) (`codex login`) and a ChatGPT plan that includes it. No API keys, no extra subscription beyond the two you already have.

---

## The problem this fixes

Single-model agentic coding has a structural blind spot: **the model that wrote the code also decides whether the code is good.** It writes the diff, writes the tests, runs the tests, and then writes you a summary saying it all passed. You are reading a self-assessment from the author.

That is not a small bias. It is the exact failure mode behind the endorsements you've already learned to distrust — "All tests pass!" (it didn't run them), "Fixed!" (it changed the test), "Done — fully working" (one path works). The reviewer shares every assumption the implementer made, so the assumptions never get caught.

`/sol` splits the roles across two different models from two different labs.

| | Model | Job | Never does |
|---|---|---|---|
| **Planner / reviewer** | Claude (your Claude Code session) | Writes the brief, reviews the real diff, re-runs the checks itself, directs corrections | Never edits production code |
| **Implementer** | GPT-5.6 Sol at `xhigh` reasoning, via Codex CLI | All code changes, adds tests, runs the verification loop | Never approves its own work |

Claude never touches the code. Sol never signs off on it. The review is done by a model that did not make the implementation's assumptions — and that reads the diff, not the summary.

## How it runs

Five phases, and the interesting part is what each one refuses to do.

**1. Plan.** Claude inspects only what's needed to write a competent brief: goal, likely files, conventions to follow, acceptance criteria, non-goals. It deliberately does *not* over-specify. Sol is a frontier model — an over-detailed brief substitutes the planner's guesses for the implementer's search, and produces worse code. Intent and constraints, not line-by-line instructions.

**2. Implement.** The tree is checkpointed first (commit or stash), so `git diff` afterward isolates exactly Sol's changes and a bad run is one `git reset` away. The brief goes to a file and is piped in on stdin — no shell-quoting damage to code snippets:

```bash
codex exec -m gpt-5.6-sol -c model_reasoning_effort=xhigh \
  -s workspace-write --color never \
  -o "$SCRATCHPAD/sol-report.md" \
  - < "$SCRATCHPAD/sol-brief.md" > "$SCRATCHPAD/sol-log.txt" 2>&1
```

The brief is compact XML blocks, not prose — `<task>`, `<acceptance_criteria>`, `<non_goals>`, `<verification_loop>`, `<action_safety>`, `<output_contract>`. GPT-5.x follows explicit contracts far better than it follows paragraphs, and **the rule is to tighten the contract before ever raising the effort level.** Template in [`skills/sol/references/brief-template.md`](skills/sol/references/brief-template.md).

Acceptance criteria must be checkable. Not "auth is robust" — `pytest tests/test_auth.py passes with 5-attempt lockout covered`. A criterion the reviewer can't run is a criterion nobody enforces.

**3. Review the diff, not the summary.** This is the whole point, so it's the strictest phase. Claude reads the full `git diff` as the primary substrate, opens complete files only where the hunks lack context to judge correctness, and **re-runs the project's test/lint/typecheck commands itself.** Sol's own report is read for what it claims — never trusted as verification. Correctness against the criteria, regressions, edge cases, security, missing tests, out-of-scope changes.

**4. Corrections — capped at 2 rounds.** Blocking issues resume the same Codex session, so corrections send only the delta: `file:line — observed problem, required behavior, check that must pass`. Not a restatement of the brief. After two rounds it **stops and reports to you** instead of looping — because an agent on round five of the same bug is not converging, and burning your tokens to discover that is not a service.

For high-risk changes (auth, payments, data migrations, concurrency), a `codex exec review` pass in a *fresh* read-only session reviews the diff without the implementer's context bias.

**5. Report.** Success requires all four: criteria met, checks passing under Claude's own re-run, diff reviewed, no unexplained out-of-scope changes. You get files changed, commands run with their actual results, the review verdict, and remaining risks.

## Research mode

`/sol` routes on intent. Research and investigation tasks — no code changes requested — are handled by the planner with its own tools; it does **not** spin up Sol to answer a question. Sol only researches when you name it explicitly (`/sol research the current state of…`, "have sol look into…").

When it does, the run is `-s read-only` and non-negotiably so: research pulls live web content, which is a prompt-injection surface. Sol's output is treated as data, never as instructions. Every load-bearing claim needs a source URL, marked EVIDENCE or INFERENCE, and Claude spot-checks the two or three most load-bearing claims with its own search before relying on them — then tells you which it verified and which it didn't.

## Install

| Surface | Install | Updates |
|---|---|---|
| **Claude Code** (recommended) | `/plugin marketplace add ozankasikci/sol-skill` then `/plugin install sol` | Auto via marketplace, or `claude plugin update sol@sol-skill` |
| **Cursor, Copilot, Gemini CLI, + 50 more** | `npx skills add ozankasikci/sol-skill -g` | `npx skills update sol -g` |
| **Manual** | Copy `skills/sol/` to `~/.claude/skills/sol/` | `git pull` and re-copy |

`-g` installs globally for your user, available in every project. Drop it to scope per-project.

### Prerequisites

```bash
npm i -g @openai/codex   # the Codex CLI
codex login              # ChatGPT plan that includes Codex
```

Then verify everything is wired up — read-only, runs no research, edits nothing:

```bash
bash scripts/check-codex.sh
```

It checks the CLI is on PATH, `codex exec` exists, you're authenticated, the model slug resolves, and you're in a clean git tree (which the diff-based review depends on). Failures come with the exact fix.

### Choosing a different implementer

The model is passed explicitly with `-m`, so swap it in [`skills/sol/SKILL.md`](skills/sol/SKILL.md) if you'd rather delegate to a different Codex model. `xhigh` reasoning is the default because implementation quality is the thing being bought here; it is slower, and the skill budgets a 10-minute timeout accordingly.

## What it deliberately does not do

Worth stating plainly, since these are all choices and not omissions:

- **No auto-invocation.** `disable-model-invocation: true` — `/sol` fires only when you type it. Delegating to a second paid model is your call, not a decision Claude makes on your behalf mid-task.
- **No unbounded correction loops.** Two rounds, then it reports.
- **No trusting the implementer's report.** Checks are re-run by the reviewer or they don't count.
- **No drive-by refactors.** `<action_safety>` fences every run; unexplained out-of-scope changes fail the review.
- **No writes during research.** `read-only`, always.

## Requirements

- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) (or any Agent Skills host with Bash access)
- [Codex CLI](https://github.com/openai/codex) ≥ 0.144, authenticated via `codex login`
- A git repository — the review phase diffs the working tree
- Two subscriptions you likely already have: Claude and ChatGPT. No API keys.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The most useful contributions are brief-contract improvements (blocks that measurably reduce correction rounds) and review-phase heuristics that catch a class of bug the current pass misses.

## License

MIT — see [LICENSE](LICENSE).

Repo layout modeled on [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill).
