"""RabbitMQ event bus (CLAUDE.md §15.2, §15.4).

One topic exchange (`rail`). Semantics of queues:
  * event subscriptions -> a private exclusive auto-delete queue per subscription, so every
    service gets its own copy (fan-out). State is published as full snapshots, so a message
    lost while a service is down is harmless (§15.4).
  * command subscriptions `cmd.<service>.*` -> one shared durable queue `cmd.<service>` with
    competing consumers, so a command is handled exactly once even with several replicas.

`aio_pika` is imported lazily so `BUS=memory` works without RabbitMQ installed.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from app.bus.base import Handler
from app.bus.envelope import Envelope

log = logging.getLogger("bus.rabbit")


class RabbitBus:
    def __init__(self, url: str, exchange: str = "rail"):
        self.url = url
        self.exchange_name = exchange
        self._conn = None
        self._chan = None
        self._exchange = None
        self._consumers: list[tuple[object, str]] = []

    async def start(self) -> None:
        import aio_pika

        self._conn = await aio_pika.connect_robust(self.url)
        self._chan = await self._conn.channel()
        await self._chan.set_qos(prefetch_count=200)
        self._exchange = await self._chan.declare_exchange(
            self.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
        )
        log.info("rabbit bus connected to %s (exchange=%s)", self.url, self.exchange_name)

    async def close(self) -> None:
        for queue, tag in self._consumers:
            try:
                await queue.cancel(tag)
            except Exception:  # connection may already be gone
                pass
        self._consumers.clear()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def publish(self, topic: str, payload: BaseModel | dict | list, *, corr_id: str | None = None,
                      source: str = "", sim_time: float | None = None) -> None:
        import aio_pika

        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        env = Envelope(type=topic, source=source, corr_id=corr_id, sim_time=sim_time, payload=payload)
        message = aio_pika.Message(
            body=env.model_dump_json(by_alias=True).encode(),
            content_type="application/json",
            correlation_id=corr_id,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await self._exchange.publish(message, routing_key=topic)

    async def subscribe(self, pattern: str, handler: Handler) -> None:
        queue = await self._declare_queue(pattern)
        await queue.bind(self._exchange, routing_key=pattern)

        async def on_message(message) -> None:  # aio_pika passes the IncomingMessage
            try:
                env = Envelope.model_validate_json(message.body)
            except Exception:
                log.exception("dropping malformed message on %s", pattern)
                await message.ack()
                return
            try:
                await handler(env)
            except Exception:  # a broken handler must not kill the consumer
                log.exception("handler for %s failed on %s", pattern, env.type)
            finally:
                await message.ack()

        tag = await queue.consume(on_message)
        self._consumers.append((queue, tag))

    async def _declare_queue(self, pattern: str):
        if pattern.startswith("cmd."):
            service = pattern.split(".")[1]
            return await self._chan.declare_queue(f"cmd.{service}", durable=True)
        return await self._chan.declare_queue(None, exclusive=True, auto_delete=True)
