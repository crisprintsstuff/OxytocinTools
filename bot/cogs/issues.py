from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import UserError, bot_admin, can_create_issue, is_admin
from ..embeds import BLUE, GREEN, issue_detail_embed, truncate
from ..github import GitHubError, NotFound
from ..models import IssueTemplate, Repo, parse_repo, report_style
from . import access_hint, repo_autocomplete, resolve_tracked_repo

if TYPE_CHECKING:
    from ..main import TrackerBot

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"(?<![\w@])@(?=[A-Za-z0-9])")
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"}
VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "mkv", "avi"}


def neutralize_mentions(text: str) -> str:
    """Stop ``@name`` in Discord text from pinging GitHub users (adds a zero-width space)."""
    return _MENTION_RE.sub("@​", text)


def format_attachments(attachments: list[tuple[str, str]]) -> str:
    is_github = lambda u: "github.com" in u or "githubusercontent.com" in u
    warning = " (Discord links may expire)" if any(not is_github(url) for _, url in attachments) else ""
    media_blocks = []
    list_items = []
    for name, url in attachments:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in IMAGE_EXTENSIONS:
            media_blocks.append(f"[![{name}]({url})]({url})")
        elif ext in VIDEO_EXTENSIONS:
            list_items.append(f"- 🎥 [{name}]({url})")
        else:
            list_items.append(f"- [{name}]({url})")
    content_parts = []
    if media_blocks:
        content_parts.append("\n\n".join(media_blocks))
    if list_items:
        content_parts.append("\n".join(list_items))
    return f"**Attachments**{warning}:\n" + "\n\n".join(content_parts)


def format_issue_body(body: str, *, reporter: str, reporter_username: str, guild: str,
                      channel: str | None, message_url: str | None = None,
                      message_author: str | None = None,
                      attachments: list[tuple[str, str]] | None = None,
                      filed_by: str | None = None) -> str:
    """Build the GitHub issue body: the user's text plus a footer saying where it came from."""
    parts = [body.strip() or "_No description provided._"]
    if attachments:
        parts.append(format_attachments(attachments))
    where = f"**{guild}**" + (f" #{channel}" if channel else "")
    footer = f"Reported from Discord by **{reporter}** (`{reporter_username}`) in {where}"
    if message_author and message_url:
        footer += f". [Original message]({message_url}) by **{message_author}**"
    elif message_url:
        footer += f". [Original message]({message_url})"
    if filed_by:
        footer += f". Filed by **{filed_by}**"
    parts.append("---\n<sub>" + neutralize_mentions(footer) + "</sub>")
    return "\n\n".join(parts)


async def process_attachments(bot: Any, repo: Repo, attachments: Any) -> list[tuple[str, str]]:
    """Download attachments and upload them to GitHub if possible, falling back to original URLs."""
    media_raw: str | None = None
    db = getattr(bot, "db", None)
    if db is not None and hasattr(db, "get_media_repo"):
        media_raw = await db.get_media_repo(repo.guild_id)
    if not media_raw:
        media_raw = getattr(getattr(bot, "config", None), "media_repo", None)

    target_owner, target_name, target_branch = repo.owner, repo.name, repo.default_branch
    upload_kwargs: dict[str, Any] = {}
    if media_raw:
        try:
            target_owner, target_name = parse_repo(media_raw)
            target_branch = "main"
            upload_kwargs["path_prefix"] = f"attachments/{repo.owner}_{repo.name}"
        except ValueError:
            log.warning("Ignoring invalid media_repo %r", media_raw)

    async def _handle(att: Any) -> tuple[str, str]:
        if isinstance(att, tuple):
            filename, url = att[0], att[1]
        else:
            filename = getattr(att, "filename", "attachment")
            url = getattr(att, "url", "")
        if hasattr(att, "read") and callable(att.read):
            try:
                content = await att.read()
                upload_fn = getattr(getattr(bot, "github", None), "upload_attachment", None)
                if upload_fn is not None:
                    hosted_url = await upload_fn(
                        target_owner, target_name, target_branch, filename, content, **upload_kwargs
                    )
                    if hosted_url:
                        url = hosted_url
                    elif (alert_fn := getattr(bot, "send_alert", None)) is not None:
                        await alert_fn(
                            repo.guild_id,
                            f"attach:{target_owner}/{target_name}".lower(),
                            "Attachment upload fell back to Discord CDN",
                            f"Couldn't upload `{filename}` to **{target_owner}/{target_name}** "
                            "(`discord-attachments` branch). Ensure the GitHub App has "
                            "**Contents: Read and write** permission.",
                        )
            except Exception as exc:
                log.warning("Couldn't process attachment %s for %s: %s", filename, repo.full_name, exc)
        return filename, url

    return list(await asyncio.gather(*(_handle(a) for a in attachments)))


