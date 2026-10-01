"""All-in-one runner (BUS=memory): field + planner + API in one process.

    python -m app.all  ->  http://localhost:8000  (UI),  /docs  (Swagger)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.main import create_app, subscribe_api
from app.bus.memory import MemoryBus
from app.common.config import get_env, load_settings
from app.common.logging import setup_logging
from app.field.sim import FieldService, FieldSim
from app.planner.main import PlannerService
from app.planner.pool import SolverPool
from app.railcore.infra import get_world
from app.railcore.running_time import RunningTimes


def build_app(settings: dict | None = None) -> FastAPI:
    settings = settings or load_settings()
    world = get_world()
    bus = MemoryBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = SolverPool(settings["planner"]["workers"])
        await pool.warm(settings["planner"]["workers"])
        await bus.start()
        await subscribe_api(bus, app.state.cache, app.state.ws, app.state.requester)
        field = FieldService(bus, FieldSim(world, RunningTimes(world), settings), settings)
        planner = PlannerService(bus, world, settings, pool)
        await field.start()
        await planner.start()
        app.state.field, app.state.planner = field, planner
        try:
            yield
        finally:
            await field.stop()
            await planner.stop()
            await bus.close()
            pool.shutdown()

    return create_app(bus, world, settings, lifespan)


def main() -> None:
    env = get_env()
    setup_logging("all", env.log_level)
    uvicorn.run(build_app(), host=env.host, port=env.port, log_level=env.log_level.lower())


if __name__ == "__main__":
    main()
