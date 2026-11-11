from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

EVENT_TYPES = ("releases", "commits", "issues", "pulls")
RELEASE_KINDS = ("all", "stable", "prerelease")

# Who may create GitHub issues from Discord.
ISSUE_ACCESS_LABELS = {
    "disabled": "Nobody (disabled)",
    "admins": "Bot admins only",
    "role": "Bot admins + the issue role",
    "everyone": "Everyone",
}

EVENT_LABELS = {
    "releases": "Releases",
    "commits": "Commits",
    "issues": "New issues",
    "pulls": "New pull requests",
}

_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<name>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$"
)


def parse_repo(value: str) -> tuple[str, str]:
    """Accept ``owner/name`` or a GitHub URL and return ``(owner, name)``."""
    match = _REPO_RE.match(value.strip())
    if not match:
        raise ValueError(f"`{value}` is not a valid repository. Use `owner/name` or a GitHub URL.")
    return match["owner"], match["name"]


def stream_key(event_type: str, branch: str | None = None) -> str:
    """Identify the GitHub feed a route listens to. Commits are tracked per branch."""
    if event_type == "commits":
        if not branch:
            raise ValueError("commit routes need a branch")
        return f"commits:{branch}"
    return event_type


def pattern_matches(pattern: str | None, value: str) -> bool:
    """Glob match (``v2.*``), or regex when the pattern starts with ``re:``."""
    if not pattern:
        return True
    if pattern.startswith("re:"):
        return re.search(pattern[3:], value) is not None
    return fnmatch.fnmatchcase(value, pattern)


def validate_pattern(pattern: str) -> None:
    if pattern.startswith("re:"):
        try:
            re.compile(pattern[3:])
        except re.error as exc:
            raise ValueError(f"Invalid regex: {exc}") from exc


@dataclass
class Repo:
    id: int
    guild_id: int
    owner: str
    name: str
    default_branch: str
    issue_access: str | None = None  # None = use the server default

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        return self.full_name.lower()


@dataclass
class Route:
    id: int
    repo_id: int
    guild_id: int
    owner: str
    name: str
    channel_id: int
    event_type: str
    release_kind: str = "all"
    tag_pattern: str | None = None
    branch: str | None = None
    mention_role_id: int | None = None
    crosspost: bool = False
    enabled: bool = True
    last_error: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def repo_key(self) -> str:
        return self.full_name.lower()

    @property
    def stream(self) -> str:
        return stream_key(self.event_type, self.branch)

    def accepts(self, item: dict[str, Any]) -> bool:
        """Whether a single GitHub item passes this route's filters."""
        if self.event_type == "releases":
            prerelease = bool(item.get("prerelease"))
            if self.release_kind == "stable" and prerelease:
                return False
            if self.release_kind == "prerelease" and not prerelease:
                return False
            return pattern_matches(self.tag_pattern, item.get("tag_name") or "")
        return True

    def describe(self) -> str:
        parts = [EVENT_LABELS.get(self.event_type, self.event_type)]
        if self.event_type == "releases":
            if self.release_kind != "all":
                parts.append(f"{self.release_kind} only")
            if self.tag_pattern:
                parts.append(f"tags `{self.tag_pattern}`")
        if self.event_type == "commits":
            parts.append(f"branch `{self.branch}`")
        if self.mention_role_id:
            parts.append(f"pings <@&{self.mention_role_id}>")
        if self.crosspost:
            parts.append("auto-publish")
        return ", ".join(parts)
