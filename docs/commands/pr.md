# `/pr` Commands

Maintain one live, edit-in-place status card per open pull request in a channel, complete with CI checks, review state, merge readiness, and optional per-PR timeline threads.

- **Modules:** `bot/cogs/prs.py`, `bot/prcards.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/pr track <repo> <channel> [...]` | Yes | Post live status cards for open PRs and track new ones automatically |
| `/pr edit <tracker> [...]` | Yes | Update channel, branch filter, draft/bot inclusion, threads, mention, or cleanup days |
| `/pr list` | Yes | List PR trackers, open card counts, and any errors |
| `/pr stop <tracker>` | Yes | Stop updating PR cards for a tracker (existing Discord cards stay in place) |

---

## Card Features & Timeline Threads

Each live PR card displays:
- **State & Color:** 🟢 Open, ⚪ Draft, 🟣 Merged, 🔴 Closed
- **Author & Branches:** `head → base`, creation / merge / close timestamps
- **Checks Summary:** Passed, failing (naming failed checks), or still running
- **Reviews:** Approvals, requested changes, and awaiting reviewers
- **Merge Status:** Ready to merge, merge conflicts, behind base branch, or blocked by branch protection
- **Diff Stats & Buttons:** Additions/deletions, changed files, commit count, plus **Open PR**, **Checks**, **Files**, and **🔄 Refresh** buttons

When `threads` is enabled (the default), the bot opens a thread underneath each PR card logging lifecycle changes:
- Ready for review, new commits pushed, checks failed / all passed, approved, changes requested, review requested, merge conflicts found / resolved, merged, closed, or reopened.
- When the PR closes or merges, the card performs one final update and archives the timeline thread.

**Notes:**
- **ETag & cache sharing:** PR cards share one GitHub response cache (`begin_cycle` / `end_cycle` + `http_cache`) with PR dashboards (`/dashboard create ... type:Pull requests`).
- **Permissions:** CI check summaries require the GitHub App's **Checks** and **Commit statuses** read-only permissions. Timeline threads require **Create Public Threads** and **Send Messages in Threads** in Discord.
- **Auto-cleanup:** Setting `cleanup_days` (`1–365`) automatically deletes merged/closed PR cards (and their threads) after `N` days.

---

## Command Reference

### `/pr track`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `repo` (`str`, required, autocompleted) — Tracked repository.
  - `channel` (`discord.TextChannel`, required) — Channel for PR cards.
  - `branch` (`str`, optional) — Target base branch filter (glob or `re:` regex, e.g. `main` or `release/*`).
  - `include_drafts` (`bool`, default `False`) — Create cards immediately for drafts instead of waiting until ready for review.
  - `include_bots` (`bool`, default `False`) — Include bot-opened PRs (e.g. Dependabot).
  - `threads` (`bool`, default `True`) — Open a timeline thread under each PR card.
  - `mention` (`discord.Role`, optional) — Role to ping when a new PR card is first posted (`@everyone` disallowed).
  - `cleanup_days` (`int`, range `0–365`, default `0`) — Auto-delete closed/merged cards after `N` days (`0` keeps forever).

### `/pr edit`

Modify `channel`, `branch`, `clear_branch`, `include_drafts`, `include_bots`, `threads`, `mention`, `clear_mention`, or `cleanup_days` on an existing tracker (`tracker` autocompleted). Validates permissions and duplicate `(repo, channel, branch)` configurations.

### `/pr list` & `/pr stop`

- `/pr list` shows all PR trackers in the server, their open card count, and any `last_error`.
- `/pr stop <tracker>` stops polling and updating cards for that tracker while leaving previously posted Discord cards intact.

---

## Examples

```
/pr track  repo:crisprintsstuff/OxyBrush  channel:#pull-requests  branch:main  mention:@reviewers  cleanup_days:14
/pr edit   tracker:#1  cleanup_days:30  threads:True
/pr list
/pr stop   tracker:#1
```
