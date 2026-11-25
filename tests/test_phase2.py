from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import web

from bot.checks import UserError
from bot.cogs.issues import IssueModal, Issues, find_similar_issues
from bot.config import Config
from bot.dashboards import DashboardManager
from bot.embeds import PURPLE
from bot.github import GitHubClient
from bot.watches import SYNC_MARKER, WatchManager

BUG = SimpleNamespace(id=900, name="bug-report")


def _member(user_id, name, *, admin=False):
    m = MagicMock(spec=discord.Member)
    m.id, m.name, m.display_name, m.bot = user_id, name.lower(), name, False
    m.guild_permissions = discord.Permissions(manage_guild=admin)
    m.roles = []
    m.mention = f"<@{user_id}>"
    return m


def _interaction(user, *, embed=None):
    inter = MagicMock()
    inter.guild_id = 1
    inter.guild = SimpleNamespace(id=1, name="Srv")
    inter.channel = SimpleNamespace(name="general")
    inter.user = user
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.response.send_modal = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    inter.message = SimpleNamespace(embeds=[embed or discord.Embed(title="Filed", description="crash")])
    return inter


async def test_triage_buttons_require_admin_and_act(db):
    repo = await db.add_repo(1, "crisprintsstuff", "OxyBrush", "main", None)
    await db.set_issue_access(1, "everyone")
    filing = await db.add_filing(
        message_id=1, repo=repo, channel_id=50, thread_id=None,
        issue={"number": 42, "html_url": "https://github.com/crisprintsstuff/OxyBrush/issues/42"},
        reporter_id=5, filer_id=5, close_thread=False,
    )
    github = SimpleNamespace(
        close_issue=AsyncMock(),
        comment_issue=AsyncMock(),
        add_issue_labels=AsyncMock(),
        remove_issue_label=AsyncMock(),
    )
    bot = SimpleNamespace(db=db, github=github, config=Config(discord_token="x"))
    manager = WatchManager(bot)

    alice = _member(5, "Alice", admin=False)
    admin = _member(8, "Admin", admin=True)

    # Reporter (non-admin) cannot use staff triage actions
    with pytest.raises(UserError, match="Only bot admins"):
        await manager.handle_complete(_interaction(alice), filing.id)
    with pytest.raises(UserError, match="Only bot admins"):
        await manager.handle_label_prompt(_interaction(alice), filing.id)
    with pytest.raises(UserError, match="Only bot admins"):
        await manager.handle_comment_prompt(_interaction(alice), filing.id)

    # Admin can add/remove labels
    label_inter = _interaction(admin)
    await manager.handle_label_submit(label_inter, filing, "bug, triage", "duplicate")
    github.add_issue_labels.assert_awaited_with("crisprintsstuff", "OxyBrush", 42, ["bug", "triage"])
    github.remove_issue_label.assert_awaited_with("crisprintsstuff", "OxyBrush", 42, "duplicate")
    assert "added `bug`, `triage`" in label_inter.followup.send.call_args.args[0]

    # Admin can post triage comment
    comment_inter = _interaction(admin)
    await manager.handle_comment_submit(comment_inter, filing, "Reproduced on Linux")
    assert "Reproduced on Linux" in github.comment_issue.call_args.args[3]
    assert SYNC_MARKER in github.comment_issue.call_args.args[3]

    # Admin can mark completed
    complete_inter = _interaction(admin)
    await manager.handle_complete(complete_inter, filing.id)
    github.close_issue.assert_awaited_with("crisprintsstuff", "OxyBrush", 42, reason="completed")
    assert (await db.get_filing(filing.id)).state == "closed"
    edited = complete_inter.edit_original_response.call_args.kwargs
    assert edited["embed"].color == PURPLE
    assert "Closed as completed by <@8>" in edited["embed"].description
    assert [c.label for c in edited["view"].children] == ["Open on GitHub"]


