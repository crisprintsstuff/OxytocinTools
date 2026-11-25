# `/repo` Commands

Manage tracked GitHub repositories and inspect repository metadata or releases on demand.

- **Module:** `bot/cogs/repos.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/repo track <repo> [releases_channel]` | Yes | Start tracking a repository and optionally route releases right away |
| `/repo untrack <repo>` | Yes | Stop tracking a repository and delete all its routes, dashboards, watches, trackers, digests, templates, and showcases |
| `/repo list` | No | List all tracked repositories in this server and active route counts |
| `/repo latest <repo> [include_prereleases]` | No | Show the latest release of any accessible GitHub repository |
| `/repo info <repo>` | No | Show stars, forks, open issues/PRs, default branch, language, and last push |

---

## Command Reference

### `/repo track`

Start tracking a GitHub repository in the current server.

- **Permissions:** Bot admin (`Manage Server`, admin role, or `BOT_OWNER_IDS`). Server-only.
- **Parameters:**
  - `repo` (`str`, required) — Repository identifier (`owner/name`, `https://github.com/owner/name`, or `.git` URL).
  - `releases_channel` (`discord.TextChannel`, optional) — If provided, automatically creates an active `releases` route pointing all releases to this channel and records the initial baseline.
- **Behavior:**
  - Fetches repository metadata from GitHub to verify visibility and normalize canonical `owner/name` casing and `default_branch`.
  - Requires `View Channel`, `Send Messages`, and `Embed Links` in `releases_channel` if passed.
  - Fails if the repository is already tracked in this server.

### `/repo untrack`

Stop tracking a repository in the current server.

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `repo` (`str`, required, autocompleted from tracked repos) — Tracked repository to remove.
- **Behavior:**
  - Prompts with an ephemeral **Confirm / Cancel** view.
  - Before removing the repo from SQLite (which cascades across `routes`, `dashboards`, `watches`, `pr_trackers`, `issue_templates`, `digests`, and `showcases`), captures existing dashboard and showcase message IDs and deletes those Discord messages best-effort.

### `/repo list`

List every tracked repository in the current server with its active route count.

- **Permissions:** Anyone in the server.
- **Parameters:** None.

### `/repo latest`

Fetch and display the latest release of **any** GitHub repository (does not need to be tracked).

- **Permissions:** Anyone.
- **Parameters:**
  - `repo` (`str`, required, autocompleted from tracked repos) — `owner/name` or GitHub URL.
  - `include_prereleases` (`bool`, default `False`) — When `False`, queries `/releases/latest` (non-draft, non-prerelease). When `True`, fetches recent `/releases` and shows the newest non-draft release.

### `/repo info`

Display summary metadata (stars, forks, open issues + PRs, default branch, primary language, last push timestamp, and archived status) for any accessible GitHub repository.

- **Permissions:** Anyone.
- **Parameters:**
  - `repo` (`str`, required, autocompleted from tracked repos) — `owner/name` or GitHub URL.

---

## Examples

```
/repo track    repo:crisprintsstuff/OxyBrush  releases_channel:#releases
/repo info     repo:crisprintsstuff/OxyBrush
/repo latest   repo:crisprintsstuff/OxyBrush  include_prereleases:True
/repo list
/repo untrack  repo:crisprintsstuff/OxyBrush
```
