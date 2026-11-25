from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot.cogs.issues import Issues
from bot.config import Config
from bot.embeds import PURPLE
from bot.watches import SYNC_MARKER, WatchManager, extract_issue_refs

BUG = SimpleNamespace(id=900, name="bug-report")


def _member(user_id, name, *, admin=False):
    m = MagicMock(spec=discord.Member)
    m.id, m.name, m.display_name, m.bot = user_id, name.lower(), name, False
    m.guild_permissions = discord.Permissions(manage_guild=admin)
    m.roles = []
    m.mention = f"<@{user_id}>"
    return m


def _message(msg_id, author, text, *, roles=(), channel=None, channel_id=50, reference=None):
    m = MagicMock(spec=discord.Message)
    m.id, m.author, m.clean_content, m.webhook_id = msg_id, author, text, None
    m.guild = SimpleNamespace(id=1, name="Srv")
    m.channel = channel or SimpleNamespace(
        id=channel_id, name="general", parent_id=None, send=AsyncMock()
    )
    m.role_mentions = list(roles)
    m.reference = reference
    m.attachments = []
    m.jump_url = f"https://discord.com/channels/1/{m.channel.id}/{msg_id}"
    m.reply = AsyncMock(return_value=SimpleNamespace(id=msg_id + 10_000))
    m.add_reaction = AsyncMock()
    return m


async def _setup(db, *, app_slug="oxytocin-bot"):
    repo = await db.add_repo(1, "crisprintsstuff", "OxyBrush", "main", None)
    await db.set_issue_access(1, "everyone")
    await db.add_watch(repo, "role", role_id=BUG.id, label="bug")
    numbers = iter(range(10, 50))

    async def create_issue(owner, name, title, body, labels):
        n = next(numbers)
        return {"number": n, "title": title, "html_url": f"https://github.com/{owner}/{name}/issues/{n}"}

    github = SimpleNamespace(
        create_issue=AsyncMock(side_effect=create_issue),
        comment_issue=AsyncMock(),
        get_issue=AsyncMock(),
        list_issue_comments=AsyncMock(return_value=([], '"c0"')),
        app_slug=app_slug,
        auth_mode="app",
        install_url=None,
    )
    bot = SimpleNamespace(
        db=db,
        github=github,
        config=Config(discord_token="x", issue_cooldown=60),
        get_channel=lambda _cid: None,
    )
    issues = Issues.__new__(Issues)
    issues.bot, issues._last_issue = bot, {}
    bot.get_cog = lambda name: issues if name == "Issues" else None
    bot.dashboards = None
    return WatchManager(bot), bot, repo


def test_extract_issue_refs_ignores_code_urls_and_tags():
    text = (
        "Check #12 and OxyBrush#34 and org/other#56.\n"
        "Ignore `#99`, ```python\n#88\n```, https://github.com/o/r/issues/77#issuecomment-1, "
        "<#123456> <@!654321>, and #12 duplicate."
    )
    assert extract_issue_refs(text) == [(None, 12), ("OxyBrush", 34), ("org/other", 56)]


async def test_role_watch_close_notification_edits_embed_and_pings_reporter(db):
    manager, bot, repo = await _setup(db)
    alice = _member(5, "Alice")
    msg = _message(1, alice, "@bug-report Brush lags", roles=[BUG])
    await manager.on_message(msg)
    filing = await db.find_filing(1, repo.id)
    assert filing is not None and filing.thread_id is None

    reply_embed = discord.Embed(title="📝 Filed", description="Brush lags")
    reply_msg = SimpleNamespace(
        id=filing.reply_message_id,
        embeds=[reply_embed],
        edit=AsyncMock(),
        reply=AsyncMock(),
    )
    channel = SimpleNamespace(
        id=50,
        fetch_message=AsyncMock(return_value=reply_msg),
        send=AsyncMock(),
    )
    bot.get_channel = lambda cid: channel if cid == 50 else None
    bot.github.get_issue = AsyncMock(return_value=(
        {"state": "closed", "state_reason": "completed"}, '"e1"'
    ))

    await manager.sync_threads()
    reply_msg.edit.assert_awaited_once()
    edited = reply_msg.edit.call_args.kwargs
    assert edited["embed"].color == PURPLE
    assert "✅ Closed as completed" in edited["embed"].description
    assert [c.label for c in edited["view"].children] == ["Open on GitHub"]
    reply_msg.reply.assert_awaited_once()
    assert "<@5>" in reply_msg.reply.call_args.args[0]
    assert "closed as completed" in reply_msg.reply.call_args.args[0]
    assert (await db.get_filing(filing.id)).state == "closed"

    # Second sync does not notify again
    await manager.sync_threads()
    assert reply_msg.reply.await_count == 1


