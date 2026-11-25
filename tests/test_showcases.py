from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import discord
import pytest

from bot.db import Database
from bot.github import GitHubClient, GitHubError
from bot.models import Showcase
from bot.showcases import (
    CHUNK_LIMIT,
    MAX_MESSAGES,
    ShowcaseManager,
    clean_markdown,
    extract_hero_image,
    extract_section,
    render_file_showcase,
    render_releases_showcase,
    split_markdown_chunks,
)


@pytest.fixture
async def db():
    database = await Database.open(":memory:")
    try:
        yield database
    finally:
        await database.close()


class _FakePartialMessage:
    def __init__(self, channel: _FakeChannel, mid: int):
        self.channel = channel
        self.id = mid

    async def edit(self, *, embed=None, view=None):
        if self.id in self.channel.missing_ids:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing")
        self.channel.edits.append((self.id, embed, view))

    async def delete(self):
        self.channel.deleted.append(self.id)
        self.channel.messages.pop(self.id, None)

    async def pin(self, *, reason=None):
        self.channel.pinned.append(self.id)


class _FakeSentMessage:
    def __init__(self, channel: _FakeChannel, mid: int):
        self.channel = channel
        self.id = mid

    async def pin(self, *, reason=None):
        self.channel.pinned.append(self.id)


class _FakeChannel(discord.TextChannel):
    def __init__(self, channel_id: int = 555):  # noqa: D107
        self.id = channel_id
        self.next_id = 1000
        self.messages: dict[int, tuple[object, object]] = {}
        self.edits: list[tuple[int, object, object]] = []
        self.deleted: list[int] = []
        self.pinned: list[int] = []
        self.missing_ids: set[int] = set()

    async def send(self, *, embed=None, view=None):
        self.next_id += 1
        mid = self.next_id
        self.messages[mid] = (embed, view)
        return _FakeSentMessage(self, mid)

    def get_partial_message(self, mid: int):
        return _FakePartialMessage(self, mid)


def test_clean_markdown_and_extract_section():
    md = (
        "<!-- comment -->\r\n"
        "# Intro\r\n"
        "Ignored intro text.\r\n\r\n"
        "## 🚀 New Features\r\n"
        "<details><summary>Highlights</summary>\r\n"
        "- Fast sync<br>with ETags\r\n"
        "</details>\r\n"
        "```bash\r\n"
        "# Fake Heading Inside Code\r\n"
        "echo hi\r\n"
        "```\r\n"
        "### Sub-item\r\n"
        "Details here.\r\n\r\n"
        "## Bug Fixes\r\n"
        "- Fixed crash\r\n"
    )
    cleaned = clean_markdown(md)
    assert "comment" not in cleaned
    assert "**Highlights**" in cleaned

    sec = extract_section(cleaned, "New Features, Features")
    assert sec is not None
    assert "Fast sync" in sec
    assert "# Fake Heading Inside Code" in sec
    assert "### Sub-item" in sec
    assert "Fixed crash" not in sec
    assert extract_section(cleaned, "Nonexistent") is None


def test_split_markdown_chunks_truncates_at_max_messages():
    big_para = "A" * 2000
    long_doc = "\n\n".join(f"Section {i}\n{big_para}" for i in range(12))
    chunks, truncated = split_markdown_chunks(long_doc, limit=CHUNK_LIMIT, max_chunks=MAX_MESSAGES)
    assert truncated is True
    assert len(chunks) == MAX_MESSAGES
    assert all(len(c) <= CHUNK_LIMIT for c in chunks)
    assert "truncated" in chunks[-1]


