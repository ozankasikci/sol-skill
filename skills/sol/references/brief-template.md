# Sol brief template

Fill this in and write it to `$SCRATCHPAD/sol-brief.md`, then pipe it into `codex exec` on stdin.
Piping avoids shell-quoting damage to code snippets and multi-line criteria.

Keep it short. Sol is a frontier model — it needs intent, contracts, and fences, not
line-by-line instructions. An over-specified brief produces worse code than a tight
contract, because it substitutes your guesses for the model's search.

## Implementation brief

```xml
<task>
[The user's request, verbatim.]

Plan:
- Goal: [one sentence]
- Likely files: [paths, with a one-line note on what each is for]
- Conventions to follow: [the specific ones that matter here — test framework,
  error-handling style, naming, the module this should mirror]
</task>

<acceptance_criteria>
- [Each item a runnable command or an observable behavior.]
- `pytest tests/test_auth.py` passes, including a case covering lockout after 5 attempts
- `npm run typecheck` is clean
- POST /login with a locked account returns 423, not 401
</acceptance_criteria>

<non_goals>
- [Explicit scope fence. Name the adjacent things you do NOT want touched.]
- Do not change the session-token format
- Do not add new dependencies
</non_goals>

<verification_loop>
Follow the conventions already in this repo. Add or update tests for the behavior you
change. Before finishing, run [the project's actual commands] and fix whatever they
surface. Do not report success on checks you did not run.
</verification_loop>

<action_safety>
No unrelated changes. No drive-by refactors, reformatting, or dependency bumps.
Confine edits to what the task requires.
</action_safety>

<output_contract>
End your final message with:
1. Files changed (paths only)
2. Exact commands you ran
3. The result of each command
</output_contract>
```

## Research brief

Only when the user explicitly names Sol as the researcher. Runs `-s read-only`.

```xml
<task>
[The question.] Today's date is [YYYY-MM-DD].
Repo context, if relevant: [what the answer will be used for]
</task>

<research_mode>
Search broadly. Prefer primary sources — official docs, changelogs, source code,
specs — over blog summaries. Prioritize current-year information; note when a source
is older than the window and may be stale.
</research_mode>

<citation_rules>
Every load-bearing claim needs a source URL. Mark each claim as EVIDENCE (directly
supported by a source) or INFERENCE (your reasoning from the evidence). Do not
present inference as evidence.
</citation_rules>

<output_contract>
A structured report of at most 600 words:
- Findings (the answer, ordered by importance)
- Evidence (claim → source URL)
- Open questions (what you could not establish)
No transcript of your search process.
</output_contract>
```

## Correction messages

Corrections resume the same session, so do **not** restate the brief. Send only the
delta, and make it checkable:

```
src/auth/lockout.py:42 — counter resets on every failed attempt, so lockout never
fires. Required: the counter persists across attempts within the window and locks at
5. `pytest tests/test_auth.py::test_lockout_after_five` must pass.
```

Three parts, every time: **where**, **what's wrong and what's required**, **what check must pass**.
