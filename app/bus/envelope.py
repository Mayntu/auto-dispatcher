"""Bus envelope (CLAUDE.md §15.2)."""

from __future__ import annotations

import time
import uuid

from pydantic import BaseModel, Field


class Envelope(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str
    source: str
    corr_id: str | None = None
    ts_wall: float = Field(default_factory=time.time)
    sim_time: float | None = None
    schema_: int = Field(1, alias="schema")
    payload: dict | list = {}

    model_config = {"populate_by_name": True}
