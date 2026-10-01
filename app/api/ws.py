"""WebSocket fan-out with conflation: a slow client gets only the latest field.state, never a backlog."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque

from fastapi import WebSocket, WebSocketDisconnect

from app.api.state_cache import StateCache
from app.bus.envelope import Envelope

log = logging.getLogger("ws")
FORWARD = ("field.state", "kpi.index", "plan.approved", "plan.refreshed", "journal.entry", "planner.variants", "planner.metrics",
           "incident.created", "incident.resolved", "dc.command_result", "safety.violation", "field.train_event")
MAX_QUEUE = 500


class Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.state: str | None = None
        self.queue: deque[str] = deque(maxlen=MAX_QUEUE)
        self.wake = asyncio.Event()
        self.p95_ms: float | None = None

    def push(self, topic: str, text: str) -> None:
        if topic == "field.state":
            self.state = text
        else:
            self.queue.append(text)
        self.wake.set()

    async def sender(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.state is not None or self.queue:
                if self.state is not None:
                    text, self.state = self.state, None
                    await self.ws.send_text(text)
                if self.queue:
                    await self.ws.send_text(self.queue.popleft())


class WSManager:
    def __init__(self, cache: StateCache):
        self.cache = cache
        self.clients: set[Client] = set()

    async def on_event(self, env: Envelope) -> None:
        if not self.clients or env.type not in FORWARD:
            return
        text = env.model_dump_json(by_alias=True)
        for c in list(self.clients):
            c.push(env.type, text)

    def p95_ms(self) -> float | None:
        vals = [c.p95_ms for c in self.clients if c.p95_ms is not None]
        return max(vals) if vals else None

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        client = Client(ws)
        await ws.send_text(json.dumps({"type": "snapshot", "payload": self.cache.snapshot()}, ensure_ascii=False))
        self.clients.add(client)
        sender = asyncio.create_task(client.sender())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("op") == "latency":
                    client.p95_ms = float(msg.get("p95_ms") or 0)
                elif msg.get("op") == "ping":
                    client.push("pong", json.dumps({"type": "pong", "t": msg.get("t")}))
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            self.clients.discard(client)
            sender.cancel()
