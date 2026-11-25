# `/showcase` Commands

Keep dedicated Discord channels (such as `#features`, `#whats-new`, or `#release-highlights`) automatically synced **in place** from either a repository markdown file or GitHub release notes.

- **Modules:** `bot/cogs/showcases.py`, `bot/showcases.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/showcase file <repo> <channel> [path] [section] [branch] [title] [pin]` | Yes | Sync a channel board from a markdown file in the repository |
| `/showcase releases <repo> <channel> [section] [kind] [count] [title] [pin]` | Yes | Sync a channel board from recent GitHub release notes |
| `/showcase edit <showcase> [...]` | Yes | Modify path, branch, section filter, release kind/count, title, or pin state |
| `/showcase list` | Yes | List active showcases with jump links and any errors |
| `/showcase refresh <showcase>` | Yes | Force an immediate fetch and re-render |
| `/showcase remove <showcase> [delete_message]` | Yes | Delete a showcase and (by default) its Discord message(s) |

---

## How Showcases Work

### Markdown Cleaning & Heading Extraction (`section`)
- HTML comments (`<!-- ... -->`) and wrapper tags (`<details>`, `<summary>`, `<br>`) are normalized for clean Discord embed rendering.
- When `section` is specified (comma-separated, e.g. `New Features, Features, Added`), the parser:
  - Matches ATX headings (`#`, `##`, `###`, …) **case-insensitively**, including emoji-prefixed headings like `## 🚀 New Features`.
  - Ignores `#` lines inside fenced code blocks (`` ``` `` / `~~~`).
  - Preserves nested subsections (`###` inside `## Features`) until the next heading of the same or higher level.
  - On **releases** showcases, releases without a matching section are skipped cleanly (so a bugfix-only patch release won't clutter a `#features` showcase).

### Multi-Message Reconciliation, Pinning & Caching
- Long content automatically splits cleanly at paragraph/line boundaries across **up to 5 Discord embed messages** (`CHUNK_LIMIT = 3800` chars per message).
- Only the **last** message attaches the **🔄 Refresh** (15-second cooldown) and **View on GitHub** buttons so multi-message boards read like one continuous document.
- If `pin: True`, the **first** message is pinned once on creation/recreation (requires `Manage Messages` or `Pin Messages`).
- When content shrinks across fewer messages, extra trailing messages are deleted automatically; if any showcase message is manually deleted in Discord, the bot reconciles and reposts in exact reading order.
- Uses GitHub ETags backed by L1 memory + L2 SQLite `http_cache` (`sc:` keys), skipping Discord edits when the rendered content hash (`last_hash`) hasn't changed.

---

## Command Reference

### `/showcase file`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `repo` (`str`, required, autocompleted) — Tracked repository.
  - `channel` (`discord.TextChannel`, required) — Channel to post and maintain the showcase in.
  - `path` (`str`, default `"README.md"`) — Path to a UTF-8 markdown file in the repository (e.g. `FEATURES.md`, `docs/changelog.md`).
  - `section` (`str`, optional) — Comma-separated heading(s) to extract (omit to mirror the entire file).
  - `branch` (`str`, optional) — Git branch or tag override (defaults to repo's `default_branch`).
  - `title` (`str`, optional) — Custom embed title.
  - `pin` (`bool`, default `False`) — Pin the first showcase message.

### `/showcase releases`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `repo` (`str`, required, autocompleted) — Tracked repository.
  - `channel` (`discord.TextChannel`, required) — Target channel.
  - `section` (`str`, optional) — Optional comma-separated heading(s) to extract from each release body (e.g. `New Features, Features, Added`).
  - `kind` (`Stable releases only | All releases (incl. pre-releases) | Pre-releases only`, default `Stable releases only`) — Release filter (drafts are always ignored).
  - `count` (`int`, range `1–25`, default `10`) — Maximum number of recent matching releases to show.
  - `title` (`str`, optional) — Custom embed title.
  - `pin` (`bool`, default `False`) — Pin the first showcase message.

### `/showcase edit`

Update an existing showcase (`showcase` autocompleted) and immediately re-render it in Discord:
- File showcases accept `path`, `branch`, `clear_branch`.
- Releases showcases accept `kind`, `count`.
- Both accept `section`, `clear_section`, `title`, `clear_title`, `pin`.

### `/showcase list`, `/showcase refresh`, `/showcase remove`

- `/showcase list` displays all showcases in the server with jump links, message counts, and any `last_error`.
- `/showcase refresh <showcase>` forces an immediate refresh.
- `/showcase remove <showcase> [delete_message]` removes the showcase from SQLite first (so deletions don't trigger re-creation) and deletes its Discord message(s) by default.

---

## Examples

```
/showcase file      repo:crisprintsstuff/OxyBrush  channel:#features   path:FEATURES.md  pin:True
/showcase file      repo:my-org/app                channel:#overview   path:README.md    section:"Features, Highlights"
/showcase releases  repo:crisprintsstuff/OxyBrush  channel:#whats-new  section:"New Features, Features, Added"  kind:Stable releases only  count:10  pin:True
/showcase edit      showcase:#1  section:"Highlights"
/showcase refresh   showcase:#1
```
