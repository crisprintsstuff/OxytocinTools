from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _int_list(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(part) for part in raw.replace(" ", "").split(",") if part}


@dataclass(frozen=True)
class Config:
    discord_token: str
    github_token: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    poll_interval: int = 300
    database_path: str = "data/tracker.db"
    owner_ids: set[int] = field(default_factory=set)
    dev_guild_id: int | None = None
    log_level: str = "INFO"
    issue_cooldown: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        app_id = os.getenv("GITHUB_APP_ID", "").strip() or None
        private_key = None
        if app_id:
            key_path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
            inline = os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip()
            if key_path:
                try:
                    with open(key_path, encoding="utf-8") as fh:
                        private_key = fh.read()
                except OSError as exc:
                    raise SystemExit(f"Can't read GITHUB_APP_PRIVATE_KEY_PATH ({key_path}): {exc}")
            elif inline:
                private_key = inline.replace("\\n", "\n")
            else:
                raise SystemExit("GITHUB_APP_ID is set, but neither GITHUB_APP_PRIVATE_KEY_PATH "
                                 "nor GITHUB_APP_PRIVATE_KEY is.")
        return cls(
            discord_token=token,
            github_token=os.getenv("GITHUB_TOKEN", "").strip() or None,
            github_app_id=app_id,
            github_app_private_key=private_key,
            poll_interval=max(60, int(os.getenv("POLL_INTERVAL", "300") or 300)),
            database_path=os.getenv("DATABASE_PATH", "data/tracker.db"),
            owner_ids=_int_list(os.getenv("BOT_OWNER_IDS")),
            dev_guild_id=int(dev_guild) if dev_guild else None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            issue_cooldown=max(0, int(os.getenv("ISSUE_COOLDOWN", "60") or 60)),
        )