def test_render_file_and_releases_showcases():
    sc_file = Showcase(
        id=1, repo_id=1, guild_id=10, owner="org", name="app",
        channel_id=555, source="file", file_path="FEATURES.md",
        branch="main", section="Added",
    )
    embeds, h1 = render_file_showcase(
        sc_file, "# Features\n\n## Added\n- Showcase boards\n\n## Removed\n- Old thing\n",
        default_branch="main", interval=300,
    )
    assert len(embeds) == 1
    assert "Showcase boards" in (embeds[0].description or "")
    assert "Old thing" not in (embeds[0].description or "")
    assert embeds[0].url == "https://github.com/org/app/blob/main/FEATURES.md"

    sc_rel = Showcase(
        id=2, repo_id=1, guild_id=10, owner="org", name="app",
        channel_id=555, source="releases", section="New Features, Features",
        release_kind="stable", max_releases=5,
    )
    releases = [
        {
            "tag_name": "v2.0.0-rc1", "name": "v2.0.0-rc1", "prerelease": True, "draft": False,
            "html_url": "https://github.com/org/app/releases/tag/v2.0.0-rc1",
            "published_at": "2026-10-01T12:00:00Z",
            "body": "## New Features\n- Preview feature\n",
        },
        {
            "tag_name": "v1.2.0", "name": "Release 1.2", "prerelease": False, "draft": False,
            "html_url": "https://github.com/org/app/releases/tag/v1.2.0",
            "published_at": "2026-09-15T12:00:00Z",
            "body": "## New Features\n- Live showcase command\n## Bug Fixes\n- Typos\n",
        },
        {
            "tag_name": "v1.1.1", "name": "Patch", "prerelease": False, "draft": False,
            "html_url": "https://github.com/org/app/releases/tag/v1.1.1",
            "published_at": "2026-09-01T12:00:00Z",
            "body": "## Bug Fixes\n- Only bug fix\n",
        },
    ]
    rel_embeds, h2 = render_releases_showcase(sc_rel, releases, interval=300)
    assert len(rel_embeds) == 1
    desc = rel_embeds[0].description or ""
    assert "v1.2.0" in desc and "Live showcase command" in desc
    assert "Typos" not in desc
    assert "v2.0.0-rc1" not in desc
    assert "v1.1.1" not in desc  # skipped because no Features section
    assert h1 != h2


async def test_showcase_db_crud_and_lookup(db: Database):
    repo = await db.add_repo(100, "Org", "App", "main", 1)
    sc = await db.add_showcase(
        repo, 555, "file", file_path="FEATURES.md", section="Features",
        title="Product Features", pinned=True, created_by=42,
    )
    assert sc.pinned is True
    await db.set_showcase_messages(sc.id, [1234, 5678])
    assert await db.find_showcase_by_message(123) is None  # no substring false positive
    found = await db.find_showcase_by_message(5678)
    assert found is not None and found.id == sc.id

    await db.set_showcase_state(sc.id, last_hash="abc", last_change=100.0, error=None)
    updated = await db.update_showcase(
        sc.id, file_path="CHANGELOG.md", branch="dev", section=None,
        release_kind="stable", max_releases=10, title=None, pinned=False,
    )
    assert updated is not None
    assert updated.file_path == "CHANGELOG.md"
    assert updated.branch == "dev"
    assert updated.pinned is False
    assert updated.last_hash is None  # reset for immediate re-render

    assert await db.remove_showcases_for_channel(555) == 1
    assert await db.list_showcases(100) == []


