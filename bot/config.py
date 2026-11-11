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
    poll_interval: int = 300
    database_path: str = "data/tracker.db"
    owner_ids: set[int] = field(default_factory=set)
    dev_guild_id: int | None = None
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        return cls(
            discord_token=token,
            github_token=os.getenv("GITHUB_TOKEN", "").strip() or None,
            poll_interval=max(60, int(os.getenv("POLL_INTERVAL", "300") or 300)),
            database_path=os.getenv("DATABASE_PATH", "data/tracker.db"),
            owner_ids=_int_list(os.getenv("BOT_OWNER_IDS")),
            dev_guild_id=int(dev_guild) if dev_guild else None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
