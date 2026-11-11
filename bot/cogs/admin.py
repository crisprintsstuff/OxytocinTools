from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import bot_admin

if TYPE_CHECKING:
    from ..main import TrackerBot


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@app_commands.guild_only()
class Admin(commands.GroupCog, group_name="admin", group_description="Bot administration"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="set-role", description="Let members with this role manage the bot")
    @bot_admin()
    async def set_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert interaction.guild is not None
        await self.bot.db.set_admin_role(interaction.guild.id, role.id)
        await interaction.response.send_message(
            f"✅ Members with {role.mention} can now use admin commands.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="clear-role", description="Remove the bot admin role")
    @bot_admin()
    async def clear_role(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await self.bot.db.set_admin_role(interaction.guild.id, None)
        await interaction.response.send_message(
            "✅ Admin role cleared. Only members with **Manage Server** can use admin commands now.",
            ephemeral=True)

    @app_commands.command(name="status", description="Show polling and GitHub API status")
    @bot_admin()
    async def status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        bot, poller, gh = self.bot, self.bot.poller, self.bot.github
        repos = await bot.db.list_repos(interaction.guild.id)
        routes = await bot.db.list_routes(interaction.guild.id)
        role_id = await bot.db.get_admin_role(interaction.guild.id)

        embed = discord.Embed(title="Tracker status", color=discord.Color.blurple())
        embed.add_field(name="This server",
                        value=f"{len(repos)} repo(s)\n{sum(r.enabled for r in routes)} active / "
                              f"{len(routes)} route(s)\nAdmin role: "
                              f"{f'<@&{role_id}>' if role_id else 'none'}")
        last = discord.utils.format_dt(_dt(poller.last_run), "R") \
            if poller.last_run else "not yet"
        nxt = discord.utils.format_dt(_dt(poller.next_run), "R") \
            if poller.next_run else "soon"
        embed.add_field(name="Polling",
                        value=f"Every {poller.interval}s\nLast: {last}\nNext: {nxt}\n"
                              f"Feeds (all servers): {poller.last_stream_count}")
        if gh.rate_remaining is not None:
            reset = discord.utils.format_dt(_dt(gh.rate_reset or time.time()), "R")
            rate = f"{gh.rate_remaining}/{gh.rate_limit} left, resets {reset}"
        else:
            rate = "no requests yet"
        embed.add_field(name="GitHub API",
                        value=f"Token: {'yes' if gh.token else '**no** (60 req/h)'}\n{rate}")

        problems = []
        for r in routes:
            if r.last_error and not r.enabled:
                problems.append(f"Route `#{r.id}`: {r.last_error}")
            else:
                state = await bot.db.get_stream(r.repo_key, r.stream)
                if r.enabled and state and state["last_error"]:
                    problems.append(f"Route `#{r.id}` ({r.full_name} {r.stream}): {state['last_error']}")
        if problems:
            embed.add_field(name="⚠️ Problems", value="\n".join(problems)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="check-now", description="Poll GitHub right now instead of waiting")
    @bot_admin()
    async def check_now(self, interaction: discord.Interaction) -> None:
        self.bot.poller.trigger()
        await interaction.response.send_message("🔄 Checking GitHub now.", ephemeral=True)


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Admin(bot))
