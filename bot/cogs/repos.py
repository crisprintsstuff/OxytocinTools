from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..embeds import release_embed, truncate
from ..github import GitHubError, NotFound
from ..models import parse_repo
from . import ConfirmView, missing_channel_perms, repo_autocomplete, resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot


class Repos(commands.GroupCog, group_name="repo", group_description="Track GitHub repositories"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="track", description="Start tracking a GitHub repository")
    @app_commands.describe(
        repo="owner/name or a GitHub URL",
        releases_channel="Optional: also post every release of this repo to this channel",
    )
    @app_commands.guild_only()
    @bot_admin()
    async def track(self, interaction: discord.Interaction, repo: str,
                    releases_channel: discord.TextChannel | None = None) -> None:
        assert interaction.guild is not None
        try:
            owner, name = parse_repo(repo)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        if releases_channel and (missing := missing_channel_perms(releases_channel)):
            raise UserError(f"I'm missing {', '.join(missing)} in {releases_channel.mention}.")

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self.bot.github.get_repo(owner, name)
        except NotFound:
            raise UserError(
                f"Couldn't find `{owner}/{name}` on GitHub. If it's private, the bot's "
                "GitHub token needs access to it."
            )
        owner, name = data["owner"]["login"], data["name"]  # canonical casing
        try:
            tracked = await self.bot.db.add_repo(interaction.guild.id, owner, name,
                                                 data.get("default_branch") or "main",
                                                 interaction.user.id)
        except sqlite3.IntegrityError:
            raise UserError(f"`{owner}/{name}` is already tracked.")

        msg = f"✅ Now tracking **[{tracked.full_name}]({data['html_url']})**."
        if releases_channel:
            route = await self.bot.db.add_route(tracked, releases_channel.id, "releases",
                                                created_by=interaction.user.id)
            try:
                await self.bot.poller.ensure_baseline(route)
            except GitHubError:
                pass  # the regular poll cycle will retry the baseline
            msg += f"\nReleases will be posted to {releases_channel.mention} (route #{route.id})."
        else:
            msg += "\nUse `/route add` to choose which events go to which channels."
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="untrack", description="Stop tracking a repository and delete its routes")
    @app_commands.autocomplete(repo=repo_autocomplete)
    @app_commands.guild_only()
    @bot_admin()
    async def untrack(self, interaction: discord.Interaction, repo: str) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        routes = await self.bot.db.list_routes(tracked.guild_id, tracked.id)
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Stop tracking **{tracked.full_name}** and delete its {len(routes)} route(s)?",
            view=view, ephemeral=True,
        )
        await view.wait()
        if view.confirmed:
            await self.bot.db.remove_repo(tracked.id)
            await interaction.edit_original_response(
                content=f"🗑️ Stopped tracking **{tracked.full_name}**.", view=None)
        elif view.confirmed is None:
            await interaction.edit_original_response(content="Timed out.", view=None)

    @app_commands.command(name="list", description="List tracked repositories")
    @app_commands.guild_only()
    async def list_(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        repos = await self.bot.db.list_repos(interaction.guild.id)
        if not repos:
            await interaction.response.send_message(
                "No repositories are tracked yet. An admin can add one with `/repo track`.",
                ephemeral=True)
            return
        routes = await self.bot.db.list_routes(interaction.guild.id)
        counts: dict[int, int] = {}
        for r in routes:
            if r.enabled:
                counts[r.repo_id] = counts.get(r.repo_id, 0) + 1
        lines = [
            f"• [{r.full_name}](https://github.com/{r.full_name}) — "
            f"{counts.get(r.id, 0)} active route(s)"
            for r in repos
        ]
        embed = discord.Embed(title=f"Tracked repositories ({len(repos)})",
                              description=truncate("\n".join(lines), 4000),
                              color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="latest", description="Show the latest release of a repository")
    @app_commands.describe(repo="owner/name (doesn't have to be tracked)",
                           include_prereleases="Also consider pre-releases")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def latest(self, interaction: discord.Interaction, repo: str,
                     include_prereleases: bool = False) -> None:
        try:
            owner, name = parse_repo(repo)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        await interaction.response.defer()
        try:
            release = await self.bot.github.latest_release(owner, name, include_prereleases)
        except NotFound:
            raise UserError(f"Couldn't find `{owner}/{name}` on GitHub.")
        if release is None:
            await interaction.followup.send(f"`{owner}/{name}` has no published releases.")
            return
        await interaction.followup.send(embed=release_embed(f"{owner}/{name}", release))

    @app_commands.command(name="info", description="Show information about a repository")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def info(self, interaction: discord.Interaction, repo: str) -> None:
        try:
            owner, name = parse_repo(repo)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        await interaction.response.defer()
        try:
            data = await self.bot.github.get_repo(owner, name)
        except NotFound:
            raise UserError(f"Couldn't find `{owner}/{name}` on GitHub.")
        embed = discord.Embed(title=data["full_name"], url=data["html_url"],
                              description=data.get("description") or "",
                              color=discord.Color.dark_grey())
        embed.set_thumbnail(url=data["owner"]["avatar_url"])
        embed.add_field(name="⭐ Stars", value=f"{data['stargazers_count']:,}")
        embed.add_field(name="🍴 Forks", value=f"{data['forks_count']:,}")
        embed.add_field(name="Open issues + PRs", value=f"{data['open_issues_count']:,}")
        embed.add_field(name="Default branch", value=f"`{data['default_branch']}`")
        if data.get("language"):
            embed.add_field(name="Language", value=data["language"])
        if data.get("pushed_at"):
            pushed = datetime.fromisoformat(data["pushed_at"].replace("Z", "+00:00"))
            embed.add_field(name="Last push", value=discord.utils.format_dt(pushed, "R"))
        if data.get("archived"):
            embed.set_footer(text="This repository is archived")
        await interaction.followup.send(embed=embed)


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Repos(bot))
