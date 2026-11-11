from bot.github import NotFound
from bot.poller import Poller


def rel(id_, tag, pre=False):
    return {"id": id_, "tag_name": tag, "prerelease": pre}


class FakeGitHub:
    def __init__(self):
        self.feeds = {}
        self.calls = 0
        self.rate_limited_until = None

    async def fetch_stream(self, owner, name, stream, etag=None):
        self.calls += 1
        key = (f"{owner}/{name}".lower(), stream)
        if key not in self.feeds:
            raise NotFound("missing", 404)
        items = list(self.feeds[key])
        new_etag = str(hash(tuple(map(str, items))))
        if etag == new_etag:
            return None, etag
        return items, new_etag


async def make(db):
    gh = FakeGitHub()
    sent = []

    async def sender(route, stream, items):
        sent.append((route.id, stream, [i.get("tag_name") or i.get("sha") for i in items]))

    return Poller(db, gh, sender), gh, sent


async def test_baseline_then_new_releases_routed(db):
    poller, gh, sent = await make(db)
    repo = await db.add_repo(1, "Org", "Proj", "main", None)
    stable = await db.add_route(repo, 10, "releases", release_kind="stable")
    beta = await db.add_route(repo, 20, "releases", release_kind="prerelease")
    v2 = await db.add_route(repo, 30, "releases", tag_pattern="v2.*")

    gh.feeds[("org/proj", "releases")] = [rel(1, "v1.0.0")]
    await poller.poll_once()
    assert sent == []  # first fetch is only a baseline

    gh.feeds[("org/proj", "releases")] = [rel(3, "v2.0.0"), rel(2, "v2.0.0-rc1", pre=True),
                                          rel(1, "v1.0.0")]
    await poller.poll_once()
    assert (stable.id, "releases", ["v2.0.0"]) in sent
    assert (beta.id, "releases", ["v2.0.0-rc1"]) in sent
    assert (v2.id, "releases", ["v2.0.0-rc1", "v2.0.0"]) in sent  # oldest first
    assert len(sent) == 3

    sent.clear()
    await poller.poll_once()  # unchanged (etag) -> nothing
    assert sent == []


async def test_shared_feed_fetched_once_across_guilds(db):
    poller, gh, sent = await make(db)
    for guild in (1, 2):
        repo = await db.add_repo(guild, "o", "r", "main", None)
        await db.add_route(repo, guild * 100, "releases")
    gh.feeds[("o/r", "releases")] = []
    await poller.poll_once()
    assert gh.calls == 1
    gh.feeds[("o/r", "releases")] = [rel(1, "v1")]
    await poller.poll_once()
    assert sorted(s[0] for s in sent) == [1, 2]


async def test_commits_per_branch(db):
    poller, gh, sent = await make(db)
    repo = await db.add_repo(1, "o", "r", "main", None)
    route = await db.add_route(repo, 10, "commits", branch="dev")
    gh.feeds[("o/r", "commits:dev")] = [{"sha": "a"}]
    await poller.ensure_baseline(route)
    gh.feeds[("o/r", "commits:dev")] = [{"sha": "c"}, {"sha": "b"}, {"sha": "a"}]
    await poller.poll_once()
    assert sent == [(route.id, "commits:dev", ["b", "c"])]


async def test_unused_streams_are_forgotten(db):
    poller, gh, sent = await make(db)
    repo = await db.add_repo(1, "o", "r", "main", None)
    route = await db.add_route(repo, 10, "releases")
    gh.feeds[("o/r", "releases")] = [rel(1, "v1")]
    await poller.poll_once()
    await db.set_route_enabled(route.id, False)
    await poller.poll_once()
    assert await db.get_stream("o/r", "releases") is None

    # Re-enabling re-baselines instead of replaying what happened while paused.
    gh.feeds[("o/r", "releases")] = [rel(2, "v2"), rel(1, "v1")]
    await db.set_route_enabled(route.id, True)
    await poller.poll_once()
    assert sent == []


async def test_missing_feed_does_not_break_cycle(db):
    poller, gh, sent = await make(db)
    bad = await db.add_repo(1, "gone", "repo", "main", None)
    good = await db.add_repo(1, "o", "r", "main", None)
    await db.add_route(bad, 10, "releases")
    route = await db.add_route(good, 10, "releases")
    gh.feeds[("o/r", "releases")] = []
    await poller.poll_once()
    gh.feeds[("o/r", "releases")] = [rel(1, "v1")]
    await poller.poll_once()
    assert sent == [(route.id, "releases", ["v1"])]
