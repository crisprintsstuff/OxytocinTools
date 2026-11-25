# `/watch` Commands

Automatically convert Discord role-mention messages and new forum channel posts into GitHub issues, complete with inline triage buttons, two-way comment synchronization, and automatic thread closure.

- **Modules:** `bot/cogs/watches.py`, `bot/watches.py`
- **Requires:** **Message Content Intent** enabled in the Discord Developer Portal.
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/watch role <role> <repo> [label] [channel]` | Yes | Messages mentioning `role` automatically file a GitHub issue |
| `/watch forum <forum> <repo> [label] [close_posts]` | Yes | Every new forum post automatically files a GitHub issue |
| `/watch edit <watch> [...]` | Yes | Edit role, channel/forum, label, or `close_posts` on an existing watch |
| `/watch list` | Yes | List active role and forum watches |
| `/watch remove <watch>` | Yes | Stop a watch |

---

## How Watches Work

### Filing & Access Rules
- Watches respect the server/repo [`/admin issue-access`](admin.md#admin-issue-access) policy and `BOT_OWNER_IDS`.
- Role watches enforce `ISSUE_COOLDOWN` for non-admin users; forum watches skip the per-user cooldown (use Discord's forum slowmode if needed).
- When a forum thread starter or message author is a plain `discord.User` (common in uncached forum starter messages), the bot resolves them to a `discord.Member` so role-based access and admin role checks evaluate accurately.
- Replying to someone else's message with `@trigger-role` files **their** message as the issue, credits them as the reporter, credits you as the filer, and includes attachments from both messages.
- Bare mentions with no text or attachments are ignored. Editing an existing message never triggers a duplicate filing.

### Interactive Reply & Triage Buttons
When a watch files an issue, the bot reacts with `✅` and replies with an embed showing the issue link, any similar open issues found, and persistent action buttons:
- **Open on GitHub** — Link button to the created issue.
- **Not an issue** — Usable by the reporter, filer, or a bot admin; closes the issue on GitHub as `not_planned` and posts a closure note.
- **Complete** *(admin only)* — Closes the issue on GitHub as `completed`.
- **Labels** *(admin only)* — Opens a modal to edit labels on the GitHub issue directly from Discord.
- **Comment** *(admin only)* — Opens a modal to post an official comment on the GitHub issue.

### Two-Way Comment Sync & Auto-Close
- Controlled server-wide via [`/admin comment-sync`](admin.md#admin-comment-sync) (enabled by default).
- In **role watches**: replying directly to the bot's `"Filed as #..."` reply adds a comment on GitHub (and reacts `💬`).
- In **forum watches**: follow-up messages posted inside the forum thread mirror to GitHub comments (`💬`), and new GitHub comments mirror back into the forum thread.
- When a GitHub issue closes:
  - The bot updates the reply embed and notifies the reporter.
  - On **forum watches** with `close_posts: True`, the bot posts a closing notice, adds a forum tag matching **Solved** / **Resolved** / **Fixed** / **Done** (if the issue closed as `completed` and such a tag exists), and archives + locks the post (requires `Manage Threads`).

> **Tip: Use an empty trigger role.** Create roles like `@bug-report` or `@feature-request` with **Allow anyone to `@mention` this role** turned on, and assign them to **nobody**. Members can mention them to file issues without pinging real users.

---

## Command Reference

### `/watch role`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `role` (`discord.Role`, required) — Role mention that triggers issue filing (`@everyone` and integration-managed roles are rejected).
  - `repo` (`str`, required, autocompleted) — Tracked repository where issues will be opened.
  - `label` (`str`, optional) — GitHub label automatically applied to filed issues (e.g. `bug`).
  - `channel` (`discord.TextChannel`, optional) — Restrict watch to this channel (and its threads); omit to watch all channels in the server.

### `/watch forum`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `forum` (`discord.ForumChannel`, required) — Forum channel to watch (bot needs `View Channel`, `Send Messages in Threads`, `Read Message History`, plus `Manage Threads` if `close_posts` is enabled).
  - `repo` (`str`, required, autocompleted) — Tracked repository.
  - `label` (`str`, optional) — Fixed label added to every post's issue (forum post tags matching existing GitHub repo labels by name are **also** added automatically).
  - `close_posts` (`bool`, default `True`) — Automatically archive, lock, and tag (`Solved`/`Resolved`/`Fixed`/`Done`) the forum post when the GitHub issue closes.

### `/watch edit`

Update an existing watch without removing and recreating it.

- **Parameters:**
  - `watch` (`int`, required, autocompleted) — Watch ID to modify.
  - `role`, `channel`, `clear_channel`, `label`, `clear_label`, `close_posts` — Only relevant options per watch type (`role`/`clear_channel` for role watches; `ForumChannel`/`close_posts` for forum watches).

### `/watch list` & `/watch remove`

List all configured watches in the server or delete one by ID (autocompleted).

---

## Examples

```
/watch role   role:@bug-report       repo:crisprintsstuff/OxyBrush  label:bug
/watch role   role:@feature-request  repo:crisprintsstuff/OxyBrush  label:enhancement  channel:#ideas
/watch forum  forum:#bug-forum       repo:crisprintsstuff/OxyBrush  label:triage       close_posts:True
/watch edit   watch:#2  label:needs-triage
```
