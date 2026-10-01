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

    bus: str = "memory"
    sim_epoch: datetime = datetime.fromisoformat("2026-10-02T07:55:00+05:00")
    data_dir: Path = ROOT / "data"
    web_dir: Path = ROOT / "web-mvp"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"


@lru_cache
def get_env() -> Env:
    return Env()


def load_settings(data_dir: Path | None = None) -> dict:
    path = (data_dir or get_env().data_dir) / "settings.default.json"
    return json.loads(path.read_text(encoding="utf-8"))


def segment_clear_s(settings: dict) -> int:
    """Time a single-track segment stays unavailable to an oncoming train after a train arrives: the
    crossing station interval tau_cross plus the direction change are not additive — the larger one counts
    (§12.2, occ_ext)."""
    iv = settings["intervals"]
    return max(iv["tau_cross_s"], iv["direction_change_s"])

