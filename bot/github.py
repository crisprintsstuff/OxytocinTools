from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp
import jwt as pyjwt

log = logging.getLogger(__name__)

API = "https://api.github.com"
PER_PAGE = 30


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, reset_at: float):
        wait = max(0, int(reset_at - time.time()))
        super().__init__(f"GitHub rate limit exceeded, resets in {wait}s", 403)
        self.reset_at = reset_at


def item_id(stream: str, item: dict[str, Any]) -> str:
    """Stable identifier for an item in a stream (commit SHA, otherwise numeric id)."""
    if stream.startswith("commits:"):
        return item["sha"]
    return str(item["id"])


@dataclass
class RateLimit:
    limit: int
    remaining: int
    reset: float

    @property
    def exhausted_until(self) -> float | None:
        return self.reset if self.remaining == 0 and self.reset > time.time() else None


class AppAuth:
    """Signs the short-lived JWTs that identify the GitHub App itself."""

    def __init__(self, app_id: str, private_key: str):
        self.app_id = str(app_id)
        self.private_key = private_key
        self._jwt: tuple[str, float] | None = None
        self.jwt()  # fail fast on a malformed key

    def jwt(self) -> str:
        now = time.time()
        if self._jwt and self._jwt[1] > now + 60:
            return self._jwt[0]
        # Backdate iat for clock drift; GitHub allows at most 10 minutes of validity.
        payload = {"iat": int(now) - 60, "exp": int(now) + 540, "iss": self.app_id}
        try:
            token = pyjwt.encode(payload, self.private_key, algorithm="RS256")
        except (ValueError, TypeError, pyjwt.PyJWTError) as exc:
            raise GitHubError("Invalid GitHub App private key: expected the contents of the .pem "
                              "file GitHub generated for the app") from exc
        self._jwt = (token, now + 540)
        return token


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GitHubClient:
    """Small GitHub REST client.

    Authentication, in order of preference for each repository:
      1. GitHub App installation token, if the app is installed on the repo's owner
      2. ``token`` (a personal access token), if configured
      3. anonymous (public data only, 60 requests/hour)
    Rate limits are tracked separately for each of these.
    """

    INSTALL_CACHE_HIT = 3600   # re-check where the app is installed hourly
    INSTALL_CACHE_MISS = 300   # notice new installations within 5 minutes

    def __init__(self, token: str | None = None, base_url: str = API, *,
                 app_id: str | None = None, private_key: str | None = None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.app = AppAuth(app_id, private_key) if app_id and private_key else None
        self.app_slug: str | None = None
        self.session: aiohttp.ClientSession | None = None
        self.rate_limits: dict[str, RateLimit] = {}
        self._installations: dict[str, tuple[int | None, float]] = {}
        self._install_tokens: dict[int, tuple[str, float]] = {}

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "discord-repo-tracker-bot",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if self.app:
            try:
                _, data, _ = await self._raw("GET", "/app", bucket="app", auth=f"Bearer {self.app.jwt()}")
                self.app_slug = data.get("slug")
                log.info("Authenticated as GitHub App %s", self.app_slug)
            except GitHubError as exc:
                log.error("GitHub App authentication failed: %s", exc)

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------- auth helpers
    @property
    def auth_mode(self) -> str:
        if self.app:
            return "app"
        return "token" if self.token else "anonymous"

    @property
    def install_url(self) -> str | None:
        if self.app_slug:
            return f"https://github.com/apps/{self.app_slug}/installations/new"
        return None

    async def _installation_id(self, owner: str, name: str) -> int | None:
        assert self.app is not None
        key = f"{owner}/{name}".lower()
        cached = self._installations.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
        try:
            _, data, _ = await self._raw("GET", f"/repos/{owner}/{name}/installation",
                                         bucket="app", auth=f"Bearer {self.app.jwt()}")
            inst_id: int | None = data["id"]
        except NotFound:
            inst_id = None
        ttl = self.INSTALL_CACHE_HIT if inst_id else self.INSTALL_CACHE_MISS
        self._installations[key] = (inst_id, time.time() + ttl)
        return inst_id

    async def _installation_token(self, inst_id: int) -> str:
        assert self.app is not None
        cached = self._install_tokens.get(inst_id)
        if cached and cached[1] > time.time() + 300:
            return cached[0]
        _, data, _ = await self._raw("POST", f"/app/installations/{inst_id}/access_tokens",
                                     bucket="app", auth=f"Bearer {self.app.jwt()}")
        self._install_tokens[inst_id] = (data["token"], _parse_time(data["expires_at"]))
        return data["token"]

    async def _auth_for(self, path: str) -> tuple[str, str | None]:
        """Pick (rate-limit bucket, Authorization header) for a request path."""
        parts = path.split("/")
        if self.app and len(parts) >= 4 and parts[1] == "repos":
            inst_id = await self._installation_id(parts[2], parts[3])
            if inst_id is not None:
                return f"installation:{inst_id}", f"token {await self._installation_token(inst_id)}"
        if self.token:
            return "token", f"Bearer {self.token}"
        return "anonymous", None

    def _forget_installation(self, path: str) -> None:
        parts = path.split("/")
        if len(parts) >= 4:
            cached = self._installations.pop(f"{parts[2]}/{parts[3]}".lower(), None)
            if cached and cached[0] is not None:
                self._install_tokens.pop(cached[0], None)

    @property
    def rate_limited_until(self) -> float | None:
        """When the default (non-app) credentials can be used again, if exhausted."""
        bucket = self.rate_limits.get("token" if self.token else "anonymous")
        return bucket.exhausted_until if bucket else None

    # ------------------------------------------------------------------ requests
    async def _get(self, path: str, params: dict[str, Any] | None = None,
                   etag: str | None = None) -> tuple[int, Any, str | None]:
        """GET a path. Returns (status, json, etag); status 304 means unchanged."""
        return await self._request("GET", path, params=params, etag=etag)

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                       json: Any = None, etag: str | None = None) -> tuple[int, Any, str | None]:
        bucket, auth = await self._auth_for(path)
        try:
            return await self._raw(method, path, bucket=bucket, auth=auth, params=params,
                                   json=json, etag=etag)
        except GitHubError as exc:
            if exc.status == 401 and bucket.startswith("installation:"):
                self._forget_installation(path)  # app uninstalled or token revoked
            raise

    async def _raw(self, method: str, path: str, *, bucket: str, auth: str | None,
                   params: dict[str, Any] | None = None, json: Any = None,
                   etag: str | None = None) -> tuple[int, Any, str | None]:
        assert self.session is not None, "GitHubClient.start() was not called"
        limit = self.rate_limits.get(bucket)
        if limit and (reset := limit.exhausted_until) is not None:
            raise RateLimited(reset)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if auth:
            headers["Authorization"] = auth
        async with self.session.request(method, self.base_url + path, params=params, json=json,
                                        headers=headers) as resp:
            self._record_rate_limit(bucket, resp)
            if resp.status == 304:
                return 304, None, etag
            if resp.status == 404:
                raise NotFound(f"Not found: {path}", 404)
            if resp.status in (403, 429) and (
                resp.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in resp.headers
            ):
                retry_after = resp.headers.get("Retry-After")
                current = self.rate_limits.get(bucket)
                if retry_after:  # secondary rate limit
                    reset = time.time() + int(retry_after)
                elif current and current.reset > time.time():
                    reset = current.reset
                else:
                    reset = time.time() + 60
                self.rate_limits[bucket] = RateLimit(current.limit if current else 0, 0, reset)
                raise RateLimited(reset)
            if resp.status >= 400:
                try:
                    message = (await resp.json()).get("message", resp.reason)
                except (aiohttp.ContentTypeError, ValueError):
                    message = resp.reason
                raise GitHubError(f"GitHub API {resp.status}: {message}", resp.status)
            return resp.status, await resp.json(), resp.headers.get("ETag")

    def _record_rate_limit(self, bucket: str, resp: aiohttp.ClientResponse) -> None:
        h = resp.headers
        if "X-RateLimit-Remaining" in h:
            self.rate_limits[bucket] = RateLimit(
                int(h.get("X-RateLimit-Limit", 0)), int(h["X-RateLimit-Remaining"]),
                float(h.get("X-RateLimit-Reset", 0)))

    # ----------------------------------------------------------------- endpoints
    async def get_repo(self, owner: str, name: str) -> dict[str, Any]:
        _, data, _ = await self._get(f"/repos/{owner}/{name}")
        return data

    async def create_issue(self, owner: str, name: str, title: str, body: str,
                           labels: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            # GitHub silently drops labels unless the token's account has triage access.
            payload["labels"] = labels
        _, data, _ = await self._request("POST", f"/repos/{owner}/{name}/issues", json=payload)
        return data

    async def list_issues(self, owner: str, name: str, params: dict[str, Any],
                          etag: str | None = None) -> tuple[list[dict[str, Any]] | None, str | None]:
        """List issues *and* pull requests (``pull_request`` key marks PRs). None = unchanged."""
        status, data, new_etag = await self._get(f"/repos/{owner}/{name}/issues", params, etag)
        return (None if status == 304 else data), new_etag

    async def get_branch(self, owner: str, name: str, branch: str) -> dict[str, Any]:
        _, data, _ = await self._get(f"/repos/{owner}/{name}/branches/{quote(branch, safe='')}")
        return data

    async def latest_release(self, owner: str, name: str,
                             include_prereleases: bool = False) -> dict[str, Any] | None:
        if not include_prereleases:
            try:
                _, data, _ = await self._get(f"/repos/{owner}/{name}/releases/latest")
                return data
            except NotFound:
                return None
        _, data, _ = await self._get(f"/repos/{owner}/{name}/releases", {"per_page": 10})
        published = [r for r in data if not r.get("draft")]
        return published[0] if published else None

    async def fetch_stream(self, owner: str, name: str, stream: str,
                           etag: str | None = None) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Fetch the newest items of a stream, newest first.

        Returns ``(None, etag)`` when GitHub reports nothing changed since ``etag``.
        """
        base = f"/repos/{owner}/{name}"
        if stream == "releases":
            path, params = f"{base}/releases", {"per_page": PER_PAGE}
        elif stream == "issues":
            path = f"{base}/issues"
            params = {"state": "all", "sort": "created", "direction": "desc", "per_page": PER_PAGE}
        elif stream == "pulls":
            path = f"{base}/pulls"
            params = {"state": "all", "sort": "created", "direction": "desc", "per_page": PER_PAGE}
        elif stream.startswith("commits:"):
            path = f"{base}/commits"
            params = {"sha": stream.split(":", 1)[1], "per_page": PER_PAGE}
        else:
            raise ValueError(f"unknown stream {stream!r}")

        status, data, new_etag = await self._get(path, params, etag)
        if status == 304:
            return None, etag
        if stream == "releases":
            data = [r for r in data if not r.get("draft")]
        elif stream == "issues":
            # The issues endpoint also returns pull requests; those have their own stream.
            data = [i for i in data if "pull_request" not in i]
        return data, new_etag
