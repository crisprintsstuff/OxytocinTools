import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bot.github import AppAuth, GitHubClient, GitHubError

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()


@pytest.fixture
async def server():
    calls = {"tokens": 0, "auth": []}

    def check_jwt(request):
        scheme, token = request.headers["Authorization"].split()
        assert scheme == "Bearer"
        claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"])
        assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 600

    async def app_info(request):
        check_jwt(request)
        return web.json_response({"slug": "my-tracker"})

    async def installation(request):
        check_jwt(request)
        if request.match_info["owner"].lower() == "mine":
            return web.json_response({"id": 9})
        return web.json_response({"message": "Not Found"}, status=404)

    async def access_token(request):
        check_jwt(request)
        assert request.match_info["id"] == "9"
        calls["tokens"] += 1
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        return web.json_response({"token": f"inst-{calls['tokens']}",
                                  "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}, status=201)

    async def repo(request):
        auth = request.headers.get("Authorization")
        calls["auth"].append(auth)
        if auth == "token inst-1" and request.match_info["name"] == "limited":
            return web.json_response({"message": "rate limited"}, status=403, headers={
                "X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(time.time()) + 100)})
        return web.json_response({"full_name": f"{request.match_info['owner']}/x"}, headers={
            "X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4000",
            "X-RateLimit-Reset": str(int(time.time()) + 100)})

    app = web.Application()
    app.router.add_get("/app", app_info)
    app.router.add_get("/repos/{owner}/{name}/installation", installation)
    app.router.add_post("/app/installations/{id}/access_tokens", access_token)
    app.router.add_get("/repos/{owner}/{name}", repo)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", calls
    await runner.cleanup()


async def make(url, token=None):
    gh = GitHubClient(token, base_url=url, app_id="123", private_key=PEM)
    await gh.start()
    return gh


async def test_app_installation_tokens(server):
    url, calls = server
    gh = await make(url)
    assert gh.auth_mode == "app" and gh.app_slug == "my-tracker"
    assert gh.install_url == "https://github.com/apps/my-tracker/installations/new"

    await gh.get_repo("mine", "a")
    await gh.get_repo("Mine", "b")
    assert calls["auth"] == ["token inst-1", "token inst-1"]
    assert calls["tokens"] == 1  # token is cached and reused across repos
    await gh.close()


async def test_falls_back_when_app_not_installed(server):
    url, calls = server
    gh = await make(url)
    await gh.get_repo("someone-else", "pub")
    assert calls["auth"] == [None]  # anonymous for public repos
    await gh.close()

    gh = await make(url, token="pat")
    await gh.get_repo("someone-else", "pub")
    assert calls["auth"][-1] == "Bearer pat"
    await gh.close()


async def test_rate_limits_are_per_credential(server):
    url, calls = server
    gh = await make(url, token="pat")
    with pytest.raises(GitHubError):
        await gh.get_repo("mine", "limited")
    assert gh.rate_limits["installation:9"].remaining == 0
    await gh.get_repo("someone-else", "pub")  # other credentials still work
    assert gh.rate_limits["token"].remaining == 4000
    await gh.close()


def test_bad_private_key_fails_fast():
    with pytest.raises(GitHubError, match="private key"):
        AppAuth("1", "not a key")
