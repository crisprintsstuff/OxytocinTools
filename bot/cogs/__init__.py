"""Slash command groups."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..checks import UserError
from ..models import Repo, parse_repo

if TYPE_CHECKING:
    from ..main import TrackerBot


async def repo_autocomplete(interaction: discord.Interaction,
                            current: str) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    bot: TrackerBot = interaction.client  # type: ignore[assignment]
    repos = await bot.db.list_repos(interaction.guild.id)
    current = current.lower()
    return [
        app_commands.Choice(name=r.full_name, value=r.full_name)
        for r in repos if current in r.full_name.lower()
    ][:25]


async def route_autocomplete(interaction: discord.Interaction,
                             current: str) -> list[app_commands.Choice[int]]:
    if interaction.guild is None:
        return []
    bot: TrackerBot = interaction.client  # type: ignore[assignment]
    routes = await bot.db.list_routes(interaction.guild.id)
    choices = []
    for r in routes:
        channel = interaction.guild.get_channel(r.channel_id)
        label = f"#{r.id} {r.full_name} → #{channel.name if channel else r.channel_id} ({r.describe()})"
        if not r.enabled:
            label += " [disabled]"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=r.id))
    return choices[:25]


async def resolve_tracked_repo(interaction: discord.Interaction, value: str) -> Repo:
    assert interaction.guild is not None
    bot: TrackerBot = interaction.client  # type: ignore[assignment]
    try:
        owner, name = parse_repo(value)
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    repo = await bot.db.get_repo(interaction.guild.id, owner, name)
    if repo is None:
        raise UserError(f"`{owner}/{name}` isn't tracked here. Add it with `/repo track` first.")
    return repo


def missing_channel_perms(channel: discord.abc.GuildChannel) -> list[str]:
    me = channel.guild.me
    perms = channel.permissions_for(me)
    needed = {"view_channel": "View Channel", "send_messages": "Send Messages",
              "embed_links": "Embed Links"}
    return [label for attr, label in needed.items() if not getattr(perms, attr)]


class ConfirmView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = False
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()
