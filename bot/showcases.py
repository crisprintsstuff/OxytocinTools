"""Live feature & release showcases synced in-place from GitHub files or releases."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from .checks import UserError
from .embeds import BLUE, _COMMENT_RE, _ts, truncate
from .github import GitHubError
from .models import Showcase

if TYPE_CHECKING:
    from .main import TrackerBot

log = logging.getLogger(__name__)

MANUAL_REFRESH_COOLDOWN = 15
CHUNK_LIMIT = 3800
MAX_MESSAGES = 5
COLOR = BLUE

_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_DETAILS_TAG_RE = re.compile(r"</?(?:details|div|span|p|blockquote)\b[^>]*>", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"<summary[^>]*>(.*?)</summary>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def clean_markdown(text: str | None) -> str:
    """Normalize GitHub markdown for clean display inside Discord embeds."""
    if not text:
        return ""
    text = _COMMENT_RE.sub("", text).replace("\r\n", "\n")
    text = _BR_RE.sub("\n", text)
    text = _SUMMARY_RE.sub(r"\n**\1**\n", text)
    text = _DETAILS_TAG_RE.sub("", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _normalize_heading(raw: str) -> str:
    plain = _MD_LINK_RE.sub(r"\1", raw).strip()
    plain = plain.strip("*_`~ ")
    return plain


def _heading_matches(raw: str, targets: set[str]) -> bool:
    plain = _normalize_heading(raw).lower()
    if plain in targets:
        return True
    stripped = re.sub(r"^[^\w]+", "", plain).strip()
    return bool(stripped and stripped in targets)


def extract_section(markdown: str, headings: str | None) -> str | None:
    """Extract matching ATX heading section(s) from markdown.

    ``headings`` is a comma-separated list of heading titles (case-insensitive).
    Returns ``markdown`` unchanged when ``headings`` is None/blank, or ``None``
    when no requested heading appears.
    """
    if not headings or not headings.strip():
        return markdown
    targets = {_normalize_heading(h).lower() for h in headings.split(",") if h.strip()}
    if not targets:
        return markdown

    sections: list[str] = []
    capture_level: int | None = None
    buf: list[str] = []
    in_fence = False

    def _flush() -> None:
        nonlocal capture_level, buf
        if capture_level is not None:
            body = "\n".join(buf).strip()
            if body:
                sections.append(body)
        capture_level = None
        buf = []

    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if capture_level is not None:
                buf.append(line)
            continue
        if not in_fence:
            match = _ATX_RE.match(line)
            if match:
                level = len(match.group(1))
                title = match.group(2)
                if capture_level is not None and level <= capture_level:
                    _flush()
                if _heading_matches(title, targets):
                    if capture_level is None:
                        capture_level = level
                        buf = []
                        continue
                if capture_level is not None:
                    buf.append(line)
                continue
        if capture_level is not None:
            buf.append(line)

    _flush()
    return "\n\n".join(sections) if sections else None


def split_markdown_chunks(text: str, *, limit: int = CHUNK_LIMIT,
                          max_chunks: int = MAX_MESSAGES) -> tuple[list[str], bool]:
    """Split markdown cleanly into ``<= max_chunks`` chunks each ``<= limit`` chars."""
    text = text.strip()
    if not text:
        return [""], False

    units: list[str] = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            units.append(para)
        else:
            for line in para.splitlines():
                if len(line) <= limit:
                    units.append(line)
                else:
                    for i in range(0, len(line), limit):
                        units.append(line[i:i + limit])

    chunks: list[str] = []
    current = ""
    for unit in units:
        sep = "\n\n" if current else ""
        if len(current) + len(sep) + len(unit) <= limit:
            current += sep + unit
        else:
            if current:
                chunks.append(current)
            current = unit
    if current:
        chunks.append(current)

    truncated = len(chunks) > max_chunks
    if truncated:
        notice = "\n… *(truncated — view the full page on GitHub)*"
        last = chunks[max_chunks - 1]
        if len(last) + len(notice) > limit:
            last = last[:max(0, limit - len(notice) - 1)].rstrip()
        chunks = chunks[:max_chunks - 1] + [last + notice]
    return chunks, truncated


def _build_showcase_embeds(sc: Showcase, body: str, *, title: str, source_url: str,
                           footer: str, interval: int) -> tuple[list[discord.Embed], str]:
    chunks, truncated = split_markdown_chunks(body)
    if truncated:
        link_line = f"\n\n[Read the full source on GitHub →]({source_url})"
        if len(chunks[-1]) + len(link_line) <= 4096:
            chunks[-1] += link_line

    embeds: list[discord.Embed] = []
    for idx, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=truncate(title, 256) if idx == 0 else None,
            url=source_url if idx == 0 else None,
            description=truncate(chunk, 4096),
            color=COLOR,
        )
        embeds.append(embed)
    embeds[-1].set_footer(text=truncate(footer, 2048))

    basis = "\n".join([
        sc.source, title, source_url, str(interval), sc.file_path or "",
        sc.branch or "", sc.section or "", sc.release_kind, str(sc.max_releases),
        *chunks,
    ])
    return embeds, hashlib.sha256(basis.encode("utf-8")).hexdigest()


def render_file_showcase(sc: Showcase, text: str, *, default_branch: str,
                         interval: int) -> tuple[list[discord.Embed], str]:
    """Render embeds for a repository markdown file showcase."""
    cleaned = clean_markdown(text)
    if sc.section:
        extracted = extract_section(cleaned, sc.section)
        body = extracted if extracted else f"*(No `{sc.section}` section found in `{sc.file_path}`.)*"
    else:
        body = cleaned or f"*(`{sc.file_path}` is empty.)*"

    title = sc.title or f"✨ {sc.full_name} — {sc.file_path}"
    source_url = sc.source_url(default_branch)
    minutes = max(1, interval // 60)
    footer = f"Synced from {sc.file_path} · Updates every {minutes} min · Last update"
    return _build_showcase_embeds(sc, body, title=title, source_url=source_url,
                                  footer=footer, interval=interval)


def render_releases_showcase(sc: Showcase, releases: list[dict[str, Any]], *,
                             interval: int) -> tuple[list[discord.Embed], str]:
    """Render embeds for a GitHub releases showcase."""
    filtered: list[dict[str, Any]] = []
    for rel in releases:
        if rel.get("draft"):
            continue
        is_pre = bool(rel.get("prerelease"))
        if sc.release_kind == "stable" and is_pre:
            continue
        if sc.release_kind == "prerelease" and not is_pre:
            continue
        filtered.append(rel)
    selected = filtered[:sc.max_releases]

    blocks: list[str] = []
    for rel in selected:
        tag = rel.get("tag_name") or ""
        name = (rel.get("name") or "").strip()
        heading_label = f"{tag} — {name}" if name and name != tag else (tag or name or "Release")
        rel_url = rel.get("html_url") or sc.source_url()
        pre_badge = " *(pre-release)*" if rel.get("prerelease") else ""
        ts = _ts(rel.get("published_at") or rel.get("created_at"))
        date_suffix = f" · <t:{int(ts.timestamp())}:d>" if ts else ""
        header = f"### [{truncate(heading_label, 140)}]({rel_url}){pre_badge}{date_suffix}"

        cleaned = clean_markdown(rel.get("body"))
        if sc.section:
            sec = extract_section(cleaned, sc.section) if cleaned else None
            if not sec:
                continue
            blocks.append(f"{header}\n{sec}")
        else:
            blocks.append(f"{header}\n{cleaned or '*(No release notes provided.)*'}")

    if blocks:
        body = "\n\n".join(blocks)
    elif not selected:
        body = f"*(No published `{sc.release_kind}` releases found for **{sc.full_name}**.)*"
    else:
        body = f"*(None of the latest {len(selected)} release(s) had a `{sc.section}` section.)*"

    title = sc.title or f"🚀 {sc.full_name} — Releases"
    minutes = max(1, interval // 60)
    footer = f"Synced from GitHub Releases ({sc.release_kind}) · Updates every {minutes} min · Last update"
    return _build_showcase_embeds(sc, body, title=title, source_url=sc.source_url(),
                                  footer=footer, interval=interval)


class RefreshShowcaseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sc:refresh:(?P<id>\d+)",
):
    """Persistent refresh button on showcase boards."""

    def __init__(self, showcase_id: int):
        super().__init__(
            discord.ui.Button(
                label="Refresh",
                emoji="🔄",
                style=discord.ButtonStyle.secondary,
                custom_id=f"sc:refresh:{showcase_id}",
            )
        )
        self.showcase_id = showcase_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button, match: re.Match[str]):  # type: ignore[override]
        return cls(int(match.group("id")))

    async def callback(self, interaction: discord.Interaction) -> None:
        manager = getattr(interaction.client, "showcases", None)
        if manager is None:
            raise UserError("Showcases aren't initialized yet.")
        await manager.handle_refresh(interaction, self.showcase_id)


def showcase_view(sc: Showcase, *, default_branch: str = "main") -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RefreshShowcaseButton(sc.id))
    view.add_item(discord.ui.Button(
        label="View on GitHub",
        style=discord.ButtonStyle.link,
        url=sc.source_url(default_branch),
    ))
    return view


class ShowcaseManager:
    """Fetches, renders, and keeps channel showcase messages edited in place."""

    def __init__(self, bot: TrackerBot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[str | None, Any]] = {}
        self._manual: dict[int, float] = {}

    async def _cached(self, key: str,
                      fetcher: Callable[[str | None], Awaitable[tuple[Any, str | None]]]) -> Any:
        etag, data = self._cache.get(key, (None, None))
        if etag is None and hasattr(self.bot.db, "get_http_cache"):
            row = await self.bot.db.get_http_cache(key)
            if row is not None:
                etag, data = row
                self._cache[key] = (etag, data)
        fresh, new_etag = await fetcher(etag)
        if fresh is not None:
            data = fresh
            self._cache[key] = (new_etag, data)
            if new_etag and hasattr(self.bot.db, "set_http_cache"):
                await self.bot.db.set_http_cache(key, new_etag, json.dumps(data))
        return data

    async def _record_error(self, sc: Showcase, err: str) -> None:
        await self.bot.db.set_showcase_error(sc.id, err)
        sc.last_error = err
        if hasattr(self.bot, "send_alert"):
            await self.bot.send_alert(
                sc.guild_id, f"sc:{sc.id}", f"Showcase #{sc.id} error",
                f"**{sc.describe()}** in <#{sc.channel_id}>\n{err}",
            )

    async def refresh_all(self) -> None:
        active = self.bot._active_guild_ids() if hasattr(self.bot, "_active_guild_ids") else None
        for sc in await self.bot.db.list_showcases():
            if active is not None and sc.guild_id not in active:
                continue
            await self.refresh(sc)

    async def refresh(self, sc: Showcase, *, force: bool = False) -> None:
        """Re-render one showcase, updating or reconciling its channel messages."""
        async with self._lock:
            db = self.bot.db
            repo = await db.get_repo(sc.guild_id, sc.owner, sc.name)
            default_branch = repo.default_branch if repo else "main"
            interval = getattr(getattr(self.bot, "poller", None), "interval", 300)

            try:
                if sc.source == "releases":
                    per_page = max(30, min(100, sc.max_releases * 2 if sc.release_kind != "all" else sc.max_releases))
                    cache_key = f"sc:releases:{sc.full_name.lower()}:{per_page}"
                    releases = await self._cached(
                        cache_key,
                        lambda e: self.bot.github.list_releases(sc.owner, sc.name, per_page=per_page, etag=e),
                    )
                    embeds, digest = render_releases_showcase(sc, releases or [], interval=interval)
                else:
                    ref = sc.branch or default_branch
                    path = sc.file_path or "README.md"
                    cache_key = f"sc:file:{sc.full_name.lower()}:{ref}:{path}"
                    text = await self._cached(
                        cache_key,
                        lambda e: self.bot.github.get_file_content(sc.owner, sc.name, path, ref=ref, etag=e),
                    )
                    embeds, digest = render_file_showcase(
                        sc, text or "", default_branch=default_branch, interval=interval,
                    )
            except GitHubError as exc:
                await self._record_error(sc, f"GitHub: {exc}")
                return

            changed = digest != sc.last_hash
            last_change = time.time() if changed or sc.last_change is None else sc.last_change
            embeds[-1].timestamp = datetime.fromtimestamp(last_change, tz=timezone.utc)
            if sc.message_ids and not changed and not force and not sc.last_error:
                return

            try:
                channel = self.bot.get_channel(sc.channel_id) or await self.bot.fetch_channel(sc.channel_id)
            except (discord.NotFound, discord.Forbidden):
                await self._record_error(sc, "Channel is gone or I can't see it")
                return
            if not isinstance(channel, discord.TextChannel):
                await self._record_error(sc, "Channel is not a text channel")
                return

            view = showcase_view(sc, default_branch=default_branch)
            old_ids = sc.message_ids
            new_ids: list[int] = []
            need_pin = sc.pinned and not old_ids
            try:
                rebuilding = False
                for idx, embed in enumerate(embeds):
                    msg_view = view if idx == len(embeds) - 1 else None
                    if not rebuilding and idx < len(old_ids):
                        try:
                            await channel.get_partial_message(old_ids[idx]).edit(embed=embed, view=msg_view)
                            new_ids.append(old_ids[idx])
                            continue
                        except discord.NotFound:
                            rebuilding = True
                            # Delete remaining stale chunks so recreated messages stay in reading order.
                            for stale_id in old_ids[idx + 1:]:
                                try:
                                    await channel.get_partial_message(stale_id).delete()
                                except discord.HTTPException:
                                    pass
                            need_pin = sc.pinned and idx == 0
                    msg = await channel.send(embed=embed, view=msg_view)
                    new_ids.append(msg.id)
                    if idx == 0 and sc.pinned:
                        need_pin = True
                        try:
                            await msg.pin(reason="Showcase")
                            need_pin = False
                        except discord.HTTPException as exc:
                            log.warning("Couldn't pin showcase %s: %s", sc.id, exc)

                if not rebuilding and len(old_ids) > len(embeds):
                    for extra_id in old_ids[len(embeds):]:
                        try:
                            await channel.get_partial_message(extra_id).delete()
                        except discord.HTTPException:
                            pass

                if need_pin and new_ids:
                    try:
                        await channel.get_partial_message(new_ids[0]).pin(reason="Showcase")
                    except discord.HTTPException as exc:
                        log.warning("Couldn't pin showcase %s: %s", sc.id, exc)
            except discord.HTTPException as exc:
                await self._record_error(sc, f"Discord: {exc.text or exc}")
                return

            if new_ids != old_ids:
                await db.set_showcase_messages(sc.id, new_ids)
                sc.message_ids_json = json.dumps(new_ids)
            await db.set_showcase_state(sc.id, last_hash=digest, last_change=last_change, error=None)
            sc.last_hash, sc.last_change, sc.last_error = digest, last_change, None

    async def delete_messages(self, sc: Showcase) -> int:
        """Delete existing Discord showcase messages best-effort. Returns number deleted."""
        if not sc.message_ids:
            return 0
        channel = self.bot.get_channel(sc.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(sc.channel_id)
            except discord.HTTPException:
                return 0
        if not isinstance(channel, discord.TextChannel):
            return 0
        deleted = 0
        for mid in sc.message_ids:
            try:
                await channel.get_partial_message(mid).delete()
                deleted += 1
            except discord.HTTPException:
                pass
        return deleted

    async def message_deleted(self, message_id: int) -> None:
        """Reconcile a showcase if one of its Discord messages is deleted."""
        sc = await self.bot.db.find_showcase_by_message(message_id)
        if sc is None:
            return
        remaining = [mid for mid in sc.message_ids if mid != message_id]
        await self.bot.db.set_showcase_messages(sc.id, remaining)
        sc.message_ids_json = json.dumps(remaining) if remaining else None
        log.info("Showcase %s message %s deleted; reconciling", sc.id, message_id)
        await self.refresh(sc, force=True)

    async def handle_refresh(self, interaction: discord.Interaction, showcase_id: int) -> None:
        sc = await self.bot.db.get_showcase(showcase_id, interaction.guild_id)
        if sc is None:
            raise UserError("This showcase no longer exists.")
        last = self._manual.get(sc.id)
        if last is not None and time.monotonic() - last < MANUAL_REFRESH_COOLDOWN:
            await interaction.response.send_message(
                "It was just refreshed. Try again in a few seconds.", ephemeral=True,
            )
            return
        self._manual[sc.id] = time.monotonic()
        await interaction.response.defer()
        await self.refresh(sc, force=True)
        refreshed = await self.bot.db.get_showcase(sc.id)
        if refreshed and refreshed.last_error:
            await interaction.followup.send(f"❌ Couldn't refresh: {refreshed.last_error}", ephemeral=True)
