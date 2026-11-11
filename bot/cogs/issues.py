from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, can_create_issue, is_admin
from ..embeds import GREEN, truncate
from ..github import GitHubError, NotFound
from ..models import Repo
from . import resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"(?<![\w@])@(?=[A-Za-z0-9])")


def neutralize_mentions(text: str) -> str:
    """Stop ``@name`` in Discord text from pinging GitHub users (adds a zero-width space)."""
    return _MENTION_RE.sub("@​", text)


def format_issue_body(body: str, *, reporter: str, reporter_username: str, guild: str,
                      channel: str | None, message_url: str | None = None,
                      message_author: str | None = None,
                      attachments: list[tuple[str, str]] | None = None) -> str:
    """Build the GitHub issue body: the user's text plus a footer saying where it came from."""
    parts = [body.strip() or "_No description provided._"]
    if attachments:
        lines = [f"- [{name}]({url})" for name, url in attachments]
        parts.append("**Attachments** (Discord links may expire):\n" + "\n".join(lines))
    where = f"**{guild}**" + (f" #{channel}" if channel else "")
    footer = f"Reported from Discord by **{reporter}** (`{reporter_username}`) in {where}"
    if message_author and message_url:
        footer += f". [Original message]({message_url}) by **{message_author}**"
    elif message_url:
        footer += f". [Original message]({message_url})"
    parts.append("---\n<sub>" + neutralize_mentions(footer) + "</sub>")
    return "\n\n".join(parts)


def _title_from(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return truncate(first.lstrip("#>*-_ ").strip(), 100)


class IssueModal(discord.ui.Modal):
    def __init__(self, cog: "Issues", repo: Repo, *, title: str | None = None,
                 body: str | None = None, source: discord.Message | None = None):
        super().__init__(title=truncate(f"New issue: {repo.full_name}", 45))
        self.cog, self.repo, self.source = cog, repo, source
        self.issue_title = discord.ui.TextInput(label="Title", max_length=256, default=title or None)
        self.body = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=False,
            max_length=4000, default=truncate(body, 4000) if body else None,
            placeholder="What happened? What did you expect? Steps to reproduce?",
        )
        self.labels = discord.ui.TextInput(
            label="Labels (optional, comma-separated)", required=False, max_length=200,
            placeholder="bug, ui",
        )
        for item in (self.issue_title, self.body, self.labels):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit(interaction, self.repo, self.issue_title.value, self.body.value,
                              self.labels.value, self.source)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.client.on_app_command_error(interaction, error)  # type: ignore[attr-defined]


class RepoPicker(discord.ui.View):
    """Shown when creating an issue from a message and several repos are allowed."""

    def __init__(self, cog: "Issues", user_id: int, repos: list[Repo], source: discord.Message):
        super().__init__(timeout=120)
        self.cog, self.user_id, self.source = cog, user_id, source
        self.repos = {str(r.id): r for r in repos[:25]}
        select = discord.ui.Select(
            placeholder="Which repository?",
            options=[discord.SelectOption(label=r.full_name[:100], value=str(r.id))
                     for r in self.repos.values()],
        )
        select.callback = self._picked
        self.add_item(select)
        self.select = select

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _picked(self, interaction: discord.Interaction) -> None:
        repo = self.repos[self.select.values[0]]
        await interaction.response.send_modal(self.cog.modal_for_message(repo, self.source))
        self.stop()


