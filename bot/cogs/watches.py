from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..embeds import truncate
from . import repo_autocomplete, resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot

INTENT_WARNING = (
    "\n⚠️ The bot can't read messages yet. Turn on **Message Content Intent** in the Discord "
    "Developer Portal (your app → **Bot** → Privileged Gateway Intents), then restart the bot."
)


@app_commands.guild_only()
class Watches(commands.GroupCog, group_name="watch",
              group_description="Turn role mentions or forum posts into GitHub issues"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    def _intent_note(self) -> str:
        return "" if self.bot.intents.message_content else INTENT_WARNING

    async def _watch_autocomplete(self, interaction: discord.Interaction,
                                  current: str) -> list[app_commands.Choice[int]]:
        if interaction.guild is None:
            return []
        choices = []
        for w in await self.bot.db.list_watches(interaction.guild.id):
            if w.kind == "role":
                role = interaction.guild.get_role(w.role_id or 0)
                label = f"#{w.id} @{role.name if role else w.role_id} → {w.full_name}"
            else:
                channel = interaction.guild.get_channel(w.channel_id or 0)
                label = f"#{w.id} forum #{channel.name if channel else w.channel_id} → {w.full_name}"
            if w.label:
                label += f" ({w.label})"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=w.id))
        return choices[:25]

    @app_commands.command(name="role", description="File a GitHub issue whenever a message mentions this role")
    @app_commands.describe(
        role="The role that triggers it, e.g. @bug-report (tip: a role nobody has, so nobody is pinged)",
        repo="A tracked repository",
        label="GitHub label to add, e.g. bug or enhancement",
        channel="Only react to mentions in this channel (and its threads)",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def role(self, interaction: discord.Interaction, role: discord.Role, repo: str,
                   label: str | None = None, channel: discord.TextChannel | None = None) -> None:
        assert interaction.guild is not None
        tracked = await resolve_tracked_repo(interaction, repo)
        if role.is_default():
            raise UserError("Pick a specific role, not @everyone.")
        if role.managed:
            raise UserError(f"{role.mention} belongs to an integration and can't be used.")
        for w in await self.bot.db.list_watches(interaction.guild.id, "role"):
            if w.role_id == role.id and w.repo_id == tracked.id:
                raise UserError(f"{role.mention} already files issues in **{tracked.full_name}** (watch #{w.id}).")
        watch = await self.bot.db.add_watch(tracked, "role", role_id=role.id,
                                            channel_id=channel.id if channel else None,
                                            label=label.strip() if label else None,
                                            created_by=interaction.user.id)
        notes = []
        if not role.mentionable:
            notes.append(f"⚠️ {role.mention} isn't mentionable, so only people allowed to mention any "
                         "role can trigger it. To let everyone use it, turn on **Allow anyone to "
                         "@mention this role** in the role's settings.")
        if role.members:
            notes.append(f"ℹ️ {len(role.members)} member(s) have this role and will be pinged each time. "
                         "A role nobody has works as a trigger without pinging anyone.")
        where = f" in {channel.mention}" if channel else ""
        await interaction.response.send_message(
            f"✅ Watch **#{watch.id}**: messages{where} that mention {role.mention} become issues in "
            f"**{tracked.full_name}**" + (f" with label `{watch.label}`" if watch.label else "") + ".\n"
            "Reply to someone's message with the mention to file *their* message.\n"
            + "\n".join(notes) + self._intent_note(),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="forum", description="File a GitHub issue for every new post in a forum channel")
    @app_commands.describe(
        forum="The forum channel to watch",
        repo="A tracked repository",
        label="GitHub label to add to every post's issue (post tags that match labels are added too)",
        close_posts="Close and lock the post when its GitHub issue is closed (default: yes)",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def forum(self, interaction: discord.Interaction, forum: discord.ForumChannel, repo: str,
                    label: str | None = None, close_posts: bool = True) -> None:
        assert interaction.guild is not None
        tracked = await resolve_tracked_repo(interaction, repo)
        for w in await self.bot.db.list_watches(interaction.guild.id, "forum"):
            if w.channel_id == forum.id and w.repo_id == tracked.id:
                raise UserError(f"{forum.mention} already files issues in **{tracked.full_name}** (watch #{w.id}).")
        perms = forum.permissions_for(interaction.guild.me)
        missing = [name for attr, name in (("view_channel", "View Channel"),
                                           ("send_messages_in_threads", "Send Messages in Threads"),
                                           ("read_message_history", "Read Message History"))
                   if not getattr(perms, attr)]
        if missing:
            raise UserError(f"I'm missing {', '.join(missing)} in {forum.mention}.")
        watch = await self.bot.db.add_watch(tracked, "forum", channel_id=forum.id,
                                            label=label.strip() if label else None,
                                            close_threads=close_posts, created_by=interaction.user.id)
        notes = []
        if close_posts and not perms.manage_threads:
            notes.append("⚠️ To close and lock posts, I need **Manage Threads** in that forum.")
        from ..watches import solved_tag
        if close_posts and solved_tag(forum) is None:
            notes.append("ℹ️ Add a tag named **Solved** to the forum and I'll apply it to posts "
                         "whose issue was completed.")
        await interaction.response.send_message(
            f"✅ Watch **#{watch.id}**: new posts in {forum.mention} become issues in "
            f"**{tracked.full_name}**" + (f" with label `{watch.label}`" if watch.label else "")
            + ". Post tags that match a GitHub label are added as labels.\n"
            + "\n".join(notes) + self._intent_note(),
            ephemeral=True)

    @app_commands.command(name="list", description="List watches")
    @bot_admin()
    async def list_(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        watches = await self.bot.db.list_watches(interaction.guild.id)
        if not watches:
            await interaction.response.send_message(
                "No watches yet. Add one with `/watch role` or `/watch forum`." + self._intent_note(),
                ephemeral=True)
            return
        lines = [f"`#{w.id}` {w.describe()}" for w in watches]
        embed = discord.Embed(title="Watches", description=truncate("\n".join(lines), 4000),
                              color=discord.Color.blurple())
        await interaction.response.send_message(self._intent_note().strip() or None, embed=embed,
                                                ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="remove", description="Stop a watch")
    @app_commands.autocomplete(watch=_watch_autocomplete)
    @bot_admin()
    async def remove(self, interaction: discord.Interaction, watch: int) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_watch(interaction.guild.id, watch)
        if found is None:
            raise UserError(f"Watch #{watch} doesn't exist. See `/watch list`.")
        await self.bot.db.remove_watch(found.id)
        await interaction.response.send_message(f"🗑️ Removed watch #{found.id}: {found.describe()}.",
                                                ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Watches(bot))
