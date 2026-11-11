from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp

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


class GitHubClient:
    def __init__(self, token: str | None = None, base_url: str = API):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.session: aiohttp.ClientSession | None = None
        self.rate_limit: int | None = None
        self.rate_remaining: int | None = None
        self.rate_reset: float | None = None

    async def start(self) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "discord-repo-tracker-bot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session = aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=30)
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    @property
    def rate_limited_until(self) -> float | None:
        if self.rate_remaining == 0 and self.rate_reset and self.rate_reset > time.time():
            return self.rate_reset
        return None

    async def _get(self, path: str, params: dict[str, Any] | None = None,
                   etag: str | None = None) -> tuple[int, Any, str | None]:
        """GET a path. Returns (status, json, etag); status 304 means unchanged."""
        return await self._request("GET", path, params=params, etag=etag)

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                       json: Any = None, etag: str | None = None) -> tuple[int, Any, str | None]:
        assert self.session is not None, "GitHubClient.start() was not called"
        if (reset := self.rate_limited_until) is not None:
            raise RateLimited(reset)
        headers = {"If-None-Match": etag} if etag else {}
        async with self.session.request(method, self.base_url + path, params=params, json=json,
                                        headers=headers) as resp:
            self._record_rate_limit(resp)
            if resp.status == 304:
                return 304, None, etag
            if resp.status == 404:
                raise NotFound(f"Not found: {path}", 404)
            if resp.status in (403, 429) and (
                resp.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in resp.headers
            ):
                retry_after = resp.headers.get("Retry-After")
                reset = (time.time() + int(retry_after)) if retry_after else (self.rate_reset or time.time() + 60)
                self.rate_remaining, self.rate_reset = 0, reset
                raise RateLimited(reset)
            if resp.status >= 400:
                try:
                    message = (await resp.json()).get("message", resp.reason)
                except (aiohttp.ContentTypeError, ValueError):
                    message = resp.reason
                raise GitHubError(f"GitHub API {resp.status}: {message}", resp.status)
            return resp.status, await resp.json(), resp.headers.get("ETag")

    def _record_rate_limit(self, resp: aiohttp.ClientResponse) -> None:
        h = resp.headers
        if "X-RateLimit-Remaining" in h:
            self.rate_limit = int(h.get("X-RateLimit-Limit", 0))
            self.rate_remaining = int(h["X-RateLimit-Remaining"])
            self.rate_reset = float(h.get("X-RateLimit-Reset", 0))

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
