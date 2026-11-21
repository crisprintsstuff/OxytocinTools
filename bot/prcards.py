"""Live pull request cards: one message per open PR that shows checks, reviews and merge
status, kept up to date, with a thread that logs each change."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import discord

from .embeds import GREEN, GREY, PURPLE, RED, truncate
from .github import GitHubError, RateLimited
from .models import PRCard, PRTracker

if TYPE_CHECKING:
    from .main import TrackerBot

log = logging.getLogger(__name__)

MAX_NEW_CARDS_PER_SYNC = 10
THREAD_ARCHIVE_MINUTES = 10080  # 7 days without activity

PENDING_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure",
                      "stale"}
STATE_STYLE = {"open": ("🟢", GREEN), "draft": ("⚪", GREY), "merged": ("🟣", PURPLE),
               "closed": ("🔴", RED)}


# ------------------------------------------------------------------ summaries
def summarize_checks(check_runs: list[dict[str, Any]] | None,
                     statuses: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Combine GitHub check runs and commit statuses.

    Returns {"state": passing|failing|pending|none|unavailable, "passed", "failed",
    "pending", "failed_names"}. ``None`` inputs mean the bot lacks permission to read them.
    """
    if check_runs is None and statuses is None:
        return {"state": "unavailable", "passed": 0, "failed": 0, "pending": 0, "failed_names": []}
    passed = failed = pending = 0
    failed_names: list[str] = []
    latest: dict[str, dict[str, Any]] = {}
    for run in check_runs or []:  # keep the newest run of each check (re-runs repeat names)
        name = run.get("name") or "check"
        if name not in latest or (run.get("started_at") or "") >= (latest[name].get("started_at") or ""):
            latest[name] = run
    for name, run in sorted(latest.items()):
        if run.get("status") != "completed":
            pending += 1
        elif run.get("conclusion") in FAILED_CONCLUSIONS:
            failed += 1
            failed_names.append(name)
        else:  # success, neutral, skipped
            passed += 1
    for status in statuses or []:
        state, name = status.get("state"), status.get("context") or "status"
        if state == "pending":
            pending += 1
        elif state in ("failure", "error"):
            failed += 1
            failed_names.append(name)
        else:
            passed += 1
    if failed:
        overall = "failing"
    elif pending:
        overall = "pending"
    elif passed:
        overall = "passing"
    else:
        overall = "none"
    return {"state": overall, "passed": passed, "failed": failed, "pending": pending,
            "failed_names": failed_names}


def summarize_reviews(reviews: list[dict[str, Any]], author: str | None) -> dict[str, str]:
    """Latest decision per reviewer: APPROVED, CHANGES_REQUESTED or DISMISSED.

    Plain comments don't replace an earlier decision. The PR's author is left out.
    """
    latest: dict[str, str] = {}
    for review in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        user = (review.get("user") or {}).get("login")
        state = review.get("state")
        if user and user != author and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[user] = state
    return latest


def build_status(pr: dict[str, Any], checks: dict[str, Any],
                 reviews: dict[str, str]) -> dict[str, Any]:
    requested = [u["login"] for u in pr.get("requested_reviewers") or []]
    requested += [f"@{t['slug']}" for t in pr.get("requested_teams") or []]
    return {
        "state": "merged" if pr.get("merged_at") else (pr.get("state") or "open"),
        "draft": bool(pr.get("draft")),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "checks": checks,
        "reviews": reviews,
        "requested": sorted(requested),
        "merge": pr.get("mergeable_state"),
        "merged_by": (pr.get("merged_by") or {}).get("login"),
    }


