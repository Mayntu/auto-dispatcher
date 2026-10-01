"""Event bus interface (CLAUDE.md §15.1). Services depend on this, not on an implementation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel

from app.bus.envelope import Envelope

Handler = Callable[[Envelope], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, topic: str, payload: BaseModel | dict | list, *, corr_id: str | None = None,
                      source: str = "", sim_time: float | None = None) -> None: ...

    async def subscribe(self, pattern: str, handler: Handler) -> None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


def topic_matches(pattern: str, topic: str) -> bool:
    """AMQP topic semantics: '*' is exactly one word, '#' is zero or more words."""
    p, t = pattern.split("."), topic.split(".")

    def match(i: int, j: int) -> bool:
        if i == len(p):
            return j == len(t)
        if p[i] == "#":
            return any(match(i + 1, k) for k in range(j, len(t) + 1))
        return j < len(t) and (p[i] == "*" or p[i] == t[j]) and match(i + 1, j + 1)

    return match(0, 0)
