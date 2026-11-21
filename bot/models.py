from __future__ import annotations

import fnmatch
import re
from urllib.parse import quote
from dataclasses import dataclass
from typing import Any

EVENT_TYPES = ("releases", "commits", "issues", "pulls")
RELEASE_KINDS = ("all", "stable", "prerelease")

@dataclass(frozen=True)
class ReportStyle:
    """Wording for the "report" button and issue form, based on a label."""
    emoji: str
    button: str
    form: str
    placeholder: str


_GENERIC_STYLE = ReportStyle("📝", "Open an issue", "New issue",
                             "Describe the issue or idea in as much detail as you can.")
_REPORT_STYLES = [
    (("bug",), ReportStyle("🐛", "Report a bug", "Bug report",
                           "What happened? What did you expect? Steps to reproduce?")),
    (("enhancement", "feature"), ReportStyle("💡", "Suggest a feature", "Feature request",
                                             "What would you like? What problem would it solve?")),
    (("doc",), ReportStyle("📚", "Suggest a docs change", "Docs issue",
                           "Which page or section? What's missing, wrong or unclear?")),
    (("question",), ReportStyle("❓", "Ask a question", "Question",
                                "What would you like to know? What have you tried so far?")),
]


def report_style(labels: str | None) -> ReportStyle:
    """Pick button/form wording from a label, e.g. ``bug``, ``enhancement``, ``type: feature``.

    With several comma-separated labels, the first recognised one wins.
    """
    for label in (labels or "").lower().split(","):
        for keywords, style in _REPORT_STYLES:
            if any(k in label for k in keywords):
                return style
    return _GENERIC_STYLE


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
    "pulls": "Pull requests",
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
    """Identify the GitHub feed a route listens to. Commits are tracked per branch.

    Pull request routes share one ``prs`` feed per repo (their branch is a filter on the
    PR's base branch, not a separate feed).
    """
    if event_type == "commits":
        if not branch:
            raise ValueError("commit routes need a branch")
        return f"commits:{branch}"
    if event_type == "pulls":
        return "prs"
    return event_type


# Pull request lifecycle events a route can post.
PR_EVENTS = ("opened", "ready", "merged", "closed", "reopened")
PR_EVENT_LABELS = {
    "opened": "opened",
    "ready": "ready for review",
    "merged": "merged",
    "closed": "closed",
    "reopened": "reopened",
}
DEFAULT_PR_EVENTS = ("opened", "ready")


def parse_pr_events(value: str | None) -> tuple[str, ...]:
    """Parse ``"opened, merged"`` (or ``all``). Raises ValueError on unknown names."""
    if value is None or not value.strip():
        return DEFAULT_PR_EVENTS
    names = [v.strip().lower() for v in value.replace(" ", ",").split(",") if v.strip()]
    if names == ["all"]:
        return PR_EVENTS
    aliases = {"open": "opened", "ready for review": "ready", "merge": "merged",
               "close": "closed", "reopen": "reopened"}
    names = [aliases.get(n, n) for n in names]
    unknown = [n for n in names if n not in PR_EVENTS]
    if unknown:
        raise ValueError(f"Unknown pull request event(s): {', '.join(unknown)}. "
                         f"Use any of: {', '.join(PR_EVENTS)} (or `all`).")
    return tuple(e for e in PR_EVENTS if e in names)  # canonical order, no duplicates


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
    pr_events: str | None = None       # pulls: comma-separated PR_EVENTS (None = default)
    include_drafts: bool = False       # pulls: also post about draft PRs
    include_bots: bool = False         # pulls: also post about PRs opened by bots

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def repo_key(self) -> str:
        return self.full_name.lower()

    @property
    def stream(self) -> str:
        return stream_key(self.event_type, self.branch)

    @property
    def pr_event_set(self) -> tuple[str, ...]:
        try:
            return parse_pr_events(self.pr_events)
        except ValueError:
            return DEFAULT_PR_EVENTS

    def accepts(self, item: dict[str, Any]) -> bool:
        """Whether a single GitHub item passes this route's filters."""
        if self.event_type == "releases":
            prerelease = bool(item.get("prerelease"))
            if self.release_kind == "stable" and prerelease:
                return False
            if self.release_kind == "prerelease" and not prerelease:
                return False
            return pattern_matches(self.tag_pattern, item.get("tag_name") or "")
        if self.event_type == "pulls":
            if item.get("_event") not in self.pr_event_set:
                return False
            if not self.include_bots and (item.get("user") or {}).get("type") == "Bot":
                return False
            # Drafts only count once they're marked ready (a "ready" event is never a draft).
            if item.get("draft") and not self.include_drafts:
                return False
            base = (item.get("base") or {}).get("ref") or ""
            return pattern_matches(self.branch, base)
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
        if self.event_type == "pulls":
            events = self.pr_event_set
            parts[0] += " (" + ("all events" if events == PR_EVENTS else
                                ", ".join(PR_EVENT_LABELS[e] for e in events)) + ")"
            if self.branch:
                parts.append(f"into `{self.branch}`")
            if self.include_drafts:
                parts.append("incl. drafts")
            if self.include_bots:
                parts.append("incl. bots")
        if self.mention_role_id:
            parts.append(f"pings <@&{self.mention_role_id}>")
        if self.crosspost:
            parts.append("auto-publish")
        return ", ".join(parts)


