import time

import pytest
from aiohttp import web

from bot.github import GitHubClient, NotFound, RateLimited


@pytest.fixture
async def gh(aiohttp_server_factory):
    client = GitHubClient("tok", base_url=str(aiohttp_server_factory.make_url("")))
    await client.start()
    yield client
    await client.close()


@pytest.fixture
async def aiohttp_server_factory():

    async def releases(request):
        assert request.headers["Authorization"] == "Bearer tok"
        headers = {"ETag": '"v1"', "X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999",
                   "X-RateLimit-Reset": str(int(time.time()) + 3600)}
        if request.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304, headers=headers)
        return web.json_response([{"id": 2, "draft": True}, {"id": 1, "draft": False}],
                                 headers=headers)

    async def issues(request):
        return web.json_response([{"id": 1}, {"id": 2, "pull_request": {}}])

    async def limited(request):
        return web.json_response({"message": "API rate limit exceeded"}, status=403, headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 100)})

    async def create_issue(request):
        payload = await request.json()
        return web.json_response({"number": 7, "title": payload["title"],
                                  "labels": [{"name": n} for n in payload.get("labels", [])]},
                                 status=201)

    app = web.Application()
    app.router.add_post("/repos/o/r/issues", create_issue)
    app.router.add_get("/repos/o/r/releases", releases)
    app.router.add_get("/repos/o/r/issues", issues)
    app.router.add_get("/repos/o/limited/releases", limited)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class Factory:
        @staticmethod
        def make_url(path):
            return f"http://127.0.0.1:{port}{path}"

    yield Factory
    await runner.cleanup()


async def test_etag_drafts_and_rate_tracking(gh):
    items, etag = await gh.fetch_stream("o", "r", "releases")
    assert [i["id"] for i in items] == [1] and etag == '"v1"'
    assert gh.rate_limits["token"].remaining == 4999
    assert await gh.fetch_stream("o", "r", "releases", etag) == (None, etag)


async def test_issues_exclude_pulls(gh):
    items, _ = await gh.fetch_stream("o", "r", "issues")
    assert [i["id"] for i in items] == [1]


async def test_not_found_and_rate_limit(gh):
    with pytest.raises(NotFound):
        await gh.get_repo("o", "missing")
    with pytest.raises(RateLimited):
        await gh.fetch_stream("o", "limited", "releases")
    with pytest.raises(RateLimited):  # short-circuits without another request
        await gh.fetch_stream("o", "r", "releases")


async def test_create_issue(gh):
    issue = await gh.create_issue("o", "r", "Title", "Body", ["bug"])
    assert issue["number"] == 7 and issue["labels"] == [{"name": "bug"}]
