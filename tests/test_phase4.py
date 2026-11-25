import hashlib
import hmac
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp

from bot.config import Config
from bot.dashboards import DashboardManager
from bot.main import TrackerBot
from bot.models import Route
from bot.prcards import PRCardManager
from bot.webhook import WebhookServer, verify_signature


async def test_persistent_http_cache_survives_restart(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    dash = await db.add_dashboard(repo, 10, label=None, pinned=False, created_by=None)

    channel = SimpleNamespace(
        id=10, send=AsyncMock(return_value=SimpleNamespace(id=500)),
        get_partial_message=lambda _mid: SimpleNamespace(edit=AsyncMock()),
    )
    # First manager fetches 200 and persists ETag + body in SQLite
    gh1 = SimpleNamespace(
        list_issues=AsyncMock(return_value=([{"number": 1, "title": "Bug", "html_url": "https://gh/1",
                                             "created_at": "2026-11-20T10:00:00Z", "labels": []}], '"e1"'))
    )
    bot1 = SimpleNamespace(db=db, github=gh1, poller=SimpleNamespace(interval=300),
                           get_channel=lambda _cid: channel)
    dm1 = DashboardManager(bot1)
    open_items, _ = await dm1.snapshot(dash)
    assert len(open_items) == 1

    # Brand-new DashboardManager (simulating bot restart) gets 304 and uses SQLite cached body
    gh2 = SimpleNamespace(list_issues=AsyncMock(return_value=(None, '"e1"')))
    bot2 = SimpleNamespace(db=db, github=gh2, poller=SimpleNamespace(interval=300),
                           get_channel=lambda _cid: channel)
    dm2 = DashboardManager(bot2)
    open_items2, _ = await dm2.snapshot(dash)
    assert open_items2[0]["title"] == "Bug"
    assert gh2.list_issues.call_args.args[3] == '"e1"'

    # Same for PRCardManager
    pr_gh1 = SimpleNamespace(_get=AsyncMock(return_value=(200, [{"number": 7}], '"pr1"')))
    pm1 = PRCardManager(SimpleNamespace(db=db, github=pr_gh1))
    assert await pm1.list_pulls("o", "r", state="open") == [{"number": 7}]

    pr_gh2 = SimpleNamespace(_get=AsyncMock(return_value=(304, None, '"pr1"')))
    pm2 = PRCardManager(SimpleNamespace(db=db, github=pr_gh2))
    assert await pm2.list_pulls("o", "r", state="open") == [{"number": 7}]
    assert pr_gh2._get.call_args.args[2] == '"pr1"'

    # Old cache entries can be pruned
    await db.conn.execute("UPDATE http_cache SET updated_at = ?", (time.time() - 10 * 86400,))
    await db.conn.commit()
    assert await db.prune_http_cache(7 * 86400) >= 2


async def test_admin_alert_channel_and_cooldown(db):
    bot = TrackerBot(Config(discord_token="x", database_path=":memory:"))
    bot.db = db
    alert_ch = SimpleNamespace(id=99, send=AsyncMock())
    bot.get_channel = lambda cid: alert_ch if cid == 99 else None

    # No alert channel configured -> no-op
    assert await bot.send_alert(1, "k1", "Title", "Detail") is False
    alert_ch.send.assert_not_awaited()

    await db.set_alert_channel(1, 99)
    assert await bot.send_alert(1, "k1", "Route paused", "Missing permissions") is True
    assert alert_ch.send.await_count == 1
    assert "Route paused" in alert_ch.send.call_args.kwargs["embed"].title

    # Cooldown deduplicates identical alert key
    assert await bot.send_alert(1, "k1", "Route paused", "Again") is False
    assert alert_ch.send.await_count == 1

    # _disable_route fires alert with its own key
    repo = await db.add_repo(1, "o", "r", "main", None)
    route = await db.add_route(repo, 50, "releases")
    await bot._disable_route(route, "Channel gone")
    assert alert_ch.send.await_count == 2
    await bot.close()


async def test_webhook_server_signature_and_debounce():
    triggers = 0

    def on_trigger():
        nonlocal triggers
        triggers += 1

    secret = "top-secret"
    server = WebhookServer(
        host="127.0.0.1", port=0, on_trigger=on_trigger,
        secret=secret, path="/webhook", debounce_seconds=0.05,
    )
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/health") as resp:
                assert resp.status == 200

            body = b'{"action":"opened"}'
            # Missing / bad signature rejected
            async with session.post(f"{base}/webhook", data=body,
                                    headers={"X-GitHub-Event": "pull_request"}) as resp:
                assert resp.status == 401

            sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            assert verify_signature(body, sig, secret)

            # Ping doesn't trigger poll
            async with session.post(f"{base}/webhook", data=body,
                                    headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sig}) as resp:
                assert resp.status == 200
                assert (await resp.json())["event"] == "ping"

            # Burst of events coalesces into one debounced trigger
            for ev in ("push", "pull_request"):
                async with session.post(f"{base}/webhook", data=body,
                                        headers={"X-GitHub-Event": ev, "X-Hub-Signature-256": sig}) as resp:
                    assert resp.status == 202

            import asyncio
            await asyncio.sleep(0.12)
            assert triggers == 1
    finally:
        await server.stop()
