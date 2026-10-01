"""Planner service: v1 plan, variants on incidents (debounced), apply, what-if, index stream."""

from __future__ import annotations

import asyncio
import logging
import time

from app.bus.base import EventBus
from app.bus.envelope import Envelope
from app.planner.pool import SolverPool, forecast_plan, retime_with_objective
from app.planner.snapshot import build_snapshot
from app.planner.strategies import STRATEGIES, durations_for
from app.planner.variants import generate_variants
from app.planner.whatif import run_whatif
from app.railcore.infra import World
from app.railcore.models import Incident, Plan, Variant, WhatIfRequest
from app.railcore.problem import Snapshot

log = logging.getLogger("planner")
STALE_MARGIN_S = 300  # objective units (weighted s): a variant this much worse than keeping the current plan is refused


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
        self._last_refresh = 0.0
        self._forecast_fails = 0

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
                self._forecast_fails = 0
                await self._maybe_refresh(fc)
            else:
                self._forecast_fails += 1
            # the approved plan no longer fits where the trains are (its order cycles, or the field has
            # stopped moving): re-plan from the current state with CP-SAT
            stalled = self.field.get("stalled_s", 0) >= self.settings["planner"]["replan_deviation_s"] * 3
            if self._forecast_fails == 3 or stalled:
                if not any(v.status == "proposed" for v in self.variants.values()) \
                        and (self._gen_task is None or self._gen_task.done()):
                    log.warning("current plan is no longer executable, generating variants")
                    self._dirty = True
                    if self._gen_task is None or self._gen_task.done():
                        self._gen_task = asyncio.create_task(self._generate_loop())
            src = self.forecast or self.plan
            await self.bus.publish("kpi.index", {
                "index": src.index.model_dump(mode="json"), "kpi": src.kpi.model_dump(mode="json"),
                "plan_version": self.version, "last_solve_ms": self.last_solve_ms, "forecast_ok": fc is not None,
                "plan_broken": self._forecast_fails >= 3,
            }, source="planner", sim_time=self.field["sim_time"])

    async def _maybe_refresh(self, fc: Plan) -> None:
        """Refresh (§12.6): trains drifted from the plan but the order holds -> re-time the plan automatically.
        The version is kept: the order (what the dispatcher approved) did not change, so variants stay valid."""
        limit = self.settings["planner"]["replan_deviation_s"]
        # only lagging counts: a train held before an obstacle looks "ahead" until the plan lets it go
        drift = max((t["delay_s"] for t in self.field["trains"] if t["on_field"]), default=0)
        new_trains = {e.train_id for e in fc.entries} - {e.train_id for e in self.plan.entries}
        if (drift <= limit and not new_trains) or time.monotonic() - self._last_refresh < 5 \
                or (self._gen_task and not self._gen_task.done()):
            return
        self._last_refresh = time.monotonic()
        plan = fc.model_copy(update={"version": self.version, "base_version": self.plan.base_version,
                                     "strategy": self.plan.strategy, "solver": "refresh"})
        self.plan = plan
        await self.bus.publish("plan.refreshed", plan, source="planner", sim_time=plan.created_at)
        log.info("plan v%d re-timed: max lag %.0f s, entered horizon %s", self.version, drift, sorted(new_trains))

    # ---- commands ---------------------------------------------------------------------------
    async def _on_cmd(self, env: Envelope) -> None:
        cmd = env.type.rsplit(".", 1)[-1]
        if cmd == "apply":
            await self._apply(env)
        elif cmd == "replan":
            self._dirty = True
            if self._gen_task is None or self._gen_task.done():
                self._gen_task = asyncio.create_task(self._generate_loop())
            await self._reply(env, ok=True, code=202)
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
        # the variant was solved a while ago; keep its order (the decision) and re-time it from now
        snap = self.snapshot().model_copy(update={"hint": v.plan.entries})
        durations = durations_for(STRATEGIES[v.strategy], snap.incidents, self.settings)
        # compare like for like: the variant's order and the current one, both re-timed from now and scored
        # with the objective the solver optimised for this strategy
        new = retime_with_objective(snap, self.settings, v.strategy, durations)
        cur = retime_with_objective(self.snapshot(), self.settings, v.strategy, durations)
        plan = new[0] if new else None
        current = cur[0] if cur else None
        reason = None
        if plan is None:  # the order no longer fits where the trains are now
            reason = "Поезда уже разъехались не так, как предполагал вариант — пересчитываю варианты"
        elif cur is not None and new[1] > cur[1] + STALE_MARGIN_S:
            reason = (f"Пока вариант ждал решения, он стал хуже текущего плана (задержка "
                      f"{round(plan.kpi.total_delay_s / 60)} мин против {round(current.kpi.total_delay_s / 60)}) — "
                      f"пересчитываю варианты")
        if reason:
            for other in self.variants.values():  # the whole batch was computed for the same, now outdated state
                if other.status == "proposed":
                    other.status = "stale"
            await self._publish_variants()
            await self._on_cmd(env.model_copy(update={"type": "cmd.planner.replan", "corr_id": None}))
            return await self._reply(env, ok=False, code=409, reason=reason)
        plan.strategy, plan.solver, plan.solve_ms = v.strategy, v.plan.solver, v.plan.solve_ms
        plan.kpi.robust_total_delay_s = v.plan.kpi.robust_total_delay_s
        await self._approve(plan, base)
        for other in self.variants.values():
            other.status = "applied" if other.id == vid else ("stale" if other.status == "proposed" else other.status)
        await self._publish_variants()
        log.info("variant %s (%s) applied as plan v%d", vid, v.strategy, self.version)
        await self._reply(env, ok=True, code=200, plan_version=self.version, index=plan.index.value,
                          total_delay_s=plan.kpi.total_delay_s)

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
