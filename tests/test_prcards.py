from types import SimpleNamespace

import discord

from bot.embeds import PURPLE
from bot.github import GitHubError
from bot.models import PRTracker
from bot.prcards import (PRCardManager, build_status, render_card, summarize_checks,
                         summarize_reviews, timeline)


def run(name, status="completed", conclusion="success", started="2026-11-20T10:00:00Z"):
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started}


def test_summarize_checks():
    assert summarize_checks(None, None)["state"] == "unavailable"
    assert summarize_checks([], [])["state"] == "none"
    s = summarize_checks([run("lint"), run("windows", conclusion="failure"),
                          run("mac", status="in_progress", conclusion=None)],
                         [{"state": "success", "context": "deploy"}])
    assert (s["state"], s["passed"], s["failed"], s["pending"], s["failed_names"]) == (
        "failing", 2, 1, 1, ["windows"])
    rerun = summarize_checks([run("windows", conclusion="failure", started="2026-11-20T10:00:00Z"),
                              run("windows", started="2026-11-20T11:00:00Z")], [])
    assert rerun["state"] == "passing" and rerun["passed"] == 1  # only the newest run counts
    assert summarize_checks([run("a", conclusion="skipped")], None)["state"] == "passing"
    assert summarize_checks(None, [{"state": "pending", "context": "ci"}])["state"] == "pending"


def review(user, state, at):
    return {"user": {"login": user}, "state": state, "submitted_at": at}


def test_summarize_reviews():
    reviews = [review("ana", "CHANGES_REQUESTED", "1"), review("ana", "COMMENTED", "2"),
               review("ben", "APPROVED", "1"), review("ben", "DISMISSED", "3"),
               review("cris", "APPROVED", "1")]
    assert summarize_reviews(reviews, author="cris") == {"ana": "CHANGES_REQUESTED", "ben": "DISMISSED"}


def pr(n=1, *, state="open", draft=False, merged=False, sha="aaaaaaa1", requested=(), user_type="User",
       base="main", mergeable_state="clean"):
    return {
        "number": n, "title": f"Add feature {n}", "state": state, "draft": draft,
        "html_url": f"https://github.com/o/r/pull/{n}", "created_at": "2026-11-20T10:00:00Z",
        "merged_at": "2026-11-21T10:00:00Z" if merged else None,
        "closed_at": "2026-11-21T10:00:00Z" if state == "closed" else None,
        "merged_by": {"login": "cris"} if merged else None,
        "user": {"login": "dev", "type": user_type}, "head": {"ref": "feature", "sha": sha},
        "base": {"ref": base}, "requested_reviewers": [{"login": u} for u in requested],
        "requested_teams": [], "mergeable_state": mergeable_state,
        "additions": 120, "deletions": 30, "changed_files": 5, "commits": 3,
    }


def status(p, checks=None, reviews=None):
    return build_status(p, checks or summarize_checks([run("ci")], []), reviews or {})


def test_timeline():
    old = status(pr(draft=True, requested=()), summarize_checks([run("ci", "in_progress", None)], []))
    assert timeline(None, old) == []
    new = status(pr(sha="bbbbbbb2", requested=("ana",), mergeable_state="dirty"),
                 summarize_checks([run("ci", conclusion="failure")], []), {"ben": "APPROVED"})
    assert timeline(old, new) == [
        "👀 Ready for review", "⬆️ New commits pushed (`bbbbbbb`)", "❌ Checks failed: **ci**",
        "✅ Approved by **ben**", "👋 Review requested from **ana**", "⚠️ Has merge conflicts"]
    fixed = status(pr(sha="bbbbbbb2", requested=("ana",)), reviews={"ben": "APPROVED"})
    assert timeline(new, fixed) == ["✅ All checks passed (1)", "✅ Merge conflicts resolved"]
    merged = status(pr(state="closed", merged=True))
    assert timeline(fixed, merged) == ["🟣 Merged by **cris**"]
    assert timeline(merged, status(pr())) [0] == "🔄 Reopened"
    assert timeline(fixed, status(pr(state="closed"))) == ["🔴 Closed without merging"]