def github_issue_error(bot, repo: Repo, exc: GitHubError) -> UserError | None:
    """Turn a GitHub error from creating an issue into a message for the user, if we can."""
    if isinstance(exc, NotFound):
        return UserError(f"The bot can't see **{repo.full_name}** on GitHub. "
                         + access_hint(bot, repo.full_name))
    if exc.status == 410:
        return UserError(f"Issues are turned off for **{repo.full_name}** on GitHub.")
    if exc.status in (401, 403):
        if bot.github.auth_mode == "anonymous":
            return UserError("Creating issues needs a GitHub App to be configured for the bot.")
        return UserError(
            f"The bot isn't allowed to create issues in **{repo.full_name}**. "
            "Its GitHub App needs the *Issues: Read and write* permission, and the "
            "new permission must be accepted on the installation. "
            + access_hint(bot, repo.full_name))
    return None


_STOP_WORDS = {
    "the", "and", "for", "with", "when", "that", "this", "from", "into",
    "your", "have", "does", "not", "are", "was", "were", "can", "how",
    "why", "what", "about", "after", "before",
}


def title_from(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return truncate(first.lstrip("#>*-_ ").strip(), 100)


async def find_similar_issues(github: Any, owner: str, name: str, title: str, *,
                              exclude_number: int, limit: int = 3) -> list[dict[str, Any]]:
    """Search open GitHub issues matching keywords from ``title``."""
    if not hasattr(github, "search_issues"):
        return []
    words: list[str] = []
    seen: set[str] = set()
    for word in re.findall(r"[A-Za-z0-9]{3,}", title):
        low = word.lower()
        if low in _STOP_WORDS or low in seen:
            continue
        seen.add(low)
        words.append(low)
        if len(words) >= 5:
            break
    if not words:
        return []
    try:
        data = await github.search_issues(
            owner, name, " ".join(words), state="open", kind="issues", per_page=limit + 2
        )
    except GitHubError:
        return []
    items = [
        item for item in (data.get("items") or [])
        if item.get("number") != exclude_number and "pull_request" not in item
    ]
    return items[:limit]


def format_similar_field(similar: list[dict[str, Any]]) -> str:
    lines = [
        f"• [#{item['number']}]({item['html_url']}) {truncate(item.get('title'), 70)}"
        for item in similar
    ]
    return truncate("\n".join(lines), 1024)


class IssueModal(discord.ui.Modal):
    def __init__(self, cog: "Issues", repo: Repo, *, title: str | None = None,
                 body: str | None = None, source: discord.Message | None = None,
                 labels: str | None = None, template: IssueTemplate | None = None):
        style = report_style(labels)
        form_name = (template.title_hint if template and template.title_hint else style.form)
        placeholder = (template.placeholder if template and template.placeholder else style.placeholder)
        default_body = truncate(body, 4000) if body else (
            truncate(template.body, 4000) if template and template.body else None
        )
        super().__init__(title=truncate(f"{form_name}: {repo.full_name}", 45))
        self.cog, self.repo, self.source = cog, repo, source
        self.issue_title = discord.ui.TextInput(label="Title", max_length=256, default=title or None)
        self.body = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=False,
            max_length=4000, default=default_body,
            placeholder=truncate(placeholder, 100),
        )
        self.labels = discord.ui.TextInput(
            label="Labels (optional, comma-separated)", required=False, max_length=200,
            placeholder="bug, ui", default=labels or None,
        )
        for item in (self.issue_title, self.body, self.labels):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit(interaction, self.repo, self.issue_title.value, self.body.value,
                              self.labels.value, self.source)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.client.on_app_command_error(interaction, error)  # type: ignore[attr-defined]


