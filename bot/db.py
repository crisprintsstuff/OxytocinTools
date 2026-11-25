from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite

from .models import Dashboard, Filing, PRCard, PRTracker, Repo, Route, Watch

log = logging.getLogger(__name__)

_CHECKPOINT_MODES = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id      INTEGER PRIMARY KEY,
    admin_role_id INTEGER,
    issue_access  TEXT NOT NULL DEFAULT 'admins',
    issue_role_id INTEGER
);

CREATE TABLE IF NOT EXISTS repos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    owner          TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    default_branch TEXT    NOT NULL DEFAULT 'main',
    issue_access   TEXT,                 -- NULL = use the server default
    added_by       INTEGER,
    added_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS repos_guild_name
    ON repos (guild_id, lower(owner), lower(name));

CREATE TABLE IF NOT EXISTS routes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id         INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    channel_id      INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,
    release_kind    TEXT    NOT NULL DEFAULT 'all',
    tag_pattern     TEXT,
    branch          TEXT,
    mention_role_id INTEGER,
    crosspost       INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    pr_events       TEXT,
    include_drafts  INTEGER NOT NULL DEFAULT 0,
    include_bots    INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_by      INTEGER,
    created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dashboards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    label       TEXT,
    pinned      INTEGER NOT NULL DEFAULT 0,
    last_hash   TEXT,
    last_change REAL,
    last_error  TEXT,
    kind        TEXT NOT NULL DEFAULT 'issues',
    created_by  INTEGER,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS dashboards_message ON dashboards (message_id);

-- Role mentions / forum channels that turn Discord messages into GitHub issues.
CREATE TABLE IF NOT EXISTS watches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id       INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL,          -- role | forum
    role_id       INTEGER,                   -- role watches
    channel_id    INTEGER,                   -- forum channel, or a role watch's only channel
    label         TEXT,
    close_threads INTEGER NOT NULL DEFAULT 1,
    created_by    INTEGER,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Messages that were turned into issues (one per message and repo).
CREATE TABLE IF NOT EXISTS filings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       INTEGER NOT NULL,
    repo_id          INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    thread_id        INTEGER,                -- forum post this issue belongs to
    issue_number     INTEGER NOT NULL,
    issue_url        TEXT    NOT NULL,
    reporter_id      INTEGER NOT NULL,
    filer_id         INTEGER NOT NULL,
    reply_message_id INTEGER,
    close_thread     INTEGER NOT NULL DEFAULT 0,
    state            TEXT    NOT NULL DEFAULT 'open',
    created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (message_id, repo_id)
);

-- Live PR cards: a tracker posts one card per open PR in a channel and keeps it updated.
CREATE TABLE IF NOT EXISTS pr_trackers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id         INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    channel_id      INTEGER NOT NULL,
    branch          TEXT,                     -- base branch filter (glob)
    include_drafts  INTEGER NOT NULL DEFAULT 0,
    include_bots    INTEGER NOT NULL DEFAULT 0,
    threads         INTEGER NOT NULL DEFAULT 1,
    mention_role_id INTEGER,
    last_error      TEXT,
    created_by      INTEGER,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pr_cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id  INTEGER NOT NULL REFERENCES pr_trackers(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    message_id  INTEGER,
    thread_id   INTEGER,
    state       TEXT NOT NULL DEFAULT 'open',  -- open | merged | closed
    status_json TEXT,                          -- last status, to describe what changed
    card_hash   TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tracker_id, number)
);

-- Last known state of each pull request, to notice when one is merged, closed, etc.
CREATE TABLE IF NOT EXISTS pr_snapshots (
    repo       TEXT    NOT NULL,             -- lower-case owner/name
    number     INTEGER NOT NULL,
    state      TEXT    NOT NULL,             -- open | closed | merged
    draft      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (repo, number)
);

-- When PR tracking started for a repo; PRs merged/closed/opened before it aren't announced.
CREATE TABLE IF NOT EXISTS pr_baselines (
    repo        TEXT PRIMARY KEY,
    baseline_at REAL NOT NULL
);

-- Polling state is shared by every server tracking the same repo, so each
-- GitHub feed is fetched once per cycle no matter how many servers use it.
CREATE TABLE IF NOT EXISTS streams (
    repo         TEXT NOT NULL,      -- lower-case owner/name
    stream       TEXT NOT NULL,      -- releases | issues | prs | commits:<branch>
    etag         TEXT,
    last_checked TEXT,
    last_error   TEXT,
    PRIMARY KEY (repo, stream)
);

CREATE TABLE IF NOT EXISTS seen_items (
    repo    TEXT NOT NULL,
    stream  TEXT NOT NULL,
    item_id TEXT NOT NULL,
    seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, stream, item_id)
);
"""

# Columns added after the first release; applied to existing databases on startup.
MIGRATIONS = {
    "guild_settings": {
        "issue_access": "TEXT NOT NULL DEFAULT 'admins'",
        "issue_role_id": "INTEGER",
    },
    "repos": {"issue_access": "TEXT"},
    "dashboards": {"kind": "TEXT NOT NULL DEFAULT 'issues'"},
    "routes": {
        "pr_events": "TEXT",
        "include_drafts": "INTEGER NOT NULL DEFAULT 0",
        "include_bots": "INTEGER NOT NULL DEFAULT 0",
    },
}

_REPO_COLUMNS = "id, guild_id, owner, name, default_branch, issue_access"

_ROUTE_SELECT = """
SELECT r.id, r.repo_id, p.guild_id, p.owner, p.name, r.channel_id, r.event_type,
       r.release_kind, r.tag_pattern, r.branch, r.mention_role_id, r.crosspost,
       r.enabled, r.last_error, r.pr_events, r.include_drafts, r.include_bots
