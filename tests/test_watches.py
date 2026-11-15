from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.checks import UserError
from bot.cogs.issues import Issues
from bot.config import Config
from bot.watches import WatchManager, solved_tag, strip_role_mentions

BUG = SimpleNamespace(id=900, name="bug-report")


def member(user_id, name, *, admin=False):
    m = MagicMock(spec=discord.Member)
    m.id, m.name, m.display_name, m.bot = user_id, name.lower(), name, False
    m.guild_permissions = discord.Permissions(manage_guild=admin)
    m.roles = []
    m.mention = f"<@{user_id}>"
    return m


def message(msg_id, author, text, *, roles=(BUG,), channel_id=50, reference=None, attachments=()):
    m = MagicMock(spec=discord.Message)
    m.id, m.author, m.clean_content, m.webhook_id = msg_id, author, text, None
    m.guild = SimpleNamespace(id=1, name="Srv")
    m.channel = SimpleNamespace(id=channel_id, name="general", parent_id=None)
    m.role_mentions = list(roles)
    m.reference = reference
    m.attachments = list(attachments)
    m.jump_url = f"https://discord.com/channels/1/{channel_id}/{msg_id}"
    m.reply = AsyncMock(return_value=SimpleNamespace(id=msg_id + 10_000))
    m.add_reaction = AsyncMock()
    return m


async def setup(db, *, label="bug", channel_id=None, cooldown=60):
    repo = await db.add_repo(1, "crisprintsstuff", "OxyBrush", "main", None)
    await db.set_issue_access(1, "everyone")
    watch = await db.add_watch(repo, "role", role_id=BUG.id, label=label, channel_id=channel_id)
    numbers = iter(range(42, 100))

    async def create_issue(owner, name, title, body, labels):
        n = next(numbers)
        return {"number": n, "title": title, "html_url": f"https://github.com/{owner}/{name}/issues/{n}"}

    github = SimpleNamespace(create_issue=AsyncMock(side_effect=create_issue), auth_mode="app",
                             install_url=None, close_issue=AsyncMock(), comment_issue=AsyncMock())
    bot = SimpleNamespace(db=db, github=github,
                          config=Config(discord_token="x", issue_cooldown=cooldown))
    issues = Issues.__new__(Issues)
    issues.bot, issues._last_issue = bot, {}
    bot.get_cog = lambda name: issues if name == "Issues" else None
    bot.dashboards = None
    return WatchManager(bot), bot, repo, watch


def test_strip_role_mentions():
    assert strip_role_mentions("@bug-report  Brush stutters\n@bug-report", ["bug-report"]) == "Brush stutters"


async def test_mention_files_issue(db):
    manager, bot, repo, _ = await setup(db)
    alice = member(5, "Alice")
    msg = message(1, alice, "@bug-report Brush stutters on 4K\nSymmetry on, Clay brush")
    await manager.on_message(msg)

    owner, name, title, body, labels = bot.github.create_issue.call_args.args
    assert (owner, name, title, labels) == ("crisprintsstuff", "OxyBrush", "Brush stutters on 4K", ["bug"])
    assert "@bug-report" not in body and "Symmetry on, Clay brush" in body
    assert "Reported from Discord by **Alice** (`alice`)" in body and "Filed by" not in body
    assert "https://discord.com/channels/1/50/1" in body

    reply = msg.reply.call_args.kwargs
    assert reply["embed"].title == "📝 Filed as crisprintsstuff/OxyBrush#42"
    assert [getattr(c, "item", c).label for c in reply["view"].children] == ["Open on GitHub", "Not an issue"]
    msg.add_reaction.assert_awaited_with("✅")
    filing = await db.find_filing(1, repo.id)
    assert filing.issue_number == 42 and filing.reporter_id == filing.filer_id == 5
    assert filing.reply_message_id == 10_001

    # Mentioning again on the same message doesn't file it twice.
    await manager.on_message(msg)
    assert bot.github.create_issue.await_count == 1
    assert "Already filed" in msg.reply.call_args.args[0]


async def test_reply_files_the_original_message(db):
    manager, bot, repo, _ = await setup(db)
    bob, mod = member(6, "Bob"), member(7, "Mod")
    original = message(2, bob, "the export crashes when I pick GLB", roles=())
    trigger = message(3, mod, "@bug-report happens on Windows too",
                      reference=SimpleNamespace(message_id=2, channel_id=50, resolved=original))
    await manager.on_message(trigger)

    _, _, title, body, _ = bot.github.create_issue.call_args.args
    assert title == "the export crashes when I pick GLB"
    assert "**Note from Mod:** happens on Windows too" in body
    assert "Reported from Discord by **Bob**" in body and "Filed by **Mod**" in body
    filing = await db.find_filing(2, repo.id)
    assert (filing.reporter_id, filing.filer_id) == (6, 7)


async def test_ignored_and_refused(db):
    manager, bot, _, _ = await setup(db, channel_id=77)
    alice = member(5, "Alice")
    await manager.on_message(message(1, alice, "@bug-report crash"))  # wrong channel
    await manager.on_message(message(2, alice, "@bug-report", channel_id=77))  # nothing to file
    other_role = SimpleNamespace(id=1, name="mods")
    await manager.on_message(message(3, alice, "@mods crash", roles=[other_role], channel_id=77))
    assert bot.github.create_issue.await_count == 0

    await db.set_issue_access(1, "admins")
    refused = message(4, alice, "@bug-report crash", channel_id=77)
    await manager.on_message(refused)
    assert bot.github.create_issue.await_count == 0
    assert refused.reply.call_args.args[0].startswith("🚫 Only bot admins")
    assert refused.reply.call_args.kwargs["delete_after"] == 30