@dataclass
class Dashboard:
    id: int
    repo_id: int
    guild_id: int
    owner: str
    name: str
    channel_id: int
    message_id: int | None = None
    label: str | None = None
    pinned: bool = False
    last_hash: str | None = None
    last_change: float | None = None
    last_error: str | None = None
    kind: str = "issues"  # issues | prs

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def issues_url(self) -> str:
        """The GitHub list this dashboard mirrors (issues or pull requests, with its label)."""
        if self.kind == "prs":
            url = f"https://github.com/{self.full_name}/pulls"
            query = "is:pr is:open"
        else:
            url = f"https://github.com/{self.full_name}/issues"
            query = "is:issue is:open"
        if self.label:
            url += "?q=" + quote(f'{query} label:"{self.label}"')
        return url

    def describe(self) -> str:
        text = self.full_name + (" pull requests" if self.kind == "prs" else " issues")
        return text + (f" (label `{self.label}`)" if self.label else "")


@dataclass
class Watch:
    """Turns Discord messages into issues: a role mention, or new posts in a forum channel."""
    id: int
    repo_id: int
    guild_id: int
    owner: str
    name: str
    kind: str                       # "role" | "forum"
    role_id: int | None = None
    channel_id: int | None = None
    label: str | None = None
    close_threads: bool = True

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def describe(self) -> str:
        if self.kind == "role":
            text = f"<@&{self.role_id}> → **{self.full_name}**"
            if self.channel_id:
                text += f" (only in <#{self.channel_id}>)"
        else:
            text = f"posts in <#{self.channel_id}> → **{self.full_name}**"
            if self.close_threads:
                text += ", closes posts when issues close"
        if self.label:
            text += f", label `{self.label}`"
        return text


@dataclass
class Filing:
    """A Discord message that was turned into a GitHub issue."""
    id: int
    message_id: int
    repo_id: int
    guild_id: int
    owner: str
    name: str
    channel_id: int
    thread_id: int | None
    issue_number: int
    issue_url: str
    reporter_id: int
    filer_id: int
    reply_message_id: int | None
    close_thread: bool
    state: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class PRTracker:
    """Posts a live card for each open pull request of a repo in a channel."""
    id: int
    repo_id: int
    guild_id: int
    owner: str
    name: str
    channel_id: int
    branch: str | None = None
    include_drafts: bool = False
    include_bots: bool = False
    threads: bool = True
    mention_role_id: int | None = None
    last_error: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def wants(self, pr: dict[str, Any]) -> bool:
        """Whether an open PR should get a card (filters apply when the card is created)."""
        if pr.get("draft") and not self.include_drafts:
            return False
        if not self.include_bots and (pr.get("user") or {}).get("type") == "Bot":
            return False
        return pattern_matches(self.branch, (pr.get("base") or {}).get("ref") or "")

    def describe(self) -> str:
        parts = [f"**{self.full_name}** → <#{self.channel_id}>"]
        if self.branch:
            parts.append(f"into `{self.branch}`")
        if self.include_drafts:
            parts.append("incl. drafts")
        if self.include_bots:
            parts.append("incl. bots")
        if not self.threads:
            parts.append("no threads")
        if self.mention_role_id:
            parts.append(f"pings <@&{self.mention_role_id}>")
        return ", ".join(parts)


@dataclass
class PRCard:
    id: int | None
    tracker_id: int
    number: int
    message_id: int | None = None
    thread_id: int | None = None
    state: str = "open"
    status_json: str | None = None
    card_hash: str | None = None