# ------------------------------------------------------------------ timeline
def timeline(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """One line per change between two statuses (nothing for a brand-new card)."""
    if old is None:
        return []
    lines: list[str] = []
    if old["state"] != new["state"]:
        if new["state"] == "merged":
            lines.append("🟣 Merged" + (f" by **{new['merged_by']}**" if new.get("merged_by") else ""))
        elif new["state"] == "closed":
            lines.append("🔴 Closed without merging")
        else:
            lines.append("🔄 Reopened")
    if new["state"] != "open":
        return lines
    if old["draft"] and not new["draft"]:
        lines.append("👀 Ready for review")
    elif new["draft"] and not old["draft"]:
        lines.append("📝 Converted to draft")
    if old.get("head_sha") and new.get("head_sha") and old["head_sha"] != new["head_sha"]:
        lines.append(f"⬆️ New commits pushed (`{new['head_sha'][:7]}`)")

    oc, nc = old["checks"], new["checks"]
    if nc["state"] == "failing" and (oc["state"] != "failing" or oc["failed_names"] != nc["failed_names"]):
        lines.append("❌ Checks failed: " + ", ".join(f"**{n}**" for n in nc["failed_names"][:5]))
    elif nc["state"] == "passing" and oc["state"] != "passing":
        lines.append(f"✅ All checks passed ({nc['passed']})")

    for user, state in sorted(new["reviews"].items()):
        if old["reviews"].get(user) == state:
            continue
        if state == "APPROVED":
            lines.append(f"✅ Approved by **{user}**")
        elif state == "CHANGES_REQUESTED":
            lines.append(f"📝 Changes requested by **{user}**")
    for user in new["requested"]:
        if user not in old["requested"]:
            lines.append(f"👋 Review requested from **{user}**")

    if new.get("merge") == "dirty" and old.get("merge") != "dirty":
        lines.append("⚠️ Has merge conflicts")
    elif old.get("merge") == "dirty" and new.get("merge") not in ("dirty", "unknown", None):
        lines.append("✅ Merge conflicts resolved")
    return lines


# ------------------------------------------------------------------- the card
def _ts(value: str | None) -> int | None:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()) if value else None


def _checks_text(c: dict[str, Any]) -> str:
    if c["state"] == "unavailable":
        return "🔒 The bot's GitHub App needs the **Checks** permission"
    if c["state"] == "none":
        return "No checks"
    parts = []
    if c["failed"]:
        names = ", ".join(c["failed_names"][:3]) + ("…" if len(c["failed_names"]) > 3 else "")
        parts.append(f"❌ {c['failed']} failed: {names}")
    if c["pending"]:
        parts.append(f"⏳ {c['pending']} running")
    if c["passed"]:
        parts.append(f"{'✅ ' if c['state'] == 'passing' else ''}{c['passed']} passed")
    return "\n".join(parts)


def _reviews_text(reviews: dict[str, str], requested: list[str]) -> str:
    approved = [u for u, s in reviews.items() if s == "APPROVED" and u not in requested]
    changes = [u for u, s in reviews.items() if s == "CHANGES_REQUESTED" and u not in requested]
    lines = []
    if changes:
        lines.append("📝 Changes requested by " + ", ".join(changes))
    if approved:
        lines.append(f"✅ {len(approved)} approval{'s' if len(approved) != 1 else ''} ({', '.join(approved)})")
    if requested:
        lines.append("👋 Waiting on " + ", ".join(requested))
    return truncate("\n".join(lines), 1024) or "No reviews yet"


_MERGE_TEXT = {
    "clean": "✅ Ready to merge",
    "unstable": "✅ No conflicts (some checks not passing)",
    "has_hooks": "✅ No conflicts",
    "dirty": "⚠️ Has merge conflicts",
    "behind": "⬇️ Behind the base branch",
    "blocked": "🔒 Blocked by branch rules (reviews or checks)",
    "draft": "📝 Draft",
}


def render_card(repo: str, pr: dict[str, Any], status: dict[str, Any]) -> tuple[discord.Embed, str]:
    """The card embed, and a hash of its content to skip edits when nothing changed."""
    key = "draft" if status["state"] == "open" and status["draft"] else status["state"]
    emoji, color = STATE_STYLE[key]
    author = (pr.get("user") or {}).get("login", "?")
    head, base = (pr.get("head") or {}).get("ref", "?"), (pr.get("base") or {}).get("ref", "?")
    lines = [f"**{repo}** · by **{author}** · `{head}` → `{base}`"]
    if (opened := _ts(pr.get("created_at"))) is not None:
        lines.append(f"Opened <t:{opened}:R>")
    if status["state"] == "merged" and (merged := _ts(pr.get("merged_at"))):
        lines.append(f"🟣 Merged <t:{merged}:R>" + (f" by **{status['merged_by']}**" if status.get("merged_by") else ""))
    elif status["state"] == "closed" and (closed := _ts(pr.get("closed_at"))):
        lines.append(f"🔴 Closed without merging <t:{closed}:R>")

    embed = discord.Embed(title=truncate(f"{emoji} #{pr['number']} {pr['title']}", 256),
                          url=pr.get("html_url"), description="\n".join(lines), color=color)
    if status["state"] == "open":
        embed.add_field(name="Checks", value=_checks_text(status["checks"]))
        embed.add_field(name="Reviews", value=_reviews_text(status["reviews"], status["requested"]))
        merge = "📝 Draft" if status["draft"] else _MERGE_TEXT.get(status.get("merge") or "", "⏳ Checking…")
        embed.add_field(name="Merge", value=merge)
    stats = []
    if pr.get("additions") is not None:
        stats.append(f"+{pr['additions']} −{pr.get('deletions', 0)}")
    if pr.get("changed_files") is not None:
        stats.append(f"{pr['changed_files']} file{'s' if pr['changed_files'] != 1 else ''}")
    if pr.get("commits") is not None:
        stats.append(f"{pr['commits']} commit{'s' if pr['commits'] != 1 else ''}")
    if stats:
        embed.set_footer(text=" · ".join(stats))
    digest = hashlib.sha256(json.dumps(embed.to_dict(), sort_keys=True).encode()).hexdigest()
    return embed, digest


