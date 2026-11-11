# OxytocinTools — GitHub Repo Tracker for Discord

A Discord bot that watches your GitHub repositories and posts **releases, commits,
issues, and pull requests** to the channels you choose, and lets your community
**create GitHub issues straight from Discord**. Routing rules let you send
specific releases to specific channels: stable releases to `#announcements`,
pre-releases to `#beta-testers`, `v2.*` tags to `#v2`, and so on.

- Slash commands, with autocomplete for tracked repos and routes
- Per-route filters: stable vs. pre-release, tag pattern (glob or regex), and branch for commits
- Optional role ping per route, and auto-publish in announcement channels
- Create issues with `/issue create` or by right-clicking a message, with configurable access and a cooldown
- Admin permissions: **Manage Server**, a configurable bot admin role, or owner IDs
- Works across many servers; each GitHub feed is fetched once per cycle no matter how many servers use it
- Uses GitHub ETags, so unchanged feeds don't count against your API rate limit
- Never floods a channel: the first fetch of a feed only records a baseline, and later
  posts are capped per cycle
- SQLite storage, no external services needed

## Setup

### 1. Create the Discord application

1. Go to <https://discord.com/developers/applications>, click **New Application**, open **Bot**, and copy the token.
2. No privileged intents are needed.
3. Invite the bot. Replace `CLIENT_ID` with the application ID:

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=150528
   ```

   That grants View Channels, Send Messages, Embed Links and Mention All Roles.
   Mention All Roles is only needed to ping roles that aren't set as mentionable.

### 2. Create a GitHub account and token for the bot (recommended)

Without a token GitHub allows only 60 requests/hour, and issues can't be created.

Issues created from Discord show the token's owner as their author. So **create a separate
GitHub account for the bot** (for example `myorg-bot`) instead of using your personal one.
Every issue also gets a footer naming the Discord user who reported it.

1. Sign up for the bot account and give it access to your repos. For private repos,
   add it as a collaborator. **Triage** role or higher lets it apply labels too.
2. Signed in as the bot account, create a
   [fine-grained token](https://github.com/settings/personal-access-tokens) for the repos
   you track, with these repository permissions:
   - **Metadata**: read
   - **Contents**: read
   - **Pull requests**: read
   - **Issues**: read **and write** (read-only is enough if you don't want issue creation)

   Repos owned by an organization: set the token's *Resource owner* to the organization.
   The organization may need to approve fine-grained tokens first.

### 3. Configure and run

```bash
cp .env.example .env      # fill in DISCORD_TOKEN and GITHUB_TOKEN
pip install -r requirements.txt
python -m bot
```

Or with Docker:

```bash
cp .env.example .env
docker compose up -d --build
```

Global slash commands can take a few minutes to appear the first time. While testing,
set `DEV_GUILD_ID` to your server's ID so they sync instantly.

| Variable        | Default           | Description                                         |
|-----------------|-------------------|-----------------------------------------------------|
| `DISCORD_TOKEN` | *(required)*      | Bot token                                           |
| `GITHUB_TOKEN`  | *(none)*          | GitHub token for higher rate limits and private repos |
| `POLL_INTERVAL` | `300`             | Seconds between checks (minimum 60)                 |
| `DATABASE_PATH` | `data/tracker.db` | SQLite file location                                |
| `BOT_OWNER_IDS` | *(none)*          | Comma-separated user IDs with admin rights everywhere |
| `DEV_GUILD_ID`  | *(none)*          | Sync commands instantly to this server              |
| `ISSUE_COOLDOWN`| `60`              | Seconds between issues per non-admin user (0 = off) |
| `LOG_LEVEL`     | `INFO`            | Python log level                                    |

## Commands

🔒 = admin only. Admins are members with **Manage Server**, members with the role set via
`/admin set-role`, and anyone listed in `BOT_OWNER_IDS`.

### Repositories — `/repo`
| Command | Description |
|---|---|
| 🔒 `/repo track <repo> [releases_channel]` | Track a repo (`owner/name` or URL). If you pass a channel, all its releases go there right away. |
| 🔒 `/repo untrack <repo>` | Stop tracking a repo and delete its routes (asks for confirmation). |
| `/repo list` | List tracked repos and how many active routes each has. |
| `/repo latest <repo> [include_prereleases]` | Show the latest release of any repo. |
| `/repo info <repo>` | Stars, forks, default branch, last push, and so on. |

### Routes — `/route`
A **route** sends one kind of event from one repo to one channel, with optional filters.
A repo can have any number of routes.

| Command | Description |
|---|---|
| 🔒 `/route add <repo> <channel> <event> [release_kind] [tag_pattern] [branch] [mention] [publish]` | Create a route. |
| 🔒 `/route list [repo]` | Show routes, including paused ones and their errors. |
| 🔒 `/route toggle <route>` | Pause or resume a route. |
| 🔒 `/route remove <route>` | Delete a route. |
| 🔒 `/route test <route>` | Post the most recent matching item, to check formatting and permissions. |

**Events:** `Releases`, `Commits` (per branch), `New issues`, `New pull requests`.

**Release filters**
- `release_kind`: `All releases` / `Stable releases only` / `Pre-releases only`
- `tag_pattern`: a glob like `v2.*`, or a regex prefixed with `re:`, e.g. `re:^v\d+\.\d+\.\d+$`

### Issues — `/issue` and the message menu
| Command | Description |
|---|---|
| `/issue create <repo>` | Opens a form (title, description, labels) and creates the issue on GitHub. |
| Right-click a message → **Apps → Create GitHub issue** | Turns a chat message into an issue. The form is pre-filled with the message, and the issue links back to it and lists its attachments. If you can file in several repos, you pick one first. |

Who can use these is set per server, and can be overridden per repo:

| Access level | Who can create issues |
|---|---|
| **Bot admins only** *(default)* | Admins (Manage Server, the bot admin role, or bot owners) |
| **Bot admins + the issue role** | Admins and members with the role set via `/admin issue-role` |
| **Everyone** | Any server member |
| **Nobody (disabled)** | No one, admins included |

Safeguards:
- Autocomplete and the message menu only offer repos the user is allowed to file in.
- Non-admins have a cooldown between issues (`ISSUE_COOLDOWN`).
- `@mentions` in text written by non-admins are defused, so Discord users can't ping
  GitHub users or teams through the bot.
- Every issue ends with "Reported from Discord by **name** (`username`) in **server** #channel".

GitHub only applies labels if the bot's account has Triage access or higher. If any labels
weren't applied, the confirmation message says which ones.

### Administration — `/admin`
| Command | Description |
|---|---|
| 🔒 `/admin set-role <role>` | Let members with this role use admin commands. |
| 🔒 `/admin clear-role` | Remove that role. |
| 🔒 `/admin issue-access <level> [repo]` | Set who can create issues: the server default, or one repo's override (`Use server default` clears an override). |
| 🔒 `/admin issue-role [role]` | Set the role used by the **admins + issue role** level. Leave it empty to clear. |
| 🔒 `/admin status` | Poll timing, GitHub rate limit, issue settings, and any failing routes or feeds. |
| 🔒 `/admin check-now` | Poll GitHub immediately. |

## Example: routing releases

```
/repo track  repo:my-org/app
/route add   repo:my-org/app  channel:#announcements  event:Releases  release_kind:Stable releases only  mention:@Updates  publish:True
/route add   repo:my-org/app  channel:#beta-testers   event:Releases  release_kind:Pre-releases only
/route add   repo:my-org/app  channel:#legacy         event:Releases  tag_pattern:v1.*
/route add   repo:my-org/app  channel:#dev-feed       event:Commits   branch:develop
/route add   repo:my-org/app  channel:#triage         event:New issues
```

## Example: letting testers report bugs

```
/admin issue-role    role:@Testers
/admin issue-access  level:Bot admins + the issue role
/admin issue-access  level:Everyone  repo:my-org/public-app    # anyone can report here
/admin issue-access  level:Nobody (disabled)  repo:my-org/internal
```

## How it works

Every `POLL_INTERVAL` seconds the bot collects every feed that has at least one enabled
route, such as `my-org/app` releases or `my-org/app` commits on `develop`, and fetches each
once. Conditional requests (ETags) mean an unchanged feed costs nothing against the rate limit.
New items are compared with the IDs already seen, and each route posts the ones that pass
its filters, oldest first.

- **New routes start from "now".** When a route is created, the feed's current state is
  recorded, so old releases are never re-announced.
- **Paused or removed routes** release their feed. When the route is resumed, the bot takes
  a fresh baseline instead of replaying everything that happened in between.
- If the bot loses access to a channel, or the channel is deleted, the route is paused and
  the error appears in `/route list` and `/admin status`.
- If the bot is removed from a server, that server's configuration is deleted.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Layout:

```
bot/
  main.py      bot class, message delivery, error handling
  poller.py    polling loop, baselines, routing new items to routes
  github.py    GitHub REST client (ETags, rate limits)
  db.py        SQLite storage
  models.py    Repo/Route models, filters, repo parsing
  embeds.py    Discord embed formatting
  checks.py    admin permission check
  cogs/        slash commands: repos.py, routes.py, issues.py, admin.py
tests/
```
