from __future__ import annotations

import discord
from discord import app_commands


class NotBotAdmin(app_commands.CheckFailure):
    pass


async def is_admin(interaction: discord.Interaction) -> bool:
    bot = interaction.client
    if interaction.user.id in bot.config.owner_ids:  # type: ignore[attr-defined]
        return True
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    perms = interaction.user.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    role_id = await bot.db.get_admin_role(interaction.guild.id)  # type: ignore[attr-defined]
    return role_id is not None and any(r.id == role_id for r in interaction.user.roles)


def bot_admin():
    """Allow server managers, members with the configured admin role, and bot owners."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        if await is_admin(interaction):
            return True
        raise NotBotAdmin(
            "You need **Manage Server** permission or the bot admin role to use this command."
        )

    return app_commands.check(predicate)


class UserError(app_commands.AppCommandError):
    """An error whose message is safe and useful to show to the user."""


async def can_create_issue(interaction: discord.Interaction, repo) -> tuple[bool, str | None]:
    """Whether the user may open issues in ``repo`` from Discord, and why not if they can't."""
    if interaction.guild is None:
        return False, "This only works in a server."
    bot = interaction.client
    default, role_id = await bot.db.get_issue_settings(interaction.guild.id)  # type: ignore[attr-defined]
    level = repo.issue_access or default
    if level == "disabled":
        return False, f"Creating issues in **{repo.full_name}** is disabled on this server."
    if await is_admin(interaction):
        return True, None
    if level == "everyone":
        return True, None
    if level == "role" and role_id is not None and isinstance(interaction.user, discord.Member) \
            and any(r.id == role_id for r in interaction.user.roles):
        return True, None
    if level == "role" and role_id is not None:
        return False, f"You need the <@&{role_id}> role to create issues in **{repo.full_name}**."
    return False, f"Only bot admins can create issues in **{repo.full_name}**."
