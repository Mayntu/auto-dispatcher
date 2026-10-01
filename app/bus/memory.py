"""In-process bus: one queue + consumer task per subscription, so a slow handler never blocks publishers."""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel

from app.bus.base import Handler, topic_matches
from app.bus.envelope import Envelope

log = logging.getLogger("bus")


class MemoryBus:
    def __init__(self) -> None:
        self._subs: list[tuple[str, asyncio.Queue]] = []
        self._tasks: list[asyncio.Task] = []

    async def publish(self, topic: str, payload: BaseModel | dict | list, *, corr_id: str | None = None,
                      source: str = "", sim_time: float | None = None) -> None:
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        env = Envelope(type=topic, source=source, corr_id=corr_id, sim_time=sim_time, payload=payload)
        for pattern, queue in self._subs:
            if topic_matches(pattern, topic):
                queue.put_nowait(env)

    async def subscribe(self, pattern: str, handler: Handler) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.append((pattern, queue))
        self._tasks.append(asyncio.create_task(self._consume(pattern, queue, handler)))

    async def _consume(self, pattern: str, queue: asyncio.Queue, handler: Handler) -> None:
        while True:
            env = await queue.get()
            try:
                await handler(env)
            except Exception:  # a broken handler must not kill the bus
                log.exception("handler for %s failed on %s", pattern, env.type)

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._subs.clear()