class TemplateModal(discord.ui.Modal):
    def __init__(self, cog: "Issues", repo: Repo, label_key: str,
                 existing: IssueTemplate | None = None):
        target = "default" if label_key == "*" else label_key
        super().__init__(title=truncate(f"Template ({target}): {repo.full_name}", 45))
        self.cog, self.repo, self.label_key = cog, repo, label_key
        self.title_hint = discord.ui.TextInput(
            label="Form title (optional)", required=False, max_length=30,
            default=existing.title_hint if existing else None,
            placeholder="Bug report",
        )
        self.placeholder = discord.ui.TextInput(
            label="Description placeholder (optional)", required=False, max_length=100,
            default=existing.placeholder if existing else None,
            placeholder="OS, version, steps to reproduce...",
        )
        self.body = discord.ui.TextInput(
            label="Pre-filled body template (optional)", style=discord.TextStyle.paragraph,
            required=False, max_length=3000,
            default=existing.body if existing else None,
            placeholder="**Version:**\n**Steps to reproduce:**\n1. ",
        )
        for item in (self.title_hint, self.placeholder, self.body):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tpl = await self.cog.bot.db.set_issue_template(
            self.repo,
            self.label_key,
            title_hint=self.title_hint.value.strip() or None,
            placeholder=self.placeholder.value.strip() or None,
            body=self.body.value.strip() or None,
        )
        scope = "server/repo default (`*`)" if tpl.label == "*" else f"label `{tpl.label}`"
        await interaction.response.send_message(
            f"✅ Saved issue template for **{self.repo.full_name}** ({scope}).",
            ephemeral=True,
        )

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
        template = await self.cog.bot.db.get_issue_template(repo.id, None)
        await interaction.response.send_modal(
            self.cog.modal_for_message(repo, self.source, template=template)
        )
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

    def cooldown_remaining(self, guild_id: int, user_id: int) -> float:
        """Seconds until this user may create another issue (0 = now). Ignores admin status."""
        cooldown = self.bot.config.issue_cooldown
        last = self._last_issue.get((guild_id, user_id))
        if not cooldown or last is None:  # never created one: don't treat as time 0
            return 0.0                    # (monotonic time starts at boot)
        return max(0.0, last + cooldown - time.monotonic())

    async def check_cooldown(self, interaction: discord.Interaction) -> None:
        if await is_admin(interaction):
            return
        remaining = self.cooldown_remaining(interaction.guild_id or 0, interaction.user.id)
        if remaining > 0:
            raise UserError(f"Slow down! You can create another issue in {int(remaining) + 1}s.")

    def issue_created(self, repo: Repo, guild_id: int, user_id: int, issue: dict) -> None:
        """Bookkeeping after any issue is created from Discord (form, message or watch)."""
        self._last_issue[(guild_id, user_id)] = time.monotonic()
        log.info("User %s created issue %s#%s", user_id, repo.full_name, issue["number"])
        dashboards = getattr(self.bot, "dashboards", None)
        if dashboards is not None:  # show the new issue on this repo's dashboards right away
            asyncio.create_task(dashboards.refresh_repo(repo.guild_id, repo.id))

    def modal_for_message(self, repo: Repo, message: discord.Message, *,
                          template: IssueTemplate | None = None) -> IssueModal:
        content = message.clean_content
        return IssueModal(self, repo, title=title_from(content), body=content,
                          source=message, template=template)

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
        raw_attachments = list(source.attachments) if source else []
        attachments = await process_attachments(self.bot, repo, raw_attachments) if raw_attachments else None
        full_body = format_issue_body(
            body if admin else neutralize_mentions(body),
            reporter=user.display_name, reporter_username=user.name,
            guild=interaction.guild.name if interaction.guild else "Discord", channel=channel,
            message_url=source.jump_url if source else None,
            message_author=source.author.display_name if source else None,
            attachments=attachments,
        )
        labels = [label.strip() for label in labels_raw.split(",") if label.strip()][:10]

        try:
            issue = await self.bot.github.create_issue(repo.owner, repo.name, title, full_body, labels)
        except GitHubError as exc:
            raise (github_issue_error(self.bot, repo, exc) or exc)
        self.issue_created(repo, interaction.guild_id or 0, user.id, issue)

        embed = discord.Embed(title=truncate(f"#{issue['number']} {issue['title']}", 256),
                              url=issue["html_url"], color=GREEN,
                              description=f"Created in **{repo.full_name}**.")
        similar = await find_similar_issues(
            self.bot.github, repo.owner, repo.name, title, exclude_number=issue["number"]
        )
        if similar:
            embed.add_field(
                name="🔎 Possible related open issues",
                value=format_similar_field(similar),
                inline=False,
            )
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
    @app_commands.describe(repo="A tracked repository", label="Optional label or template to pre-fill")
    @app_commands.guild_only()
    async def create(self, interaction: discord.Interaction, repo: str,
                     label: str | None = None) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        allowed, reason = await can_create_issue(interaction, tracked)
        if not allowed:
            raise UserError(reason or "You can't create issues here.")
        await self.check_cooldown(interaction)
        cleaned_label = label.strip() if label else None
        template = await self.bot.db.get_issue_template(tracked.id, cleaned_label)
        await interaction.response.send_modal(
            IssueModal(self, tracked, labels=cleaned_label, template=template)
        )

    @create.autocomplete("repo")
    async def _create_repo_ac(self, interaction: discord.Interaction, current: str):
        return await self._repo_autocomplete(interaction, current)

    @app_commands.command(name="view", description="View a GitHub issue or pull request")
    @app_commands.describe(repo="owner/name or a tracked repository", number="Issue or PR number")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def view(self, interaction: discord.Interaction, repo: str, number: app_commands.Range[int, 1]) -> None:
        try:
            owner, name = parse_repo(repo)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        await interaction.response.defer()
        try:
            item, _ = await self.bot.github.get_issue(owner, name, number)
        except NotFound:
            raise UserError(f"Couldn't find `{owner}/{name}#{number}`. "
                            + access_hint(self.bot, f"{owner}/{name}"))
        assert item is not None
        view = discord.ui.View()
        if item.get("html_url"):
            view.add_item(discord.ui.Button(label="Open on GitHub", url=item["html_url"]))
        await interaction.followup.send(embed=issue_detail_embed(f"{owner}/{name}", item), view=view)

    @app_commands.command(name="search", description="Search issues and pull requests in a repository")
    @app_commands.describe(
        repo="owner/name or a tracked repository",
        query="Search keywords",
        state="Filter by state",
        type="Filter by issues or pull requests",
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(name="All", value="all"),
            app_commands.Choice(name="Open", value="open"),
            app_commands.Choice(name="Closed", value="closed"),
        ],
        type=[
            app_commands.Choice(name="Issues & PRs", value="all"),
            app_commands.Choice(name="Issues only", value="issues"),
            app_commands.Choice(name="Pull requests only", value="prs"),
        ],
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def search(self, interaction: discord.Interaction, repo: str, query: str,
                     state: app_commands.Choice[str] | None = None,
                     type: app_commands.Choice[str] | None = None) -> None:
        try:
            owner, name = parse_repo(repo)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        await interaction.response.defer()
        st = state.value if state else "all"
        kind = type.value if type else "all"
        try:
            data = await self.bot.github.search_issues(owner, name, query, state=st, kind=kind, per_page=10)
        except NotFound:
            raise UserError(f"Couldn't search `{owner}/{name}`. " + access_hint(self.bot, f"{owner}/{name}"))
        items = data.get("items") or []
        total = data.get("total_count", len(items))
        if not items:
            await interaction.followup.send(f"No matching results in **{owner}/{name}** for `{truncate(query, 80)}`.")
            return
        lines = []
        for item in items[:10]:
            is_pr = "pull_request" in item
            merged = bool((item.get("pull_request") or {}).get("merged_at"))
            if merged:
                emoji = "🟣"
            elif item.get("state") == "closed":
                emoji = "⚪" if not is_pr and item.get("state_reason") == "not_planned" else ("🔴" if is_pr else "🟣")
            elif is_pr and item.get("draft"):
                emoji = "⚪"
            else:
                emoji = "🟢"
            labels = " ".join(f"`{truncate(l['name'], 18)}`" for l in (item.get("labels") or [])[:2] if isinstance(l, dict))
            line = f"{emoji} [#{item['number']}]({item['html_url']}) {truncate(item['title'], 60)}"
            if labels:
                line += f" {labels}"
            lines.append(line)
        embed = discord.Embed(
            title=truncate(f"Search: {owner}/{name} ({total} result{'s' if total != 1 else ''})", 256),
            description=truncate("\n".join(lines), 4000),
            color=BLUE,
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="template-set", description="Configure a custom form template for a repo or label")
    @app_commands.describe(
        repo="A tracked repository",
        label="GitHub label (leave empty for the repo's default template)",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @app_commands.guild_only()
    @bot_admin()
    async def template_set(self, interaction: discord.Interaction, repo: str,
                           label: str | None = None) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        key = (label or "").strip().lower() or "*"
        templates = await self.bot.db.list_issue_templates(tracked.guild_id, tracked.id)
        existing = next((t for t in templates if t.label == key), None)
        await interaction.response.send_modal(TemplateModal(self, tracked, key, existing))

    @app_commands.command(name="template-list", description="List configured issue form templates")
    @app_commands.describe(repo="Only show templates for this repository")
    @app_commands.autocomplete(repo=repo_autocomplete)
    @app_commands.guild_only()
    @bot_admin()
    async def template_list(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        assert interaction.guild is not None
        repo_id = (await resolve_tracked_repo(interaction, repo)).id if repo else None
        templates = await self.bot.db.list_issue_templates(interaction.guild.id, repo_id)
        if not templates:
            await interaction.response.send_message(
                "No custom issue templates yet. Add one with `/issue template-set`.",
                ephemeral=True,
            )
            return
        lines = []
        for tpl in templates:
            scope = "default (`*`)" if tpl.label == "*" else f"label `{tpl.label}`"
            parts = []
            if tpl.title_hint:
                parts.append(f"title: `{tpl.title_hint}`")
            if tpl.placeholder:
                parts.append(f"hint: `{truncate(tpl.placeholder, 40)}`")
            if tpl.body:
                parts.append("has body")
            detail = ", ".join(parts) if parts else "empty"
            lines.append(f"• **{tpl.full_name}** ({scope}) — {detail}")
        embed = discord.Embed(
            title="Issue form templates",
            description=truncate("\n".join(lines), 4000),
            color=BLUE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="template-remove", description="Remove a custom issue form template")
    @app_commands.describe(
        repo="A tracked repository",
        label="Label to remove (leave empty for the repo's default template)",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @app_commands.guild_only()
    @bot_admin()
    async def template_remove(self, interaction: discord.Interaction, repo: str,
                              label: str | None = None) -> None:
        tracked = await resolve_tracked_repo(interaction, repo)
        key = (label or "").strip().lower() or "*"
        removed = await self.bot.db.remove_issue_template(tracked.guild_id, tracked.id, key)
        if not removed:
            scope = "default" if key == "*" else f"label `{key}`"
            raise UserError(f"No template found for **{tracked.full_name}** ({scope}).")
        scope = "default (`*`)" if key == "*" else f"label `{key}`"
        await interaction.response.send_message(
            f"🗑️ Removed issue template for **{tracked.full_name}** ({scope}).",
            ephemeral=True,
        )

    async def from_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        repos = await self.allowed_repos(interaction)
        if not repos:
            raise UserError("There are no repositories you can create issues in on this server.")
        await self.check_cooldown(interaction)
        if len(repos) == 1:
            template = await self.bot.db.get_issue_template(repos[0].id, None)
            await interaction.response.send_modal(
                self.modal_for_message(repos[0], message, template=template)
            )
            return
        await interaction.response.send_message(
            "Create an issue from this message in which repository?",
            view=RepoPicker(self, interaction.user.id, repos, message), ephemeral=True)


async def setup(bot: TrackerBot) -> None:
    await bot.add_cog(Issues(bot))
