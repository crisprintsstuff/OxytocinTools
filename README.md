# OxytocinTools — GitHub Repo Tracker for Discord

A Discord bot that watches your GitHub repositories and posts **releases, commits,
issues, and pull requests** to the channels you choose, and lets your community
**create GitHub issues straight from Discord**. Routing rules let you send
specific releases to specific channels: stable releases to `#announcements`,
pre-releases to `#beta-testers`, `v2.*` tags to `#v2`, and so on.

- Slash commands, with autocomplete for tracked repos and routes
- Per-route filters: stable vs. pre-release, tag pattern (glob or regex), and branch for commits
- Optional role ping per route, and auto-publish in announcement channels
- Live issue dashboards: one message per repo that stays up to date, with a Report button
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
   To let the bot pin dashboards, also give it **Manage Messages** in those channels
   (or use `permissions=158720` in the link to grant it everywhere).

### 2. Create a GitHub App (recommended)

A GitHub App is how the bot reads your repos and creates issues. You install it once on
your account and it covers all your repos, public and private, including ones you create
later. No per-repo invites are needed. Issues it creates show as opened by
**`your-app-name[bot]`**, with a footer naming the Discord user who reported them.

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**
   (<https://github.com/settings/apps/new>). For an organization, use the org's
   **Settings → Developer settings → GitHub Apps** instead.
2. Fill in:
   - **GitHub App name**: anything unique, e.g. `oxytocin-tracker`. Issues show as `oxytocin-tracker[bot]`.
   - **Homepage URL**: anything, e.g. this repo's URL.
   - **Webhook**: **uncheck "Active"**. The bot polls, so it doesn't need webhooks.
   - **Repository permissions**:
     | Permission | Access |
     |---|---|
     | Contents | Read-only (releases and commits) |
     | Issues | **Read and write** (Read-only if you don't want issue creation) |
     | Pull requests | Read-only |
     | Metadata | Read-only (selected automatically) |
   - **Where can this GitHub App be installed?** **Only on this account**.
3. Click **Create GitHub App**. On the next page:
   - Note the **App ID** near the top.
   - Scroll to **Private keys**, click **Generate a private key**, and save the downloaded
     `.pem` file as `data/github-app.pem` in the bot's folder. Keep it secret: it's ignored by git.
4. In the left sidebar click **Install App**, then **Install** next to your account. Choose
   **All repositories**, or pick specific ones.
5. Put the App ID in `.env` as `GITHUB_APP_ID`. `GITHUB_APP_PRIVATE_KEY_PATH` already defaults to
   `data/github-app.pem`.

Repos owned by other people or organizations, where your app isn't installed, can still be
tracked if they're public. The bot reads them anonymously, which is limited to 60 requests/hour,
or with `GITHUB_TOKEN` if you set one (5000/hour; a classic token with no scopes is enough).
You can't create issues in those repos.

If you later add a permission to the app, GitHub asks you to approve the change on the
installation (**Settings → Applications → Installed GitHub Apps → Configure**) before it takes effect.

<details>
<summary>Not using an app?</summary>

Set only `GITHUB_TOKEN`, using a classic token. Issues will be opened as the token's owner,
and the token only reaches repos that account owns or collaborates on. Fine-grained tokens
can't write to repos owned by another account.
</details>

### 3. Configure and run

```bash
cp .env.example .env      # fill in DISCORD_TOKEN and GITHUB_APP_ID
python3 -m venv .venv     # on Debian/Ubuntu, first: sudo apt install python3-venv
source .venv/bin/activate # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m bot
```

The virtual environment (`.venv`) keeps the bot's packages separate from your system's
Python. Without it, Debian and Ubuntu refuse `pip install` with an
`externally-managed-environment` error. Later, start the bot with
`source .venv/bin/activate` and then `python -m bot`, or with `.venv/bin/python -m bot`.

Or with Docker:

```bash
cp .env.example .env      # and put the key at data/github-app.pem
docker compose up -d --build
```

Slash commands are registered every time the bot starts. If new ones don't show up, restart
your Discord app (or press Ctrl+R) to refresh its command list. The bot also removes
commands left over from older versions, or from other code that used the same Discord
application.

| Variable        | Default           | Description                                         |
|-----------------|-------------------|-----------------------------------------------------|
| `DISCORD_TOKEN` | *(required)*      | Bot token                                           |
| `GITHUB_APP_ID` | *(none)*          | GitHub App ID (recommended)                         |
| `GITHUB_APP_PRIVATE_KEY_PATH` | `data/github-app.pem` | App private key file              |
| `GITHUB_APP_PRIVATE_KEY` | *(none)* | The key inline, with `\n` for line breaks, instead of a file |
| `GITHUB_TOKEN`  | *(none)*          | Optional personal token: fallback for repos without the app |
| `POLL_INTERVAL` | `300`             | Seconds between checks (minimum 60)                 |
| `DATABASE_PATH` | `data/tracker.db` | SQLite file location                                |
| `BOT_OWNER_IDS` | *(none)*          | Comma-separated user IDs with admin rights everywhere |
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

Issues can only be created in repos where the GitHub App is installed. If GitHub doesn't
apply some of the labels someone typed, the confirmation message says which ones.

### Issue dashboards — `/dashboard`
A dashboard is one message in a channel that the bot keeps editing, so it always shows a
repo's current issues. The channel doesn't fill up with new posts.

| Command | Description |
|---|---|
| 🔒 `/dashboard create <repo> <channel> [label] [pin]` | Post a dashboard. `label` shows only issues with that GitHub label (e.g. a "bug" dashboard). `pin` pins it. |
| 🔒 `/dashboard list` | Show dashboards, with links and any errors. |
| 🔒 `/dashboard remove <dashboard> [delete_message]` | Delete a dashboard and, by default, its message. |

Each dashboard shows:
- Open issue and pull request counts
- The 15 newest open issues, with labels and age
- Issues closed in the last 7 days
- When it last changed

Buttons:
- **🐛 Report issue**: opens the issue form for that repo, pre-filled with the dashboard's
  label. It follows the same access rules and cooldown as `/issue create`.
- **🔄 Refresh**: updates it now (anyone can press it, at most once every 15 seconds).
- **View on GitHub**: opens the repo's issues page.

Dashboards update on every GitHub check (`POLL_INTERVAL`), right after an issue is
created from Discord, and when someone presses Refresh. The message is only edited when
something actually changed, and unchanged GitHub data doesn't use up your rate limit.

- **If someone deletes a dashboard message**, the bot posts it again. To get rid of a
  dashboard for good, use `/dashboard remove`.
- **Buttons keep working** after the bot restarts.
- **Untracking the repo** or deleting the channel removes its dashboards.
- **Pinning** needs the **Manage Messages** (or **Pin Messages**) permission in that channel.
- **Counts** read the newest 100 open items; above that they show as `100+`.

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

## Example: a bug dashboard

```
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#oxybrush-issues  pin:True
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#bugs  label:bug
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
- **Authentication:** for each repo the bot uses the GitHub App's installation token if the
  app is installed on that repo. Otherwise it uses `GITHUB_TOKEN`, or anonymous access.
  Each has its own rate limit (installations get 5000+/hour), and `/admin status` shows them all.

## Development

```bash
source .venv/bin/activate
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
  dashboards.py  live issue dashboards: rendering, refreshing, buttons
  cogs/        slash commands: repos.py, routes.py, issues.py, dashboards.py, admin.py
tests/
```