def card_view(pr: dict[str, Any]) -> discord.ui.View:
    url = pr.get("html_url") or ""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Open PR", url=url))
    view.add_item(discord.ui.Button(label="Checks", url=f"{url}/checks"))
    view.add_item(discord.ui.Button(label="Files", url=f"{url}/files"))
    return view


# -------------------------------------------------------------------- manager
class PRCardManager:
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._cache: dict[tuple[Any, ...], tuple[str | None, Any]] = {}
        self._used: set[tuple[Any, ...]] = set()
        self._no_checks: set[str] = set()  # repos where reading checks is forbidden (per cycle)

    def checks_blocked(self, repo: str) -> bool:
        """Whether the last update found that the bot may not read this repo's checks."""
        return repo.lower() in self._no_checks

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with an ETag cache, so unchanged data doesn't count against the rate limit."""
        key = (path, tuple(sorted((params or {}).items())))
        self._used.add(key)
        etag, cached = self._cache.get(key, (None, None))
        status, data, new_etag = await self.bot.github._get(path, params, etag if cached is not None else None)
        if status == 304:
            return cached
        self._cache[key] = (new_etag, data)
        return data

    # The cache is shared with PR dashboards. A poll cycle is: begin_cycle(), dashboards
    # and cards fetch what they need, end_cycle() drops responses nobody asked for.
    def begin_cycle(self) -> None:
        self._used.clear()
        self._no_checks.clear()

    def end_cycle(self) -> None:
        for key in [k for k in self._cache if k not in self._used]:
            del self._cache[key]

    async def refresh_all(self) -> None:
        """A whole cycle on its own (cards only)."""
        self.begin_cycle()
        await self.update_all()
        self.end_cycle()

    async def update_all(self) -> None:
        async with self._lock:
            for tracker in await self.bot.db.list_pr_trackers():
                try:
                    await self._sync(tracker)
                except RateLimited:
                    break
                except Exception:
                    log.exception("Failed to update PR cards for tracker %s", tracker.id)

    async def fetch_status(self, owner: str, name: str,
                           number: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """A PR's details and summarized status (checks, reviews, merge). Raises GitHubError."""
        base = f"/repos/{owner}/{name}"
        pr = await self._get(f"{base}/pulls/{number}")
        checks = await self._checks(f"{owner}/{name}".lower(), base, (pr.get("head") or {}).get("sha"))
        reviews = summarize_reviews(await self._get(f"{base}/pulls/{number}/reviews", {"per_page": 100}),
                                    (pr.get("user") or {}).get("login"))
        return pr, build_status(pr, checks, reviews)

    async def list_pulls(self, owner: str, name: str, **params: Any) -> list[dict[str, Any]]:
        return await self._get(f"/repos/{owner}/{name}/pulls", params)

    async def sync(self, tracker: PRTracker) -> None:
        async with self._lock:
            await self._sync(tracker)

    async def _sync(self, tracker: PRTracker) -> None:
        base = f"/repos/{tracker.owner}/{tracker.name}"
        try:
            open_prs = await self._get(f"{base}/pulls", {"state": "open", "per_page": 100,
                                                         "sort": "created", "direction": "asc"})
        except GitHubError as exc:
            await self.bot.db.set_pr_tracker_error(tracker.id, f"GitHub: {exc}")
            if isinstance(exc, RateLimited):
                raise
            return
        cards = {c.number: c for c in await self.bot.db.list_pr_cards(tracker.id)}
        open_numbers = {pr["number"] for pr in open_prs}
        created = 0
        error = None
        for pr in open_prs:
            card = cards.get(pr["number"])
            if card is None:
                if not tracker.wants(pr) or created >= MAX_NEW_CARDS_PER_SYNC:
                    continue
                card = PRCard(None, tracker.id, pr["number"])
                created += 1
            error = await self._update(tracker, card, pr["number"]) or error
        # Cards whose PR just left the open list were merged or closed.
        for card in cards.values():
            if card.state == "open" and card.number not in open_numbers:
                error = await self._update(tracker, card, card.number) or error
        await self.bot.db.set_pr_tracker_error(tracker.id, error)

    async def _checks(self, repo_key: str, base: str, sha: str | None) -> dict[str, Any]:
        if not sha:
            return summarize_checks([], [])
        if repo_key in self._no_checks:
            return summarize_checks(None, None)
        try:
            runs = (await self._get(f"{base}/commits/{sha}/check-runs", {"per_page": 100}))["check_runs"]
        except GitHubError as exc:
            if exc.status != 403:
                raise
            runs = None
        try:
            statuses = (await self._get(f"{base}/commits/{sha}/status"))["statuses"]
        except GitHubError as exc:
            if exc.status != 403:
                raise
            statuses = None
        if runs is None and statuses is None:
            self._no_checks.add(repo_key)
        return summarize_checks(runs, statuses)

    async def _update(self, tracker: PRTracker, card: PRCard, number: int) -> str | None:
        """Bring one card up to date. Returns an error message to record, if any."""
        try:
            pr, status = await self.fetch_status(tracker.owner, tracker.name, number)
        except RateLimited:
            raise
        except GitHubError as exc:
            log.warning("PR %s#%s: %s", tracker.full_name, number, exc)
            return f"GitHub: {exc}"
        old = json.loads(card.status_json) if card.status_json else None
        embed, digest = render_card(tracker.full_name, pr, status)
        lines = timeline(old, status)
        if digest == card.card_hash and not lines and card.message_id:
            return None

        channel = self.bot.get_channel(tracker.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return "The channel is gone or isn't a text channel"
        try:
            await self._post_card(tracker, channel, card, pr, embed)
            if lines and card.thread_id:
                await self._post_timeline(card, lines)
            if status["state"] != "open" and card.thread_id:
                await self._archive(card)
        except discord.HTTPException as exc:
            return f"Discord: {exc.text or exc}"
        card.state = status["state"]
        card.status_json = json.dumps(status)
        card.card_hash = digest
        await self.bot.db.save_pr_card(card)
        return None

    async def _post_card(self, tracker: PRTracker, channel: discord.TextChannel, card: PRCard,
                         pr: dict[str, Any], embed: discord.Embed) -> None:
        view = card_view(pr)
        if card.message_id:
            try:
                await channel.get_partial_message(card.message_id).edit(embed=embed, view=view)
                return
            except discord.NotFound:
                card.message_id = None  # the card was deleted: post it again below
        content = f"<@&{tracker.mention_role_id}>" if tracker.mention_role_id and card.id is None else None
        message = await channel.send(
            content, embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False,
                roles=[discord.Object(tracker.mention_role_id)] if tracker.mention_role_id else False))
        card.message_id = message.id
        if tracker.threads and card.thread_id is None:
            try:
                thread = await message.create_thread(
                    name=truncate(f"#{pr['number']} {pr['title']}", 100),
                    auto_archive_duration=THREAD_ARCHIVE_MINUTES)
                card.thread_id = thread.id
            except discord.HTTPException as exc:
                log.warning("Couldn't create a thread for %s#%s: %s", tracker.full_name, pr["number"], exc)

    async def _thread(self, card: PRCard) -> discord.Thread | None:
        thread = self.bot.get_channel(card.thread_id)  # type: ignore[arg-type]
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(card.thread_id)  # type: ignore[arg-type]
            except discord.HTTPException:
                return None
        return thread if isinstance(thread, discord.Thread) else None

    async def _post_timeline(self, card: PRCard, lines: list[str]) -> None:
        thread = await self._thread(card)
        if thread is None:
            return
        # Sending to an archived (not locked) thread re-opens it.
        await thread.send(truncate("\n".join(lines), 2000),
                          allowed_mentions=discord.AllowedMentions.none())

    async def _archive(self, card: PRCard) -> None:
        thread = await self._thread(card)
        if thread is not None and not thread.archived:
            try:
                await thread.edit(archived=True)
            except discord.HTTPException as exc:
                log.warning("Couldn't archive thread %s: %s", thread.id, exc)
