# `/dashboard` Commands

Maintain single edit-in-place channel overview dashboards for a repository's **Issues** or **Pull requests**, keeping channels clean and always current.

- **Modules:** `bot/cogs/dashboards.py`, `bot/dashboards.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/dashboard create <repo> <channel> [label] [pin] [type]` | Yes | Post a live Issues or Pull requests dashboard |
| `/dashboard list` | Yes | List active dashboards with jump links and any errors |
| `/dashboard remove <dashboard> [delete_message]` | Yes | Remove a dashboard and (by default) delete its Discord message |

---

## Dashboard Types

### Issues Dashboard (`type: Issues`, default)
- Open issue and pull request counts (reads newest 100 items; shows `100+` above that)
- The 15 newest open issues with labels and relative ages (filtered by `label` if provided)
- Up to 5 issues closed in the last 7 days
- **Interactive buttons:**
  - **Report button** — Opens the GitHub issue creation modal pre-filled with `label` and any matching [`/issue template-set`](issue.md#issue-template-set) template, using label-aware wording:

    | Dashboard `label` contains | Button Label | Modal Title |
    |---|---|---|
    | `bug` | 🐛 Report a bug | Bug report |
    | `enhancement` or `feature` | 💡 Suggest a feature | Feature request |
    | `doc` | 📚 Suggest a docs change | Docs issue |
    | `question` | ❓ Ask a question | Question |
    | *(other or no label)* | 📝 Open an issue | New issue |

  - **🔄 Refresh** — Anyone can press to force a refresh (15-second per-dashboard cooldown).
  - **View on GitHub** — Link to the repository's issues (or label-filtered) page.

### Pull Requests Dashboard (`type: Pull requests`)
- Counts: open PRs, drafts, ready to merge, and needing attention (failing checks, conflicts, or changes requested)
- 15 newest open PRs (drafts sorted last) with status icons (`✅`, `❌`, `⏳`, `👍`, `📝`, `👀`, `⚠️`, `⚪`)
- **Waiting longest for review** section and PRs merged in the last 7 days
- Shares ETag/API cache with `/pr track` cards
- Includes **🔄 Refresh** and **View on GitHub** buttons

### Reliability Notes
- Unchanged GitHub responses return HTTP `304 Not Modified` via ETags (`http_cache`) and skip Discord edits entirely.
- Issue dashboards automatically refresh right after a Discord user creates an issue for that repository.
- If someone deletes the dashboard message manually, the bot automatically re-posts it (use `/dashboard remove` to stop it permanently).

---

## Command Reference

### `/dashboard create`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `repo` (`str`, required, autocompleted) — Tracked repository.
  - `channel` (`discord.TextChannel`, required) — Target channel.
  - `label` (`str`, optional) — Only include issues/PRs carrying this GitHub label.
  - `pin` (`bool`, default `False`) — Pin the posted dashboard message (requires `Manage Messages` or `Pin Messages`).
  - `type` (`Issues | Pull requests`, default `Issues`) — Dashboard mode.

### `/dashboard list`

Show all dashboards in the server with direct jump links and any `last_error`.

### `/dashboard remove`

Remove a dashboard (`dashboard` autocompleted). When `delete_message` is `True` (default), deletes the Discord message too.

---

## Examples

```
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#issues        pin:True
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#bugs          label:bug
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#pull-requests type:Pull requests  pin:True
/dashboard list
/dashboard remove  dashboard:#2  delete_message:True
```
