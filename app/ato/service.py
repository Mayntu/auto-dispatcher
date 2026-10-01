"""ATO service: eco-driving profiles synced to the approved plan (CLAUDE.md §13).

Listens to `plan.approved` / `plan.refreshed` / `cmd.ato.*`, keeps the latest `field.state`,
and publishes `ato.profiles` with one `SpeedProfile` per train currently on the field.
Profiles are CPU-bounded, so each is computed in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging

from app.bus.base import EventBus
from app.bus.envelope import Envelope
from app.railcore.eco import train_profile
from app.railcore.infra import World
from app.railcore.models import Plan, SpeedProfile, TrainState
from app.railcore.running_time import RunningTimes

log = logging.getLogger("ato")


class AtoService:
    def __init__(self, bus: EventBus, world: World, settings: dict):
        self.bus = bus
        self.world = world
        self.settings = settings
        self.rts = RunningTimes(world)
        self.plan: Plan | None = None
        self.field: dict | None = None
        self._computed_version = -1
        self._dirty = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.bus.subscribe("plan.approved", self._on_plan)
        await self.bus.subscribe("plan.refreshed", self._on_plan)
        await self.bus.subscribe("field.state", self._on_field)
        await self.bus.subscribe("cmd.ato.*", self._on_cmd)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _on_plan(self, env: Envelope) -> None:
        self.plan = Plan.model_validate(env.payload)
        self._dirty.set()

    async def _on_field(self, env: Envelope) -> None:
        self.field = env.payload
        if self.plan is not None and self.field.get("plan_version") != self._computed_version:
            self._dirty.set()

    async def _on_cmd(self, env: Envelope) -> None:
        cmd = env.type.rsplit(".", 1)[-1]
        if cmd == "recompute":
            self._dirty.set()
            await self.bus.publish("ato.command_result", {"ok": True, "command": cmd},
                                   corr_id=env.corr_id, source="ato")
        else:
            await self.bus.publish("ato.command_result", {"ok": False, "reason": f"unknown command {cmd}"},
                                   corr_id=env.corr_id, source="ato")

    async def _loop(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await asyncio.sleep(0.2)  # coalesce rapid plan+state updates
            try:
                await self._recompute()
            except Exception:
                log.exception("ato recompute failed")

    async def _recompute(self) -> None:
        if self.plan is None or self.field is None:
            return
        plan, version = self.plan, self.plan.version
        states = [TrainState.model_validate(t) for t in self.field["trains"]]
        targets = [st for st in states if st.on_field and st.train_id in self.world.trains]
        results = await asyncio.gather(
            *(asyncio.to_thread(train_profile, self.world, self.rts, plan, st,
                                self.world.trains[st.train_id], version) for st in targets),
            return_exceptions=True,
        )
        profiles = [r for r in results if isinstance(r, SpeedProfile)]
        for r in results:
            if isinstance(r, Exception):
                log.warning("ato profile failed: %s", r)
        self._computed_version = version
        await self.bus.publish("ato.profiles", [p.model_dump(mode="json") for p in profiles],
                               source="ato", sim_time=self.field.get("sim_time"))
        log.info("ato profiles: %d / %d trains (plan v%d)", len(profiles), len(targets), version)
