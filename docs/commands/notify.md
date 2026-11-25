# `/notify` Commands

Post an edit-in-place **Notification Roles** button board in a channel so community members can self-assign or opt out of ping roles with a single click, without needing staff to hand out roles manually.

- **Modules:** `bot/cogs/notify.py`, `bot/notify.py`
- **Index:** [All Commands](../README.md#command-reference) · [Management Guide](../management.md)

---

## Summary

| Command | Admin Only | Description |
|---|---|---|
| `/notify create <channel> [title] [description] [pin]` | Yes | Post a new notification role button panel |
| `/notify add <panel> <role> [label] [emoji] [description]` | Yes | Add or update a role toggle button on a panel |
| `/notify remove-role <panel> <role>` | Yes | Remove a role button from a panel |
| `/notify edit <panel> [title] [description] [pin]` | Yes | Edit the panel title, intro text, or pin setting |
| `/notify list` | Yes | List all notification panels and configured roles |
| `/notify delete <panel> [delete_message]` | Yes | Delete a panel (and optionally its Discord message) |

---

## How It Works & Permissions

- **Bot permission required:** The bot needs **Manage Roles** in the server, and its highest role must be **positioned above** every notification role in **Server Settings → Roles**.
- **Self-assignment safety guards:** The bot refuses `@everyone`, bot/integration-managed roles, any role with moderation or admin permissions (`Administrator`, `Manage Server`, `Manage Roles`, `Manage Channels`, `Manage Webhooks`, `Ban Members`, `Kick Members`, `Moderate Members`), and any role at or above the invoking admin's highest role (unless the caller is the server owner).
- **Tamper-resistant buttons:** Clicking a button (`nr:toggle:<panel_id>:<role_id>`) only toggles roles explicitly registered on that panel in SQLite and re-validates permissions at click time.
- **Auto-recovery:** If someone manually deletes a panel message, the bot automatically re-posts it. If a configured Discord role is deleted from the server, it is automatically removed from any panels and the panel embeds refresh.
- **Limits:** Discord allows up to **25** role buttons per panel.

---

## Command Reference

### `/notify create`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `channel` (`discord.TextChannel`, required) — Channel to post the panel in.
  - `title` (`str`, default `"🔔 Notification Roles"`) — Panel embed title.
  - `description` (`str`, default `"Click a button below to opt in or out of notification pings."`) — Intro text above the role list.
  - `pin` (`bool`, default `False`) — Pin the posted panel message (requires `Manage Messages` or `Pin Messages`).

### `/notify add`

- **Permissions:** Bot admin. Server-only.
- **Parameters:**
  - `panel` (`int`, required, autocompleted) — Target notification panel.
  - `role` (`discord.Role`, required) — Plain notification role members can toggle. Re-adding an existing role updates its button label, emoji, and description.
  - `label` (`str`, optional) — Button label (defaults to the role name; max 80 chars).
  - `emoji` (`str`, optional) — Standard Unicode emoji (e.g. `🔔`, `🚀`) or custom server emoji (`<:name:id>`).
  - `description` (`str`, optional) — Short explanation shown next to the role in the embed (max 200 chars).

### `/notify remove-role`

Remove a role's button and embed line from the panel and immediately update the live message.

### `/notify edit`

Update `title`, `description`, or `pin` on an existing panel and immediately update (and pin/unpin) the live message.

### `/notify list`

List every notification panel in the server with direct jump links and configured role mentions.

### `/notify delete`

Remove a panel from the bot. When `delete_message` is `True` (default), deletes its Discord message too.

---

## End-to-End Setup Example

Create roles `@Releases` and `@PR Updates` in **Server Settings → Roles** (below the bot's highest role, with **Allow anyone to `@mention`** optional since the bot grants explicit role pings), then:

```text
/notify create       channel:#roles  title:"🔔 Pick Your Pings"  pin:True
/notify add          panel:#1  role:@Releases    label:Releases    emoji:🚀  description:"New stable and beta releases"
/notify add          panel:#1  role:@PR Updates  label:PR Updates  emoji:🔀  description:"New pull request cards"

/route edit          route:#1    mention:@Releases
/pr edit             tracker:#1  mention:@PR Updates
```

Now members can click **🚀 Releases** or **🔀 PR Updates** in `#roles` whenever they want to opt in or out of pings.
