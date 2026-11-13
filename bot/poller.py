from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Awaitable, Callable

from .db import Database
from .github import GitHubClient, GitHubError, NotFound, RateLimited, item_id
from .models import Route

log = logging.getLogger(__name__)

# Never announce more than this many items from one feed in a single cycle.
MAX_ITEMS_PER_CYCLE = 10

Sender = Callable[[Route, str, list[dict[str, Any]]], Awaitable[None]]


class Poller:
    """Periodically fetches every GitHub feed that has at least one enabled route.

    The first time a feed is fetched its current contents are recorded as a
    baseline and nothing is announced; after that, only unseen items are sent.
    """

    def __init__(self, db: Database, github: GitHubClient, sender: Sender,
                 interval: int = 300, wait_until_ready: Callable[[], Awaitable[Any]] | None = None,
                 after_cycle: Callable[[], Awaitable[Any]] | None = None,
                 release_notes_wait: int = 0):
        self.db = db
        # Seconds to hold back a new release whose notes are still empty (0 = don't wait).
        self.release_notes_wait = release_notes_wait
        self._after_cycle = after_cycle
        self.github = github
        self.sender = sender
        self.interval = interval
        self._wait_until_ready = wait_until_ready
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last_run: float | None = None
        self.last_duration: float | None = None
        self.last_stream_count = 0

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="github-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def trigger(self) -> None:
        """Run a poll cycle now instead of waiting for the interval."""
        self._wake.set()

    @property
    def next_run(self) -> float | None:
        return self.last_run + self.interval if self.last_run else None

    async def _loop(self) -> None:
        if self._wait_until_ready:
            await self._wait_until_ready()
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("Poll cycle failed")
            if self._after_cycle:
                try:
                    await self._after_cycle()
                except Exception:
                    log.exception("After-cycle hook failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # ------------------------------------------------------------------- polling
    async def poll_once(self) -> None:
        async with self._lock:
            started = time.monotonic()
            routes = await self.db.list_enabled_routes()
            groups: dict[tuple[str, str], list[Route]] = defaultdict(list)
            for route in routes:
                groups[(route.repo_key, route.stream)].append(route)

            # Drop state for feeds nobody listens to any more, so that if one is
            # re-added later it starts from a fresh baseline instead of replaying
            # everything that happened in between.
            for key in await self.db.list_streams():
                if key not in groups:
                    await self.db.delete_stream(*key)

            rate_limited = 0
            for (repo_key, stream), stream_routes in groups.items():
                try:
                    await self._poll_stream(repo_key, stream, stream_routes)
                except RateLimited:
                    # Limits are per credential (app installation, token, anonymous), so
                    # keep going: other feeds may use other credentials. Exhausted ones
                    # fail instantly without making a request.
                    rate_limited += 1
                except Exception:
                    log.exception("Failed polling %s %s", repo_key, stream)

            if rate_limited:
                log.warning("Skipped %d feed(s) this cycle due to GitHub rate limits", rate_limited)
            self.last_stream_count = len(groups)
            self.last_run = time.time()
            self.last_duration = time.monotonic() - started

    async def ensure_baseline(self, route: Route) -> None:
        """Record the current state of a route's feed so old items are not announced.

        Raises GitHubError (e.g. NotFound for a missing branch) so the caller can
        report it.
        """
        async with self._lock:
            if await self.db.get_stream(route.repo_key, route.stream) is None:
                await self._poll_stream(route.repo_key, route.stream, [])

    def _awaiting_notes(self, stream: str, item: dict[str, Any]) -> bool:
        """A new release with empty notes is held back until notes appear or time runs out."""
        if stream != "releases" or not self.release_notes_wait or (item.get("body") or "").strip():
            return False
        published = item.get("published_at") or item.get("created_at")
        if not published:
            return False
        age = time.time() - datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
        return age < self.release_notes_wait

    async def _poll_stream(self, repo_key: str, stream: str, routes: list[Route]) -> None:
        owner, name = repo_key.split("/", 1)
        if routes:
            owner, name = routes[0].owner, routes[0].name
        state = await self.db.get_stream(repo_key, stream)

        try:
            items, etag = await self.github.fetch_stream(
                owner, name, stream, state["etag"] if state else None
            )
        except GitHubError as exc:
            if state is not None and not isinstance(exc, RateLimited):
                await self.db.set_stream_error(repo_key, stream, str(exc))
            if isinstance(exc, NotFound):
                log.warning("%s %s: %s", repo_key, stream, exc)
                if state is None:
                    raise
                return
            raise

        if items is None:  # 304 Not Modified
            await self.db.save_stream(repo_key, stream, etag=etag)
            return

        ids = [item_id(stream, item) for item in items]
        if state is None:
            await self.db.mark_seen(repo_key, stream, ids)
            await self.db.save_stream(repo_key, stream, etag=etag)
            log.info("Baselined %s %s with %d items", repo_key, stream, len(ids))
            return

        seen = await self.db.seen_ids(repo_key, stream)
        waiting = {i for item, i in zip(items, ids)
                   if i not in seen and self._awaiting_notes(stream, item)}
        new = [item for item, i in zip(items, ids) if i not in seen and i not in waiting]
        await self.db.mark_seen(repo_key, stream, [i for i in ids if i not in waiting])
        await self.db.prune_seen(repo_key, stream, ids)
        # While a release is waiting for notes, skip the ETag so the next poll always
        # re-checks it, even if nothing changed (otherwise the time limit could never pass).
        await self.db.save_stream(repo_key, stream, etag=None if waiting else etag)
        if waiting:
            log.info("%s: %d release(s) waiting for release notes", repo_key, len(waiting))
        if not new:
            return

        new.reverse()  # GitHub returns newest first; announce oldest first.
        if not stream.startswith("commits:"):
            new = new[-MAX_ITEMS_PER_CYCLE:]
        log.info("%s %s: %d new item(s)", repo_key, stream, len(new))

        for route in routes:
            matching = [item for item in new if route.accepts(item)]
            if not matching:
                continue
            try:
                await self.sender(route, stream, matching)
            except Exception:
                log.exception("Failed to deliver to route %s", route.id)
