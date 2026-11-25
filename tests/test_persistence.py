import json
import os
import sqlite3

import pytest

from bot.config import PROJECT_ROOT, Config
from bot.db import Database


def test_config_resolves_relative_paths_from_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", "custom/bot.db")
    monkeypatch.delenv("CONFIG_BACKUP_PATH", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)

    cfg = Config.from_env()
    assert cfg.database_path == str(PROJECT_ROOT / "custom/bot.db")
    assert cfg.config_backup_path == str(PROJECT_ROOT / "custom/config-backup.json")

    monkeypatch.setenv("CONFIG_BACKUP_PATH", "off")
    assert Config.from_env().config_backup_path is None


async def test_wal_checkpoint_and_rolling_backups(tmp_path):
    db_path = str(tmp_path / "tracker.db")
    db = await Database.open(db_path)
    repo = await db.add_repo(1, "o", "r", "main", None)
    await db.add_route(repo, 10, "releases")

    b1 = await db.backup(keep=2)
    b2 = await db.backup(keep=2)
    b3 = await db.backup(keep=2)
    assert b1 and b2 and b3
    backups = sorted(os.listdir(tmp_path / "backups"))
    assert len(backups) == 2
    assert os.path.basename(b1) not in backups
    assert os.path.basename(b2) in backups and os.path.basename(b3) in backups

    with sqlite3.connect(b3) as conn:
        rows = conn.execute("SELECT owner, name FROM repos").fetchall()
        assert rows == [("o", "r")]

    await db.close()
    wal_path = tmp_path / "tracker.db-wal"
    assert not wal_path.exists() or wal_path.stat().st_size == 0


async def test_export_import_and_auto_snapshot(tmp_path):
    db_path = str(tmp_path / "tracker.db")
    snap_path = str(tmp_path / "config-backup.json")
    db = await Database.open(db_path, config_backup_path=snap_path)

    await db.set_admin_role(100, 200)
    await db.set_issue_access(100, "role")
    await db.set_issue_role(100, 300)
    repo = await db.add_repo(100, "Org", "App", "main", 1)
    await db.set_repo_issue_access(repo.id, "everyone")
    route = await db.add_route(repo, 11, "releases", release_kind="stable", tag_pattern="v*")
    await db.set_route_enabled(route.id, False, "paused")
    dash = await db.add_dashboard(repo, 12, label="bug", pinned=True, created_by=1, kind="issues")
    await db.set_dashboard_message(dash.id, 999)
    await db.add_watch(repo, "role", role_id=400, channel_id=13, label="bug")
    await db.add_watch(repo, "forum", channel_id=14, label="triage", close_threads=False)
    await db.add_pr_tracker(repo, 15, branch="main", include_drafts=True, include_bots=False,
                            threads=True, mention_role_id=500, created_by=1)

    assert os.path.exists(snap_path)
    exported = json.loads((tmp_path / "config-backup.json").read_text(encoding="utf-8"))
    await db.close()

    # Restore into a brand-new empty database
    db2_path = str(tmp_path / "restored.db")
    db2 = await Database.open(db2_path)
    assert not await db2.has_any_config()
    counts = await db2.load_config_snapshot(snap_path)
    assert counts == {
        "guilds": 1, "repos": 1, "routes": 1,
        "dashboards": 1, "watches": 2, "pr_trackers": 1,
    }
    assert await db2.get_admin_role(100) == 200
    assert await db2.get_issue_settings(100) == ("role", 300)
    restored_repo = await db2.get_repo(100, "org", "app")
    assert restored_repo is not None and restored_repo.issue_access == "everyone"
    (r2,) = await db2.list_routes(100)
    assert (r2.channel_id, r2.release_kind, r2.tag_pattern, r2.enabled) == (11, "stable", "v*", False)
    (d2,) = await db2.list_dashboards(100)
    assert (d2.channel_id, d2.message_id, d2.label, d2.pinned) == (12, 999, "bug", True)
    assert len(await db2.list_watches(100)) == 2
    (t2,) = await db2.list_pr_trackers(100)
    assert (t2.channel_id, t2.branch, t2.include_drafts, t2.mention_role_id) == (15, "main", True, 500)

    # Importing again is idempotent (no duplicates created)
    counts_again = await db2.import_config(exported, guild_id=100)
    assert counts_again == {
        "guilds": 1, "repos": 0, "routes": 0,
        "dashboards": 0, "watches": 0, "pr_trackers": 0,
    }
    with pytest.raises(ValueError, match="does not contain"):
        await db2.import_config(exported, guild_id=999)
    await db2.close()


async def test_update_route_watch_and_pr_tracker(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    route = await db.add_route(repo, 10, "releases", release_kind="all")
    updated_route = await db.update_route(
        route.id, 1,
        channel_id=20, release_kind="stable", tag_pattern="v2.*",
        branch=None, mention_role_id=55, crosspost=True,
        pr_events=None, include_drafts=False, include_bots=False,
    )
    assert updated_route is not None
    assert (updated_route.channel_id, updated_route.release_kind, updated_route.tag_pattern,
            updated_route.mention_role_id, updated_route.crosspost) == (20, "stable", "v2.*", 55, True)

    watch = await db.add_watch(repo, "role", role_id=10, channel_id=11, label="bug")
    updated_watch = await db.update_watch(
        watch.id, 1, role_id=20, channel_id=None, label="enhancement", close_threads=True
    )
    assert updated_watch is not None
    assert (updated_watch.role_id, updated_watch.channel_id, updated_watch.label) == (20, None, "enhancement")

    tracker = await db.add_pr_tracker(repo, 30, branch="main", include_drafts=False,
                                      include_bots=False, threads=True, mention_role_id=None,
                                      created_by=None)
    updated_tracker = await db.update_pr_tracker(
        tracker.id, 1, channel_id=31, branch=None, include_drafts=True,
        include_bots=True, threads=False, mention_role_id=77,
    )
    assert updated_tracker is not None
    assert (updated_tracker.channel_id, updated_tracker.branch, updated_tracker.include_drafts,
            updated_tracker.include_bots, updated_tracker.threads,
            updated_tracker.mention_role_id) == (31, None, True, True, False, 77)
