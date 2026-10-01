"""Bus factory: pick an `EventBus` implementation from the environment (CLAUDE.md §5, §15.1)."""

from __future__ import annotations

from app.bus.base import EventBus
from app.common.config import Env, get_env


def build_bus(env: Env | None = None) -> EventBus:
    env = env or get_env()
    if env.is_rabbit:
        from app.bus.rabbit import RabbitBus

        return RabbitBus(env.rabbit_url, env.rabbit_exchange)
    from app.bus.memory import MemoryBus

    return MemoryBus()
