import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from aiohttp import web

from bot.cogs.issues import process_attachments
from bot.config import Config
from bot.digests import DigestManager, render_changelog_digest, render_review_digest
from bot.embeds import RED, build_embeds
from bot.github import GitHubClient
from bot.models import PRCard, Repo, Route
from bot.prcards import PRCardManager


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_workflow_failure_stream_filter_and_embed():
    async def runs_handler(request):
        return web.json_response({
            "workflow_runs": [
                {
                    "id": 10, "name": "CI", "run_number": 42, "status": "completed",
                    "conclusion": "success", "head_branch": "main", "event": "push",
                    "head_sha": "abcdef1234", "html_url": "https://gh/runs/10",
                },
                {
                    "id": 11, "name": "CI", "run_number": 43, "status": "completed",
                    "conclusion": "failure", "head_branch": "main", "event": "push",
                    "head_sha": "1234567890", "html_url": "https://gh/runs/11",
                    "head_commit": {"message": "Break build"},
                    "triggering_actor": {"login": "cris"},
                },
            ]
        })

    app = web.Application()
    app.router.add_get("/repos/o/r/actions/runs", runs_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    client = GitHubClient("tok", base_url=f"http://127.0.0.1:{port}")
    await client.start()
    try:
        items, _ = await client.fetch_stream("o", "r", "workflows")
        assert [i["id"] for i in items] == [11]
    finally:
        await client.close()
        await runner.cleanup()

    route = Route(1, 1, 1, "o", "r", 10, "workflows", branch="main")
    assert route.stream == "workflows"
    assert route.accepts(items[0])
    assert not route.accepts({**items[0], "head_branch": "feature"})

    (embed,) = build_embeds("o/r", "workflows", items)
    assert embed.color == RED and "CI #43" in embed.title and "Break build" in embed.description


async def test_pr_card_refresh_button_and_cleanup(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    tracker = await db.add_pr_tracker(repo, 10, branch=None, include_drafts=False,
                                      include_bots=False, threads=False,
                                      mention_role_id=None, created_by=None, cleanup_days=7)

    deleted_ids = []

    class FakeChannel(discord.TextChannel):
        def __init__(self):
            self.id = 10
            self.edits = []

        def get_partial_message(self, mid):
            async def edit(**kwargs):
                self.edits.append(mid)

            async def delete():
                deleted_ids.append(mid)

            return SimpleNamespace(edit=edit, delete=delete)

    channel = FakeChannel()
    pr_payload = {
        "number": 5, "title": "Fix bug", "state": "open", "draft": False,
        "html_url": "https://gh/pr/5", "created_at": "2026-11-20T10:00:00Z",
        "user": {"login": "dev"}, "head": {"ref": "fix", "sha": "abc1234"},
        "base": {"ref": "main"}, "requested_reviewers": [], "requested_teams": [],
        "mergeable_state": "clean",
    }
    github = SimpleNamespace(_get=AsyncMock(side_effect=[
        (200, pr_payload, '"p1"'),
        (200, {"check_runs": []}, '"c1"'),
        (200, {"statuses": []}, '"s1"'),
        (200, [], '"r1"'),
    ]))
    bot = SimpleNamespace(db=db, github=github, get_channel=lambda _cid: channel)
    manager = PRCardManager(bot)

    await db.save_pr_card(PRCard(None, tracker.id, 5, message_id=777, state="open"))
    inter = MagicMock(guild_id=1)
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock()
    await manager.handle_refresh(inter, tracker.id, 5)
    inter.response.defer.assert_awaited_once()
    assert channel.edits == [777]

    # Cooldown prevents immediate second refresh
    inter2 = MagicMock(guild_id=1)
    inter2.response.send_message = AsyncMock()
    await manager.handle_refresh(inter2, tracker.id, 5)
    assert "just refreshed" in inter2.response.send_message.call_args.args[0]

    # Old merged card gets cleaned up after cleanup_days
    old_card = await db.save_pr_card(PRCard(
        None, tracker.id, 6, message_id=888, state="merged",
        closed_at=time.time() - 8 * 86400,
    ))
    await manager._cleanup_old_cards(tracker, [old_card])
    assert deleted_ids == [888]
    cards = {c.number: c for c in await db.list_pr_cards(tracker.id)}
    assert cards[6].state == "cleaned" and cards[6].message_id is None


async def test_orphan_branch_and_media_repo_routing(db):
    seen_commits = []

    async def git_ref(request):
        return web.json_response({"message": "Not Found"}, status=404)

    async def create_blob(request):
        return web.json_response({"sha": "blob1"}, status=201)

    async def create_tree(request):
        return web.json_response({"sha": "tree1"}, status=201)

    async def create_commit(request):
        payload = await request.json()
        seen_commits.append(payload)
        return web.json_response({"sha": "commit1"}, status=201)

    async def create_ref(request):
        return web.json_response({"ref": "refs/heads/discord-attachments"}, status=201)

    async def put_content(request):
        path = request.match_info["path"]
        return web.json_response({
            "content": {"download_url": f"https://raw.githubusercontent.com/org/media/discord-attachments/{path}"}
        }, status=201)

    app = web.Application()
    app.router.add_get("/repos/org/media/git/ref/{ref:.+}", git_ref)
    app.router.add_post("/repos/org/media/git/blobs", create_blob)
    app.router.add_post("/repos/org/media/git/trees", create_tree)
    app.router.add_post("/repos/org/media/git/commits", create_commit)
    app.router.add_post("/repos/org/media/git/refs", create_ref)
    app.router.add_put("/repos/org/media/contents/{path:.+}", put_content)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    client = GitHubClient("tok", base_url=f"http://127.0.0.1:{port}")
    await client.start()
    try:
        repo = await db.add_repo(1, "cris", "private-app", "main", None)
        await db.set_media_repo(1, "org/media")
        bot = SimpleNamespace(db=db, github=client, config=Config(discord_token="x"))
        att = SimpleNamespace(filename="shot.png", url="https://cdn/shot.png", read=AsyncMock(return_value=b"png"))
        res = await process_attachments(bot, repo, [att])
        assert res[0][1].startswith("https://raw.githubusercontent.com/org/media/discord-attachments/attachments/cris_private-app/")
        assert seen_commits and seen_commits[0]["parents"] == []
    finally:
        await client.close()
        await runner.cleanup()


async def test_digests_render_and_run_due(db):
    now = time.time()
    repo = await db.add_repo(1, "o", "r", "main", None)
    changelog = await db.add_digest(repo, 10, "changelog", interval_days=7, mention_role_id=77)
    reviews = await db.add_digest(repo, 20, "reviews", interval_days=7, stale_days=3)

    embed = render_changelog_digest(
        changelog,
        [{"number": 1, "title": "Feat", "html_url": "https://gh/1",
          "merged_at": _iso(now - 3600), "user": {"login": "alice"}}],
        [{"number": 2, "title": "Bug", "html_url": "https://gh/2",
          "closed_at": _iso(now - 3600)}],
        now=now,
    )
    assert "Merged pull requests (1)" in embed.fields[0].name
    assert "Closed issues (1)" in embed.fields[1].name

    assert render_review_digest(reviews, [], {}, now=now) is None
    rev_embed = render_review_digest(
        reviews,
        [{"number": 9, "title": "Stale PR", "html_url": "https://gh/9", "draft": False,
          "created_at": _iso(now - 5 * 86400), "user": {"login": "bob"}}],
        {9: {"reviews": {}, "requested": ["cris"]}},
        now=now,
    )
    assert rev_embed is not None and "Stale PR" in rev_embed.description and "cris" in rev_embed.description

    ch10 = SimpleNamespace(id=10, send=AsyncMock())
    ch20 = SimpleNamespace(id=20, send=AsyncMock())
    prcards = SimpleNamespace(
        list_pulls=AsyncMock(side_effect=[
            [{"number": 1, "title": "Feat", "html_url": "https://gh/1",
              "merged_at": _iso(now - 3600), "user": {"login": "alice"}}],
            [],
        ]),
        fetch_status=AsyncMock(),
    )
    github = SimpleNamespace(list_issues=AsyncMock(return_value=([], '"e1"')))
    bot = SimpleNamespace(
        db=db, github=github, prcards=prcards,
        get_channel=lambda cid: ch10 if cid == 10 else ch20,
    )
    dm = DigestManager(bot)
    await dm.run_due()
    ch10.send.assert_awaited_once()
    assert ch10.send.call_args.kwargs["content"] == "<@&77>"
    ch20.send.assert_not_awaited()  # empty review digest skipped silently
    assert (await db.get_digest(changelog.id)).last_sent_at is not None
    assert (await db.get_digest(reviews.id)).last_sent_at is not None

    # Not due again immediately
    await dm.run_due()
    assert ch10.send.await_count == 1
