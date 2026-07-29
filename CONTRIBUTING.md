# Contributing

Thanks for taking a look. This is a small, single-skill repo — the whole runtime is
[`skills/sol/SKILL.md`](skills/sol/SKILL.md) plus its brief template.

## What's most useful

In rough order of value:

1. **Brief-contract improvements.** A block or phrasing that measurably reduces
   correction rounds. If you found that a particular `<verification_loop>` wording
   makes Sol actually run the checks instead of claiming it did, that's the good stuff.
2. **Review-phase heuristics.** A class of bug the current review pass reliably misses,
   plus the check that catches it.
3. **Host compatibility.** Making the skill work correctly on an Agent Skills host
   where it currently doesn't.
4. **Preflight coverage.** A real failure mode `scripts/check-codex.sh` doesn't catch yet.

## Ground rules

- **Claims need evidence.** If a change is supposed to improve output quality, say what
  you ran and what you observed. "Feels better" is not reviewable; "cut corrections from
  2 rounds to 0 on these 3 tasks" is. Sample size can be small — just be honest about it.
- **Don't grow the brief without cause.** The skill's central bet is that a tight
  contract beats a long one. Additions that make the brief more prescriptive need to
  justify themselves against that.
- **Keep the role split intact.** Any change that lets the planner edit production code,
  or lets Sol's self-report count as verification, defeats the point of the skill.
- **`disable-model-invocation: true` stays.** Spending a second model's budget is the
  user's explicit call.

## Testing a change

There's no test suite — the deliverable is a prompt. Verify by using it:

```bash
bash scripts/check-codex.sh          # preflight still passes
```

Then run the modified skill against a real task in a real repo, on a clean tree, and
report in your PR: the task, the number of correction rounds, and whether the review
caught anything. A before/after on the same task is ideal.

For manifest changes, confirm the JSON parses and versions agree across
`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, and the `version:`
field in `skills/sol/SKILL.md` — CI checks this, but it's faster to check locally.

## Releasing

Bump the version in all three places above, add a `CHANGELOG.md` entry, and tag.
