from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import discord

GREEN = discord.Color.from_rgb(46, 160, 67)
ORANGE = discord.Color.from_rgb(210, 153, 34)
PURPLE = discord.Color.from_rgb(130, 80, 223)
BLUE = discord.Color.from_rgb(9, 105, 218)
GREY = discord.Color.from_rgb(101, 109, 118)
RED = discord.Color.from_rgb(207, 34, 46)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def truncate(text: str | None, limit: int) -> str:
    text = _COMMENT_RE.sub("", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _author(embed: discord.Embed, user: dict[str, Any] | None) -> None:
    if user:
        embed.set_author(name=user.get("login", "unknown"), url=user.get("html_url"),
                         icon_url=user.get("avatar_url"))


def release_embed(repo: str, release: dict[str, Any]) -> discord.Embed:
    pre = bool(release.get("prerelease"))
    tag = release.get("tag_name", "")
    title = release.get("name") or tag
    embed = discord.Embed(
        title=f"{repo} {title}"[:256],
        url=release.get("html_url"),
        description=truncate(release.get("body"), 1500) or "*No release notes.*",
        color=ORANGE if pre else GREEN,
        timestamp=_ts(release.get("published_at") or release.get("created_at")),
    )
    _author(embed, release.get("author"))
    embed.add_field(name="Tag", value=f"`{tag}`")
    embed.add_field(name="Type", value="Pre-release" if pre else "Release")
    assets = release.get("assets") or []
    if assets:
        lines = [f"[{a['name']}]({a['browser_download_url']})" for a in assets[:5]]
        if len(assets) > 5:
            lines.append(f"…and {len(assets) - 5} more")
        embed.add_field(name="Assets", value=truncate("\n".join(lines), 1024), inline=False)
    embed.set_footer(text="New pre-release" if pre else "New release")
    return embed


def commits_embed(repo: str, branch: str, commits: list[dict[str, Any]]) -> discord.Embed:
    shown = commits[-10:]  # newest 10, oldest first
    lines = []
    for c in shown:
        msg = (c["commit"]["message"].splitlines() or [""])[0]
        author = (c.get("author") or {}).get("login") or c["commit"]["author"].get("name", "?")
        lines.append(f"[`{c['sha'][:7]}`]({c['html_url']}) {truncate(msg, 70)} — {author}")
    if len(commits) > len(shown):
        lines.insert(0, f"*…{len(commits) - len(shown)} earlier commit(s) not shown*")
    n = len(commits)
    last = commits[-1]
    embed = discord.Embed(
        title=f"[{repo}:{branch}] {n} new commit{'s' if n != 1 else ''}",
        url=f"https://github.com/{repo}/commits/{branch}",
        description=truncate("\n".join(lines), 4000),
        color=BLUE,
        timestamp=_ts(last["commit"]["committer"].get("date")),
    )
    _author(embed, last.get("author"))
    return embed


def issue_embed(repo: str, issue: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"[{repo}] Issue opened: #{issue['number']} {issue['title']}"[:256],
        url=issue.get("html_url"),
        description=truncate(issue.get("body"), 800),
        color=GREEN,
        timestamp=_ts(issue.get("created_at")),
    )
    _author(embed, issue.get("user"))
    labels = [lbl["name"] for lbl in issue.get("labels", []) if isinstance(lbl, dict)]
    if labels:
        embed.add_field(name="Labels", value=truncate(", ".join(labels), 1024))
    return embed



# event -> (title verb, color, timestamp field, show description)
_PR_EVENT_STYLE = {
    "opened": ("PR opened", GREEN, "created_at", True),
    "ready": ("PR ready for review", GREEN, "updated_at", True),
    "merged": ("PR merged", PURPLE, "merged_at", False),
    "closed": ("PR closed", RED, "closed_at", False),
    "reopened": ("PR reopened", GREEN, "updated_at", True),
}


def pull_embed(repo: str, pr: dict[str, Any]) -> discord.Embed:
    event = pr.get("_event") or "opened"
    verb, color, when, show_body = _PR_EVENT_STYLE.get(event, _PR_EVENT_STYLE["opened"])
    if pr.get("draft") and event == "opened":
        verb, color = "Draft PR opened", GREY
    embed = discord.Embed(
        title=f"[{repo}] {verb}: #{pr['number']} {pr['title']}"[:256],
        url=pr.get("html_url"),
        description=truncate(pr.get("body"), 800) if show_body else None,
        color=color,
        timestamp=_ts(pr.get(when) or pr.get("updated_at")),
    )
    _author(embed, pr.get("user"))
    base = (pr.get("base") or {}).get("ref")
    head = (pr.get("head") or {}).get("label")
    if base and head:
        embed.add_field(name="Branches", value=f"`{head}` → `{base}`")
    if pr.get("draft"):
        embed.set_footer(text="Draft")
    return embed


def issue_detail_embed(repo: str, item: dict[str, Any], *, compact: bool = False) -> discord.Embed:
    """Detailed (for /issue view) or compact (for #123 auto-preview) embed for an issue or PR."""
    is_pr = "pull_request" in item
    state = item.get("state") or "open"
    merged = bool(item.get("merged_at") or (item.get("pull_request") or {}).get("merged_at"))
    if is_pr:
        if merged:
            emoji, state_label, color = "🟣", "Merged PR", PURPLE
        elif state == "closed":
            emoji, state_label, color = "🔴", "Closed PR", RED
        elif item.get("draft"):
            emoji, state_label, color = "⚪", "Draft PR", GREY
        else:
            emoji, state_label, color = "🟢", "Open PR", GREEN
    else:
        if state == "closed" and item.get("state_reason") == "not_planned":
            emoji, state_label, color = "⚪", "Closed (not planned)", GREY
        elif state == "closed":
            emoji, state_label, color = "🟣", "Closed", PURPLE
        else:
            emoji, state_label, color = "🟢", "Open", GREEN

    title = truncate(f"{emoji} {repo}#{item['number']} {item['title']}", 256)
    labels = [lbl["name"] for lbl in item.get("labels", []) if isinstance(lbl, dict)]
    comments = item.get("comments") or 0

    if compact:
        snippet = truncate(item.get("body"), 220)
        meta = [f"**{state_label}**"]
        if labels:
            meta.append(" ".join(f"`{truncate(n, 20)}`" for n in labels[:4]))
        if comments:
            meta.append(f"💬 {comments}")
        parts = [snippet] if snippet else []
        parts.append(" · ".join(meta))
        embed = discord.Embed(
            title=title,
            url=item.get("html_url"),
            description=truncate("\n".join(parts), 500),
            color=color,
            timestamp=_ts(item.get("created_at")),
        )
        _author(embed, item.get("user"))
        return embed

    embed = discord.Embed(
        title=title,
        url=item.get("html_url"),
        description=truncate(item.get("body"), 1000) or "*No description.*",
        color=color,
        timestamp=_ts(item.get("created_at")),
    )
    _author(embed, item.get("user"))
    embed.add_field(name="Status", value=state_label)
    if labels:
        embed.add_field(name="Labels", value=truncate(", ".join(labels), 1024))
    assignees = [a["login"] for a in item.get("assignees") or [] if isinstance(a, dict) and a.get("login")]
    if assignees:
        embed.add_field(name="Assignees", value=truncate(", ".join(assignees), 1024))
    embed.add_field(name="Comments", value=str(comments))
    milestone = (item.get("milestone") or {}).get("title")
    if milestone:
        embed.add_field(name="Milestone", value=truncate(milestone, 256))
    return embed


def workflow_embed(repo: str, run: dict[str, Any]) -> discord.Embed:
    name = run.get("name") or "Workflow"
    number = run.get("run_number")
    conclusion = (run.get("conclusion") or "failure").replace("_", " ")
    branch = run.get("head_branch") or "?"
    sha = (run.get("head_sha") or "")[:7]
    head_msg = ((run.get("head_commit") or {}).get("message") or "").splitlines()
    msg_line = truncate(head_msg[0], 100) if head_msg else ""
    desc = f"Branch: `{branch}` · Event: `{run.get('event') or 'push'}` · Result: **{conclusion}**"
    if sha:
        desc += f"\nCommit: `{sha}`" + (f" — {msg_line}" if msg_line else "")
    embed = discord.Embed(
        title=truncate(f"❌ [{repo}] {name}" + (f" #{number}" if number else "") + f" ({conclusion})", 256),
        url=run.get("html_url"),
        description=truncate(desc, 1000),
        color=RED,
        timestamp=_ts(run.get("updated_at") or run.get("created_at")),
    )
    _author(embed, run.get("triggering_actor") or run.get("actor"))
    return embed


def build_embeds(repo: str, stream: str, items: list[dict[str, Any]]) -> list[discord.Embed]:
    """Turn new items of a feed into the embeds to post (one message per embed)."""
    if stream.startswith("commits:"):
        return [commits_embed(repo, stream.split(":", 1)[1], items)]
    if stream == "releases":
        return [release_embed(repo, i) for i in items]
    if stream == "issues":
        return [issue_embed(repo, i) for i in items]
    if stream in ("prs", "pulls"):
        return [pull_embed(repo, i) for i in items]
    if stream == "workflows":
        return [workflow_embed(repo, i) for i in items]
    raise ValueError(stream)