async def test_showcase_manager_multi_message_reconcile_and_cache(db: Database):
    repo = await db.add_repo(100, "Org", "App", "main", 1)
    sc = await db.add_showcase(repo, 555, "file", file_path="FEATURES.md", pinned=True)
    channel = _FakeChannel(555)

    p1 = "Part 1: " + ("x" * 2500)
    p2 = "Part 2: " + ("y" * 2500)
    gh_calls = 0
    current_text = f"{p1}\n\n{p2}"

    async def fake_get_file(owner, name, path, ref=None, etag=None):
        nonlocal gh_calls
        gh_calls += 1
        if etag == '"etag-1"' and current_text.startswith("Part 1"):
            return None, '"etag-1"'
        return current_text, '"etag-2"' if "Updated" in current_text else '"etag-1"'

    alerts: list[tuple[int, str, str, str]] = []

    async def fake_send_alert(gid, key, title, detail):
        alerts.append((gid, key, title, detail))
        return True

    bot = SimpleNamespace(
        db=db,
        poller=SimpleNamespace(interval=300),
        github=SimpleNamespace(get_file_content=fake_get_file),
        get_channel=lambda cid: channel if cid == 555 else None,
        send_alert=fake_send_alert,
    )
    mgr = ShowcaseManager(bot)  # type: ignore[arg-type]

    # First post: spans 2 messages, pins first message, puts buttons only on the last message.
    await mgr.refresh(sc)
    assert len(sc.message_ids) == 2
    first_id, second_id = sc.message_ids
    assert channel.pinned == [first_id]
    assert channel.messages[first_id][1] is None
    assert channel.messages[second_id][1] is not None

    # No-op poll: ETag 304 -> zero Discord edits.
    await mgr.refresh(sc)
    assert channel.edits == []
    assert gh_calls == 2

    # Content shrinks to 1 chunk: edits first message, deletes trailing second message.
    current_text = "Short single chunk update"
    await mgr.refresh(sc)
    assert sc.message_ids == [first_id]
    assert channel.deleted == [second_id]
    assert channel.edits[-1][0] == first_id

    # Simulate deleted message: reconciles by posting a fresh message.
    await mgr.message_deleted(first_id)
    reconciled = await db.get_showcase(sc.id)
    assert reconciled is not None
    assert len(reconciled.message_ids) == 1 and reconciled.message_ids[0] != first_id
    sc = reconciled

    # Error handling records error and notifies alert channel.
    async def failing_get_file(owner, name, path, ref=None, etag=None):
        raise GitHubError("File not found", 404)

    bot.github.get_file_content = failing_get_file
    await mgr.refresh(sc, force=True)
    refreshed = await db.get_showcase(sc.id)
    assert refreshed is not None and "GitHub:" in (refreshed.last_error or "")
    assert len(alerts) == 1


async def test_github_file_and_releases_methods():
    gh = GitHubClient()
    encoded = base64.b64encode("# Features\n- Works\n".encode("utf-8")).decode("ascii")
    calls: list[tuple[str, dict | None, str | None]] = []

    async def fake_get(path, params=None, etag=None, *, repo=None):
        calls.append((path, params, etag))
        if etag == '"same"':
            return 304, None, '"same"'
        if path.endswith("/releases"):
            return 200, [{"tag_name": "v1.0.0"}], '"rel-1"'
        return 200, {"type": "file", "encoding": "base64", "content": encoded}, '"file-1"'

    gh._get = fake_get  # type: ignore[method-assign]

    text, etag = await gh.get_file_content("Org", "App", "/docs/FEATURES.md", ref="main")
    assert text == "# Features\n- Works\n"
    assert etag == '"file-1"'
    assert calls[0] == ("/repos/Org/App/contents/docs/FEATURES.md", {"ref": "main"}, None)

    unchanged, etag2 = await gh.get_file_content("Org", "App", "docs/FEATURES.md", etag='"same"')
    assert unchanged is None and etag2 == '"same"'

    rels, rel_etag = await gh.list_releases("Org", "App", per_page=10)
    assert rels == [{"tag_name": "v1.0.0"}] and rel_etag == '"rel-1"'


def test_clean_markdown_github_readme_html_badges_nav_and_hero():
    readme = """<p align="center">
  <img src="docs/images/banner.png" alt="App Banner" width="100%">
</p>

<p align="center">
  <a href="https://github.com/org/app/releases"><img alt="release" src="https://img.shields.io/github/v/release/org/app"></a>
  <a href="https://github.com/org/app/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/org/app/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="stage" src="https://img.shields.io/badge/stage-beta-blue">
  <a href="#license"><img alt="license" src="https://img.shields.io/badge/license-MIT-4c1"></a>
</p>

<p align="center">
  <a href="https://github.com/org/app/releases/tag/v1.0"><b>Download</b></a> ·
  <a href="https://github.com/org/app/wiki"><b>Wiki &amp; Docs</b></a> ·
  <a href="docs/TESTING_GUIDE.md">Testing guide</a> ·
  <a href="#license">License</a>
</p>

![Screenshot](docs/images/screenshot.png)

[![Build](https://img.shields.io/badge/build-passing-green)](https://ci.example.com)

Read the [release notes](docs/releases/v1.0.md) for more details.
"""
    sc = Showcase(
        id=1, repo_id=1, guild_id=10, owner="org", name="app",
        channel_id=555, source="file", file_path="README.md", branch="main",
    )
    embeds, _ = render_file_showcase(sc, readme, default_branch="main", interval=300)
    assert len(embeds) == 1
    desc = embeds[0].description or ""

    for bad in ("<p", "</p", "<a ", "</a>", "<img", "<b", "</b>", "![", "shields.io", "badge.svg"):
        assert bad not in desc

    assert (
        "[**Download**](https://github.com/org/app/releases/tag/v1.0) · "
        "[**Wiki & Docs**](https://github.com/org/app/wiki) · "
        "[Testing guide](https://github.com/org/app/blob/main/docs/TESTING_GUIDE.md) · "
        "[License](https://github.com/org/app/blob/main/README.md#license)"
    ) in desc
    assert "[release notes](https://github.com/org/app/blob/main/docs/releases/v1.0.md)" in desc
    assert embeds[0].image.url == "https://raw.githubusercontent.com/org/app/main/docs/images/banner.png"