async def test_discord_reply_and_forum_followup_sync_to_github(db):
    manager, bot, repo = await _setup(db)
    alice = _member(5, "Alice")
    bob = _member(6, "Bob")
    trigger = _message(1, alice, "@bug-report Crash", roles=[BUG])
    await manager.on_message(trigger)
    filing = await db.find_filing(1, repo.id)

    # Reply to the bot's "Filed as #..." message
    followup = _message(
        2, bob, "Happens on Wayland too",
        reference=SimpleNamespace(message_id=filing.reply_message_id, channel_id=50),
    )
    await manager.on_message(followup)
    bot.github.comment_issue.assert_awaited_once()
    args = bot.github.comment_issue.call_args.args
    assert args[:3] == ("crisprintsstuff", "OxyBrush", 10)
    assert "Happens on Wayland too" in args[3]
    assert SYNC_MARKER in args[3]
    followup.add_reaction.assert_awaited_with("💬")

    # Follow-up in a watched forum thread
    class FakeThread(discord.Thread):
        def __init__(self):
            self.id = 400
            self.parent_id = 300

    thread = FakeThread()
    forum_issue = {"number": 22, "html_url": "https://github.com/crisprintsstuff/OxyBrush/issues/22"}
    await db.add_filing(
        message_id=400, repo=repo, channel_id=300, thread_id=400,
        issue=forum_issue, reporter_id=5, filer_id=5, close_thread=True,
    )
    thread_msg = _message(401, alice, "Added logs", channel=thread)
    await manager.on_message(thread_msg)
    assert bot.github.comment_issue.await_count == 2
    assert bot.github.comment_issue.call_args.args[2] == 22


async def test_github_comments_mirror_to_forum_thread_without_echo(db):
    manager, bot, repo = await _setup(db, app_slug="oxytocin-bot")

    class FakeThread(discord.Thread):
        def __init__(self):
            self.id = 400
            self.archived = False
            self.sent = []

        async def send(self, *args, **kwargs):
            self.sent.append(kwargs.get("embed"))

        async def edit(self, **kwargs):
            self.archived = kwargs.get("archived", self.archived)

    thread = FakeThread()
    forum_issue = {"number": 30, "html_url": "https://github.com/crisprintsstuff/OxyBrush/issues/30"}
    filing = await db.add_filing(
        message_id=400, repo=repo, channel_id=300, thread_id=400,
        issue=forum_issue, reporter_id=5, filer_id=5, close_thread=True,
    )
    bot.get_channel = lambda cid: thread if cid == 400 else None
    bot.github.get_issue = AsyncMock(return_value=({"state": "open"}, '"e1"'))
    bot.github.list_issue_comments = AsyncMock(return_value=(
        [
            {"id": 101, "body": f"From discord\n{SYNC_MARKER}", "user": {"login": "dev"}, "html_url": "https://gh/1"},
            {"id": 102, "body": "Bot note", "user": {"login": "oxytocin-bot[bot]"}, "html_url": "https://gh/2"},
            {"id": 103, "body": "Can you share the .oxy file?", "user": {"login": "cris"}, "html_url": "https://gh/3"},
        ],
        '"c1"',
    ))

    await manager.sync_threads()
    assert len(thread.sent) == 1
    assert thread.sent[0].author.name == "cris"
    assert "Can you share the .oxy file?" in thread.sent[0].description
    assert (await db.get_filing(filing.id)).last_comment_id == 103

    # Second cycle with no newer comments doesn't re-post
    await manager.sync_threads()
    assert len(thread.sent) == 1


async def test_autolink_previews_respect_cooldown_and_toggle(db):
    manager, bot, repo = await _setup(db)
    alice = _member(5, "Alice")
    channel = SimpleNamespace(id=50, name="general", parent_id=None, send=AsyncMock())
    bot.github.get_issue = AsyncMock(return_value=(
        {
            "number": 12, "title": "Fix export", "state": "open",
            "html_url": "https://github.com/crisprintsstuff/OxyBrush/issues/12",
            "labels": [{"name": "bug"}], "comments": 1, "user": {"login": "cris"},
        },
        '"e1"',
    ))

    await manager.on_message(_message(1, alice, "See #12 for details", channel=channel))
    assert channel.send.await_count == 1
    assert "OxyBrush#12" in channel.send.call_args.kwargs["embeds"][0].title

    # Cooldown prevents duplicate preview in the same channel
    await manager.on_message(_message(2, alice, "Still looking at #12", channel=channel))
    assert channel.send.await_count == 1

    # Explicit repo#number works even when disabled? Only when enabled:
    await db.set_autolink_enabled(1, False)
    await manager.on_message(_message(3, alice, "Also OxyBrush#12", channel=channel))
    assert channel.send.await_count == 1
