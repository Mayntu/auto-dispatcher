"""ATO service entrypoint (CLAUDE.md §5, §13).

    BUS=rabbit python -m app.ato.main
"""

from __future__ import annotations

import asyncio
import logging

from app.ato.service import AtoService
from app.bus.factory import build_bus
from app.common.config import get_env, load_settings
from app.common.logging import setup_logging
from app.common.runtime import run_forever
from app.railcore.infra import get_world

log = logging.getLogger("ato.main")


async def run() -> None:
    env = get_env()
    setup_logging("ato", env.log_level)
    settings = load_settings()
    world = get_world()
    bus = build_bus(env)
    await bus.start()
    service = AtoService(bus, world, settings)
    await service.start()
    log.info("ato service started (bus=%s)", env.bus)
    try:
        await run_forever()
    finally:
        await service.stop()
        await bus.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
