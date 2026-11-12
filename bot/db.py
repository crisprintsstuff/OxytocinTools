from __future__ import annotations

import os
from typing import Any, Iterable

import aiosqlite

from .models import Dashboard, Repo, Route

SCHEMA = """
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
    created_by  INTEGER,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS dashboards_message ON dashboards (message_id);

-- Polling state is shared by every server tracking the same repo, so each
-- GitHub feed is fetched once per cycle no matter how many servers use it.
CREATE TABLE IF NOT EXISTS streams (
    repo         TEXT NOT NULL,      -- lower-case owner/name
    stream       TEXT NOT NULL,      -- releases | issues | pulls | commits:<branch>
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
}

_REPO_COLUMNS = "id, guild_id, owner, name, default_branch, issue_access"

_ROUTE_SELECT = """
SELECT r.id, r.repo_id, p.guild_id, p.owner, p.name, r.channel_id, r.event_type,
       r.release_kind, r.tag_pattern, r.branch, r.mention_role_id, r.crosspost,
       r.enabled, r.last_error
FROM routes r JOIN repos p ON p.id = r.repo_id
"""


_DASHBOARD_SELECT = """
SELECT d.id, d.repo_id, p.guild_id, p.owner, p.name, d.channel_id, d.message_id, d.label,
       d.pinned, d.last_hash, d.last_change, d.last_error
FROM dashboards d JOIN repos p ON p.id = d.repo_id
"""


def _dashboard(row: Iterable[Any]) -> Dashboard:
    values = list(row)
    values[8] = bool(values[8])
    return Dashboard(*values)


def _route(row: Iterable[Any]) -> Route:
    (id_, repo_id, guild_id, owner, name, channel_id, event_type, release_kind,
     tag_pattern, branch, mention_role_id, crosspost, enabled, last_error) = row
    return Route(
        id=id_, repo_id=repo_id, guild_id=guild_id, owner=owner, name=name,
        channel_id=channel_id, event_type=event_type, release_kind=release_kind,
        tag_pattern=tag_pattern, branch=branch, mention_role_id=mention_role_id,
        crosspost=bool(crosspost), enabled=bool(enabled), last_error=last_error,
    )


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str) -> "Database":
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            for column, decl in columns.items():
                if column not in existing:
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

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

    async def set_issue_role(self, guild_id: int, role_id: int | None) -> None:
        await self.conn.execute(
            "INSERT INTO guild_settings (guild_id, issue_role_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET issue_role_id = excluded.issue_role_id",
            (guild_id, role_id),
        )
        await self.conn.commit()

    async def delete_guild(self, guild_id: int) -> None:
        await self.conn.execute("DELETE FROM repos WHERE guild_id = ?", (guild_id,))
        await self.conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
        await self.conn.commit()

    # --------------------------------------------------------------------- repos
    async def add_repo(self, guild_id: int, owner: str, name: str,
                       default_branch: str, added_by: int | None) -> Repo:
        cur = await self.conn.execute(
            "INSERT INTO repos (guild_id, owner, name, default_branch, added_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, owner, name, default_branch, added_by),
        )
        await self.conn.commit()
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

    async def remove_repo(self, repo_id: int) -> None:
        await self.conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        await self.conn.commit()

    # -------------------------------------------------------------------- routes
    async def add_route(self, repo: Repo, channel_id: int, event_type: str, *,
                        release_kind: str = "all", tag_pattern: str | None = None,
                        branch: str | None = None, mention_role_id: int | None = None,
                        crosspost: bool = False, created_by: int | None = None) -> Route:
        cur = await self.conn.execute(
            "INSERT INTO routes (repo_id, channel_id, event_type, release_kind, tag_pattern, "
            "branch, mention_role_id, crosspost, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo.id, channel_id, event_type, release_kind, tag_pattern, branch,
             mention_role_id, int(crosspost), created_by),
        )
        await self.conn.commit()
        route = await self.get_route(repo.guild_id, cur.lastrowid)
        assert route is not None
        return route

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

    async def set_route_enabled(self, route_id: int, enabled: bool,
                                error: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE routes SET enabled = ?, last_error = ? WHERE id = ?",
            (int(enabled), error, route_id),
        )
        await self.conn.commit()

    async def remove_routes_for_channel(self, channel_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM routes WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()
        return cur.rowcount

    # ---------------------------------------------------------------- dashboards
    async def add_dashboard(self, repo: Repo, channel_id: int, *, label: str | None,
                            pinned: bool, created_by: int | None) -> Dashboard:
        cur = await self.conn.execute(
            "INSERT INTO dashboards (repo_id, channel_id, label, pinned, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (repo.id, channel_id, label, int(pinned), created_by),
        )
        await self.conn.commit()
        dash = await self.get_dashboard(cur.lastrowid)
        assert dash is not None
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

    async def remove_dashboards_for_channel(self, channel_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM dashboards WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()
        return cur.rowcount

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

    async def delete_stream(self, repo: str, stream: str) -> None:
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
