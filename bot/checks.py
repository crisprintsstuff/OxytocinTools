from __future__ import annotations

import discord
from discord import app_commands


class NotBotAdmin(app_commands.CheckFailure):
    pass


async def member_is_admin(bot, guild: discord.Guild | None, user: discord.abc.User) -> bool:
    """Bot owners, members with Manage Server/Administrator, or the bot admin role."""
    if user.id in bot.config.owner_ids:
        return True
    if guild is None or not isinstance(user, discord.Member):
        return False
    perms = user.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    role_id = await bot.db.get_admin_role(guild.id)
    return role_id is not None and any(r.id == role_id for r in user.roles)


async def is_admin(interaction: discord.Interaction) -> bool:
    return await member_is_admin(interaction.client, interaction.guild, interaction.user)


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


async def member_can_create_issue(bot, guild: discord.Guild | None, user: discord.abc.User,
                                  repo) -> tuple[bool, str | None]:
    """Whether ``user`` may open issues in ``repo`` from Discord, and why not if they can't."""
    if guild is None:
        return False, "This only works in a server."
    default, role_id = await bot.db.get_issue_settings(guild.id)
    level = repo.issue_access or default
    if level == "disabled":
        return False, f"Creating issues in **{repo.full_name}** is disabled on this server."
    if await member_is_admin(bot, guild, user):
        return True, None
    if level == "everyone":
        return True, None
    if level == "role" and role_id is not None and isinstance(user, discord.Member) \
            and any(r.id == role_id for r in user.roles):
        return True, None
    if level == "role" and role_id is not None:
        return False, f"You need the <@&{role_id}> role to create issues in **{repo.full_name}**."
    return False, f"Only bot admins can create issues in **{repo.full_name}**."


async def can_create_issue(interaction: discord.Interaction, repo) -> tuple[bool, str | None]:
    return await member_can_create_issue(interaction.client, interaction.guild,
                                         interaction.user, repo)
