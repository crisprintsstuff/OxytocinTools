"""Turn Discord messages into GitHub issues: role mentions and forum channel posts."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Iterable

import discord

from .checks import UserError, member_can_create_issue, member_is_admin
from .cogs.issues import format_issue_body, github_issue_error, neutralize_mentions, title_from
from .embeds import GREEN, truncate
from .github import GitHubError, RateLimited
from .models import Filing, Repo, Watch

if TYPE_CHECKING:
    from .main import TrackerBot

log = logging.getLogger(__name__)

# Forum tags applied to a post when its GitHub issue is closed (first match wins).
SOLVED_TAG_NAMES = ("solved", "resolved", "fixed", "done", "completed", "closed")
LABEL_CACHE_SECONDS = 600
NOTICE_SECONDS = 30
STARTER_RETRIES = 3


def strip_role_mentions(text: str, role_names: Iterable[str]) -> str:
    """Remove ``@Role`` mentions (as in ``Message.clean_content``) and tidy whitespace."""
    for name in role_names:
        text = text.replace(f"@{name}", "")
    lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def solved_tag(forum: discord.ForumChannel) -> discord.ForumTag | None:
    tags = {t.name.strip().lower(): t for t in forum.available_tags}
    return next((tags[name] for name in SOLVED_TAG_NAMES if name in tags), None)


class CloseFiledButton(discord.ui.DynamicItem[discord.ui.Button], template=r"filed:close:(?P<id>[0-9]+)"):
    def __init__(self, filing_id: int):
        super().__init__(discord.ui.Button(label="Not an issue", emoji="✖️",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"filed:close:{filing_id}"))
        self.filing_id = filing_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        try:
            await bot.watches.handle_close(interaction, self.filing_id)
        except Exception as exc:
            await bot.on_app_command_error(interaction, exc)  # type: ignore[arg-type]


def filing_view(filing_id: int, issue_url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Open on GitHub", url=issue_url))
    view.add_item(CloseFiledButton(filing_id))
    return view


class WatchManager:
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        self._inflight: set[tuple[int, int]] = set()
        self._labels: dict[str, tuple[float, dict[str, str]]] = {}
        self._issues: dict[tuple[str, int], tuple[str | None, dict[str, Any]]] = {}

    # --------------------------------------------------------------- triggers
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id or not message.role_mentions:
            return
        mentioned = {r.id: r for r in message.role_mentions}
        channel_ids = {message.channel.id, getattr(message.channel, "parent_id", None)}
        watches = [w for w in await self.bot.db.list_watches(message.guild.id, "role")
                   if w.role_id in mentioned and (w.channel_id is None or w.channel_id in channel_ids)]
        if not watches:
            return
        if isinstance(message.channel, discord.Thread) and message.id == message.channel.id and \
                any(w.channel_id == message.channel.parent_id
                    for w in await self.bot.db.list_watches(message.guild.id, "forum")):
            return  # the first message of a watched forum post: on_thread_create files it
        source = await self._source_message(message)
        role_names = [mentioned[w.role_id].name for w in watches]
        for watch in self._one_per_repo(watches):
            await self.file(watch, trigger=message, source=source, role_names=role_names)

    async def on_thread_create(self, thread: discord.Thread) -> None:
        parent = thread.parent
        if not isinstance(parent, discord.ForumChannel):
            return
        watches = [w for w in await self.bot.db.list_watches(thread.guild.id, "forum")
                   if w.channel_id == parent.id]
        if not watches:
            return
        starter = await self._starter_message(thread)
        if starter is None or starter.author.bot:
            return
        for watch in self._one_per_repo(watches):
            repo = await self.bot.db.get_repo(watch.guild_id, watch.owner, watch.name)
            tag_labels = await self._tag_labels(repo, thread.applied_tags) if repo else []
            await self.file(watch, trigger=starter, source=starter, role_names=[], thread=thread,
                            title=thread.name, extra_labels=tag_labels)

    @staticmethod
    def _one_per_repo(watches: list[Watch]) -> list[Watch]:
        seen, unique = set(), []
        for w in watches:
            if w.repo_id not in seen:
                seen.add(w.repo_id)
                unique.append(w)
        return unique

    async def _source_message(self, message: discord.Message) -> discord.Message:
        """A reply files the message it replies to (with its author as the reporter)."""
        ref = message.reference
        if ref is None or ref.message_id is None or ref.channel_id not in (None, message.channel.id):
            return message
        resolved = ref.resolved
        if not isinstance(resolved, discord.Message):
            try:
                resolved = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return message
        return message if resolved.author.bot else resolved

    async def _starter_message(self, thread: discord.Thread) -> discord.Message | None:
        if thread.starter_message:
            return thread.starter_message
        for attempt in range(STARTER_RETRIES):  # the post's first message can lag behind the thread
            try:
                return await thread.fetch_message(thread.id)
            except discord.NotFound:
                await asyncio.sleep(1.5 * (attempt + 1))
            except discord.HTTPException as exc:
                log.warning("Couldn't read forum post %s: %s", thread.id, exc)
                return None
        return None

    async def _tag_labels(self, repo: Repo, tags: Iterable[discord.ForumTag]) -> list[str]:
        """Forum tags that match one of the repo's GitHub labels (ignoring case)."""
        names = [t.name for t in tags]
        if not names:
            return []
        cached = self._labels.get(repo.key)
        if cached is None or cached[0] < time.time():
            try:
                labels = await self.bot.github.list_labels(repo.owner, repo.name)
            except GitHubError as exc:
                log.warning("Couldn't load labels for %s: %s", repo.full_name, exc)
                return []
            cached = (time.time() + LABEL_CACHE_SECONDS,
                      {lbl["name"].lower(): lbl["name"] for lbl in labels})
            self._labels[repo.key] = cached
        return [cached[1][n.strip().lower()] for n in names if n.strip().lower() in cached[1]]

    # ----------------------------------------------------------------- filing
    async def file(self, watch: Watch, *, trigger: discord.Message, source: discord.Message,
                   role_names: list[str], thread: discord.Thread | None = None,
                   title: str | None = None, extra_labels: Iterable[str] = ()) -> Filing | None:
        """Create a GitHub issue from ``source``; ``trigger`` is the message that asked for it."""
        bot, guild = self.bot, trigger.guild
        assert guild is not None
        repo = await bot.db.get_repo(watch.guild_id, watch.owner, watch.name)
        if repo is None:
            return None
        key = (source.id, repo.id)
        if key in self._inflight:
            return None
        existing = await bot.db.find_filing(source.id, repo.id)
        if existing:
            await self._notice(trigger, f"Already filed as [{existing.full_name}#{existing.issue_number}]"
                                        f"(<{existing.issue_url}>).")
            return None

        filer = trigger.author
        allowed, reason = await member_can_create_issue(bot, guild, filer, repo)
        if not allowed:
            await self._notice(trigger, f"🚫 {reason}")
            return None
        admin = await member_is_admin(bot, guild, filer)
        issues_cog = bot.get_cog("Issues")
        # Forum posts are deliberate (and Discord's slowmode covers them); chat mentions aren't.
        if thread is None and not admin and issues_cog is not None:
            remaining = issues_cog.cooldown_remaining(guild.id, filer.id)  # type: ignore[attr-defined]
            if remaining > 0:
                await self._notice(trigger, f"⏳ Slow down! You can file another issue in {int(remaining) + 1}s.")
                return None

        text = strip_role_mentions(source.clean_content, role_names)
        note = strip_role_mentions(trigger.clean_content, role_names) if source is not trigger else ""
        if not text and note:
            text, note = note, ""
        if not text and not source.attachments and thread is None:
            return None  # just a bare mention: nothing to file
        title = (title or "").strip() or title_from(text) or f"Report from {source.author.display_name}"
        body = text
        if note:
            body += f"\n\n**Note from {filer.display_name}:** {note}"
        if not admin:
            body = neutralize_mentions(body)
        full_body = format_issue_body(
            body, reporter=source.author.display_name, reporter_username=source.author.name,
            guild=guild.name, channel=getattr(trigger.channel, "name", None),
            message_url=source.jump_url,
            attachments=[(a.filename, a.url) for a in source.attachments],
            filed_by=filer.display_name if filer.id != source.author.id else None,
        )
        labels = list(dict.fromkeys(lbl for lbl in [watch.label, *extra_labels] if lbl))

        self._inflight.add(key)
        try:
            issue = await bot.github.create_issue(repo.owner, repo.name, truncate(title, 256),
                                                  full_body, labels)
        except GitHubError as exc:
            error = github_issue_error(bot, repo, exc)
            await self._notice(trigger, f"❌ Couldn't file this on GitHub: {error or exc}")
            return None
        finally:
            self._inflight.discard(key)

        filing = await bot.db.add_filing(
            message_id=source.id, repo=repo, channel_id=trigger.channel.id,
            thread_id=thread.id if thread else None, issue=issue, reporter_id=source.author.id,
            filer_id=filer.id, close_thread=bool(thread is not None and watch.close_threads),
        )
        if issues_cog is not None:
            issues_cog.issue_created(repo, guild.id, filer.id, issue)  # type: ignore[attr-defined]

        embed = discord.Embed(title=truncate(f"📝 Filed as {repo.full_name}#{issue['number']}", 256),
                              url=issue["html_url"], description=truncate(issue["title"], 300),
                              color=GREEN)
        footer = f"Reported by {source.author.display_name}"
        if filer.id != source.author.id:
            footer += f" · filed by {filer.display_name}"
        embed.set_footer(text=footer)
        reply = await self._reply(trigger, embed=embed, view=filing_view(filing.id, issue["html_url"]))
        if reply is not None:
            await bot.db.set_filing_reply(filing.id, reply.id)
        try:
            await trigger.add_reaction("✅")
        except discord.HTTPException:
            pass
        return filing

    async def _reply(self, message: discord.Message, content: str | None = None, **kwargs: Any):
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        try:
            return await message.reply(content, mention_author=False, **kwargs)
        except discord.HTTPException:
            try:  # e.g. missing Read Message History: post without the reply link
                return await message.channel.send(content, **kwargs)
            except discord.HTTPException as exc:
                log.warning("Couldn't post in channel %s: %s", message.channel.id, exc)
                return None

    async def _notice(self, message: discord.Message, text: str) -> None:
        """A short reply that removes itself, so failed attempts don't clutter the channel."""
        await self._reply(message, text, delete_after=NOTICE_SECONDS)

    # ------------------------------------------------------------ undo button
    async def handle_close(self, interaction: discord.Interaction, filing_id: int) -> None:
        bot = self.bot
        filing = await bot.db.get_filing(filing_id)
        if filing is None or filing.guild_id != interaction.guild_id:
            raise UserError("This report is no longer tracked.")
        if filing.state != "open":
            await interaction.response.send_message("That issue is already closed.", ephemeral=True)
            return
        user = interaction.user
        if user.id not in (filing.reporter_id, filing.filer_id) and \
                not await member_is_admin(bot, interaction.guild, user):
            raise UserError("Only the person who reported it, the person who filed it, or a bot "
                            "admin can close it.")
        await interaction.response.defer()
        try:
            await bot.github.comment_issue(filing.owner, filing.name, filing.issue_number,
                                           neutralize_mentions(
                                               f"Closed from Discord by **{user.display_name}** "
                                               f"(`{user.name}`): not an issue."))
        except GitHubError as exc:
            log.warning("Couldn't comment on %s#%s: %s", filing.full_name, filing.issue_number, exc)
        try:
            await bot.github.close_issue(filing.owner, filing.name, filing.issue_number)
        except GitHubError as exc:
            await interaction.followup.send(f"❌ Couldn't close the issue on GitHub: {exc}", ephemeral=True)
            return
        await bot.db.set_filing_state(filing.id, "not_planned")
        message = interaction.message
        if message is not None and message.embeds:
            embed = message.embeds[0]
            embed.color = discord.Color.dark_grey()
            embed.description = (embed.description or "") + f"\n\n✖️ Closed as not an issue by {user.mention}"
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Open on GitHub", url=filing.issue_url))
            await interaction.edit_original_response(embed=embed, view=view,
                                                     allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------ forum post closing
    async def sync_threads(self) -> None:
        """Close and tag forum posts whose GitHub issue was closed."""
        for filing in await self.bot.db.open_thread_filings():
            try:
                await self._sync_thread(filing)
            except RateLimited:
                return
            except Exception:
                log.exception("Failed to sync forum post %s", filing.thread_id)

    async def _sync_thread(self, filing: Filing) -> None:
        key = (filing.full_name.lower(), filing.issue_number)
        etag, cached = self._issues.get(key, (None, None))
        issue, new_etag = await self.bot.github.get_issue(filing.owner, filing.name,
                                                          filing.issue_number, etag if cached else None)
        if issue is None:
            issue = cached
        else:
            self._issues[key] = (new_etag, issue)
        if not issue or issue.get("state") != "closed":
            return

        try:
            thread = self.bot.get_channel(filing.thread_id) or \
                await self.bot.fetch_channel(filing.thread_id)  # type: ignore[arg-type]
        except (discord.NotFound, discord.Forbidden):
            await self.bot.db.set_filing_state(filing.id, "gone")
            return
        if not isinstance(thread, discord.Thread):
            await self.bot.db.set_filing_state(filing.id, "gone")
            return

        reason = issue.get("state_reason")
        how = {"completed": "as completed ✅", "not_planned": "as not planned"}.get(reason, "")
        text = f"The GitHub issue [{filing.full_name}#{filing.issue_number}](<{filing.issue_url}>) was closed {how}".strip() + "."
        try:
            if thread.archived:
                await thread.edit(archived=False)
            await thread.send(text, allowed_mentions=discord.AllowedMentions.none())
            forum = thread.parent
            tag = solved_tag(forum) if isinstance(forum, discord.ForumChannel) else None
            if reason == "completed" and tag and tag not in thread.applied_tags and len(thread.applied_tags) < 5:
                await thread.add_tags(tag, reason="GitHub issue closed")
            await thread.edit(archived=True, locked=True, reason="GitHub issue closed")
        except discord.HTTPException as exc:
            log.warning("Couldn't close forum post %s (needs Manage Threads?): %s", thread.id, exc)
        await self.bot.db.set_filing_state(filing.id, "closed")