async def test_similar_issue_hints_on_submit_and_watch(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    await db.set_issue_access(1, "everyone")
    await db.add_watch(repo, "role", role_id=BUG.id, label="bug")

    github = SimpleNamespace(
        create_issue=AsyncMock(return_value={
            "number": 99, "title": "Export crashes on GLB",
            "html_url": "https://github.com/o/r/issues/99", "labels": [{"name": "bug"}],
        }),
        search_issues=AsyncMock(return_value={
            "items": [
                {"number": 99, "title": "Export crashes on GLB", "html_url": "https://gh/99"},
                {"number": 12, "title": "GLB export crash on macOS", "html_url": "https://gh/12"},
                {"number": 13, "title": "PR unrelated", "html_url": "https://gh/13", "pull_request": {}},
            ]
        }),
        auth_mode="app", install_url=None,
    )
    similar = await find_similar_issues(github, "o", "r", "Export crashes when saving GLB", exclude_number=99)
    assert [s["number"] for s in similar] == [12]
    assert "export crashes saving glb" in github.search_issues.call_args.args[2]

    # Issues.submit includes related issues field
    bot = SimpleNamespace(db=db, github=github, config=Config(discord_token="x", issue_cooldown=0))
    cog = Issues.__new__(Issues)
    cog.bot, cog._last_issue = bot, {}
    inter = _interaction(_member(5, "Alice"))
    inter.client = bot
    await cog.submit(inter, repo, "Export crashes on GLB", "details", "bug", None)
    sent_embed = inter.followup.send.call_args.kwargs["embed"]
    assert any("related open issues" in f.name and "#12" in f.value for f in sent_embed.fields)

    # WatchManager.file also includes related issues field
    bot.get_cog = lambda name: cog if name == "Issues" else None
    manager = WatchManager(bot)
    msg = MagicMock(spec=discord.Message)
    msg.id, msg.author, msg.clean_content, msg.webhook_id = 10, _member(5, "Alice"), "@bug-report Export crashes on GLB", None
    msg.guild = SimpleNamespace(id=1, name="Srv")
    msg.channel = SimpleNamespace(id=50, name="general", parent_id=None)
    msg.role_mentions = [BUG]
    msg.reference = None
    msg.attachments = []
    msg.jump_url = "https://discord.com/channels/1/50/10"
    msg.reply = AsyncMock(return_value=SimpleNamespace(id=10010))
    msg.add_reaction = AsyncMock()
    await manager.on_message(msg)
    watch_embed = msg.reply.call_args.kwargs["embed"]
    assert any("related open issues" in f.name and "#12" in f.value for f in watch_embed.fields)


async def test_issue_templates_db_modal_and_dashboard(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    await db.set_issue_access(1, "everyone")

    default_tpl = await db.set_issue_template(
        repo, "*", title_hint="Default Form", placeholder="General hint", body="Default body"
    )
    bug_tpl = await db.set_issue_template(
        repo, "Bug", title_hint="Custom Bug", placeholder="OS + GPU", body="**OS:**\n**Repro:**"
    )
    assert (await db.get_issue_template(repo.id, "bug")).id == bug_tpl.id
    assert (await db.get_issue_template(repo.id, "ui, bug")).id == bug_tpl.id
    assert (await db.get_issue_template(repo.id, "unknown")).id == default_tpl.id
    assert (await db.get_issue_template(repo.id, None)).id == default_tpl.id
    assert len(await db.list_issue_templates(1)) == 2

    modal = IssueModal(None, repo, labels="bug", template=bug_tpl)
    assert modal.title == "Custom Bug: o/r"
    assert modal.body.placeholder == "OS + GPU"
    assert modal.body.default == "**OS:**\n**Repro:**"

    # Dashboard Report button picks up the matching template
    dash = await db.add_dashboard(repo, 10, label="bug", pinned=False, created_by=None)
    cog = Issues.__new__(Issues)
    bot = SimpleNamespace(db=db, config=Config(discord_token="x", issue_cooldown=0))
    cog.bot, cog._last_issue = bot, {}
    bot.get_cog = lambda name: cog if name == "Issues" else None
    dm = DashboardManager(bot)
    inter = _interaction(_member(5, "Alice"))
    inter.client = bot
    await dm.handle_report(inter, dash.id)
    sent_modal = inter.response.send_modal.call_args.args[0]
    assert sent_modal.title == "Custom Bug: o/r"
    assert sent_modal.body.default == "**OS:**\n**Repro:**"

    assert await db.remove_issue_template(1, repo.id, "bug") is True
    assert await db.remove_issue_template(1, repo.id, "bug") is False


async def test_github_add_and_remove_issue_labels():
    seen = []

    async def add_labels(request):
        seen.append(("POST", await request.json()))
        return web.json_response([{"name": "bug"}])

    async def delete_label(request):
        label = request.match_info["label"]
        seen.append(("DELETE", label))
        if label == "missing":
            return web.json_response({"message": "Not Found"}, status=404)
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/repos/o/r/issues/5/labels", add_labels)
    app.router.add_delete("/repos/o/r/issues/5/labels/{label}", delete_label)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    client = GitHubClient("tok", base_url=f"http://127.0.0.1:{port}")
    await client.start()
    try:
        res = await client.add_issue_labels("o", "r", 5, ["bug"])
        assert res == [{"name": "bug"}]
        await client.remove_issue_label("o", "r", 5, "old label")
        await client.remove_issue_label("o", "r", 5, "missing")  # 404 ignored
        assert seen == [("POST", {"labels": ["bug"]}), ("DELETE", "old label"), ("DELETE", "missing")]
    finally:
        await client.close()
        await runner.cleanup()