async def test_cooldown_applies_to_mentions(db):
    manager, bot, _, _ = await setup(db)
    alice = member(5, "Alice")
    await manager.on_message(message(1, alice, "@bug-report first"))
    second = message(2, alice, "@bug-report second")
    await manager.on_message(second)
    assert bot.github.create_issue.await_count == 1
    assert "Slow down" in second.reply.call_args.args[0]
    admin = member(8, "Admin", admin=True)  # admins are exempt
    await manager.on_message(message(3, admin, "@bug-report third"))
    await manager.on_message(message(4, admin, "@bug-report fourth"))
    assert bot.github.create_issue.await_count == 3


async def test_not_an_issue_button(db):
    manager, bot, repo, _ = await setup(db)
    alice, bob = member(5, "Alice"), member(6, "Bob")
    await manager.on_message(message(1, alice, "@bug-report crash"))
    filing = await db.find_filing(1, repo.id)

    def interaction(user):
        inter = MagicMock()
        inter.guild_id, inter.user = 1, user
        inter.guild = SimpleNamespace(id=1)
        inter.response.defer = AsyncMock()
        inter.response.send_message = AsyncMock()
        inter.edit_original_response = AsyncMock()
        inter.message = SimpleNamespace(embeds=[discord.Embed(title="Filed", description="crash")])
        return inter

    with pytest.raises(UserError, match="Only the person who reported it"):
        await manager.handle_close(interaction(bob), filing.id)
    bot.github.close_issue.assert_not_awaited()

    inter = interaction(alice)
    await manager.handle_close(inter, filing.id)
    bot.github.close_issue.assert_awaited_with("crisprintsstuff", "OxyBrush", 42)
    assert "Closed from Discord by **Alice**" in bot.github.comment_issue.call_args.args[3]
    assert (await db.get_filing(filing.id)).state == "not_planned"
    edited = inter.edit_original_response.call_args.kwargs
    assert "Closed as not an issue by <@5>" in edited["embed"].description
    assert [c.label for c in edited["view"].children] == ["Open on GitHub"]

    again = interaction(alice)
    await manager.handle_close(again, filing.id)
    assert "already closed" in again.response.send_message.call_args.args[0]


class FakeForum(discord.ForumChannel):
    def __init__(self, tags):
        self._tags = tags
        self.id = 300

    @property
    def available_tags(self):
        return self._tags


class FakeThread(discord.Thread):
    def __init__(self, forum, applied=()):
        self.id, self.archived, self._forum, self._applied = 400, False, forum, list(applied)
        self.name = "Crash when exporting GLB"
        self.starter = None
        self.sent, self.edits, self.added = [], [], []

    @property
    def guild(self):
        return SimpleNamespace(id=1, name="Srv")

    @property
    def starter_message(self):
        return self.starter

    @property
    def parent_id(self):
        return self._forum.id

    @property
    def parent(self):
        return self._forum

    @property
    def applied_tags(self):
        return self._applied

    async def send(self, text, **kwargs):
        self.sent.append(text)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def add_tags(self, *tags, reason=None):
        self.added.extend(tags)


def tag(name):
    return SimpleNamespace(name=name)


def test_solved_tag():
    assert solved_tag(FakeForum([tag("Bug"), tag("Resolved")])).name == "Resolved"
    assert solved_tag(FakeForum([tag("Bug")])) is None


async def test_forum_post_becomes_issue_and_closes_with_it(db):
    manager, bot, repo, _ = await setup(db)
    await db.add_watch(repo, "forum", channel_id=300, label="from-discord")
    bot.github.list_labels = AsyncMock(return_value=[{"name": "bug"}, {"name": "enhancement"}])
    solved = tag("Solved")
    forum = FakeForum([tag("Bug"), tag("Windows"), solved])
    thread = FakeThread(forum, applied=[tag("Bug"), tag("Windows")])
    starter = message(400, member(5, "Alice"), "Steps: open a file, export as GLB", roles=())
    thread.starter = starter

    await manager.on_thread_create(thread)
    _, _, title, body, labels = bot.github.create_issue.call_args.args
    assert title == "Crash when exporting GLB"
    assert labels == ["from-discord", "bug"]  # "Windows" isn't a GitHub label
    filing = await db.find_filing(400, repo.id)
    assert filing.thread_id == 400 and filing.close_thread

    # The issue is still open: nothing happens.
    bot.github.get_issue = AsyncMock(return_value=({"state": "open"}, '"e1"'))
    bot.get_channel = lambda _id: thread
    await manager.sync_threads()
    assert thread.sent == []

    # Closed as completed: the post gets a note, the Solved tag, and is locked.
    bot.github.get_issue = AsyncMock(return_value=({"state": "closed", "state_reason": "completed"}, '"e2"'))
    await manager.sync_threads()
    assert "was closed as completed" in thread.sent[0]
    assert thread.added == [solved]
    assert thread.edits[-1] == {"archived": True, "locked": True, "reason": "GitHub issue closed"}
    assert (await db.get_filing(filing.id)).state == "closed"
    await manager.sync_threads()  # only once
    assert len(thread.sent) == 1


async def test_forum_starter_is_not_filed_twice_by_a_role_mention(db):
    manager, bot, repo, _ = await setup(db)
    await db.add_watch(repo, "forum", channel_id=300)
    starter = message(400, member(5, "Alice"), "@bug-report crash")
    starter.channel = FakeThread(FakeForum([]))  # forum id 300, thread id 400 == message id
    await manager.on_message(starter)
    assert bot.github.create_issue.await_count == 0
