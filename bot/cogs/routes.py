from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin
from ..embeds import truncate
from ..github import GitHubError, NotFound
from ..models import EVENT_LABELS, PR_EVENTS, parse_pr_events, validate_pattern
from ..poller import current_pr_event
from . import missing_channel_perms, repo_autocomplete, resolve_tracked_repo, route_autocomplete

if TYPE_CHECKING:
    from ..main import TrackerBot
    from ..models import Route

EVENT_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in EVENT_LABELS.items()]
KIND_CHOICES = [
    app_commands.Choice(name="All releases", value="all"),
    app_commands.Choice(name="Stable releases only", value="stable"),
    app_commands.Choice(name="Pre-releases only", value="prerelease"),
]


@app_commands.guild_only()
class Routes(commands.GroupCog, group_name="route",
             group_description="Choose which repository events go to which channels"):
    def __init__(self, bot: TrackerBot):
        self.bot = bot
        super().__init__()

    async def _get(self, interaction: discord.Interaction, route_id: int) -> Route:
        assert interaction.guild is not None
        route = await self.bot.db.get_route(interaction.guild.id, route_id)
        if route is None:
            raise UserError(f"Route #{route_id} doesn't exist. See `/route list`.")
        return route

    @app_commands.command(name="add", description="Send a repository's events to a channel")
    @app_commands.describe(
        repo="A tracked repository",
        channel="Channel to post in",
        event="What to post",
        release_kind="Releases only: stable, pre-releases, or both",
        tag_pattern="Releases only: tag filter, e.g. v2.* (glob) or re:^v\\d+\\.\\d+\\.\\d+$ (regex)",
        branch="Commits: branch to watch (default: the repo's default branch). "
               "Pull requests: only PRs into this branch, e.g. main",
        mention="Role to ping with each post",
        publish="Auto-publish posts when the channel is an announcement channel",
        pr_events="Pull requests only: which events to post (default: opened, ready)",
        include_drafts="Pull requests only: also post about draft PRs (default: no)",
        include_bots="Pull requests only: also post about PRs from bots like Dependabot (default: no)",
    )
    @app_commands.choices(event=EVENT_CHOICES, release_kind=KIND_CHOICES)
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def add(self, interaction: discord.Interaction, repo: str, channel: discord.TextChannel,
                  event: app_commands.Choice[str],
                  release_kind: app_commands.Choice[str] | None = None,
                  tag_pattern: str | None = None, branch: str | None = None,
                  mention: discord.Role | None = None, publish: bool = False,
                  pr_events: str | None = None, include_drafts: bool | None = None,
                  include_bots: bool | None = None) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        event_type = event.value

        if event_type != "releases" and (release_kind or tag_pattern):
            raise UserError("`release_kind` and `tag_pattern` only apply to release routes.")
        if event_type not in ("commits", "pulls") and branch:
            raise UserError("`branch` only applies to commit and pull request routes.")
        if event_type != "pulls" and (pr_events or include_drafts is not None
                                      or include_bots is not None):
            raise UserError("`pr_events`, `include_drafts` and `include_bots` only apply to "
                            "pull request routes.")
        if event_type == "pulls":
            try:
                events = parse_pr_events(pr_events)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
            pr_events = ",".join(events)
            if branch:
                try:
                    validate_pattern(branch)
                except ValueError as exc:
                    raise UserError(str(exc)) from exc
        if tag_pattern:
            try:
                validate_pattern(tag_pattern)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
        if missing := missing_channel_perms(channel):
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")
        if publish and not channel.is_news():
            raise UserError(f"{channel.mention} isn't an announcement channel, so posts can't be published.")
        if mention and mention.is_default():
            raise UserError("Pinging @everyone isn't supported; pick a role.")

        kind = release_kind.value if release_kind else "all"
        if event_type == "commits":
            branch = branch or tracked.default_branch

        for existing in await self.bot.db.list_routes(tracked.guild_id, tracked.id):
            if (existing.channel_id, existing.event_type, existing.release_kind,
                    existing.tag_pattern, existing.branch, existing.pr_events,
                    existing.include_drafts, existing.include_bots) == (
                    channel.id, event_type, kind, tag_pattern, branch, pr_events,
                    bool(include_drafts), bool(include_bots)):
                raise UserError(f"An identical route already exists (#{existing.id}).")

        await interaction.response.defer(ephemeral=True, thinking=True)
        if event_type == "commits":
            try:
                await self.bot.github.get_branch(tracked.owner, tracked.name, branch)  # type: ignore[arg-type]
            except NotFound:
                raise UserError(f"Branch `{branch}` doesn't exist in {tracked.full_name}.")

        route = await self.bot.db.add_route(
            tracked, channel.id, event_type, release_kind=kind, tag_pattern=tag_pattern,
            branch=branch, mention_role_id=mention.id if mention else None,
            crosspost=publish, created_by=interaction.user.id, pr_events=pr_events,
            include_drafts=bool(include_drafts), include_bots=bool(include_bots),
        )
        note = ""
        try:
            await self.bot.poller.ensure_baseline(route)
        except GitHubError as exc:
            note = f"\n⚠️ Couldn't reach GitHub yet ({exc}); I'll retry on the next poll."

        if mention and not mention.mentionable and not channel.permissions_for(
                channel.guild.me).mention_everyone:
            note += (f"\n⚠️ {mention.mention} isn't mentionable and I lack **Mention @everyone, "
                     "@here, and All Roles**, so pings won't notify anyone.")
        await interaction.followup.send(
            f"✅ Route **#{route.id}**: **{tracked.full_name}** → {channel.mention} "
            f"({route.describe()}). Only events from now on will be posted.{note}",
            ephemeral=True,
        )

    @add.autocomplete("pr_events")
    async def _pr_events_autocomplete(self, interaction: discord.Interaction,
                                      current: str) -> list[app_commands.Choice[str]]:
        suggestions = ["opened, ready", "opened, ready, merged", "merged",
                       "merged, closed", "all"] + list(PR_EVENTS)
        if current.strip():
            suggestions.insert(0, current.strip())
        return [app_commands.Choice(name=v, value=v)
                for v in dict.fromkeys(suggestions) if current.lower() in v.lower() or v == current.strip()][:25]

    @app_commands.command(name="remove", description="Delete a route")
    @app_commands.autocomplete(route_id=route_autocomplete)
    @app_commands.rename(route_id="route")
    @bot_admin()
    async def remove(self, interaction: discord.Interaction, route_id: int) -> None:
        route = await self._get(interaction, route_id)
        await self.bot.db.remove_route(route.id)
        await interaction.response.send_message(
            f"🗑️ Removed route #{route.id} ({route.full_name} → <#{route.channel_id}>).",
            ephemeral=True)

    @app_commands.command(name="toggle", description="Pause or resume a route")
    @app_commands.autocomplete(route_id=route_autocomplete)
    @app_commands.rename(route_id="route")
    @bot_admin()
    async def toggle(self, interaction: discord.Interaction, route_id: int) -> None:
        route = await self._get(interaction, route_id)
        enabled = not route.enabled
        if enabled:
            channel = interaction.guild.get_channel(route.channel_id)  # type: ignore[union-attr]
            if channel is None:
                raise UserError("That route's channel no longer exists; remove the route instead.")
            if missing := missing_channel_perms(channel):
                raise UserError(f"I'm still missing {', '.join(missing)} in {channel.mention}.")
        await self.bot.db.set_route_enabled(route.id, enabled)
        if enabled:
            await interaction.response.defer(ephemeral=True)
            route.enabled = True
            try:
                await self.bot.poller.ensure_baseline(route)
            except GitHubError:
                pass
            await interaction.followup.send(f"▶️ Route #{route.id} resumed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⏸️ Route #{route.id} paused.", ephemeral=True)

    @app_commands.command(name="list", description="List routes")
    @app_commands.describe(repo="Only show routes for this repository")
    @app_commands.autocomplete(repo=repo_autocomplete)
    @bot_admin()
    async def list_(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        assert interaction.guild is not None
        repo_id = (await resolve_tracked_repo(interaction, repo)).id if repo else None
        routes = await self.bot.db.list_routes(interaction.guild.id, repo_id)
        if not routes:
            await interaction.response.send_message(
                "No routes yet. Create one with `/route add`.", ephemeral=True)
            return
        lines = []
        current = None
        for r in routes:
            if r.full_name != current:
                current = r.full_name
                lines.append(f"\n**{r.full_name}**")
            status = "" if r.enabled else " ⏸️ *paused*"
            lines.append(f"`#{r.id}` → <#{r.channel_id}> — {r.describe()}{status}")
            if r.last_error:
                lines.append(f"  ⚠️ {truncate(r.last_error, 150)}")
        embed = discord.Embed(title="Routes", description=truncate("\n".join(lines), 4000),
                              color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="test", description="Post the most recent matching event to a route's channel")
    @app_commands.autocomplete(route_id=route_autocomplete)
    @app_commands.rename(route_id="route")
    @bot_admin()
    async def test(self, interaction: discord.Interaction, route_id: int) -> None:
        route = await self._get(interaction, route_id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        items, _ = await self.bot.github.fetch_stream(route.owner, route.name, route.stream)
        items = items or []
        if route.event_type == "pulls":  # label each PR by its current state
            items = [e for e in (current_pr_event(pr, route.pr_event_set) for pr in items) if e]
        match = next((i for i in items if route.accepts(i)), None)
        if match is None:
            await interaction.followup.send("Nothing on GitHub matches this route yet.", ephemeral=True)
            return
        await self.bot.deliver(route, route.stream, [match])
        refreshed = await self.bot.db.get_route(route.guild_id, route.id)
        if refreshed and not refreshed.enabled and refreshed.last_error:
            await interaction.followup.send(f"❌ Delivery failed: {refreshed.last_error}", ephemeral=True)
        else:
            await interaction.followup.send(f"📨 Sent a test post to <#{route.channel_id}>.", ephemeral=True)


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Routes(bot))