class Issues(commands.GroupCog, group_name="issue",
             group_description="Create GitHub issues from Discord"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        self._last_issue: dict[tuple[int, int], float] = {}
        self.ctx_menu = app_commands.ContextMenu(name="Create GitHub issue",
                                                 callback=self.from_message)
        app_commands.guild_only(self.ctx_menu)
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # ------------------------------------------------------------------ helpers
    async def allowed_repos(self, interaction: discord.Interaction) -> list[Repo]:
        assert interaction.guild is not None
        repos = await self.bot.db.list_repos(interaction.guild.id)
        return [r for r in repos if (await can_create_issue(interaction, r))[0]]

    async def check_cooldown(self, interaction: discord.Interaction) -> None:
        cooldown = self.bot.config.issue_cooldown
        if not cooldown or await is_admin(interaction):
            return
        key = (interaction.guild_id or 0, interaction.user.id)
        remaining = self._last_issue.get(key, 0) + cooldown - time.monotonic()
        if remaining > 0:
            raise UserError(f"Slow down! You can create another issue in {int(remaining) + 1}s.")

    def modal_for_message(self, repo: Repo, message: discord.Message) -> IssueModal:
        content = message.clean_content
        return IssueModal(self, repo, title=_title_from(content), body=content, source=message)

    async def submit(self, interaction: discord.Interaction, repo: Repo, title: str, body: str,
                     labels_raw: str, source: discord.Message | None) -> None:
        # Re-check: settings or roles may have changed while the form was open.
        allowed, reason = await can_create_issue(interaction, repo)
        if not allowed:
            raise UserError(reason or "You can't create issues here.")
        await self.check_cooldown(interaction)
        title = title.strip()
        if not title:
            raise UserError("The issue needs a title.")
        await interaction.response.defer(ephemeral=True, thinking=True)

        admin = await is_admin(interaction)
        user = interaction.user
        channel = getattr(interaction.channel, "name", None)
        full_body = format_issue_body(
            body if admin else neutralize_mentions(body),
            reporter=user.display_name, reporter_username=user.name,
            guild=interaction.guild.name if interaction.guild else "Discord", channel=channel,
            message_url=source.jump_url if source else None,
            message_author=source.author.display_name if source else None,
            attachments=[(a.filename, a.url) for a in source.attachments] if source else None,
        )
        labels = [label.strip() for label in labels_raw.split(",") if label.strip()][:10]

        try:
            issue = await self.bot.github.create_issue(repo.owner, repo.name, title, full_body, labels)
        except NotFound:
            raise UserError(f"The bot's GitHub account can't see **{repo.full_name}**. "
                            "Its token needs access to this repository.")
        except GitHubError as exc:
            if exc.status == 410:
                raise UserError(f"Issues are turned off for **{repo.full_name}** on GitHub.")
            if exc.status == 403:
                raise UserError(f"The bot's GitHub token isn't allowed to create issues in "
                                f"**{repo.full_name}**. It needs *Issues: Read and write* access.")
            raise
        self._last_issue[(interaction.guild_id or 0, user.id)] = time.monotonic()
        log.info("User %s created issue %s#%s", user.id, repo.full_name, issue["number"])

        embed = discord.Embed(title=truncate(f"#{issue['number']} {issue['title']}", 256),
                              url=issue["html_url"], color=GREEN,
                              description=f"Created in **{repo.full_name}**.")
        ignored = set(labels) - {lbl["name"] for lbl in issue.get("labels", [])}
        if ignored:
            embed.set_footer(text="GitHub didn't apply some labels: " + ", ".join(sorted(ignored)))
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _repo_autocomplete(self, interaction: discord.Interaction,
                                 current: str) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        return [app_commands.Choice(name=r.full_name, value=r.full_name)
                for r in await self.allowed_repos(interaction)
                if current.lower() in r.full_name.lower()][:25]

    # ----------------------------------------------------------------- commands
    @app_commands.command(name="create", description="Open a new GitHub issue in a tracked repository")
    @app_commands.describe(repo="A tracked repository")
    @app_commands.guild_only()
    async def create(self, interaction: discord.Interaction, repo: str) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        allowed, reason = await can_create_issue(interaction, tracked)
        if not allowed:
            raise UserError(reason or "You can't create issues here.")
        await self.check_cooldown(interaction)
        await interaction.response.send_modal(IssueModal(self, tracked))

    @create.autocomplete("repo")
    async def _create_repo_ac(self, interaction: discord.Interaction, current: str):
        return await self._repo_autocomplete(interaction, current)

    async def from_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        repos = await self.allowed_repos(interaction)
        if not repos:
            raise UserError("There are no repositories you can create issues in on this server.")
        await self.check_cooldown(interaction)
        if len(repos) == 1:
            await interaction.response.send_modal(self.modal_for_message(repos[0], message))
            return
        await interaction.response.send_message(
            "Create an issue from this message in which repository?",
            view=RepoPicker(self, interaction.user.id, repos, message), ephemeral=True)


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Issues(bot))
