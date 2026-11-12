import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot.dashboards import DashboardManager, render_dashboard
from bot.github import GitHubError
from bot.models import Dashboard

NOW = time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(n, title="Bug", *, pr=False, closed_ago=None, labels=()):
    item = {"number": n, "title": title, "html_url": f"https://github.com/o/r/issues/{n}",
            "created_at": iso(NOW - 3600), "labels": [{"name": l} for l in labels]}
    if pr:
        item["pull_request"] = {}
    if closed_ago is not None:
        item["closed_at"] = iso(NOW - closed_ago)
    return item


DASH = Dashboard(id=1, repo_id=1, guild_id=1, owner="o", name="r", channel_id=10)


def test_render_counts_lists_and_closed():
    open_items = [issue(3, "Crash [beta] *now*", labels=["bug", "ui"]), issue(2, pr=True), issue(1)]
    closed = [issue(9, closed_ago=86400), issue(8, closed_ago=30 * 86400), issue(7, pr=True, closed_ago=60)]
    embed, digest = render_dashboard(DASH, open_items, closed, interval=300, now=NOW)
    assert "**2** open issues · **1** open pull request" in embed.description
    assert "[#3](https://github.com/o/r/issues/3) Crash \\[beta\\] \\*now\\* `bug` `ui`" in embed.description
    assert "#2" not in embed.description  # PRs are counted, not listed
    field = embed.fields[0].value
    assert "#9" in field and "#8" not in field and "#7" not in field
    assert embed.footer.text.startswith("Updates automatically every 5 min")

    again, digest2 = render_dashboard(DASH, open_items, closed, interval=300, now=NOW)
    assert digest == digest2
    _, digest3 = render_dashboard(DASH, open_items[:1], closed, interval=300, now=NOW)
    assert digest3 != digest


def test_render_empty_overflow_and_label():
    embed, _ = render_dashboard(DASH, [], [], interval=300, now=NOW)
    assert "No open issues" in embed.description and embed.fields[0].value == "None"

    many = [issue(n) for n in range(100)]
    labelled = Dashboard(**{**DASH.__dict__, "label": "bug"})
    embed, _ = render_dashboard(labelled, many, [], interval=300, now=NOW)
    assert "**100+** open issues" in embed.description
    assert "…and 85+ more on GitHub" in embed.description
    assert embed.title.endswith("· bug")
    assert "label%3A%22bug%22" in embed.url