def test_clean_markdown_html_formatting_lists_tables_and_entities():
    raw = """<h2>What&#39;s New &amp; Improved</h2>
<p>First <strong>bold</strong>, <em>italic</em>, <u>under</u>, <del>old</del>, and <code>inline()</code>.</p>
<blockquote><p>Quoted &mdash; line 1<br>line 2</p></blockquote>
<ul>
  <li>Alpha<ul><li>Nested &nbsp; item</li></ul></li>
  <li>Beta</li>
</ul>
<ol start="3">
  <li>Third</li>
  <li>Fourth</li>
</ol>
<table>
  <tr><th>Feature</th><th>Status</th></tr>
  <tr><td>Sync</td><td>Ready</td></tr>
  <tr><td>OnlyCol</td><td></td></tr>
</table>
<table>
  <tr><th>Name</th><th>Min</th><th>Max</th></tr>
  <tr><td>Small</td><td>1k</td><td>10k</td></tr>
</table>
<table>
  <tr><td><img src="a.png"></td><td><img src="b.png"></td></tr>
</table>
<pre><code class="language-rust">fn main() {}</code></pre>
<style>.hidden { display: none; }</style>
<script>alert("nope")</script>
"""
    cleaned = clean_markdown(raw)
    assert "## What's New & Improved" in cleaned
    assert "First **bold**, *italic*, __under__, ~~old~~, and `inline()`." in cleaned
    assert "> Quoted — line 1" in cleaned
    assert "> line 2" in cleaned
    assert "- Alpha\n  - Nested   item" in cleaned or "- Alpha\n  - Nested item" in cleaned
    assert "- Beta" in cleaned
    assert "3. Third" in cleaned
    assert "4. Fourth" in cleaned
    assert "- **Sync:** Ready" in cleaned
    assert "- OnlyCol" in cleaned
    assert "- **Small** — Min: 1k · Max: 10k" in cleaned
    assert "```\nfn main() {}\n```" in cleaned
    assert "display: none" not in cleaned
    assert "alert(" not in cleaned
    assert "<" not in cleaned or "fn main" in cleaned


def test_clean_markdown_preserves_code_discord_tokens_and_generics():
    raw = (
        "Released <t:1700000000:d> in <#123456> by <@789> — see <https://example.com>.\n\n"
        "Use `<span>keep</span>` and `docs/guide.md` (`[keep](rel.md)`) and type `Vec<String>`.\n"
        "Outside code: Vec<String> and Option<T> stay intact, while a < b && c > d.\n\n"
        "```html\n"
        "<div class=\"x\"><p>Keep HTML inside fenced code</p></div>\n"
        "```\n"
    )
    cleaned = clean_markdown(raw, owner="org", name="app", ref="main", file_path="README.md")
    assert "<t:1700000000:d>" in cleaned
    assert "<#123456>" in cleaned
    assert "<@789>" in cleaned
    assert "<https://example.com>" in cleaned
    assert "`<span>keep</span>`" in cleaned
    assert "`[keep](rel.md)`" in cleaned
    assert "Vec<String>" in cleaned
    assert "Option<T>" in cleaned
    assert "a < b && c > d" in cleaned
    assert '<div class="x"><p>Keep HTML inside fenced code</p></div>' in cleaned


