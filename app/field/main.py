"""Field service entrypoint (CLAUDE.md §5, §11).

    BUS=rabbit python -m app.field.main

Owns field truth: movement, signals, routes, incidents, safety monitor. Publishes `field.state`
at 2 Hz and the incident/DC topics; listens to `plan.*` and `cmd.field.*`.
"""

from __future__ import annotations

import asyncio
import logging

from app.bus.factory import build_bus
from app.common.config import get_env, load_settings
from app.common.logging import setup_logging
from app.common.runtime import run_forever
from app.field.sim import FieldService, FieldSim
from app.railcore.infra import get_world
from app.railcore.running_time import RunningTimes

log = logging.getLogger("field.main")


async def run() -> None:
    env = get_env()
    setup_logging("field", env.log_level)
    settings = load_settings()
    world = get_world()
    bus = build_bus(env)
    await bus.start()
    service = FieldService(bus, FieldSim(world, RunningTimes(world), settings), settings)
    await service.start()
    log.info("field service started (bus=%s)", env.bus)
    try:
        await run_forever()
    finally:
        await service.stop()
        await bus.close()
        log.info("field service stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
