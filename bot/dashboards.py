"""Live issue dashboards: one message per repo that the bot keeps up to date."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import discord

from .checks import UserError, can_create_issue
from .embeds import truncate
from .github import GitHubError
from .models import Dashboard, report_style

if TYPE_CHECKING:
    from .main import TrackerBot

log = logging.getLogger(__name__)

OPEN_SHOWN = 15
CLOSED_SHOWN = 5
CLOSED_DAYS = 7
PAGE = 100
MANUAL_REFRESH_COOLDOWN = 15
COLOR = discord.Color.from_rgb(9, 105, 218)


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _title(text: str, limit: int) -> str:
    text = discord.utils.escape_markdown(truncate(text, limit))
    return text.replace("[", "\\[").replace("]", "\\]")


def _line(issue: dict[str, Any], when: str, *, labels: bool = True) -> str:
    line = f"[#{issue['number']}]({issue['html_url']}) {_title(issue['title'], 60)}"
    names = [lbl["name"] for lbl in issue.get("labels", []) if isinstance(lbl, dict)][:3]
    if labels and names:
        line += " " + " ".join(f"`{truncate(n, 20)}`" for n in names)
    return f"{line} · <t:{_ts(issue[when])}:R>"


def render_dashboard(dash: Dashboard, open_items: list[dict[str, Any]],
                     closed_items: list[dict[str, Any]], *, interval: int,
                     now: float | None = None) -> tuple[discord.Embed, str]:
    """Build the dashboard embed. Returns (embed, content hash used to skip no-op edits)."""
    now = now or time.time()
    issues = [i for i in open_items if "pull_request" not in i]
    prs = [i for i in open_items if "pull_request" in i]
    more = "+" if len(open_items) >= PAGE else ""

    lines = [f"**{len(issues)}{more}** open issue{'s' if len(issues) != 1 else ''} · "
             f"**{len(prs)}{more}** open pull request{'s' if len(prs) != 1 else ''}", ""]
    if issues:
        lines += [_line(i, "created_at") for i in issues[:OPEN_SHOWN]]
        if len(issues) > OPEN_SHOWN:
            lines.append(f"[…and {len(issues) - OPEN_SHOWN}{more} more on GitHub]({dash.issues_url})")
    else:
        lines.append("🎉 No open issues!")

    cutoff = now - CLOSED_DAYS * 86400
    closed = sorted(
        (i for i in closed_items
         if "pull_request" not in i and i.get("closed_at") and _ts(i["closed_at"]) >= cutoff),
        key=lambda i: i["closed_at"], reverse=True,
    )
    closed_text = "\n".join(_line(i, "closed_at", labels=False) for i in closed[:CLOSED_SHOWN])
    if len(closed) > CLOSED_SHOWN:
        closed_text += f"\n…and {len(closed) - CLOSED_SHOWN} more"

    title = f"📋 {dash.full_name} issues"
    if dash.label:
        title += f" · {dash.label}"
    embed = discord.Embed(title=truncate(title, 256), url=dash.issues_url,
                          description=truncate("\n".join(lines), 4000), color=COLOR)
    embed.add_field(name=f"✅ Closed in the last {CLOSED_DAYS} days",
                    value=truncate(closed_text, 1024) or "None", inline=False)
    minutes = max(1, interval // 60)
    embed.set_footer(text=f"Updates automatically every {minutes} min · Last change")
    # The button wording is part of the hash so dashboards re-render when it changes.
    button = report_style(dash.label).button
    basis = "\n".join([embed.title or "", embed.description or "", closed_text, str(interval), button])
    return embed, hashlib.sha256(basis.encode()).hexdigest()


# ---------------------------------------------------------------- PR dashboard
PR_SHOWN = 15
PR_DETAILS = 25        # open PRs whose checks/reviews are looked up (newest first)
WAITING_SHOWN = 5
MERGED_SHOWN = 5
PR_LEGEND = ("✅ checks pass · ❌ failing · ⏳ running · 👍 approved · 📝 changes requested · "
             "👀 needs review · ⚠️ conflicts · ⚪ draft")


def _pr_icons(status: dict[str, Any] | None) -> str:
    if status is None:
        return "➖"
    if status["draft"]:
        return "⚪"
    checks = {"passing": "✅", "failing": "❌", "pending": "⏳"}.get(status["checks"]["state"], "➖")
    decisions = set(status["reviews"].values())
    if "CHANGES_REQUESTED" in decisions:
        review = "📝"
    elif "APPROVED" in decisions and not status["requested"]:
        review = "👍"
    else:
        review = "👀"
    return checks + review + (" ⚠️" if status.get("merge") == "dirty" else "")


def _needs_review(status: dict[str, Any] | None) -> bool:
    return status is not None and not status["draft"] and \
        not {"APPROVED", "CHANGES_REQUESTED"} & set(status["reviews"].values())


def render_pr_dashboard(dash: Dashboard, open_prs: list[dict[str, Any]],
                        statuses: dict[int, dict[str, Any]], closed: list[dict[str, Any]], *,
                        interval: int, now: float | None = None) -> tuple[discord.Embed, str]:
    """Open PRs with status icons, those waiting longest for review, and recent merges."""
    now = now or time.time()
    more = "+" if len(open_prs) >= PAGE else ""
    drafts = [p for p in open_prs if p.get("draft")]
    ready = [p for p in open_prs if not p.get("draft")]
    st = statuses.get
    mergeable = [p for p in ready if (st(p["number"]) or {}).get("merge") == "clean"]
    attention = [p for p in ready if (s := st(p["number"])) and (
        s["checks"]["state"] == "failing" or s.get("merge") == "dirty"
        or "CHANGES_REQUESTED" in s["reviews"].values())]

    lines = [f"**{len(ready)}{more}** open · **{len(drafts)}** draft{'s' if len(drafts) != 1 else ''} · "
             f"**{len(mergeable)}** ready to merge · **{len(attention)}** need attention", ""]
    ordered = sorted(ready, key=lambda p: p["created_at"], reverse=True) + \
        sorted(drafts, key=lambda p: p["created_at"], reverse=True)
    for p in ordered[:PR_SHOWN]:
        author = (p.get("user") or {}).get("login", "?")
        lines.append(f"{_pr_icons(st(p['number']))} [#{p['number']}]({p['html_url']}) "
                     f"{_title(p['title'], 55)} · {author} · <t:{_ts(p['created_at'])}:R>")
    if len(ordered) > PR_SHOWN:
        lines.append(f"[…and {len(ordered) - PR_SHOWN}{more} more on GitHub]({dash.issues_url})")
    if not open_prs:
        lines.append("🎉 No open pull requests!")

    waiting = sorted((p for p in ready if _needs_review(st(p["number"]))), key=lambda p: p["created_at"])
    waiting_text = "\n".join(f"[#{p['number']}]({p['html_url']}) {_title(p['title'], 50)} · "
                             f"opened <t:{_ts(p['created_at'])}:R>" for p in waiting[:WAITING_SHOWN])

    cutoff = now - CLOSED_DAYS * 86400
    merged = sorted((p for p in closed if p.get("merged_at") and _ts(p["merged_at"]) >= cutoff),
                    key=lambda p: p["merged_at"], reverse=True)
    merged_text = "\n".join(f"[#{p['number']}]({p['html_url']}) {_title(p['title'], 50)} · "
                            f"<t:{_ts(p['merged_at'])}:R>" for p in merged[:MERGED_SHOWN])
    if len(merged) > MERGED_SHOWN:
        merged_text += f"\n…and {len(merged) - MERGED_SHOWN} more"

    title = f"🔀 {dash.full_name} pull requests" + (f" · {dash.label}" if dash.label else "")
    embed = discord.Embed(title=truncate(title, 256), url=dash.issues_url,
                          description=truncate("\n".join(lines), 4000), color=COLOR)
    embed.add_field(name="⏰ Waiting longest for review", value=truncate(waiting_text, 1024) or "Nothing waiting",
                    inline=False)
    embed.add_field(name=f"🟣 Merged in the last {CLOSED_DAYS} days ({len(merged)})",
                    value=truncate(merged_text, 1024) or "None", inline=False)
    minutes = max(1, interval // 60)
    embed.set_footer(text=f"{PR_LEGEND}\nUpdates automatically every {minutes} min · Last change")
    basis = "\n".join([embed.title or "", embed.description or "", waiting_text, merged_text,
                       str(interval)])
    return embed, hashlib.sha256(basis.encode()).hexdigest()


def _has_label(pr: dict[str, Any], label: str | None) -> bool:
    return not label or any(lbl.get("name", "").lower() == label.lower() for lbl in pr.get("labels", []))


# --------------------------------------------------------------------- buttons
async def _run(interaction: discord.Interaction, coro) -> None:
    try:
        await coro
    except Exception as exc:  # show the error to the user like slash commands do
        await interaction.client.on_app_command_error(interaction, exc)  # type: ignore[attr-defined]


class ReportButton(discord.ui.DynamicItem[discord.ui.Button], template=r"dash:report:(?P<id>[0-9]+)"):
    def __init__(self, dashboard_id: int, label: str | None = None):
        style = report_style(label)
        super().__init__(discord.ui.Button(label=style.button, emoji=style.emoji,
                                           style=discord.ButtonStyle.success,
                                           custom_id=f"dash:report:{dashboard_id}"))
        self.dashboard_id = dashboard_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        await _run(interaction, bot.dashboards.handle_report(interaction, self.dashboard_id))


class RefreshButton(discord.ui.DynamicItem[discord.ui.Button], template=r"dash:refresh:(?P<id>[0-9]+)"):
    def __init__(self, dashboard_id: int):
        super().__init__(discord.ui.Button(label="Refresh", emoji="🔄",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"dash:refresh:{dashboard_id}"))
        self.dashboard_id = dashboard_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        await _run(interaction, bot.dashboards.handle_refresh(interaction, self.dashboard_id))


def dashboard_view(dash: Dashboard) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if dash.kind != "prs":
        view.add_item(ReportButton(dash.id, dash.label))
    view.add_item(RefreshButton(dash.id))
    view.add_item(discord.ui.Button(label="View on GitHub", url=dash.issues_url))
    return view


# --------------------------------------------------------------------- manager
class DashboardManager:
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._cache: dict[tuple[Any, ...], tuple[str | None, list[dict[str, Any]]]] = {}
        self._manual: dict[int, float] = {}

    async def _fetch(self, owner: str, name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        key = (owner.lower(), name.lower(), tuple(sorted(params.items())))
        etag, cached = self._cache.get(key, (None, []))
        items, new_etag = await self.bot.github.list_issues(owner, name, params, etag)
        if items is None:  # 304: unchanged since last time
            return cached
        self._cache[key] = (new_etag, items)
        return items

    async def snapshot(self, dash: Dashboard) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        label = {"labels": dash.label} if dash.label else {}
        # Round "since" down to the day so the URL (and its ETag) stays stable all day.
        since = (datetime.now(timezone.utc) - timedelta(days=CLOSED_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
        open_items = await self._fetch(dash.owner, dash.name, {
            "state": "open", "sort": "created", "direction": "desc", "per_page": PAGE, **label})
        closed = await self._fetch(dash.owner, dash.name, {
            "state": "closed", "sort": "updated", "direction": "desc", "per_page": 50,
            "since": since, **label})
        return open_items, closed

    async def render_prs(self, dash: Dashboard) -> tuple[discord.Embed, str]:
        """Fetch a PR dashboard's data (via the PR cards' shared cache) and render it."""
        cards = self.bot.prcards
        open_prs = [p for p in await cards.list_pulls(dash.owner, dash.name, state="open", per_page=PAGE)
                    if _has_label(p, dash.label)]
        closed = [p for p in await cards.list_pulls(dash.owner, dash.name, state="closed", sort="updated",
                                                    direction="desc", per_page=30)
                  if _has_label(p, dash.label)]
        statuses: dict[int, dict[str, Any]] = {}
        newest = sorted((p for p in open_prs if not p.get("draft")), key=lambda p: p["created_at"],
                        reverse=True)[:PR_DETAILS]
        for p in newest:
            try:
                _, statuses[p["number"]] = await cards.fetch_status(dash.owner, dash.name, p["number"])
            except GitHubError as exc:
                if exc.status in (403, 429) or "rate limit" in str(exc).lower():
                    raise
                log.warning("Dashboard %s: PR #%s: %s", dash.id, p["number"], exc)
        for p in open_prs:  # drafts only need their draft flag
            if p.get("draft"):
                statuses.setdefault(p["number"], {"draft": True})
        return render_pr_dashboard(dash, open_prs, statuses, closed, interval=self.bot.poller.interval)

    async def refresh_all(self) -> None:
        ready = getattr(self.bot, "is_ready", lambda: False)()
        has_get_guild = hasattr(self.bot, "get_guild")
        for dash in await self.bot.db.list_dashboards():
            if ready and has_get_guild and self.bot.get_guild(dash.guild_id) is None:
                continue
            try:
                await self.refresh(dash)
            except Exception:
                log.exception("Failed to refresh dashboard %s", dash.id)

    async def refresh_repo(self, guild_id: int, repo_id: int) -> None:
        for dash in await self.bot.db.list_dashboards(guild_id):
            if dash.repo_id == repo_id:
                await self.refresh(dash, force=True)

    async def refresh(self, dash: Dashboard, *, force: bool = False) -> None:
        """Re-render a dashboard, editing its message only if something changed.

        Re-posts the message if it was deleted. Errors are stored on the dashboard
        (shown in /dashboard list) rather than raised.
        """
        async with self._lock:
            db = self.bot.db
            try:
                if dash.kind == "prs":
                    embed, digest = await self.render_prs(dash)
                else:
                    open_items, closed = await self.snapshot(dash)
                    embed, digest = render_dashboard(dash, open_items, closed,
                                                     interval=self.bot.poller.interval)
            except GitHubError as exc:
                await db.set_dashboard_error(dash.id, f"GitHub: {exc}")
                return
            changed = digest != dash.last_hash
            last_change = time.time() if changed or dash.last_change is None else dash.last_change
            embed.timestamp = datetime.fromtimestamp(last_change, tz=timezone.utc)
            if dash.message_id and not changed and not force and not dash.last_error:
                return

            try:
                channel = self.bot.get_channel(dash.channel_id) or \
                    await self.bot.fetch_channel(dash.channel_id)
            except (discord.NotFound, discord.Forbidden):
                await db.set_dashboard_error(dash.id, "Channel is gone or I can't see it")
                return
            if not isinstance(channel, discord.TextChannel):
                await db.set_dashboard_error(dash.id, "Channel is not a text channel")
                return

            view = dashboard_view(dash)
            message_id = dash.message_id
            try:
                if message_id:
                    try:
                        await channel.get_partial_message(message_id).edit(embed=embed, view=view)
                    except discord.NotFound:
                        message_id = None
                if not message_id:
                    msg = await channel.send(embed=embed, view=view)
                    message_id = msg.id
                    await db.set_dashboard_message(dash.id, msg.id)
                    if dash.pinned:
                        try:
                            await msg.pin(reason="Dashboard")
                        except discord.HTTPException as exc:
                            log.warning("Couldn't pin dashboard %s: %s", dash.id, exc)
            except discord.HTTPException as exc:
                await db.set_dashboard_error(dash.id, f"Discord: {exc.text or exc}")
                return
            await db.set_dashboard_state(dash.id, last_hash=digest, last_change=last_change,
                                         error=None)
            dash.message_id, dash.last_hash, dash.last_change, dash.last_error = \
                message_id, digest, last_change, None

    async def message_deleted(self, message_id: int) -> None:
        """Someone deleted a dashboard message: post it again (use /dashboard remove to stop)."""
        dash = await self.bot.db.get_dashboard_by_message(message_id)
        if dash is None:
            return
        await self.bot.db.set_dashboard_message(dash.id, None)
        dash.message_id = None
        log.info("Dashboard %s message deleted; re-posting", dash.id)
        await self.refresh(dash, force=True)

    # ------------------------------------------------------------- interactions
    async def _get(self, interaction: discord.Interaction, dashboard_id: int) -> Dashboard:
        dash = await self.bot.db.get_dashboard(dashboard_id, interaction.guild_id)
        if dash is None:
            raise UserError("This dashboard no longer exists.")
        return dash

    async def handle_report(self, interaction: discord.Interaction, dashboard_id: int) -> None:
        from .cogs.issues import IssueModal

        dash = await self._get(interaction, dashboard_id)
        repo = await self.bot.db.get_repo(dash.guild_id, dash.owner, dash.name)
        cog = self.bot.get_cog("Issues")
        if repo is None or cog is None:
            raise UserError("This dashboard's repository is no longer tracked.")
        allowed, reason = await can_create_issue(interaction, repo)
        if not allowed:
            raise UserError(reason or "You can't create issues here.")
        await cog.check_cooldown(interaction)  # type: ignore[attr-defined]
        await interaction.response.send_modal(IssueModal(cog, repo, labels=dash.label))  # type: ignore[arg-type]

    async def handle_refresh(self, interaction: discord.Interaction, dashboard_id: int) -> None:
        dash = await self._get(interaction, dashboard_id)
        last = self._manual.get(dash.id)
        if last is not None and time.monotonic() - last < MANUAL_REFRESH_COOLDOWN:
            await interaction.response.send_message("It was just refreshed. Try again in a few seconds.",
                                                    ephemeral=True)
            return
        self._manual[dash.id] = time.monotonic()
        await interaction.response.defer()  # acknowledge without sending a message
        await self.refresh(dash, force=True)
        refreshed = await self.bot.db.get_dashboard(dash.id)
        if refreshed and refreshed.last_error:
            await interaction.followup.send(f"❌ Couldn't refresh: {refreshed.last_error}", ephemeral=True)