def test_section_showcase_scopes_hero_image_and_relative_subdir_links():
    readme = """<p align="center"><img src="docs/images/banner.png"></p>

## Other
Intro text.

## Highlights
- Added [the guide](sub/guide.md) and [parent](../TOP.md).
"""
    sc_sec = Showcase(
        id=2, repo_id=1, guild_id=10, owner="org", name="app",
        channel_id=555, source="file", file_path="docs/README.md",
        branch="main", section="Highlights",
    )
    embeds, _ = render_file_showcase(sc_sec, readme, default_branch="main", interval=300)
    assert embeds[0].image.url is None
    desc = embeds[0].description or ""
    assert "[the guide](https://github.com/org/app/blob/main/docs/sub/guide.md)" in desc
    assert "[parent](https://github.com/org/app/blob/main/TOP.md)" in desc

    assert extract_hero_image(
        "[![b](https://img.shields.io/badge/x-y-blue)](https://x.com)\n![Shot](docs/shot.jpg)",
        owner="org", name="app", ref="main", file_path="README.md",
    ) == "https://raw.githubusercontent.com/org/app/main/docs/shot.jpg"


async def test_showcase_http_cache_roundtrip_and_legacy_double_encoded_recovery(db: Database):
    repo = await db.add_repo(100, "Org", "App", "main", 1)
    sc_file = await db.add_showcase(repo, 555, "file", file_path="README.md")
    sc_rel = await db.add_showcase(repo, 556, "releases")
    ch_file = _FakeChannel(555)
    ch_rel = _FakeChannel(556)

    file_calls: list[str | None] = []
    rel_calls: list[str | None] = []

    async def fake_get_file(owner, name, path, ref=None, etag=None):
        file_calls.append(etag)
        if etag == '"f-1"':
            return None, '"f-1"'
        if etag == '"f-2"':
            return None, '"f-2"'
        return "<b>Fresh</b>", '"f-2"' if file_calls.count(None) > 1 else '"f-1"'

    async def fake_list_rels(owner, name, per_page=30, etag=None):
        rel_calls.append(etag)
        if etag == '"r-1"':
            return None, '"r-1"'
        return [{
            "tag_name": "v1.0.0", "name": "v1.0.0", "prerelease": False, "draft": False,
            "html_url": "https://github.com/Org/App/releases/tag/v1.0.0",
            "published_at": "2026-10-01T12:00:00Z",
            "body": "<p>First release</p>",
        }], '"r-1"'

    def _get_ch(cid):
        return ch_file if cid == 555 else (ch_rel if cid == 556 else None)

    bot = SimpleNamespace(
        db=db,
        poller=SimpleNamespace(interval=300),
        github=SimpleNamespace(get_file_content=fake_get_file, list_releases=fake_list_rels),
        get_channel=_get_ch,
    )

    mgr1 = ShowcaseManager(bot)  # type: ignore[arg-type]
    await mgr1.refresh(sc_file)
    await mgr1.refresh(sc_rel)

    # Simulate restart with a fresh ShowcaseManager loading ETag + body from SQLite http_cache.
    mgr2 = ShowcaseManager(bot)  # type: ignore[arg-type]
    await mgr2.refresh(sc_file, force=True)
    await mgr2.refresh(sc_rel, force=True)
    assert file_calls == [None, '"f-1"']
    assert rel_calls == [None, '"r-1"']
    assert ch_rel.edits and "First release" in (ch_rel.edits[-1][1].description or "")

    # Seed legacy double-encoded string in http_cache and verify recovery.
    cache_key = "sc:file:org/app:main:README.md"
    await db.set_http_cache(cache_key, '"legacy-etag"', json.dumps("# Legacy Double Encoded"))
    mgr3 = ShowcaseManager(bot)  # type: ignore[arg-type]
    await mgr3.refresh(sc_file, force=True)
    assert file_calls[-1] is None
    repaired = await db.get_http_cache(cache_key)
    assert repaired == ('"f-2"', "<b>Fresh</b>")
