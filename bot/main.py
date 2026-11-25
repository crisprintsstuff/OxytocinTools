from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .checks import NotBotAdmin, UserError
from .config import Config
from .dashboards import DashboardManager, RefreshButton, ReportButton
from .db import Database
from .digests import DigestManager
from .embeds import ORANGE, build_embeds, truncate
from .github import GitHubClient, GitHubError
from .models import Route
from .poller import Poller
from .prcards import PRCardManager, RefreshPRCardButton
from .showcases import RefreshShowcaseButton, ShowcaseManager
from .watches import (
    CloseFiledButton,
    CommentFiledButton,
    CompleteFiledButton,
    LabelFiledButton,
    WatchManager,
)
from .webhook import WebhookServer

log = logging.getLogger("bot")

ALERT_COOLDOWN = 6 * 3600

EXTENSIONS = ("bot.cogs.repos", "bot.cogs.routes", "bot.cogs.admin", "bot.cogs.issues",
              "bot.cogs.dashboards", "bot.cogs.watches", "bot.cogs.prs", "bot.cogs.digests",
              "bot.cogs.showcases")


class TrackerBot(commands.Bot):
    db: Database
    github: GitHubClient
    poller: Poller
    dashboards: DashboardManager
    watches: WatchManager
    prcards: PRCardManager
    digests: DigestManager
    showcases: ShowcaseManager
    webhook: WebhookServer | None

    def __init__(self, config: Config, *, message_content: bool = True):
        intents = discord.Intents.default()
        # Privileged: needed to read messages for role-mention and forum watches.
        intents.message_content = message_content
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         help_command=None)
        self.config = config
        self._cleaned_guild_commands = False
        self._last_db_backup: float = 0.0
        self._alert_sent: dict[tuple[int, str], float] = {}
        self.webhook = None

    def _active_guild_ids(self) -> set[int] | None:
        return {g.id for g in self.guilds} if self.is_ready() else None

    def _commands_hash(self) -> str:
        payload = json.dumps(
            [c.to_dict(self.tree) for c in self.tree.get_commands()],
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    async def sync_commands(self, *, force: bool = False) -> int:
        digest = self._commands_hash()
        prev = await self.db.get_meta("commands_hash")
        if not force and prev == digest:
            count = len(self.tree.get_commands())
            log.info("Slash commands unchanged (%d command(s)); skipping global sync", count)
            return count
        synced = await self.tree.sync()
        await self.db.set_meta("commands_hash", digest)
        log.info("Registered %d commands", len(synced))
        return len(synced)

    async def setup_hook(self) -> None:
        self.db = await Database.open(
            self.config.database_path,
            config_backup_path=self.config.config_backup_path,
        )
        if self.config.config_backup_path:
            if not await self.db.has_any_config() and os.path.exists(self.config.config_backup_path):
                try:
                    restored = await self.db.load_config_snapshot(self.config.config_backup_path)
                    if restored:
                        log.info("Restored configuration from %s: %s",
                                 self.config.config_backup_path, restored)
                except Exception as exc:
                    log.warning("Couldn't restore config backup %s: %s",
                                self.config.config_backup_path, exc)
            elif await self.db.has_any_config():
                try:
                    await self.db.save_config_snapshot(self.config.config_backup_path)
                except Exception as exc:
                    log.warning("Couldn't write initial config backup %s: %s",
                                self.config.config_backup_path, exc)
        if self.config.backup_count > 0 and self.config.database_path != ":memory:":
            try:
                await self.db.backup(keep=self.config.backup_count)
                self._last_db_backup = time.time()
            except Exception as exc:
                log.warning("Startup database backup failed: %s", exc)
        try:
            self.github = GitHubClient(self.config.github_token,
                                       app_id=self.config.github_app_id,
                                       private_key=self.config.github_app_private_key)
        except GitHubError as exc:
            raise SystemExit(str(exc)) from exc
        await self.github.start()
        self.dashboards = DashboardManager(self)
        self.watches = WatchManager(self)
        self.prcards = PRCardManager(self)
        self.digests = DigestManager(self)
        self.showcases = ShowcaseManager(self)
        self.poller = Poller(self.db, self.github, self.deliver,
                             interval=self.config.poll_interval,
                             wait_until_ready=self.wait_until_ready,
                             after_cycle=self.after_poll,
                             release_notes_wait=self.config.release_notes_wait * 60,
                             active_guilds=self._active_guild_ids)
        # Dashboard, filing & PR card buttons keep working after restarts.
        self.add_dynamic_items(
            ReportButton, RefreshButton, RefreshPRCardButton, RefreshShowcaseButton,
            CloseFiledButton, CompleteFiledButton, LabelFiledButton, CommentFiledButton,
        )
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.tree.on_error = self.on_app_command_error

        # Sync globally only when the command schema changes (or via /admin sync).
        await self.sync_commands()
        if self.config.dev_guild_id:
            log.info("DEV_GUILD_ID is no longer used: commands are registered for all servers")

        if self.github.auth_mode == "anonymous":
            log.warning("No GitHub App or GITHUB_TOKEN configured: public repos only, "
                        "60 requests/hour, and issues can't be created")
        self.poller.start()
        if self.config.webhook_port > 0:
            self.webhook = WebhookServer(
                host=self.config.webhook_host,
                port=self.config.webhook_port,
                on_trigger=self.poller.trigger,
                secret=self.config.webhook_secret,
                path=self.config.webhook_path,
            )
            await self.webhook.start()

    async def close(self) -> None:
        if getattr(self, "webhook", None):
            await self.webhook.stop()  # type: ignore[union-attr]
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
        log.warning("Removed from guild %s (%s); keeping its configuration in the database",
                    guild.id, guild.name)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        removed = await self.db.remove_routes_for_channel(channel.id)
        removed += await self.db.remove_dashboards_for_channel(channel.id)
        removed += await self.db.remove_watches_for_channel(channel.id)
        removed += await self.db.remove_pr_trackers_for_channel(channel.id)
        removed += await self.db.remove_digests_for_channel(channel.id)
        removed += await self.db.remove_showcases_for_channel(channel.id)
        if getattr(channel, "guild", None) and await self.db.get_alert_channel(channel.guild.id) == channel.id:
            await self.db.set_alert_channel(channel.guild.id, None)
            removed += 1
        if removed:
            log.info("Channel %s deleted; removed %d route(s)/dashboard(s)", channel.id, removed)

    async def send_alert(self, guild_id: int, key: str, title: str, detail: str) -> bool:
        """Post a deduplicated error alert to the server's configured alert channel."""
        now = time.monotonic()
        last = self._alert_sent.get((guild_id, key))
        if last is not None and now - last < ALERT_COOLDOWN:
            return False
        channel_id = await self.db.get_alert_channel(guild_id)
        if not channel_id:
            return False
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return False
        if channel is None or not hasattr(channel, "send"):
            return False
        embed = discord.Embed(
            title=truncate(f"⚠️ {title}", 256),
            description=truncate(detail, 1500),
            color=ORANGE,
        )
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return False
        self._alert_sent[(guild_id, key)] = now
        return True

    async def after_poll(self) -> None:
        self.prcards.begin_cycle()  # PR dashboards and PR cards share one response cache
        try:
            await self.dashboards.refresh_all()
            await self.watches.sync_threads()
            await self.prcards.update_all()
            await self.digests.run_due()
            await self.showcases.refresh_all()
        finally:
            self.prcards.end_cycle()
        await self.db.prune_http_cache()
        await self.db.checkpoint("PASSIVE")
        if (self.config.backup_count > 0 and self.config.database_path != ":memory:"
                and time.time() - self._last_db_backup >= 86400):
            try:
                await self.db.backup(keep=self.config.backup_count)
                self._last_db_backup = time.time()
            except Exception as exc:
                log.warning("Periodic database backup failed: %s", exc)

    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.watches.on_message(message)
        except Exception:
            log.exception("Failed to handle message %s", message.id)

    async def on_thread_create(self, thread: discord.Thread) -> None:
        try:
            await self.watches.on_thread_create(thread)
        except Exception:
            log.exception("Failed to handle new thread %s", thread.id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.dashboards.message_deleted(payload.message_id)
        await self.showcases.message_deleted(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids:
            await self.dashboards.message_deleted(message_id)
            await self.showcases.message_deleted(message_id)

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
        await self.send_alert(
            route.guild_id,
            f"route:{route.id}",
            f"Route #{route.id} paused",
            f"**{route.full_name}** → <#{route.channel_id}>\n{reason}",
        )

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
    try:
        TrackerBot(config).run(config.discord_token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.error("The Message Content Intent is off, so watches (/watch) can't read messages. "
                  "Turn it on in the Discord Developer Portal: your app > Bot > Privileged "
                  "Gateway Intents > Message Content Intent. Starting without it for now.")
        TrackerBot(config, message_content=False).run(config.discord_token, log_handler=None)
