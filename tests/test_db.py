import sqlite3

import pytest


async def test_repo_and_route_crud(db):
    repo = await db.add_repo(10, "Octo", "Repo", "main", 1)
    assert (await db.get_repo(10, "octo", "repo")).id == repo.id
    assert await db.get_repo(11, "octo", "repo") is None
    with pytest.raises(sqlite3.IntegrityError):
        await db.add_repo(10, "octo", "REPO", "main", 1)

    r1 = await db.add_route(repo, 100, "releases", release_kind="stable")
    r2 = await db.add_route(repo, 200, "commits", branch="dev", mention_role_id=5)
    assert r2.stream == "commits:dev" and r2.mention_role_id == 5
    assert [r.id for r in await db.list_routes(10)] == [r1.id, r2.id]
    assert await db.get_route(11, r1.id) is None  # other guilds can't see it

    await db.set_route_enabled(r1.id, False, "boom")
    assert [r.id for r in await db.list_enabled_routes()] == [r2.id]
    assert (await db.get_route(10, r1.id)).last_error == "boom"

    assert await db.remove_routes_for_channel(200) == 1
    await db.remove_repo(repo.id)
    assert await db.list_routes(10) == []


async def test_delete_guild_cascades(db):
    repo = await db.add_repo(10, "o", "r", "main", None)
    await db.add_route(repo, 1, "releases")
    await db.set_admin_role(10, 99)
    assert await db.get_admin_role(10) == 99
    await db.delete_guild(10)
    assert await db.list_repos(10) == []
    assert await db.list_enabled_routes() == []
    assert await db.get_admin_role(10) is None


async def test_seen_and_prune(db):
    await db.mark_seen("o/r", "releases", ["1", "2", "3"])
    await db.conn.execute("UPDATE seen_items SET seen_at = datetime('now', '-60 days')")
    await db.prune_seen("o/r", "releases", ["2"])
    assert await db.seen_ids("o/r", "releases") == {"2"}
