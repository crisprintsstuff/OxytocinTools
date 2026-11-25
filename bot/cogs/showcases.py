from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..embeds import truncate
from ..models import Showcase
from . import missing_channel_perms, repo_autocomplete, resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot

KIND_CHOICES = [
    app_commands.Choice(name="Stable releases only", value="stable"),
    app_commands.Choice(name="All releases (incl. pre-releases)", value="all"),
    app_commands.Choice(name="Pre-releases only", value="prerelease"),
]


def _jump(sc: Showcase) -> str:
    ids = sc.message_ids
    if not ids:
        return "*not posted yet*"
    suffix = f" ({len(ids)} msgs)" if len(ids) > 1 else ""
    return f"[message](https://discord.com/channels/{sc.guild_id}/{sc.channel_id}/{ids[0]}){suffix}"


@app_commands.guild_only()
class Showcases(commands.GroupCog, group_name="showcase",
                group_description="Live feature & release boards kept in sync with GitHub"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    async def _showcase_autocomplete(self, interaction: discord.Interaction,
                                     current: str) -> list[app_commands.Choice[int]]:
        if interaction.guild is None:
            return []
        choices = []
        for sc in await self.bot.db.list_showcases(interaction.guild.id):
            channel = interaction.guild.get_channel(sc.channel_id)
            label = f"#{sc.id} {sc.describe()} in #{channel.name if channel else sc.channel_id}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=sc.id))
        return choices[:25]

    async def _create_and_post(self, interaction: discord.Interaction,
                               channel: discord.TextChannel, sc: Showcase) -> None:
        await self.bot.showcases.refresh(sc, force=True)
        posted = await self.bot.db.get_showcase(sc.id)
        assert posted is not None
        if posted.last_error and not posted.message_ids:
            await self.bot.db.remove_showcase(posted.id)
            raise UserError(f"Couldn't create showcase: {posted.last_error}")

        note = ""
        perms = channel.permissions_for(channel.guild.me)
        if posted.pinned and not (perms.manage_messages or getattr(perms, "pin_messages", False)):
            note = "\n⚠️ Couldn't pin: I need **Manage Messages** (or **Pin Messages**)."
        await interaction.followup.send(
            f"✅ Showcase **#{posted.id}** ({posted.describe()}) posted in {channel.mention} "
            f"({_jump(posted)}). It updates automatically on GitHub checks.{note}",
            ephemeral=True,
        )

    @app_commands.command(name="file", description="Sync a channel board from a repo markdown file")
    @app_commands.describe(
        repo="A tracked repository",
        channel="Channel where the showcase will be posted and updated",
        path="Path to the markdown file in the repo (default: README.md)",
        section="Optional comma-separated heading(s) to extract, e.g. Features, What's New",
        branch="Branch or tag to read from (default: repo's default branch)",
        title="Optional custom embed title",
        pin="Pin the first showcase message (needs Manage Messages / Pin Messages)",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def file_(self, interaction: discord.Interaction, repo: str,
                    channel: discord.TextChannel, path: str = "README.md",
                    section: str | None = None, branch: str | None = None,
                    title: str | None = None, pin: bool = False) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        clean_path = path.strip().lstrip("/") or "README.md"
        clean_section = section.strip() if section and section.strip() else None
        clean_branch = branch.strip() if branch and branch.strip() else None
        clean_title = title.strip() if title and title.strip() else None
        if missing := missing_channel_perms(channel):
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")
        for existing in await self.bot.db.list_showcases(tracked.guild_id):
            if (existing.repo_id, existing.channel_id, existing.source,
                (existing.file_path or "").lower(), existing.branch or "",
                (existing.section or "").lower()) == (
                    tracked.id, channel.id, "file", clean_path.lower(),
                    clean_branch or "", (clean_section or "").lower()):
                raise UserError(f"That showcase already exists (#{existing.id}): {_jump(existing)}")

        await interaction.response.defer(ephemeral=True, thinking=True)
        sc = await self.bot.db.add_showcase(
            tracked, channel.id, "file",
            file_path=clean_path, branch=clean_branch, section=clean_section,
            title=clean_title, pinned=pin, created_by=interaction.user.id,
        )
        await self._create_and_post(interaction, channel, sc)

    @app_commands.command(name="releases", description="Sync a channel board from GitHub release notes")
    @app_commands.describe(
        repo="A tracked repository",
        channel="Channel where the showcase will be posted and updated",
        section="Optional comma-separated heading(s), e.g. New Features, Features, Added",
        kind="Which releases to include (default: stable only)",
        count="Maximum number of recent releases to show (1-25, default: 10)",
        title="Optional custom embed title",
        pin="Pin the first showcase message (needs Manage Messages / Pin Messages)",
    )
    @app_commands.choices(kind=KIND_CHOICES)
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def releases(self, interaction: discord.Interaction, repo: str,
                       channel: discord.TextChannel, section: str | None = None,
                       kind: app_commands.Choice[str] | None = None,
                       count: app_commands.Range[int, 1, 25] = 10,
                       title: str | None = None, pin: bool = False) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        clean_section = section.strip() if section and section.strip() else None
        clean_title = title.strip() if title and title.strip() else None
        release_kind = kind.value if kind else "stable"
        if missing := missing_channel_perms(channel):
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")
        for existing in await self.bot.db.list_showcases(tracked.guild_id):
            if (existing.repo_id, existing.channel_id, existing.source,
                existing.release_kind, (existing.section or "").lower()) == (
                    tracked.id, channel.id, "releases", release_kind,
                    (clean_section or "").lower()):
                raise UserError(f"That showcase already exists (#{existing.id}): {_jump(existing)}")

        await interaction.response.defer(ephemeral=True, thinking=True)
        sc = await self.bot.db.add_showcase(
            tracked, channel.id, "releases",
            section=clean_section, release_kind=release_kind, max_releases=count,
            title=clean_title, pinned=pin, created_by=interaction.user.id,
        )
        await self._create_and_post(interaction, channel, sc)

    @app_commands.command(name="edit", description="Edit settings on an existing showcase board")
    @app_commands.describe(
        showcase="The showcase board to edit",
        path="New markdown file path (file showcases only)",
        branch="Branch/tag override (file showcases only)",
        clear_branch="Reset branch override to the repo default branch",
        section="Comma-separated heading filter(s)",
        clear_section="Remove heading filter and show full content",
        kind="Release filter (releases showcases only)",
        count="Max recent releases to include (1-25, releases only)",
        title="Custom embed title",
        clear_title="Reset to default generated title",
        pin="Pin or unpin the first showcase message",
    )
    @app_commands.choices(kind=KIND_CHOICES)
    @app_commands.autocomplete(showcase=_showcase_autocomplete)
    @bot_admin()
    async def edit(self, interaction: discord.Interaction, showcase: int,
                   path: str | None = None, branch: str | None = None,
                   clear_branch: bool = False, section: str | None = None,
                   clear_section: bool = False,
                   kind: app_commands.Choice[str] | None = None,
                   count: app_commands.Range[int, 1, 25] | None = None,
                   title: str | None = None, clear_title: bool = False,
                   pin: bool | None = None) -> None:
        assert interaction.guild is not None
        sc = await self.bot.db.get_showcase(showcase, interaction.guild.id)
        if sc is None:
            raise UserError(f"Showcase #{showcase} doesn't exist. See `/showcase list`.")

        if sc.source != "file" and (path is not None or branch is not None or clear_branch):
            raise UserError("`path` and `branch` apply only to file showcases.")
        if sc.source != "releases" and (kind is not None or count is not None):
            raise UserError("`kind` and `count` apply only to releases showcases.")

        new_path = path.strip().lstrip("/") if path is not None else sc.file_path
        if branch is not None:
            new_branch = branch.strip() or None
        elif clear_branch:
            new_branch = None
        else:
            new_branch = sc.branch

        if section is not None:
            new_section = section.strip() or None
        elif clear_section:
            new_section = None
        else:
            new_section = sc.section

        new_kind = kind.value if kind is not None else sc.release_kind
        new_count = count if count is not None else sc.max_releases
        if title is not None:
            new_title = title.strip() or None
        elif clear_title:
            new_title = None
        else:
            new_title = sc.title
        new_pin = pin if pin is not None else sc.pinned

        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await self.bot.db.update_showcase(
            sc.id, file_path=new_path, branch=new_branch, section=new_section,
            release_kind=new_kind, max_releases=new_count, title=new_title, pinned=new_pin,
        )
        assert updated is not None
        await self.bot.showcases.refresh(updated, force=True)
        refreshed = await self.bot.db.get_showcase(updated.id)
        assert refreshed is not None
        warn = f"\n⚠️ Refresh error: {refreshed.last_error}" if refreshed.last_error else ""
        await interaction.followup.send(
            f"✅ Updated showcase **#{refreshed.id}**: {refreshed.describe()} ({_jump(refreshed)}).{warn}",
            ephemeral=True,
        )

    @app_commands.command(name="list", description="List showcase boards")
    @bot_admin()
    async def list_(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        showcases = await self.bot.db.list_showcases(interaction.guild.id)
        if not showcases:
            await interaction.response.send_message(
                "No showcases yet. Create one with `/showcase file` or `/showcase releases`.",
                ephemeral=True,
            )
            return
        lines = []
        for sc in showcases:
            lines.append(f"`#{sc.id}` {sc.describe()} · {_jump(sc)}")
            if sc.last_error:
                lines.append(f"  ⚠️ {truncate(sc.last_error, 150)}")
        embed = discord.Embed(title="Showcases", description=truncate("\n".join(lines), 4000),
                              color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="refresh", description="Refresh a showcase board immediately")
    @app_commands.describe(showcase="The showcase board to refresh")
    @app_commands.autocomplete(showcase=_showcase_autocomplete)
    @bot_admin()
    async def refresh(self, interaction: discord.Interaction, showcase: int) -> None:
        assert interaction.guild is not None
        sc = await self.bot.db.get_showcase(showcase, interaction.guild.id)
        if sc is None:
            raise UserError(f"Showcase #{showcase} doesn't exist. See `/showcase list`.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.showcases.refresh(sc, force=True)
        refreshed = await self.bot.db.get_showcase(sc.id)
        if refreshed and refreshed.last_error:
            raise UserError(f"Couldn't refresh showcase #{sc.id}: {refreshed.last_error}")
        assert refreshed is not None
        await interaction.followup.send(
            f"🔄 Refreshed showcase **#{refreshed.id}** ({_jump(refreshed)}).", ephemeral=True,
        )

    @app_commands.command(name="remove", description="Remove a showcase board")
    @app_commands.describe(showcase="The showcase board to remove",
                           delete_message="Also delete the showcase message(s) (default: yes)")
    @app_commands.autocomplete(showcase=_showcase_autocomplete)
    @bot_admin()
    async def remove(self, interaction: discord.Interaction, showcase: int,
                     delete_message: bool = True) -> None:
        assert interaction.guild is not None
        sc = await self.bot.db.get_showcase(showcase, interaction.guild.id)
        if sc is None:
            raise UserError(f"Showcase #{showcase} doesn't exist. See `/showcase list`.")
        await self.bot.db.remove_showcase(sc.id)  # first, so deleting messages doesn't recreate them
        if delete_message:
            await self.bot.showcases.delete_messages(sc)
        await interaction.response.send_message(
            f"🗑️ Removed showcase #{sc.id} ({sc.describe()}).", ephemeral=True,
        )


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Showcases(bot))
