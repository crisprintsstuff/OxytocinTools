# `/issue` Commands & Context Menu

Create, view, and search GitHub issues directly from Discord, configure reusable per-label issue templates, and convert chat messages into issues with a right-click action.

- **Module:** `bot/cogs/issues.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/issue create <repo> [label]` | Controlled by access policy | Open the issue creation modal (pre-filled from matching template if configured) |
| `/issue view <repo> <number>` | No | Show details and current status of any issue or pull request |
| `/issue search <repo> <query> [state] [type]` | No | Search issues and pull requests in a repository |
| `/issue template-set <repo> [label]` | Yes | Configure custom modal title, placeholder, and pre-filled body |
| `/issue template-list [repo]` | Yes | List configured issue form templates |
| `/issue template-remove <repo> [label]` | Yes | Delete a repo-default or label-specific template |
| **Apps → Create GitHub issue** *(message context menu)* | Controlled by access policy | Right-click any chat message to file a GitHub issue pre-filled with its text & attachments |

---

## Access Control & Safeguards

Who can open issues (via `/issue create`, **Apps → Create GitHub issue**, dashboards, or `/watch`) is configured per server via [`/admin issue-access`](admin.md#admin-issue-access) and [`/admin issue-role`](admin.md#admin-issue-role), with optional per-repo overrides:

| Level | Who can file |
|---|---|
| `Bot admins only` *(default)* | Manage Server, admin role, `BOT_OWNER_IDS` |
| `Bot admins + the issue role` | Admins plus members holding the configured issue role |
| `Everyone` | Any server member |
| `Nobody (disabled)` | Disabled for everyone (including admins) |

Built-in protections:
- **Repo autocomplete filtering:** `/issue create` and the context-menu `RepoPicker` only list repositories the invoking user is permitted to file in.
- **Cooldown:** Non-admin users are rate-limited by `ISSUE_COOLDOWN` (default `60` seconds between filed issues). Admins are exempt.
- **Mention neutralization:** Text submitted by non-admins has `@username` neutralized with a zero-width space so Discord members cannot mass-ping GitHub users/teams.
- **Duplicate / related issue lookup:** After opening an issue, the confirmation embed queries open issues matching keywords from the title and displays up to 3 possible related issues.
- **Attachment re-hosting:** Files/images attached to a message-filed issue are uploaded to an orphan `discord-attachments` branch (or the server/global `media-repo` when configured) when the GitHub App has `Contents: Read and write` permission; otherwise Discord CDN links are included with an expiry note.

---

## Command Reference

### `/issue create`

Open the Discord modal to submit a new GitHub issue.

- **Permissions:** Server-only; checked against effective `issue-access` policy.
- **Parameters:**
  - `repo` (`str`, required, autocompleted from allowed tracked repos) — Target repository.
  - `label` (`str`, optional) — Pre-fills the Labels field in the modal and selects a matching `/issue template-set` template (or falls back to the repo's `*` default template).

### `/issue view`

Inspect any GitHub issue or pull request by number (the repository does not have to be tracked).

- **Parameters:**
  - `repo` (`str`, required, autocompleted from tracked repos) — `owner/name` or URL.
  - `number` (`int >= 1`, required) — Issue or PR number.

### `/issue search`

Search up to 10 matching issues and/or pull requests in a repository.

- **Parameters:**
  - `repo` (`str`, required, autocompleted) — `owner/name` or URL.
  - `query` (`str`, required) — GitHub search keywords.
  - `state` (`All | Open | Closed`, default `All`) — Filter by item state.
  - `type` (`Issues & PRs | Issues only | Pull requests only`, default `Issues & PRs`) — Restrict item type.

### `/issue template-set`

Open a modal to configure custom form hints and default markdown body for `/issue create` and dashboard **Report** buttons.

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `repo` (`str`, required, autocompleted) — Tracked repository.
  - `label` (`str`, optional) — Case-insensitive label name (e.g. `bug`, `feature`). Leave empty to configure the repository's fallback default template (`*`).

### `/issue template-list`

List all configured templates in the server (optionally filtered to one `repo`). Bot admin only.

### `/issue template-remove`

Remove a template for a `repo` and optional `label` (empty removes the repo default `*`). Bot admin only.

### Message Context Menu: `Create GitHub issue`

Right-click any message → **Apps → Create GitHub issue**:
- If the user has access to one tracked repo, opens `IssueModal` immediately with the first non-empty line as title and full message body + jump link + attachments.
- If multiple repos are allowed, shows an interactive `RepoPicker` select menu first.

---

## Examples

```
/issue template-set     repo:crisprintsstuff/OxyBrush  label:bug
/issue create           repo:crisprintsstuff/OxyBrush  label:bug
/issue search           repo:crisprintsstuff/OxyBrush  query:"crash on export"  state:Open
/issue view             repo:crisprintsstuff/OxyBrush  number:42
```
