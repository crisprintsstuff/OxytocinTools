import time
from types import SimpleNamespace

from bot.dashboards import DashboardManager, dashboard_view, render_pr_dashboard
from bot.models import Dashboard
from bot.prcards import PRCardManager
from tests.test_dashboards import FakeChannel
from tests.test_prcards import FakeGitHub, pr, review, run, serve, status, summarize_checks

DASH = Dashboard(id=1, repo_id=1, guild_id=1, owner="o", name="r", channel_id=10, kind="prs")
NOW = time.mktime(time.strptime("2026-11-21", "%Y-%m-%d"))


def test_render_pr_dashboard():
    ready, failing, conflict, approved, draft = pr(1), pr(2), pr(3), pr(4), pr(5, draft=True)
    for p, created in ((ready, "10"), (failing, "11"), (conflict, "12"), (approved, "13"), (draft, "14")):
        p["created_at"] = f"2026-11-{created}T10:00:00Z"
    statuses = {
        1: status(ready),
        2: status(failing, summarize_checks([run("ci", conclusion="failure")], [])),
        3: status(pr(3, mergeable_state="dirty"), reviews={"ana": "CHANGES_REQUESTED"}),
        4: status(approved, reviews={"ben": "APPROVED"}),
        5: {"draft": True},
    }
    merged = pr(9, state="closed", merged=True)
    merged["merged_at"] = "2026-11-20T10:00:00Z"
    old = pr(8, state="closed", merged=True)
    old["merged_at"] = "2026-10-01T10:00:00Z"

    embed, digest = render_pr_dashboard(DASH, [ready, failing, conflict, approved, draft], statuses,
                                        [merged, old, pr(7, state="closed")], interval=300, now=NOW)
    assert embed.title == "🔀 o/r pull requests"
    lines = embed.description.splitlines()
    assert lines[0] == "**4** open · **1** draft · **3** ready to merge · **2** need attention"
    assert lines[2].startswith("✅👍 [#4]")       # newest first
    assert lines[3].startswith("✅📝 ⚠️ [#3]")
    assert lines[4].startswith("❌👀 [#2]")
    assert lines[5].startswith("✅👀 [#1]")
    assert lines[6].startswith("⚪ [#5]")          # drafts after ready PRs
    waiting, merged_field = embed.fields
    assert [line.split("]")[0] for line in waiting.value.splitlines()] == ["[#1", "[#2"]  # oldest first
    assert merged_field.name == "🟣 Merged in the last 7 days (1)" and "[#9]" in merged_field.value
    assert "✅ checks pass" in embed.footer.text
    assert render_pr_dashboard(DASH, [ready, failing, conflict, approved, draft], statuses,
                               [merged], interval=300, now=NOW)[1] == digest

    empty, _ = render_pr_dashboard(DASH, [], {}, [], interval=300, now=NOW)
    assert "No open pull requests" in empty.description and empty.fields[1].value == "None"


def test_pr_dashboard_links_and_buttons():
    assert DASH.issues_url == "https://github.com/o/r/pulls"
    labelled = Dashboard(**{**DASH.__dict__, "label": "ui"})
    assert "is%3Apr%20is%3Aopen%20label%3A%22ui%22" in labelled.issues_url
    assert labelled.describe() == "o/r pull requests (label `ui`)"
    labels = [getattr(c, "item", c).label for c in dashboard_view(DASH).children]
    assert labels == ["Refresh", "View on GitHub"]  # no Report button on PR dashboards


async def test_pr_dashboard_refresh_shares_the_card_cache(db):
    gh, channel = FakeGitHub(), FakeChannel()
    p1, p2 = pr(1, requested=("ana",)), pr(2, draft=True)
    p1["labels"], p2["labels"] = [{"name": "UI"}], []
    serve(gh, [p1, p2], reviews={1: [review("ben", "APPROVED", "2026-11-20T12:00:00Z")]})
    gh.data["/repos/o/r/pulls"] = [p1, p2]
    closed_key = "/repos/o/r/pulls"
    calls = []
    real_get = gh._get

    async def get(path, params=None, etag=None):
        calls.append((path, (params or {}).get("state"), etag is not None))
        if path == closed_key and (params or {}).get("state") == "closed":
            return 200, [], "closed"
        return await real_get(path, params, etag)
    gh._get = get

    bot = SimpleNamespace(db=db, github=gh, poller=SimpleNamespace(interval=300),
                          get_channel=lambda _id: channel)
    bot.prcards = PRCardManager(bot)
    manager = DashboardManager(bot)
    repo = await db.add_repo(1, "o", "r", "main", None)
    dash = await db.add_dashboard(repo, 10, label="ui", pinned=False, created_by=None, kind="prs")

    bot.prcards.begin_cycle()
    await manager.refresh(dash)
    bot.prcards.end_cycle()
    embed = channel.sent[0]["embed"]
    assert embed.title == "🔀 o/r pull requests · ui"
    assert "✅👀 [#1]" in embed.description and "#2" not in embed.description  # label filter
    assert "/repos/o/r/pulls/2" not in {c[0] for c in calls}  # drafts aren't looked up

    calls.clear()
    bot.prcards.begin_cycle()
    await manager.refresh(await db.get_dashboard(dash.id))
    bot.prcards.end_cycle()
    assert channel.edited == []  # nothing changed
    assert all(sent_etag for path, state, sent_etag in calls if state != "closed")  # conditional


async def test_existing_dashboards_become_issue_dashboards(tmp_path):
    import aiosqlite

    from bot.db import Database

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(
            "CREATE TABLE repos (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,"
            " owner TEXT NOT NULL, name TEXT NOT NULL, default_branch TEXT NOT NULL DEFAULT 'main',"
            " added_by INTEGER, added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO repos (guild_id, owner, name) VALUES (1, 'o', 'r');"
            "CREATE TABLE dashboards (id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,"
            " channel_id INTEGER NOT NULL, message_id INTEGER, label TEXT,"
            " pinned INTEGER NOT NULL DEFAULT 0, last_hash TEXT, last_change REAL, last_error TEXT,"
            " created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO dashboards (repo_id, channel_id) VALUES (1, 10);")
    db = await Database.open(path)
    (dash,) = await db.list_dashboards(1)
    assert dash.kind == "issues" and dash.issues_url == "https://github.com/o/r/issues"
    await db.close()
