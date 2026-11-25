# `/route` Commands

Configure event routes that push GitHub releases, commits, new issues, pull request lifecycle updates, and failed workflow runs to Discord channels.

- **Module:** `bot/cogs/routes.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/route add <repo> <channel> <event> [...]` | Yes | Create a new event route |
| `/route edit <route> [...]` | Yes | Update channel, filters, mention role, or auto-publish on an existing route |
| `/route list [repo]` | Yes | List routes, status (`active` / `paused`), and any delivery errors |
| `/route toggle <route>` | Yes | Pause or resume a route |
| `/route remove <route>` | Yes | Delete a route |
| `/route test <route>` | Yes | Post the most recent matching item on GitHub to test formatting and permissions |

---

## Supported Event Types

| `event` choice | Underlying stream | Supported filters |
|---|---|---|
| `Releases` (`releases`) | Published releases (drafts always excluded) | `release_kind`, `tag_pattern` |
| `Commits` (`commits`) | Commits pushed to a branch | `branch` (defaults to repo's `default_branch`) |
| `New issues` (`issues`) | Newly opened GitHub issues (excludes PRs) | — |
| `Pull requests` (`pulls`) | PR state transitions | `pr_events`, `branch`, `include_drafts`, `include_bots` |
| `Workflow failures` (`workflows`) | Failed GitHub Actions workflow runs | `branch` |

---

## Command Reference

### `/route add`

Create a route from a tracked repository to a Discord text/announcement channel.

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `repo` (`str`, required, autocompleted) — Tracked repository.
  - `channel` (`discord.TextChannel`, required) — Target Discord channel (bot needs `View Channel`, `Send Messages`, `Embed Links`).
  - `event` (`Releases | Commits | New issues | Pull requests | Workflow failures`, required) — Event stream to follow.
  - `release_kind` (`All releases | Stable releases only | Pre-releases only`, optional, default `All releases`) — *Releases only*.
  - `tag_pattern` (`str`, optional) — *Releases only*. Case-insensitive glob (e.g. `v2.*`) or regex prefixed with `re:` (e.g. `re:^v\d+\.\d+\.\d+$`).
  - `branch` (`str`, optional) — *Commits*: exact branch (verified on GitHub; defaults to repo's `default_branch`). *Pulls / Workflows*: glob or `re:` pattern matched against base/head branch.
  - `mention` (`discord.Role`, optional) — Role pinged when an item is delivered (`@everyone` disallowed).
  - `publish` (`bool`, default `False`) — Auto-publish (crosspost) messages when `channel` is a Discord Announcement channel.
  - `pr_events` (`str`, optional, autocompleted) — *Pull requests only*. Comma-separated subset of `opened`, `ready`, `merged`, `closed`, `reopened`, or `all`. Default when omitted: `opened, ready`.
  - `include_drafts` (`bool`, optional, default `False`) — *Pull requests only*. Also notify on draft PRs.
  - `include_bots` (`bool`, optional, default `False`) — *Pull requests only*. Also notify on bot-opened PRs (e.g. Dependabot).
- **Behavior:**
  - Rejects incompatible filter combinations (for example, passing `tag_pattern` on a `Commits` route) and duplicate routes.
  - Calls `poller.ensure_baseline(route)` immediately so historical events are **not** flooded into the channel.

### `/route edit`

Modify settings of an existing route without losing its route ID.

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `route` (`int`, required, autocompleted) — Route ID to edit.
  - `channel`, `release_kind`, `tag_pattern`, `branch`, `mention`, `clear_mention`, `publish`, `pr_events`, `include_drafts`, `include_bots` — All optional; only provided fields change. Pass `none` or `-` to clear `tag_pattern` or `branch` (on `pulls`/`workflows` routes); use `clear_mention:True` to remove the ping role.
- **Behavior:**
  - When a `commits` route switches `branch`, verifies the new branch exists on GitHub and refreshes the stream baseline.

### `/route list`

List configured routes (optionally filtered to one `repo`), grouped by repository, showing active/paused state and any `last_error`.

### `/route toggle`

Pause an active route or resume a paused route. When resuming, verifies the target channel still exists with required permissions and takes a fresh baseline so events that occurred while paused are not replayed.

### `/route remove`

Delete a route permanently.

### `/route test`

Fetch the live GitHub stream for `route`, find the newest item passing its filters, and deliver it immediately to the route's channel to verify embed layout, role pings, and channel permissions.

---

## Delivery & Reliability Notes

- **Shared feed fetching:** Multiple servers tracking the same `(repo, stream)` share one ETag-cached GitHub fetch per poll cycle.
- **Release notes wait window:** Releases published with empty notes are held for up to `RELEASE_NOTES_WAIT` minutes (default `30`) so editors have time to populate notes before announcement. Draft releases are never posted.
- **Auto-pause on channel loss:** If Discord returns `404 Unknown Channel` / `403 Missing Permissions` (or the channel is deleted), the route is automatically disabled, the error is recorded on the route, and an alert is sent if [`/admin alerts`](admin.md#admin-alerts) is set.

---

## Examples

```
/route add   repo:my-org/app  channel:#announcements  event:Releases  release_kind:Stable releases only  mention:@Updates  publish:True
/route add   repo:my-org/app  channel:#v2-releases    event:Releases  tag_pattern:v2.*
/route add   repo:my-org/app  channel:#changelog      event:Pull requests  pr_events:merged  branch:main
/route edit  route:#1  mention:@NewRole
/route test  route:#1
```
