import time
from datetime import datetime, timezone

import aiosqlite
import pytest

from bot.db import Database
from bot.embeds import PURPLE, RED, pull_embed
from bot.models import Route, parse_pr_events
from bot.poller import current_pr_event, detect_pr_events
from tests.test_poller import make

NOW = time.time()


def iso(ago):
    return datetime.fromtimestamp(NOW - ago, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(n, *, state="open", draft=False, merged_ago=None, closed_ago=None, created_ago=3600,
       updated_ago=0, base="main", bot=False):
    return {
        "id": 1000 + n, "number": n, "title": f"PR {n}", "state": state, "draft": draft,
        "created_at": iso(created_ago), "updated_at": iso(updated_ago),
        "merged_at": iso(merged_ago) if merged_ago is not None else None,
        "closed_at": iso(closed_ago if closed_ago is not None else merged_ago)
        if (closed_ago is not None or merged_ago is not None) else None,
        "html_url": f"https://github.com/o/r/pull/{n}", "body": "Details",
        "user": {"login": "dependabot[bot]" if bot else "cris", "type": "Bot" if bot else "User"},
        "base": {"ref": base}, "head": {"label": "cris:feature"},
    }


def kinds(events):
    return [(e["number"], e["_event"]) for e in events]


def test_parse_pr_events():
    assert parse_pr_events(None) == ("opened", "ready")
    assert parse_pr_events("merged, opened") == ("opened", "merged")  # canonical order
    assert parse_pr_events("all") == ("opened", "ready", "merged", "closed", "reopened")
    assert parse_pr_events("open close merge") == ("opened", "merged", "closed")
    with pytest.raises(ValueError, match="approved"):
        parse_pr_events("opened, approved")


def test_detect_transitions():
    snaps = {1: ("open", False), 2: ("open", False), 3: ("closed", False), 4: ("open", True),
             5: ("open", False)}
    prs = [pr(1, state="closed", merged_ago=10), pr(2, state="closed", closed_ago=10),
           pr(3), pr(4), pr(5)]
    assert sorted(kinds(detect_pr_events(prs, snaps, NOW - 86400))) == [
        (1, "merged"), (2, "closed"), (3, "reopened"), (4, "ready")]


def test_detect_unknown_prs_by_timestamp():
    baseline = NOW - 600
    prs = [
        pr(10, created_ago=60),                                   # opened after tracking began
        pr(11, created_ago=300, state="closed", merged_ago=30),   # opened and merged since
        pr(12, created_ago=9e5, state="closed", merged_ago=8e5),  # old merged PR, new comment
        pr(13, created_ago=9e5),                                  # old open PR, new comment
        pr(14, created_ago=9e5, state="closed", closed_ago=60),   # old PR, closed just now
    ]
    assert sorted(kinds(detect_pr_events(prs, {}, baseline))) == [
        (10, "opened"), (11, "merged"), (11, "opened"), (14, "closed")]


def route(**kw):
    base = dict(id=1, repo_id=1, guild_id=1, owner="o", name="r", channel_id=1, event_type="pulls")
    base.update(kw)
    return Route(**base)


def test_route_filters():
    opened, merged = {**pr(1), "_event": "opened"}, {**pr(1), "_event": "merged"}
    assert route().accepts(opened) and not route().accepts(merged)  # default: opened, ready
    assert route(pr_events="merged").accepts(merged)
    draft = {**pr(2, draft=True), "_event": "opened"}
    assert not route().accepts(draft) and route(include_drafts=True).accepts(draft)
    ready = {**pr(2), "_event": "ready"}
    assert route().accepts(ready)  # a draft that became ready is posted
    bot = {**pr(3, bot=True), "_event": "opened"}
    assert not route().accepts(bot) and route(include_bots=True).accepts(bot)
    dev = {**pr(4, base="dev"), "_event": "opened"}
    assert route(branch="main").accepts(opened) and not route(branch="main").accepts(dev)
    assert route(branch="release/*").accepts({**pr(5, base="release/1.0"), "_event": "opened"})
    assert route().stream == "prs" and route(branch="main").stream == "prs"
    assert route(pr_events="opened,ready,merged,closed,reopened").describe().startswith(
        "Pull requests (all events)")
    assert "into `main`, incl. drafts" in route(branch="main", include_drafts=True).describe()


def test_current_pr_event():
    assert current_pr_event(pr(1, state="closed", merged_ago=5), ["merged"])["_event"] == "merged"
    assert current_pr_event(pr(1, state="closed", merged_ago=5), ["opened"]) is None
    assert current_pr_event(pr(2), ["ready", "merged"])["_event"] == "ready"
    assert current_pr_event(pr(3, draft=True), ["ready", "opened"])["_event"] == "opened"


def test_pr_embeds():
    merged = pull_embed("o/r", {**pr(7, state="closed", merged_ago=5), "_event": "merged"})
    assert merged.title == "[o/r] PR merged: #7 PR 7" and merged.color == PURPLE
    assert merged.description is None
    closed = pull_embed("o/r", {**pr(8, state="closed", closed_ago=5), "_event": "closed"})
    assert closed.title.startswith("[o/r] PR closed") and closed.color == RED
    draft = pull_embed("o/r", {**pr(9, draft=True), "_event": "opened"})
    assert draft.title.startswith("[o/r] Draft PR opened") and draft.footer.text == "Draft"
    assert pull_embed("o/r", pr(10)).title.startswith("[o/r] PR opened")  # legacy items


async def test_poller_pr_lifecycle(db):
    poller, gh, sent = await make(db)
    repo = await db.add_repo(1, "o", "r", "main", None)
    everything = await db.add_route(repo, 10, "pulls", pr_events="opened,ready,merged,closed,reopened")
    merges = await db.add_route(repo, 20, "pulls", pr_events="merged", branch="main")

    gh.feeds[("o/r", "prs")] = [pr(1, created_ago=9e5), pr(2, draft=True, created_ago=9e5)]
    await poller.poll_once()
    assert sent == []  # baseline
    await db.set_pr_baseline("o/r", NOW - 600)  # tracking began 10 minutes ago

    def sent_names():
        return [(rid, sorted((i["number"], i["_event"]) for i in items)) for rid, _, items in calls]

    calls = []

    async def sender(route, stream, items):
        calls.append((route.id, stream, items))
    poller.sender = sender

    gh.feeds[("o/r", "prs")] = [pr(2), pr(1, state="closed", merged_ago=10), pr(3, created_ago=30)]
    await poller.poll_once()
    assert sorted(sent_names()) == [
        (everything.id, [(1, "merged"), (2, "ready"), (3, "opened")]),
        (merges.id, [(1, "merged")]),
    ]
    assert all(stream == "prs" for _, stream, _ in calls)

    calls.clear()
    await poller.poll_once()  # unchanged (ETag) -> nothing
    assert calls == []

    gh.feeds[("o/r", "prs")] = [pr(1), pr(2), pr(3)]  # PR 1 reopened
    await poller.poll_once()
    assert sent_names() == [(everything.id, [(1, "reopened")])]


async def test_pr_state_is_forgotten_when_no_route_uses_it(db):
    poller, gh, sent = await make(db)
    repo = await db.add_repo(1, "o", "r", "main", None)
    route_ = await db.add_route(repo, 10, "pulls", pr_events="merged")
    gh.feeds[("o/r", "prs")] = [pr(1)]
    await poller.poll_once()
    assert await db.get_pr_snapshots("o/r") == {1: ("open", False)}
    await db.set_route_enabled(route_.id, False)
    await poller.poll_once()
    assert await db.get_pr_snapshots("o/r") == {} and await db.get_pr_baseline("o/r") is None


async def test_old_routes_table_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(
            "CREATE TABLE repos (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,"
            " owner TEXT NOT NULL, name TEXT NOT NULL, default_branch TEXT NOT NULL DEFAULT 'main',"
            " added_by INTEGER, added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO repos (guild_id, owner, name) VALUES (1, 'o', 'r');"
            "CREATE TABLE routes (id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,"
            " channel_id INTEGER NOT NULL, event_type TEXT NOT NULL,"
            " release_kind TEXT NOT NULL DEFAULT 'all', tag_pattern TEXT, branch TEXT,"
            " mention_role_id INTEGER, crosspost INTEGER NOT NULL DEFAULT 0,"
            " enabled INTEGER NOT NULL DEFAULT 1, last_error TEXT, created_by INTEGER,"
            " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO routes (repo_id, channel_id, event_type) VALUES (1, 10, 'pulls');")
    db = await Database.open(path)
    (old,) = await db.list_routes(1)
    assert (old.pr_events, old.include_drafts, old.include_bots) == (None, False, False)
    assert old.pr_event_set == ("opened", "ready") and old.stream == "prs"
    await db.close()
