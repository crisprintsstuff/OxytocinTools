# OxytocinTools — GitHub Repo Tracker for Discord

A Discord bot that watches your GitHub repositories and posts **releases, commits,
issues, and pull requests** to the channels you choose, and lets your community
**create GitHub issues straight from Discord**. Routing rules let you send
specific releases to specific channels: stable releases to `#announcements`,
pre-releases to `#beta-testers`, `v2.*` tags to `#v2`, and so on.

- Slash commands, with autocomplete for tracked repos and routes
- Per-route filters: stable vs. pre-release, tag pattern (glob or regex), and branch for commits
- Optional role ping per route, and auto-publish in announcement channels
- Live issue and pull request dashboards: one message per repo that stays up to date
- **Watches:** mention a role like `@bug-report`, or post in a forum channel, and the bot files the GitHub issue for you
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
2. On the same **Bot** page, under **Privileged Gateway Intents**, turn on **Message Content Intent**.
   Watches (`/watch`) need it to read the messages they turn into issues. Everything else
   works without it, and if it's off the bot starts anyway and logs a reminder.
3. Invite the bot. Replace `CLIENT_ID` with the application ID:

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=326417738816
   ```

   That grants View Channels, Send Messages, Embed Links, Add Reactions, Read Message History,
   Mention All Roles, Manage Messages (pinning dashboards), Create Public Threads (PR card
   threads), and Send Messages in Threads and Manage Threads (replying in and closing threads).
   Already invited the bot with fewer permissions? Open this link again and authorize it to
   update them.

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
     | Contents | Read-only (or **Read and write** to permanently re-host Discord attachments) |
     | Issues | **Read and write** (Read-only if you don't want issue creation) |
     | Pull requests | Read-only |
     | Checks | Read-only (CI results on PR cards) |
     | Commit statuses | Read-only (CI results on PR cards) |
     | Actions | Read-only (workflow failure alerts) |
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
| `RELEASE_NOTES_WAIT` | `30`         | Minutes to hold back a release with empty notes (0 = off) |
| `DATABASE_PATH` | `data/tracker.db` | SQLite file location                                |
| `CONFIG_BACKUP_PATH` | `data/config-backup.json` | Portable JSON mirror of server config (`off` to disable) |
| `DB_BACKUP_COUNT` | `7`             | Rolling SQLite backups to keep in `data/backups/` (`0` to disable) |
| `MEDIA_REPO`    | *(none)*          | Optional public `owner/name` repo to host Discord attachments |
| `WEBHOOK_PORT`  | `0`               | Optional HTTP port for GitHub webhooks (`0` = disabled) |
| `WEBHOOK_HOST`  | `127.0.0.1`       | Bind address for the webhook server                 |
| `WEBHOOK_PATH`  | `/webhook`        | URL path for GitHub webhook POSTs                   |
| `WEBHOOK_SECRET`| *(none)*          | HMAC secret (`X-Hub-Signature-256`) for webhooks    |
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
| 🔒 `/route add <repo> <channel> <event> [release_kind] [tag_pattern] [branch] [mention] [publish] [pr_events] [include_drafts] [include_bots]` | Create a route. |
| 🔒 `/route edit <route> [...]` | Update a route's channel, filters, mention, or publish setting without recreating it. |
| 🔒 `/route list [repo]` | Show routes, including paused ones and their errors. |
| 🔒 `/route toggle <route>` | Pause or resume a route. |
| 🔒 `/route remove <route>` | Delete a route. |
| 🔒 `/route test <route>` | Post the most recent matching item, to check formatting and permissions. |

**Events:** `Releases`, `Commits` (per branch), `New issues`, `Pull requests`, `Workflow failures`.

**Release filters**
- `release_kind`: `All releases` / `Stable releases only` / `Pre-releases only`
- `tag_pattern`: a glob like `v2.*`, or a regex prefixed with `re:`, e.g. `re:^v\d+\.\d+\.\d+$`

**Pull request options**
- `pr_events`: which changes to post, comma-separated. The default is `opened, ready`.

  | Event | Posted when |
  |---|---|
  | `opened` | A PR is opened |
  | `ready` | A draft PR is marked ready for review |
  | `merged` | A PR is merged |
  | `closed` | A PR is closed without merging |
  | `reopened` | A closed PR is reopened |

  `all` means all five. For example, `pr_events:merged` makes a "what just landed" channel.
- `branch`: only PRs into this branch, e.g. `main`. Globs like `release/*` work.
- `include_drafts`: also post about draft PRs. Off by default: a draft is posted once it's
  marked ready (with the `ready` event).
- `include_bots`: also post about PRs opened by bots such as Dependabot. Off by default.

The bot checks the PRs updated since its last look, so a PR that's opened and merged between
two checks gets both posts, and PRs merged before a route existed are never announced.

### Issues — `/issue` and the message menu
| Command | Description |
|---|---|
| `/issue create <repo> [label]` | Opens a form (title, description, labels) and creates the issue on GitHub (uses any matching custom template). |
| `/issue view <repo> <number>` | Show details and current status of an issue or pull request. |
| `/issue search <repo> <query> [state] [type]` | Search issues and pull requests in a repository. |
| 🔒 `/issue template-set <repo> [label]` | Configure custom form title, placeholder, and pre-filled body for a repo or label. |
| 🔒 `/issue template-list [repo]` | List configured issue form templates. |
| 🔒 `/issue template-remove <repo> [label]` | Remove an issue form template. |
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
- When filing via `/issue create` or a watch, the bot checks for similar open issues and lists any matches in its confirmation embed.

Issues can only be created in repos where the GitHub App is installed. If GitHub doesn't
apply some of the labels someone typed, the confirmation message says which ones.

### Watches — `/watch`
Watches turn Discord messages into GitHub issues without anyone running a command.
The reporter is credited, and the usual issue-access rules and cooldown apply.

| Command | Description |
|---|---|
| 🔒 `/watch role <role> <repo> [label] [channel]` | Messages that mention `role` become issues in `repo`, with `label`. Optionally only in one channel (and its threads). |
| 🔒 `/watch forum <forum> <repo> [label] [close_posts]` | Every new post in a forum channel becomes an issue. |
| 🔒 `/watch edit <watch> [...]` | Update a watch's role, channel, label, or `close_posts` setting. |
| 🔒 `/watch list` / `/watch remove <watch>` | Show or stop watches. |

**Role mentions**
- *"@bug-report Brush stutters on 4K screens…"* becomes an issue. The first line (without the
  mention) is the title, the whole message is the description, and attachments and a link back
  to the message are included.
- **Pictures, videos, and files** attached to the message or reply are included. Images are
  rendered inline directly in the GitHub issue. When the GitHub App has *Contents: Read and write*
  permission, attachments are automatically re-hosted on an orphan `discord-attachments` branch
  (or a dedicated repo set via `/admin media-repo` / `MEDIA_REPO`) so they never expire.
- **Reply** to someone's message with the mention to file *their* message: they're the
  reporter, you're credited as the person who filed it, attachments from both messages are
  included, and anything else you wrote is added as a note.
- The bot replies with a link to the issue, an **Open on GitHub** button and a **Not an issue**
  button, plus admin-only triage buttons (**Complete**, **Labels**, **Comment**), and reacts ✅.
  Pressing **Not an issue** (the reporter, the person who filed it, or an admin) closes the issue
  on GitHub as *not planned* and leaves a comment saying who did it.
- **Two-way updates:** When the issue closes on GitHub, the bot updates its reply embed and pings
  the reporter. Replying to the bot's `"Filed as #..."` message adds a comment on GitHub (reacts 💬).
- **Auto-preview `#123`:** Mentioning `#123` or `repo#123` in chat posts a compact preview embed for
  tracked repos (toggleable via `/admin autolink`).
- A bare mention with no text or attachments is ignored, and editing a message never files it again.
- If someone isn't allowed to file in that repo (see `/admin issue-access`) or is on cooldown,
  the bot replies with the reason and deletes the reply after 30 seconds.

**Tip: use a role nobody has.** A role must be mentionable for regular members to trigger it,
and mentioning a role pings everyone who has it. Create roles like `@bug-report` and
`@feature-request`, turn on **Allow anyone to @mention this role**, and don't give them to anyone:
they work as triggers and notify nobody.

**Forum channels**
- The post title becomes the issue title, and the post's first message becomes the description.
- Post **tags** that match one of the repo's GitHub labels (ignoring case) are added as labels, so
  forum tags *Bug* and *Enhancement* map to the `bug` and `enhancement` labels. Tags without a
  matching label are skipped.
- The bot replies in the post with the issue link and the **Not an issue** button.
- **Two-way comment sync:** Follow-up messages in the post are posted as GitHub comments (reacts 💬),
  and new GitHub comments are mirrored back into the post (toggleable via `/admin comment-sync`).
- With `close_posts` on (the default), when the issue is closed on GitHub the bot posts a note
  in the thread, then closes and locks it. If the issue was *completed* and the forum has a tag
  named **Solved** (or Resolved, Fixed, Done), that tag is added too. This needs **Manage Threads**.
- Forum posts skip the per-user cooldown; use the forum's slowmode if you need one.

### PR cards — `/pr`
One live card per open pull request, kept up to date, with a thread logging every change.

| Command | Description |
|---|---|
| 🔒 `/pr track <repo> <channel> [branch] [include_drafts] [include_bots] [threads] [mention] [cleanup_days]` | Post a card for each open PR in `channel`, and one for each new PR as it opens. |
| 🔒 `/pr edit <tracker> [...]` | Update a PR card tracker's channel, branch filter, thread mode, cleanup days, or mention role. |
| 🔒 `/pr list` | Show trackers, how many open PRs each has, and any errors. |
| 🔒 `/pr stop <tracker>` | Stop updating the cards. The cards stay where they are. |

Each card has **Open PR**, **Checks**, **Files**, and **Refresh** (for open PRs) buttons, and shows:
- **State:** 🟢 open, ⚪ draft, 🟣 merged, 🔴 closed (with the matching color)
- The author, branches, and when it was opened, merged or closed
- **Checks:** passed, failed (with the failing checks' names) and still running
- **Reviews:** approvals, changes requested, and who it's waiting on
- **Merge:** ready to merge, has conflicts, behind the base branch, or blocked by branch rules
- Lines added and removed, files changed and commits, plus **Open PR**, **Checks** and **Files** buttons

With `threads` on (the default), the bot opens a thread under each card and posts a line for each
change: ready for review, new commits pushed, checks failed or all passed, approved, changes
requested, review requested, merge conflicts found or resolved, merged, closed or reopened.
When a PR is merged or closed, its card is updated one last time and its thread is archived.

- Filters work like PR routes: `branch` (glob), and `include_drafts` / `include_bots` (off by
  default; a draft gets its card once it's marked ready).
- Cards update on every GitHub check (`POLL_INTERVAL`), and are only edited when something changed.
  Unchanged GitHub data doesn't count against the rate limit (ETags).
- If someone deletes a card, it's posted again on the next change (without pinging again).
- CI results need the GitHub App's **Checks** and **Commit statuses** permissions (read-only).
  Without them, the card says so and shows everything else. If you add them later, approve the
  change under **Settings → Applications → Installed GitHub Apps → Configure**.

### Dashboards — `/dashboard`
A dashboard is one message in a channel that the bot keeps editing, so it always shows a
repo's current issues or pull requests. The channel doesn't fill up with new posts.

| Command | Description |
|---|---|
| 🔒 `/dashboard create <repo> <channel> [label] [pin] [type]` | Post a dashboard. `type` is **Issues** (the default) or **Pull requests**. `label` shows only issues or PRs with that GitHub label (e.g. a "bug" dashboard). `pin` pins it. |
| 🔒 `/dashboard list` | Show dashboards, with links and any errors. |
| 🔒 `/dashboard remove <dashboard> [delete_message]` | Delete a dashboard and, by default, its message. |

**Issue dashboards** show:
- Open issue and pull request counts
- The 15 newest open issues, with labels and age
- Issues closed in the last 7 days
- When it last changed

Buttons:
- **Report button**: opens the issue form for that repo, with the dashboard's label already
  filled in. It follows the same access rules and cooldown as `/issue create`. The button
  and form wording match the label:

  | Dashboard label contains | Button | Form title |
  |---|---|---|
  | `bug` | 🐛 Report a bug | Bug report |
  | `enhancement` or `feature` | 💡 Suggest a feature | Feature request |
  | `doc` (e.g. `documentation`) | 📚 Suggest a docs change | Docs issue |
  | `question` | ❓ Ask a question | Question |
  | anything else, or no label | 📝 Open an issue | New issue |
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

**Pull request dashboards** (`type:Pull requests`) show:
- Counts: open, drafts, ready to merge, and needing attention (failing checks, conflicts or
  changes requested)
- The 15 newest open PRs (drafts last), each with status icons: ✅ checks pass · ❌ failing ·
  ⏳ running · 👍 approved · 📝 changes requested · 👀 needs review · ⚠️ conflicts · ⚪ draft
- **Waiting longest for review:** open PRs with no approval or requested changes yet, oldest first
- PRs merged in the last 7 days

They have **Refresh** and **View on GitHub** buttons (no Report button). They share GitHub
data with PR cards (`/pr track`), so using both costs hardly any extra requests. CI icons need
the GitHub App's **Checks** and **Commit statuses** permissions.

### Digests — `/digest`
| Command | Description |
|---|---|
| 🔒 `/digest add <repo> <channel> <type> [interval_days] [stale_days] [mention]` | Schedule a periodic **Changelog** (merged PRs & closed issues) or **Review nudges** (PRs waiting `>stale_days` for review) summary. |
| 🔒 `/digest list` | List scheduled digests and any errors. |
| 🔒 `/digest remove <digest>` | Remove a scheduled digest. |
| 🔒 `/digest run <digest>` | Post a digest immediately. |

### Showcases — `/showcase`
Edit-in-place channel boards that keep a dedicated channel (e.g. `#features` or `#whats-new`) up to date from either a repository markdown file or recent GitHub release notes. Long content automatically splits across multiple messages (up to 5) with **🔄 Refresh** and **View on GitHub** buttons at the bottom.

| Command | Description |
|---|---|
| 🔒 `/showcase file <repo> <channel> [path] [section] [branch] [title] [pin]` | Sync from a repo markdown file (default `README.md`), optionally filtering to comma-separated heading(s) like `Features, What's New`. |
| 🔒 `/showcase releases <repo> <channel> [section] [kind] [count] [title] [pin]` | Sync from recent GitHub releases (`stable`, `all`, or `prerelease`), optionally extracting specific heading(s) like `New Features, Features, Added`. |
| 🔒 `/showcase edit <showcase> [...]` | Edit path, branch, section filter, release kind/count, title, or pin setting and immediately re-render. |
| 🔒 `/showcase list` | List showcase boards, links, and any errors. |
| 🔒 `/showcase refresh <showcase>` | Force an immediate refresh. |
| 🔒 `/showcase remove <showcase> [delete_message]` | Delete a showcase board and, by default, its message(s). |

### Administration — `/admin`
| Command | Description |
|---|---|
| 🔒 `/admin set-role <role>` | Let members with this role use admin commands. |
| 🔒 `/admin clear-role` | Remove that role. |
| 🔒 `/admin issue-access <level> [repo]` | Set who can create issues: the server default, or one repo's override (`Use server default` clears an override). |
| 🔒 `/admin issue-role [role]` | Set the role used by the **admins + issue role** level. Leave it empty to clear. |
| 🔒 `/admin status` | Poll timing, GitHub rate limit, issue settings, and any failing routes or feeds. |
| 🔒 `/admin check-now` | Poll GitHub immediately. |
| 🔒 `/admin export` | Download this server's configuration as a JSON file. |
| 🔒 `/admin import <file>` | Restore server configuration from a JSON export (existing items are kept). |
| 🔒 `/admin sync` | Force-sync global slash commands with Discord. |
| 🔒 `/admin autolink <enabled>` | Toggle automatic `#123` issue/PR preview embeds in chat. |
| 🔒 `/admin comment-sync <enabled>` | Toggle two-way comment syncing for filed issues. |
| 🔒 `/admin media-repo [repo]` | Set or clear a dedicated repository for hosting Discord attachments. |
| 🔒 `/admin alerts [channel]` | Set a private staff channel for deduplicated error alerts (empty to disable). |

## Example: routing releases

```
/repo track  repo:my-org/app
/route add   repo:my-org/app  channel:#announcements  event:Releases  release_kind:Stable releases only  mention:@Updates  publish:True
/route add   repo:my-org/app  channel:#beta-testers   event:Releases  release_kind:Pre-releases only
/route add   repo:my-org/app  channel:#legacy         event:Releases  tag_pattern:v1.*
/route add   repo:my-org/app  channel:#dev-feed       event:Commits   branch:develop
/route add   repo:my-org/app  channel:#triage         event:New issues
/route add   repo:my-org/app  channel:#reviews        event:Pull requests  branch:main
/route add   repo:my-org/app  channel:#changelog      event:Pull requests  pr_events:merged  branch:main
```

## Example: letting testers report bugs

```
/admin issue-role    role:@Testers
/admin issue-access  level:Bot admins + the issue role
/admin issue-access  level:Everyone  repo:my-org/public-app    # anyone can report here
/admin issue-access  level:Nobody (disabled)  repo:my-org/internal
```

## Example: following pull requests

```
/pr track          repo:crisprintsstuff/OxyBrush  channel:#pull-requests  branch:main  mention:@reviewers
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#pull-requests  type:Pull requests  pin:True
/route add         repo:crisprintsstuff/OxyBrush  channel:#changelog  event:Pull requests  pr_events:merged
```

## Example: a bug dashboard

```
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#oxybrush-issues  pin:True
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#bugs  label:bug
/dashboard create  repo:crisprintsstuff/OxyBrush  channel:#ideas  label:enhancement
```

## Example: reporting without commands

```
/watch role   role:@bug-report       repo:crisprintsstuff/OxyBrush  label:bug
/watch role   role:@feature-request  repo:crisprintsstuff/OxyBrush  label:enhancement
/watch forum  forum:#oxybrush-reports  repo:crisprintsstuff/OxyBrush
/admin issue-access  level:Everyone  repo:crisprintsstuff/OxyBrush
```

## How it works

Every `POLL_INTERVAL` seconds the bot collects every feed that has at least one enabled
route, such as `my-org/app` releases or `my-org/app` commits on `develop`, and fetches each
once. Conditional requests (ETags) mean an unchanged feed costs nothing against the rate limit.
New items are compared with the IDs already seen, and each route posts the ones that pass
its filters, oldest first.

- **Releases wait for their notes.** If a release is published with empty notes, the bot
  holds it back and posts it as soon as the notes are filled in. If none are added within
  `RELEASE_NOTES_WAIT` minutes (30 by default), it posts the release anyway. Tip: on
  GitHub you can also **Save draft**, write the notes, then publish. Drafts are never posted.
- **New routes start from "now".** When a route is created, the feed's current state is
  recorded, so old releases are never re-announced.
- **Paused or removed routes** release their feed. When the route is resumed, the bot takes
  a fresh baseline instead of replaying everything that happened in between.
- If the bot loses access to a channel, or the channel is deleted, the route is paused and
  the error appears in `/route list` and `/admin status`.
- If the bot is removed from a server, its configuration is kept in the database (polling and updates are skipped until it's re-invited).
- **Authentication:** for each repo the bot uses the GitHub App's installation token if the
  app is installed on that repo. Otherwise it uses `GITHUB_TOKEN`, or anonymous access.
  Each has its own rate limit (installations get 5000+/hour), and `/admin status` shows them all.

## Persistence, backups & running as a service

- **Path anchoring:** Relative `DATABASE_PATH`, `GITHUB_APP_PRIVATE_KEY_PATH`, and `CONFIG_BACKUP_PATH` values resolve against the repository root regardless of the working directory you launch the bot from.
- **SQLite durability & rolling backups:** The database runs in WAL mode with `busy_timeout`, checkpoints passively after each poll cycle, truncates on clean shutdown, and writes rolling online backups to `data/backups/` on startup and once every 24 hours (`DB_BACKUP_COUNT`, default `7`).
- **Config mirror & auto-restore:** Every configuration change is atomically mirrored to `data/config-backup.json` (`CONFIG_BACKUP_PATH`). If the bot starts with an empty database and that file exists, it automatically restores your servers, repos, routes, dashboards, watches, and PR trackers. You can also use `/admin export` and `/admin import` at any time.
- **Slash command sync:** Global slash commands are only synced with Discord when the command schema actually changes (tracked via `commands_hash` in SQLite), or on demand via `/admin sync`.
- **Persistent ETag cache & optional webhooks:** Dashboard and PR card GitHub ETags are cached in SQLite (`http_cache`) so restarts don't trigger a fresh burst of API usage. Set `WEBHOOK_PORT` (and `WEBHOOK_SECRET`) to accept GitHub webhooks for near-instant poll triggers alongside the regular polling fallback.

<details>
<summary>Example systemd service (Linux)</summary>

```ini
[Unit]
Description=OxytocinTools Discord GitHub Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cris
WorkingDirectory=/home/cris/oxylabs/OxytocinTools
ExecStart=/home/cris/oxylabs/OxytocinTools/.venv/bin/python -m bot
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

</details>

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
  dashboards.py  live issue and PR dashboards: rendering, refreshing, buttons
  watches.py   role-mention and forum watches that file issues, and closing forum posts
  prcards.py   live PR cards: check and review summaries, the timeline, keeping cards updated
  cogs/        slash commands: repos.py, routes.py, issues.py, dashboards.py, watches.py, prs.py, admin.py
tests/
```
