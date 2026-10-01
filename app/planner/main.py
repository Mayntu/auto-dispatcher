"""Planner service: v1 plan, variants on incidents (debounced), apply, what-if, index stream."""

from __future__ import annotations

import asyncio
import logging
import time

from app.bus.base import EventBus
from app.bus.envelope import Envelope
from app.planner.pool import SolverPool, forecast_plan
from app.planner.snapshot import build_snapshot
from app.planner.variants import generate_variants
from app.planner.whatif import run_whatif
from app.railcore.infra import World
from app.railcore.models import Incident, Plan, Variant, WhatIfRequest
from app.railcore.problem import Snapshot

log = logging.getLogger("planner")


class PlannerService:
    def __init__(self, bus: EventBus, world: World, settings: dict, pool: SolverPool):
        self.bus = bus
        self.world = world
        self.settings = settings
        self.pool = pool
        self.plan: Plan | None = None
        self.version = 0
        self.variants: dict[str, Variant] = {}
        self.incidents: dict[str, Incident] = {}
        self.field: dict | None = None
        self.forecast: Plan | None = None
        self.last_solve_ms = 0
        self._first_state = asyncio.Event()
        self._dirty = False
        self._gen_task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.bus.subscribe("field.state", self._on_field)
        await self.bus.subscribe("incident.*", self._on_incident)
        await self.bus.subscribe("cmd.planner.*", self._on_cmd)
        await asyncio.wait_for(self._first_state.wait(), timeout=10)
        t0 = time.perf_counter()
        res = await self.pool.solve(self.snapshot().model_dump(mode="json"), "balanced", self.settings)
        plan = Plan.model_validate(res["plan"])
        self.last_solve_ms = round((time.perf_counter() - t0) * 1000)
        await self._approve(plan, None)
        log.info("plan v1 built in %d ms (%s), index %.1f", self.last_solve_ms, res["status"], plan.index.value)
        self._tasks.append(asyncio.create_task(self._index_loop()))

    async def stop(self) -> None:
        for t in self._tasks + ([self._gen_task] if self._gen_task else []):
            t.cancel()

    def snapshot(self) -> Snapshot:
        return build_snapshot(self.field, self.world.timetable, list(self.incidents.values()), self.plan)

    # ---- events -----------------------------------------------------------------------------
    async def _on_field(self, env: Envelope) -> None:
        self.field = env.payload
        self._first_state.set()

    async def _on_incident(self, env: Envelope) -> None:
        inc = Incident.model_validate(env.payload)
        if inc.status == "active":
            self.incidents[inc.id] = inc
        else:
            self.incidents.pop(inc.id, None)
        self._dirty = True
        if self._gen_task is None or self._gen_task.done():
            self._gen_task = asyncio.create_task(self._generate_loop())

    async def _generate_loop(self) -> None:
        """Debounce: incidents arriving within `debounce_ms` (or during a solve) are planned together."""
        while self._dirty:
            await asyncio.sleep(self.settings["planner"]["debounce_ms"] / 1000)
            self._dirty = False
            try:
                await self._generate()
            except Exception:
                log.exception("variant generation failed")

    async def _generate(self) -> None:
        if self.plan is None:
            return
        t0 = time.perf_counter()
        snap = self.snapshot()
        forecast = forecast_plan(snap, self.settings) or self.forecast
        variants = await generate_variants(self.pool, self.world, snap, self.settings, self.version, forecast)
        self.last_solve_ms = round((time.perf_counter() - t0) * 1000)
        for v in self.variants.values():
            if v.status == "proposed":
                v.status = "stale"
        self.variants = {v.id: v for v in variants}
        await self._publish_variants()
        await self.bus.publish("planner.metrics", {"solve_ms": self.last_solve_ms, "kind": "variants",
                                                   "strategies": [v.strategy for v in variants]},
                               source="planner", sim_time=snap.now)
        log.info("3 variants in %d ms for incidents %s", self.last_solve_ms, snap and [i.id for i in snap.incidents])

    async def _publish_variants(self) -> None:
        await self.bus.publish("planner.variants", {
            "incident_ids": sorted(self.incidents), "base_plan_version": self.version,
            "solve_ms": self.last_solve_ms, "variants": [v.model_dump(mode="json") for v in self.variants.values()],
        }, source="planner")

    async def _approve(self, plan: Plan, base_version: int | None) -> None:
        self.version += 1
        plan.version, plan.base_version = self.version, base_version
        self.plan = plan
        await self.bus.publish("plan.approved", plan, source="planner", sim_time=plan.created_at)

    async def _index_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.plan is None or self.field is None:
                continue
            fc = forecast_plan(self.snapshot(), self.settings)
            if fc is not None:
                self.forecast = fc
            src = self.forecast or self.plan
            await self.bus.publish("kpi.index", {
                "index": src.index.model_dump(mode="json"), "kpi": src.kpi.model_dump(mode="json"),
                "plan_version": self.version, "last_solve_ms": self.last_solve_ms, "forecast_ok": fc is not None,
            }, source="planner", sim_time=self.field["sim_time"])

    # ---- commands ---------------------------------------------------------------------------
    async def _on_cmd(self, env: Envelope) -> None:
        cmd = env.type.rsplit(".", 1)[-1]
        if cmd == "apply":
            await self._apply(env)
        elif cmd == "whatif":
            await self._whatif(env)
        else:
            await self._reply(env, ok=False, code=400, reason=f"unknown command {cmd}")

    async def _reply(self, env: Envelope, **result) -> None:
        await self.bus.publish("planner.command_result", result, corr_id=env.corr_id, source="planner")

    async def _apply(self, env: Envelope) -> None:
        vid, base = env.payload.get("variant_id"), env.payload.get("base_plan_version")
        v = self.variants.get(vid)
        if v is None:
            return await self._reply(env, ok=False, code=404, reason="Вариант не найден")
        if v.status != "proposed" or base != self.version or v.base_plan_version != self.version:
            if v.status == "proposed":
                v.status = "stale"
            return await self._reply(env, ok=False, code=409,
                                     reason=f"Вариант устарел: текущая версия плана {self.version}")
        plan = v.plan.model_copy(deep=True)
        await self._approve(plan, base)
        for other in self.variants.values():
            other.status = "applied" if other.id == vid else ("stale" if other.status == "proposed" else other.status)
        await self._publish_variants()
        log.info("variant %s (%s) applied as plan v%d", vid, v.strategy, self.version)
        await self._reply(env, ok=True, code=200, plan_version=self.version)

    async def _whatif(self, env: Envelope) -> None:
        req = WhatIfRequest.model_validate(env.payload)
        if self.plan is None:
            return await self._reply(env, ok=False, code=409, reason="План ещё не построен")
        snap = self.snapshot()
        try:
            res = await run_whatif(self.pool, self.world, snap, req, self.settings)
        except (ValueError, KeyError) as e:
            await self.bus.publish("planner.whatif.result", {"ok": False, "reason": str(e)},
                                   corr_id=env.corr_id, source="planner")
            return
        await self.bus.publish("planner.whatif.result", {"ok": True, **res.model_dump(mode="json")},
                               corr_id=env.corr_id, source="planner")
