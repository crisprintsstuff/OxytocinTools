from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..models import ISSUE_ACCESS_LABELS
from . import repo_autocomplete, resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot

ACCESS_CHOICES = [app_commands.Choice(name=label, value=key)
                  for key, label in ISSUE_ACCESS_LABELS.items()]
ACCESS_CHOICES.append(app_commands.Choice(name="Use server default (repo only)", value="inherit"))


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

    @app_commands.command(name="issue-access",
                          description="Choose who can create GitHub issues from Discord")
    @app_commands.describe(level="Who may create issues",
                           repo="Only change this repository (leave empty for the server default)")
    @app_commands.choices(level=ACCESS_CHOICES)
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def issue_access(self, interaction: discord.Interaction,
                           level: app_commands.Choice[str], repo: str | None = None) -> None:
        assert interaction.guild is not None
        db = self.bot.db
        if repo:
            tracked = await resolve_tracked_repo(interaction, repo)
            await db.set_repo_issue_access(tracked.id, None if level.value == "inherit" else level.value)
            target = f"**{tracked.full_name}**"
        else:
            if level.value == "inherit":
                raise UserError("`Use server default` only applies when you pick a repository.")
            await db.set_issue_access(interaction.guild.id, level.value)
            target = "this server (repos without their own setting)"

        default, role_id = await db.get_issue_settings(interaction.guild.id)
        msg = f"✅ Issue creation for {target}: **{level.name}**."
        effective = default if level.value == "inherit" else level.value
        if effective == "role" and role_id is None:
            msg += "\n⚠️ No issue role is set yet, so only admins can create issues. Use `/admin issue-role`."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="issue-role",
                          description="Set the role allowed to create issues (leave empty to clear)")
    @bot_admin()
    async def issue_role(self, interaction: discord.Interaction,
                         role: discord.Role | None = None) -> None:
        assert interaction.guild is not None
        await self.bot.db.set_issue_role(interaction.guild.id, role.id if role else None)
        if role:
            msg = (f"✅ Members with {role.mention} can create issues in repos whose access is "
                   "**Bot admins + the issue role**.")
        else:
            msg = "✅ Issue role cleared."
        await interaction.response.send_message(msg, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

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
        if gh.auth_mode == "app":
            auth = f"GitHub App `{gh.app_slug or gh.app.app_id}`"  # type: ignore[union-attr]
            if gh.install_url:
                auth += f" ([install]({gh.install_url}))"
            if gh.token:
                auth += "\nFallback: personal token"
        elif gh.auth_mode == "token":
            auth = "Personal access token"
        else:
            auth = "**None**: public repos only, 60 req/h, no issue creation"
        names = {"anonymous": "Anonymous", "token": "Token", "app": "App"}
        rates = []
        for bucket, rl in sorted(gh.rate_limits.items()):
            label = "Installation " + bucket.split(":", 1)[1] if bucket.startswith("installation:") \
                else names.get(bucket, bucket)
            rates.append(f"{label}: {rl.remaining}/{rl.limit}, resets {discord.utils.format_dt(_dt(rl.reset), 'R')}")
        embed.add_field(name="GitHub API",
                        value=(auth + "\n" + ("\n".join(rates) or "No requests yet"))[:1024],
                        inline=False)

        default, issue_role = await bot.db.get_issue_settings(interaction.guild.id)
        overrides = [f"{r.full_name}: {ISSUE_ACCESS_LABELS[r.issue_access]}"
                     for r in repos if r.issue_access]
        issue_info = (f"Default: {ISSUE_ACCESS_LABELS.get(default, default)}\n"
                      f"Issue role: {f'<@&{issue_role}>' if issue_role else 'none'}\n"
                      f"Cooldown: {bot.config.issue_cooldown}s (admins exempt)")
        if overrides:
            issue_info += "\nOverrides:\n" + "\n".join(overrides)
        embed.add_field(name="Issue creation", value=issue_info[:1024], inline=False)

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
