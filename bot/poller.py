from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable

from .db import Database
from .github import GitHubClient, GitHubError, NotFound, RateLimited, item_id
from .models import Route

log = logging.getLogger(__name__)

# Never announce more than this many items from one feed in a single cycle.
MAX_ITEMS_PER_CYCLE = 10

Sender = Callable[[Route, str, list[dict[str, Any]]], Awaitable[None]]


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def pr_status(pr: dict[str, Any]) -> str:
    """open | closed | merged"""
    return "merged" if pr.get("merged_at") else (pr.get("state") or "open")


def detect_pr_events(prs: list[dict[str, Any]], snapshots: dict[int, tuple[str, bool]],
                     baseline: float) -> list[dict[str, Any]]:
    """Compare PRs with their last known (state, draft) and return lifecycle events.

    Each event is a copy of the PR with ``_event`` set to opened, ready, merged, closed or
    reopened, oldest change first. PRs not seen before are judged by their timestamps, so
    a PR merged long before tracking started isn't announced when someone comments on it.
    """
    events: list[dict[str, Any]] = []
    for pr in sorted(prs, key=lambda p: p.get("updated_at") or ""):
        new = pr_status(pr)
        old = snapshots.get(pr["number"])

        def add(kind: str) -> None:
            events.append({**pr, "_event": kind})

        if old is None:
            if (_epoch(pr.get("created_at")) or 0) > baseline:
                add("opened")
            if new == "merged" and (_epoch(pr.get("merged_at")) or 0) > baseline:
                add("merged")
            elif new == "closed" and (_epoch(pr.get("closed_at")) or 0) > baseline:
                add("closed")
            continue
        old_state, old_draft = old
        if old_state == "open" and new in ("merged", "closed"):
            add(new)
        elif old_state != "open" and new == "open":
            add("reopened")
        if new == "open" and old_draft and not pr.get("draft"):
            add("ready")
    return events


def current_pr_event(pr: dict[str, Any], wanted: Iterable[str]) -> dict[str, Any] | None:
    """Label a PR with the event that best describes its current state (for /route test)."""
    candidates = {"merged": ["merged"], "closed": ["closed"],
                  "open": ["opened", "ready", "reopened"]}[pr_status(pr)]
    if pr_status(pr) == "open" and pr.get("draft"):
        candidates = ["opened"]
    wanted = list(wanted)
    kind = next((c for c in candidates if c in wanted), None)
    return {**pr, "_event": kind} if kind else None


class Poller:
    """Periodically fetches every GitHub feed that has at least one enabled route.

    The first time a feed is fetched its current contents are recorded as a
    baseline and nothing is announced; after that, only unseen items are sent.
    """

    def __init__(self, db: Database, github: GitHubClient, sender: Sender,
                 interval: int = 300, wait_until_ready: Callable[[], Awaitable[Any]] | None = None,
                 after_cycle: Callable[[], Awaitable[Any]] | None = None,
                 release_notes_wait: int = 0,
                 active_guilds: Callable[[], set[int] | None] | None = None):
        self.db = db
        # Seconds to hold back a new release whose notes are still empty (0 = don't wait).
        self.release_notes_wait = release_notes_wait
        self._after_cycle = after_cycle
        self._active_guilds = active_guilds
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
            if self._active_guilds is not None:
                active = self._active_guilds()
                if active is not None:
                    routes = [r for r in routes if r.guild_id in active]
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
        if stream == "prs":
            await self._poll_prs(repo_key, routes)
            return
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

    async def _poll_prs(self, repo_key: str, routes: list[Route]) -> None:
        """Pull requests: compare each recently updated PR with its last known state."""
        owner, name = repo_key.split("/", 1)
        if routes:
            owner, name = routes[0].owner, routes[0].name
        state = await self.db.get_stream(repo_key, "prs")
        baseline = await self.db.get_pr_baseline(repo_key)
        fresh = state is None or baseline is None
        try:
            items, etag = await self.github.fetch_stream(
                owner, name, "prs", None if fresh else state["etag"])  # type: ignore[index]
        except GitHubError as exc:
            if state is not None and not isinstance(exc, RateLimited):
                await self.db.set_stream_error(repo_key, "prs", str(exc))
            if isinstance(exc, NotFound) and state is not None:
                log.warning("%s prs: %s", repo_key, exc)
                return
            raise

        if items is None:  # 304 Not Modified
            await self.db.save_stream(repo_key, "prs", etag=etag)
            return
        rows = [(pr["number"], pr_status(pr), bool(pr.get("draft")), pr.get("updated_at"))
                for pr in items]
        if fresh:
            await self.db.set_pr_baseline(repo_key, time.time())
            await self.db.save_pr_snapshots(repo_key, rows)
            await self.db.save_stream(repo_key, "prs", etag=etag)
            log.info("Baselined %s pull requests (%d)", repo_key, len(rows))
            return

        events = detect_pr_events(items, await self.db.get_pr_snapshots(repo_key),
                                  baseline)  # type: ignore[arg-type]
        await self.db.save_pr_snapshots(repo_key, rows)
        await self.db.save_stream(repo_key, "prs", etag=etag)
        if not events:
            return
        events = events[-MAX_ITEMS_PER_CYCLE:]
        log.info("%s: %d pull request event(s)", repo_key, len(events))
        for route in routes:
            matching = [e for e in events if route.accepts(e)]
            if not matching:
                continue
            try:
                await self.sender(route, "prs", matching)
            except Exception:
                log.exception("Failed to deliver to route %s", route.id)
