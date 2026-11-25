"""Self-assignable notification role panels with persistent Discord toggle buttons."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import discord

from .checks import UserError, resolve_member
from .embeds import BLUE, truncate
from .models import NotifyPanel

if TYPE_CHECKING:
    from .main import TrackerBot

log = logging.getLogger(__name__)

MAX_PANEL_ITEMS = 25
DANGEROUS_PERMS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "moderate_members",
)

_CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_~]+:\d+>$")


def validate_assignable_role(
    guild: discord.Guild,
    role: discord.Role,
    *,
    actor: discord.Member | None = None,
) -> None:
    """Validate that ``role`` is safe and assignable by the bot (and optionally ``actor``)."""
    if role.is_default():
        raise UserError("`@everyone` cannot be used as a notification role.")
    if role.managed:
        raise UserError(f"{role.mention} is managed by an integration/bot and can't be self-assigned.")
    perms = role.permissions
    if any(getattr(perms, name, False) for name in DANGEROUS_PERMS):
        raise UserError(
            "That role has server moderation/admin permissions and can't be self-assigned. "
            "Pick a plain notification role."
        )

    me = guild.me
    if me is not None:
        if not me.guild_permissions.manage_roles:
            raise UserError("I need the **Manage Roles** permission to assign notification roles.")
        if role >= me.top_role:
            raise UserError(
                f"My highest role must be **above** {role.mention} in Server Settings → Roles "
                f"so I can assign it."
            )

    if actor is not None and actor.id != guild.owner_id:
        if role >= actor.top_role:
            raise UserError(
                f"You can only add roles below your own highest role ({actor.top_role.mention})."
            )


def validate_emoji(emoji: str | None) -> str | None:
    """Validate a button emoji string (Unicode or custom Discord emoji) or return ``None``."""
    if emoji is None:
        return None
    cleaned = emoji.strip()
    if not cleaned:
        return None
    if _CUSTOM_EMOJI_RE.match(cleaned):
        return cleaned
    if cleaned.isascii() or len(cleaned) > 16:
        raise UserError("Emoji must be a standard Unicode emoji (e.g. 🔔, 🚀) or a custom server emoji.")
    return cleaned


def build_panel_embed(panel: NotifyPanel) -> discord.Embed:
    """Build the embed for a notification role picker panel."""
    lines = [panel.description.strip()] if panel.description.strip() else []
    if panel.items:
        item_lines = []
        for it in panel.items:
            prefix = f"{it.emoji} " if it.emoji else ""
            if it.description:
                item_lines.append(f"• {prefix}<@&{it.role_id}> — {it.description}")
            else:
                item_lines.append(f"• {prefix}<@&{it.role_id}>")
        if lines:
            lines.append("")
        lines.extend(item_lines)
    else:
        if lines:
            lines.append("")
        lines.append("*(No notification roles added yet — an admin can add them with `/notify add`.)*")

    embed = discord.Embed(
        title=truncate(panel.title, 256),
        description=truncate("\n".join(lines), 4096),
        color=BLUE,
    )
    embed.set_footer(text="Click a button below to toggle a role · Click again to opt out")
    return embed


class NotifyRoleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nr:toggle:(?P<panel_id>\d+):(?P<role_id>\d+)",
):
    """Persistent toggle button for one self-assignable notification role."""

    def __init__(self, panel_id: int, role_id: int, *, label: str = "Role",
                 emoji: str | discord.PartialEmoji | None = None):
        super().__init__(
            discord.ui.Button(
                label=truncate(label, 80),
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"nr:toggle:{panel_id}:{role_id}",
            )
        )
        self.panel_id = panel_id
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button, match: re.Match[str]):  # type: ignore[override]
        return cls(
            int(match.group("panel_id")),
            int(match.group("role_id")),
            label=item.label or "Role",
            emoji=item.emoji,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        manager = getattr(interaction.client, "notify_panels", None)
        if manager is None:
            raise UserError("Notification panels aren't initialized yet.")
        await manager.handle_toggle(interaction, self.panel_id, self.role_id)


def panel_view(panel: NotifyPanel) -> discord.ui.View | None:
    """Build the persistent toggle-button View for ``panel``, or ``None`` if empty."""
    if not panel.items:
        return None
    view = discord.ui.View(timeout=None)
    for it in panel.items[:MAX_PANEL_ITEMS]:
        view.add_item(NotifyRoleButton(panel.id, it.role_id, label=it.label, emoji=it.emoji))
    return view


class NotifyPanelManager:
    """Manages posting, updating, and role toggling for notification role panels."""

    def __init__(self, bot: TrackerBot):
        self.bot = bot
        self._lock = asyncio.Lock()

    async def _resolve_channel(self, channel_id: int) -> discord.TextChannel | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def sync_message(self, panel: NotifyPanel) -> NotifyPanel:
        """Create or update the Discord message backing ``panel``."""
        async with self._lock:
            channel = await self._resolve_channel(panel.channel_id)
            if channel is None:
                raise UserError("Target channel is missing, inaccessible, or not a text channel.")

            embed = build_panel_embed(panel)
            view = panel_view(panel)

            if panel.message_id:
                try:
                    await channel.get_partial_message(panel.message_id).edit(embed=embed, view=view)
                    return panel
                except discord.NotFound:
                    pass

            msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.bot.db.set_notify_panel_message(panel.id, msg.id)
            panel.message_id = msg.id
            if panel.pinned:
                try:
                    await msg.pin(reason="Notification role panel")
                except discord.HTTPException as exc:
                    log.warning("Couldn't pin notify panel %s: %s", panel.id, exc)
            return panel

    async def delete_message(self, panel: NotifyPanel) -> bool:
        """Best-effort delete ``panel``'s Discord message."""
        if not panel.message_id:
            return False
        channel = await self._resolve_channel(panel.channel_id)
        if channel is None:
            return False
        try:
            await channel.get_partial_message(panel.message_id).delete()
            return True
        except discord.HTTPException:
            return False

    async def message_deleted(self, message_id: int) -> None:
        """Re-post a panel automatically if its Discord message is deleted."""
        panel = await self.bot.db.find_notify_panel_by_message(message_id)
        if panel is None:
            return
        await self.bot.db.set_notify_panel_message(panel.id, None)
        panel.message_id = None
        log.info("Notify panel %s message %s deleted; re-posting", panel.id, message_id)
        try:
            await self.sync_message(panel)
        except (discord.HTTPException, UserError) as exc:
            log.warning("Couldn't re-post notify panel %s: %s", panel.id, exc)

    async def role_deleted(self, guild_id: int, role_id: int) -> None:
        """Remove a deleted Discord role from any guild panels and refresh their messages."""
        panel_ids = await self.bot.db.remove_notify_role_across_guild(guild_id, role_id)
        for pid in panel_ids:
            panel = await self.bot.db.get_notify_panel(pid, guild_id)
            if panel is None:
                continue
            try:
                await self.sync_message(panel)
            except (discord.HTTPException, UserError) as exc:
                log.warning("Couldn't refresh notify panel %s after role delete: %s", pid, exc)

    async def handle_toggle(
        self, interaction: discord.Interaction, panel_id: int, role_id: int
    ) -> None:
        """Toggle ``role_id`` on the clicking member if registered on ``panel_id``."""
        guild = interaction.guild
        if guild is None:
            raise UserError("Notification roles only work inside a server.")

        panel = await self.bot.db.get_notify_panel(panel_id, guild.id)
        if panel is None or not any(it.role_id == role_id for it in panel.items):
            raise UserError("That notification role is no longer configured on this panel.")

        role = guild.get_role(role_id)
        if role is None:
            raise UserError("That notification role no longer exists in this server.")
        if role.is_default() or role.managed or any(
            getattr(role.permissions, perm, False) for perm in DANGEROUS_PERMS
        ):
            raise UserError("That role can no longer be self-assigned for safety reasons.")

        member = await resolve_member(guild, interaction.user)
        if member is None:
            raise UserError("Couldn't verify your server membership.")

        has_role = any(r.id == role.id for r in member.roles)
        try:
            if has_role:
                await member.remove_roles(role, reason=f"Opted out via notify panel #{panel.id}")
                toggled_in = False
            else:
                await member.add_roles(role, reason=f"Opted in via notify panel #{panel.id}")
                toggled_in = True
        except discord.Forbidden as exc:
            raise UserError(
                f"I don't have permission to update {role.mention}. "
                f"Make sure I have **Manage Roles** and my highest role is above {role.mention}."
            ) from exc
        except discord.HTTPException as exc:
            raise UserError(f"Discord couldn't update your role: {exc.text or exc}") from exc

        panel_role_ids = {it.role_id for it in panel.items}
        member_role_ids = {r.id for r in member.roles if r.id in panel_role_ids}
        if toggled_in:
            member_role_ids.add(role.id)
        else:
            member_role_ids.discard(role.id)

        active = [
            f"<@&{it.role_id}>"
            for it in panel.items
            if it.role_id in member_role_ids
        ]
        summary = ", ".join(active) if active else "None"
        headline = (
            f"🔔 Added {role.mention} — you'll now be pinged for this!"
            if toggled_in
            else f"🔕 Removed {role.mention} — you won't be pinged for this anymore."
        )
        await interaction.response.send_message(
            f"{headline}\n**Active pings from this panel:** {summary}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
