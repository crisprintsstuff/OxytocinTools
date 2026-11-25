# `/digest` Commands

Schedule recurring channel summaries of recent repository activity (**Changelog**) or stale open pull requests waiting for review (**Review nudges**), with optional role pings.

- **Modules:** `bot/cogs/digests.py`, `bot/digests.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/digest add <repo> <channel> <type> [...]` | Yes | Schedule a periodic Changelog or Review nudges digest |
| `/digest list` | Yes | List scheduled digests and any errors |
| `/digest remove <digest>` | Yes | Remove a scheduled digest |
| `/digest run <digest>` | Yes | Post a digest immediately on demand |

---

## Digest Types

| `type` choice | What it posts | Behavior when empty |
|---|---|---|
| `Changelog` (`changelog`) | PRs merged and issues closed within the last `interval_days` (up to 15 each) | Posts `"No merged pull requests or closed issues in this period."` |
| `Review nudges` (`reviews`) | Non-draft open PRs older than `stale_days` with neither an approval nor requested changes | Skips posting during scheduled runs; `/digest run` reports that nothing is waiting |

---

## Command Reference

### `/digest add`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `repo` (`str`, required, autocompleted) — Tracked repository.
  - `channel` (`discord.TextChannel`, required) — Target channel (bot needs `View Channel`, `Send Messages`, `Embed Links`).
  - `type` (`Changelog (merged PRs & closed issues) | Review nudges (PRs waiting for review)`, required) — Digest type.
  - `interval_days` (`int`, range `1–30`, default `7`) — How often the digest posts (also defines the Changelog lookback window).
  - `stale_days` (`int`, range `1–30`, default `3`) — *Review nudges only*. Minimum PR age in days before nudging.
  - `mention` (`discord.Role`, optional) — Role pinged when a digest is posted (`@everyone` disallowed).
- **Behavior:**
  - Prevents duplicate `(repo, channel, type)` digests.
  - `Review nudges` leverages `prcards` CI/review cache (`REVIEW_STATUS_LOOKUPS` up to 25 PRs) to check whether a PR already has `APPROVED` or `CHANGES_REQUESTED` reviews.

### `/digest list`

List all scheduled digests in the server with interval, mention role, and any `last_error`.

### `/digest remove`

Stop and delete a scheduled digest (`digest` autocompleted).

### `/digest run`

Trigger an immediate digest delivery outside its `interval_days` schedule and report any delivery or GitHub errors.

---

## Examples

```
/digest add   repo:crisprintsstuff/OxyBrush  channel:#weekly-changelog  type:Changelog      interval_days:7
/digest add   repo:crisprintsstuff/OxyBrush  channel:#code-review       type:Review nudges  interval_days:2  stale_days:3  mention:@Reviewers
/digest run   digest:#1
/digest list
```
