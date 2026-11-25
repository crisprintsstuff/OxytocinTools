from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin, resolve_member
from ..embeds import truncate
from ..models import NotifyPanel
from ..notify import MAX_PANEL_ITEMS, validate_assignable_role, validate_emoji
from . import missing_channel_perms

if TYPE_CHECKING:
    from ..main import TrackerBot


def _jump(panel: NotifyPanel) -> str:
    if not panel.message_id:
        return "*not posted yet*"
    return f"[message](https://discord.com/channels/{panel.guild_id}/{panel.channel_id}/{panel.message_id})"


@app_commands.guild_only()
class Notify(commands.GroupCog, group_name="notify",
             group_description="Self-assignable notification role panels"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    async def _panel_autocomplete(self, interaction: discord.Interaction,
                                  current: str) -> list[app_commands.Choice[int]]:
        if interaction.guild is None:
            return []
        choices = []
        for p in await self.bot.db.list_notify_panels(interaction.guild.id):
            channel = interaction.guild.get_channel(p.channel_id)
            label = f"#{p.id} {p.title} in #{channel.name if channel else p.channel_id}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=p.id))
        return choices[:25]

    @app_commands.command(name="create", description="Post a self-assignable notification role panel")
    @app_commands.describe(
        channel="Channel where the notification role panel will live",
        title="Embed title (default: 🔔 Notification Roles)",
        description="Embed intro text shown above the role list",
        pin="Pin the panel message (needs Manage Messages / Pin Messages)",
    )
    @bot_admin()
    async def create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str = "🔔 Notification Roles",
        description: str = "Click a button below to opt in or out of notification pings.",
        pin: bool = False,
    ) -> None:
        assert interaction.guild is not None
        if missing := missing_channel_perms(channel):
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")
        clean_title = title.strip() or "🔔 Notification Roles"
        clean_desc = description.strip() or "Click a button below to opt in or out of notification pings."

        await interaction.response.defer(ephemeral=True, thinking=True)
        panel = await self.bot.db.add_notify_panel(
            interaction.guild.id,
            channel.id,
            title=clean_title,
            description=clean_desc,
            pinned=pin,
            created_by=interaction.user.id,
        )
        try:
            panel = await self.bot.notify_panels.sync_message(panel)
        except (discord.HTTPException, UserError) as exc:
            await self.bot.db.remove_notify_panel(panel.id)
            raise UserError(f"Couldn't post panel: {exc}") from exc

        note = ""
        perms = channel.permissions_for(channel.guild.me)
        if pin and not (perms.manage_messages or getattr(perms, "pin_messages", False)):
            note = "\n⚠️ Couldn't pin: I need **Manage Messages** (or **Pin Messages**)."
        await interaction.followup.send(
            f"✅ Notification panel **#{panel.id}** posted in {channel.mention} ({_jump(panel)}).\n"
            f"Add roles with `/notify add panel:{panel.id} role:@Role`.{note}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="add", description="Add or update a notification role button on a panel")
    @app_commands.describe(
        panel="The notification panel to update",
        role="Role members can toggle (must be below the bot's highest role)",
        label="Button text (default: role name)",
        emoji="Optional emoji on the button (e.g. 🔔, 🚀)",
        description="Optional short description shown in the panel embed",
    )
    @app_commands.autocomplete(panel=_panel_autocomplete)
    @bot_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        panel: int,
        role: discord.Role,
        label: str | None = None,
        emoji: str | None = None,
        description: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_notify_panel(panel, interaction.guild.id)
        if found is None:
            raise UserError(f"Notification panel #{panel} doesn't exist. See `/notify list`.")

        existing_item = next((it for it in found.items if it.role_id == role.id), None)
        if existing_item is None and len(found.items) >= MAX_PANEL_ITEMS:
            raise UserError(f"Discord allows at most {MAX_PANEL_ITEMS} buttons per panel.")

        actor = await resolve_member(interaction.guild, interaction.user)
        validate_assignable_role(interaction.guild, role, actor=actor)
        clean_emoji = validate_emoji(emoji)
        clean_label = truncate((label.strip() if label and label.strip() else role.name), 80)
        clean_desc = truncate(description.strip(), 200) if description and description.strip() else None

        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await self.bot.db.add_notify_panel_item(
            found.id, role.id, clean_label, emoji=clean_emoji, description=clean_desc,
        )
        assert updated is not None
        try:
            updated = await self.bot.notify_panels.sync_message(updated)
        except discord.HTTPException as exc:
            if existing_item is None:
                await self.bot.db.remove_notify_panel_item(found.id, role.id)
            else:
                await self.bot.db.add_notify_panel_item(
                    found.id, existing_item.role_id, existing_item.label,
                    emoji=existing_item.emoji, description=existing_item.description,
                )
            raise UserError(f"Discord rejected the panel update: {exc.text or exc}") from exc

        verb = "Updated" if existing_item else "Added"
        await interaction.followup.send(
            f"✅ {verb} {role.mention} on panel **#{updated.id}** ({_jump(updated)}).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="remove-role", description="Remove a notification role button from a panel")
    @app_commands.describe(panel="The notification panel to edit", role="Role button to remove")
    @app_commands.autocomplete(panel=_panel_autocomplete)
    @bot_admin()
    async def remove_role(self, interaction: discord.Interaction, panel: int, role: discord.Role) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_notify_panel(panel, interaction.guild.id)
        if found is None:
            raise UserError(f"Notification panel #{panel} doesn't exist. See `/notify list`.")
        if not await self.bot.db.remove_notify_panel_item(found.id, role.id):
            raise UserError(f"{role.mention} isn't on panel #{found.id}.")

        refreshed = await self.bot.db.get_notify_panel(found.id, interaction.guild.id)
        assert refreshed is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.notify_panels.sync_message(refreshed)
        await interaction.followup.send(
            f"🗑️ Removed {role.mention} from panel **#{refreshed.id}** ({_jump(refreshed)}).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="edit", description="Edit a notification panel's title, description, or pin setting")
    @app_commands.describe(
        panel="The notification panel to edit",
        title="New embed title",
        description="New intro description",
        pin="Pin or unpin the panel message",
    )
    @app_commands.autocomplete(panel=_panel_autocomplete)
    @bot_admin()
    async def edit(
        self,
        interaction: discord.Interaction,
        panel: int,
        title: str | None = None,
        description: str | None = None,
        pin: bool | None = None,
    ) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_notify_panel(panel, interaction.guild.id)
        if found is None:
            raise UserError(f"Notification panel #{panel} doesn't exist. See `/notify list`.")
        if title is None and description is None and pin is None:
            raise UserError("Pick at least one option to change.")

        new_title = title.strip() if title is not None and title.strip() else found.title
        new_desc = description.strip() if description is not None and description.strip() else found.description
        new_pin = found.pinned if pin is None else pin

        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await self.bot.db.update_notify_panel(
            found.id, interaction.guild.id, title=new_title, description=new_desc, pinned=new_pin,
        )
        assert updated is not None
        updated = await self.bot.notify_panels.sync_message(updated)

        if pin is not None and pin != found.pinned and updated.message_id:
            ch = interaction.guild.get_channel(updated.channel_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    msg = ch.get_partial_message(updated.message_id)
                    if pin:
                        await msg.pin(reason="Notification role panel")
                    else:
                        await msg.unpin(reason="Notification role panel unpinned")
                except discord.HTTPException:
                    pass

        await interaction.followup.send(
            f"✅ Updated panel **#{updated.id}** ({_jump(updated)}).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="list", description="List notification role panels in this server")
    @bot_admin()
    async def list_(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        panels = await self.bot.db.list_notify_panels(interaction.guild.id)
        if not panels:
            await interaction.response.send_message(
                "No notification panels yet. Create one with `/notify create`.", ephemeral=True
            )
            return
        lines = []
        for p in panels:
            roles_summary = ", ".join(f"<@&{it.role_id}>" for it in p.items) or "*no roles yet*"
            lines.append(f"`#{p.id}` {p.describe()} — {_jump(p)}\n  Roles: {roles_summary}")
        embed = discord.Embed(
            title="Notification role panels",
            description=truncate("\n".join(lines), 4000),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="delete", description="Delete a notification role panel")
    @app_commands.describe(
        panel="The notification panel to delete",
        delete_message="Also delete its message in Discord (default: true)",
    )
    @app_commands.autocomplete(panel=_panel_autocomplete)
    @bot_admin()
    async def delete(
        self, interaction: discord.Interaction, panel: int, delete_message: bool = True
    ) -> None:
        assert interaction.guild is not None
        found = await self.bot.db.get_notify_panel(panel, interaction.guild.id)
        if found is None:
            raise UserError(f"Notification panel #{panel} doesn't exist. See `/notify list`.")
        removed_msg = await self.bot.notify_panels.delete_message(found) if delete_message else False
        await self.bot.db.remove_notify_panel(found.id)
        extra = " and deleted its message" if removed_msg else ""
        await interaction.response.send_message(
            f"🗑️ Removed notification panel **#{found.id}**{extra}.", ephemeral=True
        )


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Notify(bot))