async def test_dashboard_db(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    dash = await db.add_dashboard(repo, 10, label="bug", pinned=True, created_by=5)
    assert dash.pinned and dash.label == "bug" and dash.message_id is None
    await db.set_dashboard_message(dash.id, 555)
    assert (await db.get_dashboard_by_message(555)).id == dash.id
    assert await db.get_dashboard(dash.id, guild_id=2) is None
    await db.remove_repo(repo.id)
    assert await db.list_dashboards() == []


class FakeChannel(discord.TextChannel):
    """A TextChannel stand-in that records sends and edits."""

    def __init__(self):  # noqa: D401 - deliberately skip discord's constructor
        self.sent, self.edited, self.missing = [], [], set()

    async def send(self, **kwargs):
        msg = SimpleNamespace(id=1000 + len(self.sent), pin=AsyncMock())
        self.sent.append(kwargs)
        return msg

    def get_partial_message(self, message_id):
        async def edit(**kwargs):
            if message_id in self.missing:
                raise discord.NotFound(MagicMock(status=404), "Unknown Message")
            self.edited.append((message_id, kwargs))
        return SimpleNamespace(edit=edit)


async def make_manager(db):
    channel = FakeChannel()
    github = SimpleNamespace(list_issues=AsyncMock(return_value=([issue(1)], '"e1"')))
    bot = SimpleNamespace(db=db, github=github, poller=SimpleNamespace(interval=300),
                          get_channel=lambda _id: channel)
    return DashboardManager(bot), channel, github


async def test_refresh_posts_skips_unchanged_and_reposts(db):
    manager, channel, github = await make_manager(db)
    repo = await db.add_repo(1, "o", "r", "main", None)
    dash = await db.add_dashboard(repo, 10, label=None, pinned=True, created_by=None)

    await manager.refresh(dash)
    assert len(channel.sent) == 1 and channel.edited == []
    dash = await db.get_dashboard(dash.id)
    assert dash.message_id == 1000 and dash.last_error is None
    labels = [getattr(c, "item", c).label for c in channel.sent[0]["view"].children]
    assert labels == ["Open an issue", "Refresh", "View on GitHub"]

    await manager.refresh(dash)  # nothing changed -> no Discord calls
    assert len(channel.sent) == 1 and channel.edited == []

    github.list_issues.return_value = ([issue(2), issue(1)], '"e2"')
    await manager.refresh(dash)
    assert [m for m, _ in channel.edited] == [1000]

    channel.missing.add(1000)  # message was deleted -> post a new one
    await manager.refresh(await db.get_dashboard(dash.id), force=True)
    assert len(channel.sent) == 2
    assert (await db.get_dashboard(dash.id)).message_id == 1001


async def test_refresh_uses_etag_cache_and_records_errors(db):
    manager, channel, github = await make_manager(db)
    repo = await db.add_repo(1, "o", "r", "main", None)
    dash = await db.add_dashboard(repo, 10, label=None, pinned=False, created_by=None)
    await manager.refresh(dash)

    github.list_issues.return_value = (None, '"e1"')  # 304 Not Modified -> reuse cached data
    await manager.refresh(await db.get_dashboard(dash.id), force=True)
    assert "#1" in channel.edited[-1][1]["embed"].description
    assert github.list_issues.call_args.args[3] == '"e1"'

    github.list_issues.side_effect = GitHubError("boom", 500)
    await manager.refresh(await db.get_dashboard(dash.id))
    assert "boom" in (await db.get_dashboard(dash.id)).last_error


async def test_deleted_message_is_reposted(db):
    manager, channel, _ = await make_manager(db)
    repo = await db.add_repo(1, "o", "r", "main", None)
    dash = await db.add_dashboard(repo, 10, label=None, pinned=False, created_by=None)
    await manager.refresh(dash)
    await manager.message_deleted(1000)
    assert len(channel.sent) == 2
    await manager.message_deleted(424242)  # unrelated message: ignored
    assert len(channel.sent) == 2


def test_report_styles():
    from bot.models import report_style

    assert report_style("bug").button == "Report a bug"
    assert report_style("enhancement").button == "Suggest a feature"
    assert report_style("Type: Feature").emoji == "💡"
    assert report_style("documentation").form == "Docs issue"
    assert report_style("question").button == "Ask a question"
    assert report_style("ui, bug").button == "Report a bug"  # first recognised label wins
    assert report_style("wontfix").button == report_style(None).button == "Open an issue"


def test_button_wording_changes_trigger_rerender():
    from bot.dashboards import dashboard_view

    bugs = Dashboard(**{**DASH.__dict__, "label": "bug"})
    ideas = Dashboard(**{**DASH.__dict__, "label": "enhancement"})
    assert dashboard_view(ideas).children[0].item.label == "Suggest a feature"
    assert str(dashboard_view(ideas).children[0].item.emoji) == "💡"
    # Same issues, different label wording -> different hash, so old messages get updated.
    assert render_dashboard(bugs, [], [], interval=300, now=NOW)[1] != \
        render_dashboard(ideas, [], [], interval=300, now=NOW)[1]


async def test_refresh_button_works_right_after_boot(db, monkeypatch):
    import bot.dashboards as dashboards

    monkeypatch.setattr(dashboards.time, "monotonic", lambda: 3.0)  # machine just booted
    manager, channel, _ = await make_manager(db)
    repo = await db.add_repo(1, "o", "r", "main", None)
    dash = await db.add_dashboard(repo, 10, label=None, pinned=False, created_by=None)
    interaction = MagicMock(guild_id=1)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    await manager.handle_refresh(interaction, dash.id)
    interaction.response.defer.assert_awaited()  # refreshed, not "it was just refreshed"
    interaction.response.send_message.assert_not_awaited()
