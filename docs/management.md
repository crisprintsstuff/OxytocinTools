# Bot Management & Operations Guide

This guide covers installing, configuring, running, backing up, monitoring, and upgrading **OxytocinTools**.

- [Documentation Index](README.md) · [Repository README](../README.md)

---

## 1. Discord Application & Invite Permissions

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application, open the **Bot** tab, and copy its token (`DISCORD_TOKEN`).
2. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
   - **Required for:** role-mention watches (`/watch role`), forum channel watches (`/watch forum`), reply-to-sync comments, and `#123` chat auto-linking (`/admin autolink`).
   - If disabled, the bot still starts and runs polling, routes, dashboards, PR cards, digests, and showcases, logging a reminder.
3. Authorize the bot into your server (replace `CLIENT_ID` with your Application ID):

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=326686174272
   ```

   Permission integer `326686174272` grants:

   | Permission | Used by |
   |---|---|
   | View Channels & Read Message History | All channel posting, watches, and startup cleanup |
   | Send Messages & Embed Links | Routes, dashboards, PR cards, digests, showcases, notify panels, issue embeds |
   | Add Reactions | Watch acknowledgement reactions (`✅`, `💬`) |
   | Mention `@everyone`, `@here`, and All Roles | Role pings on routes, PR cards, digests, and issue closure notices |
   | Manage Roles | Self-assignable notification role panels (`/notify`); bot's highest role must sit above ping roles |
   | Manage Messages | Pinning `/dashboard`, `/showcase`, and `/notify` messages |
   | Create Public Threads, Send Messages in Threads, Manage Threads | PR card timeline threads (`/pr track`) and forum watch thread sync/closure (`/watch forum`) |

---

## 2. GitHub Authentication

OxytocinTools supports three authentication tiers (in priority order per repository):

### Option A: GitHub App *(recommended)*

Installing a GitHub App once covers all selected/future public and private repositories on that account, and opens Discord-filed issues under `your-app-name[bot]`.

Create the app at **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App** (leave **Webhook → Active** unchecked unless you run the optional webhook server):

| Permission | Level | Needed for |
|---|---|---|
| **Contents** | Read-only *(or **Read and write**)* | Reading repo markdown files (`/showcase file`); **Read & write** enables re-hosting Discord attachments on an orphan `discord-attachments` branch |
| **Issues** | **Read and write** *(or Read-only)* | Reading issues/PRs/comments; **Read & write** required to file, comment on, label, or close issues from Discord |
| **Pull requests** | Read-only | PR routes, PR cards, PR dashboards, review digests |
| **Checks** | Read-only | CI check-run summaries on PR cards & PR dashboards |
| **Commit statuses** | Read-only | Legacy commit status checks on PR cards & PR dashboards |
| **Actions** | Read-only | Workflow failure routes (`/route add ... event:workflows`) |
| **Metadata** | Read-only | Selected automatically by GitHub |

After creating the app:
1. Note the **App ID** → set `GITHUB_APP_ID` in `.env`.
2. Generate a private key `.pem` file and save it as `data/github-app.pem` (or pass inline via `GITHUB_APP_PRIVATE_KEY`).
3. Click **Install App** and select repositories.
4. Whenever you add permissions to the GitHub App later, approve the new permission prompt under **Settings → Applications → Installed GitHub Apps → Configure**.

### Option B: Personal Access Token (`GITHUB_TOKEN`)

Used as a fallback when no GitHub App installation covers a repository (for example, tracking public third-party repositories where your app isn't installed). Raises rate limit from 60 req/hr to 5,000 req/hr.

### Option C: Anonymous

Public repositories only, capped at **60 requests/hour**, no issue creation.

---

## 3. Environment Configuration (`.env`)

All settings are loaded from `.env` in the repository root (`bot/config.py`). Relative file paths (`DATABASE_PATH`, `GITHUB_APP_PRIVATE_KEY_PATH`, `CONFIG_BACKUP_PATH`) are anchored to the repository root so behavior stays identical regardless of working directory.

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Discord bot token |
| `GITHUB_APP_ID` | *(none)* | GitHub App numeric ID (recommended) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | *(none; `data/github-app.pem` in `.env.example`)* | Path to the GitHub App `.pem` private key file |
| `GITHUB_APP_PRIVATE_KEY` | *(none)* | Inline `.pem` key using `\n` for line breaks (alternative to file path) |
| `GITHUB_TOKEN` | *(none)* | Optional PAT fallback for repos without a GitHub App installation |
| `POLL_INTERVAL` | `300` | Seconds between poll cycles (enforced minimum: `60`) |
| `RELEASE_NOTES_WAIT` | `30` | Minutes to hold back an empty release waiting for notes (`0` = post immediately) |
| `DATABASE_PATH` | `data/tracker.db` | SQLite database file path (supports `:memory:` in tests) |
| `CONFIG_BACKUP_PATH` | `<db_dir>/config-backup.json` | JSON config mirror path; set `off` / `none` / `false` / `0` to disable |
| `DB_BACKUP_COUNT` | `7` | Rolling daily SQLite backup files kept in `data/backups/` (`0` to disable) |
| `MEDIA_REPO` | *(none)* | Default fallback `owner/name` repo to host Discord attachments (`discord-attachments` branch); per-server `/admin media-repo` overrides this |
| `WEBHOOK_PORT` | `0` | HTTP listen port for optional GitHub webhook listener (`0` = disabled) |
| `WEBHOOK_HOST` | `127.0.0.1` | Bind address for the optional webhook HTTP server |
| `WEBHOOK_PATH` | `/webhook` | URL path for incoming GitHub webhook POST requests |
| `WEBHOOK_SECRET` | *(none)* | Optional HMAC secret validating `X-Hub-Signature-256` webhook payloads |
| `BOT_OWNER_IDS` | *(none)* | Comma-separated Discord user IDs granted admin permissions in every server |
| `ISSUE_COOLDOWN` | `60` | Minimum seconds between filed issues per non-admin user (`0` = off; admins exempt) |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 4. Running the Bot

### Python Virtual Environment

```bash
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m bot
```

### Linux `systemd` Service

Create `/etc/systemd/system/oxytocintools.service`:

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

Common management commands:

```bash
# Enable on boot and start
sudo systemctl daemon-reload
sudo systemctl enable --now oxytocintools.service

