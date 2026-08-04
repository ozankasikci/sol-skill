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
4. **Preflight coverage.** A real failure mode `skills/sol/scripts/check-codex.sh` doesn't catch yet.

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

Most of the deliverable is a prompt, so most verification is by using it. The one part
with real tests is the progress watcher:

```bash
python3 skills/sol/scripts/tests/test_sol_watch.py   # watcher: 43 checks
bash skills/sol/scripts/check-codex.sh               # preflight still passes
```

If you change the watcher's classification rules, add a fixture line rather than only
adjusting an assertion — the fixtures are the record of what codex actually emits.

Then run the modified skill against a real task in a real repo, on a clean tree, and
report in your PR: the task, the number of correction rounds, and whether the review
caught anything. A before/after on the same task is ideal.

For manifest changes, confirm the JSON parses and that the version agrees between
`.claude-plugin/plugin.json` and the `version:` field in `skills/sol/SKILL.md` — CI
checks this, but it's faster to check locally.

## Releasing

This repo is the plugin source. The Claude Code marketplace that serves it lives in
[ozankasikci/claude-plugins](https://github.com/ozankasikci/claude-plugins), which
references this repo by `source: {source: github, repo: ozankasikci/sol-skill}`.

Three steps, and the third is easy to forget:

1. Bump the version in `.claude-plugin/plugin.json` and `skills/sol/SKILL.md` (CI enforces they match).
2. Add a `CHANGELOG.md` entry, commit, and tag.
3. Bump the pinned `version` for the `sol` entry in the marketplace repo, or installs
   keep resolving to the old number.