def test_render_card():
    p = pr(requested=("ana",))
    embed, digest = render_card("o/r", p, status(p, reviews={"ben": "APPROVED", "eve": "CHANGES_REQUESTED"}))
    assert embed.title == "🟢 #1 Add feature 1"
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Checks"] == "✅ 1 passed"
    assert fields["Reviews"] == "📝 Changes requested by eve\n✅ 1 approval (ben)\n👋 Waiting on ana"
    assert fields["Merge"] == "✅ Ready to merge"
    assert embed.footer.text == "+120 −30 · 5 files · 3 commits"
    assert "`feature` → `main`" in embed.description
    assert render_card("o/r", p, status(p, reviews={"ben": "APPROVED", "eve": "CHANGES_REQUESTED"}))[1] == digest

    done = pr(state="closed", merged=True)
    embed, _ = render_card("o/r", done, status(done))
    assert embed.title.startswith("🟣") and embed.color == PURPLE and not embed.fields
    assert "Merged <t:" in embed.description and "by **cris**" in embed.description
    draft = pr(draft=True)
    embed, _ = render_card("o/r", draft, status(draft, summarize_checks(None, None)))
    assert embed.title.startswith("⚪")
    assert {f.name: f.value for f in embed.fields}["Merge"] == "📝 Draft"
    assert "needs the **Checks** permission" in embed.fields[0].value


def test_tracker_filters():
    t = PRTracker(1, 1, 1, "o", "r", 10)
    assert t.wants(pr()) and not t.wants(pr(draft=True)) and not t.wants(pr(user_type="Bot"))
    assert PRTracker(1, 1, 1, "o", "r", 10, include_drafts=True).wants(pr(draft=True))
    assert not PRTracker(1, 1, 1, "o", "r", 10, branch="main").wants(pr(base="dev"))


# --------------------------------------------------------------- full cycle
class FakeGitHub:
    def __init__(self):
        self.data = {}
        self.forbidden = set()

    async def _get(self, path, params=None, etag=None):
        if path in self.forbidden:
            raise GitHubError("Resource not accessible by integration", 403)
        data = self.data[path]
        tag = str(hash(repr(data)))
        return (304, None, etag) if etag == tag else (200, data, tag)


class FakeThread(discord.Thread):
    def __init__(self, thread_id):
        self.id, self.archived, self.sent = thread_id, False, []

    async def send(self, text, **kwargs):
        self.sent.append(text)
        self.archived = False

    async def edit(self, **kwargs):
        self.archived = kwargs.get("archived", self.archived)


class FakeChannel(discord.TextChannel):
    def __init__(self):
        self.id, self.sent, self.edits, self.made_threads, self.deleted = 10, [], [], {}, set()

    async def send(self, content=None, **kwargs):
        channel, msg_id = self, 500 + len(self.sent)
        self.sent.append((content, kwargs))

        async def create_thread(name, auto_archive_duration):
            thread = FakeThread(900 + msg_id)
            thread.name = name
            channel.made_threads[thread.id] = thread
            return thread
        return SimpleNamespace(id=msg_id, create_thread=create_thread)

    def get_partial_message(self, message_id):
        async def edit(**kwargs):
            if message_id in self.deleted:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Unknown Message"), "gone")
            self.edits.append((message_id, kwargs))
        return SimpleNamespace(edit=edit)


async def setup(db):
    repo = await db.add_repo(1, "o", "r", "main", None)
    tracker = await db.add_pr_tracker(repo, 10, branch=None, include_drafts=False, include_bots=False,
                                      threads=True, mention_role_id=77, created_by=None)
    gh, channel = FakeGitHub(), FakeChannel()
    bot = SimpleNamespace(db=db, github=gh,
                          get_channel=lambda cid: channel if cid == 10 else channel.made_threads.get(cid))
    return PRCardManager(bot), tracker, gh, channel


def serve(gh, prs, *, checks=None, reviews=None, extra=()):
    gh.data["/repos/o/r/pulls"] = [p for p in prs if p["state"] == "open"]
    for p in list(prs) + list(extra):
        gh.data[f"/repos/o/r/pulls/{p['number']}"] = p
        gh.data[f"/repos/o/r/pulls/{p['number']}/reviews"] = (reviews or {}).get(p["number"], [])
        sha = p["head"]["sha"]
        gh.data[f"/repos/o/r/commits/{sha}/check-runs"] = {"check_runs": (checks or {}).get(sha, [run("ci")])}
        gh.data[f"/repos/o/r/commits/{sha}/status"] = {"statuses": []}


