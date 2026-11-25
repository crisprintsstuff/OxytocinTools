from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot.checks import UserError
from bot.db import Database
from bot.notify import (
    NotifyPanelManager,
    build_panel_embed,
    panel_view,
    validate_assignable_role,
    validate_emoji,
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
        self.channel.messages[self.id] = (embed, view)
        self.channel.edits.append((self.id, embed, view))

    async def delete(self):
        self.channel.deleted.append(self.id)
        self.channel.messages.pop(self.id, None)


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

    async def send(self, *, embed=None, view=None, allowed_mentions=None):
        self.next_id += 1
        mid = self.next_id
        self.messages[mid] = (embed, view)
        return _FakeSentMessage(self, mid)

    def get_partial_message(self, mid: int):
        return _FakePartialMessage(self, mid)


class _FakeRole:
    def __init__(
        self,
        role_id: int,
        name: str,
        position: int = 1,
        *,
        default: bool = False,
        managed: bool = False,
        perms: discord.Permissions | None = None,
    ):
        self.id = role_id
        self.name = name
        self.position = position
        self._default = default
        self.managed = managed
        self.permissions = perms or discord.Permissions.none()
        self.mention = f"<@&{role_id}>"

    def is_default(self) -> bool:
        return self._default

    def __ge__(self, other: object) -> bool:
        return self.position >= getattr(other, "position", 0)

    def __gt__(self, other: object) -> bool:
        return self.position > getattr(other, "position", 0)


class _FakeMember(discord.Member):
    def __init__(self, user_id: int, top_role: _FakeRole, roles: list[_FakeRole] | None = None,
                 perms: discord.Permissions | None = None):  # noqa: D107
        self._id = user_id
        self._top_role = top_role
        self._roles = list(roles or [])
        self._perms = perms or discord.Permissions.none()
        self.added: list[int] = []
        self.removed: list[int] = []

    @property
    def id(self) -> int:
        return self._id

    @property
    def top_role(self):
        return self._top_role

    @property
    def roles(self):
        return self._roles

    @property
    def guild_permissions(self):
        return self._perms

    async def add_roles(self, *roles, reason=None):
        for r in roles:
            if all(existing.id != r.id for existing in self._roles):
                self._roles.append(r)
            self.added.append(r.id)

    async def remove_roles(self, *roles, reason=None):
        remove_ids = {r.id for r in roles}
        self._roles = [r for r in self._roles if r.id not in remove_ids]
        self.removed.extend(remove_ids)


class _FakeInteractionResponse:
    def __init__(self):
        self.sent: list[tuple[str, bool, object]] = []

    async def send_message(self, content: str, *, ephemeral: bool = False, allowed_mentions=None):
        self.sent.append((content, ephemeral, allowed_mentions))


async def test_notify_panel_db_crud_and_cascades(db: Database):
    panel = await db.add_notify_panel(100, 555, title="Pings", description="Pick one", pinned=True, created_by=1)
    assert panel.pinned is True and panel.items == []

    updated = await db.add_notify_panel_item(panel.id, 11, "Releases", emoji="🚀", description="New builds")
    assert updated is not None and len(updated.items) == 1
    assert updated.items[0].label == "Releases"

    # Re-adding the same role updates metadata instead of duplicating
    updated = await db.add_notify_panel_item(panel.id, 11, "All Releases", emoji="🎉", description="Every release")
    assert updated is not None and len(updated.items) == 1
    assert updated.items[0].label == "All Releases" and updated.items[0].emoji == "🎉"

    await db.set_notify_panel_message(panel.id, 9001)
    found = await db.find_notify_panel_by_message(9001)
    assert found is not None and found.id == panel.id and len(found.items) == 1

    affected = await db.remove_notify_role_across_guild(100, 11)
    assert affected == [panel.id]
    reloaded = await db.get_notify_panel(panel.id, 100)
    assert reloaded is not None and reloaded.items == []

    assert await db.remove_notify_panels_for_channel(555) == 1
    assert await db.list_notify_panels(100) == []


def test_validate_assignable_role_and_emoji():
    bot_top = _FakeRole(1, "Bot", position=10)
    actor_top = _FakeRole(2, "Admin", position=8)
    me = _FakeMember(999, bot_top, perms=discord.Permissions(manage_roles=True))
    actor = _FakeMember(42, actor_top)
    guild = SimpleNamespace(me=me, owner_id=77)

    # Valid notification role
    ok_role = _FakeRole(10, "Releases", position=3)
    validate_assignable_role(guild, ok_role, actor=actor)  # type: ignore[arg-type]

    # Rejects @everyone, managed, dangerous perms, above bot, above actor, missing manage_roles
    with pytest.raises(UserError, match="@everyone"):
        validate_assignable_role(guild, _FakeRole(11, "@everyone", default=True))  # type: ignore[arg-type]
    with pytest.raises(UserError, match="managed"):
        validate_assignable_role(guild, _FakeRole(12, "BotRole", managed=True))  # type: ignore[arg-type]
    with pytest.raises(UserError, match="moderation/admin"):
        validate_assignable_role(
            guild, _FakeRole(13, "Mods", perms=discord.Permissions(ban_members=True))
        )  # type: ignore[arg-type]
    with pytest.raises(UserError, match="above"):
        validate_assignable_role(guild, _FakeRole(14, "HigherThanBot", position=10))  # type: ignore[arg-type]
    with pytest.raises(UserError, match="your own highest role"):
        validate_assignable_role(guild, _FakeRole(15, "HigherThanAdmin", position=9), actor=actor)  # type: ignore[arg-type]

    no_perm_me = _FakeMember(999, bot_top, perms=discord.Permissions.none())
    with pytest.raises(UserError, match="Manage Roles"):
        validate_assignable_role(SimpleNamespace(me=no_perm_me, owner_id=77), ok_role)  # type: ignore[arg-type]

    assert validate_emoji(" 🔔 ") == "🔔"
    assert validate_emoji("<:oxy:123456>") == "<:oxy:123456>"
    assert validate_emoji("   ") is None
    with pytest.raises(UserError, match="Unicode emoji"):
        validate_emoji("bell")


async def test_notify_panel_manager_sync_delete_and_toggle(db: Database):
    panel = await db.add_notify_panel(100, 555, pinned=True)
    panel = await db.add_notify_panel_item(panel.id, 11, "Releases", emoji="🚀", description="Release pings")
    assert panel is not None

    rel_role = _FakeRole(11, "Releases", position=3)
    admin_role = _FakeRole(12, "Elevated", position=3, perms=discord.Permissions(administrator=True))
    member_top = _FakeRole(1, "Member", position=1)
    member = _FakeMember(42, member_top, roles=[])
    channel = _FakeChannel(555)

    roles_map = {11: rel_role, 12: admin_role}
    guild = SimpleNamespace(
        id=100,
        get_role=lambda rid: roles_map.get(rid),
        get_member=lambda uid: member if uid == 42 else None,
    )
    bot = SimpleNamespace(
        db=db,
        get_channel=lambda cid: channel if cid == 555 else None,
    )
    mgr = NotifyPanelManager(bot)  # type: ignore[arg-type]

    # Initial sync posts & pins message
    panel = await mgr.sync_message(panel)
    assert panel.message_id is not None
    orig_id = panel.message_id
    assert channel.pinned == [orig_id]
    embed = build_panel_embed(panel)
    assert "<@&11>" in (embed.description or "") and "Release pings" in (embed.description or "")
    view = panel_view(panel)
    assert view is not None and view.children[0].custom_id == f"nr:toggle:{panel.id}:11"

    # Toggle ON then OFF
    resp1 = _FakeInteractionResponse()
    await mgr.handle_toggle(
        SimpleNamespace(guild=guild, user=member, response=resp1),  # type: ignore[arg-type]
        panel.id,
        11,
    )
    assert member.added == [11]
    assert "Added" in resp1.sent[0][0] and "<@&11>" in resp1.sent[0][0]
    assert resp1.sent[0][1] is True

    resp2 = _FakeInteractionResponse()
    await mgr.handle_toggle(
        SimpleNamespace(guild=guild, user=member, response=resp2),  # type: ignore[arg-type]
        panel.id,
        11,
    )
    assert member.removed == [11]
    assert "Removed" in resp2.sent[0][0] and "None" in resp2.sent[0][0]

    # Unregistered role custom_id is rejected
    with pytest.raises(UserError, match="no longer configured"):
        await mgr.handle_toggle(
            SimpleNamespace(guild=guild, user=member, response=_FakeInteractionResponse()),  # type: ignore[arg-type]
            panel.id,
            999,
        )

    # Elevated permissions at click time are rejected even if role is on panel
    await db.add_notify_panel_item(panel.id, 12, "Elevated")
    with pytest.raises(UserError, match="safety reasons"):
        await mgr.handle_toggle(
            SimpleNamespace(guild=guild, user=member, response=_FakeInteractionResponse()),  # type: ignore[arg-type]
            panel.id,
            12,
        )

    # Message delete triggers auto-repost with new message ID
    await mgr.message_deleted(orig_id)
    reposted = await db.get_notify_panel(panel.id)
    assert reposted is not None and reposted.message_id is not None and reposted.message_id != orig_id

    # Role delete cleans panel items and edits live message
    await mgr.role_deleted(100, 11)
    after_role_del = await db.get_notify_panel(panel.id)
    assert after_role_del is not None and [it.role_id for it in after_role_del.items] == [12]
    assert channel.edits[-1][0] == reposted.message_id
