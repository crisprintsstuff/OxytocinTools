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

    async def git_ref(request):
        ref = request.match_info["ref"]
        if ref == "heads/discord-attachments":
            return web.json_response({"message": "Not Found"}, status=404)
        if ref == "heads/main":
            return web.json_response({"ref": "refs/heads/main", "object": {"sha": "sha123"}})
        return web.json_response({"message": "Not Found"}, status=404)

    async def create_git_ref(request):
        payload = await request.json()
        return web.json_response({"ref": payload["ref"], "object": {"sha": payload["sha"]}}, status=201)

    async def put_content(request):
        payload = await request.json()
        path = request.match_info["path"]
        return web.json_response({
            "content": {
                "name": path.split("/")[-1],
                "path": path,
                "download_url": f"https://raw.githubusercontent.com/o/r/{payload.get('branch', 'main')}/{path}",
            }
        }, status=201)

    app = web.Application()
    app.router.add_post("/repos/o/r/issues", create_issue)
    app.router.add_get("/repos/o/r/releases", releases)
    app.router.add_get("/repos/o/r/issues", issues)
    app.router.add_get("/repos/o/limited/releases", limited)
    app.router.add_get("/repos/o/r/git/ref/{ref:.+}", git_ref)
    app.router.add_post("/repos/o/r/git/refs", create_git_ref)
    app.router.add_put("/repos/o/r/contents/{path:.+}", put_content)

    async def issue_comments(request):
        headers = {"ETag": '"c1"'}
        if request.headers.get("If-None-Match") == '"c1"':
            return web.Response(status=304, headers=headers)
        return web.json_response([{"id": 10, "body": "First comment"}], headers=headers)

    async def search_issues(request):
        return web.json_response({
            "total_count": 1,
            "items": [{"number": 5, "title": request.query.get("q", ""), "state": "open"}],
        })

    app.router.add_get("/repos/o/r/issues/7/comments", issue_comments)
    app.router.add_get("/search/issues", search_issues)
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


async def test_upload_attachment(gh):
    url = await gh.upload_attachment("o", "r", "main", "screenshot.png", b"fakeimagebytes")
    assert url is not None
    assert url.startswith("https://raw.githubusercontent.com/o/r/discord-attachments/attachments/")
    assert url.endswith("screenshot.png")

    # Second upload re-uses known branch:
    url2 = await gh.upload_attachment("o", "r", "main", "video.mp4", b"fakevideobytes")
    assert url2 is not None
    assert url2.endswith("video.mp4")

    # Huge file is rejected:
    huge = b"x" * (26 * 1024 * 1024)
    assert await gh.upload_attachment("o", "r", "main", "huge.bin", huge) is None


async def test_upload_attachment_does_not_fallback_to_default_branch(aiohttp_server_factory):
    from aiohttp import web

    async def fail_ref(request):
        return web.json_response({"message": "Forbidden"}, status=403)

    app = web.Application()
    app.router.add_get("/repos/o/r/git/ref/{ref:.+}", fail_ref)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    client = GitHubClient("tok", base_url=f"http://127.0.0.1:{port}")
    await client.start()
    try:
        assert await client.upload_attachment("o", "r", "main", "pic.png", b"data") is None
    finally:
        await client.close()
        await runner.cleanup()


async def test_list_issue_comments_and_search(gh):
    comments, etag = await gh.list_issue_comments("o", "r", 7)
    assert comments == [{"id": 10, "body": "First comment"}] and etag == '"c1"'
    assert await gh.list_issue_comments("o", "r", 7, etag) == (None, '"c1"')

    res = await gh.search_issues("o", "r", "crash", state="open", kind="issues")
    assert res["total_count"] == 1
    assert res["items"][0]["title"] == "repo:o/r is:open is:issue crash"