# Check status and follow live logs
systemctl status oxytocintools.service
journalctl -u oxytocintools.service -f

# Restart after code or .env changes
sudo systemctl restart oxytocintools.service

# Stop cleanly (triggers SQLite WAL TRUNCATE checkpoint on shutdown)
sudo systemctl stop oxytocintools.service
```

### Docker Compose

```bash
cp .env.example .env   # place your key at data/github-app.pem
docker compose up -d --build
docker compose logs -f
```

---

## 5. Persistence, Backups & Recovery

OxytocinTools uses a layered persistence design in SQLite (`data/tracker.db`):

1. **SQLite WAL Mode:** Enabled on open alongside `busy_timeout`, passive WAL checkpoints after every poll cycle, and a `TRUNCATE` checkpoint on graceful shutdown.
2. **Rolling Binary Backups (`DB_BACKUP_COUNT`):** Uses SQLite's online backup API (`sqlite3.Connection.backup`) to write timestamped snapshots to `data/backups/` on startup and once every 24 hours, pruning older files beyond `DB_BACKUP_COUNT`.
3. **Atomic JSON Config Mirror (`CONFIG_BACKUP_PATH`):** Every configuration change (repos, routes, dashboards, watches, PR trackers, issue templates, digests, showcases, and server settings) atomically writes `data/config-backup.json` via a temp file + `os.replace`.
   - If the bot starts with an empty database (`has_any_config() == False`) and `config-backup.json` exists, it automatically imports the configuration at startup.
4. **Manual Server Export / Import:** Admins can run [`/admin export`](commands/admin.md#admin-export) to download a per-server JSON backup and [`/admin import`](commands/admin.md#admin-import) to restore or merge config idempotently (existing entries are skipped).
5. **Persistent HTTP Cache (`http_cache`):** Dashboard, PR card, and showcase GitHub ETags + payloads are persisted in SQLite and pruned automatically so restarts do not exhaust API rate limits.

> **Important:** Never overwrite or delete `data/tracker.db` while the bot process is running; stop the service first if restoring a binary `.db` backup.

---

## 6. Monitoring, Webhooks & Command Sync

- **Status Inspection:** Run [`/admin status`](commands/admin.md#admin-status) for counts of repos, routes, dashboards, watches, PR trackers, digests, and showcases, poll timing, per-credential GitHub rate limits, issue access rules, and any stored `last_error` values.
- **Staff Error Alerts:** Configure a private channel with [`/admin alerts`](commands/admin.md#admin-alerts). Delivery, GitHub, and permission errors post deduplicated alerts (6-hour cooldown per error key).
- **Webhooks (Optional):** Setting `WEBHOOK_PORT` (e.g. `8080`) and `WEBHOOK_SECRET` runs an HTTP listener that validates `X-Hub-Signature-256` and debounces an immediate poll cycle (`poller.trigger()`) on incoming GitHub webhook POSTs, while regular `POLL_INTERVAL` polling remains active as a safety net.
- **Slash Command Sync:** On startup, the bot computes a SHA-256 hash of registered global commands (`commands_hash` in SQLite) and only calls `tree.sync()` when the schema changed, avoiding Discord sync rate limits. Use [`/admin sync`](commands/admin.md#admin-sync) to force a manual sync. Leftover guild-specific commands from older installs are automatically cleared once per startup.

---

## 7. Upgrading Safely

```bash
cd /home/cris/oxylabs/OxytocinTools
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -v
sudo systemctl restart oxytocintools.service
```

---

## 8. Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| New slash commands don't appear in Discord | Discord client cache or command schema unchanged | Run `/admin sync`, or press `Ctrl+R` / restart your Discord client |
| `@role` mentions or forum watches do nothing | **Message Content Intent** disabled in Developer Portal | Enable **Message Content Intent**, then restart the bot |
| Forum watch says user needs role even though they have it | Member not cached when Discord returned plain `User` | Fixed in current `main` (`resolve_member` fetches the `Member` if uncached); pull & restart |
| PR cards say CI checks are blocked | GitHub App lacks **Checks** / **Commit statuses** (Read-only) or prompt wasn't accepted | Add permissions on GitHub App and accept changes under **Installed GitHub Apps → Configure** |
| Attachments show `"Discord links may expire"` in GitHub issues | GitHub App lacks **Contents: Read and write** on the repo or `MEDIA_REPO` | Grant **Contents: Read and write**, accept prompt, or configure `/admin media-repo` |
| Showcase or dashboard stops updating | Channel deleted, permissions removed, or invalid file/branch | Check `/showcase list`, `/dashboard list`, or `/admin status` for the exact `last_error` |
