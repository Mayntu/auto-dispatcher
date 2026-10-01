"""Process configuration (env) and editable settings (settings.default.json)."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- bus / transport (CLAUDE.md §15) ---
    bus: str = "memory"  # memory | rabbit
    rabbit_url: str = "amqp://guest:guest@localhost:5672/"
    rabbit_exchange: str = "rail"
    database_url: str | None = None

    sim_epoch: datetime = datetime.fromisoformat("2026-10-02T07:55:00+05:00")
    data_dir: Path = ROOT / "data"
    web_dir: Path = ROOT / "web-mvp"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    service: str = "app"

    # --- auth (CLAUDE.md §16.1); disabled by default for the MVP dashboard ---
    auth_enabled: bool = False
    jwt_secret: str = "change-me"
    jwt_ttl_h: int = 12
    dispatcher_login: str = "dispatcher"
    dispatcher_password: str = "dispatcher"
    admin_login: str = "admin"
    admin_password: str = "admin"

    @property
    def is_rabbit(self) -> bool:
        return self.bus.lower() == "rabbit"


@lru_cache
def get_env() -> Env:
    return Env()


def load_settings(data_dir: Path | None = None) -> dict:
    path = (data_dir or get_env().data_dir) / "settings.default.json"
    return json.loads(path.read_text(encoding="utf-8"))
