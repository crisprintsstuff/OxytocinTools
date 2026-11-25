from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..github import NotFound
from ..models import ISSUE_ACCESS_LABELS, parse_repo
from . import access_hint, missing_channel_perms, repo_autocomplete, resolve_tracked_repo

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
        dashboards = await bot.db.list_dashboards(interaction.guild.id)
        watches = await bot.db.list_watches(interaction.guild.id)
        trackers = await bot.db.list_pr_trackers(interaction.guild.id)
        digests = await bot.db.list_digests(interaction.guild.id)
        showcases = await bot.db.list_showcases(interaction.guild.id)
        notify_panels = await bot.db.list_notify_panels(interaction.guild.id)
        role_id = await bot.db.get_admin_role(interaction.guild.id)
        alert_ch = await bot.db.get_alert_channel(interaction.guild.id)

        embed = discord.Embed(title="Tracker status", color=discord.Color.blurple())
        embed.add_field(name="This server",
                        value=f"{len(repos)} repo(s)\n{sum(r.enabled for r in routes)} active / "
                              f"{len(routes)} route(s)\n"
                              f"{len(dashboards)} dashboard(s) · {len(watches)} watch(es) · "
                              f"{len(trackers)} PR tracker(s) · {len(digests)} digest(s) · "
                              f"{len(showcases)} showcase(s) · {len(notify_panels)} notify panel(s)\n"
                              f"Admin role: {f'<@&{role_id}>' if role_id else 'none'} · "
                              f"Alert channel: {f'<#{alert_ch}>' if alert_ch else 'none'}")
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
        autolink = await bot.db.get_autolink_enabled(interaction.guild.id)
        sync_comments = await bot.db.get_comment_sync_enabled(interaction.guild.id)
        overrides = [f"{r.full_name}: {ISSUE_ACCESS_LABELS[r.issue_access]}"
                     for r in repos if r.issue_access]
        media_repo = await bot.db.get_media_repo(interaction.guild.id) or bot.config.media_repo
        issue_info = (f"Default: {ISSUE_ACCESS_LABELS.get(default, default)}\n"
                      f"Issue role: {f'<@&{issue_role}>' if issue_role else 'none'}\n"
                      f"Cooldown: {bot.config.issue_cooldown}s (admins exempt)\n"
                      f"Auto-link #123: {'on' if autolink else 'off'} · "
                      f"Comment sync: {'on' if sync_comments else 'off'}"
                      + (f"\nMedia repo: `{media_repo}`" if media_repo else ""))
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
        for d in dashboards:
            if d.last_error:
                problems.append(f"Dashboard `#{d.id}` ({d.full_name}): {d.last_error}")
        for t in trackers:
            if t.last_error:
                problems.append(f"PR tracker `#{t.id}` ({t.full_name}): {t.last_error}")
        for dg in digests:
            if dg.last_error:
                problems.append(f"Digest `#{dg.id}` ({dg.full_name}): {dg.last_error}")
        for sc in showcases:
            if sc.last_error:
                problems.append(f"Showcase `#{sc.id}` ({sc.full_name}): {sc.last_error}")
        if problems:
            embed.add_field(name="⚠️ Problems", value="\n".join(problems)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="check-now", description="Poll GitHub right now instead of waiting")
    @bot_admin()
    async def check_now(self, interaction: discord.Interaction) -> None:
        self.bot.poller.trigger()
        await interaction.response.send_message("🔄 Checking GitHub now.", ephemeral=True)

    @app_commands.command(name="export", description="Export this server's bot configuration as JSON")
    @bot_admin()
    async def export_(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        data = await self.bot.db.export_config(interaction.guild.id)
        raw = json.dumps(data, indent=2).encode("utf-8")
        guild_entry = data["guilds"].get(str(interaction.guild.id), {})
        repos = guild_entry.get("repos", [])
        routes = sum(len(r.get("routes", [])) for r in repos)
        dashes = sum(len(r.get("dashboards", [])) for r in repos)
        watches = sum(len(r.get("watches", [])) for r in repos)
        trackers = sum(len(r.get("pr_trackers", [])) for r in repos)
        showcases = sum(len(r.get("showcases", [])) for r in repos)
        panels = len(guild_entry.get("notify_panels", []))
        file = discord.File(io.BytesIO(raw), filename=f"oxytocintools-{interaction.guild.id}.json")
        await interaction.response.send_message(
            f"📦 Exported **{len(repos)}** repo(s), **{routes}** route(s), "
            f"**{dashes}** dashboard(s), **{watches}** watch(es), "
            f"**{trackers}** PR tracker(s), **{showcases}** showcase(s), and **{panels}** notify panel(s).",
            file=file,
            ephemeral=True,
        )

    @app_commands.command(name="import",
                          description="Restore this server's bot configuration from a JSON export")
    @app_commands.describe(file="JSON file previously created by /admin export")
    @bot_admin()
    async def import_(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        assert interaction.guild is not None
        if file.size > 1_000_000:
            raise UserError("Backup file is too large (max 1 MB).")
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw = await file.read()
        try:
            data = json.loads(raw.decode("utf-8"))
            counts = await self.bot.db.import_config(data, guild_id=interaction.guild.id)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise UserError(f"Couldn't import configuration: {exc}") from exc
        if hasattr(self.bot, "poller"):
            self.bot.poller.trigger()
        await interaction.followup.send(
            f"✅ Imported **{counts['repos']}** repo(s), **{counts['routes']}** route(s), "
            f"**{counts['dashboards']}** dashboard(s), **{counts['watches']}** watch(es), "
            f"**{counts['pr_trackers']}** PR tracker(s), **{counts['showcases']}** showcase(s), "
            f"and **{counts['notify_panels']}** notify panel(s) (existing entries skipped).",
            ephemeral=True,
        )

    @app_commands.command(name="sync", description="Force-sync global slash commands with Discord")
    @bot_admin()
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await self.bot.sync_commands(force=True)
        await interaction.followup.send(f"✅ Synced {count} global slash command(s).", ephemeral=True)

    @app_commands.command(name="autolink",
                          description="Toggle automatic #123 issue/PR preview embeds in chat")
    @app_commands.describe(enabled="Whether #123 references in chat post preview embeds")
    @bot_admin()
    async def autolink(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        await self.bot.db.set_autolink_enabled(interaction.guild.id, enabled)
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"✅ Automatic `#123` issue/PR previews are now **{state}**.", ephemeral=True
        )

    @app_commands.command(name="comment-sync",
                          description="Toggle two-way comment syncing for filed issues")
    @app_commands.describe(enabled="Sync Discord replies/thread posts and GitHub comments")
    @bot_admin()
    async def comment_sync(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        await self.bot.db.set_comment_sync_enabled(interaction.guild.id, enabled)
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"✅ Two-way comment syncing for filed issues is now **{state}**.", ephemeral=True
        )

    @app_commands.command(name="media-repo",
                          description="Set a dedicated repo for hosting Discord attachments (empty to clear)")
    @app_commands.describe(repo="owner/name or URL of a repo (leave empty to host in each repo)")
    @bot_admin()
    async def media_repo(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        assert interaction.guild is not None
        if not repo or not repo.strip():
            await self.bot.db.set_media_repo(interaction.guild.id, None)
            await interaction.response.send_message(
                "✅ Cleared dedicated media repo. Attachments will be hosted in each issue's repo.",
                ephemeral=True,
            )
            return
        try:
            owner, name = parse_repo(repo)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self.bot.github.get_repo(owner, name)
        except NotFound:
            raise UserError(f"Couldn't find `{owner}/{name}` on GitHub. "
                            + access_hint(self.bot, f"{owner}/{name}"))
        canonical = f"{data['owner']['login']}/{data['name']}"
        await self.bot.db.set_media_repo(interaction.guild.id, canonical)
        await interaction.followup.send(
            f"✅ Attachments for this server will be hosted in **{canonical}** (`discord-attachments` branch).",
            ephemeral=True,
        )

    @app_commands.command(name="alerts",
                          description="Set a channel for bot error alerts (leave empty to disable)")
    @app_commands.describe(channel="Private staff channel for error alerts (empty to disable)")
    @bot_admin()
    async def alerts(self, interaction: discord.Interaction,
                     channel: discord.TextChannel | None = None) -> None:
        assert interaction.guild is not None
        if channel is None:
            await self.bot.db.set_alert_channel(interaction.guild.id, None)
            await interaction.response.send_message(
                "✅ Error alert channel cleared.", ephemeral=True
            )
            return
        if missing := missing_channel_perms(channel):
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")
        await self.bot.db.set_alert_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"✅ Bot error alerts will be posted to {channel.mention}.", ephemeral=True
        )


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Admin(bot))
