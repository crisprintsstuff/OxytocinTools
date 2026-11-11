from bot.config import Config
from bot.embeds import build_embeds
from bot.main import EXTENSIONS, TrackerBot

RELEASE = {
    "id": 1, "tag_name": "v1.0.0", "name": "First", "prerelease": False,
    "html_url": "https://github.com/o/r/releases/v1.0.0", "body": "<!-- hidden -->Notes " * 500,
    "published_at": "2026-01-01T00:00:00Z", "author": {"login": "me"},
    "assets": [{"name": f"a{i}.zip", "browser_download_url": "https://x"} for i in range(8)],
}
COMMIT = {
    "sha": "abcdef1234", "html_url": "https://github.com/o/r/commit/abcdef1234",
    "commit": {"message": "Fix bug\n\nlong body", "author": {"name": "Me"},
               "committer": {"date": "2026-01-01T00:00:00Z"}},
    "author": None,
}


def test_embeds_build_and_respect_limits():
    (embed,) = build_embeds("o/r", "releases", [RELEASE])
    assert len(embed.description) <= 1500 and "hidden" not in embed.description
    (embed,) = build_embeds("o/r", "commits:main", [COMMIT] * 15)
    assert "15 new commits" in embed.title and "5 earlier" in embed.description
    build_embeds("o/r", "issues", [{"number": 1, "title": "t", "body": None, "labels": []}])
    build_embeds("o/r", "pulls", [{"number": 2, "title": "t", "draft": True,
                                   "base": {"ref": "main"}, "head": {"label": "x:y"}}])


async def test_all_commands_register():
    bot = TrackerBot(Config(discord_token="x"))
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
    groups = {g.name: sorted(c.name for c in g.commands) for g in bot.tree.get_commands()}
    assert groups == {
        "repo": ["info", "latest", "list", "track", "untrack"],
        "route": ["add", "list", "remove", "test", "toggle"],
        "admin": ["check-now", "clear-role", "set-role", "status"],
    }
    # Payloads must serialise (catches invalid option definitions).
    for cmd in bot.tree.get_commands():
        cmd.to_dict(bot.tree)
    await bot.close()
