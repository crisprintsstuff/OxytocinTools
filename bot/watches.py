"""Turn Discord messages into GitHub issues: role mentions and forum channel posts."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Iterable

import discord

from .checks import UserError, member_can_create_issue, member_is_admin, resolve_member
from .cogs.issues import (
    find_similar_issues,
    format_attachments,
    format_issue_body,
    format_similar_field,
    github_issue_error,
    neutralize_mentions,
    process_attachments,
    title_from,
)
from .embeds import BLUE, GREEN, PURPLE, issue_detail_embed, truncate
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
SYNC_MARKER = "<!-- discord-sync -->"
AUTOLINK_COOLDOWN = 120
MAX_AUTOLINKS_PER_MESSAGE = 3
MAX_MIRRORED_COMMENTS_PER_CYCLE = 5

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"https?://\S+")
_DISCORD_TAG_RE = re.compile(r"<(?:@!?|@&|#|a?:\w+:)\d+>")
_ISSUE_REF_RE = re.compile(
    r"(?<![\w/])(?:(?P<repo>[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?))?#(?P<num>[1-9][0-9]{0,5})\b"
)


def extract_issue_refs(text: str) -> list[tuple[str | None, int]]:
    """Extract up to MAX_AUTOLINKS_PER_MESSAGE unique (repo_hint, number) pairs from text."""
    cleaned = _CODE_BLOCK_RE.sub(" ", text or "")
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _DISCORD_TAG_RE.sub(" ", cleaned)
    refs: list[tuple[str | None, int]] = []
    seen: set[tuple[str | None, int]] = set()
    for match in _ISSUE_REF_RE.finditer(cleaned):
        hint = match.group("repo")
        key = (hint.lower() if hint else None, int(match.group("num")))
        if key not in seen:
            seen.add(key)
            refs.append((hint, key[1]))
            if len(refs) >= MAX_AUTOLINKS_PER_MESSAGE:
                break
    return refs


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


class CompleteFiledButton(discord.ui.DynamicItem[discord.ui.Button], template=r"filed:complete:(?P<id>[0-9]+)"):
    def __init__(self, filing_id: int):
        super().__init__(discord.ui.Button(label="Complete", emoji="✅",
                                           style=discord.ButtonStyle.success,
                                           custom_id=f"filed:complete:{filing_id}"))
        self.filing_id = filing_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        try:
            await bot.watches.handle_complete(interaction, self.filing_id)
        except Exception as exc:
            await bot.on_app_command_error(interaction, exc)  # type: ignore[arg-type]


class LabelFiledButton(discord.ui.DynamicItem[discord.ui.Button], template=r"filed:label:(?P<id>[0-9]+)"):
    def __init__(self, filing_id: int):
        super().__init__(discord.ui.Button(label="Labels", emoji="🏷️",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"filed:label:{filing_id}"))
        self.filing_id = filing_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        try:
            await bot.watches.handle_label_prompt(interaction, self.filing_id)
        except Exception as exc:
            await bot.on_app_command_error(interaction, exc)  # type: ignore[arg-type]


class CommentFiledButton(discord.ui.DynamicItem[discord.ui.Button], template=r"filed:comment:(?P<id>[0-9]+)"):
    def __init__(self, filing_id: int):
        super().__init__(discord.ui.Button(label="Comment", emoji="💬",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"filed:comment:{filing_id}"))
        self.filing_id = filing_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        try:
            await bot.watches.handle_comment_prompt(interaction, self.filing_id)
        except Exception as exc:
            await bot.on_app_command_error(interaction, exc)  # type: ignore[arg-type]


class TriageLabelModal(discord.ui.Modal):
    def __init__(self, manager: "WatchManager", filing: Filing):
        super().__init__(title=truncate(f"Labels: {filing.full_name}#{filing.issue_number}", 45))
        self.manager, self.filing = manager, filing
        self.add_labels = discord.ui.TextInput(
            label="Add labels (comma-separated)", required=False, max_length=200,
            placeholder="bug, high-priority",
        )
        self.remove_labels = discord.ui.TextInput(
            label="Remove labels (comma-separated)", required=False, max_length=200,
            placeholder="triage, duplicate",
        )
        self.add_item(self.add_labels)
        self.add_item(self.remove_labels)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.manager.handle_label_submit(
            interaction, self.filing, self.add_labels.value, self.remove_labels.value
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.client.on_app_command_error(interaction, error)  # type: ignore[attr-defined]


class TriageCommentModal(discord.ui.Modal):
    def __init__(self, manager: "WatchManager", filing: Filing):
        super().__init__(title=truncate(f"Comment on #{filing.issue_number}", 45))
        self.manager, self.filing = manager, filing
        self.comment = discord.ui.TextInput(
            label="Comment", style=discord.TextStyle.paragraph, max_length=2000,
            placeholder="Write a comment to post on GitHub...",
        )
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.manager.handle_comment_submit(interaction, self.filing, self.comment.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.client.on_app_command_error(interaction, error)  # type: ignore[attr-defined]


def filing_view(filing_id: int, issue_url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Open on GitHub", url=issue_url))
    view.add_item(CloseFiledButton(filing_id))
    view.add_item(CompleteFiledButton(filing_id))
    view.add_item(LabelFiledButton(filing_id))
    view.add_item(CommentFiledButton(filing_id))
    return view


class WatchManager:
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        self._inflight: set[tuple[int, int]] = set()
        self._labels: dict[str, tuple[float, dict[str, str]]] = {}
        self._issues: dict[tuple[str, int], tuple[str | None, dict[str, Any]]] = {}
        self._comments: dict[tuple[str, int], tuple[str | None, list[dict[str, Any]]]] = {}
        self._autolink_seen: dict[tuple[int, str, int], float] = {}

    # --------------------------------------------------------------- triggers
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        if message.role_mentions:
            mentioned = {r.id: r for r in message.role_mentions}
            channel_ids = {message.channel.id, getattr(message.channel, "parent_id", None)}
            watches = [w for w in await self.bot.db.list_watches(message.guild.id, "role")
                       if w.role_id in mentioned and (w.channel_id is None or w.channel_id in channel_ids)]
            if watches:
                if isinstance(message.channel, discord.Thread) and message.id == message.channel.id and \
                        any(w.channel_id == message.channel.parent_id
                            for w in await self.bot.db.list_watches(message.guild.id, "forum")):
                    return  # the first message of a watched forum post: on_thread_create files it
                source = await self._source_message(message)
                role_names = [mentioned[w.role_id].name for w in watches]
                for watch in self._one_per_repo(watches):
                    await self.file(watch, trigger=message, source=source, role_names=role_names)
                return

        synced = await self._maybe_sync_discord_comment(message)
        await self._maybe_autolink(message, skip_ref=synced)

    async def _maybe_sync_discord_comment(self, message: discord.Message) -> tuple[str, int] | None:
        assert message.guild is not None
        if not await self.bot.db.get_comment_sync_enabled(message.guild.id):
            return None
        filing: Filing | None = None
        if isinstance(message.channel, discord.Thread) and message.id != message.channel.id:
            filing = await self.bot.db.find_filing_by_thread(message.channel.id)
        elif message.reference and message.reference.message_id \
                and message.reference.channel_id in (None, message.channel.id):
            filing = await self.bot.db.find_filing_by_reply(message.reference.message_id)
        if filing is None or filing.state != "open":
            return None

        text = (message.clean_content or "").strip()
        raw_attachments = list(getattr(message, "attachments", []))
        if not text and not raw_attachments:
            return None
        repo = await self.bot.db.get_repo(filing.guild_id, filing.owner, filing.name)
        if repo is None:
            return None
        if message.author.id not in (filing.reporter_id, filing.filer_id):
            allowed, _ = await member_can_create_issue(self.bot, message.guild, message.author, repo)
            if not allowed:
                return None

        if not await member_is_admin(self.bot, message.guild, message.author):
            text = neutralize_mentions(text)
        parts = [text] if text else []
        if raw_attachments:
            uploaded = await process_attachments(self.bot, repo, raw_attachments)
            parts.append(format_attachments(uploaded))
        footer = (
            f"---\n<sub>Comment from Discord by **{message.author.display_name}** "
            f"(`{message.author.name}`) · [Jump to message]({message.jump_url})</sub>\n"
            f"{SYNC_MARKER}"
        )
        parts.append(neutralize_mentions(footer))
        try:
            await self.bot.github.comment_issue(
                filing.owner, filing.name, filing.issue_number, "\n\n".join(parts)
            )
        except GitHubError as exc:
            log.warning("Couldn't sync comment to %s#%s: %s",
                        filing.full_name, filing.issue_number, exc)
            return None
        try:
            await message.add_reaction("💬")
        except discord.HTTPException:
            pass
        return (repo.key, filing.issue_number)

    async def _resolve_ref_repo(self, guild_id: int, channel: Any,
                                repos: list[Repo], hint: str | None) -> Repo | None:
        if hint is not None:
            low = hint.lower()
            if "/" in low:
                return next((r for r in repos if r.key == low), None)
            matches = [r for r in repos if r.name.lower() == low]
            return matches[0] if len(matches) == 1 else None
        if len(repos) == 1:
            return repos[0]
        channel_ids = {cid for cid in (getattr(channel, "id", None), getattr(channel, "parent_id", None))
                       if cid is not None}
        if not channel_ids:
            return None
        by_id = {r.id: r for r in repos}
        linked_repo_ids: set[int] = set()
        for item in (
            await self.bot.db.list_routes(guild_id),
            await self.bot.db.list_dashboards(guild_id),
            await self.bot.db.list_watches(guild_id),
            await self.bot.db.list_pr_trackers(guild_id),
        ):
            for entry in item:
                if entry.channel_id in channel_ids and entry.repo_id in by_id:
                    linked_repo_ids.add(entry.repo_id)
        if len(linked_repo_ids) == 1:
            return by_id[next(iter(linked_repo_ids))]
        return None

    async def _maybe_autolink(self, message: discord.Message, *,
                              skip_ref: tuple[str, int] | None = None) -> None:
        assert message.guild is not None
        if not hasattr(message.channel, "send"):
            return
        if not await self.bot.db.get_autolink_enabled(message.guild.id):
            return
        refs = extract_issue_refs(message.clean_content or "")
        if not refs:
            return
        repos = await self.bot.db.list_repos(message.guild.id)
        if not repos or not hasattr(self.bot.github, "get_issue"):
            return
        now = time.monotonic()
        embeds: list[discord.Embed] = []
        for hint, number in refs:
            repo = await self._resolve_ref_repo(message.guild.id, message.channel, repos, hint)
            if repo is None or (repo.key, number) == skip_ref:
                continue
            cd_key = (message.channel.id, repo.key, number)
            last = self._autolink_seen.get(cd_key)
            if last is not None and now - last < AUTOLINK_COOLDOWN:
                continue
            try:
                issue, _ = await self.bot.github.get_issue(repo.owner, repo.name, number)
            except GitHubError:
                continue
            if issue is None:
                continue
            self._autolink_seen[cd_key] = now
            embeds.append(issue_detail_embed(repo.full_name, issue, compact=True))
        if embeds:
            try:
                await message.channel.send(
                    embeds=embeds, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass

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

        filer = await resolve_member(guild, trigger.author) or trigger.author
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

        if source is trigger:
            raw_attachments = list(getattr(source, "attachments", []))
        else:
            seen_att_ids = set()
            raw_attachments = []
            for att in [*getattr(source, "attachments", []), *getattr(trigger, "attachments", [])]:
                att_id = getattr(att, "id", None)
                if att_id is not None:
                    if att_id in seen_att_ids:
                        continue
                    seen_att_ids.add(att_id)
                elif att in raw_attachments:
                    continue
                raw_attachments.append(att)

        text = strip_role_mentions(source.clean_content, role_names)
        note = strip_role_mentions(trigger.clean_content, role_names) if source is not trigger else ""
        if not text and note:
            text, note = note, ""
        if not text and not raw_attachments and thread is None:
            return None  # just a bare mention: nothing to file
        title = (title or "").strip() or title_from(text)
        if not title:
            if raw_attachments:
                first_name = getattr(raw_attachments[0], "filename", None)
                if not first_name and isinstance(raw_attachments[0], tuple):
                    first_name = raw_attachments[0][0]
                title = f"{first_name or 'Attachment'} from {source.author.display_name}"
            else:
                title = f"Report from {source.author.display_name}"
        body = text
        if note:
            body += f"\n\n**Note from {filer.display_name}:** {note}"
        if not admin:
            body = neutralize_mentions(body)
        attachments = await process_attachments(bot, repo, raw_attachments) if raw_attachments else None
        full_body = format_issue_body(
            body, reporter=source.author.display_name, reporter_username=source.author.name,
            guild=guild.name, channel=getattr(trigger.channel, "name", None),
            message_url=source.jump_url,
            attachments=attachments,
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
        similar = await find_similar_issues(
            bot.github, repo.owner, repo.name, title, exclude_number=issue["number"]
        )
        if similar:
            embed.add_field(
                name="🔎 Possible related open issues",
                value=format_similar_field(similar),
                inline=False,
            )
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

    async def _require_admin_filing(self, interaction: discord.Interaction,
                                    filing_id: int, action_label: str) -> Filing:
        filing = await self.bot.db.get_filing(filing_id)
        if filing is None or filing.guild_id != interaction.guild_id:
            raise UserError("This report is no longer tracked.")
        if not await member_is_admin(self.bot, interaction.guild, interaction.user):
            raise UserError(f"Only bot admins can {action_label} from Discord.")
        return filing

    async def handle_complete(self, interaction: discord.Interaction, filing_id: int) -> None:
        filing = await self._require_admin_filing(interaction, filing_id, "mark an issue completed")
        if filing.state != "open":
            await interaction.response.send_message("That issue is already closed.", ephemeral=True)
            return
        user = interaction.user
        await interaction.response.defer()
        try:
            await self.bot.github.comment_issue(
                filing.owner, filing.name, filing.issue_number,
                neutralize_mentions(
                    f"Closed from Discord by **{user.display_name}** (`{user.name}`): completed."
                ) + f"\n{SYNC_MARKER}",
            )
        except GitHubError as exc:
            log.warning("Couldn't comment on %s#%s: %s", filing.full_name, filing.issue_number, exc)
        try:
            await self.bot.github.close_issue(
                filing.owner, filing.name, filing.issue_number, reason="completed"
            )
        except GitHubError as exc:
            await interaction.followup.send(f"❌ Couldn't close the issue on GitHub: {exc}", ephemeral=True)
            return
        await self.bot.db.set_filing_state(filing.id, "closed")
        message = interaction.message
        if message is not None and message.embeds:
            embed = message.embeds[0]
            embed.color = PURPLE
            embed.description = (embed.description or "") + f"\n\n✅ Closed as completed by {user.mention}"
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Open on GitHub", url=filing.issue_url))
            await interaction.edit_original_response(embed=embed, view=view,
                                                     allowed_mentions=discord.AllowedMentions.none())
        if filing.thread_id and filing.close_thread:
            thread = await self._fetch_thread(filing.thread_id)
            if thread is not None:
                try:
                    if thread.archived:
                        await thread.edit(archived=False)
                    forum = thread.parent
                    tag = solved_tag(forum) if isinstance(forum, discord.ForumChannel) else None
                    if tag and tag not in thread.applied_tags and len(thread.applied_tags) < 5:
                        await thread.add_tags(tag, reason="GitHub issue closed")
                    await thread.edit(archived=True, locked=True, reason="GitHub issue closed")
                except discord.HTTPException:
                    pass

    async def handle_label_prompt(self, interaction: discord.Interaction, filing_id: int) -> None:
        filing = await self._require_admin_filing(interaction, filing_id, "edit labels")
        await interaction.response.send_modal(TriageLabelModal(self, filing))

    async def handle_label_submit(self, interaction: discord.Interaction, filing: Filing,
                                  add_raw: str, remove_raw: str) -> None:
        to_add = [s.strip() for s in add_raw.split(",") if s.strip()][:10]
        to_remove = [s.strip() for s in remove_raw.split(",") if s.strip()][:10]
        if not to_add and not to_remove:
            raise UserError("Enter at least one label to add or remove.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if to_add:
                await self.bot.github.add_issue_labels(
                    filing.owner, filing.name, filing.issue_number, to_add
                )
            for lbl in to_remove:
                await self.bot.github.remove_issue_label(
                    filing.owner, filing.name, filing.issue_number, lbl
                )
        except GitHubError as exc:
            raise UserError(f"Couldn't update labels on GitHub: {exc}") from exc
        summary = []
        if to_add:
            summary.append("added " + ", ".join(f"`{l}`" for l in to_add))
        if to_remove:
            summary.append("removed " + ", ".join(f"`{l}`" for l in to_remove))
        await interaction.followup.send(
            f"🏷️ Updated **{filing.full_name}#{filing.issue_number}**: {'; '.join(summary)}.",
            ephemeral=True,
        )

    async def handle_comment_prompt(self, interaction: discord.Interaction, filing_id: int) -> None:
        filing = await self._require_admin_filing(interaction, filing_id, "post triage comments")
        await interaction.response.send_modal(TriageCommentModal(self, filing))

    async def handle_comment_submit(self, interaction: discord.Interaction, filing: Filing,
                                    text: str) -> None:
        body = text.strip()
        if not body:
            raise UserError("Comment cannot be empty.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        full = (
            f"{body}\n\n---\n"
            f"<sub>Triage comment from Discord by **{user.display_name}** (`{user.name}`)</sub>\n"
            f"{SYNC_MARKER}"
        )
        try:
            await self.bot.github.comment_issue(
                filing.owner, filing.name, filing.issue_number, full
            )
        except GitHubError as exc:
            raise UserError(f"Couldn't post comment on GitHub: {exc}") from exc
        await interaction.followup.send(
            f"💬 Posted comment on [{filing.full_name}#{filing.issue_number}](<{filing.issue_url}>).",
            ephemeral=True,
        )

    # ------------------------------------------------------ filing lifecycle & sync
    async def sync_threads(self) -> None:
        """Mirror GitHub comments to forum posts and handle closed issues for all watches."""
        ready = getattr(self.bot, "is_ready", lambda: False)()
        has_get_guild = hasattr(self.bot, "get_guild")
        for filing in await self.bot.db.open_filings():
            if ready and has_get_guild and self.bot.get_guild(filing.guild_id) is None:
                continue
            try:
                await self._sync_thread(filing)
            except RateLimited:
                return
            except Exception:
                log.exception("Failed to sync filing %s (issue %s#%s)",
                              filing.id, filing.full_name, filing.issue_number)

    async def _fetch_thread(self, thread_id: int) -> discord.Thread | None:
        thread = self.bot.get_channel(thread_id)
        if thread is None and hasattr(self.bot, "fetch_channel"):
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden):
                return None
        return thread if isinstance(thread, discord.Thread) else None

    async def _sync_github_comments(self, filing: Filing) -> None:
        assert filing.thread_id is not None
        key = (filing.full_name.lower(), filing.issue_number)
        etag, cached = self._comments.get(key, (None, []))
        comments, new_etag = await self.bot.github.list_issue_comments(
            filing.owner, filing.name, filing.issue_number, etag if cached else None
        )
        if comments is None:
            return
        self._comments[key] = (new_etag, comments)
        if not comments:
            return
        max_id = max(c["id"] for c in comments)
        app_slug = getattr(self.bot.github, "app_slug", None)
        bot_login = f"{app_slug}[bot]".lower() if app_slug else None
        new_comments = [
            c for c in sorted(comments, key=lambda x: x["id"])
            if c["id"] > filing.last_comment_id
            and SYNC_MARKER not in (c.get("body") or "")
            and not (bot_login and ((c.get("user") or {}).get("login") or "").lower() == bot_login)
        ]
        if new_comments:
            thread = await self._fetch_thread(filing.thread_id)
            if thread is None:
                return
            try:
                if thread.archived:
                    await thread.edit(archived=False)
                for comment in new_comments[-MAX_MIRRORED_COMMENTS_PER_CYCLE:]:
                    user = comment.get("user") or {}
                    embed = discord.Embed(
                        description=truncate(comment.get("body"), 1500) or "*Empty comment.*",
                        color=BLUE,
                        url=comment.get("html_url"),
                    )
                    embed.set_author(
                        name=user.get("login", "unknown"),
                        url=user.get("html_url"),
                        icon_url=user.get("avatar_url"),
                    )
                    embed.set_footer(text=f"{filing.full_name}#{filing.issue_number} · GitHub comment")
                    await thread.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                log.warning("Couldn't mirror comments to thread %s: %s", thread.id, exc)
                return
        if max_id > filing.last_comment_id:
            await self.bot.db.set_filing_last_comment_id(filing.id, max_id)
            filing.last_comment_id = max_id

    @staticmethod
    def _close_style(reason: str | None) -> tuple[str, str, discord.Color]:
        if reason == "completed":
            return "as completed ✅", "✅ Closed as completed", PURPLE
        if reason == "not_planned":
            return "as not planned", "✖️ Closed as not planned", discord.Color.dark_grey()
        return "", "🔒 Closed", PURPLE

    async def _mark_reply_closed(self, channel: Any, filing: Filing,
                                 badge: str, color: discord.Color) -> discord.Message | None:
        if not filing.reply_message_id or not hasattr(channel, "fetch_message"):
            return None
        try:
            reply_msg = await channel.fetch_message(filing.reply_message_id)
        except (discord.HTTPException, AttributeError):
            return None
        if getattr(reply_msg, "embeds", None):
            try:
                embed = reply_msg.embeds[0]
                embed.color = color
                embed.description = (embed.description or "") + f"\n\n{badge}"
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label="Open on GitHub", url=filing.issue_url))
                await reply_msg.edit(embed=embed, view=view,
                                     allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        return reply_msg

    async def _sync_thread(self, filing: Filing) -> None:
        if filing.thread_id is not None and hasattr(self.bot.github, "list_issue_comments") \
                and await self.bot.db.get_comment_sync_enabled(filing.guild_id):
            await self._sync_github_comments(filing)

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

        reason = issue.get("state_reason")
        how, badge, color = self._close_style(reason)

        if filing.thread_id is None:
            try:
                channel = self.bot.get_channel(filing.channel_id)
                if channel is None and hasattr(self.bot, "fetch_channel"):
                    channel = await self.bot.fetch_channel(filing.channel_id)
            except (discord.NotFound, discord.Forbidden):
                await self.bot.db.set_filing_state(filing.id, "gone")
                return
            if channel is None or not hasattr(channel, "send"):
                await self.bot.db.set_filing_state(filing.id, "gone")
                return
            reply_msg = await self._mark_reply_closed(channel, filing, badge, color)
            text = (f"<@{filing.reporter_id}> Your issue "
                    f"[{filing.full_name}#{filing.issue_number}](<{filing.issue_url}>) "
                    f"was closed {how}").strip() + "."
            allowed = discord.AllowedMentions(everyone=False, roles=False,
                                              users=[discord.Object(id=filing.reporter_id)])
            try:
                if reply_msg is not None and hasattr(reply_msg, "reply"):
                    await reply_msg.reply(text, allowed_mentions=allowed)
                else:
                    await channel.send(text, allowed_mentions=allowed)
            except discord.HTTPException as exc:
                log.warning("Couldn't post close notice for filing %s: %s", filing.id, exc)
            await self.bot.db.set_filing_state(filing.id, "closed")
            return

        thread = await self._fetch_thread(filing.thread_id)
        if thread is None:
            await self.bot.db.set_filing_state(filing.id, "gone")
            return

        await self._mark_reply_closed(thread, filing, badge, color)
        if filing.close_thread:
            text = (f"The GitHub issue [{filing.full_name}#{filing.issue_number}](<{filing.issue_url}>) "
                    f"was closed {how}").strip() + "."
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
