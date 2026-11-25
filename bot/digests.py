"""Scheduled digests: periodic changelog and stale-review nudge summaries."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import discord

from .embeds import ORANGE, PURPLE, truncate
from .github import GitHubError
from .models import Digest

if TYPE_CHECKING:
    from .main import TrackerBot

log = logging.getLogger(__name__)

MAX_DIGEST_ITEMS = 15
REVIEW_STATUS_LOOKUPS = 25


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def render_changelog_digest(digest: Digest, closed_prs: list[dict[str, Any]],
                            closed_issues: list[dict[str, Any]], *,
                            now: float | None = None) -> discord.Embed:
    now = now or time.time()
    cutoff = now - digest.interval_days * 86400
    merged = sorted(
        (p for p in closed_prs if p.get("merged_at") and _ts(p["merged_at"]) >= cutoff),
        key=lambda p: p["merged_at"], reverse=True,
    )
    issues = sorted(
        (i for i in closed_issues
         if "pull_request" not in i and i.get("closed_at") and _ts(i["closed_at"]) >= cutoff),
        key=lambda i: i["closed_at"], reverse=True,
    )
    embed = discord.Embed(
        title=truncate(f"📰 {digest.full_name} — {digest.interval_days}-day summary", 256),
        url=f"https://github.com/{digest.full_name}",
        color=PURPLE,
        timestamp=datetime.fromtimestamp(now, tz=timezone.utc),
    )
    if merged:
        pr_lines = [
            f"• [#{p['number']}]({p['html_url']}) {truncate(p['title'], 60)} · {(p.get('user') or {}).get('login', '?')}"
            for p in merged[:MAX_DIGEST_ITEMS]
        ]
        if len(merged) > MAX_DIGEST_ITEMS:
            pr_lines.append(f"…and {len(merged) - MAX_DIGEST_ITEMS} more")
        pr_text = "\n".join(pr_lines)
    else:
        pr_text = "None"
    embed.add_field(name=f"🟣 Merged pull requests ({len(merged)})", value=truncate(pr_text, 1024), inline=False)

    if issues:
        issue_lines = [
            f"• [#{i['number']}]({i['html_url']}) {truncate(i['title'], 65)}"
            for i in issues[:MAX_DIGEST_ITEMS]
        ]
        if len(issues) > MAX_DIGEST_ITEMS:
            issue_lines.append(f"…and {len(issues) - MAX_DIGEST_ITEMS} more")
        issue_text = "\n".join(issue_lines)
    else:
        issue_text = "None"
    embed.add_field(name=f"✅ Closed issues ({len(issues)})", value=truncate(issue_text, 1024), inline=False)
    if not merged and not issues:
        embed.description = "No merged pull requests or closed issues in this period."
    return embed


def render_review_digest(digest: Digest, open_prs: list[dict[str, Any]],
                         statuses: dict[int, dict[str, Any]], *,
                         now: float | None = None) -> discord.Embed | None:
    now = now or time.time()
    cutoff = now - digest.stale_days * 86400
    waiting: list[tuple[dict[str, Any], list[str]]] = []
    for pr in sorted(open_prs, key=lambda p: p.get("created_at") or ""):
        if pr.get("draft"):
            continue
        created = _ts(pr["created_at"]) if pr.get("created_at") else now
        if created > cutoff:
            continue
        st = statuses.get(pr["number"])
        if st is not None:
            decisions = set(st.get("reviews", {}).values())
            if decisions & {"APPROVED", "CHANGES_REQUESTED"}:
                continue
            requested = st.get("requested") or []
        else:
            requested = [u["login"] for u in pr.get("requested_reviewers") or []]
        waiting.append((pr, requested))

    if not waiting:
        return None

    lines = []
    for pr, requested in waiting[:MAX_DIGEST_ITEMS]:
        author = (pr.get("user") or {}).get("login", "?")
        line = (f"• [#{pr['number']}]({pr['html_url']}) {truncate(pr['title'], 55)} · "
                f"{author} · <t:{_ts(pr['created_at'])}:R>")
        if requested:
            line += f" (waiting on {', '.join(requested[:3])})"
        lines.append(line)
    if len(waiting) > MAX_DIGEST_ITEMS:
        lines.append(f"…and {len(waiting) - MAX_DIGEST_ITEMS} more")

    return discord.Embed(
        title=truncate(
            f"⏰ {digest.full_name} — {len(waiting)} PR{'s' if len(waiting) != 1 else ''} "
            f"waiting >{digest.stale_days}d for review",
            256,
        ),
        url=f"https://github.com/{digest.full_name}/pulls",
        description=truncate("\n".join(lines), 4000),
        color=ORANGE,
        timestamp=datetime.fromtimestamp(now, tz=timezone.utc),
    )


class DigestManager:
    def __init__(self, bot: TrackerBot):
        self.bot = bot

    async def run_due(self) -> None:
        now = time.time()
        ready = getattr(self.bot, "is_ready", lambda: False)()
        has_get_guild = hasattr(self.bot, "get_guild")
        for digest in await self.bot.db.list_digests():
            if ready and has_get_guild and self.bot.get_guild(digest.guild_id) is None:
                continue
            if digest.last_sent_at is not None and now - digest.last_sent_at < digest.interval_days * 86400:
                continue
            try:
                await self.send(digest)
            except Exception:
                log.exception("Failed running digest %s", digest.id)

    async def send(self, digest: Digest, *, force: bool = False) -> bool:
        now = time.time()
        try:
            if digest.kind == "changelog":
                since = datetime.fromtimestamp(
                    now - digest.interval_days * 86400, tz=timezone.utc
                ).strftime("%Y-%m-%dT00:00:00Z")
                closed_prs = await self.bot.prcards.list_pulls(
                    digest.owner, digest.name, state="closed", sort="updated",
                    direction="desc", per_page=50,
                )
                closed_issues, _ = await self.bot.github.list_issues(
                    digest.owner, digest.name,
                    {"state": "closed", "sort": "updated", "direction": "desc",
                     "per_page": 50, "since": since},
                )
                embed = render_changelog_digest(
                    digest, closed_prs, closed_issues or [], now=now
                )
            else:
                open_prs = await self.bot.prcards.list_pulls(
                    digest.owner, digest.name, state="open", per_page=100
                )
                cutoff = now - digest.stale_days * 86400
                candidates = [
                    p for p in open_prs
                    if not p.get("draft") and _ts(p["created_at"]) <= cutoff
                ][:REVIEW_STATUS_LOOKUPS]
                statuses: dict[int, dict[str, Any]] = {}
                for pr in candidates:
                    try:
                        _, statuses[pr["number"]] = await self.bot.prcards.fetch_status(
                            digest.owner, digest.name, pr["number"]
                        )
                    except GitHubError:
                        pass
                embed = render_review_digest(digest, open_prs, statuses, now=now)
                if embed is None:
                    if not force:
                        await self.bot.db.set_digest_sent(digest.id, now)
                    return False
        except GitHubError as exc:
            err = f"GitHub: {exc}"
            await self.bot.db.set_digest_error(digest.id, err)
            if hasattr(self.bot, "send_alert"):
                await self.bot.send_alert(
                    digest.guild_id, f"digest:{digest.id}", f"Digest #{digest.id} error",
                    f"{digest.describe()}\n{err}",
                )
            return False

        channel = self.bot.get_channel(digest.channel_id)
        if channel is None and hasattr(self.bot, "fetch_channel"):
            try:
                channel = await self.bot.fetch_channel(digest.channel_id)
            except discord.HTTPException:
                channel = None
        if channel is None or not hasattr(channel, "send"):
            err = "Channel is gone or isn't messageable"
            await self.bot.db.set_digest_error(digest.id, err)
            if hasattr(self.bot, "send_alert"):
                await self.bot.send_alert(
                    digest.guild_id, f"digest:{digest.id}", f"Digest #{digest.id} error",
                    f"{digest.describe()}\n{err}",
                )
            return False

        content = f"<@&{digest.mention_role_id}>" if digest.mention_role_id else None
        allowed = discord.AllowedMentions(
            everyone=False, users=False,
            roles=[discord.Object(digest.mention_role_id)] if digest.mention_role_id else False,
        )
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            err = f"Discord: {exc.text or exc}"
            await self.bot.db.set_digest_error(digest.id, err)
            if hasattr(self.bot, "send_alert"):
                await self.bot.send_alert(
                    digest.guild_id, f"digest:{digest.id}", f"Digest #{digest.id} error",
                    f"{digest.describe()}\n{err}",
                )
            return False
        await self.bot.db.set_digest_sent(digest.id, now)
        return True
