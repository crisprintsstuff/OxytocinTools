from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import discord
import pytest

from bot.checks import UserError, can_create_issue
from bot.cogs.issues import Issues, _title_from, format_issue_body, neutralize_mentions
from bot.config import Config
from bot.db import Database
from bot.github import GitHubError


def member(*, manage=False, roles=(), user_id=5):
    m = MagicMock(spec=discord.Member)
    m.id = user_id
    m.name, m.display_name = "alice", "Alice"
    m.guild_permissions = discord.Permissions(manage_guild=manage)
    m.roles = [SimpleNamespace(id=r) for r in roles]
    return m


def interaction(db, user, *, cooldown=60, github=None):
    client = SimpleNamespace(db=db, config=Config(discord_token="x", issue_cooldown=cooldown),
                             github=github)
    inter = MagicMock()
    inter.client, inter.user, inter.guild_id = client, user, 1
    inter.guild = SimpleNamespace(id=1, name="My Server")
    inter.channel = SimpleNamespace(name="bugs")
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


async def test_migrates_old_database(tmp_path):
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:  # schema from the first version
        await conn.executescript(
            "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, admin_role_id INTEGER);"
            "INSERT INTO guild_settings VALUES (1, 42);"
            "CREATE TABLE repos (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,"
            " owner TEXT NOT NULL, name TEXT NOT NULL, default_branch TEXT NOT NULL DEFAULT 'main',"
            " added_by INTEGER, added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO repos (guild_id, owner, name) VALUES (1, 'o', 'r');")
    db = await Database.open(path)
    assert await db.get_admin_role(1) == 42
    assert await db.get_issue_settings(1) == ("admins", None)
    assert (await db.get_repo(1, "o", "r")).issue_access is None
    await db.close()


async def test_access_levels(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    admin, regular, tester = member(manage=True), member(), member(roles=[7])

    async def ok(user):
        return (await can_create_issue(interaction(db, user), await db.get_repo(1, "o", "r")))[0]

    assert await ok(admin) and not await ok(regular)          # default: admins only
    await db.set_issue_access(1, "role")
    assert not await ok(tester)                               # no role configured yet
    await db.set_issue_role(1, 7)
    assert await ok(tester) and not await ok(regular)
    await db.set_repo_issue_access(repo.id, "everyone")       # per-repo override
    assert await ok(regular)
    await db.set_repo_issue_access(repo.id, "disabled")
    assert not await ok(admin)                                # disabled means nobody
    await db.set_repo_issue_access(repo.id, None)             # back to server default
    assert await ok(tester)


def test_neutralize_mentions():
    assert neutralize_mentions("cc @octocat and @team/devs") == "cc @​octocat and @​team/devs"
    assert neutralize_mentions("mail me@example.com") == "mail me@example.com"


def test_issue_body_and_title():
    body = format_issue_body(
        "It crashes", reporter="@Alice", reporter_username="alice", guild="Srv", channel="bugs",
        message_url="https://discord.com/channels/1/2/3", message_author="Bob",
        attachments=[("log.txt", "https://cdn/log.txt")])
    assert body.startswith("It crashes")
    assert "[log.txt](https://cdn/log.txt)" in body
    assert "**@​Alice** (`alice`) in **Srv** #bugs" in body
    assert "[Original message](https://discord.com/channels/1/2/3) by **Bob**" in body
    assert format_issue_body("  ", reporter="a", reporter_username="a", guild="g",
                             channel=None).startswith("_No description provided._")
    assert _title_from("\n## Crash on start\nmore") == "Crash on start"


async def test_submit_creates_issue_and_enforces_cooldown(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    await db.set_issue_access(1, "everyone")
    github = SimpleNamespace(create_issue=AsyncMock(return_value={
        "number": 12, "title": "Bug", "html_url": "https://github.com/o/r/issues/12",
        "labels": [{"name": "bug"}]}))
    inter = interaction(db, member(), github=github)
    cog = Issues.__new__(Issues)  # skip GroupCog init; only submit() is exercised
    cog.bot, cog._last_issue = inter.client, {}

    await cog.submit(inter, repo, " Bug ", "ping @octocat", "bug, nope", None)
    owner, name, title, body, labels = github.create_issue.call_args.args
    assert (owner, name, title, labels) == ("o", "r", "Bug", ["bug", "nope"])
    assert "@​octocat" in body and "Reported from Discord by **Alice**" in body
    embed = inter.followup.send.call_args.kwargs["embed"]
    assert embed.url.endswith("/12") and "nope" in embed.footer.text

    with pytest.raises(UserError, match="Slow down"):
        await cog.submit(interaction(db, member(), github=github), repo, "Again", "", "", None)
    # Admins are exempt from the cooldown.
    admin = member(manage=True)
    await cog.submit(interaction(db, admin, github=github), repo, "Again", "", "", None)
    await cog.submit(interaction(db, admin, github=github), repo, "Again", "", "", None)


async def test_submit_permission_errors(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    github = SimpleNamespace(create_issue=AsyncMock(side_effect=GitHubError("no", 403)),
                             auth_mode="app", install_url="https://github.com/apps/x/installations/new")
    cog = Issues.__new__(Issues)
    cog.bot, cog._last_issue = interaction(db, member(), github=github).client, {}
    with pytest.raises(UserError, match="Only bot admins"):
        await cog.submit(interaction(db, member(), github=github), repo, "T", "", "", None)
    with pytest.raises(UserError, match=r"Issues: Read and write.*install it here"):
        await cog.submit(interaction(db, member(manage=True), github=github), repo, "T", "", "", None)


async def test_cooldown_allows_first_issue_right_after_boot(db, monkeypatch):
    import bot.cogs.issues as issues

    monkeypatch.setattr(issues.time, "monotonic", lambda: 3.0)  # machine just booted
    cog = Issues.__new__(Issues)
    inter = interaction(db, member())
    cog.bot, cog._last_issue = inter.client, {}
    await cog.check_cooldown(inter)  # must not raise
    cog._last_issue[(1, 5)] = 3.0
    with pytest.raises(UserError, match="Slow down"):
        await cog.check_cooldown(inter)


def test_issue_form_matches_label():
    from bot.cogs.issues import IssueModal
    from bot.models import Repo

    repo = Repo(1, 1, "o", "r", "main")
    assert IssueModal(None, repo, labels="enhancement").title == "Feature request: o/r"
    assert IssueModal(None, repo, labels="bug").body.placeholder.startswith("What happened")
    assert IssueModal(None, repo).title == "New issue: o/r"
