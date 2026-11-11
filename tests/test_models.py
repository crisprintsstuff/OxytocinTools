import pytest

from bot.models import Route, parse_repo, pattern_matches, stream_key, validate_pattern


@pytest.mark.parametrize("value,expected", [
    ("octocat/Hello-World", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World", ("octocat", "Hello-World")),
    ("github.com/octocat/hello.js.git", ("octocat", "hello.js")),
    ("  https://www.github.com/a-b/c_d/  ", ("a-b", "c_d")),
])
def test_parse_repo(value, expected):
    assert parse_repo(value) == expected


@pytest.mark.parametrize("value", ["nope", "a/b/c", "https://gitlab.com/a/b", ""])
def test_parse_repo_invalid(value):
    with pytest.raises(ValueError):
        parse_repo(value)


def test_patterns():
    assert pattern_matches(None, "anything")
    assert pattern_matches("v2.*", "v2.1.0")
    assert not pattern_matches("v2.*", "v3.0.0")
    assert pattern_matches(r"re:^v\d+\.\d+\.\d+$", "v1.2.3")
    assert not pattern_matches(r"re:^v\d+\.\d+\.\d+$", "v1.2.3-rc1")
    with pytest.raises(ValueError):
        validate_pattern("re:(")


def test_stream_key():
    assert stream_key("releases") == "releases"
    assert stream_key("commits", "main") == "commits:main"
    with pytest.raises(ValueError):
        stream_key("commits")


def _route(**kw):
    base = dict(id=1, repo_id=1, guild_id=1, owner="o", name="r", channel_id=1,
                event_type="releases")
    base.update(kw)
    return Route(**base)


def test_release_filters():
    stable = {"tag_name": "v1.0.0", "prerelease": False}
    beta = {"tag_name": "v1.1.0-beta", "prerelease": True}
    assert _route().accepts(stable) and _route().accepts(beta)
    assert _route(release_kind="stable").accepts(stable)
    assert not _route(release_kind="stable").accepts(beta)
    assert _route(release_kind="prerelease").accepts(beta)
    assert not _route(release_kind="prerelease").accepts(stable)
    assert not _route(tag_pattern="v2.*").accepts(stable)
