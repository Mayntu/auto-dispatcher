"""Recorder service entrypoint (CLAUDE.md §5, §17).

    BUS=rabbit DATABASE_URL=postgresql://... python -m app.recorder.main

Subscribes to everything (`#`) and writes to TimescaleDB. Without `DATABASE_URL` it does not
start (per §17): the API keeps a ring buffer of recent frames instead.
"""

from __future__ import annotations

import asyncio
import logging

from app.bus.factory import build_bus
from app.common.config import get_env
from app.common.logging import setup_logging
from app.common.runtime import run_forever

log = logging.getLogger("recorder.main")


async def run() -> None:
    env = get_env()
    setup_logging("recorder", env.log_level)
    if not env.database_url:
        log.warning("DATABASE_URL is not set — recorder disabled")
        return
    from app.recorder.writer import Writer

    bus = build_bus(env)
    await bus.start()
    writer = Writer(env.database_url)
    await writer.connect()
    await bus.subscribe("#", writer.on_event)
    log.info("recorder service started (bus=%s)", env.bus)
    try:
        await run_forever()
    finally:
        await bus.close()
        await writer.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
