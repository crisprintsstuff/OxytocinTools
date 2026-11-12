from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .checks import NotBotAdmin, UserError
from .config import Config
from .dashboards import DashboardManager, RefreshButton, ReportButton
from .db import Database
from .embeds import build_embeds
from .github import GitHubClient, GitHubError
from .models import Route
from .poller import Poller

log = logging.getLogger("bot")

EXTENSIONS = ("bot.cogs.repos", "bot.cogs.routes", "bot.cogs.admin", "bot.cogs.issues",
              "bot.cogs.dashboards")


class TrackerBot(commands.Bot):
    db: Database
    github: GitHubClient
    poller: Poller
    dashboards: DashboardManager

    def __init__(self, config: Config):
        intents = discord.Intents.default()  # no privileged intents needed
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         help_command=None)
        self.config = config
        self._cleaned_guild_commands = False

    async def setup_hook(self) -> None:
        self.db = await Database.open(self.config.database_path)
        try:
            self.github = GitHubClient(self.config.github_token,
                                       app_id=self.config.github_app_id,
                                       private_key=self.config.github_app_private_key)
        except GitHubError as exc:
            raise SystemExit(str(exc)) from exc
        await self.github.start()
        self.dashboards = DashboardManager(self)
        self.poller = Poller(self.db, self.github, self.deliver,
                             interval=self.config.poll_interval,
                             wait_until_ready=self.wait_until_ready,
                             after_cycle=self.dashboards.refresh_all)
        # Dashboard buttons keep working after restarts: their IDs encode the dashboard.
        self.add_dynamic_items(ReportButton, RefreshButton)
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.tree.on_error = self.on_app_command_error

        # Registering globally replaces the application's whole global command list,
        # so commands left over from older code or another bot are removed.
        synced = await self.tree.sync()
        log.info("Registered %d commands", len(synced))
        if self.config.dev_guild_id:
            log.info("DEV_GUILD_ID is no longer used: commands are registered for all servers")

        if self.github.auth_mode == "anonymous":
            log.warning("No GitHub App or GITHUB_TOKEN configured: public repos only, "
                        "60 requests/hour, and issues can't be created")
        self.poller.start()

    async def close(self) -> None:
        if hasattr(self, "poller"):
            await self.poller.stop()
        if hasattr(self, "github"):
            await self.github.close()
        if hasattr(self, "db"):
            await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d server(s)", self.user, self.user and self.user.id,
                 len(self.guilds))
        if not self._cleaned_guild_commands:
            self._cleaned_guild_commands = True
            asyncio.create_task(self.remove_stale_guild_commands())

    async def remove_stale_guild_commands(self) -> None:
        """Delete per-server commands, e.g. from an older version or another bot on this app.

        This bot only registers global commands, so any server-specific command is a
        leftover that Discord would still show but nothing would answer.
        """
        for guild in self.guilds:
            try:
                leftovers = await self.tree.fetch_commands(guild=guild)
                if not leftovers:
                    continue
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Removed %d leftover command(s) from server %s: %s", len(leftovers),
                         guild.name, ", ".join("/" + c.name for c in leftovers))
            except discord.HTTPException as exc:
                log.warning("Couldn't check leftover commands in %s: %s", guild.name, exc)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Removed from guild %s; deleting its configuration", guild.id)
        await self.db.delete_guild(guild.id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        removed = await self.db.remove_routes_for_channel(channel.id)
        removed += await self.db.remove_dashboards_for_channel(channel.id)
        if removed:
            log.info("Channel %s deleted; removed %d route(s)/dashboard(s)", channel.id, removed)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.dashboards.message_deleted(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids:
            await self.dashboards.message_deleted(message_id)

    # ------------------------------------------------------------------ delivery
    async def deliver(self, route: Route, stream: str, items: list[dict[str, Any]]) -> None:
        """Post new GitHub items to a route's channel."""
        try:
            channel = self.get_channel(route.channel_id) or await self.fetch_channel(route.channel_id)
        except (discord.NotFound, discord.Forbidden) as exc:
            await self._disable_route(route, f"Channel unavailable: {exc}")
            return
        if not isinstance(channel, discord.abc.Messageable) or getattr(channel, "guild", None) is None \
                or channel.guild.id != route.guild_id:
            await self._disable_route(route, "Channel is not a text channel in this server")
            return

        content = f"<@&{route.mention_role_id}>" if route.mention_role_id else None
        allowed = discord.AllowedMentions(
            everyone=False, users=False,
            roles=[discord.Object(id=route.mention_role_id)] if route.mention_role_id else False,
        )
        for embed in build_embeds(route.full_name, stream, items):
            try:
                msg = await channel.send(content=content, embed=embed, allowed_mentions=allowed)
            except (discord.Forbidden, discord.NotFound) as exc:
                await self._disable_route(route, f"Cannot post in channel: {exc.text or exc}")
                return
            if route.crosspost and isinstance(channel, discord.TextChannel) and channel.is_news():
                try:
                    await msg.publish()
                except discord.HTTPException as exc:
                    log.warning("Could not publish message in %s: %s", channel.id, exc)

    async def _disable_route(self, route: Route, reason: str) -> None:
        log.warning("Disabling route %s: %s", route.id, reason)
        await self.db.set_route_enabled(route.id, False, reason)

    # -------------------------------------------------------------------- errors
    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, (UserError, NotBotAdmin)):
            message = str(original)
        elif isinstance(original, app_commands.CommandNotFound):
            message = ("That command is left over from an older setup and no longer exists. "
                       "It will disappear from the menu once the bot restarts. To add a repo, "
                       "use `/repo track`.")
        elif isinstance(original, app_commands.NoPrivateMessage):
            message = "This command only works in a server."
        elif isinstance(original, app_commands.CheckFailure):
            message = "You don't have permission to use this command."
        elif isinstance(original, GitHubError):
            message = f"GitHub error: {original}"
        else:
            log.error("Unhandled error in /%s", interaction.command and interaction.command.qualified_name,
                      exc_info=original)
            message = "Something went wrong running that command. Check the bot logs."
        message = f"❌ {message}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


def run() -> None:
    config = Config.from_env()
    discord.utils.setup_logging(level=getattr(logging, config.log_level, logging.INFO))
    bot = TrackerBot(config)
    bot.run(config.discord_token, log_handler=None)
