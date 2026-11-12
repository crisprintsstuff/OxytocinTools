from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..embeds import truncate
from ..models import Dashboard
from . import missing_channel_perms, repo_autocomplete, resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot


def _jump(dash: Dashboard) -> str:
    if not dash.message_id:
        return "*not posted yet*"
    return f"[message](https://discord.com/channels/{dash.guild_id}/{dash.channel_id}/{dash.message_id})"


@app_commands.guild_only()
class Dashboards(commands.GroupCog, group_name="dashboard",
                 group_description="Live issue dashboards that stay up to date in a channel"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    async def _dashboard_autocomplete(self, interaction: discord.Interaction,
                                      current: str) -> list[app_commands.Choice[int]]:
        if interaction.guild is None:
            return []
        choices = []
        for d in await self.bot.db.list_dashboards(interaction.guild.id):
            channel = interaction.guild.get_channel(d.channel_id)
            label = f"#{d.id} {d.describe()} in #{channel.name if channel else d.channel_id}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=d.id))
        return choices[:25]

    @app_commands.command(name="create", description="Post a live issue dashboard for a repository")
    @app_commands.describe(
        repo="A tracked repository",
        channel="Where to post the dashboard",
        label="Only show issues with this GitHub label, e.g. bug",
        pin="Pin the dashboard message (needs Manage Messages / Pin Messages)",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def create(self, interaction: discord.Interaction, repo: str,
                     channel: discord.TextChannel, label: str | None = None,
                     pin: bool = False) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        label = label.strip() if label else None
        if missing := missing_channel_perms(channel):
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")
        for d in await self.bot.db.list_dashboards(tracked.guild_id):
            if (d.repo_id, d.channel_id, (d.label or "").lower()) == \
                    (tracked.id, channel.id, (label or "").lower()):
                raise UserError(f"That dashboard already exists (#{d.id}): {_jump(d)}")

        await interaction.response.defer(ephemeral=True, thinking=True)
        dash = await self.bot.db.add_dashboard(tracked, channel.id, label=label, pinned=pin,
                                               created_by=interaction.user.id)
        await self.bot.dashboards.refresh(dash, force=True)
        dash = await self.bot.db.get_dashboard(dash.id)
        assert dash is not None
        if dash.last_error and not dash.message_id:
            await self.bot.db.remove_dashboard(dash.id)
            raise UserError(f"Couldn't create the dashboard: {dash.last_error}")

        note = ""
        perms = channel.permissions_for(channel.guild.me)
        if pin and not (perms.manage_messages or getattr(perms, "pin_messages", False)):
            note = "\n⚠️ I couldn't pin it: I need the **Manage Messages** (or **Pin Messages**) permission."
        await interaction.followup.send(
            f"✅ Dashboard **#{dash.id}** for **{dash.describe()}** posted in {channel.mention} "
            f"({_jump(dash)}). It updates on every GitHub check. Use `/dashboard remove` to "
            f"take it down.{note}",
            ephemeral=True)

    @app_commands.command(name="list", description="List issue dashboards")
    @bot_admin()
    async def list_(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        dashboards = await self.bot.db.list_dashboards(interaction.guild.id)
        if not dashboards:
            await interaction.response.send_message(
                "No dashboards yet. Create one with `/dashboard create`.", ephemeral=True)
            return
        lines = []
        for d in dashboards:
            lines.append(f"`#{d.id}` **{d.describe()}** in <#{d.channel_id}> · {_jump(d)}")
            if d.last_error:
                lines.append(f"  ⚠️ {truncate(d.last_error, 150)}")
        embed = discord.Embed(title="Issue dashboards", description=truncate("\n".join(lines), 4000),
                              color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="remove", description="Delete an issue dashboard")
    @app_commands.describe(dashboard="The dashboard to remove",
                           delete_message="Also delete the dashboard message (default: yes)")
    @app_commands.autocomplete(dashboard=_dashboard_autocomplete)
    @bot_admin()
    async def remove(self, interaction: discord.Interaction, dashboard: int,
                     delete_message: bool = True) -> None:
        assert interaction.guild is not None
        dash = await self.bot.db.get_dashboard(dashboard, interaction.guild.id)
        if dash is None:
            raise UserError(f"Dashboard #{dashboard} doesn't exist. See `/dashboard list`.")
        await self.bot.db.remove_dashboard(dash.id)  # first, so the delete isn't re-posted
        channel = interaction.guild.get_channel(dash.channel_id)
        if delete_message and dash.message_id and isinstance(channel, discord.TextChannel):
            try:
                await channel.get_partial_message(dash.message_id).delete()
            except discord.HTTPException:
                pass
        await interaction.response.send_message(
            f"🗑️ Removed dashboard #{dash.id} ({dash.describe()}).", ephemeral=True)


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Dashboards(bot))
