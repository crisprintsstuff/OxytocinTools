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


def pull_embed(repo: str, pr: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"[{repo}] PR opened: #{pr['number']} {pr['title']}"[:256],
        url=pr.get("html_url"),
        description=truncate(pr.get("body"), 800),
        color=GREY if pr.get("draft") else PURPLE,
        timestamp=_ts(pr.get("created_at")),
    )
    _author(embed, pr.get("user"))
    base = (pr.get("base") or {}).get("ref")
    head = (pr.get("head") or {}).get("label")
    if base and head:
        embed.add_field(name="Branches", value=f"`{head}` → `{base}`")
    if pr.get("draft"):
        embed.set_footer(text="Draft")
    return embed


def build_embeds(repo: str, stream: str, items: list[dict[str, Any]]) -> list[discord.Embed]:
    """Turn new items of a feed into the embeds to post (one message per embed)."""
    if stream.startswith("commits:"):
        return [commits_embed(repo, stream.split(":", 1)[1], items)]
    if stream == "releases":
        return [release_embed(repo, i) for i in items]
    if stream == "issues":
        return [issue_embed(repo, i) for i in items]
    if stream == "pulls":
        return [pull_embed(repo, i) for i in items]
    raise ValueError(stream)
