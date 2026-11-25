from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_path(raw: str) -> str:
    if raw == ":memory:" or os.path.isabs(raw):
        return raw
    return str(PROJECT_ROOT / raw)


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
    config_backup_path: str | None = None
    backup_count: int = 7
    owner_ids: set[int] = field(default_factory=set)
    dev_guild_id: int | None = None
    log_level: str = "INFO"
    issue_cooldown: int = 60
    release_notes_wait: int = 30  # minutes

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        load_dotenv(PROJECT_ROOT / ".env")
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        app_id = os.getenv("GITHUB_APP_ID", "").strip() or None
        private_key = None
        if app_id:
            raw_key_path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
            inline = os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip()
            if raw_key_path:
                key_path = _resolve_path(raw_key_path)
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
        raw_db_path = os.getenv("DATABASE_PATH", "data/tracker.db").strip() or "data/tracker.db"
        database_path = _resolve_path(raw_db_path)
        raw_backup = os.getenv("CONFIG_BACKUP_PATH", "").strip()
        if raw_backup.lower() in ("off", "none", "false", "0"):
            config_backup_path = None
        elif raw_backup:
            config_backup_path = _resolve_path(raw_backup)
        elif database_path != ":memory:":
            config_backup_path = os.path.join(
                os.path.dirname(os.path.abspath(database_path)), "config-backup.json"
            )
        else:
            config_backup_path = None
        backup_count = max(0, int(os.getenv("DB_BACKUP_COUNT", "7") or 7))
        return cls(
            discord_token=token,
            github_token=os.getenv("GITHUB_TOKEN", "").strip() or None,
            github_app_id=app_id,
            github_app_private_key=private_key,
            poll_interval=max(60, int(os.getenv("POLL_INTERVAL", "300") or 300)),
            database_path=database_path,
            config_backup_path=config_backup_path,
            backup_count=backup_count,
            owner_ids=_int_list(os.getenv("BOT_OWNER_IDS")),
            dev_guild_id=int(dev_guild) if dev_guild else None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            issue_cooldown=max(0, int(os.getenv("ISSUE_COOLDOWN", "60") or 60)),
            release_notes_wait=max(0, int(os.getenv("RELEASE_NOTES_WAIT", "30") or 30)),
        )
