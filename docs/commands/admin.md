# `/admin` Commands

Server-level administration, roles, issue permissions, diagnostics, configuration backups, chat integrations, and error alerting.

- **Module:** `bot/cogs/admin.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Who Is a Bot Admin?

All `/admin` commands (and all 🔒 commands across the bot) require one of:
1. Discord **Administrator** or **Manage Server** (`manage_guild`) permission in the server
2. Membership in the server's configured bot admin role (`/admin set-role`)
3. User ID listed in `BOT_OWNER_IDS` in `.env` (applies globally across servers)

---

## Summary

| Command | Description |
|---|---|
| `/admin set-role <role>` | Grant a Discord role access to all bot admin commands |
| `/admin clear-role` | Remove the bot admin role |
| `/admin issue-access <level> [repo]` | Set who can create GitHub issues (server default or per-repo override) |
| `/admin issue-role [role]` | Set or clear the role used by `Bot admins + the issue role` |
| `/admin status` | Show counts, poll schedule, GitHub API rate limits, issue rules, and errors |
| `/admin check-now` | Trigger an immediate GitHub poll cycle |
| `/admin export` | Download this server's configuration as a JSON file |
| `/admin import <file>` | Restore server config from an exported JSON file (idempotent) |
| `/admin sync` | Force-sync global slash commands with Discord |
| `/admin autolink <enabled>` | Toggle automatic `#123` / `repo#123` preview embeds in chat |
| `/admin comment-sync <enabled>` | Toggle two-way comment syncing on Discord-filed issues |
| `/admin media-repo [repo]` | Set or clear a dedicated repo for hosting Discord attachments |
| `/admin alerts [channel]` | Set or disable a private staff channel for error alerts |

---

## Command Reference

### Roles & Permissions

#### `/admin set-role <role>`
Sets the server's bot admin role (`guild_settings.admin_role_id`). Members with this role gain access to all 🔒 commands and bypass the non-admin issue cooldown.

#### `/admin clear-role`
Clears the configured bot admin role so only **Manage Server** / **Administrator** / `BOT_OWNER_IDS` retain admin access.

---

### Issue Access Control

#### `/admin issue-access <level> [repo]`
Sets who can open issues from Discord (via `/issue create`, message context menu, dashboards, and `/watch`):
- `level` choices:
  - **Bot admins only** (`admin`) — default
  - **Bot admins + the issue role** (`role`)
  - **Everyone** (`everyone`)
  - **Nobody (disabled)** (`disabled`)
  - **Use server default (repo only)** (`inherit`) — only valid when `repo` is provided; clears that repo's override
- `repo` (optional, autocompleted) — Apply as a per-repo override instead of the server default.

#### `/admin issue-role [role]`
Sets the Discord role permitted when `issue-access` is **Bot admins + the issue role**. Omit `role` to clear it.

---

### Diagnostics & Polling

#### `/admin status`
Displays an ephemeral diagnostic embed containing:
- Tracked repos, active/total routes, dashboards, watches, PR trackers, digests, showcases, admin role, and alert channel
- Poll interval, last run time, next scheduled run, and shared feed count across all servers
- Active GitHub authentication mode and live per-bucket rate limits + reset timestamps
- Effective issue-creation defaults, overrides, cooldown, `autolink`, `comment-sync`, and `media-repo`
- Any `last_error` values across routes, streams, dashboards, PR trackers, digests, and showcases

#### `/admin check-now`
Wakes the poller immediately (`poller.trigger()`) instead of waiting for the next `POLL_INTERVAL` tick.

#### `/admin sync`
Forces `tree.sync()` globally with Discord (normally skipped on boot unless the command schema changed).

---

### Backup & Restore

#### `/admin export`
Exports this server's full configuration (guild settings, tracked repos, routes, dashboards, watches, PR trackers, issue templates, digests, and showcases) as `oxytocintools-<guild_id>.json`.

#### `/admin import <file>`
Uploads and imports a JSON backup created by `/admin export` (max 1 MB) for the current server. Existing items are matched by signature and skipped without duplicating, then `poller.trigger()` runs immediately.

---

### Chat Integrations, Media Hosting & Alerts

#### `/admin autolink <enabled>`
Turns automatic `#123` / `owner/name#123` issue & PR preview embeds on or off for this server (requires Message Content Intent).

#### `/admin comment-sync <enabled>`
Turns two-way comment syncing (Discord replies / forum thread messages ↔ GitHub issue comments) on or off for this server.

#### `/admin media-repo [repo]`
Sets a dedicated GitHub repository (`owner/name` or URL) for hosting Discord attachments uploaded via issues/watches (stored on its `discord-attachments` branch under `attachments/<owner>_<name>/`). Omit `repo` to revert to hosting in each issue's own repository (or `MEDIA_REPO` env fallback).

#### `/admin alerts [channel]`
Sets a private staff channel where the bot posts deduplicated error alerts (6-hour cooldown per guild + error key) for failing routes, dashboards, showcases, PR trackers, digests, and attachment uploads. Omit `channel` to disable alerts.

---

## Examples

```
/admin set-role      role:@Maintainers
/admin issue-role    role:@Testers
/admin issue-access  level:Bot admins + the issue role
/admin media-repo    repo:my-org/discord-media
/admin alerts        channel:#bot-alerts
/admin status
/admin export
```