FROM routes r JOIN repos p ON p.id = r.repo_id
"""


_DASHBOARD_SELECT = """
SELECT d.id, d.repo_id, p.guild_id, p.owner, p.name, d.channel_id, d.message_id, d.label,
       d.pinned, d.last_hash, d.last_change, d.last_error, d.kind
FROM dashboards d JOIN repos p ON p.id = d.repo_id
"""


def _dashboard(row: Iterable[Any]) -> Dashboard:
    values = list(row)
    values[8] = bool(values[8])
    return Dashboard(*values)


def _route(row: Iterable[Any]) -> Route:
    (id_, repo_id, guild_id, owner, name, channel_id, event_type, release_kind,
     tag_pattern, branch, mention_role_id, crosspost, enabled, last_error,
     pr_events, include_drafts, include_bots) = row
    return Route(
        id=id_, repo_id=repo_id, guild_id=guild_id, owner=owner, name=name,
        channel_id=channel_id, event_type=event_type, release_kind=release_kind,
        tag_pattern=tag_pattern, branch=branch, mention_role_id=mention_role_id,
        crosspost=bool(crosspost), enabled=bool(enabled), last_error=last_error,
        pr_events=pr_events, include_drafts=bool(include_drafts),
        include_bots=bool(include_bots),
    )


class Database:
    def __init__(self, conn: aiosqlite.Connection, path: str = ":memory:",
                 config_backup_path: str | None = None):
        self.conn = conn
        self.path = path
        self.config_backup_path = config_backup_path
        self._suppress_snapshot = False

    @classmethod
    async def open(cls, path: str, *, config_backup_path: str | None = None) -> "Database":
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            for column, decl in columns.items():
                if column not in existing:
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await conn.commit()
        return cls(conn, path, config_backup_path=config_backup_path)

    async def close(self) -> None:
        if self.path != ":memory:":
            try:
                await self.checkpoint("TRUNCATE")
            except Exception as exc:
                log.warning("Failed to checkpoint WAL on close: %s", exc)
        await self.conn.close()

    async def checkpoint(self, mode: str = "PASSIVE") -> None:
        mode_upper = mode.upper()
        if mode_upper not in _CHECKPOINT_MODES:
            raise ValueError(f"Unsupported WAL checkpoint mode: {mode!r}")
        if self.path == ":memory:":
            return
        await self.conn.execute(f"PRAGMA wal_checkpoint({mode_upper})")

    async def backup(self, backup_dir: str | None = None, keep: int = 7) -> str | None:
        """Create an online SQLite backup and prune older backups beyond ``keep``."""
        if self.path == ":memory:" or keep <= 0:
            return None
        target_dir = backup_dir or os.path.join(os.path.dirname(os.path.abspath(self.path)), "backups")
        os.makedirs(target_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        target = os.path.join(target_dir, f"tracker-{stamp}.db")
        tmp_path = f"{target}.tmp"
        async with aiosqlite.connect(tmp_path) as dst:
            await self.conn.backup(dst)
        os.replace(tmp_path, target)
        existing = sorted(
            f for f in os.listdir(target_dir)
            if f.startswith("tracker-") and f.endswith(".db")
        )
        for old in existing[:-keep]:
            try:
                os.remove(os.path.join(target_dir, old))
            except OSError:
                pass
        return target

    # ------------------------------------------------------------------ metadata
    async def get_meta(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            await self.conn.execute("DELETE FROM bot_meta WHERE key = ?", (key,))
        else:
            await self.conn.execute(
                "INSERT INTO bot_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await self.conn.commit()

    # ------------------------------------------------------------------ settings
    async def get_admin_role(self, guild_id: int) -> int | None:
        async with self.conn.execute(
            "SELECT admin_role_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_admin_role(self, guild_id: int, role_id: int | None) -> None:
        await self.conn.execute(
            "INSERT INTO guild_settings (guild_id, admin_role_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET admin_role_id = excluded.admin_role_id",
            (guild_id, role_id),
        )
        await self.conn.commit()
        await self._maybe_snapshot()

    async def get_issue_settings(self, guild_id: int) -> tuple[str, int | None]:
        """Return the server's default issue access level and issue role."""
        async with self.conn.execute(
            "SELECT issue_access, issue_role_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        return (row[0], row[1]) if row else ("admins", None)

    async def set_issue_access(self, guild_id: int, level: str) -> None:
        await self.conn.execute(
            "INSERT INTO guild_settings (guild_id, issue_access) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET issue_access = excluded.issue_access",
            (guild_id, level),
        )
        await self.conn.commit()
        await self._maybe_snapshot()

    async def set_issue_role(self, guild_id: int, role_id: int | None) -> None:
        await self.conn.execute(
            "INSERT INTO guild_settings (guild_id, issue_role_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET issue_role_id = excluded.issue_role_id",
            (guild_id, role_id),
        )
        await self.conn.commit()
        await self._maybe_snapshot()

    async def delete_guild(self, guild_id: int) -> None:
        await self.conn.execute("DELETE FROM repos WHERE guild_id = ?", (guild_id,))
        await self.conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
        await self.conn.commit()
        await self._maybe_snapshot()

    # --------------------------------------------------------------------- repos
    async def add_repo(self, guild_id: int, owner: str, name: str,
                       default_branch: str, added_by: int | None) -> Repo:
        cur = await self.conn.execute(
            "INSERT INTO repos (guild_id, owner, name, default_branch, added_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, owner, name, default_branch, added_by),
        )
        await self.conn.commit()
        await self._maybe_snapshot()
        return Repo(cur.lastrowid, guild_id, owner, name, default_branch)

    async def get_repo(self, guild_id: int, owner: str, name: str) -> Repo | None:
        async with self.conn.execute(
            f"SELECT {_REPO_COLUMNS} FROM repos "
            "WHERE guild_id = ? AND lower(owner) = lower(?) AND lower(name) = lower(?)",
            (guild_id, owner, name),
        ) as cur:
            row = await cur.fetchone()
        return Repo(*row) if row else None

    async def list_repos(self, guild_id: int) -> list[Repo]:
        async with self.conn.execute(
            f"SELECT {_REPO_COLUMNS} FROM repos "
            "WHERE guild_id = ? ORDER BY lower(owner), lower(name)",
            (guild_id,),
        ) as cur:
            return [Repo(*row) for row in await cur.fetchall()]

    async def set_repo_issue_access(self, repo_id: int, level: str | None) -> None:
        await self.conn.execute("UPDATE repos SET issue_access = ? WHERE id = ?", (level, repo_id))
        await self.conn.commit()
        await self._maybe_snapshot()

    async def remove_repo(self, repo_id: int) -> None:
        await self.conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        await self.conn.commit()
        await self._maybe_snapshot()

    # -------------------------------------------------------------------- routes
    async def add_route(self, repo: Repo, channel_id: int, event_type: str, *,
                        release_kind: str = "all", tag_pattern: str | None = None,
                        branch: str | None = None, mention_role_id: int | None = None,
                        crosspost: bool = False, created_by: int | None = None,
                        pr_events: str | None = None, include_drafts: bool = False,
                        include_bots: bool = False) -> Route:
        cur = await self.conn.execute(
            "INSERT INTO routes (repo_id, channel_id, event_type, release_kind, tag_pattern, "
            "branch, mention_role_id, crosspost, created_by, pr_events, include_drafts, "
            "include_bots) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo.id, channel_id, event_type, release_kind, tag_pattern, branch,
             mention_role_id, int(crosspost), created_by, pr_events, int(include_drafts),
             int(include_bots)),
        )
        await self.conn.commit()
        route = await self.get_route(repo.guild_id, cur.lastrowid)
        assert route is not None
        await self._maybe_snapshot()
        return route

    async def update_route(self, route_id: int, guild_id: int, *, channel_id: int,
                           release_kind: str, tag_pattern: str | None, branch: str | None,
                           mention_role_id: int | None, crosspost: bool,
                           pr_events: str | None, include_drafts: bool,
                           include_bots: bool) -> Route | None:
        await self.conn.execute(
            "UPDATE routes SET channel_id = ?, release_kind = ?, tag_pattern = ?, branch = ?, "
            "mention_role_id = ?, crosspost = ?, pr_events = ?, include_drafts = ?, "
            "include_bots = ?, last_error = NULL "
            "WHERE id = ? AND repo_id IN (SELECT id FROM repos WHERE guild_id = ?)",
            (channel_id, release_kind, tag_pattern, branch, mention_role_id, int(crosspost),
             pr_events, int(include_drafts), int(include_bots), route_id, guild_id),
        )
        await self.conn.commit()
        await self._maybe_snapshot()
        return await self.get_route(guild_id, route_id)

    async def get_route(self, guild_id: int, route_id: int) -> Route | None:
        async with self.conn.execute(
            _ROUTE_SELECT + " WHERE r.id = ? AND p.guild_id = ?", (route_id, guild_id)
        ) as cur:
            row = await cur.fetchone()
        return _route(row) if row else None

    async def list_routes(self, guild_id: int, repo_id: int | None = None) -> list[Route]:
        sql = _ROUTE_SELECT + " WHERE p.guild_id = ?"
        params: list[Any] = [guild_id]
        if repo_id is not None:
            sql += " AND r.repo_id = ?"
            params.append(repo_id)
        sql += " ORDER BY lower(p.owner), lower(p.name), r.id"
        async with self.conn.execute(sql, params) as cur:
            return [_route(row) for row in await cur.fetchall()]

    async def list_enabled_routes(self) -> list[Route]:
        async with self.conn.execute(_ROUTE_SELECT + " WHERE r.enabled = 1") as cur:
            return [_route(row) for row in await cur.fetchall()]

    async def remove_route(self, route_id: int) -> None:
        await self.conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        await self.conn.commit()
        await self._maybe_snapshot()

    async def set_route_enabled(self, route_id: int, enabled: bool,
                                error: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE routes SET enabled = ?, last_error = ? WHERE id = ?",
            (int(enabled), error, route_id),
        )
        await self.conn.commit()
        await self._maybe_snapshot()

    async def remove_routes_for_channel(self, channel_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM routes WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()
        if cur.rowcount:
            await self._maybe_snapshot()
        return cur.rowcount

    # ---------------------------------------------------------------- dashboards
    async def add_dashboard(self, repo: Repo, channel_id: int, *, label: str | None,
                            pinned: bool, created_by: int | None,
                            kind: str = "issues") -> Dashboard:
        cur = await self.conn.execute(
            "INSERT INTO dashboards (repo_id, channel_id, label, pinned, created_by, kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repo.id, channel_id, label, int(pinned), created_by, kind),
        )
        await self.conn.commit()
        dash = await self.get_dashboard(cur.lastrowid)
        assert dash is not None
        await self._maybe_snapshot()
        return dash

    async def get_dashboard(self, dashboard_id: int, guild_id: int | None = None) -> Dashboard | None:
        sql, params = _DASHBOARD_SELECT + " WHERE d.id = ?", [dashboard_id]
        if guild_id is not None:
            sql += " AND p.guild_id = ?"
            params.append(guild_id)
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return _dashboard(row) if row else None

    async def list_dashboards(self, guild_id: int | None = None) -> list[Dashboard]:
        sql, params = _DASHBOARD_SELECT, []
        if guild_id is not None:
            sql += " WHERE p.guild_id = ?"
            params.append(guild_id)
        sql += " ORDER BY lower(p.owner), lower(p.name), d.id"
        async with self.conn.execute(sql, params) as cur:
            return [_dashboard(row) for row in await cur.fetchall()]

    async def set_dashboard_message(self, dashboard_id: int, message_id: int | None) -> None:
        await self.conn.execute("UPDATE dashboards SET message_id = ? WHERE id = ?",
                                (message_id, dashboard_id))
        await self.conn.commit()
        await self._maybe_snapshot()

    async def set_dashboard_state(self, dashboard_id: int, *, last_hash: str | None,
                                  last_change: float | None, error: str | None) -> None:
        await self.conn.execute(
            "UPDATE dashboards SET last_hash = ?, last_change = ?, last_error = ? WHERE id = ?",
            (last_hash, last_change, error, dashboard_id),
        )
        await self.conn.commit()

    async def set_dashboard_error(self, dashboard_id: int, error: str | None) -> None:
        await self.conn.execute("UPDATE dashboards SET last_error = ? WHERE id = ?",
                                (error, dashboard_id))
        await self.conn.commit()

    async def get_dashboard_by_message(self, message_id: int) -> Dashboard | None:
        async with self.conn.execute(_DASHBOARD_SELECT + " WHERE d.message_id = ?",
                                     (message_id,)) as cur:
            row = await cur.fetchone()
        return _dashboard(row) if row else None

    async def remove_dashboard(self, dashboard_id: int) -> None:
        await self.conn.execute("DELETE FROM dashboards WHERE id = ?", (dashboard_id,))
        await self.conn.commit()
        await self._maybe_snapshot()

    async def remove_dashboards_for_channel(self, channel_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM dashboards WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()
        if cur.rowcount:
            await self._maybe_snapshot()
        return cur.rowcount

    # ------------------------------------------------------------------- watches
    _WATCH_SELECT = (
        "SELECT w.id, w.repo_id, p.guild_id, p.owner, p.name, w.kind, w.role_id, w.channel_id, "
        "w.label, w.close_threads FROM watches w JOIN repos p ON p.id = w.repo_id"
    )

    @staticmethod
    def _watch(row: Iterable[Any]) -> Watch:
        values = list(row)
        values[9] = bool(values[9])
        return Watch(*values)

    async def add_watch(self, repo: Repo, kind: str, *, role_id: int | None = None,
                        channel_id: int | None = None, label: str | None = None,
                        close_threads: bool = True, created_by: int | None = None) -> Watch:
        cur = await self.conn.execute(
            "INSERT INTO watches (repo_id, kind, role_id, channel_id, label, close_threads, "
            "created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (repo.id, kind, role_id, channel_id, label, int(close_threads), created_by),
        )
        await self.conn.commit()
        watch = await self.get_watch(repo.guild_id, cur.lastrowid)
        assert watch is not None
        await self._maybe_snapshot()
        return watch

    async def update_watch(self, watch_id: int, guild_id: int, *, role_id: int | None,
                           channel_id: int | None, label: str | None,
                           close_threads: bool) -> Watch | None:
        await self.conn.execute(
            "UPDATE watches SET role_id = ?, channel_id = ?, label = ?, close_threads = ? "
            "WHERE id = ? AND repo_id IN (SELECT id FROM repos WHERE guild_id = ?)",
            (role_id, channel_id, label, int(close_threads), watch_id, guild_id),
        )
        await self.conn.commit()
        await self._maybe_snapshot()
        return await self.get_watch(guild_id, watch_id)

    async def get_watch(self, guild_id: int, watch_id: int) -> Watch | None:
        async with self.conn.execute(self._WATCH_SELECT + " WHERE w.id = ? AND p.guild_id = ?",
                                     (watch_id, guild_id)) as cur:
            row = await cur.fetchone()
        return self._watch(row) if row else None

    async def list_watches(self, guild_id: int, kind: str | None = None) -> list[Watch]:
        sql, params = self._WATCH_SELECT + " WHERE p.guild_id = ?", [guild_id]
        if kind:
            sql += " AND w.kind = ?"
            params.append(kind)
        sql += " ORDER BY w.id"
        async with self.conn.execute(sql, params) as cur:
            return [self._watch(row) for row in await cur.fetchall()]

    async def remove_watch(self, watch_id: int) -> None:
        await self.conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        await self.conn.commit()
        await self._maybe_snapshot()

    async def remove_watches_for_channel(self, channel_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM watches WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()
        if cur.rowcount:
            await self._maybe_snapshot()
        return cur.rowcount

    # ------------------------------------------------------------------- filings
    _FILING_SELECT = (
        "SELECT f.id, f.message_id, f.repo_id, f.guild_id, p.owner, p.name, f.channel_id, "
        "f.thread_id, f.issue_number, f.issue_url, f.reporter_id, f.filer_id, "
        "f.reply_message_id, f.close_thread, f.state FROM filings f JOIN repos p ON p.id = f.repo_id"
    )

    @staticmethod
    def _filing(row: Iterable[Any]) -> Filing:
        values = list(row)
        values[13] = bool(values[13])
        return Filing(*values)

    async def add_filing(self, *, message_id: int, repo: Repo, channel_id: int,
                         thread_id: int | None, issue: dict[str, Any], reporter_id: int,
                         filer_id: int, close_thread: bool) -> Filing:
        cur = await self.conn.execute(
            "INSERT INTO filings (message_id, repo_id, guild_id, channel_id, thread_id, "
            "issue_number, issue_url, reporter_id, filer_id, close_thread) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, repo.id, repo.guild_id, channel_id, thread_id, issue["number"],
             issue["html_url"], reporter_id, filer_id, int(close_thread)),
        )
        await self.conn.commit()
        filing = await self.get_filing(cur.lastrowid)
        assert filing is not None
        return filing

    async def get_filing(self, filing_id: int) -> Filing | None:
        async with self.conn.execute(self._FILING_SELECT + " WHERE f.id = ?", (filing_id,)) as cur:
            row = await cur.fetchone()
        return self._filing(row) if row else None

    async def find_filing(self, message_id: int, repo_id: int) -> Filing | None:
        async with self.conn.execute(self._FILING_SELECT + " WHERE f.message_id = ? AND f.repo_id = ?",
                                     (message_id, repo_id)) as cur:
            row = await cur.fetchone()
        return self._filing(row) if row else None

    async def open_thread_filings(self) -> list[Filing]:
        """Forum posts whose thread should close when their issue does."""
        async with self.conn.execute(
            self._FILING_SELECT + " WHERE f.thread_id IS NOT NULL AND f.close_thread = 1 "
            "AND f.state = 'open' AND f.created_at > datetime('now', '-180 days')"
        ) as cur:
            return [self._filing(row) for row in await cur.fetchall()]

    async def set_filing_reply(self, filing_id: int, reply_message_id: int) -> None:
        await self.conn.execute("UPDATE filings SET reply_message_id = ? WHERE id = ?",
                                (reply_message_id, filing_id))
        await self.conn.commit()

    async def set_filing_state(self, filing_id: int, state: str) -> None:
        await self.conn.execute("UPDATE filings SET state = ? WHERE id = ?", (state, filing_id))
        await self.conn.commit()

    # ------------------------------------------------------------------- streams
    async def get_stream(self, repo: str, stream: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT etag, last_checked, last_error FROM streams WHERE repo = ? AND stream = ?",
            (repo, stream),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {"etag": row[0], "last_checked": row[1], "last_error": row[2]}

    async def list_streams(self) -> list[tuple[str, str]]:
        async with self.conn.execute("SELECT repo, stream FROM streams") as cur:
            return [(r, s) for r, s in await cur.fetchall()]

    async def save_stream(self, repo: str, stream: str, *, etag: str | None,
                          error: str | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO streams (repo, stream, etag, last_checked, last_error) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?) "
            "ON CONFLICT(repo, stream) DO UPDATE SET etag = excluded.etag, "
            "last_checked = excluded.last_checked, last_error = excluded.last_error",
            (repo, stream, etag, error),
        )
        await self.conn.commit()

    async def set_stream_error(self, repo: str, stream: str, error: str) -> None:
        await self.conn.execute(
            "UPDATE streams SET last_error = ?, last_checked = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND stream = ?",
            (error, repo, stream),
        )
        await self.conn.commit()

    # ------------------------------------------------------------- PR trackers
    _TRACKER_SELECT = (
        "SELECT t.id, t.repo_id, p.guild_id, p.owner, p.name, t.channel_id, t.branch, "
        "t.include_drafts, t.include_bots, t.threads, t.mention_role_id, t.last_error "
        "FROM pr_trackers t JOIN repos p ON p.id = t.repo_id"
    )

    @staticmethod
    def _tracker(row: Iterable[Any]) -> PRTracker:
        v = list(row)
        for i in (7, 8, 9):
            v[i] = bool(v[i])
        return PRTracker(*v)

    async def add_pr_tracker(self, repo: Repo, channel_id: int, *, branch: str | None,
                             include_drafts: bool, include_bots: bool, threads: bool,
                             mention_role_id: int | None, created_by: int | None) -> PRTracker:
        cur = await self.conn.execute(
            "INSERT INTO pr_trackers (repo_id, channel_id, branch, include_drafts, include_bots, "
            "threads, mention_role_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (repo.id, channel_id, branch, int(include_drafts), int(include_bots), int(threads),
             mention_role_id, created_by))
        await self.conn.commit()
        tracker = await self.get_pr_tracker(cur.lastrowid)
        assert tracker is not None
        await self._maybe_snapshot()
        return tracker

    async def update_pr_tracker(self, tracker_id: int, guild_id: int, *, channel_id: int,
                                branch: str | None, include_drafts: bool, include_bots: bool,
                                threads: bool, mention_role_id: int | None) -> PRTracker | None:
        await self.conn.execute(
            "UPDATE pr_trackers SET channel_id = ?, branch = ?, include_drafts = ?, "
            "include_bots = ?, threads = ?, mention_role_id = ?, last_error = NULL "
            "WHERE id = ? AND repo_id IN (SELECT id FROM repos WHERE guild_id = ?)",
            (channel_id, branch, int(include_drafts), int(include_bots), int(threads),
             mention_role_id, tracker_id, guild_id),
        )
        await self.conn.commit()
        await self._maybe_snapshot()
        return await self.get_pr_tracker(tracker_id, guild_id)

    async def get_pr_tracker(self, tracker_id: int, guild_id: int | None = None) -> PRTracker | None:
        sql, params = self._TRACKER_SELECT + " WHERE t.id = ?", [tracker_id]
        if guild_id is not None:
            sql += " AND p.guild_id = ?"
            params.append(guild_id)
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return self._tracker(row) if row else None

    async def list_pr_trackers(self, guild_id: int | None = None) -> list[PRTracker]:
        sql, params = self._TRACKER_SELECT, []
        if guild_id is not None:
            sql += " WHERE p.guild_id = ?"
            params.append(guild_id)
        async with self.conn.execute(sql + " ORDER BY t.id", params) as cur:
            return [self._tracker(row) for row in await cur.fetchall()]

    async def set_pr_tracker_error(self, tracker_id: int, error: str | None) -> None:
        await self.conn.execute("UPDATE pr_trackers SET last_error = ? WHERE id = ?",
                                (error, tracker_id))
        await self.conn.commit()

    async def remove_pr_tracker(self, tracker_id: int) -> None:
        await self.conn.execute("DELETE FROM pr_trackers WHERE id = ?", (tracker_id,))
        await self.conn.commit()
        await self._maybe_snapshot()

    async def remove_pr_trackers_for_channel(self, channel_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM pr_trackers WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()
        if cur.rowcount:
            await self._maybe_snapshot()
        return cur.rowcount

    async def list_pr_cards(self, tracker_id: int) -> list[PRCard]:
        async with self.conn.execute(
            "SELECT id, tracker_id, number, message_id, thread_id, state, status_json, card_hash "
            "FROM pr_cards WHERE tracker_id = ? ORDER BY number", (tracker_id,)
        ) as cur:
            return [PRCard(*row) for row in await cur.fetchall()]

    async def save_pr_card(self, card: PRCard) -> PRCard:
        if card.id is None:
            cur = await self.conn.execute(
                "INSERT INTO pr_cards (tracker_id, number, message_id, thread_id, state, "
                "status_json, card_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (card.tracker_id, card.number, card.message_id, card.thread_id, card.state,
                 card.status_json, card.card_hash))
            card.id = cur.lastrowid
        else:
            await self.conn.execute(
                "UPDATE pr_cards SET message_id = ?, thread_id = ?, state = ?, status_json = ?, "
                "card_hash = ? WHERE id = ?",
                (card.message_id, card.thread_id, card.state, card.status_json, card.card_hash,
                 card.id))
        await self.conn.commit()
        return card

    # -------------------------------------------------------------- PR tracking
    async def get_pr_baseline(self, repo: str) -> float | None:
        async with self.conn.execute("SELECT baseline_at FROM pr_baselines WHERE repo = ?",
                                     (repo,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_pr_baseline(self, repo: str, baseline_at: float) -> None:
        await self.conn.execute(
            "INSERT INTO pr_baselines (repo, baseline_at) VALUES (?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET baseline_at = excluded.baseline_at",
            (repo, baseline_at))
        await self.conn.commit()

    async def get_pr_snapshots(self, repo: str) -> dict[int, tuple[str, bool]]:
        """number -> (state, draft) for every PR seen in this repo."""
        async with self.conn.execute(
            "SELECT number, state, draft FROM pr_snapshots WHERE repo = ?", (repo,)
        ) as cur:
            return {n: (state, bool(draft)) for n, state, draft in await cur.fetchall()}

    async def save_pr_snapshots(self, repo: str,
                                rows: Iterable[tuple[int, str, bool, str | None]]) -> None:
        await self.conn.executemany(
            "INSERT INTO pr_snapshots (repo, number, state, draft, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(repo, number) DO UPDATE SET "
            "state = excluded.state, draft = excluded.draft, updated_at = excluded.updated_at",
            [(repo, n, state, int(draft), updated) for n, state, draft, updated in rows],
        )
        # Finished PRs that haven't changed in a long time are no longer needed.
        await self.conn.execute(
            "DELETE FROM pr_snapshots WHERE repo = ? AND state != 'open' "
            "AND updated_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-180 days')", (repo,))
        await self.conn.commit()

    async def delete_stream(self, repo: str, stream: str) -> None:
        if stream == "prs":
            await self.conn.execute("DELETE FROM pr_snapshots WHERE repo = ?", (repo,))
            await self.conn.execute("DELETE FROM pr_baselines WHERE repo = ?", (repo,))
        await self.conn.execute("DELETE FROM streams WHERE repo = ? AND stream = ?", (repo, stream))
        await self.conn.execute("DELETE FROM seen_items WHERE repo = ? AND stream = ?", (repo, stream))
        await self.conn.commit()

    async def seen_ids(self, repo: str, stream: str) -> set[str]:
        async with self.conn.execute(
            "SELECT item_id FROM seen_items WHERE repo = ? AND stream = ?", (repo, stream)
        ) as cur:
            return {row[0] for row in await cur.fetchall()}

    async def mark_seen(self, repo: str, stream: str, item_ids: Iterable[str]) -> None:
        await self.conn.executemany(
            "INSERT OR IGNORE INTO seen_items (repo, stream, item_id) VALUES (?, ?, ?)",
            [(repo, stream, i) for i in item_ids],
        )
        await self.conn.commit()

    async def prune_seen(self, repo: str, stream: str, keep_ids: Iterable[str],
                         days: int = 30) -> None:
        """Forget old IDs that have dropped out of GitHub's latest-items window.

        IDs still present in the current window are always kept, otherwise they
        would be announced again on the next poll.
        """
        keep = list(keep_ids)
        placeholders = ",".join("?" * len(keep)) or "NULL"
        await self.conn.execute(
            "DELETE FROM seen_items WHERE repo = ? AND stream = ? "
            f"AND seen_at < datetime('now', ?) AND item_id NOT IN ({placeholders})",
            (repo, stream, f"-{days} days", *keep),
        )
        await self.conn.commit()

    # ----------------------------------------------------------- config backup
    async def list_guild_ids(self) -> list[int]:
        async with self.conn.execute(
            "SELECT guild_id FROM guild_settings UNION SELECT guild_id FROM repos"
        ) as cur:
            return sorted(row[0] for row in await cur.fetchall())

    async def has_any_config(self) -> bool:
        async with self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM repos) OR EXISTS(SELECT 1 FROM guild_settings)"
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row[0])

    async def export_config(self, guild_id: int | None = None) -> dict[str, Any]:
        gids = [guild_id] if guild_id is not None else await self.list_guild_ids()
        guilds: dict[str, Any] = {}
        for gid in gids:
            admin_role_id = await self.get_admin_role(gid)
            issue_access, issue_role_id = await self.get_issue_settings(gid)
            repos = await self.list_repos(gid)
            routes = await self.list_routes(gid)
            dashboards = await self.list_dashboards(gid)
            watches = await self.list_watches(gid)
            trackers = await self.list_pr_trackers(gid)

            repo_entries: list[dict[str, Any]] = []
            for repo in repos:
                repo_entries.append({
                    "owner": repo.owner,
                    "name": repo.name,
                    "default_branch": repo.default_branch,
                    "issue_access": repo.issue_access,
                    "routes": [
                        {
                            "channel_id": r.channel_id,
                            "event_type": r.event_type,
                            "release_kind": r.release_kind,
                            "tag_pattern": r.tag_pattern,
                            "branch": r.branch,
                            "mention_role_id": r.mention_role_id,
                            "crosspost": r.crosspost,
                            "enabled": r.enabled,
                            "pr_events": r.pr_events,
                            "include_drafts": r.include_drafts,
                            "include_bots": r.include_bots,
                        }
                        for r in routes if r.repo_id == repo.id
                    ],
                    "dashboards": [
                        {
                            "channel_id": d.channel_id,
                            "message_id": d.message_id,
                            "label": d.label,
                            "pinned": d.pinned,
                            "kind": d.kind,
                        }
                        for d in dashboards if d.repo_id == repo.id
                    ],
                    "watches": [
                        {
                            "kind": w.kind,
                            "role_id": w.role_id,
                            "channel_id": w.channel_id,
                            "label": w.label,
                            "close_threads": w.close_threads,
                        }
                        for w in watches if w.repo_id == repo.id
                    ],
                    "pr_trackers": [
                        {
                            "channel_id": t.channel_id,
                            "branch": t.branch,
                            "include_drafts": t.include_drafts,
                            "include_bots": t.include_bots,
                            "threads": t.threads,
                            "mention_role_id": t.mention_role_id,
                        }
                        for t in trackers if t.repo_id == repo.id
                    ],
                })

            guilds[str(gid)] = {
                "settings": {
                    "admin_role_id": admin_role_id,
                    "issue_access": issue_access,
                    "issue_role_id": issue_role_id,
                },
                "repos": repo_entries,
            }
        return {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "guilds": guilds,
        }

    async def import_config(self, data: dict[str, Any], *,
                            guild_id: int | None = None) -> dict[str, int]:
        if not isinstance(data, dict) or not isinstance(data.get("guilds"), dict):
            raise ValueError("Invalid backup format: expected a 'guilds' object.")
        if guild_id is not None:
            key = str(guild_id)
            if key not in data["guilds"]:
                raise ValueError("Backup does not contain configuration for this server.")
            entries: list[tuple[int, Any]] = [(guild_id, data["guilds"][key])]
        else:
            try:
                entries = [(int(gid_str), gdata) for gid_str, gdata in data["guilds"].items()]
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid guild ID in backup.") from exc

        counts = {
            "guilds": 0, "repos": 0, "routes": 0,
            "dashboards": 0, "watches": 0, "pr_trackers": 0,
        }
        self._suppress_snapshot = True
        try:
            for gid, gdata in entries:
                if not isinstance(gdata, dict):
                    raise ValueError(f"Invalid guild entry for {gid}.")
                counts["guilds"] += 1
                settings = gdata.get("settings") or {}
                if "admin_role_id" in settings:
                    await self.set_admin_role(gid, settings.get("admin_role_id"))
                if settings.get("issue_access"):
                    await self.set_issue_access(gid, settings["issue_access"])
                if "issue_role_id" in settings:
                    await self.set_issue_role(gid, settings.get("issue_role_id"))

                for rdata in gdata.get("repos") or []:
                    repo = await self.get_repo(gid, rdata["owner"], rdata["name"])
                    if repo is None:
                        repo = await self.add_repo(
                            gid, rdata["owner"], rdata["name"],
                            rdata.get("default_branch") or "main", None,
                        )
                        counts["repos"] += 1
                    if "issue_access" in rdata and rdata["issue_access"] != repo.issue_access:
                        await self.set_repo_issue_access(repo.id, rdata["issue_access"])

                    existing_routes = {
                        (r.channel_id, r.event_type, r.release_kind, r.tag_pattern,
                         r.branch, r.pr_events, r.include_drafts, r.include_bots)
                        for r in await self.list_routes(gid, repo.id)
                    }
                    for rt in rdata.get("routes") or []:
                        sig = (
                            rt["channel_id"], rt["event_type"],
                            rt.get("release_kind", "all"), rt.get("tag_pattern"),
                            rt.get("branch"), rt.get("pr_events"),
                            bool(rt.get("include_drafts", False)),
                            bool(rt.get("include_bots", False)),
                        )
                        if sig in existing_routes:
                            continue
                        route = await self.add_route(
                            repo, rt["channel_id"], rt["event_type"],
                            release_kind=rt.get("release_kind", "all"),
                            tag_pattern=rt.get("tag_pattern"),
                            branch=rt.get("branch"),
                            mention_role_id=rt.get("mention_role_id"),
                            crosspost=bool(rt.get("crosspost", False)),
                            pr_events=rt.get("pr_events"),
                            include_drafts=bool(rt.get("include_drafts", False)),
                            include_bots=bool(rt.get("include_bots", False)),
                        )
                        if rt.get("enabled") is False:
                            await self.set_route_enabled(route.id, False)
                        existing_routes.add(sig)
                        counts["routes"] += 1

                    existing_dashes = {
                        (d.channel_id, (d.label or "").lower(), d.kind)
                        for d in await self.list_dashboards(gid) if d.repo_id == repo.id
                    }
                    for dd in rdata.get("dashboards") or []:
                        sig = (dd["channel_id"], (dd.get("label") or "").lower(),
                               dd.get("kind", "issues"))
                        if sig in existing_dashes:
                            continue
                        dash = await self.add_dashboard(
                            repo, dd["channel_id"], label=dd.get("label"),
                            pinned=bool(dd.get("pinned", False)), created_by=None,
                            kind=dd.get("kind", "issues"),
                        )
                        if dd.get("message_id") is not None:
                            await self.set_dashboard_message(dash.id, dd["message_id"])
                        existing_dashes.add(sig)
                        counts["dashboards"] += 1

                    existing_watches = {
                        (w.kind, w.role_id, w.channel_id)
                        for w in await self.list_watches(gid) if w.repo_id == repo.id
                    }
                    for wd in rdata.get("watches") or []:
                        sig = (wd["kind"], wd.get("role_id"), wd.get("channel_id"))
                        if sig in existing_watches:
                            continue
                        await self.add_watch(
                            repo, wd["kind"], role_id=wd.get("role_id"),
                            channel_id=wd.get("channel_id"), label=wd.get("label"),
                            close_threads=bool(wd.get("close_threads", True)),
                        )
                        existing_watches.add(sig)
                        counts["watches"] += 1

                    existing_trackers = {
                        (t.channel_id, t.branch)
                        for t in await self.list_pr_trackers(gid) if t.repo_id == repo.id
                    }
                    for td in rdata.get("pr_trackers") or []:
                        sig = (td["channel_id"], td.get("branch"))
                        if sig in existing_trackers:
                            continue
                        await self.add_pr_tracker(
                            repo, td["channel_id"], branch=td.get("branch"),
                            include_drafts=bool(td.get("include_drafts", False)),
                            include_bots=bool(td.get("include_bots", False)),
                            threads=bool(td.get("threads", True)),
                            mention_role_id=td.get("mention_role_id"), created_by=None,
                        )
                        existing_trackers.add(sig)
                        counts["pr_trackers"] += 1
        finally:
            self._suppress_snapshot = False
        await self._maybe_snapshot()
        return counts

    async def save_config_snapshot(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        payload = json.dumps(await self.export_config(), indent=2)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)

    async def load_config_snapshot(self, path: str) -> dict[str, int] | None:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return await self.import_config(data)

    async def _maybe_snapshot(self) -> None:
        if self.config_backup_path and not self._suppress_snapshot:
            try:
                await self.save_config_snapshot(self.config_backup_path)
            except Exception as exc:
                log.warning("Failed to write config snapshot %s: %s", self.config_backup_path, exc)
