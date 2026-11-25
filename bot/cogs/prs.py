from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..embeds import truncate
from ..models import validate_pattern
from . import missing_channel_perms, repo_autocomplete, resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot


@app_commands.guild_only()
class PullRequests(commands.GroupCog, group_name="pr",
                   group_description="Live cards for open pull requests"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    async def _tracker_autocomplete(self, interaction: discord.Interaction,
                                    current: str) -> list[app_commands.Choice[int]]:
        if interaction.guild is None:
            return []
        choices = []
        for t in await self.bot.db.list_pr_trackers(interaction.guild.id):
            channel = interaction.guild.get_channel(t.channel_id)
            label = f"#{t.id} {t.full_name} in #{channel.name if channel else t.channel_id}"
            if t.branch:
                label += f" (into {t.branch})"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=t.id))
        return choices[:25]

    @app_commands.command(name="track", description="Post a live card for each open pull request")
    @app_commands.describe(
        repo="A tracked repository",
        channel="Where to post the cards",
        branch="Only PRs into this branch, e.g. main (globs like release/* work)",
        include_drafts="Also make cards for draft PRs (default: only once they're ready)",
        include_bots="Also make cards for PRs from bots like Dependabot (default: no)",
        threads="Open a thread under each card with a line for every change (default: yes)",
        mention="Role to ping when a new card is posted",
        cleanup_days="Delete merged/closed PR cards after N days (0 = keep forever)",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def track(self, interaction: discord.Interaction, repo: str, channel: discord.TextChannel,
                    branch: str | None = None, include_drafts: bool = False,
                    include_bots: bool = False, threads: bool = True,
                    mention: discord.Role | None = None,
                    cleanup_days: app_commands.Range[int, 0, 365] = 0) -> None:
        assert interaction.guild is not None
        tracked = await resolve_tracked_repo(interaction, repo)
        branch = branch.strip() if branch else None
        if branch:
            try:
                validate_pattern(branch)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
        missing = missing_channel_perms(channel)
        perms = channel.permissions_for(interaction.guild.me)
        if threads:
            missing += [name for attr, name in (("create_public_threads", "Create Public Threads"),
                                                ("send_messages_in_threads", "Send Messages in Threads"))
                        if not getattr(perms, attr)]
        if missing:
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")
        if mention and mention.is_default():
            raise UserError("Pinging @everyone isn't supported; pick a role.")
        for t in await self.bot.db.list_pr_trackers(interaction.guild.id):
            if (t.repo_id, t.channel_id, t.branch) == (tracked.id, channel.id, branch):
                raise UserError(f"{channel.mention} already has cards for **{tracked.full_name}** (#{t.id}).")

        await interaction.response.defer(ephemeral=True, thinking=True)
        tracker = await self.bot.db.add_pr_tracker(
            tracked, channel.id, branch=branch, include_drafts=include_drafts,
            include_bots=include_bots, threads=threads,
            mention_role_id=mention.id if mention else None, created_by=interaction.user.id,
            cleanup_days=cleanup_days)
        await self.bot.prcards.sync(tracker)
        cards = await self.bot.db.list_pr_cards(tracker.id)
        tracker = await self.bot.db.get_pr_tracker(tracker.id) or tracker
        notes = []
        if tracker.last_error:
            notes.append(f"⚠️ {tracker.last_error}")
        if self.bot.prcards.checks_blocked(tracked.full_name):
            notes.append("⚠️ I can't read CI results yet: the bot's GitHub App needs the **Checks** and "
                         "**Commit statuses** permissions (read-only). Cards show the rest meanwhile.")
        await interaction.followup.send(
            f"✅ PR cards **#{tracker.id}**: {tracker.describe()}. Posted {len(cards)} card(s) for open "
            "PRs; new ones get a card as they open, and all of them update on every check."
            + ("\n" + "\n".join(notes) if notes else ""),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="list", description="List PR card trackers")
    @bot_admin()
    async def list_(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        trackers = await self.bot.db.list_pr_trackers(interaction.guild.id)
        if not trackers:
            await interaction.response.send_message("No PR cards yet. Start with `/pr track`.",
                                                    ephemeral=True)
            return
        lines = []
        for t in trackers:
            cards = await self.bot.db.list_pr_cards(t.id)
            open_cards = sum(c.state == "open" for c in cards)
            lines.append(f"`#{t.id}` {t.describe()} · {open_cards} open PR card(s)")
            if t.last_error:
                lines.append(f"  ⚠️ {truncate(t.last_error, 150)}")
        embed = discord.Embed(title="PR cards", description=truncate("\n".join(lines), 4000),
                              color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="edit", description="Edit an existing PR card tracker")
    @app_commands.describe(
        tracker="The PR card tracker to edit",
        channel="New channel for PR cards",
        branch="Only PRs into this branch (glob or re:...)",
        clear_branch="Watch PRs into any branch",
        include_drafts="Also make cards for draft PRs",
        include_bots="Also make cards for PRs from bots",
        threads="Open a timeline thread under each card",
        mention="Role to ping when a new card is posted",
        clear_mention="Remove the mention role",
        cleanup_days="Delete merged/closed PR cards after N days (0 = keep forever)",
    )
    @app_commands.autocomplete(tracker=_tracker_autocomplete)
    @bot_admin()
    async def edit(self, interaction: discord.Interaction, tracker: int,
                   channel: discord.TextChannel | None = None,
                   branch: str | None = None, clear_branch: bool = False,
                   include_drafts: bool | None = None, include_bots: bool | None = None,
                   threads: bool | None = None, mention: discord.Role | None = None,
                   clear_mention: bool = False,
                   cleanup_days: app_commands.Range[int, 0, 365] | None = None) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_pr_tracker(tracker, interaction.guild.id)
        if found is None:
            raise UserError(f"PR card tracker #{tracker} doesn't exist. See `/pr list`.")
        if clear_branch and branch is not None:
            raise UserError("Choose either `branch` or `clear_branch`, not both.")
        if clear_mention and mention is not None:
            raise UserError("Choose either `mention` or `clear_mention`, not both.")
        if mention and mention.is_default():
            raise UserError("Pinging @everyone isn't supported; pick a role.")

        new_branch = None if clear_branch else (
            (branch.strip() or None) if branch is not None else found.branch
        )
        if new_branch:
            try:
                validate_pattern(new_branch)
            except ValueError as exc:
                raise UserError(str(exc)) from exc

        new_threads = found.threads if threads is None else threads
        new_channel_id = channel.id if channel else found.channel_id
        target_ch = channel or interaction.guild.get_channel(new_channel_id)
        if isinstance(target_ch, discord.TextChannel) and (channel is not None or new_threads):
            missing = missing_channel_perms(target_ch) if channel is not None else []
            perms = target_ch.permissions_for(interaction.guild.me)
            if new_threads:
                missing += [name for attr, name in (("create_public_threads", "Create Public Threads"),
                                                    ("send_messages_in_threads", "Send Messages in Threads"))
                            if not getattr(perms, attr)]
            if missing:
                raise UserError(f"I'm missing {', '.join(missing)} in {target_ch.mention}.")

        for other in await self.bot.db.list_pr_trackers(interaction.guild.id):
            if other.id != found.id and (other.repo_id, other.channel_id, other.branch) == (
                found.repo_id, new_channel_id, new_branch
            ):
                raise UserError(f"<#{new_channel_id}> already has cards for **{found.full_name}** (#{other.id}).")

        new_drafts = found.include_drafts if include_drafts is None else include_drafts
        new_bots = found.include_bots if include_bots is None else include_bots
        new_mention_id = None if clear_mention else (mention.id if mention else found.mention_role_id)

        new_cleanup = found.cleanup_days if cleanup_days is None else cleanup_days

        updated = await self.bot.db.update_pr_tracker(
            found.id, interaction.guild.id,
            channel_id=new_channel_id, branch=new_branch,
            include_drafts=new_drafts, include_bots=new_bots,
            threads=new_threads, mention_role_id=new_mention_id,
            cleanup_days=new_cleanup,
        )
        assert updated is not None
        await interaction.response.send_message(
            f"✅ Updated PR cards **#{updated.id}**: {updated.describe()}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="stop", description="Stop updating PR cards (the cards stay)")
    @app_commands.autocomplete(tracker=_tracker_autocomplete)
    @bot_admin()
    async def stop(self, interaction: discord.Interaction, tracker: int) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_pr_tracker(tracker, interaction.guild.id)
        if found is None:
            raise UserError(f"PR card tracker #{tracker} doesn't exist. See `/pr list`.")
        await self.bot.db.remove_pr_tracker(found.id)
        await interaction.response.send_message(
            f"🛑 Stopped PR cards #{found.id} ({found.full_name}). Existing cards stay as they are.",
            ephemeral=True)


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(PullRequests(bot))