async def test_cards_follow_a_pr_from_open_to_merged(db):
    manager, tracker, gh, channel = await setup(db)
    serve(gh, [pr(1), pr(2, draft=True), pr(3, user_type="Bot")],
          checks={"aaaaaaa1": [run("ci", "in_progress", None)]})
    await manager.refresh_all()

    assert len(channel.sent) == 1  # drafts and bots don't get cards
    content, kwargs = channel.sent[0]
    assert content == "<@&77>" and kwargs["embed"].title == "🟢 #1 Add feature 1"
    assert [getattr(b, "item", b).label for b in kwargs["view"].children] == [
        "Open PR", "Checks", "Files", "Refresh"
    ]
    (card,) = await db.list_pr_cards(tracker.id)
    thread = channel.made_threads[card.thread_id]
    assert thread.name == "#1 Add feature 1"

    await manager.refresh_all()  # nothing changed: no edits, no timeline
    assert channel.edits == [] and thread.sent == []

    serve(gh, [pr(1, requested=("ana",)), pr(2, draft=True)],
          checks={"aaaaaaa1": [run("ci", conclusion="failure")]},
          reviews={1: [review("ben", "APPROVED", "2026-11-20T12:00:00Z")]})
    await manager.refresh_all()
    assert len(channel.edits) == 1 and channel.edits[0][0] == card.message_id
    assert thread.sent == ["❌ Checks failed: **ci**\n✅ Approved by **ben**\n👋 Review requested from **ana**"]

    # The draft is marked ready: it gets a card now.
    serve(gh, [pr(1, requested=("ana",)), pr(2)], checks={"aaaaaaa1": [run("ci", conclusion="failure")]},
          reviews={1: [review("ben", "APPROVED", "2026-11-20T12:00:00Z")]})
    await manager.refresh_all()
    assert len(channel.sent) == 2 and channel.sent[1][1]["embed"].title.startswith("🟢 #2")

    # PR 1 is merged: it leaves the open list, its card turns purple and the thread is archived.
    serve(gh, [pr(2)], extra=[pr(1, state="closed", merged=True)])
    await manager.refresh_all()
    final = [kw for mid, kw in channel.edits if mid == card.message_id][-1]["embed"]
    assert final.title.startswith("🟣 #1") and final.color == PURPLE
    assert thread.sent[-1].startswith("🟣 Merged by **cris**") and thread.archived
    cards = {c.number: c for c in await db.list_pr_cards(tracker.id)}
    assert cards[1].state == "merged" and cards[2].state == "open"

    edits = len(channel.edits)
    await manager.refresh_all()  # finished cards aren't fetched again
    assert len(channel.edits) == edits
    assert "/repos/o/r/pulls/1" not in {k[0] for k in manager._cache}


async def test_checks_permission_and_deleted_card(db):
    manager, tracker, gh, channel = await setup(db)
    serve(gh, [pr(1)])
    gh.forbidden = {"/repos/o/r/commits/aaaaaaa1/check-runs", "/repos/o/r/commits/aaaaaaa1/status"}
    await manager.refresh_all()
    embed = channel.sent[0][1]["embed"]
    assert "needs the **Checks** permission" in embed.fields[0].value
    assert manager.checks_blocked("O/R")

    (card,) = await db.list_pr_cards(tracker.id)
    channel.deleted.add(card.message_id)  # someone deleted the card
    serve(gh, [pr(1, requested=("ana",))])
    await manager.refresh_all()
    assert len(channel.sent) == 2 and channel.sent[1][0] is None  # re-posted, no second ping
    (card,) = await db.list_pr_cards(tracker.id)
    assert card.message_id == 501 and card.thread_id == 1400  # keeps its original thread


async def test_pr_cards_fetch_channel_fallback(db):
    from unittest.mock import AsyncMock

    manager, tracker, gh, channel = await setup(db)
    manager.bot.get_channel = lambda cid: None
    manager.bot.fetch_channel = AsyncMock(return_value=channel)
    serve(gh, [pr(1)])
    await manager.refresh_all()
    manager.bot.fetch_channel.assert_awaited_with(10)
    assert len(channel.sent) == 1
