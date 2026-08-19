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

## Visual tasks

Two capabilities the plain template does not reach for.

**Attaching images.** List them in a sidecar beside the brief —
`<run-dir>/tasks/NN-<slug>.images`, one path per line, `#` comments ignored, paths
absolute or relative to the repo root. The launcher passes each as `codex exec -i`,
and a missing path fails the run at preflight. (On a direct `codex exec` call, pass
`-i <file>` yourself, repeatable.) This puts images in front of Sol — a screenshot of the broken state, the mockup to match, the
chart that renders wrong. Verified: Sol reads them and describes their actual content.
Reference the attachment in `<task>` so it knows what the image is for:

```xml
<task>
The attached screenshot shows the settings panel overflowing its container at 320px.
Fix the layout so it matches the second attached image, which is the intended design.
</task>
```

**Asking for an asset.** Codex has a built-in `image_gen` tool, so a brief can ask for
a raster asset directly and get AI-generated pixels. Sol routes to code on its own when
the visual is code-native (a geometric shape, an icon belonging to an existing SVG
system), so state the intent, not the method. The criteria have to change shape,
because there is no test command for a picture:

```xml
<acceptance_criteria>
- `assets/title-bg.png` exists and `file` reports a PNG of at least 1024x768.
- It depicts a lighthouse on a cliff at dusk, in the painterly style of the
  attached reference — checked by looking at it, not by a command.
</acceptance_criteria>
```

Review such a run by opening the image, not by reading `git diff`: a binary shows only
`Binary files differ`, which certifies nothing.

## Parallel brief additions

In parallel mode each worker runs in its own isolated git worktree, so no two workers
ever touch the same working directory at once — but they share one repository's
history, and the launcher integrates their branches afterward by cherry-picking each
onto a common base. A scope fence keeps declared file scopes from overlapping, which is
what makes that integration conflict-free and keeps each worker's isolated-green
verification still meaningful once merged. Add this to every parallel brief's
`<non_goals>`, plus a git-operation fence — the launcher, not the worker, owns commits
and branches:

```xml
<non_goals>
- Touch only these paths: [the task's declared file scope]. Another worker's changes
  will be integrated onto the same base branch — staying inside your scope is what
  keeps that integration conflict-free.
- Do not commit, do not create or switch branches, do not run git rebase or merge.
  The launcher commits your work on its own branch.
</non_goals>
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
