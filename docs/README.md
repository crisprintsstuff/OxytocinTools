# OxytocinTools Documentation

Welcome to the documentation for **OxytocinTools**, a Discord bot that tracks GitHub repositories, routes events into channels, turns chat/forum messages into GitHub issues, and maintains live dashboards, PR cards, digests, and feature showcases.

## Operations & Management

- **[Bot Management & Operations Guide](management.md)** — Discord & GitHub App setup, invite permissions, `.env` configuration reference, running via `.venv` / `systemd` / Docker, SQLite persistence & backups, webhooks, alert channels, upgrading, and troubleshooting.

## Command Reference

Commands marked with 🔒 require bot admin permissions (**Manage Server** / **Administrator**, the role set via [`/admin set-role`](commands/admin.md#admin-set-role), or a user ID in `BOT_OWNER_IDS`).

| Command Group | Summary | Guide |
|---|---|---|
| `/repo` | Track/untrack repositories, list tracked repos, inspect info & latest releases | [repo.md](commands/repo.md) |
| `/route` | Route releases, commits, issues, PRs, and workflows into channels with filters | [route.md](commands/route.md) |
| `/issue` & message menu | File issues via modal/context menu, view/search issues & PRs, manage templates | [issue.md](commands/issue.md) |
| `/watch` | Turn role mentions or forum posts into GitHub issues with two-way triage & sync | [watch.md](commands/watch.md) |
| `/pr` | Maintain live PR status cards with CI checks, reviews, and timeline threads | [pr.md](commands/pr.md) |
| `/dashboard` | Maintain edit-in-place issue and PR overview dashboards with Report & Refresh buttons | [dashboard.md](commands/dashboard.md) |
| `/digest` | Schedule periodic changelogs and stale pull request review nudges | [digest.md](commands/digest.md) |
| `/showcase` | Keep feature/changelog channels synced in-place from repo markdown files or release notes | [showcase.md](commands/showcase.md) |
| `/admin` | Roles, issue access policy, status diagnostics, config export/import, chat features, alerts | [admin.md](commands/admin.md) |

---

Back to [repository README](../README.md).
