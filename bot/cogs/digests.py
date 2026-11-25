from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..embeds import truncate
from . import missing_channel_perms, repo_autocomplete, resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot

KIND_CHOICES = [
    app_commands.Choice(name="Changelog (merged PRs & closed issues)", value="changelog"),
    app_commands.Choice(name="Review nudges (PRs waiting for review)", value="reviews"),
]


@app_commands.guild_only()
class Digests(commands.GroupCog, group_name="digest",
              group_description="Scheduled changelog and review nudge digests"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    async def _digest_autocomplete(self, interaction: discord.Interaction,
                                   current: str) -> list[app_commands.Choice[int]]:
        if interaction.guild is None:
            return []
        choices = []
        for d in await self.bot.db.list_digests(interaction.guild.id):
            channel = interaction.guild.get_channel(d.channel_id)
            label = f"#{d.id} {d.full_name} ({d.kind}) in #{channel.name if channel else d.channel_id}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=d.id))
        return choices[:25]

    @app_commands.command(name="add", description="Schedule a periodic changelog or review nudge digest")
    @app_commands.describe(
        repo="A tracked repository",
        channel="Channel to post the digest in",
        type="Changelog summary or stale review nudges",
        interval_days="Days between digests (default: 7)",
        stale_days="Review nudges only: minimum PR age in days (default: 3)",
        mention="Role to ping when the digest is posted",
    )
    @app_commands.choices(type=KIND_CHOICES)
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def add(self, interaction: discord.Interaction, repo: str, channel: discord.TextChannel,
                  type: app_commands.Choice[str],
                  interval_days: app_commands.Range[int, 1, 30] = 7,
                  stale_days: app_commands.Range[int, 1, 30] = 3,
                  mention: discord.Role | None = None) -> None:
        assert interaction.guild is not None
        tracked = await resolve_tracked_repo(interaction, repo)
        if missing := missing_channel_perms(channel):
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")
        if mention and mention.is_default():
            raise UserError("Pinging @everyone isn't supported; pick a role.")
        for existing in await self.bot.db.list_digests(interaction.guild.id):
            if (existing.repo_id, existing.channel_id, existing.kind) == (
                tracked.id, channel.id, type.value
            ):
                raise UserError(
                    f"{channel.mention} already has a `{type.value}` digest for **{tracked.full_name}** (#{existing.id})."
                )
        digest = await self.bot.db.add_digest(
            tracked, channel.id, type.value,
            interval_days=interval_days, stale_days=stale_days,
            mention_role_id=mention.id if mention else None,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            f"✅ Digest **#{digest.id}**: {digest.describe()}. Use `/digest run` to send one now.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="list", description="List scheduled digests")
    @bot_admin()
    async def list_(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        digests = await self.bot.db.list_digests(interaction.guild.id)
        if not digests:
            await interaction.response.send_message(
                "No digests scheduled yet. Add one with `/digest add`.", ephemeral=True
            )
            return
        lines = []
        for d in digests:
            lines.append(f"`#{d.id}` {d.describe()}")
            if d.last_error:
                lines.append(f"  ⚠️ {truncate(d.last_error, 150)}")
        embed = discord.Embed(title="Scheduled digests", description=truncate("\n".join(lines), 4000),
                              color=discord.Color.blurple())
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="remove", description="Remove a scheduled digest")
    @app_commands.autocomplete(digest=_digest_autocomplete)
    @bot_admin()
    async def remove(self, interaction: discord.Interaction, digest: int) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_digest(digest, interaction.guild.id)
        if found is None:
            raise UserError(f"Digest #{digest} doesn't exist. See `/digest list`.")
        await self.bot.db.remove_digest(found.id)
        await interaction.response.send_message(
            f"🗑️ Removed digest #{found.id} ({found.full_name}).", ephemeral=True
        )

    @app_commands.command(name="run", description="Send a scheduled digest right now")
    @app_commands.autocomplete(digest=_digest_autocomplete)
    @bot_admin()
    async def run(self, interaction: discord.Interaction, digest: int) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_digest(digest, interaction.guild.id)
        if found is None:
            raise UserError(f"Digest #{digest} doesn't exist. See `/digest list`.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.bot.prcards.begin_cycle()
        try:
            posted = await self.bot.digests.send(found, force=True)
        finally:
            self.bot.prcards.end_cycle()
        refreshed = await self.bot.db.get_digest(found.id)
        if refreshed and refreshed.last_error:
            raise UserError(f"Couldn't send digest: {refreshed.last_error}")
        if posted:
            await interaction.followup.send(f"📨 Sent digest #{found.id} to <#{found.channel_id}>.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"ℹ️ Nothing waiting for review in **{found.full_name}** right now.", ephemeral=True
            )


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Digests(bot))
